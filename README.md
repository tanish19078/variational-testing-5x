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
| `config.py` | Typed, immutable config from env / `.env` (Decimal money, never float) |
| `variational/models.py` | Pydantic wire models; decimals serialise as fixed-point strings |
| `variational/client.py` | Async httpx client: retries, backoff, typed errors, `DRY_RUN` |
| `variational/risk.py` | Risk engine: position cap + consecutive-failure kill switch |
| `variational/errors.py` | Typed exception hierarchy |
| `range_bot.py` | The range strategy: symmetric quotes, idempotent reconciliation |
| `monitor.py` | Read-only live market monitor + mark-price source |
| `signature_helper.py` | EIP-712 / EIP-2612 signing primitive for on-chain flows |
| `mock/mock_server.py` | Stdlib mock of the Omni order API, with fault injection |
| `tests/` | pytest suite driving the full lifecycle against the mock |
| `docs/` | write-ups — start with the frontend reverse-engineering explainer |

## Docs

- **[How We Read Variational Omni's Frontend](docs/frontend-api-reverse-engineering.md)**
  — a plain-language walkthrough (with diagrams) of how the web app's network
  requests were mapped: the two backends, the RFQ order model, the auth model,
  the instrument naming scheme, the error vocabulary, the Cloudflare wall and
  why we stopped there, and exactly which findings are proven vs. assumed.

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
- `STATS_URL` — the public stats feed the monitor / mark source reads

## Running

**1. Start the mock backend** (one terminal):

```bash
python -m mock.mock_server          # http://127.0.0.1:8787
```

**2. Run the bot** against it (another terminal):

```bash
python range_bot.py
```

With `DRY_RUN=false` and `VR_BASE_URL` pointed at the mock, the bot fetches a
real mark from the public feed, then places, reconciles, and cancels orders on
the mock's book. On shutdown it best-effort cancels all resting orders.

**Live read-only monitor** (no mock needed; hits the public stats feed only):

```bash
python monitor.py            # one snapshot for UNDERLYING
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
pytest
```

The suite starts the mock in-process on a random port and drives the real httpx
transport end to end: order place → `rfq_id` → cancel, `DRY_RUN` suppression,
retry/backoff on 503, error mapping (5xx / 401 / 429 / 400), the risk kill
switch and position-cap gating, and the bot's idempotent requote logic.

## Safety notes

- **No secrets are committed.** `.env`, caches, and reverse-engineering scratch
  are gitignored.
- If you ever place a live session cookie in `.env`, **rotate it after use** — a
  browser session token grants full account access.
- The client honours `DRY_RUN` (on by default), so an accidental run mutates
  nothing.
