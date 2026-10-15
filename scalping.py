"""Paper recovery/breakout experiment. Closed pool candles; quote-based marks.

No wallet, orders, inferred candles, intrabar fills or future-data backfills.
See SCALPING-2026-10-15.md for the forward-test hypotheses.
"""
from dataclasses import asdict, replace
import json
import sqlite3
import time
import urllib.parse
from pathlib import Path
from bot import Engine, Config, DataError, USD, SOL, decimal, mint_ok
from active import ActiveMarket as LegacyMarket
from scalping_v1 import ScalpEngine as PreviousScalpEngine

REVISION = 'active-scalp-v2'
LIMITS = {'max_position': (10*USD, 100*USD), 'max_positions': (1, 4),
          'stop_bps': (100, 200), 'take_profit_bps': (200, 600)}
PROFILE = dict(id='active', name='Actief', strategy='Herstel + uitbraak', revision=REVISION,
    timed_exit=False, candle_source='GeckoTerminal · gesloten 1-minuutcandles',
    frequency_target='10–30 instappen per dag is een onderzoeksdoel, geen quotum',
    description='Stijgende trend met eenvoudiger herstel of bevestigde uitbraak. '
    'Liquiditeit vanaf $200.000 en vijfminutenvolume vanaf $10.000. '
    'Maximaal $100 per positie en $2 gepland risico; structurele stop maximaal 2% netto, '
    'winstdoel 2% netto en maximaal 0,8% gemodelleerde retourkosten. '
    'Geen verplichte tijdsverkoop. Afwijzingen tellen kandidaatbeoordelingen, niet unieke munten. '
    'Paperproef: meer trades of winst zijn niet gegarandeerd.')


def migrate(path, target, now):
    """Atomic, once-only policy migration. No funding, forced sells or halt reset."""
    if not Path(path).exists():
        return
    with sqlite3.connect(path, timeout=5) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
        if row is None:
            return
        s = json.loads(row[0])
        if s.get('active_revision', {}).get('id') == REVISION:
            return
        if s['mode'] != 'paper':
            raise ValueError('scalping migration is paper-only')
        old = s['config']
        # Existing positions retain their previous exit contract until naturally closed.
        for p in s['positions'].values():
            p.setdefault('strategy', 'adaptive-exits-v2')
            p.setdefault('legacy_config', old)
        previous = s.get('active_revision', {}).get('id', 'adaptive-exits-v2')
        archives = s.setdefault('active_archives', {})
        archives[previous] = dict(results=s.get('scalp_results'), revision=s.get('active_revision'))
        s['config'] = asdict(target)
        s.update(dashboard_overrides={}, pending={}, history={}, scalp_consumed={}, scalp_losses={})
        s['scalp_entry_attempts'] = {}
        s['active_revision'] = dict(id=REVISION, ts=now, equity=s['equity'],
            realized=s['realized'], closed=s['closed'], legacy_positions=len(s['positions']),
            cash_adjustments=s.get('cash_adjustments', 0))
        s['scalp_results'] = dict(closed=0, wins=0, pnl=0, gross_wins=0, gross_losses=0)
        s['setup_results'] = {}
        s['active_diagnostics'] = {}
        s['dashboard_revision'] = s.get('dashboard_revision', 0)+1
        db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(s),))
        db.execute('INSERT INTO events(ts,kind,body) VALUES (?,?,?)', (now,'STRATEGY_CHANGE',
            json.dumps(dict(previous=old,current=s['config'],policy=PROFILE,
                description=PROFILE['description'],baseline=s['active_revision']))))


def activate_new(store, now):
    s = store.load()
    if s.get('active_revision', {}).get('id') != REVISION:
        with store.db:
            s['active_revision'] = dict(id=REVISION,ts=now,equity=s['equity'],realized=s['realized'],
                                       closed=s['closed'],legacy_positions=0,cash_adjustments=s.get('cash_adjustments',0))
            store.save(s)
            store.event(now,'STRATEGY_CHANGE',current=s['config'],policy=PROFILE,
                        description=PROFILE['description'],baseline=s['active_revision'])


class Candles:
    """Free read-only feed: at most 8 requests/rolling minute, 2 per refresh.

    Resolve a liquid pool belonging to the mint, then request that exact token's
    USD candles (never assume it is the base asset). One-minute provider caching
    is reflected in explicit staleness checks, not relabelled as realtime.
    """
    ROOT = 'https://api.geckoterminal.com/api/v2/networks/solana'
    HEADERS = {'Accept':'application/json;version=20230203'}

    def __init__(self, http, clock=time.time):
        self.http, self.clock = http, clock
        self.pools, self.bars, self.attempts, self.errors = {}, {}, {}, {}
        self.pool_options, self.bad_pools = {}, {}
        self.calls, self.backoff = [], 0

    @staticmethod
    def parse(data, now):
        try:
            rows = data['data']['attributes']['ohlcv_list']
            if not isinstance(rows, list):
                raise ValueError('invalid list')
            bars = {}
            for r in rows:
                if len(r) != 6 or type(r[0]) is not int or r[0] % 60:
                    raise ValueError('invalid candle timestamp')
                t, o, h, l, c, v = r[0], *[decimal(x) for x in r[1:]]
                if min(o,h,l,c) <= 0 or v < 0 or l > min(o,c) or h < max(o,c) or l > h:
                    raise ValueError('invalid candle values')
                if t+60 > now:  # Do not use the still-forming candle.
                    continue
                value = [t,str(o),str(h),str(l),str(c),str(v)]
                if t in bars and bars[t] != value:
                    raise ValueError('conflicting candle')
                bars[t] = value
            return [bars[t] for t in sorted(bars)][-40:]
        except (KeyError, TypeError, ValueError, DataError) as exc:
            raise DataError('invalid_candles') from exc

    def refresh(self, snaps, held=()):
        now = int(self.clock())
        self.calls = [t for t in self.calls if now-t < 60]
        if now < self.backoff:
            return
        # Held tokens get reserved attention; rotate other candidates fairly.
        ranked = sorted(snaps, key=lambda s: (s.mint not in held,-s.volume_5m,s.mint))[:6]
        due = sorted(ranked, key=lambda s: (self.attempts.get(s.mint,0),s.mint not in held))
        begun = self.clock()
        requests = 0
        for s in due:
            now = int(self.clock())
            if len(self.calls) >= 8 or self.clock()-begun >= 8:
                break
            if now-self.attempts.get(s.mint,0) < 60:
                continue
            self.attempts[s.mint] = now
            self.calls.append(now)
            requests += 1
            try:
                pool = self.pools.get(s.mint)
                if not pool or now-pool['ts'] > 3600:
                    data = self.http.json(self.ROOT+'/tokens/'+s.mint+'/pools?page=1',self.HEADERS)
                    if not isinstance(data,dict) or not isinstance(data.get('data'),list):
                        raise DataError('invalid_pool_response')
                    candidates = []
                    for r in data.get('data',[]):
                        a, rel = r['attributes'],r['relationships']
                        tokens = [rel[k]['data']['id'] for k in ('base_token','quote_token')]
                        address = a['address']
                        if ('solana_'+s.mint in tokens and mint_ok(address)
                                and self.bad_pools.get(address,0) <= now
                                and decimal(a.get('reserve_in_usd',0)) >= 200000):
                            activity=decimal((a.get('volume_usd') or {}).get('m5') or 0)
                            candidates.append((activity,decimal(a['reserve_in_usd']),address))
                    if not candidates:
                        raise DataError('no_liquid_candle_pool')
                    addresses=list(dict.fromkeys(x[2] for x in sorted(candidates,reverse=True)))
                    self.pool_options[s.mint]=addresses[1:]
                    self.pools[s.mint] = dict(address=addresses[0],ts=now)
                    self.bars.pop(s.mint,None)
                    # Pool lookup uses this turn's slot. Fetch chart next turn.
                    self.attempts[s.mint] = now-45
                else:
                    query = urllib.parse.urlencode(dict(aggregate=1,limit=40,currency='usd',
                        token=s.mint,include_empty_intervals='false'))
                    data = self.http.json(self.ROOT+'/pools/'+pool['address']+'/ohlcv/minute?'+query,self.HEADERS)
                    self.bars[s.mint] = self.parse(data,int(self.clock()))
                    _,reason=self.get(s.mint,int(self.clock()))
                    if reason:
                        raise DataError(reason)
                self.errors.pop(s.mint,None)
            except (DataError,KeyError,TypeError,ValueError) as exc:
                self.errors[s.mint] = str(exc)[:100]
                self.bars.pop(s.mint,None)
                if '429' in str(exc):
                    self.backoff = now+60
                    break
                if pool and str(exc) in ('invalid_candles','candles_stale','candle_gap',
                                         'inactive_candles','candles_unavailable'):
                    self.bad_pools[pool['address']]=now+300
                    options=self.pool_options.get(s.mint,[])
                    self.pools.pop(s.mint,None)
                    if options:
                        self.pools[s.mint]=dict(address=options.pop(0),ts=now)
                    self.attempts[s.mint]=now-45
            if requests >= 2:
                break
        keep = {s.mint for s in snaps} | set(held)
        self.bad_pools={p:until for p,until in self.bad_pools.items() if until>now}
        for cache in (self.pools,self.bars,self.attempts,self.errors,self.pool_options):
            for mint in list(cache):
                if mint not in keep:
                    del cache[mint]

    def get(self, mint, now):
        bars = self.bars.get(mint,[])
        if len(bars) < 20:
            return [], 'candles_unavailable'
        bars = [b for b in bars if b[0]+60 <= now]
        if len(bars) < 20 or now-(bars[-1][0]+60) > 90:
            return [], 'candles_stale'
        if any(b[0]-a[0] != 60 for a,b in zip(bars[-20:],bars[-19:])):
            return [], 'candle_gap'
        if any(decimal(b[5]) <= 0 for b in bars[-20:]):
            return [], 'inactive_candles'
        return bars[-20:], None

    def health(self, now):
        ready = sum(self.get(m,now)[1] is None for m in self.bars)
        return dict(source='GeckoTerminal',timeframe='1m',ready=ready,tracked=len(self.attempts),
                    errors=dict(self.errors),backoff_until=self.backoff,
                    requests_last_minute=len([t for t in self.calls if now-t<60]))


class ScalpMarket(LegacyMarket):
    def __init__(self, config, http, clock=time.time):
        super().__init__(config,http,clock)
        self.candles = Candles(http,clock)

    def collect(self, held=()):
        # Broad discovery and batch prices; six intensive slots fit the feed budget.
        original = self.config
        self.config = replace(original,max_candidates=80)
        self.watch.setdefault(SOL,int(self.clock())+1800)
        try:
            snaps = super().collect(held)
        finally:
            self.config = original
        candidates = [s for s in snaps if s.mint in held or
            (s.safe and s.liquidity >= original.min_liquidity and
             s.volume_5m >= original.min_volume_5m and
             self.clock()-s.pair_created >= original.min_pair_age_seconds)]
        self.candles.refresh(candidates,held)
        self.discovery['candles'] = self.candles.health(int(self.clock()))
        self.discovery['intensive_cap'] = 6
        return snaps


def ema(values, period):
    result = values[0]
    a = decimal(2)/decimal(period+1)
    for v in values[1:]:
        result += a*(v-result)
    return result


def setup_from_bars(bars, price):
    """Two distinct closed-bar hypotheses, both reconfirmed on fresh quote data."""
    closes = [decimal(b[4]) for b in bars]
    if len(closes) != 20:
        return None, 'candles_unavailable'
    if not (closes[-1] > closes[-16] and ema(closes,5) > ema(closes,15)):
        return None, 'scalp_trend_not_rising'
    last, prev = bars[-1], bars[-2]
    if not (closes[-1] > closes[-2] and closes[-1] > decimal(last[1])
            and decimal(last[5]) >= decimal(prev[5])*decimal('0.8')):
        return None, 'scalp_wait_reclaim'
    if price < closes[-1]*decimal('0.995') or price > closes[-1]*decimal('1.005'):
        return None, 'scalp_reclaim_moved'
    # A breakout clears a 10-bar high; its recent local low defines risk.
    prior = bars[-11:-1]
    high_bar = max(prior, key=lambda b: decimal(b[2]))
    resistance = decimal(high_bar[2])
    if closes[-1] > resistance and price >= resistance:
        kind = 'breakout'
        trough_bar = min(bars[-3:], key=lambda b: decimal(b[3]))
        trigger = resistance
        identity = 'breakout:'+str(high_bar[0])
    else:
        peak_index = max(range(10,18), key=lambda i: decimal(bars[i][2]))
        peak = decimal(bars[peak_index][2])
        trough_rows = bars[peak_index+1:-1]
        if not trough_rows or len(trough_rows)>6:
            return None, 'scalp_wait_pullback'
        trough_bar = min(trough_rows, key=lambda b: decimal(b[3]))
        depth = (peak-decimal(trough_bar[3]))/peak
        if not decimal('0.003') <= depth <= decimal('0.05'):
            return None, 'scalp_wait_pullback'
        kind, trigger = 'recovery', closes[-2]
        if price <= trigger:
            return None, 'scalp_wait_reclaim'
        identity = 'recovery:'+str(bars[peak_index][0])+':'+str(trough_bar[0])
        resistance = peak
    tr = [max(decimal(b[2])-decimal(b[3]),abs(decimal(b[2])-closes[i-1]),
              abs(decimal(b[3])-closes[i-1])) for i,b in enumerate(bars) if i>0]
    buffer = max(price*decimal('0.001'),sum(tr[-14:])/14*decimal('0.15'))
    stop = decimal(trough_bar[3])-buffer
    if stop <= 0 or stop >= price:
        return None, 'scalp_invalid_stop'
    return dict(id=identity, kind=kind, bar=last[0], stop_price=str(stop),
                reclaim=str(trigger), resistance=str(resistance)),None


class ScalpEngine(Engine):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.candles = None
        self.current = {}
        self.consumed = {}
        self.setups = {}
        self.entry_attempts = {}

    def step(self,snapshots,ts,discovery=None):
        self.current = {s.mint:s for s in snapshots}
        self.cycle_event_id = self.store.db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        self.consumed = self.store.load().get('scalp_consumed',{})
        self.entry_attempts = self.store.load().get('scalp_entry_attempts',{})
        self.setups = {}
        return super().step(snapshots,ts,discovery)

    def pending_items(self,pending,current):
        # Repeated quote rejection on the busiest token must not starve others.
        return sorted(pending.items(),key=lambda item: (
            self.entry_attempts.get(item[0],0),
            -current[item[0]].volume_5m if item[0] in current else 0,item[0]))[:1]

    def gate(self,snap,history,ts):
        c = self.c
        if not 0<=ts-snap.observed<=c.sample_ttl_seconds:
            return 'stale_market'
        if snap.safe is not True:
            return 'mint_check:'+snap.safety_reason
        if snap.liquidity<c.min_liquidity:
            return 'insufficient_liquidity'
        if snap.volume_5m<c.min_volume_5m:
            return 'insufficient_volume'
        if not 0<snap.pair_created<=ts-c.min_pair_age_seconds:
            return 'pair_too_young_or_unknown'
        if snap.buys_5m<c.min_buys_5m or snap.buys_5m*10000<c.min_buy_share_bps*max(1,snap.buys_5m+snap.sells_5m):
            return 'weak_buy_activity'
        if self.candles is None:
            return 'candles_unavailable'
        bars,reason = self.candles.get(snap.mint,ts)
        if reason:
            return reason
        price=decimal(snap.price)
        if abs(price/decimal(bars[-1][4])-1)>decimal('0.015'):
            return 'candle_quote_divergence'
        setup,reason=setup_from_bars(bars,price)
        if reason:
            return reason
        if self.consumed.get(snap.mint)==setup['id']:
            return 'scalp_setup_used'
        self.setups[snap.mint]=setup
        return None

    def entry_guard(self,snap,pending,ts):
        reason=super().entry_guard(snap,pending,ts)
        if reason:return reason
        if pending.get('observed') is not None and self.candles:
            _, candle_reason = self.candles.get(snap.mint,self.clock() if self.clock else ts)
            if candle_reason:return candle_reason
        if pending.get('observed') is not None and snap.observed<=pending['observed']:
            return 'awaiting_fresh_confirmation'
        return None

    def entry_cost_guard(self,snap,history,budget,drag):
        setup=self.setups[snap.mint]
        price=decimal(snap.price)
        stop_net=int(decimal(budget-drag)*decimal(setup['stop_price'])/price)
        loss=budget-stop_net
        if loss <= drag or loss*10000>budget*self.c.stop_bps:
            return 'scalp_stop_outside_budget'
        if loss>self.c.risk_per_trade:
            return 'scalp_stop_outside_budget'
        return None

    def position_fields(self,snap,budget,proceeds,ts):
        setup=self.setups[snap.mint]
        floor=int(decimal(proceeds)*decimal(setup['stop_price'])/decimal(snap.price))
        return dict(strategy=REVISION,setup_type=setup['kind'],mint=snap.mint,setup=setup,stop_floor=floor,
                    stop_floor_reason='scalp_structure_stop',take_profit_bps=self.c.take_profit_bps,
                    entry_price=snap.price)

    def on_open(self,state,mint,pos):
        state.setdefault('scalp_consumed',{})[mint]=pos['setup']['id']
        self.consumed[mint]=pos['setup']['id']
        # Keep only a bounded set of recent setup identities.
        state['scalp_consumed']=dict(list(state['scalp_consumed'].items())[-100:])

    def on_close(self,state,mint,pos,pnl,ts):
        if pos.get('strategy')!=REVISION:return
        losses=state.setdefault('scalp_losses',{})
        losses[mint]=losses.get(mint,0)+1 if pnl<=0 else 0
        state['scalp_losses']=dict(list(losses.items())[-100:])
        state['cooldowns'][mint]=ts+(1800 if losses[mint]>=2 else 300 if pnl<=0 else 0)
        r=state.setdefault('scalp_results',dict(closed=0,wins=0,pnl=0,gross_wins=0,gross_losses=0))
        r['closed']+=1;r['wins']+=int(pnl>0);r['pnl']+=pnl
        r['gross_wins']+=max(0,pnl);r['gross_losses']+=max(0,-pnl)
        group=state.setdefault('setup_results',{}).setdefault(pos['setup_type'],
            dict(closed=0,wins=0,pnl=0,gross_wins=0,gross_losses=0))
        group['closed']+=1;group['wins']+=int(pnl>0);group['pnl']+=pnl
        group['gross_wins']+=max(0,pnl);group['gross_losses']+=max(0,-pnl)

    def exit_reason(self,pos,net,ts):
        if pos.get('strategy')!=REVISION:
            legacy=PreviousScalpEngine(self.store,Config(**pos.get('legacy_config',asdict(self.c))),self.quotes)
            legacy.current,legacy.candles=self.current,self.candles
            return legacy.exit_reason(pos,net,ts)
        if net<=pos['stop_floor']:return 'scalp_structure_stop'
        if (net-pos['cost'])*10000>=pos['cost']*pos['take_profit_bps']:return 'take_profit'
        snap=self.current.get(pos['mint'])
        if snap and 0<=ts-snap.observed<=self.c.sample_ttl_seconds and decimal(snap.price)<decimal(pos['setup']['stop_price']):
            return 'scalp_pattern_failed'
        if self.candles:
            bars,reason=self.candles.get(pos['mint'],ts)
            if not reason and bars[-2][0]>=pos['opened']:
                closes=[decimal(b[4]) for b in bars]
                if (closes[-1]<decimal(pos['setup']['reclaim']) and closes[-2]<decimal(pos['setup']['reclaim'])
                        and closes[-1]<closes[-2] and closes[-1]<ema(closes,5)):
                    return 'scalp_pattern_failed'
                if closes[-1]<ema(closes,15) and closes[-2]<ema(closes[:-1],15) and closes[-1]<closes[-2]:
                    return 'scalp_trend_failed'
        return None

    def on_scan(self, state, reasons, ts):
        # Candidate evaluations repeat across cycles; entry rechecks are separate.
        # This hook runs inside the same transaction as the scan and ledger.
        diagnostics=state.setdefault('active_diagnostics',{})
        diagnostics.setdefault('since',ts)
        if diagnostics.get('day') != state['day']:
            diagnostics.update(day=state['day'],today={},entry_rechecks_today={})
        total=diagnostics.setdefault('total',{})
        today=diagnostics.setdefault('today',{})
        for reason in reasons.values():
            key='mint_check' if reason.startswith('mint_check:') else reason
            total[key]=total.get(key,0)+1
            today[key]=today.get(key,0)+1
        rechecks=diagnostics.setdefault('entry_rechecks',{})
        known={'expired_signal','awaiting_fresh_confirmation','already_held_or_cooldown',
               'position_or_daily_entry_limit','price_moved_after_signal','cash_reserve',
               'roundtrip_cost_or_inconsistent_quotes','costs_consume_stop_budget',
               'scalp_stop_outside_budget','stale_market','candles_stale','candles_unavailable',
               'daily_loss_limit','total_loss_limit','manually_paused','unpriced_position'}
        attempts=state.setdefault('scalp_entry_attempts',{})
        # Primary-key range keeps this bounded as the durable audit grows.
        for kind,raw in self.store.db.execute("SELECT kind,body FROM events WHERE id>? AND kind IN ('BUY','REJECT')",(self.cycle_event_id,)):
            event=json.loads(raw)
            attempts[event['mint']]=ts
            if kind != 'REJECT':
                continue
            reason=event['reason']
            key=('mint_check' if reason.startswith('mint_check:') else reason if
                 reason in known or reason.startswith('scalp_') else 'quote_data_error')
            rechecks[key]=rechecks.get(key,0)+1
            daily=diagnostics['entry_rechecks_today']
            daily[key]=daily.get(key,0)+1
        state['scalp_entry_attempts']=dict(sorted(attempts.items(),key=lambda x:-x[1])[:100])
        diagnostics.update(last_scan=ts,cycles=diagnostics.get('cycles',0)+1)

    def keep_history(self,history,keep,ts):
        return {m:h[-4:] for m,h in history.items() if m in keep}
