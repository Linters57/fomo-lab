"""Bounded dashboard exports and paper-only commands. No network credentials."""
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

LIMITS = {'max_position': (1_000_000, 400_000_000), 'max_positions': (1, 4),
          'stop_bps': (500, 2000), 'take_profit_bps': (800, 6000)}


def atomic_json(path, value):
    temp = Path(str(path) + '.tmp')
    with temp.open('w') as f:
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def restore_config(path, base, Config, limits=None):
    limits = LIMITS if limits is None else limits
    if not Path(path).exists():
        return base
    import sqlite3
    with sqlite3.connect(path) as db:
        row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
    if not row:
        return base
    state = json.loads(row[0])
    # Only values explicitly changed via the dashboard may differ from base.
    overrides = state.get('dashboard_overrides', {})
    for key, value in overrides.items():
        if key not in limits or type(value) is not int or not limits[key][0] <= value <= limits[key][1]:
            raise ValueError('invalid saved dashboard setting')
    return Config(**{**asdict(base), **overrides}).validate()


def migrate_authorized_capital(path, target, now=None):
    """One-time requested 100 -> 1000 paper capital change, under ProcessLock.

    Preserve trades, P&L, cost accrual and halt flags. Never count funding as profit.
    No generic experiment reset or automatic acceptance of unrelated config changes.
    """
    if not Path(path).exists() or target.initial_cash != 1_000_000_000:
        return
    import sqlite3
    db = sqlite3.connect(path, timeout=5)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
            if row is None:
                return
            state = json.loads(row[0])
            old = state['config']['initial_cash']
            if old == target.initial_cash:
                return
            expected = {**state['config'], 'initial_cash': target.initial_cash}
            if state['mode'] != 'paper' or old != 100_000_000 or expected != asdict(target):
                raise ValueError('capital migration does not match the authorized paper change')
            delta = target.initial_cash - old
            for key in ('cash', 'equity', 'last_priced_equity', 'day_start'):
                state[key] += delta
            state['config'] = asdict(target)
            state['pending'] = {}
            state['dashboard_revision'] = state.get('dashboard_revision', 0) + 1
            db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state),))
            db.execute('INSERT INTO events(ts,kind,body) VALUES (?,?,?)',
                       (int(time.time()) if now is None else now, 'CAPITAL_CHANGE',
                        json.dumps(dict(previous=old, current=target.initial_cash,
                                        delta=delta, reason='user_requested_starting_capital'))))
    finally:
        db.close()


def migrate_authorized_strategy(path, target, Config, now=None):
    """Apply the requested aggressive $1000 paper profile exactly once, under lock."""
    authorized = Config(**{'initial_cash': 1000000000, 'monthly_operating_cost': 250000, 'max_position': 250000000, 'max_positions': 3, 'reserve': 200000000, 'risk_per_trade': 30000000, 'daily_loss': 100000000, 'total_loss': 250000000, 'max_daily_entries': 12, 'stop_bps': 1200, 'take_profit_bps': 2400}).validate()
    if not Path(path).exists() or asdict(target) != asdict(authorized):
        return
    import sqlite3
    db = sqlite3.connect(path, timeout=5)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
            if row is None:
                return
            state = json.loads(row[0])
            if state.get('strategy_migration') == 'aggressive_1000_v1':
                return
            overrides = state.get('dashboard_overrides', {})
            if state['config'] == {**asdict(authorized), **overrides}:
                return
            previous = asdict(Config(initial_cash=1_000_000_000, monthly_operating_cost=250_000))
            old_limits = {'max_position': (1_000_000, 10_000_000), 'max_positions': (1, 2),
                          'stop_bps': (500, 1000), 'take_profit_bps': (800, 3000)}
            if any(k not in old_limits or type(v) is not int or not old_limits[k][0] <= v <= old_limits[k][1]
                   for k, v in overrides.items()):
                raise ValueError('unexpected prior strategy override')
            if state['mode'] != 'paper' or state['config'] != {**previous, **overrides}:
                raise ValueError('strategy migration does not match the authorized paper change')
            before = state['config']
            state['config'] = asdict(authorized)
            state['dashboard_overrides'] = {}
            state['pending'] = {}
            state['strategy_migration'] = 'aggressive_1000_v1'
            state['dashboard_revision'] = state.get('dashboard_revision', 0) + 1
            db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state),))
            db.execute('INSERT INTO events(ts,kind,body) VALUES (?,?,?)',
                       (int(time.time()) if now is None else now, 'STRATEGY_CHANGE',
                        json.dumps(dict(profile='Offensief 1000', previous=before,
                                        current=state['config'], reason='user_requested_risk_profile'))))
    finally:
        db.close()


def apply_command(store, engine, path, Config, now=None, limits=None):
    limits = LIMITS if limits is None else limits
    now = int(time.time()) if now is None else now
    try:
        if not path.exists() or path.stat().st_size > 4096:
            return
        command = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(command, dict) or not isinstance(command.get('id'), str):
        return
    state = store.load()
    if state.get('dashboard_ack', {}).get('id') == command['id']:
        return
    updated_config = None
    error = None
    try:
        if state['mode'] != 'paper':
            raise ValueError('Alleen beschikbaar voor paper trading.')
        if type(command.get('expires')) is not int or command['expires'] < now:
            raise ValueError('Opdracht verlopen; opnieuw indienen.')
        if command.get('revision') != state.get('dashboard_revision', 0):
            raise ValueError('Instellingen gewijzigd; vernieuw eerst het dashboard.')
        if command.get('action') == 'pause' and type(command.get('paused')) is bool:
            state['dashboard_paused'] = command['paused']
            state['pending'] = {}
        elif command.get('action') == 'settings':
            if state['positions']:
                raise ValueError('Instellingen pas aanpassen als alle posities gesloten zijn.')
            changes = command.get('settings')
            if not isinstance(changes, dict) or not changes:
                raise ValueError('Geen geldige instellingen.')
            for key, value in changes.items():
                if key not in limits or type(value) is not int or not limits[key][0] <= value <= limits[key][1]:
                    raise ValueError('Instelling buiten de toegestane grenzen.')
            updated_config = Config(**{**state['config'], **changes}).validate()
            state['config'] = asdict(updated_config)
            state['dashboard_overrides'] = {**state.get('dashboard_overrides', {}), **changes}
            state['pending'] = {}
        else:
            raise ValueError('Onbekende opdracht.')
    except (ValueError, TypeError) as exc:
        error = str(exc)
    state['dashboard_ack'] = {'id': command['id'], 'ok': error is None,
                              'message': error or 'Toegepast door de paperbot.', 'ts': now}
    if error is None:
        state['dashboard_revision'] = state.get('dashboard_revision', 0) + 1
    with store.db:
        store.save(state)
        store.event(now, 'CONTROL', action=command.get('action'),
                    settings=command.get('settings'), paused=command.get('paused'),
                    command_id=command['id'], ok=error is None,
                    message=state['dashboard_ack']['message'])
    if updated_config is not None and error is None:
        engine.c = updated_config


def export(store, path, status_fn, now=None, limits=None, profile=None, max_bytes=350_000):
    now = int(time.time()) if now is None else now
    state = store.load()
    def events(where, limit):
        return [dict(id=i, ts=ts, kind=kind, **json.loads(body)) for i, ts, kind, body
                in store.db.execute('SELECT id,ts,kind,body FROM events WHERE ' + where +
                                    ' ORDER BY id DESC LIMIT ?', (limit,))]
    recent = events("kind != 'EQUITY'", 160)
    # Quotes remain in the durable audit; keep the online mirror compact.
    for event in recent:
        event.pop('quote', None)
        event.pop('exit_quote', None)
        if event['kind'] == 'SCAN':
            event.pop('tokens', None)
    trades = events("kind IN ('BUY','SELL')", 500)
    for event in trades:
        event.pop('quote', None)
        event.pop('exit_quote', None)
    scan = events("kind = 'SCAN'", 1)
    funding = events("kind = 'CAPITAL_CHANGE'", 100)
    curve = [dict(ts=ts, value=json.loads(raw)['value'] +
                  sum(change['delta'] for change in funding if change['id'] > event_id))
             for event_id, ts, raw in store.db.execute(
        "SELECT id,ts,body FROM events WHERE kind='EQUITY' ORDER BY id DESC LIMIT 720")][::-1]
    payload = dict(schema=1, generated_at=now, status=status_fn(store), config=state['config'],
                   positions=state['positions'], paused=state.get('dashboard_paused', False),
                   revision=state.get('dashboard_revision', 0), ack=state.get('dashboard_ack'),
                   tokens=scan[0].get('tokens', []) if scan else [],
                   scan_ts=scan[0]['ts'] if scan else None,
                   events=recent, trades=trades, curve=curve, capital_adjusted=bool(funding),
                   wins=state['wins'], started=state['started'], limits=LIMITS if limits is None else limits, profile=profile,
                   discovery=scan[0].get("discovery") if scan else None,
                   total_events=store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0])
    # Explicit retention limits in UI; trading ledger is never trimmed here.
    while len(json.dumps(payload).encode()) > max_bytes:
        if len(payload['events']) > 10:
            payload['events'] = payload['events'][:len(payload['events'])//2]
        elif len(payload['trades']) > 10:
            payload['trades'] = payload['trades'][:len(payload['trades'])//2]
        else:
            raise ValueError('dashboard export too large')
    atomic_json(path, payload)
