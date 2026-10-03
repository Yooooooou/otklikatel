from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ENV_PATH = Path(".env")


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    load_dotenv(ENV_PATH)
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Не найден {path}. Создайте его по образцу из репозитория.")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


class SecretFilter(logging.Filter):
    """Вырезает значения секретов из любых лог-сообщений."""

    KEYS = ("HH_CLIENT_SECRET", "HH_ACCESS_TOKEN", "HH_REFRESH_TOKEN", "ANTHROPIC_API_KEY")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for k in self.KEYS:
            v = os.environ.get(k)
            if v and len(v) > 6:
                msg = msg.replace(v, "***")
        record.msg, record.args = msg, ()
        return True


def setup_logging(cfg: dict[str, Any]) -> None:
    lc = cfg.get("logging", {})
    level = getattr(logging, str(lc.get("level", "INFO")).upper(), logging.INFO)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    fp = lc.get("file_path")
    if fp:
        Path(fp).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(fp, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(SecretFilter())
        root.addHandler(h)
    # httpx логирует полные URL — приглушаем
    logging.getLogger("httpx").setLevel(logging.WARNING)
