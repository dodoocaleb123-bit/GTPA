"""MT5 bridge configuration (environment variables). Runs on the Windows machine that hosts the MT5 terminal."""
import os

BACKEND_URL = os.environ.get("GPTA_BACKEND_URL", "http://localhost:8000").rstrip("/")
API_TOKEN = os.environ.get("GPTA_API_TOKEN", "")
SYMBOL_HINT = os.environ.get("MT5_SYMBOL", "")            # exact broker symbol if known; else auto-discovered
POLL_SECONDS = float(os.environ.get("MT5_POLL_SECONDS", "1"))
# Broker server clocks are usually NOT UTC. If unset, the offset is estimated from the first live tick.
SERVER_UTC_OFFSET_SECONDS = os.environ.get("MT5_SERVER_UTC_OFFSET_SECONDS")
MT5_LOGIN = os.environ.get("MT5_LOGIN")                    # optional; otherwise use the terminal's logged-in demo account
MT5_PASSWORD = os.environ.get("MT5_PASSWORD")
MT5_SERVER = os.environ.get("MT5_SERVER")
