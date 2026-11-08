"""把点歌写进 next5-overlay 的收件箱，不再直接操作网易云。

一行一条 JSON，只追加不改写；规则（冷却、去重、上限）全部由
next5-overlay 那边的队列执行，这里不做判断。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..config import Config
from .base import BaseController


class QueueController(BaseController):
    def __init__(self, cfg: Config, logger) -> None:
        self.cfg = cfg
        self.logger = logger
        raw = getattr(cfg.queue, "inbox", "") or "../next5-overlay/queue.inbox.jsonl"
        path = Path(raw)
        self.path = path if path.is_absolute() else (cfg.root / path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.logger.info("点歌收件箱：%s", self.path)

    def add_to_next(
        self, query: str, match: str | None = None, artist: str = "", user: str = ""
    ) -> bool:
        song = (match or query or "").strip()
        if not song:
            return False
        record = {
            "user": (user or "匿名观众").strip(),
            "song": song,
            "artist": (artist or "").strip(),
            "ts": round(time.time(), 3),
        }
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
        except OSError as exc:
            self.logger.error("写点歌收件箱失败：%s", exc)
            return False
        self.logger.info("已入队：%s 点《%s》", record["user"], song)
        return True

    def close(self) -> None:
        pass
