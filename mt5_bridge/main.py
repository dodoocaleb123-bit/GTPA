"""Run on the Windows machine:  py -m mt5_bridge.main   (needs `pip install MetaTrader5 requests`)."""
import time

from . import config
from .connector import MT5Connector
from .quote_publisher import QuotePublisher


def main() -> None:
    import MetaTrader5 as mt5                      # Windows-only package
    if not config.API_TOKEN:
        raise SystemExit("GPTA_API_TOKEN is required")
    off = int(config.SERVER_UTC_OFFSET_SECONDS) if config.SERVER_UTC_OFFSET_SECONDS else None
    con = MT5Connector(mt5, config.SYMBOL_HINT, off)
    pub = QuotePublisher(config.BACKEND_URL, config.API_TOKEN)
    while True:
        if con.symbol is None and not con.connect(config.MT5_LOGIN, config.MT5_PASSWORD, config.MT5_SERVER):
            print("MT5 not connected / gold symbol not found; retrying")
            time.sleep(5)
            continue
        q = con.read()
        if q:
            pub.publish(q)
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
