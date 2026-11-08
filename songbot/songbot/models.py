"""贯穿整个程序的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class Danmaku:
    """一条弹幕。"""

    user: str
    content: str
    ts: datetime = field(default_factory=datetime.now)
    raw: Any = None

    def __str__(self) -> str:  # 日志里好认
        return f"{self.user}: {self.content}"


@dataclass(slots=True)
class SongRequest:
    """从弹幕里解析出来的点歌请求。"""

    user: str
    song: str
    artist: str = ""
    ts: datetime = field(default_factory=datetime.now)
    raw_content: str = ""
    danmaku: Danmaku | None = None

    @property
    def keyword(self) -> str:
        """搜索用的完整词：有歌手就带上，命中率高很多。"""
        return f"{self.song} {self.artist}".strip() if self.artist else self.song

    def label(self) -> str:
        return f"《{self.song}》" + (f"（{self.artist}）" if self.artist else "")


@dataclass(slots=True)
class QueueEntry:
    """本地记录的一条「已交给网易云」的点歌。"""

    request: SongRequest
    accepted_at: datetime
    pushed: bool = False
    note: str = ""
