"""日志：同时写控制台和 logs/songbot.log（UTF-8）。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(log_path: Path, *, verbose: bool = False) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Windows 控制台默认 GBK，中文日志容易炸，尽量切 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    logger = logging.getLogger("songbot")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s"))

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger
