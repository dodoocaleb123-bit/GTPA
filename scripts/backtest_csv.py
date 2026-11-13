"""Backtest the live strategy engine on four independently sourced candle CSVs.

  python scripts/backtest_csv.py --m1 m1.csv --m5 m5.csv --m15 m15.csv --h1 h1.csv [--oos 0.7] [--no-news-assumption]

CSV columns: time (UTC, ISO or epoch), open, high, low, close. Timeframes must come from the provider
(NOT resampled from M1). Results are labelled SIMULATED. News is only 'assumed clear' if you pass --assume-no-news,
and the report says so; otherwise nothing can qualify (strict mode)."""
import argparse, json, sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.backtesting.engine import run_backtest, split_out_of_sample   # noqa: E402
from app.config import load_config                                      # noqa: E402


def load(path):
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"], utc=True, unit="s" if pd.api.types.is_numeric_dtype(df["time"]) else None)
    df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    bad = (df.high < df[["open", "close"]].max(axis=1)) | (df.low > df[["open", "close"]].min(axis=1)) | (df.high < df.low)
    if bad.any():
        raise SystemExit(f"{path}: {int(bad.sum())} candles with invalid OHLC; fix the data first")
    return df[["time", "open", "high", "low", "close"]]


ap = argparse.ArgumentParser()
for tf in ("m1", "m5", "m15", "h1"):
    ap.add_argument(f"--{tf}", required=True)
ap.add_argument("--spread", type=float, default=0.30)
ap.add_argument("--assume-no-news", action="store_true")
ap.add_argument("--oos", type=float, default=None, help="chronological in-sample fraction; reports both halves")
ap.add_argument("--out")
a = ap.parse_args()
frames = {tf.upper(): load(getattr(a, tf)) for tf in ("m1", "m5", "m15", "h1")}
cfg = load_config()
runs = {}
if a.oos:
    ins, oos, cut = split_out_of_sample(frames, a.oos)
    runs = {"cut": str(cut), "in_sample": run_backtest(ins, cfg, assumed_spread=a.spread, assume_no_news=a.assume_no_news),
            "out_of_sample": run_backtest(oos, cfg, assumed_spread=a.spread, assume_no_news=a.assume_no_news)}
else:
    runs = run_backtest(frames, cfg, assumed_spread=a.spread, assume_no_news=a.assume_no_news)
text = json.dumps(runs, indent=1, default=str)
Path(a.out).write_text(text) if a.out else print(text)
