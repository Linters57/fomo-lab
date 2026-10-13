"""Owner-approved Momentum v2: confirmed entries, 75/25 profits, selective time.

Paper-only hypotheses from the 2026-10-13 review. No optimised-return claim.
"""
from dataclasses import asdict
import os,time,urllib.parse
from bot import Engine,JupiterTokens,DataError,USDC,decimal,mint_ok
REVISION='momentum-runner-v2'
PROFILE=dict(id='momentum',name='Momentum',strategy='Momentum + gedeeltelijke winstneming',
    revision=REVISION,timed_exit=True,
    description='5-minutenmomentum met stijgende 15-minutentrend en twee opeenvolgende '
    'koersstijgingen. Bij het netto winstdoel 75% verkopen, 25% laten meelopen met '
    '8% trailing stop, maximaal twee extra uren. Na 60 minuten alleen verlengen tot '
    '120 minuten bij winst én een verse stijgende trend. Initiële stop volgens instelling (standaard 12%). '
    'Paperproef; percentages zijn hypothesen, geen bewezen optimum.')


def activate(store,now):
    with store.db:
        s=store.load()
        if s.get('momentum_revision',{}).get('id')==REVISION:return
        for p in s['positions'].values():
            p.setdefault('strategy','momentum-v1')
            p.setdefault('legacy_config',s['config'])
        s['pending']={};s['history']={}
        s['momentum_revision']=dict(id=REVISION,ts=now,equity=s['equity'],
            realized=s['realized'],closed=s['closed'],legacy_positions=len(s['positions']))
        s['momentum_results']=dict(closed=0,wins=0,pnl=0,partials=0)
        s['dashboard_revision']=s.get('dashboard_revision',0)+1
        store.save(s)
        store.event(now,'STRATEGY_CHANGE',current=s['config'],policy=PROFILE,
                    description=PROFILE['description'],baseline=s['momentum_revision'])


def ema(values,period=10):
    out=values[0];a=decimal(2)/decimal(period+1)
    for x in values[1:]:out+=a*(x-out)
    return out


class MomentumEngine(Engine):
    def step(self,snapshots,ts,discovery=None):
        self.current={s.mint:s for s in snapshots}
        return super().step(snapshots,ts,discovery)

    def history_seconds(self):return 1020

    def keep_history(self,history,keep,ts):
        return {m:h[-24:] for m,h in history.items() if m in keep}

    def window(self,history,observed,seconds):
        h=[x for x in history if x[0]<=observed]
        anchors=[i for i,x in enumerate(h) if x[0]<=observed-seconds]
        return h[anchors[-1]:] if anchors else h

    def trend(self,snap,history,now):
        if not snap or not 0<=now-snap.observed<=self.c.sample_ttl_seconds:return False
        h=self.window(history,snap.observed,900)
        if len(h)<12 or h[-1][0]-h[0][0]<900:return False
        if any(b[0]-a[0]>self.c.max_history_gap_seconds for a,b in zip(h,h[1:])):return False
        if any(x[2]!=snap.pair for x in h):return False
        p=[decimal(x[1]) for x in h]
        return (p[-1]>p[0] and p[-1]>p[-2]>p[-3] and p[-1]>ema(p)
                and ema(p)>ema(p[:-3]))

    def gate(self,snap,history,ts):
        if snap.safe is not True:return 'mint_check:'+snap.safety_reason
        reason=super().gate(snap,self.window(history,snap.observed,self.c.warmup_seconds),ts)
        if reason:return reason
        if not self.trend(snap,history,ts):return 'momentum_long_trend_unconfirmed'
        return None

    def entry_guard(self,snap,pending,ts):
        reason=super().entry_guard(snap,pending,ts)
        if reason:return reason
        if pending.get('observed') is not None and snap.observed<=pending['observed']:
            return 'awaiting_fresh_confirmation'
        return None

    def position_fields(self,snap,budget,proceeds,ts):
        return dict(strategy=REVISION,mint=snap.mint,original_cost=budget,
            original_quantity=None,realized_partial=0,trade_id=snap.mint+':'+str(ts),
            entry_stop_bps=self.c.stop_bps,entry_target_bps=self.c.take_profit_bps)

    def partial_exit(self,state,mint,pos,net,ts):
        if pos.get('strategy')!=REVISION or pos.get('runner'):return None
        if (net-pos['cost'])*10000<pos['cost']*pos['entry_target_bps']:return None
        if net<=pos.get('stop_floor',0):return None
        qty=pos['quantity'];sell_qty=qty*75//100;left=qty-sell_qty
        if sell_qty<=0 or left<=0:raise DataError('partial_quantity_too_small')
        # Quote each exact leg; never allocate the full quote as a made-up fill.
        output,q=self.quote(mint,USDC,sell_qty,ts)
        remaining,rq=self.quote(mint,USDC,left,ts)
        now=self.clock() if self.clock else ts
        if not 0<=now-q.observed<=self.c.quote_ttl_seconds:raise DataError('partial_quote_expired')
        proceeds=max(0,output-self.c.network_cost)
        mark=max(0,remaining-self.c.network_cost)
        allocated=pos['cost']*sell_qty//qty
        if (proceeds-allocated)*10000<allocated*pos['entry_target_bps']:
            raise DataError('partial_quote_below_profit_target')
        pnl=proceeds-allocated
        # Mutate the ledger only after BOTH exact-quantity quotes succeed.
        state['cash']+=proceeds;state['realized']+=pnl
        pos.update(original_quantity=qty,quantity=left,cost=pos['cost']-allocated,
            realized_partial=pnl,runner=True,runner_started=ts,
            runner_deadline=ts+7200,runner_peak=mark,
            stop_floor=max(mark*9200//10000,(pos['cost']-allocated)*(10000-pos['entry_stop_bps'])//10000),
            stop_floor_reason='momentum_runner_trail')
        r=state.setdefault('momentum_results',dict(closed=0,wins=0,pnl=0,partials=0))
        r['pnl']+=pnl;r['partials']+=1
        self.store.event(ts,'SELL',mint=mint,symbol=pos['symbol'],quantity=sell_qty,
            proceeds=proceeds,cost=allocated,pnl=pnl,reason='partial_take_profit',
            strategy=REVISION,partial=True,closed=False,opened=pos['opened'],
            trade_id=pos['trade_id'],remaining_quantity=left,remaining_cost=pos['cost'],
            quote=asdict(q))
        return mark

    def exit_reason(self,pos,net,ts):
        if pos.get('strategy')!=REVISION:
            if pos.get('legacy_config'):
                from bot import Config
                return Engine(self.store,Config(**pos['legacy_config']),self.quotes).exit_reason(pos,net,ts)
            return super().exit_reason(pos,net,ts)
        floor=pos['cost']*(10000-pos['entry_stop_bps'])//10000
        if pos.get('runner'):
            pos['runner_peak']=max(net,pos['runner_peak'])
            pos['stop_floor']=max(pos['stop_floor'],pos['runner_peak']*9200//10000,floor)
            if net<=pos['stop_floor']:return 'momentum_runner_trail'
            if ts>=pos['runner_deadline']:return 'momentum_runner_time_exit'
            return None
        if net<=floor:return 'stop_loss'
        if pos.get('time_extended'):
            pos['extension_peak']=max(net,pos['extension_peak'])
            pos['stop_floor']=max(pos.get('stop_floor',0),pos['extension_peak']*9200//10000,floor)
            pos['stop_floor_reason']='momentum_extension_trail'
            if net<=pos['stop_floor']:return 'momentum_extension_trail'
            if ts>=pos['opened']+7200:return 'momentum_extended_time_exit'
            return None
        if ts-pos['opened']>=self.c.max_hold_seconds:
            snap=self.current.get(pos['mint'])
            history=self.store.load()['history'].get(pos['mint'],[])
            # Engine.step's new observations are not saved yet; include current data.
            if snap and (not history or snap.observed>history[-1][0]):history=history+[[snap.observed,snap.price,snap.pair]]
            now=self.clock() if self.clock else ts
            if ts<pos['opened']+7200 and net>pos['cost'] and self.trend(snap,history,now):
                pos.update(time_extended=True,extension_peak=net,
                    stop_floor=max(floor,net*9200//10000),stop_floor_reason='momentum_extension_trail')
                self.store.event(ts,'HOLD_EXTENSION',mint=pos['mint'],symbol=pos['symbol'],
                    reason='profitable_confirmed_trend',deadline=pos['opened']+7200,strategy=REVISION)
                return None
            return 'time_exit'
        return None

    def on_close(self,state,mint,pos,pnl,ts):
        if pos.get('strategy')!=REVISION:return
        r=state.setdefault('momentum_results',dict(closed=0,wins=0,pnl=0,partials=0))
        r['pnl']+=pnl;r['closed']+=1;r['wins']+=int(pnl+pos.get('realized_partial',0)>0)


class MomentumMarket(JupiterTokens):
    """Eight stable slots allow the 15-minute confirmation to build on real updates."""
    def __init__(self,config,http):
        super().__init__(config,http);self.watch={};self.discovery={}

    def collect(self,held=()):
        now=int(time.time());headers={'x-api-key':os.environ['JUPITER_API_KEY']} if os.environ.get('JUPITER_API_KEY') else {}
        rows=self.http.json('https://api.jup.ag/tokens/v2/toptraded/5m?limit=50',headers)
        if not isinstance(rows,list):raise DataError('invalid token market response')
        raw_unique=len({r.get('id') for r in rows if isinstance(r,dict)})
        parsed=[self.parse(r,now) for r in rows]
        safe=sorted((s for s in parsed if s and s.safe),key=lambda s:(-s.liquidity,s.mint))
        self.watch={m:until for m,until in self.watch.items() if until>now}
        for s in safe:
            if len(self.watch)>=self.config.max_candidates:break
            if s.mint not in self.watch:self.watch[s.mint]=now+1800
        requested=set(self.watch)|set(held)
        if any(not mint_ok(m) for m in requested):raise DataError('invalid watched mint')
        if requested:
            query=urllib.parse.urlencode({'query':','.join(sorted(requested))})
            extra=self.http.json('https://api.jup.ag/tokens/v2/search?'+query,headers)
            if not isinstance(extra,list):raise DataError('invalid watched-token response')
            rows=extra
        parsed=[self.parse(r,int(time.time())) for r in rows]
        fresh={s.mint:s for s in parsed if s and s.mint in requested and
               0<=time.time()-s.observed<=self.config.sample_ttl_seconds}
        self.discovery=dict(raw_unique=raw_unique,safety_pass=len(safe),eligible=len(safe),
            watchlist=len(self.watch),fresh=len(fresh),refreshed_at=now,
            sources=['toptraded/5m'],cap=self.config.max_candidates,source_errors=0)
        if not fresh:raise DataError('no fresh token observations')
        return list(fresh.values())
