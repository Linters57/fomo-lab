"""Active paper experiment: short momentum/breakout, cost-aware triple-barrier exits.

The parameters are forward-test hypotheses, not optimised or proven returns.
See STRATEGY-RESEARCH.md for sources, exact rules and market-data limitations.
"""
from dataclasses import replace
import os
import time
import urllib.parse
from bot import Config, Engine, JupiterTokens, DataError, TOKEN_PROGRAM, USD, decimal

LIMITS = {'max_position': (10*USD, 250*USD), 'max_positions': (1, 4),
          'stop_bps': (200, 1200), 'take_profit_bps': (300, 3000)}
PROFILE = dict(id='active', name='Actief', strategy='Momentum + uitbraak',
               revision='adaptive-exits-v2', timed_exit=False,
               aged_stop_bps=200, aged_trailing_bps=200, cost_momentum_ratio=1.5,
               trailing_activation_bps=300, trailing_distance_bps=150,
               history_retention_seconds=1800, raw_retention_hours=24,
               description='3-minutenmomentum met koopdruk en uitbraak/voortzetting; 30-secondenbevestiging. '
                           'Geen verplichte tijdsverkoop: na 15 minuten een verliesgrens van 2% en een '
                           'meelopende stop op 2% afstand. Vanaf +3% blijft de trailing stop 1,5%. '
                           'Instapmomentum moet minstens 1,5 maal de geschatte retourkosten bedragen. '
                           'Experimenteel: nog geen bewezen rendement.')


class ActiveEngine(Engine):
    def observation_window(self, snap, history):
        past = [h for h in history if h[0] < snap.observed]
        past.append([snap.observed, snap.price, snap.pair])
        anchors = [i for i, h in enumerate(past) if h[0] <= snap.observed-self.c.warmup_seconds]
        return past[anchors[-1]:] if anchors else past

    def entry_cost_guard(self, snap, history, budget, drag):
        window = self.observation_window(snap, history)
        momentum = decimal(snap.price)/decimal(window[0][1])-1
        # Observed momentum is a filter, never a prediction of the next return.
        if momentum*budget < decimal('1.5')*drag:
            return 'momentum_too_small_for_costs'
        return None

    def pending_items(self, pending, current):
        # Bound entry quote work; rank previous signals by current buying share/volume.
        def rank(item):
            s = current.get(item[0])
            return (-(s.buys_5m/max(1,s.buys_5m+s.sells_5m)) if s else 0,
                    -s.volume_5m if s else 0, item[0])
        return sorted(pending.items(), key=rank)[:2]

    def history_seconds(self):
        # Keep enough to select an anchor relative to provider time, not wall time.
        return 2*self.c.warmup_seconds

    def keep_history(self, history, keep, ts):
        eligible = [(m, h[-40:]) for m, h in history.items() if h and
                    (m in keep or ts-h[-1][0] <= 1800)]
        return dict(sorted(eligible, key=lambda item: (item[0] not in keep, -item[1][-1][0]))[:80])

    def gate(self, snap, history, ts):
        c = self.c
        # Only real, distinct provider observations known at decision time.
        window = self.observation_window(snap, history)
        reason = super().gate(snap, window, ts)
        if reason:
            return reason
        if len(window) < 4:
            return 'warming_up'
        if snap.safe is not True:
            return 'mint_check:' + snap.safety_reason
        prices = [decimal(h[1]) for h in window]
        # A sampled-price high, NOT an OHLC candle high or VWAP.
        if prices[-1] < max(prices[:-1])*decimal('0.999'):
            return 'below_recent_high'
        ema = prices[0]
        for value in prices[1:]:
            ema += decimal('0.5')*(value-ema)
        if prices[-1] <= ema or prices[-1] <= prices[-2]:
            return 'trend_not_confirmed'
        return None

    def entry_guard(self, snap, pending, ts):
        reason = super().entry_guard(snap, pending, ts)
        if reason:
            return reason
        # Reusing the same provider update is not confirmation.
        if pending.get('observed') is not None and snap.observed <= pending['observed']:
            return 'awaiting_fresh_confirmation'
        return None

    def exit_reason(self, pos, net, ts):
        # Net proceeds already include modeled sell costs. A clock alone never sells.
        cost = pos['cost']
        pos['peak_exit_value'] = max(net, pos.get('peak_exit_value', pos['cost']))
        peak = pos['peak_exit_value']
        floor = cost*(10000-self.c.stop_bps)//10000
        label = 'stop_loss'
        if ts-pos['opened'] >= self.c.max_hold_seconds:
            # Start this peak at the first priced observation after the threshold;
            # do not treat an unseen pre-threshold high as an executable order.
            pos['aged_peak_exit_value'] = max(net, pos.get('aged_peak_exit_value', net))
            aged_floor = max(cost*9800//10000, pos['aged_peak_exit_value']*9800//10000)
            if aged_floor >= floor:
                floor, label = aged_floor, 'aged_stop'
        if peak*10000 >= cost*10300 and peak*9850//10000 > floor:
            floor, label = peak*9850//10000, 'trailing_stop'
        if pos.get('stop_floor', 0) > floor:
            floor, label = pos['stop_floor'], pos['stop_floor_reason']
        pos['stop_floor'], pos['stop_floor_reason'] = floor, label
        if net <= floor:
            return label
        if (net-cost)*10000 >= cost*self.c.take_profit_bps:
            return 'take_profit'
        return None


def activate_revision(store, now):
    """Record the authorized policy change once without resetting accounting/halts."""
    with store.db:
        state = store.load()
        if state.get('active_revision', {}).get('id') == PROFILE['revision']:
            return
        previous = state.get('active_revision', {}).get('id', 'fixed-time-v1')
        state['active_revision'] = dict(id=PROFILE['revision'], ts=now,
                                       equity=state['equity'], realized=state['realized'],
                                       closed=state['closed'])
        state['pending'] = {}
        state['dashboard_revision'] = state.get('dashboard_revision', 0)+1
        store.save(state)
        store.event(now, 'STRATEGY_CHANGE', previous_policy=previous,
                    current=state['config'], policy=PROFILE,
                    description=PROFILE['description'], baseline=state['active_revision'])


def restart_authorized_test(store, now):
    """One-time owner-requested funding and daily-entry reset, 2026-10-12.

    Preserve the ledger and historical losses. Funding is not trading profit.
    Never liquidate or erase a position to apply a reset.
    """
    reset_id = 'owner-request-2026-10-12-1117'
    with store.db:
        state = store.load()
        if state.get('test_restart', {}).get('id') == reset_id:
            return False
        if state['mode'] != 'paper' or state['positions'] or state['unpriced']:
            return False
        previous = state['cash']
        target = 1000*USD
        delta = target-previous
        baseline = dict(id=reset_id, ts=now, equity=target-state['operating_cost'],
                        previous_cash=previous, cash=target, funding_delta=delta,
                        realized=state['realized'], closed=state['closed'],
                        previous_entries_today=state['entries_today'])
        state['cash'] = target
        state['cash_adjustments'] = state.get('cash_adjustments', 0)+delta
        for key in ('equity', 'last_priced_equity', 'day_start'):
            state[key] = target-state['operating_cost']
        state.update(entries_today=0, day_halted=False, halted=False,
                     pending={}, cooldowns={}, test_restart=baseline)
        state['dashboard_revision'] = state.get('dashboard_revision', 0)+1
        store.save(state)
        store.event(now, 'CAPITAL_CHANGE', previous=previous, current=target,
                    delta=delta, reason='user_requested_paper_test_restart',
                    description='Testherstart: saldo naar 1.000 USDC en aankoopteller naar 0. '
                                'Fictieve bijstorting is geen handelswinst; eerdere resultaten blijven bewaard.',
                    baseline=baseline)
        return True



class ActiveMarket:
    SOURCES = ('toptraded/5m', 'toptraded/1h', 'toptrending/5m')

    def __init__(self, config, http, clock=time.time):
        self.config, self.http, self.clock = config, http, clock
        self.watch = {}
        self.refresh_at = 0
        self.discovery = {}

    @staticmethod
    def parse(row, received):
        item = JupiterTokens.parse(row, received)
        if item is None:
            return None
        try:
            audit = row.get('audit') or {}
            reputation = (row.get('isVerified') is True or
                          (decimal(row.get('organicScore', -1)) >= 60 and
                           decimal(row.get('holderCount', -1)) >= 500))
            approved = (reputation and audit.get('isSus', False) is False
                        and row.get('tokenProgram') == TOKEN_PROGRAM
                        and audit.get('mintAuthorityDisabled') is True
                        and audit.get('freezeAuthorityDisabled') is True
                        and 0 <= decimal(audit.get('topHoldersPercentage', -1)) <= 40)
            return replace(item, safe=bool(approved),
                           safety_reason='active_provider_checks_passed' if approved else 'active_provider_checks_failed')
        except (ValueError, TypeError, DataError):
            return replace(item, safe=False, safety_reason='active_provider_checks_missing')

    def collect(self, held=()):
        now = int(self.clock())
        headers = {'x-api-key': os.environ['JUPITER_API_KEY']} if os.environ.get('JUPITER_API_KEY') else {}
        if now >= self.refresh_at:
            rows, errors = {}, 0
            for source in self.SOURCES:
                try:
                    result = self.http.json('https://api.jup.ag/tokens/v2/'+source+'?limit=100', headers)
                    if not isinstance(result, list):
                        raise DataError('invalid discovery response')
                    for row in result:
                        if isinstance(row, dict) and isinstance(row.get('id'), str):
                            rows[row['id']] = row
                except DataError:
                    errors += 1
            candidates = [self.parse(r, int(self.clock())) for r in rows.values()]
            safe = [s for s in candidates if s is not None and s.safe]
            eligible = [s for s in safe if s.liquidity >= self.config.min_liquidity
                        and 0 <= now-s.observed <= self.config.sample_ttl_seconds
                        and now-s.pair_created >= self.config.min_pair_age_seconds]
            retained = {m: until for m, until in self.watch.items() if until > now}
            ranked = sorted(eligible, key=lambda s: (-s.volume_5m, s.mint))
            # Sticky 30-minute slots avoid erasing history on category turnover.
            for s in ranked:
                if s.mint in retained or len(retained) < self.config.max_candidates:
                    retained[s.mint] = now+1800
            self.watch = retained
            self.discovery = dict(raw_unique=len(rows), safety_pass=len(safe), eligible=len(eligible),
                                  watchlist=len(retained), source_errors=errors, refreshed_at=now,
                                  sources=list(self.SOURCES), cap=self.config.max_candidates)
            self.refresh_at = now+(300 if rows else 60)
        requested = sorted(set(self.watch) | set(held))
        if not requested:
            return []
        query = urllib.parse.urlencode({'query': ','.join(requested)})
        rows = self.http.json('https://api.jup.ag/tokens/v2/search?'+query, headers)
        if not isinstance(rows, list):
            raise DataError('invalid watchlist response')
        received = int(self.clock())
        parsed = [self.parse(r, received) for r in rows if isinstance(r, dict)]
        best = {s.mint: s for s in parsed if s is not None and s.mint in requested
                and 0 <= received-s.observed <= self.config.sample_ttl_seconds}
        self.discovery.update(fresh=len(best), watchlist=len(self.watch))
        # Unsafe updates reach the gate (for audit); never reuse an old safe value.
        return list(best.values())
