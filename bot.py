#!/usr/bin/env python3
"""Fomo Lab 0.1: read-only Solana data + a persistent paper ledger. No signer.

Python 3.11+, standard library only. All money is integer micro-USDC; USDC/USD
parity is a modelling assumption, not a guarantee. See README.md before use.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = '0.3.0'
USD = 1_000_000
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
SOL = 'So11111111111111111111111111111111111111112'
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
BASE58 = set('123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz')


class DataError(Exception):
    pass


def decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DataError('invalid number') from None
    if not result.is_finite():
        raise DataError('non-finite number')
    return result


def integer(value):
    result = decimal(value)
    if result != result.to_integral_value():
        raise DataError('fractional integer')
    return int(result)


def mint_ok(value):
    return isinstance(value, str) and 32 <= len(value) <= 44 and set(value) <= BASE58


def dollars(value):
    return f'{Decimal(value) / USD:.4f}'


@dataclass(frozen=True)
class Config:
    market_source: str = 'jupiter'
    initial_cash: int = 100 * USD
    max_position: int = 10 * USD
    max_positions: int = 2
    reserve: int = 20 * USD
    risk_per_trade: int = USD
    daily_loss: int = 3 * USD
    total_loss: int = 10 * USD
    max_daily_entries: int = 4
    stop_bps: int = 800
    take_profit_bps: int = 1600
    max_hold_seconds: int = 3600
    cooldown_seconds: int = 1800
    poll_seconds: int = 60
    warmup_seconds: int = 300
    max_history_gap_seconds: int = 90
    min_momentum_bps: int = 100
    max_momentum_bps: int = 600
    max_chase_bps: int = 200
    min_liquidity: int = 250_000 * USD
    min_volume_5m: int = 5_000 * USD
    min_buys_5m: int = 20
    min_buy_share_bps: int = 5500
    min_pair_age_seconds: int = 86400
    max_roundtrip_cost_bps: int = 500
    quote_haircut_bps: int = 25
    # Fixed conservative cost ASSUMPTIONS per side, not verified live fees.
    network_cost: int = 30_000
    account_setup_cost: int = 250_000
    monthly_operating_cost: int = 0
    quote_ttl_seconds: int = 15
    sample_ttl_seconds: int = 90
    max_candidates: int = 8
    daily_timezone: str = 'Europe/Berlin'

    def validate(self):
        for key, value in asdict(self).items():
            if key not in ('daily_timezone', 'market_source') and (type(value) is not int or value < 0):
                raise ValueError(f'{key} must be a nonnegative integer')
        for key in ('initial_cash', 'max_position', 'max_positions', 'risk_per_trade',
                    'daily_loss', 'total_loss', 'max_daily_entries', 'stop_bps',
                    'take_profit_bps', 'max_hold_seconds', 'poll_seconds',
                    'warmup_seconds', 'max_history_gap_seconds', 'quote_ttl_seconds',
                    'sample_ttl_seconds', 'max_candidates'):
            if getattr(self, key) == 0:
                raise ValueError(f'{key} must be positive')
        if not (self.min_momentum_bps < self.max_momentum_bps < 10000):
            raise ValueError('invalid momentum range')
        for key in ('stop_bps', 'quote_haircut_bps', 'max_roundtrip_cost_bps', 'min_buy_share_bps'):
            if getattr(self, key) >= 10000:
                raise ValueError(f'{key} must be below 10000')
        if not (self.reserve < self.initial_cash and self.max_position <= self.initial_cash):
            raise ValueError('invalid cash/position limits')
        if self.poll_seconds > self.max_history_gap_seconds:
            raise ValueError('poll interval exceeds history gap')
        if self.max_candidates > 20:
            raise ValueError('candidate cap is 20')
        ZoneInfo(self.daily_timezone)
        if self.market_source not in ('jupiter', 'dexscreener'):
            raise ValueError('unknown market source')
        return self

    @classmethod
    def load(cls, path):
        raw = json.loads(Path(path).read_text()) if path else {}
        return cls(**raw).validate()


@dataclass(frozen=True)
class Snapshot:
    mint: str
    symbol: str
    observed: int
    price: str
    liquidity: int
    volume_5m: int
    buys_5m: int
    sells_5m: int
    pair_created: int
    pair: str
    safe: bool = False
    safety_reason: str = 'not_checked'


@dataclass(frozen=True)
class Quote:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    observed: int
    fee_bps: int
    source: str


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError('redirect refused')


class Http:
    def __init__(self, timeout=10):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def json(self, url, headers=None, payload=None):
        if urllib.parse.urlparse(url).scheme != 'https':
            raise DataError('HTTPS required')
        body = None if payload is None else json.dumps(payload).encode()
        hdr = {'User-Agent': 'FomoLab/0.1 (paper research)', 'Accept': 'application/json'}
        if body is not None:
            hdr['Content-Type'] = 'application/json'
        hdr.update(headers or {})
        req = urllib.request.Request(url, data=body, headers=hdr)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(4_000_001)
                if len(raw) > 4_000_000:
                    raise DataError('oversized response')
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise DataError(f'HTTP {exc.code}') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise DataError('network unavailable or timed out') from None
        except (ValueError, UnicodeDecodeError):
            raise DataError('invalid JSON response') from None


class Jupiter:
    def __init__(self, http=None):
        self.http = http or Http()

    def quote(self, input_mint, output_mint, amount, now):
        if not mint_ok(input_mint) or not mint_ok(output_mint) or amount <= 0:
            raise DataError('invalid quote request')
        params = urllib.parse.urlencode({'inputMint': input_mint, 'outputMint': output_mint,
                                         'amount': str(amount)})
        key = os.environ.get('JUPITER_API_KEY')
        headers = {'x-api-key': key} if key else {}
        started = int(time.time())
        # No taker, private key, /execute call, wallet transaction or signing code.
        data = self.http.json('https://api.jup.ag/swap/v2/order?' + params, headers)
        if not isinstance(data, dict) or data.get('error') or data.get('errorCode'):
            raise DataError('quote rejected')
        try:
            if (data['inputMint'] != input_mint or data['outputMint'] != output_mint
                    or integer(data['inAmount']) != amount or data.get('swapMode') != 'ExactIn'):
                raise DataError('quote identity mismatch')
            output = integer(data['outAmount'])
            fees = integer(data['feeBps'])
            if output <= 0 or not 0 <= fees < 10000:
                raise DataError('invalid quote amounts')
        except (KeyError, TypeError):
            raise DataError('missing quote fields') from None
        return Quote(input_mint, output_mint, amount, output, started, fees, 'jupiter_quote_only')


class SolanaSafety:
    """Narrow mint checks, NOT a rug-pull audit or holder-concentration check."""
    def __init__(self, http=None):
        self.http = http or Http()
        self.endpoint = os.environ.get('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com')
        self.cache = {}

    def check(self, mint):
        if not mint_ok(mint):
            return False, 'invalid_mint'
        now = time.monotonic()
        if mint in self.cache and now - self.cache[mint][0] < 300:
            return self.cache[mint][1:]
        try:
            data = self.http.json(self.endpoint, payload={
                'jsonrpc': '2.0', 'id': 1, 'method': 'getAccountInfo',
                'params': [mint, {'encoding': 'jsonParsed', 'commitment': 'confirmed'}]})
            value = data['result']['value']
            if value['owner'] != TOKEN_PROGRAM:
                return False, 'unsupported_token_program'
            parsed = value['data']['parsed']
            info = parsed['info']
            if parsed['type'] != 'mint' or info['isInitialized'] is not True:
                return False, 'not_initialized_mint'
            if info['mintAuthority'] is not None or info['freezeAuthority'] is not None:
                return False, 'mint_or_freeze_authority'
            self.cache[mint] = (now, True, 'legacy_mint_authorities_revoked')
            return True, 'legacy_mint_authorities_revoked'
        except (DataError, KeyError, TypeError, ValueError):
            return False, 'safety_data_unavailable'


class DexScreener:
    def __init__(self, config, http=None):
        self.config = config
        self.http = http or Http()

    @staticmethod
    def parse(pair, observed):
        try:
            mint = pair['baseToken']['address']
            if pair['chainId'] != 'solana' or mint == USDC or not mint_ok(mint):
                return None
            price = decimal(pair['priceUsd'])
            liquidity = int(decimal(pair['liquidity']['usd']) * USD)
            volume = int(decimal(pair['volume']['m5']) * USD)
            buys = integer(pair['txns']['m5']['buys'])
            sells = integer(pair['txns']['m5']['sells'])
            created = integer(pair['pairCreatedAt']) // 1000
            if price <= 0 or min(liquidity, volume, buys, sells) < 0 or not 0 < created <= observed:
                return None
            return Snapshot(mint, str(pair['baseToken']['symbol'])[:40], observed, str(price),
                            liquidity, volume, buys, sells, created, str(pair['pairAddress']))
        except (KeyError, TypeError, ValueError, DataError):
            return None

    def collect(self, held=()):
        rows = []
        observed = int(time.time())
        # A bounded search universe, NOT a complete Solana universe or FOMO feed.
        data = self.http.json('https://api.dexscreener.com/latest/dex/search?q=SOL')
        if not isinstance(data, dict) or not isinstance(data.get('pairs'), list):
            raise DataError('invalid market response')
        rows.extend(data['pairs'])
        # Always refresh open positions, even when absent from discovery results.
        for mint in held:
            if not mint_ok(mint):
                raise DataError('invalid held mint')
            pairs = self.http.json('https://api.dexscreener.com/token-pairs/v1/solana/' + mint)
            if not isinstance(pairs, list):
                raise DataError('invalid held-token response')
            rows.extend(p for p in pairs if p.get('baseToken', {}).get('address') == mint)
        best = {}
        for row in rows:
            item = self.parse(row, observed)
            if item and (item.mint not in best or item.liquidity > best[item.mint].liquidity):
                best[item.mint] = item
        ordered = sorted(best.values(), key=lambda s: (-s.liquidity, s.mint))
        chosen = {s.mint: s for s in ordered[:self.config.max_candidates]}
        chosen.update({m: best[m] for m in held if m in best})
        if not chosen:
            raise DataError('no usable market snapshots')
        return list(chosen.values())


class JupiterTokens:
    """Token-level quotes and statistics, with provider update timestamps.

    Category membership is discovery, never proof of a profitable signal.
    Requires current provider authentication wherever keyless access is refused.
    """
    def __init__(self, config, http=None):
        self.config, self.http = config, http or Http()

    @staticmethod
    def parse(row, received):
        try:
            mint = row['id']
            if not mint_ok(mint) or mint == USDC:
                return None
            updated = int(datetime.fromisoformat(row['updatedAt'].replace('Z', '+00:00')).timestamp())
            created = int(datetime.fromisoformat(row['firstPool']['createdAt'].replace('Z', '+00:00')).timestamp())
            if updated > received + 5 or updated <= 0 or not 0 < created <= received:
                return None
            price = decimal(row['usdPrice'])
            liquidity = int(decimal(row['liquidity']) * USD)
            stats = row['stats5m']
            volume = int((decimal(stats['buyVolume']) + decimal(stats['sellVolume'])) * USD)
            buys, sells = integer(stats['numBuys']), integer(stats['numSells'])
            if price <= 0 or min(liquidity, volume, buys, sells) < 0:
                return None
            audit = row.get('audit') or {}
            # Provider checks are imperfect; also require the independent RPC check.
            approved = (row.get('isVerified') is True and 'isSus' not in audit
                        and row.get('tokenProgram') == TOKEN_PROGRAM
                        and audit.get('mintAuthorityDisabled') is True
                        and audit.get('freezeAuthorityDisabled') is True
                        and 'topHoldersPercentage' in audit
                        and 0 <= decimal(audit['topHoldersPercentage']) <= 30)
            return Snapshot(mint, str(row['symbol'])[:40], min(received, updated), str(price),
                            liquidity, volume, buys, sells, created, 'jupiter-token-price:' + mint,
                            bool(approved), 'provider_checks_passed' if approved else 'provider_checks_failed_or_missing')
        except (DataError, KeyError, TypeError, ValueError, OverflowError):
            return None

    def collect(self, held=()):
        received = int(time.time())
        key = os.environ.get('JUPITER_API_KEY')
        headers = {'x-api-key': key} if key else {}
        rows = self.http.json('https://api.jup.ag/tokens/v2/toptraded/5m?limit=50', headers)
        if not isinstance(rows, list):
            raise DataError('invalid token market response')
        if held:
            if any(not mint_ok(mint) for mint in held):
                raise DataError('invalid held mint')
            params = urllib.parse.urlencode({'query': ','.join(held)})
            extra = self.http.json('https://api.jup.ag/tokens/v2/search?' + params, headers)
            if not isinstance(extra, list):
                raise DataError('invalid held-token response')
            rows.extend(extra)
        parsed = [self.parse(row, received) for row in rows]
        best = {s.mint: s for s in parsed if s is not None
                and 0 <= received - s.observed <= self.config.sample_ttl_seconds}
        if not best:
            raise DataError('no fresh token observations')
        # Don't let ineligible assets crowd eligible candidates out of the small budget.
        eligible = sorted((s for s in best.values() if s.safe), key=lambda s: (-s.liquidity, s.mint))
        chosen = {s.mint: s for s in eligible[:self.config.max_candidates]}
        chosen.update({m: best[m] for m in held if m in best})
        return list(chosen.values())


def market_source(config):
    return JupiterTokens(config) if config.market_source == 'jupiter' else DexScreener(config)


class Store:
    def __init__(self, path, config=None, mode=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL,
            kind TEXT NOT NULL, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS snapshots (ts INTEGER NOT NULL, mint TEXT NOT NULL,
            body TEXT NOT NULL, PRIMARY KEY(ts,mint));
        ''')
        row = self.db.execute('SELECT body FROM state WHERE id=1').fetchone()
        if row is None:
            if config is None or mode not in ('demo', 'paper'):
                raise ValueError('database has no initialized experiment')
            self.save({'version': VERSION, 'config': asdict(config), 'mode': mode,
                       'cash': config.initial_cash, 'positions': {}, 'pending': {},
                       'history': {}, 'cooldowns': {}, 'accounts': [], 'last_cycle': -1,
                       'started': None, 'day': None, 'day_start': config.initial_cash,
                       'day_halted': False, 'entries_today': 0, 'halted': False,
                       'equity': config.initial_cash, 'last_priced_equity': config.initial_cash,
                       'operating_cost': 0,
                       'unpriced': [], 'realized': 0, 'closed': 0, 'wins': 0})
            self.db.commit()
        elif config is not None:
            state = json.loads(row[0])
            if state['config'] != asdict(config) or state['mode'] != mode:
                self.db.close()
                raise ValueError('experiment config/mode differs; use a new database')

    def load(self):
        return json.loads(self.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])

    def save(self, state):
        self.db.execute('INSERT OR REPLACE INTO state VALUES (1,?)', (json.dumps(state),))

    def event(self, ts, kind, **body):
        self.db.execute('INSERT INTO events(ts,kind,body) VALUES (?,?,?)',
                        (ts, kind, json.dumps(body)))

    def close(self):
        self.db.close()


class ProcessLock:
    def __init__(self, path):
        self.path = Path(str(path) + '.lock')

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.handle.write(b'0')
                self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise ValueError('another bot already uses this database') from None
        return self

    def __exit__(self, *args):
        self.handle.close()


class Engine:
    def __init__(self, store, config, quotes, safety=None, clock=None, entries_allowed=None):
        self.store, self.c, self.quotes = store, config, quotes
        self.safety = safety
        self.clock = clock  # None means simulated/replay time.
        self.entries_allowed = entries_allowed or (lambda: True)

    def entry_guard(self, snap, pending, ts):
        if not self.entries_allowed():
            return 'shutdown_requested'
        now = self.clock() if self.clock else ts
        if not 0 <= now - snap.observed <= self.c.sample_ttl_seconds:
            return 'stale_market'
        if not 0 <= now - pending['ts'] <= self.c.max_history_gap_seconds:
            return 'expired_signal'
        return None

    def quote(self, a, b, amount, ts):
        now = self.clock() if self.clock else ts
        q = self.quotes.quote(a, b, amount, now)
        after = self.clock() if self.clock else ts
        if (q.input_mint != a or q.output_mint != b or q.in_amount != amount
                or type(q.out_amount) is not int or q.out_amount <= 0
                or not 0 <= after - q.observed <= self.c.quote_ttl_seconds):
            raise DataError('mismatched or stale quote')
        # Quote platform/pool fees are already in output. Do not charge them twice.
        effective = q.out_amount * (10000 - self.c.quote_haircut_bps) // 10000
        if effective <= 0:
            raise DataError('zero effective output')
        return effective, q

    def gate(self, snap, history, ts):
        c = self.c
        if not 0 <= ts - snap.observed <= c.sample_ttl_seconds:
            return 'stale_market'
        if snap.liquidity < c.min_liquidity or snap.volume_5m < c.min_volume_5m:
            return 'liquidity_or_volume'
        if not 0 < snap.pair_created <= ts - c.min_pair_age_seconds:
            return 'pair_too_young_or_unknown'
        total = snap.buys_5m + snap.sells_5m
        if (snap.buys_5m < c.min_buys_5m or total <= 0
                or snap.buys_5m * 10000 < c.min_buy_share_bps * total):
            return 'weak_buy_activity'
        if len(history) < 2 or history[-1][0] - history[0][0] < c.warmup_seconds:
            return 'warming_up'
        if any(b[0] - a[0] > c.max_history_gap_seconds for a, b in zip(history, history[1:])):
            return 'history_gap'
        change = (decimal(snap.price) / decimal(history[0][1]) - 1) * 10000
        if not c.min_momentum_bps <= change <= c.max_momentum_bps:
            return 'momentum_outside_range'
        return None

    def pending_items(self, pending, current):
        return sorted(pending.items())

    def exit_reason(self, pos, net, ts):
        ret_bps = (net - pos['cost']) * 10000 // pos['cost']
        return ('stop_loss' if ret_bps <= -self.c.stop_bps else
                'take_profit' if ret_bps >= self.c.take_profit_bps else
                'time_exit' if ts - pos['opened'] >= self.c.max_hold_seconds else None)

    def history_seconds(self):
        return self.c.warmup_seconds

    def keep_history(self, history, keep, ts):
        return {m: h for m, h in history.items() if m in keep}

    def step(self, snapshots, ts, discovery=None):
        c, db = self.c, self.store
        with db.db:
            db.db.execute('BEGIN IMMEDIATE')
            s = db.load()
            if ts <= s['last_cycle']:
                return s
            if s['started'] is None:
                s['started'] = ts
            day = datetime.fromtimestamp(ts, ZoneInfo(c.daily_timezone)).date().isoformat()
            if day != s['day']:
                # Preserve the last observed account value across midnight/gaps.
                s.update(day=day, day_start=s['last_priced_equity'], day_halted=False, entries_today=0)
            s['operating_cost'] = c.monthly_operating_cost * (ts - s['started']) // (30 * 86400)
            current = {item.mint: item for item in snapshots}
            for item in current.values():
                if decimal(item.price) <= 0 or not 0 <= ts - item.observed <= c.sample_ttl_seconds:
                    continue
                db.db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?)',
                              (item.observed, item.mint, json.dumps(asdict(item))))
                history = s['history'].setdefault(item.mint, [])
                # Switching the liquidity pool must not produce artificial momentum.
                if history and history[-1][2] != item.pair:
                    history.clear()
                if not history or item.observed > history[-1][0]:
                    history.append([item.observed, item.price, item.pair])
                # Include one observation just before the lookback boundary.
                while len(history) > 2 and history[1][0] <= ts - self.history_seconds():
                    history.pop(0)
            old_pending = s['pending']
            s['pending'] = {}
            marks, unpriced, closed_this_cycle = {}, [], set()
            # Exits run even during a daily/total halt and without discovery data.
            for mint, pos in list(s['positions'].items()):
                try:
                    output, q = self.quote(mint, USDC, pos['quantity'], ts)
                    net = max(0, output - c.network_cost)
                except DataError as exc:
                    unpriced.append(mint)
                    db.event(ts, 'UNPRICED', mint=mint, reason=str(exc))
                    continue
                marks[mint] = net
                reason = self.exit_reason(pos, net, ts)
                if reason:
                    pnl = net - pos['cost']
                    s['cash'] += net
                    s['realized'] += pnl
                    s['closed'] += 1
                    s['wins'] += int(pnl > 0)
                    del s['positions'][mint]
                    del marks[mint]
                    s['cooldowns'][mint] = ts + c.cooldown_seconds
                    closed_this_cycle.add(mint)
                    db.event(ts, 'SELL', mint=mint, symbol=pos['symbol'], quantity=pos['quantity'],
                             proceeds=net, pnl=pnl, reason=reason, quote=asdict(q))
            # Unpriced positions contribute ZERO to a disclosed lower-bound valuation.
            # They are not magically sold or deleted. Any unpriced holding blocks entries.
            equity = s['cash'] + sum(marks.values()) - s['operating_cost']
            if not unpriced:
                if c.initial_cash - equity >= c.total_loss:
                    s['halted'] = True
                if s['day_start'] - equity >= c.daily_loss:
                    s['day_halted'] = True
            global_block = ('unpriced_position' if unpriced else 'total_loss_limit' if s['halted']
                            else 'daily_loss_limit' if s['day_halted'] else
                            'manually_paused' if s.get('dashboard_paused') else None)
            # Execute a prior-cycle signal only after a fresh market + safety recheck.
            for mint, pending in self.pending_items(old_pending, current):
                snap = current.get(mint)
                reason = global_block
                if not reason and not snap:
                    reason = 'expired_signal'
                if not reason:
                    reason = self.entry_guard(snap, pending, ts)
                if not reason:
                    reason = self.gate(snap, s['history'].get(mint, []), ts)
                if not reason and (mint in s['positions'] or mint in closed_this_cycle
                                   or s['cooldowns'].get(mint, 0) > ts):
                    reason = 'already_held_or_cooldown'
                if not reason and (len(s['positions']) >= c.max_positions
                                   or s['entries_today'] >= c.max_daily_entries):
                    reason = 'position_or_daily_entry_limit'
                if not reason:
                    chase = (decimal(snap.price) / decimal(pending['price']) - 1) * 10000
                    if abs(chase) > c.max_chase_bps:
                        reason = 'price_moved_after_signal'
                if not reason:
                    safe, detail = (snap.safe, snap.safety_reason)
                    if safe is True and self.safety:
                        safe, detail = self.safety.check(mint)
                    if safe is not True:
                        reason = 'mint_check:' + detail
                if not reason:
                    reason = self.entry_guard(snap, pending, ts)
                if reason:
                    db.event(ts, 'REJECT', mint=mint, reason=reason)
                    continue
                # Stop is defined on TOTAL entry cost, including modeled account rent.
                budget = min(c.max_position, c.risk_per_trade * 10000 // c.stop_bps,
                             s['cash'] - s['operating_cost'] - c.reserve)
                setup = 0 if mint in s['accounts'] else c.account_setup_cost
                amount = budget - c.network_cost - setup
                if amount <= 0:
                    db.event(ts, 'REJECT', mint=mint, reason='cash_reserve')
                    continue
                try:
                    quantity, buy_q = self.quote(USDC, mint, amount, ts)
                    back, sell_q = self.quote(mint, USDC, quantity, ts)
                    # Buy remains a hypothetical quote. Its age must still be valid
                    # after obtaining the exit quote.
                    now = self.clock() if self.clock else ts
                    if not 0 <= now - buy_q.observed <= c.quote_ttl_seconds:
                        raise DataError('entry quote expired during exit check')
                    proceeds = max(0, back - c.network_cost)
                    drag = budget - proceeds
                    if drag < 0 or drag * 10000 > budget * c.max_roundtrip_cost_bps:
                        raise DataError('roundtrip_cost_or_inconsistent_quotes')
                    if drag >= budget * c.stop_bps // 10000:
                        raise DataError('costs_consume_stop_budget')
                except DataError as exc:
                    db.event(ts, 'REJECT', mint=mint, reason=str(exc))
                    continue
                reason = self.entry_guard(snap, pending, ts)
                if reason:
                    db.event(ts, 'REJECT', mint=mint, reason=reason)
                    continue
                s['cash'] -= budget
                s['positions'][mint] = {'quantity': quantity, 'cost': budget, 'opened': ts,
                                        'symbol': snap.symbol}
                if mint not in s['accounts']:
                    s['accounts'].append(mint)
                s['entries_today'] += 1
                marks[mint] = proceeds
                equity -= drag
                # Costs can trip the loss limits; latch immediately for further entries.
                if c.initial_cash - equity >= c.total_loss:
                    s['halted'] = True
                    global_block = 'total_loss_limit'
                if s['day_start'] - equity >= c.daily_loss:
                    s['day_halted'] = True
                    global_block = global_block or 'daily_loss_limit'
                db.event(ts, 'BUY', mint=mint, symbol=snap.symbol, quantity=quantity,
                         cost=budget, modeled_setup=setup, roundtrip_drag=drag,
                         signal_ts=pending['ts'], quote=asdict(buy_q), exit_quote=asdict(sell_q))
            scan_reasons = {}
            for mint, snap in sorted(current.items()):
                if (global_block or mint in s['positions'] or mint in closed_this_cycle
                        or s['cooldowns'].get(mint, 0) > ts
                        or len(s['positions']) >= c.max_positions
                        or s['entries_today'] >= c.max_daily_entries):
                    scan_reasons[mint] = global_block or 'position_limit_held_or_cooldown'
                    continue
                reason = self.entry_guard(snap, {'ts': ts}, ts)
                reason = reason or self.gate(snap, s['history'].get(mint, []), ts)
                scan_reasons[mint] = reason or 'signal_for_next_cycle'
                if not reason:
                    s['pending'][mint] = {'ts': ts, 'price': snap.price, 'observed': snap.observed}
                    db.event(ts, 'SIGNAL', mint=mint, symbol=snap.symbol)
            db.event(ts, 'SCAN', candidates=len(current), reasons=scan_reasons, discovery=discovery,
                     tokens=[{**asdict(item), 'reason': scan_reasons[item.mint]}
                             for item in current.values()])
            # Bounded active memory; full snapshots remain in the audit database.
            keep = set(current) | set(s['positions'])
            s['history'] = self.keep_history(s['history'], keep, ts)
            s['cooldowns'] = {m: until for m, until in s['cooldowns'].items() if until > ts}
            s.update(last_cycle=ts, equity=s['cash'] + sum(marks.values()) - s['operating_cost'],
                     unpriced=unpriced)
            if not unpriced:
                s['last_priced_equity'] = s['equity']
            if s['cash'] < 0 or len(s['positions']) > c.max_positions:
                raise RuntimeError('ledger invariant violated')
            db.event(ts, 'EQUITY', value=s['equity'], unpriced=unpriced,
                     valuation='lower_bound' if unpriced else 'hypothetical_quotes',
                     halted=s['halted'], day_halted=s['day_halted'])
            db.save(s)
            return s


class DemoQuotes:
    def __init__(self):
        self.price = Decimal(1)
        self.failed = False

    def quote(self, a, b, amount, now):
        if self.failed:
            raise DataError('synthetic_exit_route_unavailable')
        raw = decimal(amount) / self.price if a == USDC else decimal(amount) * self.price
        output = int(raw * Decimal('0.995'))  # Synthetic, known 0.5% quote fee.
        return Quote(a, b, amount, output, now, 50, 'SYNTHETIC_NOT_MARKET_DATA')


def demo_snapshot(ts, price, mint=SOL, **changes):
    raw = dict(mint=mint, symbol='DEMO', observed=ts, price=str(price),
               liquidity=500_000 * USD, volume_5m=25_000 * USD,
               buys_5m=50, sells_5m=20, pair_created=ts - 172800,
               pair='synthetic-pool', safe=True, safety_reason='SYNTHETIC_ONLY')
    raw.update(changes)
    return Snapshot(**raw)


def status(store):
    s = store.load()
    return {'mode': s['mode'], 'cash_usdc': dollars(s['cash']),
            'equity_usdc_estimate': dollars(s['equity']),
            'open_positions': len(s['positions']), 'closed_trades': s['closed'],
            'realized_pnl_usdc': dollars(s['realized']), 'unpriced_positions': len(s['unpriced']),
            'operating_cost_usdc': dollars(s['operating_cost']),
            'total_halt': s['halted'], 'daily_halt': s['day_halted'],
            'last_cycle': s['last_cycle']}


def log_committed_events(store, after_id):
    """Mirror durable audit entries to private host logs, after commit only."""
    if store.db.in_transaction:
        raise RuntimeError('audit logging requires a committed transaction')
    for event_id, ts, kind, raw in store.db.execute(
            'SELECT id,ts,kind,body FROM events WHERE id > ? ORDER BY id', (after_id,)):
        body = json.loads(raw)
        if kind != 'EQUITY':
            if kind == 'SCAN':
                for token in body.get('tokens', []):
                    mint = token['mint']
                    print('[fomo-paper] ' + json.dumps({
                        'event': 'SCAN_TOKEN', 'ts': ts, 'audit_id': event_id,
                        **token, 'reason': body['reasons'].get(mint, 'not_selected')
                    }), flush=True)
            readable = {key + '_usdc': dollars(body[key]) for key in
                        ('cost', 'proceeds', 'pnl', 'modeled_setup', 'roundtrip_drag')
                        if key in body}
            print('[fomo-paper] ' + json.dumps({
                'event': kind, 'ts': ts, 'audit_id': event_id,
                **body, **readable}), flush=True)
        after_id = event_id
    return after_id


def report(store, path):
    s = store.load()
    rows = store.db.execute("SELECT ts,kind,body FROM events WHERE kind != 'EQUITY' ORDER BY id DESC LIMIT 500").fetchall()
    events = ''.join('<tr><td>' + html.escape(datetime.fromtimestamp(ts, timezone.utc).isoformat())
                     + '</td><td>' + html.escape(kind) + '</td><td><pre>'
                     + html.escape(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
                     + '</pre></td></tr>' for ts, kind, body in rows)
    cards = ''.join(f'<div><span>{html.escape(k)}</span><strong>{html.escape(str(v))}</strong></div>'
                    for k, v in status(store).items())
    label = 'SYNTHETISCHE DEMO — GEEN RENDEMENTSMETING' if s['mode'] == 'demo' else 'PAPER TRADING — GEEN ECHTE TRANSACTIES'
    output = f'''<!doctype html><html lang="nl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Fomo Lab | experiment</title><style>body{{font:16px system-ui;background:#101821;color:#edf4fa;margin:0;padding:32px;max-width:1200px}}
    h1{{font-size:34px}}.tag{{color:#f2ce79}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}
    .grid div{{background:#1d2a37;padding:18px;border-radius:10px}}span{{display:block;color:#abc1d1;font-size:13px}}strong{{font-size:24px}}
    table{{border-collapse:collapse;width:100%;margin-top:24px}}td,th{{padding:12px;border-bottom:1px solid #344350;text-align:left;vertical-align:top}}
    pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}p{{max-width:900px;line-height:1.6}}</style>
    <p class="tag">{label}</p><h1>Fomo Lab</h1><p>Een controleerbaar experiment met {html.escape(dollars(s['config']['initial_cash']))} virtuele USDC.
    Koersen, quotes en vaste kostenaannames zijn geen uitvoeringsgarantie. Onprijsbare posities blijven open en tellen
    als nul in de expliciet conservatieve ondergrens. USDC = USD is een rekenaanname.</p><section class="grid">{cards}</section>
    <h2>Posities</h2><pre>{html.escape(json.dumps(s['positions'], indent=2))}</pre>
    <h2>Recente gebeurtenissen (maximaal 500)</h2><table><tr><th>UTC</th><th>Actie</th><th>Details</th></tr>{events}</table></html>'''
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + '.tmp')
    temp.write_text(output, encoding='utf-8')
    temp.replace(target)


def run_demo(args, c):
    # Deliberately synthetic path for checking bookkeeping, not strategy returns.
    with ProcessLock(args.db):
        store = Store(args.db, c, 'demo')
        q = DemoQuotes()
        engine = Engine(store, c, q)
        start = 1_790_000_000
        prices = ['1', '1.005', '1.010', '1.015', '1.020', '1.025', '1.030', '1.30',
                  '1.20', '1.19', '1.18']
        for i, price in enumerate(prices):
            q.price = decimal(price)
            engine.step([demo_snapshot(start + i * 60, price)], start + i * 60)
        report(store, args.report)
        print(json.dumps(status(store), indent=2))
        store.close()


def run_paper(args, c):
    import dashboard_state as dashboard
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with ProcessLock(args.db):
        dashboard.migrate_authorized_strategy(args.db, c, Config)
        c = dashboard.restore_config(args.db, c, Config)
        dashboard.migrate_authorized_capital(args.db, c)
        store = Store(args.db, c, 'paper')
        dashboard_path = Path(args.db).parent / 'dashboard.json'
        command_path = Path(args.db).parent / 'dashboard-command.json'
        log_cursor = store.db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        market, safety = market_source(c), SolanaSafety()
        engine = Engine(store, c, Jupiter(), safety, clock=lambda: int(time.time()),
                        entries_allowed=lambda: not stop.is_set())
        with store.db:
            store.event(int(time.time()), 'RUN_START', version=VERSION, mode='paper')
        try:
            cycle, failures = 0, 0
            while not stop.is_set() and (args.cycles == 0 or cycle < args.cycles):
                begun = time.monotonic()
                dashboard.apply_command(store, engine, command_path, Config)
                c = engine.c
                snapshots = []
                try:
                    snapshots = market.collect(store.load()['positions'])
                    failures = 0
                except DataError as exc:
                    failures += 1
                    with store.db:
                        store.event(int(time.time()), 'DATA_ERROR', reason=str(exc))
                    print(json.dumps({'data_error': str(exc), 'entries_blocked': True}), flush=True)
                engine.step(snapshots, int(time.time()))
                log_cursor = log_committed_events(store, log_cursor)
                print('[fomo-paper] ' + json.dumps({'event': 'STATUS', **status(store)}), flush=True)
                report(store, args.report)
                dashboard.export(store, dashboard_path, status)
                cycle += 1
                if args.cycles and cycle >= args.cycles:
                    break
                # Back off when discovery fails, but keep timely exit checks for open positions.
                interval = c.poll_seconds if store.load()['positions'] else min(300, c.poll_seconds * 2 ** min(failures, 2))
                stop.wait(max(0, interval - (time.monotonic() - begun)))
        finally:
            with store.db:
                store.event(int(time.time()), 'RUN_END', version=VERSION,
                            reason='signal' if stop.is_set() else 'cycle_limit'
                            if args.cycles and cycle >= args.cycles else 'error')
            log_committed_events(store, log_cursor)
            dashboard.export(store, dashboard_path, status)
            store.close()


def doctor(c):
    results = {}
    for name, action in (
        ('market', lambda: {'eligible_tokens': len(market_source(c).collect())}),
        ('mint_check', lambda: dict(zip(('passed', 'reason'), SolanaSafety().check(SOL)))),
        ('quote', lambda: asdict(Jupiter().quote(USDC, SOL, 10 * USD, int(time.time())))),
    ):
        try:
            results[name] = action()
        except DataError as exc:
            results[name] = {'error': str(exc)}
    print(json.dumps(results, indent=2))
    return 0 if ('error' not in results['market'] and results['mint_check'].get('passed')
                 and 'error' not in results['quote']) else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='JSON config; integers in micro-USDC / bps')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('demo', 'paper', 'status', 'report'):
        p = sub.add_parser(command)
        p.add_argument('--db', default=f'state/{command if command in ("demo", "paper") else "paper"}.sqlite')
        if command in ('demo', 'paper', 'report'):
            p.add_argument('--report', default=f'state/{command}-report.html')
        if command == 'paper':
            p.add_argument('--cycles', type=int, default=0, help='0 = until stopped')
    sub.add_parser('doctor')
    args = parser.parse_args(argv)
    c = Config.load(args.config)
    if args.command == 'doctor':
        return doctor(c)
    if args.command == 'demo':
        run_demo(args, c)
    elif args.command == 'paper':
        if args.cycles < 0:
            raise ValueError('cycles cannot be negative')
        run_paper(args, c)
    else:
        if not Path(args.db).exists():
            raise ValueError('database does not exist')
        store = Store(args.db)
        if args.command == 'report':
            report(store, args.report)
        print(json.dumps(status(store), indent=2))
        store.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, DataError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
