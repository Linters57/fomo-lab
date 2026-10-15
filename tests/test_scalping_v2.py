import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
import bot
import dashboard_state
from scalping import Candles, ScalpEngine, REVISION, PROFILE, LIMITS, migrate, setup_from_bars
from test_scalping import CheapQuotes

NOW = 1791878400
ROOT = Path(__file__).resolve().parents[1]


def bars(kind='recovery'):
    closes = [100+i*.04 for i in range(20)]
    if kind == 'recovery':
        closes[-5:] = [101.0,100.6,100.4,100.45,100.6]
    else:
        closes[-1] = 100.9
    rows = [[NOW-1200+i*60,str(c-.03),str(c+.08),str(c-.1),str(c),'10000']
            for i,c in enumerate(closes)]
    if kind == 'recovery':
        rows[-2][2] = '100.75'
    return rows


class ActiveV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'paper.sqlite'
        self.c = bot.Config.load(ROOT/'active-config.json')
        self.store = bot.Store(self.path,self.c,'paper')
        # Reuse a previously modeled token account; never hide first-use rent.
        state = self.store.load()
        state['accounts'] = [bot.SOL]
        with self.store.db:
            self.store.save(state)
        self.q = CheapQuotes()
        self.engine = ScalpEngine(self.store,self.c,self.q)
        self.feed = Candles(None,lambda:NOW)
        self.engine.candles = self.feed

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def snap(self,ts=NOW,kind='recovery'):
        price = bars(kind)[-1][4]
        return replace(bot.demo_snapshot(ts,price),liquidity=250000*bot.USD,
                       volume_5m=15000*bot.USD)

    def buy(self,kind='recovery'):
        self.feed.bars[bot.SOL] = bars(kind)
        self.q.price = bot.decimal(bars(kind)[-1][4])
        self.engine.step([self.snap(kind=kind)],NOW)
        self.engine.step([self.snap(NOW+15,kind)],NOW+15)
        return self.store.load()['positions'].get(bot.SOL)

    def test_recovery_does_not_require_previous_high_or_three_percent_headroom(self):
        rows = bars()
        self.assertLess(bot.decimal(rows[-1][4]),bot.decimal(rows[-2][2]))
        setup,reason = setup_from_bars(rows,bot.decimal(rows[-1][4]))
        self.assertIsNone(reason)
        self.assertEqual(setup['kind'],'recovery')
        self.assertLess(bot.decimal(setup['resistance'])/bot.decimal(rows[-1][4]),bot.decimal('1.02'))
        self.assertIsNotNone(self.buy())

    def test_breakout_opens_with_separate_type_and_rechecks_before_buy(self):
        self.feed.bars[bot.SOL] = bars('breakout')
        snap = self.snap(kind='breakout')
        self.q.price = bot.decimal(snap.price)
        self.engine.step([snap],NOW)
        self.assertFalse(self.store.load()['positions'])
        self.engine.step([replace(snap,observed=NOW+15)],NOW+15)
        p = self.store.load()['positions'][bot.SOL]
        self.assertEqual(p['setup_type'],'breakout')
        buy = json.loads(self.store.db.execute("SELECT body FROM events WHERE kind='BUY'").fetchone()[0])
        self.assertEqual(buy['setup_type'],'breakout')

    def test_position_cost_risk_and_target_are_net_and_per_type(self):
        p = self.buy()
        self.assertEqual(p['cost'],100*bot.USD)
        self.assertLessEqual(p['cost']-p['stop_floor'],2*bot.USD)
        self.assertLess(p['cost']-p['stop_floor'],p['cost']*15//1000)
        self.assertEqual(p['take_profit_bps'],200)
        self.assertIsNone(self.engine.exit_reason(p,p['cost']*10199//10000,NOW+30))
        self.assertEqual(self.engine.exit_reason(p,p['cost']*102//100,NOW+30),'take_profit')
        self.q.price *= bot.decimal('1.03')
        self.engine.step([],NOW+30)
        s = self.store.load()
        self.assertEqual(s['setup_results']['recovery']['closed'],1)
        self.assertEqual(s['setup_results']['recovery']['pnl'],s['scalp_results']['pnl'])
        sell = json.loads(self.store.db.execute("SELECT body FROM events WHERE kind='SELL'").fetchone()[0])
        self.assertEqual(sell['setup_type'],'recovery')

    def test_old_thresholds_are_relaxed_but_safety_and_separate_minima_remain(self):
        self.feed.bars[bot.SOL] = bars()
        snap = self.snap()
        self.assertIsNone(self.engine.gate(snap,[],NOW))
        for changes,reason in [({'liquidity':199999*bot.USD},'insufficient_liquidity'),
                               ({'volume_5m':9999*bot.USD},'insufficient_volume'),
                               ({'safe':False},'mint_check:demo'),
                               ({'observed':NOW-46},'stale_market')]:
            actual = self.engine.gate(replace(snap,**changes),[],NOW)
            self.assertTrue(actual.startswith('mint_check:') if 'safe' in changes else actual==reason)

    def test_duplicate_confirmation_missing_candles_and_costly_quotes_never_buy(self):
        self.feed.bars[bot.SOL] = bars()
        self.q.price = bot.decimal(self.snap().price)
        self.engine.step([self.snap()],NOW)
        self.engine.step([self.snap()],NOW+15)
        self.assertFalse(self.store.load()['positions'])
        self.engine.quotes = bot.DemoQuotes()
        self.engine.quotes.price = self.q.price
        self.engine.step([self.snap(NOW+30)],NOW+30)
        self.assertFalse(self.store.load()['positions'])
        self.feed.bars.clear()
        self.assertEqual(self.engine.gate(self.snap(),[],NOW),'candles_unavailable')

    def test_structural_stop_over_two_percent_is_rejected_without_forced_trade(self):
        self.feed.bars[bot.SOL] = bars()
        self.engine.gate(self.snap(),[],NOW)
        self.engine.setups[bot.SOL]['stop_price'] = '97'
        self.assertEqual(self.engine.entry_cost_guard(self.snap(),[],100*bot.USD,600000),
                         'scalp_stop_outside_budget')

    def test_first_use_rent_is_not_hidden_to_manufacture_a_trade(self):
        state = self.store.load()
        state['accounts'] = []
        with self.store.db:
            self.store.save(state)
        self.assertIsNone(self.buy())
        self.assertEqual(self.store.load()['cash'],self.c.initial_cash)
        event = self.store.db.execute("SELECT body FROM events WHERE kind='REJECT'").fetchone()
        self.assertEqual(json.loads(event[0])['reason'],'roundtrip_cost_or_inconsistent_quotes')

    def test_rejected_high_volume_token_does_not_starve_other_signals(self):
        self.engine.entry_attempts = {'high':NOW}
        high,low = replace(self.snap(),volume_5m=100000*bot.USD),self.snap()
        self.assertEqual(self.engine.pending_items({'high':{},'low':{}},{'high':high,'low':low})[0][0],'low')

    def test_no_clock_exit_missing_quotes_preserve_holding_and_stop_exits_during_halt(self):
        p = self.buy()
        self.engine.current = {}
        self.assertIsNone(self.engine.exit_reason(p,p['cost'],NOW+86400))
        class Missing:
            def quote(self,*a):
                raise bot.DataError('offline')
        self.engine.quotes = Missing()
        self.engine.step([],NOW+30)
        self.assertIn(bot.SOL,self.store.load()['positions'])
        self.engine.quotes = self.q
        s = self.store.load()
        s['halted'] = True
        with self.store.db:
            self.store.save(s)
        self.q.price *= bot.decimal('.95')
        self.engine.step([],NOW+45)
        self.assertFalse(self.store.load()['positions'])
        self.assertEqual(self.store.load()['setup_results']['recovery']['closed'],1)

    def test_diagnostics_persist_count_repeated_evaluations_and_reset_only_daily_slice(self):
        self.feed.bars[bot.SOL] = bars()
        snap = replace(self.snap(),volume_5m=1)
        self.engine.step([snap],NOW)
        self.engine.step([replace(snap,observed=NOW+15)],NOW+15)
        d = self.store.load()['active_diagnostics']
        self.assertEqual(d['total']['insufficient_volume'],2)
        self.assertEqual(d['today']['insufficient_volume'],2)
        self.engine = ScalpEngine(self.store,self.c,self.q)
        self.engine.candles = self.feed
        self.engine.step([replace(snap,observed=NOW+86400)],NOW+86400)
        d = self.store.load()['active_diagnostics']
        self.assertEqual(d['total']['insufficient_volume'],3)
        self.assertEqual(d['today']['insufficient_volume'],1)
        self.engine.step([],NOW+86400)  # Duplicate cycle must not count twice.
        self.assertEqual(self.store.load()['active_diagnostics'],d)

    def test_migration_archives_v1_preserves_account_and_old_position_contract(self):
        from scalping_v1 import REVISION as OLD
        old = bot.Config.load(ROOT/'tests/pullback-v1-config.json')
        s = self.store.load()
        s.update(config=asdict(old),active_revision={'id':OLD,'ts':NOW-100},
                 scalp_results={'closed':3,'pnl':123},cash=700*bot.USD,halted=True,
                 day_halted=True,dashboard_paused=True,entries_today=12)
        p = dict(strategy=OLD,mint=bot.SOL,cost=200*bot.USD,quantity=1,opened=NOW-100,
                 symbol='SOL',stop_floor=196*bot.USD,take_profit_bps=300,
                 setup={'stop_price':'98','reclaim':'100'})
        s['positions'][bot.SOL] = p
        with self.store.db:
            self.store.save(s)
        migrate(self.path,self.c,NOW)
        after = self.store.load()
        for key in ('cash','realized','closed','equity','halted','day_halted','dashboard_paused','entries_today'):
            self.assertEqual(after[key],s[key],key)
        self.assertEqual(after['active_archives'][OLD]['results']['closed'],3)
        self.assertEqual(after['positions'][bot.SOL]['legacy_config'],asdict(old))
        self.assertIsNone(self.engine.exit_reason(after['positions'][bot.SOL],204*bot.USD,NOW))
        self.assertEqual(self.engine.exit_reason(after['positions'][bot.SOL],206*bot.USD,NOW),'take_profit')
        self.engine.on_close(after,bot.SOL,after['positions'][bot.SOL],6*bot.USD,NOW)
        self.assertEqual(after['scalp_results']['closed'],0)
        migrate(self.path,self.c,NOW+1)
        self.assertEqual(after,self.store.load())

    def test_export_exposes_diagnostics_and_type_results(self):
        self.buy()
        p = Path(self.tmp.name)/'dashboard.json'
        dashboard_state.export(self.store,p,bot.status,NOW+15,limits=LIMITS,profile=PROFILE)
        data = json.loads(p.read_text())
        self.assertEqual(data['profile']['revision'],REVISION)
        self.assertEqual(data['active_diagnostics']['cycles'],2)
        self.assertEqual(data['positions'][bot.SOL]['setup_type'],'recovery')

    def test_bad_or_stale_pool_rotates_to_active_alternative_without_fabrication(self):
        now = [NOW]
        calls = []
        other = '1'*32
        class Http:
            def json(self,url,headers):
                calls.append(url)
                if '/tokens/' in url:
                    return {'data':[{'attributes':{'address':a,'reserve_in_usd':'250000',
                        'volume_usd':{'m5':v}},'relationships':{'base_token':{'data':{'id':'solana_'+bot.SOL}},
                        'quote_token':{'data':{'id':'solana_other'}}}}
                        for a,v in [(bot.SOL,'20000'),(other,'10000')]]}
                rows = bars()
                if '/'+bot.SOL+'/ohlcv/' in url:
                    rows.append([rows[-1][0],'99','100','98','99','2'])
                return {'data':{'attributes':{'ohlcv_list':[[r[0],*[float(x) for x in r[1:]]] for r in rows]}}}
        feed = Candles(Http(),lambda:now[0])
        feed.refresh([self.snap()])
        now[0] += 15
        feed.refresh([self.snap()])
        self.assertEqual(feed.errors[bot.SOL],'invalid_candles')
        self.assertEqual(feed.pools[bot.SOL]['address'],other)
        self.assertFalse(feed.bars)
        now[0] += 15
        feed.refresh([self.snap()])
        self.assertIsNone(feed.get(bot.SOL,now[0])[1])
        self.assertEqual(len(calls),3)


if __name__ == '__main__':
    unittest.main()
