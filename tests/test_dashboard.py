import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
import bot
import dashboard_state as dash

START=1790000000
class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.c=bot.Config().validate()
        self.store=bot.Store(self.root/'paper.sqlite',self.c,'paper')
        self.q=bot.DemoQuotes()
        self.engine=bot.Engine(self.store,self.c,self.q)
    def tearDown(self):
        self.store.close();self.temp.cleanup()
    def command(self, **changes):
        value=dict(id='test',expires=START+100,revision=0,action='pause',paused=True)
        value.update(changes);p=self.root/'command.json';p.write_text(json.dumps(value))
        dash.apply_command(self.store,self.engine,p,bot.Config,START)
        return self.store.load()
    def test_pause_persists_prevents_entry_but_allows_exit(self):
        for i in range(7):
            self.q.price=bot.decimal(1+i*.005)
            self.engine.step([bot.demo_snapshot(START+i*60,str(self.q.price))],START+i*60)
        self.assertEqual(len(self.store.load()['positions']),1)
        self.command()
        self.q.price=bot.decimal('1.30')
        self.engine.step([bot.demo_snapshot(START+420,'1.30')],START+420)
        s=self.store.load();self.assertTrue(s['dashboard_paused']);self.assertEqual(s['closed'],1)
        self.assertEqual(s['positions'],{});self.assertEqual(s['pending'],{})
    def test_settings_are_validated_durable_and_restore_without_reset(self):
        s=self.command(action='settings',settings={'max_position':5000000,'stop_bps':700})
        self.assertTrue(s['dashboard_ack']['ok']);self.assertEqual(s['cash'],100000000)
        self.assertEqual(self.engine.c.max_position,5000000)
        restored=dash.restore_config(self.root/'paper.sqlite',self.c,bot.Config)
        self.assertEqual(restored.stop_bps,700)
        other=bot.Store(self.root/'paper.sqlite',restored,'paper');other.close()
        self.assertEqual(len(list(self.store.db.execute("SELECT * FROM events WHERE kind='CONTROL'"))),1)
        dash.apply_command(self.store,self.engine,self.root/'command.json',bot.Config,START)
        self.assertEqual(len(list(self.store.db.execute("SELECT * FROM events WHERE kind='CONTROL'"))),1)
    def test_rejects_expired_stale_unknown_and_risky_changes(self):
        for i,changes in enumerate([{'expires':START-1},{'revision':9},{'action':'settings','settings':{'initial_cash':200000000}}, {'action':'settings','settings':{'max_position':11000000}}]):
            s=self.command(id=str(i),**changes);self.assertFalse(s['dashboard_ack']['ok']);self.assertEqual(s['config'],asdict(self.c))
    def test_settings_cannot_change_open_trade(self):
        s=self.store.load();s['positions']={'fake':{'cost':10000000}};self.store.save(s);self.store.db.commit()
        s=self.command(action='settings',settings={'stop_bps':600})
        self.assertFalse(s['dashboard_ack']['ok']);self.assertEqual(self.engine.c.stop_bps,800)
    def test_export_contains_true_snapshot_and_stays_bounded(self):
        self.engine.step([bot.demo_snapshot(START,'1')],START+2)
        p=self.root/'dashboard.json';dash.export(self.store,p,bot.status,START+2)
        data=json.loads(p.read_text());self.assertEqual(data['tokens'][0]['observed'],START)
        self.assertEqual(data['tokens'][0]['reason'],'warming_up')
        self.assertEqual(data['status']['cash_usdc'],'100.0000')
        self.assertLess(p.stat().st_size,350000)
