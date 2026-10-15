import json,tempfile,unittest
from pathlib import Path
from dataclasses import replace,asdict
import bot
from scalping import Candles,ScalpEngine,REVISION,migrate,setup_from_bars
ROOT=Path(__file__).resolve().parents[1]
NOW=1791878400

def bars():
    closes=[100,100.3,100.6,100.9,101.2,101.5,101.8,102.1,102.4,102.7,103,103.3,103.6,103.9,104.2,109.5,106.5,105,105.1,105.8]
    rows=[[NOW-1200+i*60,str(c-.1),str(c+.2),str(c-.3),str(c),'10000'] for i,c in enumerate(closes)]
    rows[15][2]='110';rows[16][2]='108';rows[17][2]='107';rows[17][3]='104.5';rows[18][2]='105.6';rows[18][3]='104.8'
    return rows
class CheapQuotes(bot.DemoQuotes):
    def quote(self,a,b,amount,now):
        q=super().quote(a,b,amount,now)
        value=bot.decimal(amount)/self.price if a==bot.USDC else bot.decimal(amount)*self.price
        return replace(q,out_amount=int(value),fee_bps=0)
class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'paper.sqlite'
        self.c=bot.Config.load(ROOT/'active-config.json');self.store=bot.Store(self.path,self.c,'paper')
        with self.store.db:
            state=self.store.load();state['accounts']=[bot.SOL];self.store.save(state)
        self.q=CheapQuotes();self.q.price=bot.decimal('105.8');self.e=ScalpEngine(self.store,self.c,self.q)
        self.feed=Candles(None,lambda:NOW);self.feed.bars[bot.SOL]=bars();self.e.candles=self.feed
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def snap(self,t=NOW):return replace(bot.demo_snapshot(t,'105.8'),liquidity=1000000*bot.USD,volume_5m=100000*bot.USD)
    def buy(self):
        self.e.step([self.snap()],NOW);self.e.step([self.snap(NOW+15)],NOW+15)
        return self.store.load()['positions'].get(bot.SOL)
    def test_pattern_complete_trade_and_no_repeat(self):
        self.assertIsNone(setup_from_bars(bars(),bot.decimal('105.8'))[1])
        p=self.buy();self.assertIsNotNone(p);self.assertEqual(p['strategy'],REVISION)
        self.assertLessEqual(p['cost'],100*bot.USD);self.assertLessEqual(p['cost']-p['stop_floor'],2*bot.USD)
        self.assertLessEqual((p['cost']-p['stop_floor'])*10000,p['cost']*200)
        self.q.price=bot.decimal('111');self.e.step([],NOW+30)
        s=self.store.load();self.assertFalse(s['positions']);self.assertEqual(s['scalp_results']['closed'],1);self.assertGreater(s['scalp_results']['pnl'],0)
        trade=json.loads(self.store.db.execute("SELECT body FROM events WHERE kind='SELL'").fetchone()[0]);self.assertEqual(trade['strategy'],REVISION);self.assertEqual(trade['reason'],'take_profit')
        self.assertEqual(self.e.gate(self.snap(NOW+45),[],NOW+45),'scalp_setup_used')
    def test_missing_stale_gap_duplicate_reject(self):
        for mode in ('missing','stale','gap'):
            self.feed.bars[bot.SOL]=bars()
            if mode=='missing':self.feed.bars.clear()
            elif mode=='stale':self.feed.bars[bot.SOL]=[[b[0]-300,*b[1:]] for b in bars()]
            else:self.feed.bars[bot.SOL][4][0]-=60
            self.assertIsNotNone(self.e.gate(self.snap(),[],NOW))
        self.feed.bars[bot.SOL]=bars();self.e.step([self.snap()],NOW);self.e.step([self.snap()],NOW+15);self.assertFalse(self.store.load()['positions'])
    def test_cost_stop_guard_and_resistance_is_diagnostic_only(self):
        self.assertIsNone(self.e.gate(self.snap(),[],NOW));self.assertIsNone(self.e.entry_cost_guard(self.snap(),[],100*bot.USD,400000))
        self.e.setups[bot.SOL]['stop_price']='99';self.assertEqual(self.e.entry_cost_guard(self.snap(),[],100*bot.USD,400000),'scalp_stop_outside_budget')
        self.e.gate(self.snap(),[],NOW);self.e.setups[bot.SOL]['resistance']='106';self.assertIsNone(self.e.entry_cost_guard(self.snap(),[],100*bot.USD,400000))
        self.q=bot.DemoQuotes();self.q.price=bot.decimal('105.8');self.e.quotes=self.q;self.assertIsNone(self.buy())
    def test_no_clock_exit_and_missing_quote_never_fills(self):
        p=self.buy();self.assertIsNotNone(p);self.e.current={};self.assertIsNone(self.e.exit_reason(p,p['cost'],NOW+86400))
        self.assertEqual(self.e.exit_reason(p,p['stop_floor'],NOW+30),'scalp_structure_stop')
        class Missing:
            def quote(self,*a):raise bot.DataError('offline')
        self.e.quotes=Missing();self.e.step([],NOW+30);self.assertIn(bot.SOL,self.store.load()['positions']);self.assertIn(bot.SOL,self.store.load()['unpriced'])
    def test_loss_backoff_and_restart(self):
        p=self.buy();s=self.store.load();self.e.on_close(s,bot.SOL,p,-bot.USD,NOW+30);self.assertEqual(s['cooldowns'][bot.SOL],NOW+330)
        self.e.on_close(s,bot.SOL,p,-bot.USD,NOW+45);self.assertEqual(s['cooldowns'][bot.SOL],NOW+1845)
        with self.store.db:self.store.save(s)
        e=ScalpEngine(self.store,self.c,self.q);e.candles=self.feed;e.step([],NOW+60);self.assertEqual(e.consumed[bot.SOL],p['setup']['id'])
    def test_migration_preserves_funds_positions_halts(self):
        s=self.store.load();old=bot.Config.load(ROOT/'tests/legacy-active-config.json');s.update(config=asdict(old),cash=700*bot.USD,realized=-100*bot.USD,halted=True,day_halted=True)
        s['positions']={bot.SOL:dict(cost=200*bot.USD,quantity=bot.USD,opened=NOW-1800,symbol='SOL')}
        with self.store.db:self.store.save(s)
        migrate(self.path,self.c,NOW);after=self.store.load()
        for k in ('cash','realized','halted','day_halted'):self.assertEqual(after[k],s[k])
        self.assertEqual(after['positions'][bot.SOL]['legacy_config'],asdict(old));self.assertEqual(after['config'],asdict(self.c))
        migrate(self.path,self.c,NOW+1);self.assertEqual(after,self.store.load());self.assertEqual(self.e.exit_reason(after['positions'][bot.SOL],190*bot.USD,NOW),'aged_stop')
    def test_parser_no_intrabar_or_invalid_data(self):
        r=[[x[0],*[float(v) for v in x[1:]]] for x in bars()];r.append([NOW,100,999,1,999,10000]);data={'data':{'attributes':{'ohlcv_list':r}}}
        parsed=Candles.parse(data,NOW+20);self.assertEqual(len(parsed),20);self.assertEqual(parsed[-1][0],NOW-60)
        r[-1][2]=0
        with self.assertRaises(bot.DataError):Candles.parse(data,NOW+20)
    def test_provider_budget_identity_orientation_backoff(self):
        now=[NOW];calls=[]
        class Http:
            def json(self,url,headers):
                calls.append(url)
                if '/ohlcv/' in url:return {'data':{'attributes':{'ohlcv_list':[[b[0],*[float(v) for v in b[1:]]] for b in bars()]}}}
                return {'data':[{'attributes':{'address':bot.SOL,'reserve_in_usd':'1000000'},'relationships':{'base_token':{'data':{'id':'solana_other'}},'quote_token':{'data':{'id':'solana_'+bot.SOL}}}}]}
        f=Candles(Http(),lambda:now[0]);f.refresh([self.snap()]);self.assertEqual(len(calls),1)
        now[0]+=15;f.refresh([self.snap()]);self.assertEqual(len(calls),2);self.assertIn('token='+bot.SOL,calls[-1]);self.assertIsNone(f.get(bot.SOL,now[0])[1])
        now[0]+=15;f.refresh([self.snap()]);self.assertEqual(len(calls),2);f.calls=[now[0]]*8;f.attempts={};f.refresh([self.snap()]);self.assertEqual(len(calls),2)
        class Bad:
            def json(self,*a):raise bot.DataError('HTTP 429')
        f.http=Bad();f.calls=[];f.refresh([self.snap()]);self.assertGreater(f.backoff,now[0]);self.assertNotIn(bot.SOL,f.bars)

    def test_recovery_below_previous_high_and_breakout_are_distinct(self):
        rows=bars();rows[-1][4]='105.5';rows[-1][2]='105.5';rows[-1][1]='105.2'
        setup,reason=setup_from_bars(rows,bot.decimal('105.5'))
        self.assertIsNone(reason);self.assertEqual(setup['type'],'recovery')
        self.assertLess(bot.decimal(rows[-1][4]),bot.decimal(rows[-2][2]))
        rows=[]
        for i in range(20):
            c=100+i*.1;rows.append([NOW-1200+i*60,str(c-.05),str(c+.03),str(c-.1),str(c),'10000'])
        setup,reason=setup_from_bars(rows,bot.decimal(rows[-1][4]))
        self.assertIsNone(reason);self.assertEqual(setup['type'],'breakout')
        self.assertLess(bot.decimal(setup['stop_price']),bot.decimal(rows[-1][4]))
        rows[-1][4]='99';self.assertEqual(setup_from_bars(rows,bot.decimal('99'))[1],'scalp_trend_not_rising')

    def test_diagnostics_survive_restart_and_count_quote_rejects_separately(self):
        with self.store.db:
            state=self.store.load();state['accounts']=[];self.store.save(state)
        self.assertIsNone(self.buy()) # New-account all-in model is >0.8% even without pool fees.
        d=self.store.load()['scalp_diagnostics']
        self.assertEqual(d['cycles'],2);self.assertEqual(d['evaluations'],2)
        self.assertEqual(d['reasons']['signal_for_next_cycle'],2)
        self.assertEqual(d['entry_rejections']['roundtrip_cost_or_inconsistent_quotes'],1)
        e=ScalpEngine(self.store,self.c,self.q);e.candles=self.feed
        e.step([replace(self.snap(NOW+30),volume_5m=0)],NOW+30)
        self.assertEqual(self.store.load()['scalp_diagnostics']['reasons']['insufficient_volume'],1)
        self.assertEqual(self.store.load()['scalp_diagnostics']['cycles'],3)

    def test_type_results_and_old_position_exit_contract(self):
        p=self.buy();self.assertEqual(p['entry_type'],'recovery')
        self.q.price=bot.decimal('111');self.e.step([],NOW+30)
        r=self.store.load()['scalp_results'];self.assertEqual(r['by_setup']['recovery']['closed'],1)
        sell=json.loads(self.store.db.execute("SELECT body FROM events WHERE kind='SELL'").fetchone()[0])
        self.assertEqual(sell['entry_type'],'recovery')
        p['strategy']='pullback-scalp-v1';p['take_profit_bps']=300
        self.assertIsNone(self.e.exit_reason(p,p['cost']*1025//1000,NOW+86400))
        self.assertEqual(self.e.exit_reason(p,p['cost']*1031//1000,NOW+86400),'take_profit')

    def test_four_stable_candle_slots_and_rotation(self):
        now=[NOW];feed=Candles(None,lambda:now[0]);feed.backoff=NOW+10000
        # Do not make requests in this test; inspect scheduling via a synthetic transport.
        class Fail:
            def json(self,*args):raise bot.DataError('offline')
        feed.http=Fail();feed.backoff=0
        snaps=[replace(self.snap(),mint=str(i)*32) for i in range(1,7)]
        feed.refresh(snaps);first=set(feed.focus);self.assertEqual(len(first),4)
        now[0]+=60;feed.refresh(snaps);self.assertEqual(first,set(feed.focus))
        now[0]+=300;feed.refresh(snaps,held=[snaps[0].mint]);self.assertIn(snaps[0].mint,feed.focus)
        self.assertTrue(set(s.mint for s in snaps)-first <= set(feed.focus))
        self.assertLessEqual(len(feed.calls),6)
