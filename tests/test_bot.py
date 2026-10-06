import dataclasses
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import bot

START = 1_790_000_000
OTHER = 'JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN'  # fixture identifier only


class BotCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.c = bot.Config().validate()
        self.path = str(Path(self.temp.name) / 'paper.sqlite')
        self.store = bot.Store(self.path, self.c, 'demo')
        self.q = bot.DemoQuotes()
        self.engine = bot.Engine(self.store, self.c, self.q)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def step(self, i, price, **kwargs):
        self.q.price = Decimal(str(price))
        ts = START + i * 60
        return self.engine.step([bot.demo_snapshot(ts, price, **kwargs)], ts)

    def warmup(self):
        for i in range(6):
            self.step(i, Decimal('1') + Decimal('.005') * i)

    def enter(self):
        self.warmup()
        state = self.step(6, '1.030')
        self.assertEqual(len(state['positions']), 1)
        return state

    def events(self, kind):
        return [json.loads(row[0]) for row in self.store.db.execute(
            'SELECT body FROM events WHERE kind=? ORDER BY id', (kind,))]

    def test_next_cycle_entry_and_exact_integer_costs(self):
        self.warmup()
        state = self.store.load()
        self.assertFalse(state['positions'])
        self.assertIn(bot.SOL, state['pending'])
        state = self.step(6, '1.030')
        expected_amount = 10_000_000 - 30_000 - 250_000
        expected_qty = int(Decimal(expected_amount) / Decimal('1.030') * Decimal('.995'))
        expected_qty = expected_qty * 9975 // 10000
        expected_out = int(Decimal(expected_qty) * Decimal('1.030') * Decimal('.995')) * 9975 // 10000
        self.assertEqual(state['cash'], 90_000_000)
        self.assertEqual(state['positions'][bot.SOL]['quantity'], expected_qty)
        self.assertEqual(state['equity'], 90_000_000 + expected_out - 30_000)
        self.assertEqual(self.events('BUY')[0]['signal_ts'], START + 300)

    def test_shutdown_during_exit_quote_blocks_new_entry_and_signals(self):
        self.warmup()
        allowed = [True]
        original = self.q.quote
        def interrupted_quote(a, b, amount, now):
            result = original(a, b, amount, now)
            if b == bot.USDC:
                allowed[0] = False
            return result
        self.engine.entries_allowed = lambda: allowed[0]
        self.q.quote = interrupted_quote
        state = self.step(6, '1.030')
        self.assertFalse(state['positions'])
        self.assertFalse(state['pending'])
        self.assertEqual(state['cash'], self.c.initial_cash)
        self.assertEqual(self.events('REJECT')[-1]['reason'], 'shutdown_requested')

    def test_shutdown_still_allows_existing_position_exit(self):
        self.enter()
        self.engine.entries_allowed = lambda: False
        state = self.step(7, '1.30')
        self.assertFalse(state['positions'])
        self.assertEqual(state['closed'], 1)
        self.assertFalse(state['pending'])

    def test_paper_runner_handles_sigterm_and_records_clean_stop(self):
        from types import SimpleNamespace
        handlers = {}
        args = SimpleNamespace(db=str(Path(self.temp.name) / 'runner.sqlite'),
                               report=str(Path(self.temp.name) / 'runner.html'), cycles=0)
        class Market:
            def collect(inner, positions):
                handlers[bot.signal.SIGTERM](bot.signal.SIGTERM, None)
                return []
        with patch.object(bot.signal, 'signal', side_effect=lambda s, f: handlers.update({s: f})), \
                patch.object(bot, 'market_source', return_value=Market()), \
                patch('builtins.print'):
            bot.run_paper(args, self.c)
        with sqlite3.connect(args.db) as db:
            end = json.loads(db.execute("SELECT body FROM events WHERE kind='RUN_END'").fetchone()[0])
            self.assertEqual(end['reason'], 'signal')
            self.assertEqual(end['version'], bot.VERSION)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='BUY'").fetchone()[0], 0)
        # The writer lock must also have been released.
        with bot.ProcessLock(args.db):
            pass

    def test_signal_expires_during_safety_request(self):
        self.warmup()
        now = [START + 360]
        self.engine.clock = lambda: now[0]
        class SlowSafety:
            def check(inner, mint):
                now[0] += 31
                return True, 'fixture'
        self.engine.safety = SlowSafety()
        with patch.object(self.q, 'quote', wraps=self.q.quote) as quote:
            state = self.step(6, '1.030')
            quote.assert_not_called()
        self.assertFalse(state['positions'])
        self.assertEqual(self.events('REJECT')[-1]['reason'], 'expired_signal')

    def test_fresh_quotes_do_not_rescue_expired_signal(self):
        self.warmup()
        now = [START + 389]  # Signal is 89 seconds old before requesting quotes.
        self.engine.clock = lambda: now[0]
        original = self.q.quote
        def delayed_quote(a, b, amount, requested):
            now[0] += 2
            return original(a, b, amount, requested)
        self.q.quote = delayed_quote
        state = self.step(6, '1.030')
        self.assertFalse(state['positions'])
        self.assertEqual(state['cash'], self.c.initial_cash)
        self.assertEqual(self.events('REJECT')[-1]['reason'], 'expired_signal')

    def test_market_expires_during_safety_request(self):
        self.warmup()
        now = [START + 360]
        self.engine.clock = lambda: now[0]
        class SlowSafety:
            def check(inner, mint):
                now[0] += 91
                return True, 'fixture'
        self.engine.safety = SlowSafety()
        state = self.step(6, '1.030')
        self.assertFalse(state['positions'])
        self.assertFalse(state['pending'])
        self.assertEqual(self.events('REJECT')[-1]['reason'], 'stale_market')

    def test_fees_not_added_twice_and_profit_reconciles(self):
        self.enter()
        state = self.step(7, '1.30')
        self.assertFalse(state['positions'])
        self.assertEqual(state['cash'], self.c.initial_cash + state['realized'])
        self.assertEqual(state['equity'], state['cash'])
        self.assertEqual(state['closed'], 1)
        self.assertEqual(self.events('SELL')[0]['reason'], 'take_profit')
        self.assertEqual(len(self.events('BUY')), 1)

    def test_crash_can_exceed_stop_and_trips_daily_latch(self):
        self.enter()
        state = self.step(7, '.60')
        self.assertTrue(state['day_halted'])
        self.assertLess(state['realized'], -3 * bot.USD)
        self.assertFalse(state['positions'])
        self.assertEqual(self.events('SELL')[0]['reason'], 'stop_loss')

    def test_no_route_retains_position_and_blocks_entries(self):
        self.enter()
        self.q.failed = True
        state = self.step(7, '1.04')
        self.assertEqual(state['unpriced'], [bot.SOL])
        self.assertEqual(state['equity'], state['cash'])
        self.assertEqual(state['realized'], 0)
        self.assertEqual(state['closed'], 0)
        self.assertEqual(len(state['positions']), 1)
        self.assertEqual(state['pending'], {})
        self.q.failed = False
        state = self.step(8, '.60')
        self.assertEqual(state['closed'], 1)

    def test_duplicate_cycle_and_restart_do_not_duplicate_fill(self):
        initial = self.enter()
        state = self.step(6, '1.030')
        self.assertEqual(initial, state)
        self.store.close()
        self.store = bot.Store(self.path, self.c, 'demo')
        self.engine = bot.Engine(self.store, self.c, self.q)
        self.step(6, '1.030')
        self.assertEqual(len(self.events('BUY')), 1)

    def test_configuration_cannot_silently_reset_experiment(self):
        self.enter()
        with self.assertRaisesRegex(ValueError, 'differs'):
            bot.Store(self.path, dataclasses.replace(self.c, max_position=11 * bot.USD), 'demo')
        with self.assertRaisesRegex(ValueError, 'differs'):
            bot.Store(self.path, self.c, 'paper')

    def test_unknown_safety_blocks_trade(self):
        self.warmup()
        state = self.step(6, '1.030', safe=False, safety_reason='missing')
        self.assertFalse(state['positions'])
        self.assertIn('mint_check:missing', [e['reason'] for e in self.events('REJECT')])

    def test_chased_price_is_rejected(self):
        self.warmup()
        self.step(6, '1.05')
        self.assertFalse(self.store.load()['positions'])
        self.assertIn('price_moved_after_signal', [e['reason'] for e in self.events('REJECT')])

    def test_gap_in_history_blocks_signal(self):
        self.step(0, '1')
        self.step(5, '1.025')
        self.assertEqual(self.store.load()['pending'], {})

    def test_pool_switch_resets_momentum(self):
        self.warmup()
        self.step(6, '1.03', pair='another-pool')
        self.assertFalse(self.store.load()['positions'])
        self.assertEqual(len(self.store.load()['history'][bot.SOL]), 1)

    def test_stale_snapshot_cannot_fill(self):
        self.warmup()
        self.step(6, '1.03', observed=START)
        self.assertFalse(self.store.load()['positions'])

    def test_missing_discovery_does_not_disable_exit(self):
        self.enter()
        self.q.price = Decimal('.6')
        state = self.engine.step([], START + 420)
        self.assertEqual(state['closed'], 1)
        self.assertFalse(state['positions'])

    def test_exits_continue_after_halt(self):
        self.enter()
        s = self.store.load()
        s['halted'] = True
        with self.store.db:
            self.store.save(s)
        self.q.price = Decimal('1.30')
        state = self.engine.step([], START + 420)
        self.assertEqual(state['closed'], 1)
        self.assertTrue(state['halted'])

    def test_time_exit_and_total_loss_from_operating_costs(self):
        self.enter()
        self.q.price = Decimal('1.03')
        state = self.engine.step([], START + 4000)
        self.assertEqual(state['closed'], 1)
        self.assertEqual(self.events('SELL')[0]['reason'], 'time_exit')
        c = dataclasses.replace(self.c, monthly_operating_cost=20*bot.USD)
        other = bot.Store(str(Path(self.temp.name)/'costs.sqlite'), c, 'demo')
        try:
            engine = bot.Engine(other, c, self.q)
            engine.step([], START)
            state = engine.step([], START+30*86400)
            self.assertEqual(state['operating_cost'],20*bot.USD)
            self.assertEqual(state['equity'],80*bot.USD)
            self.assertTrue(state['halted'])
        finally:
            other.close()

    def test_loss_of_quote_does_not_realize_fictitious_loss(self):
        self.enter()
        previous = self.store.load()['last_priced_equity']
        self.q.failed = True
        self.step(7, '1.03')
        self.assertEqual(self.store.load()['last_priced_equity'], previous)
        self.assertFalse(self.store.load()['halted'])

    def test_position_limit_cash_reserve_and_risk_budget(self):
        self.enter()
        for i in range(7, 30):
            price = Decimal('1.03') + Decimal('.001') * i
            self.q.price = price
            ts = START + i * 60
            samples = [bot.demo_snapshot(ts, price), bot.demo_snapshot(ts, price, mint=OTHER)]
            state = self.engine.step(samples, ts)
            self.assertLessEqual(len(state['positions']), 2)
            self.assertGreaterEqual(state['cash'], self.c.reserve)
            for pos in state['positions'].values():
                self.assertLessEqual(pos['cost'] * self.c.stop_bps // 10000, self.c.risk_per_trade)

    def test_large_roundtrip_cost_rejected(self):
        self.warmup()
        original = self.q.quote
        def expensive(a, b, amount, now):
            quote = original(a, b, amount, now)
            return dataclasses.replace(quote, out_amount=quote.out_amount // 2)
        self.q.quote = expensive
        self.step(6, '1.03')
        self.assertFalse(self.store.load()['positions'])

    def test_stale_or_wrong_quote_rejected(self):
        self.warmup()
        original = self.q.quote
        self.q.quote = lambda a,b,n,t: dataclasses.replace(original(a,b,n,t), observed=t-100)
        self.step(6, '1.03')
        self.assertFalse(self.store.load()['positions'])
        self.assertIn('mismatched or stale quote', [e['reason'] for e in self.events('REJECT')])

    def test_transaction_rolls_back_if_commit_preparation_fails(self):
        self.warmup()
        before = self.store.load()
        original = self.store.save
        self.store.save = lambda state: (_ for _ in ()).throw(RuntimeError('simulated disk fault'))
        with self.assertRaises(RuntimeError):
            self.step(6, '1.03')
        self.store.save = original
        self.assertEqual(before, self.store.load())
        self.assertFalse(self.events('BUY'))
        self.step(6, '1.03')
        self.assertEqual(len(self.events('BUY')), 1)

    def test_day_change_resets_daily_latch_not_total_halt(self):
        self.enter()
        s = self.store.load()
        s.update(day_halted=True, halted=True, entries_today=4)
        with self.store.db:
            self.store.save(s)
        state = self.engine.step([], START + 86400)
        self.assertFalse(state['day_halted'])
        self.assertTrue(state['halted'])
        self.assertEqual(state['entries_today'], 0)

    def test_report_escapes_external_labels(self):
        self.warmup()
        self.step(6, '1.03', symbol='<script>alert(1)</script>')
        path = Path(self.temp.name) / 'report.html'
        bot.report(self.store, path)
        output = path.read_text()
        self.assertNotIn('<script>', output)
        self.assertIn('&lt;script&gt;', output)

    def test_process_lock_refuses_second_writer(self):
        with bot.ProcessLock(self.path):
            with self.assertRaisesRegex(ValueError, 'another bot'):
                with bot.ProcessLock(self.path):
                    pass


class AdapterCase(unittest.TestCase):
    def token_row(self):
        return {'id':bot.SOL, 'symbol':'SOL', 'usdPrice':120, 'liquidity':1000000,
                'updatedAt': '2026-09-21T00:00:00Z',
                'firstPool':{'createdAt':'2021-03-29T10:05:48Z'},
                'stats5m':{'buyVolume':20000,'sellVolume':10000,'numBuys':50,'numSells':20},
                'isVerified':True, 'tokenProgram':bot.TOKEN_PROGRAM,
                'audit':{'mintAuthorityDisabled':True,'freezeAuthorityDisabled':True,
                         'topHoldersPercentage':10}}

    def test_token_market_parser_preserves_provider_timestamp(self):
        row = self.token_row()
        received = int(bot.datetime.fromisoformat(row['updatedAt'].replace('Z','+00:00')).timestamp()) + 20
        snap = bot.JupiterTokens.parse(row, received)
        self.assertEqual(snap.observed, received-20)
        self.assertTrue(snap.safe)
        self.assertEqual(snap.volume_5m, 30000*bot.USD)

    def test_missing_holder_data_or_suspicion_prevents_entry(self):
        for audit_change in ({'isSus':False}, {'topHoldersPercentage':31}):
            row = self.token_row()
            row['audit'].update(audit_change)
            snap = bot.JupiterTokens.parse(row, 1_790_000_000)
            self.assertFalse(snap.safe)
        row = self.token_row()
        del row['audit']['topHoldersPercentage']
        self.assertFalse(bot.JupiterTokens.parse(row, 1_790_000_000).safe)

    def test_token_market_parser_rejects_missing_and_nonfinite(self):
        for changes in ({'usdPrice':'NaN'}, {'updatedAt':'unknown'}, {'liquidity':None}):
            with self.subTest(changes=changes):
                self.assertIsNone(bot.JupiterTokens.parse(self.token_row() | changes, START))

    def test_market_failure_never_falls_back_to_synthetic_prices(self):
        class FailingHttp:
            def json(self, *args, **kwargs):
                raise bot.DataError('HTTP 403')
        with self.assertRaises(bot.DataError):
            bot.JupiterTokens(bot.Config(), FailingHttp()).collect()

    def test_jupiter_quote_contract_and_no_execution(self):
        class FakeHttp:
            def json(self, url, headers):
                self.url = url
                self.headers = headers
                return {'inputMint':bot.USDC, 'outputMint':bot.SOL, 'inAmount':'10000000',
                        'outAmount':'50000000', 'swapMode':'ExactIn', 'feeBps':2,
                        'transaction':None}
        http = FakeHttp()
        with patch.dict('os.environ', {'JUPITER_API_KEY': 'test-secret'}):
            q = bot.Jupiter(http).quote(bot.USDC, bot.SOL, 10_000_000, START)
        self.assertEqual(q.out_amount, 50_000_000)
        self.assertIn('/order?', http.url)
        self.assertNotIn('taker', http.url)
        self.assertNotIn('test-secret', http.url)
        self.assertEqual(http.headers['x-api-key'], 'test-secret')

    def test_jupiter_rejects_wrong_amount_mint_error_and_nonfinite(self):
        base = {'inputMint':bot.USDC, 'outputMint':bot.SOL, 'inAmount':'10000000',
                'outAmount':'50000000', 'swapMode':'ExactIn', 'feeBps':2}
        for change in ({'inAmount':'9999999'}, {'inputMint':bot.SOL}, {'errorCode':1},
                       {'outAmount':'NaN'}, {'outAmount':'0'}, {'feeBps':-1},
                       {'outAmount':'1.1'}, {'swapMode':'ExactOut'}):
            with self.subTest(change=change):
                class FakeHttp:
                    def json(self, *a):
                        return base | change
                with self.assertRaises(bot.DataError):
                    bot.Jupiter(FakeHttp()).quote(bot.USDC, bot.SOL, 10_000_000, START)

    def test_missing_market_fields_and_quote_side_are_rejected(self):
        self.assertIsNone(bot.DexScreener.parse({'chainId':'solana'}, START))
        self.assertIsNone(bot.DexScreener.parse({'baseToken':{'address':bot.USDC},'chainId':'solana'}, START))

    def test_safety_rejects_unknown_program_or_authorities(self):
        safe = {'owner':bot.TOKEN_PROGRAM, 'data':{'parsed':{'type':'mint','info':{
            'isInitialized': True, 'mintAuthority': None, 'freezeAuthority': None}}}}
        class FakeHttp:
            def json(self, *args, **kwargs):
                return {'result':{'value':self.value}}
        http = FakeHttp()
        http.value = safe
        self.assertTrue(bot.SolanaSafety(http).check(bot.SOL)[0])
        import copy
        http.value = copy.deepcopy(safe)
        http.value['owner'] = 'Token2022-is-not-supported-in-v0.1'
        self.assertFalse(bot.SolanaSafety(http).check(bot.SOL)[0])
        http.value = copy.deepcopy(safe)
        http.value['data']['parsed']['info']['freezeAuthority'] = 'someone'
        self.assertFalse(bot.SolanaSafety(http).check(bot.SOL)[0])
        http.value = None
        self.assertFalse(bot.SolanaSafety(http).check(bot.SOL)[0])

    def test_config_validates_units_bounds_and_unknown_fields(self):
        for fields in ({'initial_cash':-1}, {'quote_haircut_bps':10000},
                       {'poll_seconds':200}, {'max_positions':0}, {'max_candidates':100}):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):
                    dataclasses.replace(bot.Config(), **fields).validate()


if __name__ == '__main__':
    unittest.main()
