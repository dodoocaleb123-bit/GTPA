"""One-off deep history download (for warm-up and backtests) through the SAME provider/validator/storage path
as live collection. Usage:  python scripts/backfill_history.py [bars=5000]   (needs TWELVE_DATA_KEY_1..4, GPTA_DB)"""
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.main import build_from_env   # noqa: E402

n = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
rt, _ = build_from_env()
rt.collect(datetime.now(timezone.utc), sizes={tf: n for tf in ("M1", "M5", "M15", "H1")})
for tf, h in rt.health.items():
    print(tf, "healthy" if h["healthy"] else f"UNHEALTHY {h['error'] or h['issues']}")
