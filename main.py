"""
main.py — Entry point for the Kraken + Telegram trading bot.
"""
import logging
import sys
import traceback

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
            backupCount=3,               # mantieni 3 backup
            encoding="utf-8",
        )
        h.setFormatter(fmt)
        return h

    # Root logger → bot.log + stdout
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(logging.StreamHandler(sys.stdout))
    root.addHandler(_file_handler("bot"))

    # Componenti con log dedicato
    for module, logfile in [
        ("telegram_bot",    "trading"),
        ("analyzer",        "analyzer"),
        ("strategy",        "strategy"),
        ("groq_evaluator",  "groq"),
        ("market_data",     "market"),
        ("discovery",       "discovery"),
        ("kraken_client",   "kraken"),
    ]:
        lgr = logging.getLogger(module)
        lgr.addHandler(_file_handler(logfile))
        lgr.propagate = True   # sale anche al root (bot.log)
logger = logging.getLogger(__name__)


def _notify_crash(exc: Exception) -> None:
    """Tenta di inviare l'errore critico su Telegram prima di uscire."""
    try:
        import requests
        import config
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
    import config

    _setup_logging(Path(__file__).parent / "logs")

    try:
        config.validate()
    except EnvironmentError as exc:
        logger.critical(str(exc))
        sys.exit(1)

    from telegram_bot import TradingBot
    try:
        bot = TradingBot()
        bot.run()
    except KeyboardInterrupt:
        logger.info("Bot fermato manualmente.")
    except Exception as exc:
        logger.critical("CRASH: %s", exc, exc_info=True)
        _notify_crash(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
