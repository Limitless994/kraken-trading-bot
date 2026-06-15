"""
main.py — Entry point for the Kraken + Telegram trading bot.
"""
import logging
import signal
import sys
import traceback

_shutdown = False
_bot_instance = None


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    logging.getLogger(__name__).info("Shutdown richiesto (signal %s)", signum)
    if _bot_instance is not None:
        _bot_instance.stop()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

def _setup_logging(log_dir: "Path") -> None:
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    log_dir = Path(log_dir)
    log_dir.mkdir(exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    def _file_handler(name: str) -> RotatingFileHandler:
        h = RotatingFileHandler(
            log_dir / f"{name}.log",
            maxBytes=2 * 1024 * 1024,   # 2 MB per file
            backupCount=3,               # keep 3 backup files
            encoding="utf-8",
        )
        h.setFormatter(fmt)
        return h

    # Root logger → bot.log + stdout
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(logging.StreamHandler(sys.stdout))
    root.addHandler(_file_handler("bot"))

    # Modules with a dedicated log file
    for module, logfile in [
        ("bot.telegram_bot",      "trading"),
        ("core.analyzer",         "analyzer"),
        ("core.strategy",         "strategy"),
        ("services.groq_evaluator", "groq"),
        ("core.market_data",      "market"),
        ("services.discovery",    "discovery"),
        ("core.kraken_client",    "kraken"),
    ]:
        lgr = logging.getLogger(module)
        lgr.addHandler(_file_handler(logfile))
        lgr.propagate = True   # also propagates to the root logger (bot.log)

logger = logging.getLogger(__name__)


class _TokenFilter(logging.Filter):
    """Scrub the Telegram bot token from log records so it never appears in log files."""
    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self._token:
            msg = record.getMessage()
            if self._token in msg:
                record.msg = msg.replace(self._token, "<TOKEN>")
                record.args = ()
        return True


def _notify_crash(exc: Exception) -> None:
    """Attempts to send the critical error to Telegram before exiting."""
    try:
        import requests
        from core import config
        msg = (
            f"BOT CRASH\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"{traceback.format_exc()[-800:]}"
        )
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": config.TELEGRAM_CHAT_ID, "text": msg},
            timeout=5,
        )
    except Exception:
        pass


def main() -> None:
    from pathlib import Path
    from core import config, db

    _setup_logging(Path(__file__).parent / "logs")

    if config.TELEGRAM_TOKEN:
        _tf = _TokenFilter(config.TELEGRAM_TOKEN)
        logging.getLogger("httpx").addFilter(_tf)
        logging.getLogger().addFilter(_tf)

    try:
        config.validate()
    except EnvironmentError as exc:
        logger.critical(str(exc))
        sys.exit(1)

    # ── DB bootstrap ─────────────────────────────────────────────────────────
    db.init_db()
    db.migrate_from_json()   # no-op after first run (files renamed to .bak)

    # Reload profile now that DB is initialized
    config.apply_profile(config._load_active_profile())

    from bot.telegram_bot import TradingBot
    try:
        global _bot_instance
        bot = TradingBot()
        _bot_instance = bot
        bot.run()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot fermato.")
        sys.exit(0)
    except Exception as exc:
        logger.critical("CRASH: %s", exc, exc_info=True)
        _notify_crash(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
