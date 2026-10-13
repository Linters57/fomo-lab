import json,tempfile,unittest
from pathlib import Path
from dataclasses import replace
import bot
from momentum import MomentumEngine,REVISION,activate
ROOT=Path(__file__).resolve().parents[1];START=1791900000
class Quotes(bot.DemoQuotes):
    def __init__(self):super().__init__();self.calls=[];self.fail_size=None
    def quote(self,a,b,amount,now):
        self.calls.append(amount)
        if amount==self.fail_size:raise bot.DataError('partial route unavailable')
        q=super().quote(a,b,amount,now)
        return replace(q,out_amount=int(bot.decimal(amount)/self.price if a==bot.USDC else bot.decimal(amount)*self.price))
class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'paper.sqlite'
        self.c=bot.Config.load(ROOT/'shared-config.json');self.store=bot.Store(self.path,self.c,'paper');self.q=Quotes();self.e=MomentumEngine(self.store,self.c,self.q)
        activate(self.store,START)
    def tearDown(self):self.store.close();self.temp.cleanup()
    def warm(self):
        for i in range(17):
            self.q.price=bot.decimal(1)+bot.decimal('.003')*i
            self.e.step([bot.demo_snapshot(START+i*60,str(self.q.price))],START+i*60)
        return self.store.load()['positions'][bot.SOL]
    def events(self):return [json.loads(x[0]) for x in self.store.db.execute("SELECT body FROM events WHERE kind='SELL' ORDER BY id")]
    def test_entry_requires_long_warmup_rising_and_distinct_confirmation(self):
        for i in range(16):
            price=1+i*.003;self.q.price=bot.decimal(price);snap=bot.demo_snapshot(START+i*60,price);self.e.step([snap],START+i*60)
        self.assertFalse(self.store.load()['positions']);self.e.step([snap],START+960);self.assertFalse(self.store.load()['positions'])
        h=[[START+i*60,str(1+i*.003),'synthetic-pool'] for i in range(16)]
        snap=bot.demo_snapshot(START+960,'1.045');h.append([snap.observed,snap.price,snap.pair])
        self.assertEqual(self.e.gate(snap,h,START+960),'momentum_long_trend_unconfirmed')
    def test_partial_and_final_accounting_restart_counts_wins_once(self):
        p=self.warm();original=p['cost'];qty=p['quantity'];self.q.price=bot.decimal('1.35')
        self.e.step([],START+1020);s=self.store.load();p=s['positions'][bot.SOL]
        self.assertTrue(p['runner']);self.assertEqual(p['quantity'],qty-qty*75//100)
        self.assertEqual(p['cost']+self.events()[0]['cost'],original)
        self.assertEqual(s['closed'],0);self.assertEqual(s['wins'],0);self.assertEqual(s['realized'],self.events()[0]['pnl'])
        self.assertEqual(len(self.events()),1);self.assertTrue(self.events()[0]['partial'])
        e=MomentumEngine(self.store,self.c,self.q);self.q.price=bot.decimal('1.0');e.step([],START+1080);s=self.store.load()
        self.assertFalse(s['positions']);self.assertEqual(s['closed'],1);self.assertEqual(s['wins'],1)
        sells=self.events();self.assertEqual(sum(x['quantity'] for x in sells),qty)
        self.assertEqual(s['realized'],sum(x['pnl'] for x in sells));self.assertEqual(sells[-1]['trade_pnl'],s['realized'])
        self.assertEqual(s['momentum_results']['closed'],1);self.assertEqual(s['momentum_results']['pnl'],s['realized'])
        self.assertLess(sells[-1]['pnl'],0);self.assertGreater(sells[-1]['trade_pnl'],0)
        self.assertEqual(s['cash']-self.c.initial_cash,s['realized'])
    def test_partial_quote_failure_never_mutates_or_counts_fill(self):
        p=self.warm();self.q.price=bot.decimal('1.35');self.q.fail_size=p['quantity']-p['quantity']*75//100
        before=self.store.load();self.e.step([],START+1020);after=self.store.load()
        for key in ['cash','realized','closed','wins','positions']:self.assertEqual(before[key],after[key])
        self.assertFalse(self.events())
    def test_partial_is_not_repeated_and_trail_ratchets(self):
        self.warm();self.q.price=bot.decimal('1.35');self.e.step([],START+1020);p=self.store.load()['positions'][bot.SOL]
        self.q.price=bot.decimal('1.5');self.e.step([],START+1080);p=self.store.load()['positions'][bot.SOL];floor=p['stop_floor']
        self.q.price=bot.decimal('1.48');self.e.step([],START+1140)
        self.assertEqual(self.store.load()['positions'][bot.SOL]['stop_floor'],floor);self.assertEqual(len(self.events()),1)
    def test_runner_deadline_sells_only_remaining_quantity(self):
        p=self.warm();self.q.price=bot.decimal('1.35');self.e.step([],START+1020);p=self.store.load()['positions'][bot.SOL]
        self.e.step([],p['runner_deadline']);self.assertFalse(self.store.load()['positions']);self.assertEqual(len(self.events()),2)
        self.assertEqual(self.events()[-1]['reason'],'momentum_runner_time_exit')
    def test_selective_extension_and_fixed_deadline(self):
        p=self.warm();t=p['opened']+3600
        h=[[t-900+i*60,str(1+i*.001),'synthetic-pool'] for i in range(16)]
        s=self.store.load();s['history'][bot.SOL]=h
        with self.store.db:self.store.save(s)
        self.e.current={bot.SOL:bot.demo_snapshot(t,'1.015')}
        self.assertIsNone(self.e.exit_reason(p,p['cost']*105//100,t));self.assertTrue(p['time_extended'])
        self.assertEqual(self.e.exit_reason(p,p['cost']*105//100,p['opened']+7200),'momentum_extended_time_exit')
        p2={**p};p2.pop('time_extended');self.e.current={}
        self.assertEqual(self.e.exit_reason(p2,p2['cost']*105//100,t),'time_exit')
        self.e.current={bot.SOL:bot.demo_snapshot(t,'1.015')}
        self.assertEqual(self.e.exit_reason(p2,p2['cost']*99//100,t),'time_exit')
    def test_extension_protects_with_ratchet_and_never_widens(self):
        p=self.warm();p.update(time_extended=True,extension_peak=p['cost']*110//100,stop_floor=p['cost']*101//100)
        self.assertIsNone(self.e.exit_reason(p,p['cost']*111//100,START+1500));floor=p['stop_floor']
        self.assertEqual(self.e.exit_reason(p,floor,START+1560),'momentum_extension_trail')
    def test_migration_preserves_balance_history_halts_and_old_exit(self):
        s=self.store.load();s.pop('momentum_revision');s.update(cash=999*bot.USD,realized=-bot.USD,halted=True,day_halted=True)
        s['positions']={bot.SOL:dict(cost=100*bot.USD,quantity=bot.USD,symbol='OLD',opened=START)}
        with self.store.db:self.store.save(s)
        activate(self.store,START+1);a=self.store.load();activate(self.store,START+2);self.assertEqual(a,self.store.load())
        for k in ['cash','realized','halted','day_halted']:self.assertEqual(a[k],s[k])
        self.assertEqual(self.e.exit_reason(a['positions'][bot.SOL],100*bot.USD,START+3600),'time_exit')
    def test_profit_exits_work_during_halt(self):
        self.warm();s=self.store.load();s.update(halted=True,day_halted=True)
        with self.store.db:self.store.save(s)
        self.q.price=bot.decimal('1.35');self.e.step([],START+1020);self.assertTrue(self.store.load()['positions'][bot.SOL]['runner'])

    def test_market_keeps_watchlist_when_category_disappears(self):
        from momentum import MomentumMarket
        from test_active import row
        from unittest.mock import patch
        now=[START];calls=[]
        class Http:
            def json(self,url,headers):
                calls.append(url);r=row(now[0]);r['isVerified']=True;r['audit'].pop('isSus')
                if '/search?' in url:return [r]
                return [r] if now[0]==START else []
        with patch('momentum.time.time',side_effect=lambda:now[0]):
            m=MomentumMarket(self.c,Http());self.assertEqual(len(m.collect()),1)
            now[0]+=60;self.assertEqual(len(m.collect()),1);self.assertIn(bot.SOL,m.watch)
            self.assertEqual(len(calls),4)
