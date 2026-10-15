"""Paper pullback/reclaim experiment. Closed pool candles; executable quote marks.

No wallet, orders, inferred candles, intrabar fills or future-data backfills.
See SCALPING-2026-10-13.md for the frozen forward-test hypotheses.
"""
from dataclasses import asdict, replace
import json
import sqlite3
import time
import urllib.parse
from pathlib import Path
from bot import Engine, Config, DataError, USD, SOL, decimal, mint_ok
from active import ActiveEngine as LegacyEngine, ActiveMarket as LegacyMarket

REVISION = 'pullback-scalp-v1'
LIMITS = {'max_position': (10*USD, 200*USD), 'max_positions': (1, 4),
          'stop_bps': (150, 250), 'take_profit_bps': (300, 600)}
PROFILE = dict(id='active', name='Actief', strategy='Pullback scalping', revision=REVISION,
    timed_exit=False, candle_source='GeckoTerminal · gesloten 1-minuutcandles',
    description='Stijgende 15-minutentrend → terugval → bevestigd herstel. '
    'Maximaal 0,8% gemodelleerde retourkosten; stop onder het lokale dieptepunt '
    '(1,5–2,5% netto), winstdoel 3% netto. Verkoop bij patroonbreuk, geen verplichte '
    'tijdsverkoop. Gratis minutencandles kunnen vertraagd zijn; zonder verse candles '
    'geen aankoop. Paperproef, geen bewezen rendement of FOMO-uitvoering.')


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
        s['config'] = asdict(target)
        s.update(dashboard_overrides={}, pending={}, history={}, scalp_consumed={}, scalp_losses={})
        s['active_revision'] = dict(id=REVISION, ts=now, equity=s['equity'],
            realized=s['realized'], closed=s['closed'], legacy_positions=len(s['positions']),
            cash_adjustments=s.get('cash_adjustments', 0))
        s['scalp_results'] = dict(closed=0, wins=0, pnl=0, gross_wins=0, gross_losses=0)
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
        ranked = sorted(snaps, key=lambda s: (s.mint not in held,-s.volume_5m,s.mint))[:10]
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
                                and decimal(a.get('reserve_in_usd',0)) >= 500000):
                            candidates.append((decimal(a['reserve_in_usd']),address))
                    if not candidates:
                        raise DataError('no_liquid_candle_pool')
                    self.pools[s.mint] = dict(address=max(candidates)[1],ts=now)
                    self.bars.pop(s.mint,None)
                    # Pool lookup uses this turn's slot. Fetch chart next turn.
                    self.attempts[s.mint] = now-45
                else:
                    query = urllib.parse.urlencode(dict(aggregate=1,limit=40,currency='usd',
                        token=s.mint,include_empty_intervals='false'))
                    data = self.http.json(self.ROOT+'/pools/'+pool['address']+'/ohlcv/minute?'+query,self.HEADERS)
                    self.bars[s.mint] = self.parse(data,int(self.clock()))
                self.errors.pop(s.mint,None)
            except (DataError,KeyError,TypeError,ValueError) as exc:
                self.errors[s.mint] = str(exc)[:100]
                self.bars.pop(s.mint,None)
                if '429' in str(exc):
                    self.backoff = now+60
                    break
            if requests >= 2:
                break
        keep = {s.mint for s in snaps} | set(held)
        for cache in (self.pools,self.bars,self.attempts,self.errors):
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
        # Broad discovery and batch prices; intensive candle work remains capped at 10.
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
        self.discovery['intensive_cap'] = 10
        return snaps


def ema(values, period):
    result = values[0]
    a = decimal(2)/decimal(period+1)
    for v in values[1:]:
        result += a*(v-result)
    return result


def setup_from_bars(bars, price):
    """Use only closed bars. Recover part of a prior impulse without chasing it."""
    closes = [decimal(b[4]) for b in bars]
    if not (closes[-1] > closes[-16] and ema(closes,5) > ema(closes,15)
            and ema(closes,15) > ema(closes[:-3],15)):
        return None,'scalp_trend_not_rising'
    # Impulse peak must precede 1–4 pullback candles plus a recovery candle.
    peak_index = max(range(12,18),key=lambda i: decimal(bars[i][2]))
    peak = decimal(bars[peak_index][2])
    trough_rows = bars[peak_index+1:-1]
    if not trough_rows or len(trough_rows)>4:
        return None,'scalp_wait_pullback'
    trough_bar = min(trough_rows,key=lambda b: decimal(b[3]))
    trough = decimal(trough_bar[3])
    depth = (peak-trough)/peak
    if not decimal('0.008') <= depth <= decimal('0.05'):
        return None,'scalp_wait_pullback'
    if peak < min(closes[7:peak_index])*decimal('1.015'):
        return None,'scalp_weak_impulse'
    last, prev = bars[-1],bars[-2]
    trigger = decimal(prev[2])
    if not (decimal(last[4]) > trigger and decimal(last[4]) > decimal(last[1])
            and decimal(last[5]) >= decimal(prev[5])*decimal('0.8')):
        return None,'scalp_wait_reclaim'
    if price < trigger or price > decimal(last[4])*decimal('1.005'):
        return None,'scalp_reclaim_moved'
    # Noise buffer based on recent mean true range, not fabricated tick candles.
    tr = [max(decimal(b[2])-decimal(b[3]),abs(decimal(b[2])-closes[i-1]),
              abs(decimal(b[3])-closes[i-1])) for i,b in enumerate(bars) if i>0]
    buffer = max(price*decimal('0.001'),sum(tr[-14:])/14*decimal('0.15'))
    stop = trough-buffer
    if stop <= 0 or stop >= price:
        return None,'scalp_invalid_stop'
    return dict(id=str(bars[peak_index][0])+':'+str(trough_bar[0]),
                bar=last[0],stop_price=str(stop),reclaim=str(trigger),resistance=str(peak)),None


class ScalpEngine(Engine):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.candles = None
        self.current = {}
        self.consumed = {}
        self.setups = {}

    def step(self,snapshots,ts,discovery=None):
        self.current = {s.mint:s for s in snapshots}
        self.consumed = self.store.load().get('scalp_consumed',{})
        self.setups = {}
        return super().step(snapshots,ts,discovery)

    def pending_items(self,pending,current):
        return sorted(pending.items(),key=lambda item: -current[item[0]].volume_5m if item[0] in current else 0)[:1]

    def gate(self,snap,history,ts):
        c = self.c
        if not 0<=ts-snap.observed<=c.sample_ttl_seconds:
            return 'stale_market'
        if snap.safe is not True:
            return 'mint_check:'+snap.safety_reason
        if snap.liquidity<c.min_liquidity or snap.volume_5m<c.min_volume_5m:
            return 'liquidity_or_volume'
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
        if loss*10000<budget*150 or loss*10000>budget*self.c.stop_bps:
            return 'scalp_stop_outside_budget'
        if loss>self.c.risk_per_trade:
            return 'scalp_stop_outside_budget'
        # Past resistance is a filter, not a forecast of future profit.
        target=decimal(budget)*(1+decimal(self.c.take_profit_bps)/10000)
        if decimal(budget-drag)*decimal(setup['resistance'])/price<target:
            return 'scalp_insufficient_upside'
        return None

    def position_fields(self,snap,budget,proceeds,ts):
        setup=self.setups[snap.mint]
        floor=int(decimal(proceeds)*decimal(setup['stop_price'])/decimal(snap.price))
        return dict(strategy=REVISION,mint=snap.mint,setup=setup,stop_floor=floor,
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

    def exit_reason(self,pos,net,ts):
        if pos.get('strategy')!=REVISION:
            legacy=LegacyEngine(self.store,Config(**pos.get('legacy_config',asdict(self.c))),self.quotes)
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

    def keep_history(self,history,keep,ts):
        return {m:h[-4:] for m,h in history.items() if m in keep}
