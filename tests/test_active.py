import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import bot
import dashboard_state as dashboard
from active import ActiveEngine, ActiveMarket, LIMITS, PROFILE, activate_revision, restart_authorized_test
from fleet import Experiment, SharedHttp, prune_active
START=1791550000
ROOT=Path(__file__).resolve().parents[1]
def row(now=START):
    iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    return dict(id=bot.SOL,symbol='TEST',usdPrice=1,liquidity=200000,
      updatedAt=iso(now),firstPool={'createdAt':iso(now-86400)},
      stats5m=dict(buyVolume=20000,sellVolume=10000,numBuys=60,numSells=40),
      tokenProgram=bot.TOKEN_PROGRAM,isVerified=False,organicScore=70,holderCount=1000,
      audit=dict(isSus=False,mintAuthorityDisabled=True,freezeAuthorityDisabled=True,topHoldersPercentage=20))
class TightQuotes(bot.DemoQuotes):
    def quote(self,a,b,amount,now):
        q=super().quote(a,b,amount,now)
        raw=bot.decimal(amount)/self.price if a==bot.USDC else bot.decimal(amount)*self.price
        return replace(q,out_amount=int(raw*bot.decimal('0.997')),fee_bps=30)
class ActiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.c=bot.Config.load(ROOT/'active-config.json');self.store=bot.Store(self.path/'paper.sqlite',self.c,'paper')
        self.q=TightQuotes();self.engine=ActiveEngine(self.store,self.c,self.q)
    def tearDown(self):self.store.close();self.temp.cleanup()
    def sample(self,i):
        price=str(1+i*.004);self.q.price=bot.decimal(price)
        return bot.demo_snapshot(START+i*30,price)
    def warm(self):
        for i in range(7):self.engine.step([self.sample(i)],START+i*30)
    def test_fresh_entry_size_and_idempotence(self):
        self.warm();self.assertEqual(self.store.load()['positions'],{})
        self.engine.step([self.sample(7)],START+210)
        pos=next(iter(self.store.load()['positions'].values()))
        self.assertEqual(pos['cost'],200*bot.USD);self.assertEqual(pos['cost']*self.c.stop_bps//10000,8*bot.USD)
        before=self.store.load();self.engine.step([self.sample(7)],START+210);self.assertEqual(before,self.store.load())
    def test_costly_quotes_reject_entry(self):
        self.q=bot.DemoQuotes();self.engine.quotes=self.q
        self.warm();self.engine.step([self.sample(7)],START+210)
        self.assertEqual(self.store.load()['positions'],{})
        reason=json.loads(self.store.db.execute("SELECT body FROM events WHERE kind='REJECT' ORDER BY id DESC LIMIT 1").fetchone()[0])['reason']
        self.assertEqual(reason,'roundtrip_cost_or_inconsistent_quotes')
    def test_duplicate_feed_update_is_not_confirmation(self):
        self.warm();self.engine.step([self.sample(6)],START+210)
        self.assertEqual(self.store.load()['positions'],{})
        reason=json.loads(self.store.db.execute("SELECT body FROM events WHERE kind='REJECT' ORDER BY id DESC LIMIT 1").fetchone()[0])['reason']
        self.assertEqual(reason,'awaiting_fresh_confirmation')
    def test_retains_short_absence_but_rejects_large_gap(self):
        self.warm();self.engine.step([],START+210);self.assertIn(bot.SOL,self.store.load()['history'])
        self.engine.step([self.sample(8)],START+240);self.assertIn(bot.SOL,self.store.load()['pending'])
        self.engine.step([self.sample(20)],START+600);self.assertEqual(self.store.load()['positions'],{});self.assertEqual(self.store.load()['pending'],{})
    def test_all_exit_barriers(self):
        p={'cost':100*bot.USD,'opened':START}
        self.assertIsNone(self.engine.exit_reason(p,104*bot.USD,START+30))
        self.assertEqual(self.engine.exit_reason(p,102*bot.USD,START+60),'trailing_stop')
        self.assertEqual(self.engine.exit_reason({'cost':100*bot.USD,'opened':START},95*bot.USD,START+60),'stop_loss')
        self.assertEqual(self.engine.exit_reason(p,109*bot.USD,START+60),'take_profit')
        self.assertIsNone(self.engine.exit_reason({'cost':100*bot.USD,'opened':START},100*bot.USD,START+900))
    def test_aged_stop_boundary_ratchet_and_restart(self):
        p={'cost':100*bot.USD,'opened':START}
        self.assertIsNone(self.engine.exit_reason(p,97*bot.USD,START+899))
        self.assertEqual(self.engine.exit_reason(p,97*bot.USD,START+900),'aged_stop')
        p={'cost':100*bot.USD,'opened':START}
        self.assertIsNone(self.engine.exit_reason(p,99*bot.USD,START+900))
        self.assertEqual(p['stop_floor'],98*bot.USD)
        self.assertIsNone(self.engine.exit_reason(p,102*bot.USD,START+930))
        floor=p['stop_floor']
        self.assertGreater(floor,99*bot.USD)
        # Position fields survive SQLite serialization and a reconstructed engine.
        state=self.store.load();state['positions']={bot.SOL:p}
        with self.store.db:self.store.save(state)
        p=self.store.load()['positions'][bot.SOL]
        engine=ActiveEngine(self.store,self.c,self.q)
        self.assertIsNone(engine.exit_reason(p,101*bot.USD,START+960))
        self.assertEqual(p['stop_floor'],floor)
        self.assertEqual(engine.exit_reason(p,floor,START+990),'aged_stop')
        p={'cost':100*bot.USD,'opened':START}
        self.assertIsNone(engine.exit_reason(p,100*bot.USD,START+86400))

    def test_cost_guard_rejects_weak_signal_against_actual_quotes(self):
        h=[[START,'1','synthetic-pool']]
        self.assertEqual(self.engine.entry_cost_guard(bot.demo_snapshot(START+180,'1.006'),h,200*bot.USD,2*bot.USD),'momentum_too_small_for_costs')
        self.assertIsNone(self.engine.entry_cost_guard(bot.demo_snapshot(START+180,'1.015'),h,200*bot.USD,2*bot.USD))
        for i in range(8):
            price=str(1+i*.001);self.q.price=bot.decimal(price)
            self.engine.step([bot.demo_snapshot(START+i*30,price)],START+i*30)
        self.assertEqual(self.store.load()['positions'],{})
        rejects=[json.loads(r[0])['reason'] for r in self.store.db.execute("SELECT body FROM events WHERE kind='REJECT'")]
        self.assertIn('momentum_too_small_for_costs',rejects)

    def test_engine_holds_past_time_and_exits_only_on_priced_stop(self):
        state=self.store.load()
        state['cash']-=100*bot.USD
        state['positions']={bot.SOL:dict(cost=100*bot.USD,quantity=100*bot.USD,opened=START,symbol='TEST')}
        with self.store.db:self.store.save(state)
        self.q.price=bot.decimal('1')
        self.engine.step([],START+900)
        self.assertIn(bot.SOL,self.store.load()['positions'])
        self.assertEqual(self.store.load()['positions'][bot.SOL]['stop_floor'],98*bot.USD)
        class Missing:
            def quote(self,*args):raise bot.DataError('missing exit quote')
        self.engine.quotes=Missing()
        self.engine.step([],START+930)
        self.assertIn(bot.SOL,self.store.load()['positions'])
        self.assertIn(bot.SOL,self.store.load()['unpriced'])
        self.engine.quotes=self.q;self.q.price=bot.decimal('0.98')
        self.engine.step([],START+960)
        self.assertEqual(self.store.load()['positions'],{})
        trade=json.loads(self.store.db.execute("SELECT body FROM events WHERE kind='SELL'").fetchone()[0])
        self.assertEqual(trade['reason'],'aged_stop')

    def test_revision_keeps_losses_halts_and_is_idempotent(self):
        s=self.store.load();s.update(cash=721*bot.USD,equity=721*bot.USD,realized=-279*bot.USD,closed=178,halted=True,day_halted=True)
        with self.store.db:self.store.save(s)
        activate_revision(self.store,START)
        after=self.store.load()
        for key in ('cash','equity','realized','closed','halted','day_halted'):
            self.assertEqual(after[key],s[key])
        self.assertEqual(after['active_revision']['equity'],721*bot.USD)
        activate_revision(self.store,START+1)
        self.assertEqual(self.store.load(),after)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='STRATEGY_CHANGE'").fetchone()[0],1)

    def test_requested_restart_funds_once_resets_entries_preserves_history(self):
        s=self.store.load();s.update(cash=721*bot.USD,equity=721*bot.USD,
            last_priced_equity=721*bot.USD,realized=-279*bot.USD,closed=178,
            entries_today=48,halted=True,day_halted=True)
        with self.store.db:
            self.store.save(s);self.store.event(START-1,'SELL',pnl=-279*bot.USD)
        self.assertTrue(restart_authorized_test(self.store,START))
        n=self.store.load()
        self.assertEqual(n['cash'],1000*bot.USD)
        self.assertEqual(n['day_start'],1000*bot.USD)
        self.assertEqual(n['entries_today'],0)
        self.assertFalse(n['halted']);self.assertFalse(n['day_halted'])
        self.assertEqual(n['cash_adjustments'],279*bot.USD)
        self.assertEqual(n['realized'],s['realized']);self.assertEqual(n['closed'],178)
        self.assertEqual(n['equity']-self.c.initial_cash-n['cash_adjustments'],s['realized'])
        self.assertFalse(restart_authorized_test(self.store,START+1))
        self.assertEqual(self.store.load(),n)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='SELL'").fetchone()[0],1)
        p=self.path/'reset-dashboard.json'
        dashboard.export(self.store,p,bot.status,START,limits=LIMITS,profile=PROFILE)
        exported=json.loads(p.read_text())
        self.assertEqual(exported['test_restart']['cash'],1000*bot.USD)
        self.assertEqual(exported['entries_today'],0)
        self.assertEqual(exported['cash_adjustments'],279*bot.USD)

    def test_requested_restart_never_erases_open_position(self):
        s=self.store.load();s['positions']={bot.SOL:{'cost':100*bot.USD}}
        with self.store.db:self.store.save(s)
        self.assertFalse(restart_authorized_test(self.store,START))
        self.assertEqual(self.store.load(),s)

    def test_no_falling_entry_or_future_history(self):
        h=[[START+i*30,str(1+i*.001),'synthetic-pool'] for i in range(7)]
        self.assertEqual(self.engine.gate(bot.demo_snapshot(START+180,'1.003'),h,START+180),'below_recent_high')
        snap=bot.demo_snapshot(START+180,'1.006');baseline=self.engine.gate(snap,h,START+180)
        self.assertEqual(baseline,self.engine.gate(snap,h+[[START+210,'100','synthetic-pool']],START+180))
    def test_policy_rejects_missing_audit_suspicion_and_low_reputation(self):
        self.assertTrue(ActiveMarket.parse(row(),START).safe)
        for k,v in [('isSus',True),('topHoldersPercentage',41),('mintAuthorityDisabled',False)]:
            r=row();r['audit'][k]=v;self.assertFalse(ActiveMarket.parse(r,START).safe)
        r=row();r['organicScore']=59;self.assertFalse(ActiveMarket.parse(r,START).safe)
        r=row();r['audit'].pop('topHoldersPercentage');self.assertFalse(ActiveMarket.parse(r,START).safe)
    def test_settings_use_own_limits_and_restore(self):
        p=self.path/'command.json';p.write_text(json.dumps(dict(id='settings',expires=START+60,revision=0,action='settings',settings={'stop_bps':300})))
        dashboard.apply_command(self.store,self.engine,p,bot.Config,START,limits=LIMITS)
        self.assertTrue(self.store.load()['dashboard_ack']['ok'])
        self.assertEqual(dashboard.restore_config(self.path/'paper.sqlite',self.c,bot.Config,LIMITS).stop_bps,300)
    def test_retention_preserves_trades_and_exports_bounded_profile(self):
        with self.store.db:
            for k in ['BUY','SELL','CONTROL','CAPITAL_CHANGE','STRATEGY_CHANGE','SCAN','EQUITY']:
                self.store.event(START-40*86400,k,**({'value':1000000000} if k=='EQUITY' else {'delta':0} if k=='CAPITAL_CHANGE' else {}))
        prune_active(self.store,START);kinds={r[0] for r in self.store.db.execute('SELECT kind FROM events')}
        self.assertTrue({'BUY','SELL','CONTROL','CAPITAL_CHANGE','STRATEGY_CHANGE'}<=kinds);self.assertFalse({'SCAN','EQUITY'}&kinds)
        p=self.path/'dashboard.json';dashboard.export(self.store,p,bot.status,START,limits=LIMITS,profile=PROFILE,max_bytes=160000)
        self.assertEqual(json.loads(p.read_text())['profile']['id'],'active');self.assertLess(p.stat().st_size,160000)
class MarketTests(unittest.TestCase):
    def test_batched_sticky_watchlist(self):
        calls=[];now=[START]
        class HTTP:
            def json(self,url,headers=None):
                calls.append(url)
                if '/search?' in url:return [row(now[0])]
                return [row()] if now[0]==START else []
        m=ActiveMarket(bot.Config.load(ROOT/'active-config.json'),HTTP(),lambda:now[0])
        self.assertEqual(len(m.collect()),1);self.assertEqual(len(calls),4)
        now[0]+=30;self.assertEqual(len(m.collect()),1);self.assertEqual(len(calls),5)
        now[0]+=300;self.assertEqual(len(m.collect()),1);self.assertIn(bot.SOL,m.watch)
    def test_shared_pacing_cache_backoff(self):
        clock=[0];calls=[]
        class Stop:
            def wait(self,n):clock[0]+=n;return False
        def transport(url,headers,payload):calls.append(clock[0]);return []
        h=SharedHttp(Stop(),transport,lambda:clock[0]);url='https://api.jup.ag/tokens/v2/toptraded/5m?limit=100'
        h.json(url);h.json(url);h.json('https://api.jup.ag/swap/v2/order');h.json('https://api.jup.ag/swap/v2/order')
        self.assertEqual(len(calls),3);self.assertGreaterEqual(calls[1]-calls[0],2.2)
        def fail(*args):raise bot.DataError('HTTP 429')
        h.transport=fail
        with self.assertRaises(bot.DataError):h.json('https://api.jup.ag/swap/v2/order')
        h.transport=transport
        with self.assertRaisesRegex(bot.DataError,'shared_api_backoff'):h.json(url)
    def test_separate_accounts_commands_and_exports(self):
        with tempfile.TemporaryDirectory() as t, ExitStack() as stack:
            root=Path(t);h=SharedHttp(threading.Event());s=bot.SolanaSafety(h)
            a=Experiment(root,'momentum',ROOT/'shared-config.json',h,s,threading.Event(),stack)
            b=Experiment(root,'active',ROOT/'active-config.json',h,s,threading.Event(),stack)
            p=b.root/'dashboard-command.json';p.write_text(json.dumps(dict(id='pause',expires=START+60,revision=b.store.load().get('dashboard_revision',0),action='pause',paused=True)))
            dashboard.apply_command(b.store,b.engine,p,bot.Config,START,limits=b.limits)
            self.assertTrue(b.store.load()['dashboard_paused']);self.assertFalse(a.store.load().get('dashboard_paused',False))
            self.assertEqual(a.store.load()['cash'],1000000000);self.assertEqual(b.store.load()['cash'],1000000000)
            a.export();b.export();self.assertNotEqual(a.root,b.root)
