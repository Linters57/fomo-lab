"""Synthetic resource exercise only; not a profitability backtest."""
import json
import resource
import tempfile
import time
from pathlib import Path
import bot
from active import ActiveEngine, LIMITS, PROFILE
import dashboard_state as dashboard

def main():
    resource.setrlimit(resource.RLIMIT_AS,(192*1024*1024,192*1024*1024))
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder);c=bot.Config.load(Path(__file__).with_name('active-config.json'))
        store=bot.Store(root/'paper.sqlite',c,'paper');q=bot.DemoQuotes();engine=ActiveEngine(store,c,q)
        start=1791550000;began=time.perf_counter();process=time.process_time();size=0
        for i in range(1000):
            price=str(1+(i%40)*.001);q.price=bot.decimal(price)
            # Distinct valid base58 fixture mints; no requests and no real assets.
            snapshots=[bot.demo_snapshot(start+i*30,price,mint=bot.SOL[:-2]+'1'+'123456789ABCDEFGHJKLM'[j]) for j in range(20)]
            engine.step(snapshots,start+i*30)
            dashboard.export(store,root/'dashboard.json',bot.status,start+i*30,limits=LIMITS,profile=PROFILE,max_bytes=160000)
            size=max(size,(root/'dashboard.json').stat().st_size)
        wall=time.perf_counter()-began;cpu=time.process_time()-process
        assert len(store.load()['history'])<=80 and size<=160000
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        print(json.dumps(dict(synthetic_only=True,cycles=1000,candidates_per_cycle=20,
          elapsed_seconds=round(wall,3),cpu_seconds=round(cpu,3),peak_rss_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,2),
          max_dashboard_bytes=size,database_bytes=(root/'paper.sqlite').stat().st_size,profitability_test=False)))
        store.close()
if __name__=='__main__':main()
