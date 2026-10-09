"""Two independently accounted paper experiments on one bounded worker.

Shared request pacing and read-only clients; no order execution or wallet access.
"""
from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import signal
import threading
import time
import urllib.parse
import bot
import dashboard_state as dashboard
from active import ActiveEngine, ActiveMarket, LIMITS as ACTIVE_LIMITS, PROFILE


class SharedHttp(bot.Http):
    def __init__(self, stop, transport=None, clock=time.monotonic):
        super().__init__(timeout=6)
        self.stop, self.transport, self.clock = stop, transport, clock
        self.next_request = 0
        self.cooldown = 0
        self.cache = {}

    def json(self, url, headers=None, payload=None):
        now = self.clock()
        jupiter = urllib.parse.urlparse(url).netloc == 'api.jup.ag'
        if jupiter and now < self.cooldown:
            raise bot.DataError('shared_api_backoff')
        cacheable = jupiter and '/tokens/v2/' in url and payload is None
        cached = self.cache.get(url)
        # Sharing category reads avoids duplicate requests; no quote caching.
        if cacheable and cached and now-cached[0] < 20:
            return json.loads(cached[1])
        wait = max(0, self.next_request-now)
        if self.stop.wait(wait):
            raise bot.DataError('shutdown')
        self.next_request = self.clock()+2.2  # <30 requests/min even without an API key
        try:
            data = (self.transport or super().json)(url, headers, payload)
        except bot.DataError as exc:
            if 'HTTP 429' in str(exc):
                self.cooldown = self.clock()+60
            raise
        if cacheable:
            self.cache[url] = (self.clock(), json.dumps(data))
            self.cache = dict(sorted(self.cache.items(), key=lambda x: -x[1][0])[:8])
        return data


def prune_active(store, now):
    """Bound raw research data; never delete trades, funding or control audit."""
    with store.db:
        store.db.execute('DELETE FROM snapshots WHERE ts < ?', (now-86400,))
        store.db.execute("DELETE FROM events WHERE kind='SCAN' AND ts < ?", (now-86400,))
        store.db.execute("DELETE FROM events WHERE kind='EQUITY' AND ts < ?", (now-7*86400,))
        store.db.execute("DELETE FROM events WHERE kind IN ('REJECT','SIGNAL','DATA_ERROR','UNPRICED','RUN_START','RUN_END') AND ts < ?", (now-7*86400,))
    store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')


class Experiment:
    def __init__(self, root, name, config_path, http, safety, stop, stack):
        self.name = name
        self.root = root if name == 'momentum' else root/'active'
        self.root.mkdir(exist_ok=True)
        path = self.root/'paper.sqlite'
        stack.enter_context(bot.ProcessLock(path))
        c = bot.Config.load(config_path)
        self.limits = dashboard.LIMITS if name == 'momentum' else ACTIVE_LIMITS
        self.profile = (dict(id='momentum', name='Momentum', strategy='5-minutenmomentum',
                            description='Het bestaande experiment met 5-minutenmomentum en 60-secondenbevestiging.')
                        if name == 'momentum' else PROFILE)
        if name == 'momentum':
            dashboard.migrate_authorized_strategy(path, c, bot.Config)
        c = dashboard.restore_config(path, c, bot.Config, self.limits)
        if name == 'momentum':
            dashboard.migrate_authorized_capital(path, c)
        self.store = bot.Store(path, c, 'paper')
        stack.callback(self.store.close)
        self.store.db.execute('PRAGMA journal_size_limit=4194304')
        if name == 'active':
            self.store.db.execute('PRAGMA max_page_count=49152') # 192 MiB, including retained audit
        self.engine = (bot.Engine if name == 'momentum' else ActiveEngine)(
            self.store, c, bot.Jupiter(http), safety, clock=lambda: int(time.time()),
            entries_allowed=lambda: not stop.is_set() and shutil.disk_usage(root).free > 128*1024*1024)
        self.market = bot.JupiterTokens(c, http) if name == 'momentum' else ActiveMarket(c, http)
        if name == 'active':
            history = self.store.load()['history']
            self.market.watch = {m: h[-1][0]+1800 for m, h in sorted(history.items(), key=lambda x: -x[1][-1][0])[:c.max_candidates] if h and h[-1][0]+1800 > time.time()}
        self.cursor = self.store.db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        self.due, self.failures, self.pruned_at = 0, 0, 0
        self.disabled = False
        with self.store.db:
            self.store.event(int(time.time()), 'RUN_START', version=bot.VERSION, mode='paper', profile=name)

    def export(self):
        dashboard.export(self.store, self.root/'dashboard.json', bot.status,
                         limits=self.limits, profile=self.profile, max_bytes=160_000)

    def cycle(self):
        begun = time.monotonic()
        dashboard.apply_command(self.store, self.engine, self.root/'dashboard-command.json',
                                bot.Config, limits=self.limits)
        self.market.config = self.engine.c
        snapshots = []
        try:
            snapshots = self.market.collect(self.store.load()['positions'])
            self.failures = 0
        except bot.DataError as exc:
            self.failures += 1
            with self.store.db:
                self.store.event(int(time.time()), 'DATA_ERROR', reason=str(exc))
        self.engine.step(snapshots, int(time.time()), getattr(self.market, 'discovery', None))
        self.export()
        for event_id, ts, kind, body in self.store.db.execute(
                "SELECT id,ts,kind,body FROM events WHERE id>? ORDER BY id", (self.cursor,)):
            if kind != 'EQUITY':
                event = json.loads(body)
                if kind == 'SCAN':
                    event.pop('tokens', None) # detailed scan retained on disk + dashboard
                event.pop('quote', None); event.pop('exit_quote', None)
                print('[fomo-paper] '+json.dumps({**event, 'event':kind, 'profile':self.name, 'ts':ts, 'audit_id':event_id}), flush=True)
            self.cursor = event_id
        print('[fomo-paper] '+json.dumps(dict(event='STATUS', profile=self.name, **bot.status(self.store))), flush=True)
        now = int(time.time())
        if self.name == 'active' and now-self.pruned_at >= 3600:
            prune_active(self.store, now)
            self.pruned_at = now
        # No accumulating catch-up jobs; exits remain checked while feed backs off.
        c = self.engine.c
        interval = c.poll_seconds if self.store.load()['positions'] else min(300, c.poll_seconds*2**min(self.failures, 2))
        self.due = max(time.monotonic()+1, begun+interval)


def run(root):
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    http = SharedHttp(stop)
    safety = bot.SolanaSafety(http)
    with ExitStack() as stack:
        jobs = []
        for name, config in [('momentum', 'shared-config.json'), ('active', 'active-config.json')]:
            try:
                jobs.append(Experiment(root, name, Path(__file__).with_name(config), http, safety, stop, stack))
            except (ValueError, bot.DataError) as exc:
                print('[fomo-paper] '+json.dumps(dict(event='START_FAILED', profile=name, reason=str(exc))), flush=True)
        if not jobs:
            return 78
        while not stop.is_set():
            # Existing positions and oldest due work first.
            due = [j for j in jobs if not j.disabled and j.due <= time.monotonic()]
            for job in sorted(due, key=lambda j: (not bool(j.store.load()['positions']), j.due)):
                if stop.is_set():
                    break
                try:
                    job.cycle()
                except Exception as exc:
                    # Do not let one experiment corrupt or stop the other.
                    job.disabled = True
                    print('[fomo-paper] '+json.dumps(dict(event='EXPERIMENT_STOPPED', profile=job.name, error=type(exc).__name__, reason=str(exc))), flush=True)
            if all(j.disabled for j in jobs):
                return 78
            stop.wait(0.5)
        for job in jobs:
            if not job.disabled:
                with job.store.db:
                    job.store.event(int(time.time()), 'RUN_END', version=bot.VERSION, reason='signal')
                job.export()
    return 0
