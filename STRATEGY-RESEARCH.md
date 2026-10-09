# Actief: research and implementation decision — 2026-10-09

This is a separate, long-only Solana **paper experiment** with $1,000 fictitious
capital. It does not place or sign orders and does not connect to a FOMO account.
The original Momentum experiment retains its capital, ledger and rules.

## What the research supports (and does not)

1. Hummingbot Position Executor documents stop-loss, take-profit, time barrier
   and trailing-stop exits. We implement those ideas with our existing quote-only
   ledger; we do not install Hummingbot or copy a claimed return.
   https://hummingbot.org/strategies/v2-strategies/executors/positionexecutor/
2. Wen et al., *Intraday return predictability in the cryptocurrency markets:
   Momentum, reversal, or both*, reports both effects. The abstract supports
   investigating momentum, not assuming it is universal or profitable at our horizon.
   https://www.sciencedirect.com/science/article/pii/S1062940822000833
3. Fičura, *Impact of size and volume on cryptocurrency momentum and reversal*,
   finds different behavior by size/liquidity on WEEKLY horizons. This is a reason
   to keep liquidity filters; it does NOT validate a three-minute Solana signal.
   https://wp.ffu.vse.cz/artkey/wps-202301-0003.php
4. *Technical analysis in cryptocurrency markets: Do transaction costs and bubbles
   matter?* explicitly studies transaction costs in technical-rule returns. Therefore
   roundtrip executable-route cost is a gate, not an ignored reporting detail.
   Abstract/search coverage reviewed; full publisher text was not accessible.
   https://www.sciencedirect.com/science/article/pii/S1042443122000816
5. Freqtrade documents lookahead bias and recommends forward testing. We use only
   provider observations already available at decision time, and require a subsequent
   distinct update before a hypothetical entry. Fixture tests are NOT a historical
   profitability backtest. No trustworthy historical minute dataset for our dynamic
   Solana universe has been loaded, so no Sharpe, win-rate or profitability claim is made.
   https://www.freqtrade.io/en/stable/lookahead-analysis/
   https://www.freqtrade.io/en/stable/strategy-customization/
6. Jupiter documents multiple category lists and batches of up to 100 known mint
   addresses. Its keyless shared Token/Swap bucket is 30 requests/minute; Free with
   a key is 60/minute. We stay below keyless limits regardless of key presence.
   https://developers.jup.ag/docs/tokens/token-information
   https://developers.jup.ag/docs/api-reference/tokens/category
   https://developers.jup.ag/docs/api-reference/tokens/search
   https://developers.jup.ag/docs/portal/rate-limits

## Selected test hypothesis

Short momentum plus a sampled-price breakout/continuation near a recent high,
with positive buying activity and cost-aware exits. Compared with market making,
this suits our periodic snapshots and quote-only simulation better: we lack an
order book, limit-order queue position and fill data. Grid/martingale would add
positions to losses and is not used. No LLM calls or paid AI subscriptions are needed.
Exact thresholds below are engineering hypotheses for a forward test, not estimates
optimised from the papers.

### Universe

Every five minutes fetch up to 100 from each of toptraded/5m, toptraded/1h and
toptrending/5m. Deduplicate. Retain up to 20 watchlist slots for 30 minutes,
refilling by five-minute volume. Every target 30 seconds batch-refresh those mints
plus all held assets, even if they disappear from category lists. Data outages can
lengthen the interval; old snapshots never replace fresh quotes.

Token gates: conventional SPL Token program; mint and freeze authorities revoked;
no positive suspicious flag; top-holder share at most 40%; verified OR organic
score >=60 AND holder count >=500. Missing audit data fails. An independent RPC
mint check is required before entry. These are imperfect filters, not a rug-pull
audit. Token-2022 is deliberately unsupported. Liquidity >=$100k, pool age >=6h.

### Entries

- At least $10k five-minute volume, 20 buys and 52% buys by transaction count.
- Three-minute sampled price return +0.3% through +5%, at least four distinct
  observations and no gap >120 seconds. Observations must be <=60 seconds old.
- Price within 0.1% of, or above, the earlier sampled high, rising versus previous
  observation and above a sampled-price EMA (alpha 0.5).
- Confirm again on a subsequent distinct provider update, nominally 30 seconds later.
  No more than 1.5% absolute movement from the prior signal.
- Check at most two prior signals per cycle, ranked by current buying share then
  volume, to bound RPC and quote work. Other candidates continue to be observed.
- Hypothetical buy and immediate sell-back quotes must have combined modeled drag
  <=1.5% (including per-side haircut and fixed costs). Quote TTL 15 seconds.
- Maximum $200 cost per position, 4 positions, $100 reserve, $8 planned stop-risk
  per position, 48 entries/day. Budget is also capped by risk/stop and available cash.
- No duplicate position, 5-minute post-exit cooldown. Existing halts/pause apply.

### Exits and losses

Net estimated proceeds after modeled costs trigger 4% stop, 8% profit target or
15-minute time exit. From a peak net gain of 3%, a 1.5% decline from that peak triggers
a trailing exit. Peak is persisted in the position. Stops are observed at polls,
not guaranteed prices; gaps or missing routes can exceed any threshold.

New entries halt at $150 day loss or $350 loss versus starting capital. Exit checks
continue. Missing exit valuation contributes zero to a disclosed lower bound and
blocks new entries; it never causes an invented sale. No leverage or averaging down.

## Resource and accounting isolation

Same paid worker, same 1 GB disk, same existing web service; no new Render service,
paid API tier or infrastructure purchase. Separate SQLite DB/lock/commands/Redis
keys for Actief under /var/data/fomo-lab/active. Shared read-only HTTP client with
2.2s minimum request spacing, bounded token-response cache, 60s backoff after 429.
Experiments run serially in the existing 192 MiB address-space allowance with
position-bearing jobs first. Scanner remains in its separate supervised process.

Each dashboard snapshot is <=160 kB, together below the former single-bot ceiling.
Actief retains raw snapshots/scans for 24h, equity points for 7 days and diagnostic
messages for 7 days. Trades/funding/settings audit remains. Active DB hard cap
192 MiB; low disk space blocks new entries. This bounds growth without silently
paying for more disk. Existing Momentum retention is unchanged.

Both bots start with their OWN $1000. No transfer or profit is fabricated.
Incremental host-cost assumption for Actief is zero because it reuses paid capacity;
existing $0.25/month storage modeling remains only in Momentum. Comparison explicitly
shows different start times, so cumulative profits are not a controlled experiment.

## Validation scope

Fixture tests cover separate balances/control queues, fresh confirmation, quote
cost rejection, no future history use, retained watchlists, four exit barriers,
restart settings, raw-data retention and shared rate limiting. A synthetic load
simulation measures implementation cost only, not achievable market performance.
Live discovery and resource checks after deployment are required before declaring
successful launch. Local keyless API probes returned HTTP 403; no bypass attempted.
The existing Render process already accesses Jupiter; production checks establish
whether the added documented endpoints are available there.
