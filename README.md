# GPTA — Gold Precision Trading Algorithm

Signal generation + paper trading for XAU/USD, built to the *Detailed Software Architectural Blueprint v2.0*.
**It never places broker orders** (`broker_execution_enabled: false`, locked). "Entry observed" never means a fill.

## Status — what is built and what is verified

| Blueprint area | State |
|---|---|
| Strategy engine (swings, H1/M15 trend, zones, sweeps, MSS, displacement/entry retest, SL/TP/R, volatility) | Built, unit + end-to-end tested on a synthetic BUY scenario; no-lookahead tested |
| Qualification (16 checks, machine-readable rejection codes; unavailable data never passes) | Built, tested |
| Signal lifecycle, duplicate guard, ambiguous TP/SL handling, expiry | Built, tested |
| Paper trading + metrics, backtest engine (same orchestrator as live), out-of-sample split | Built, tested |
| Twelve Data client, 4-key credential manager (encrypted, redacted), candle validator | Built; **client never run against the live API** (see below) |
| MT5 quote/spread engine + Windows bridge (`mt5_bridge/`) | Built; bridge logic tested with a fake MT5, **never run against a real terminal** |
| News gate (USD events, ±30 min blackout, strict failure) | Built; **ShowMyTrades client is a stub** (see below) |
| Telegram notifier (dedupe, retry, delivery tracking) + command bot | Built; sender/bot **never run against Telegram** |
| API (all blueprint §21 endpoints, bearer auth, rate-limited writes, versioned+audited settings) | Built, tested over real HTTP |
| Dashboard (9 pages) | Built, checked in a headless browser on simulated data |
| Docker, `.env.example`, requirements | Written, **not built** (no Docker/network in the build sandbox) |

**60 automated tests pass** (`cd backend && python -m pytest`). **No claim is made that the strategy is profitable.**
Nothing has been run on real market data. A 30-day random-walk backtest ran without errors (43k steps, 34 s) and,
as expected for noise, produced no qualified signals. Real validation = the blueprint's stages 1–4 (backtest on real
history, out-of-sample, walk-forward, paper trading) — none of which have been done.

## Run it

```bash
cp .env.example .env            # fill in keys + a long random GPTA_API_TOKEN
pip install -r backend/requirements.txt
cd backend && python -m pytest                       # all tests
python ../scripts/backfill_history.py 5000           # one-off warm-up/backtest history
uvicorn app.main:app_factory --factory --port 8000   # API + scheduler + dashboard at http://localhost:8000
```
Or `docker compose up --build` (put a TLS reverse proxy in front for HTTPS).
On the Windows PC with MT5 (demo account logged in): `pip install MetaTrader5 requests`, set `GPTA_BACKEND_URL` and
`GPTA_API_TOKEN`, run `py -m mt5_bridge.main` from the repo root.
Backtest on your own CSVs: `python scripts/backtest_csv.py --m1 … --m5 … --m15 … --h1 … --assume-no-news --oos 0.7`.

Until the news calendar is configured, **no signal can be published** (strict mode, by design).

## Things you must do/verify before trusting it (blueprint §27)

1. **ShowMyTrades**: I could not find public API documentation. `news/showmytrades_client.py` is a stub (set
   `SHOWMYTRADES_URL/KEY`, then adapt `fetch`). Until it works, the news check is UNAVAILABLE and blocks all signals.
2. **Twelve Data**: request shape (`/time_series`, `1min/5min/15min/1h`, `api-credits-*` headers) follows their docs as I
   know them; confirm XAU/USD availability, real credit usage and — important — that rotating four keys is allowed by
   their terms for your accounts.
3. **MT5**: confirm your broker's gold symbol (auto-discovered) and server clock offset (auto-estimated from the first tick; set `MT5_SERVER_UTC_OFFSET_SECONDS` to be exact). A wrong offset makes quotes look stale, which fails safe (no signals).
4. Dev database is SQLite. `database/store.py` is plain SQL isolated in one class; moving to PostgreSQL means swapping that class (SQLAlchemy/psycopg were not installable in the build sandbox).

## Deviations from the blueprint (and why)

- **Starlette instead of FastAPI, sqlite3 instead of SQLAlchemy, threads instead of APScheduler, vanilla JS dashboard instead of React/Tailwind/Lightweight Charts** — none of those packages could be installed in the build environment (no network), so I used what could be tested. Handlers are plain functions and port directly. The dashboard is one file with a hand-drawn candle chart. Swap in React later if you want; the API is the contract.
- Dashboard shows zones and the active signal's entry/SL/TP on charts. EMA lines and swing/MSS markers are not drawn yet (the API does not expose swing points).
- Websocket `/ws` pushes a small status heartbeat only; the dashboard polls REST every 5 s.

## Assumptions I made where the blueprint is silent (all in `config/strategy.yaml`, versioned)

- **Displacement (blueprint §12.4)**: the run of consecutive M5 candles in the MSS direction ending at the MSS candle; the entry zone is the full range of the nearest opposite-colour M5 candle immediately before that run (max 10 candles back). No such candle ⇒ no setup.
- Sweep reclaim must happen within 3 completed M15 candles of the first breach; sweeps older than 8 candles are ignored.
- M15 alignment is judged on structure *before* the sweep candle (a sweep itself disturbs M15 structure).
- EMA50/EMA200 (H1) is applied as an extra directional filter (blueprint §7.2); disable with `trend.require_ema_confirmation: false`.
- "Excessively wide" stop = more than 5× M15 ATR. Fewer than 30 spread observations ⇒ spread check UNAVAILABLE. Calendar older than 6 h ⇒ news check UNAVAILABLE.
- R is computed after spread cost; TP is the nearest untouched opposing M15 swing and is never stretched.
- Each setup gets exactly one decision at its entry candle; a REJECTED/UNAVAILABLE setup is not re-tried later.
- Paper fills: proposed entry worse by spread + $0.05 slippage; a candle touching both TP and SL is recorded AMBIGUOUS, never a win.
- Settings API refuses to loosen locked rules (R ≥ 2, strict spread/news, paper-only, no broker execution).
- Account risk (0.5%) and position sizing are **not implemented** — the blueprint says they are not approved.

## Layout
`backend/app/` strategy · qualification · signals · paper_trading · backtesting · data · broker · news · telegram · database · main.py (API) · runtime.py (one cycle per minute) — `mt5_bridge/` — `frontend/index.html` — `config/strategy.yaml` — `scripts/`.

## Deploying on Render
1. Push this folder to a GitHub repo. In Render: **New → Blueprint**, pick the repo (it reads `render.yaml`).
2. Fill the `sync: false` secrets (Twelve Data keys, Telegram). Copy the generated `GPTA_API_TOKEN` — it is the dashboard login.
3. Open `https://<your-service>.onrender.com`, sign in with the token. Render provides HTTPS.
4. On the Windows PC with MT5: set `GPTA_BACKEND_URL=https://<your-service>.onrender.com` and the same token, run `py -m mt5_bridge.main`.
5. Run the history backfill once from the Render **Shell** tab: `python scripts/backfill_history.py 5000`.

Use the **Starter** plan with the disk: the free plan sleeps when idle (the minute-by-minute scheduler would stop, silently halting signals) and its filesystem is wiped on restart. Keep exactly one instance.
