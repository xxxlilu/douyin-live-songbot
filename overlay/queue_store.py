"""点歌队列：状态、规则、持久化。

两个文件：
- `queue.inbox.jsonl`：songbot 往里追加「谁点了什么」，一行一条，只追加不改写；
- `queue.json`：本工具维护的权威队列（带状态），原子写。

规则（都在这里执行，songbot 不再判）：
- 同人冷却 10 秒
- 同人同时最多 5 首（pending + playing）
- 同歌 15 分钟内不重复
- 启动时丢弃超过 30 分钟的记录
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

_NORM = re.compile(r"[\s\W_]+", re.UNICODE)

PENDING = "pending"
PLAYING = "playing"
DONE = "done"
SKIPPED = "skipped"
CANCELLED = "cancelled"


def norm(text: str) -> str:
    return _NORM.sub("", text or "").lower()


class QueueStore:
    def __init__(self, state_path: Path, inbox_path: Path, rules: dict, logger=print):
        self.state_path = state_path
        self.inbox_path = inbox_path
        self.log = logger
        self.rules = {
            "cooldown_seconds": float(rules.get("cooldown_seconds", 10)),
            "max_pending_per_user": int(rules.get("max_pending_per_user", 5)),
            "duplicate_window_seconds": float(rules.get("duplicate_window_seconds", 900)),
            "expire_seconds": float(rules.get("expire_seconds", 1800)),
        }
        self.entries: list[dict] = []
        self.paused = False
        self._offset = 0
        self.rejects: list[str] = []
        self._load()

    # ---------------------------------------------------------------- 持久化
    def _load(self) -> None:
        if self.state_path.is_file():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.entries = data.get("entries", [])
                self.paused = bool(data.get("paused", False))
                self._offset = int(data.get("inbox_offset", 0) or 0)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[warn] queue.json 读不了（{exc}），从空队列开始")

    def save(self) -> None:
        payload = {
            "version": 1,
            "paused": self.paused,
            "inbox_offset": self._offset,
            "entries": self.entries,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ---------------------------------------------------------------- 收单
    def ingest(self, initial: bool = False) -> int:
        """把 inbox 里新增的点歌读进来。

        读取位置（inbox_offset）存在 queue.json 里，所以重启不会把旧点歌
        再入队一遍；inbox 只追加，不截断，避免和 songbot 抢写。
        """
        if not self.inbox_path.is_file():
            return 0

        try:
            size = self.inbox_path.stat().st_size
        except OSError:
            return 0

        offset = self._offset
        if offset > size:  # inbox 被删了或换了一个
            offset = 0
        try:
            with open(self.inbox_path, "rb") as fh:
                fh.seek(offset)
                data = fh.read()
        except OSError:
            return 0
        if not data:
            if self._offset != offset:
                self._offset = offset
                self.save()
            return 0
        self._offset = offset + len(data)

        added = 0
        for raw in data.decode("utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            ok, msg = self.add(
                user=str(obj.get("user") or "匿名观众"),
                song=str(obj.get("song") or ""),
                artist=str(obj.get("artist") or ""),
                ts=float(obj.get("ts") or time.time()),
            )
            if ok:
                added += 1
            else:
                self.rejects.append(msg)
                self.log(f"[drop] {msg}")
                self.rejects = self.rejects[-20:]

        self.save()
        return added

    def add(
        self,
        user: str,
        song: str,
        artist: str,
        ts: float | None = None,
        force: bool = False,
        netease_id: str = "",
    ) -> tuple[bool, str]:
        """force=True 是主播在面板上手动点的：跳过冷却和每人上限，仍查重复。

        netease_id 有值时说明是"精确指定"（比如粘贴了歌曲链接），不再去搜索。
        """
        now = time.time()
        ts = ts or now
        song = (song or "").strip()
        if not song:
            return False, "空歌名"
        if self.paused and not force:
            return False, f"已暂停接单，丢弃：{user} 点《{song}》"
        if now - ts > self.rules["expire_seconds"]:
            return False, f"点歌已过期（{int((now - ts) / 60)} 分钟前）：{user} 点《{song}》"

        if not force:
            mine = [
                e for e in self.entries
                if norm(e.get("user")) == norm(user) and e.get("state") in (PENDING, PLAYING)
            ]
            if mine:
                last = max(e.get("created_at", 0) for e in mine)
                if now - last < self.rules["cooldown_seconds"]:
                    return False, f"{user} 点太快，冷却中"
                if len(mine) >= self.rules["max_pending_per_user"]:
                    return False, f"{user} 已排 {len(mine)} 首，满额"

        key = norm(song)
        for e in self.entries:
            if now - e.get("created_at", 0) >= self.rules["duplicate_window_seconds"]:
                continue
            if norm(e.get("song")) == key:
                return False, f"《{song}》15 分钟内已点过"
            if netease_id and str(e.get("netease_id")) == str(netease_id):
                return False, f"这首歌（id={netease_id}）15 分钟内已点过"

        self.entries.append(
            {
                "id": uuid.uuid4().hex[:10],
                "user": user,
                "song": song,
                "artist": artist,
                "created_at": ts,
                "state": PENDING,
                "netease_id": str(netease_id or ""),
                "title": song if netease_id else "",
                "album": "",
                "inserted_at": 0.0,
                "played_at": 0.0,
                "attempts": 0,
            }
        )
        return True, "ok"

    # ---------------------------------------------------------------- 查询
    def ordered(self) -> list[dict]:
        """待播顺序：pending 在前（按创建时间），playing 单独取。"""
        return sorted(
            [e for e in self.entries if e["state"] == PENDING],
            key=lambda e: e["created_at"],
        )

    def playing(self) -> dict | None:
        for e in self.entries:
            if e["state"] == PLAYING:
                return e
        return None

    def head(self) -> dict | None:
        pending = self.ordered()
        return pending[0] if pending else None

    def count_pending(self) -> int:
        return len(self.ordered())

    # ---------------------------------------------------------------- 变更
    def set_state(self, entry: dict, state: str, **fields) -> None:
        entry["state"] = state
        entry.update(fields)
        self.save()

    def expire(self, now: float | None = None) -> int:
        now = now or time.time()
        before = len(self.entries)
        self.entries = [
            e for e in self.entries
            if not (
                e["state"] == PENDING
                and now - e.get("created_at", 0) > self.rules["expire_seconds"]
            )
        ]
        removed = before - len(self.entries)
        if removed:
            self.save()
        return removed

    def find(self, entry_id: str) -> dict | None:
        for e in self.entries:
            if e["id"] == entry_id:
                return e
        return None

    def remove(self, entry_id: str) -> tuple[bool, str]:
        e = self.find(entry_id)
        if e is None:
            return False, "没有这条"
        if e["state"] == PLAYING:
            return False, "正在播的不能删，用「跳过」"
        if e is self.head() and e.get("inserted_at"):
            return False, "队头已经在网易云队列里了，删不掉；要撤请用「跳过」"
        self.entries = [x for x in self.entries if x["id"] != entry_id]
        self.save()
        return True, "已删除"

    def pin(self, entry_id: str) -> tuple[bool, str]:
        e = self.find(entry_id)
        if e is None:
            return False, "没有这条"
        if e["state"] != PENDING:
            return False, "只有排队中的能置顶"
        if e is self.head():
            return True, "已经在队头"
        # 给它一个更早的时间，排序自然到最前
        earliest = min((x["created_at"] for x in self.ordered()), default=e["created_at"])
        e["created_at"] = earliest - 0.001
        e["inserted_at"] = 0.0
        self.save()
        return True, "已置顶"

    def clear(self) -> int:
        n = len(self.ordered())
        self.entries = [e for e in self.entries if e["state"] != PENDING]
        self.save()
        return n

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.save()
