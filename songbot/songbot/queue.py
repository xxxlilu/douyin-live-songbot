"""防刷、去重、限流。歌其实已经进网易云队列了，这里只做接受与否的判断。"""

from __future__ import annotations

import re
import time
from datetime import datetime

from .models import QueueEntry, SongRequest

_NORMALIZE = re.compile(r"[\s\W_]+", re.UNICODE)
# 「晴天 (Live)」「晴天【翻唱】」按同一首算，防止刷变体
_BRACKETS = re.compile(r"[\(\[【].*?[\)\]】]")


def normalize_song(name: str) -> str:
    """「晴天 (Live)」 和 「晴天」 归一成同一个键。"""
    without_suffix = _BRACKETS.sub("", name or "")
    return _NORMALIZE.sub("", without_suffix).lower()


class SongQueue:
    def __init__(
        self,
        *,
        per_user_cooldown: float = 90.0,
        per_user_max_pending: int = 2,
        global_cooldown: float = 3.0,
        duplicate_window: float = 1800.0,
        max_queue: int = 50,
    ) -> None:
        self.per_user_cooldown = per_user_cooldown
        self.per_user_max_pending = per_user_max_pending
        self.global_cooldown = global_cooldown
        self.duplicate_window = duplicate_window
        self.max_queue = max_queue

        self.entries: list[QueueEntry] = []
        self._last_accepted_by_user: dict[str, float] = {}
        self._last_song_at: dict[str, float] = {}
        self._last_accept_at: float = 0.0

    # ---------------- 判断 ----------------
    def check(self, req: SongRequest) -> tuple[bool, str]:
        now = time.monotonic()

        if self.global_cooldown > 0 and now - self._last_accept_at < self.global_cooldown:
            return False, "全局冷却中（同一时刻太多点歌）"

        user_key = (req.user or "").strip().lower()
        if self.per_user_cooldown > 0:
            last = self._last_accepted_by_user.get(user_key)
            if last is not None and now - last < self.per_user_cooldown:
                wait = int(self.per_user_cooldown - (now - last))
                return False, f"同一个人点太快，还要等 {wait}s"

        key = normalize_song(req.song)
        if self.duplicate_window > 0:
            last = self._last_song_at.get(key)
            if last is not None and now - last < self.duplicate_window:
                return False, "这首歌最近刚点过"

        if self.per_user_max_pending > 0:
            pending = sum(
                1
                for e in self.entries
                if (e.request.user or "").strip().lower() == user_key and not e.pushed
            )
            if pending >= self.per_user_max_pending:
                return False, "这个人排的歌还没放完"

        return True, "ok"

    # ---------------- 记录 ----------------
    def accept(self, req: SongRequest, *, pushed: bool, note: str = "") -> QueueEntry:
        now = time.monotonic()
        entry = QueueEntry(request=req, accepted_at=datetime.now(), pushed=pushed, note=note)
        self.entries.append(entry)
        self._last_accepted_by_user[(req.user or "").strip().lower()] = now
        self._last_song_at[normalize_song(req.song)] = now
        self._last_accept_at = now

        if len(self.entries) > self.max_queue:
            self.entries = self.entries[-self.max_queue:]
        return entry

    def mark_pushed(self, entry: QueueEntry) -> None:
        entry.pushed = True

    def stats(self) -> str:
        pending = sum(1 for e in self.entries if not e.pushed)
        users = len({(e.request.user or "").lower() for e in self.entries})
        return f"累计 {len(self.entries)} 条 / 未开唱 {pending} 条 / {users} 位观众"
