# variational-testing-5x

An asynchronous Python automation suite for a range-trading strategy on
[Variational Omni](https://omni.variational.io/), built as a **testing harness**.

Variational Omni does not currently expose a public trading API. So this suite
runs the full stack — client, risk engine, order lifecycle, and the range bot —
against a **local mock backend** that reproduces the venue's exact wire format
(endpoint paths, field names, the `rfq_id` round-trip, and error strings). A
separate, read-only monitor reads the venue's genuinely public, unauthenticated
stats endpoint for live mark prices.

Nothing here places live orders or circumvents any access control: order flow is
exercised entirely against `127.0.0.1`. The live network is touched only by the
read-only public stats feed.

## Layout

| Path | What it is |
|------|------------|
| `cli.py` | Single entry point for everything. `DRY_RUN` default; `--live` is the only way off it |
| `config.py` | Typed, immutable config from env / `.env` (Decimal money, never float) |
| `variational/models.py` | Pydantic wire models; decimals serialise as fixed-point strings |
| `variational/client.py` | Async httpx client: retries, backoff, typed errors, `DRY_RUN` |
| `variational/risk.py` | Risk engine: position + notional caps, loss and drawdown limits, kill switch |
| `variational/errors.py` | Typed exception hierarchy |
| `variational/ratelimit.py` | Token bucket, so we stay under our own request budget rather than discovering the venue's via 429 |
| `variational/metrics.py` | Session counters and latency percentiles |
| `variational/state.py` | Atomic JSON journal of live orders, for crash recovery |
| `variational/logging_setup.py` | Loguru console + rotating file sink, with credential scrubbing |
| `range_bot.py` | The range strategy: symmetric quotes, idempotent reconciliation |
| `monitor.py` | Read-only live market monitor + mark-price source |
| `signature_helper.py` | EIP-712 / EIP-2612 signing primitive for on-chain flows |
| `mock/mock_server.py` | Stdlib mock of the Omni order API: exact wire format, fault injection, fill engine |
| `tests/` | 85 pytest tests driving the full lifecycle against the mock over real HTTP |
| `docs/` | write-ups — start with [What We Built](docs/what-we-built.md) |

## Docs

- **[What We Built — The Whole Thing, In Plain English](docs/what-we-built.md)**
  — a map of the entire repository with diagrams: the architecture, one tick of
  the bot step by step, why there's no `float` anywhere, the four independent
  safety layers, how failures are classified and retried, the Windows clock bug
  in the rate limiter, how the tests are built and why they go over real HTTP,
  and an honest list of what this repo cannot do.

- **[How We Read Variational Omni's Frontend](docs/frontend-api-reverse-engineering.md)**
  — a plain-language walkthrough (with diagrams) of how the web app's network
  requests were mapped: the two backends, the RFQ order model, the auth model,
  the instrument naming scheme, the error vocabulary, the Cloudflare wall and
  why we stopped there, the four real bugs a live-tested third-party client
  exposed in our wire format, and exactly which findings are proven vs. assumed.

## Setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# POSIX:    source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then edit
```

Key `.env` knobs (see `.env.example` for the full list):

- `VR_BASE_URL` — order API base; defaults to the mock at `http://127.0.0.1:8787`
- `DRY_RUN` — when `true` (default) the client logs orders instead of sending them
- `UNDERLYING`, `RANGE_PCT`, `ORDER_SIZE`, `REQUOTE_TOLERANCE_PCT`
- `MAX_POSITION_SIZE`, `KILL_SWITCH_THRESHOLD`
- `MAX_NOTIONAL`, `DAILY_LOSS_LIMIT`, `MAX_DRAWDOWN` — leave **blank** to disable;
  blank means "not enforced", which is not the same as `0`
- `RATE_LIMIT_PER_S`, `RATE_LIMIT_BURST` — client-side token bucket; `0` disables
- `STATE_FILE`, `LOG_FILE`, `LOG_LEVEL`
- `STATS_URL` — the public stats feed the monitor / mark source reads

## Running

Everything goes through one entry point. `DRY_RUN` is on unless you pass
`--live`, and `--dry-run` wins if both are given, so the safe option can't be
lost to flag ordering.

**1. Start the mock backend** (one terminal):

```bash
python -m cli mock                  # http://127.0.0.1:8787
```

**2. Drive it** (another terminal):

```bash
python -m cli status                # positions, open orders, balance
python -m cli quote --qty 1         # what price would I get?

# Dry run — logs the exact payload, sends nothing
python -m cli place --side buy --price 1.90 --qty 1

# For real against the mock
python -m cli --live place --side buy --price 1.90 --qty 1
python -m cli --live place --side buy --qty 1 --market      # quote-based
python -m cli --live trigger --side sell --type stop_loss --trigger-price 1.80 --qty 1
python -m cli --live cancel-all
python -m cli --live close-all
```

**3. Move the market** and watch a resting order fill:

```bash
curl -X POST http://127.0.0.1:8787/__control \
     -H 'content-type: application/json' -d '{"mark":"1.85"}'
python -m cli status
```

**4. Run the bot:**

```bash
python -m cli run
```

With `--live` and `VR_BASE_URL` pointed at the mock, the bot fetches a real mark
from the public feed, then places, reconciles, and cancels orders on the mock's
book. On shutdown it best-effort cancels all resting orders.

**Live read-only monitor** (no mock needed; hits the public stats feed only):

```bash
python -m cli monitor
python monitor.py --watch    # refresh every POLL_INTERVAL_S
python monitor.py --all      # table of all listings, once
```

## How the strategy quotes

Each tick:

1. Fetch the current mark from the public stats feed.
2. Compute symmetric targets: `bid = mark * (1 - RANGE_PCT/100)`,
   `ask = mark * (1 + RANGE_PCT/100)`.
3. Cancel any resting order that has drifted beyond `REQUOTE_TOLERANCE_PCT` from
   its side's target; keep in-tolerance orders (idempotent — no churn).
4. Place a resting order on any side without an in-tolerance quote, subject to
   the risk engine's position cap and kill switch.

Every network call routes the risk engine's success/failure hooks, so three
consecutive server (5xx) or auth (401/403) failures trip the kill switch and
halt the bot.

## Tests

```bash
pytest          # 85 tests, ~12s, no network
```

The suite starts the mock in-process on a random port and drives the **real
httpx transport** end to end — it does not mock `httpx`. That covers the real
retry path, real backoff, real timeout handling, real header construction and
real JSON serialisation.

Covered: order place → `rfq_id` → cancel; `DRY_RUN` suppression on every
mutating endpoint; retry/backoff on 503; error mapping (5xx / 401 / 429 / 400);
the risk kill switch and position-cap gating; the bot's idempotent requote
logic; the quote-based market flow; positions parsed out of the live nested
`position_info` shape; mark-crossing fills with weighted-average entry;
reduce-only never flipping through zero; the rate limiter's burst, refill and
lock behaviour; metrics percentiles; the crash journal including corrupt-file
quarantine; credential scrubbing; and every CLI subcommand.

CI runs this on `{ubuntu, windows} × {3.10, 3.11, 3.12}`. Windows is in the
matrix on purpose: `time.monotonic()` has 15.6 ms resolution there, which
caused a real rate-limiter bug that no Linux-only matrix would have caught.

## Safety notes

- **No secrets are committed.** `.env`, caches, and reverse-engineering scratch
  are gitignored, and CI fails the build if a `vr-token` JWT, a `cf_clearance`
  value, an un-annotated wallet address, or a tracked `.env` ever appears.
- If you ever place a live session cookie in `.env`, **rotate it after use** — a
  browser session token grants full account access.
- Logs scrub credentials on the way out (cookies, JWTs, wallet addresses), and
  loguru's `diagnose` is off so a traceback can't dump a cookie out of a stack
  frame.
- The client honours `DRY_RUN` (on by default), so an accidental run mutates
  nothing.
- Orders are journalled to disk atomically *before* being treated as live, so a
  crash leaves a record to reconcile rather than invisible resting orders.
