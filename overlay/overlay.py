"""接下来 5 首 —— OBS 叠加层 + 点歌调度 + 控制面板。

职责：
  1. 读 songbot 写在 queue.inbox.jsonl 里的点歌，进队列、执行规则；
  2. 通过嗷呜连接器把队头插成网易云「下一首」，跟踪播放状态；
  3. 给 OBS 一个透明网页（/），给主播一个控制面板（/admin）。
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from connector import Connector, ConnectorError
from queue_store import DONE, PENDING, PLAYING, SKIPPED, QueueStore, norm
from songbot_proc import SongbotProcess

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
CONFIG = Path(os.environ["OVERLAY_CONFIG"]) if os.environ.get("OVERLAY_CONFIG") else HERE / "config.json"

DEFAULTS = {
    "port": 8790,
    "rows": 5,
    "title": "接下来",
    "open_browser": True,
    "open_admin": True,
    "playing_list": "%LOCALAPPDATA%\\Netease\\CloudMusic\\webdata\\file\\playingList",
    "connector": "..\\douyin-songbot\\tools\\awoo-connector\\extracted\\Awoo.Connector.Netease.exe",
    "queue_file": "queue.json",
    "inbox_file": "queue.inbox.jsonl",
    "rules": {
        "cooldown_seconds": 10,
        "max_pending_per_user": 5,
        "duplicate_window_seconds": 900,
        "expire_seconds": 1800,
    },
    "lan": {"enabled": False, "token": ""},
    "songbot": {
        "auto_start": True,
        "python": "..\\douyin-songbot\\.venv\\Scripts\\python.exe",
        "cwd": "..\\douyin-songbot",
        "args": ["-m", "songbot", "run"],
    },
}

LOCK = threading.RLock()
STATE: dict = {"payload": {}, "admin": {}, "health": {}}
LAN_URL = ""


def lan_ip() -> str:
    """拿本机在局域网里的 IP（不会真的发包）。"""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def ensure_firewall(port: int) -> None:
    """局域网访问要放行端口；失败就算了，只提示。"""
    import subprocess

    rule = "next5-overlay"
    try:
        check = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"],
            capture_output=True, text=True, timeout=10,
        )
        if check.returncode == 0 and rule in check.stdout:
            return
        subprocess.run(
            [
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={rule}", "dir=in", "action=allow",
                "protocol=TCP", f"localport={port}",
            ],
            capture_output=True, text=True, timeout=15,
        )
        log(f"[i] 已添加防火墙放行规则（TCP {port}）")
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] 防火墙规则没加成（不影响本机，手机可能连不上）：{exc}")


def fetch_song(song_id: str) -> dict | None:
    """按网易云歌曲 id 取歌名/歌手（用于粘贴链接点歌）。失败就返回 None。"""
    import requests

    session = requests.Session()
    session.trust_env = False  # 直连，别走代理
    try:
        resp = session.get(
            "https://music.163.com/api/song/detail",
            params={"ids": f"[{song_id}]"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
                "Cookie": "os=pc; appver=3.1.42;",
                "Referer": "https://music.163.com/",
            },
            timeout=8,
        )
        songs = (resp.json() or {}).get("songs") or []
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] 取歌曲信息失败（id={song_id}）：{exc}")
        return None
    if not songs:
        return None
    song = songs[0]
    artists = song.get("artists") or []
    album = song.get("album") or {}
    return {
        "title": str(song.get("name") or ""),
        "artist": "/".join(str(a.get("name", "")) for a in artists if isinstance(a, dict)),
        "album": str(album.get("name") or "") if isinstance(album, dict) else "",
    }


def log(message: str, *args) -> None:
    if args:
        try:
            message = message % args
        except Exception:
            message = f"{message} {' '.join(str(a) for a in args)}"
    print(f"{time.strftime('%H:%M:%S')}  {message}", flush=True)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG.is_file():
        try:
            user = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log(f"[warn] config.json 读不了（{exc}），用默认值")
        else:
            _deep_update(cfg, user)
    if cfg["lan"].get("enabled") and not cfg["lan"].get("token"):
        cfg["lan"]["token"] = f"{random.randint(0, 999999):06d}"
        save_config(cfg)
        log(f"[i] 已生成局域网口令：{cfg['lan']['token']}")
    return cfg


def _deep_update(base: dict, extra: dict) -> None:
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def save_config(cfg: dict) -> None:
    try:
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def expand(path: str) -> Path:
    raw = str(path).replace("%LOCALAPPDATA%", str(Path.home() / "AppData" / "Local"))
    p = Path(raw)
    return p if p.is_absolute() else (HERE / p).resolve()


# ------------------------------------------------------------------ 歌单
class PlaylistReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime = -1.0
        self._items: list[dict] = []

    def _refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._items = []
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        self._mtime = mtime
        self._items = [it for it in data.get("list", []) if isinstance(it, dict) and it.get("track")]

    @staticmethod
    def _pack(item: dict) -> dict:
        track = item["track"]
        artists = track.get("artists") or []
        return {
            "id": str(track.get("id", "")),
            "name": str(track.get("name", "")),
            "artist": "/".join(str(a.get("name", "")) for a in artists if isinstance(a, dict)),
            "album": (track.get("album") or {}).get("name", "")
            if isinstance(track.get("album"), dict)
            else "",
        }

    def window(
        self,
        current_id: str,
        current_name: str,
        exclude_ids: set[str],
        exclude_names: set[str],
        limit: int,
    ) -> tuple[list[dict], bool]:
        self._refresh()
        if not self._items:
            return [], False

        index = -1
        if current_id:
            for i, item in enumerate(self._items):
                if str((item.get("track") or {}).get("id")) == str(current_id):
                    index = i
                    break
        if index < 0 and current_name:
            for i, item in enumerate(self._items):
                if norm((item.get("track") or {}).get("name")) == norm(current_name):
                    index = i
                    break
        if index < 0:
            return [], False

        rows: list[dict] = []
        for item in self._items[index + 1:]:
            song = self._pack(item)
            if song["id"] in exclude_ids or norm(song["name"]) in exclude_names:
                continue
            rows.append(song)
            if len(rows) >= limit:
                break
        return rows, True


# ------------------------------------------------------------------ 调度
class Scheduler:
    def __init__(
        self,
        cfg: dict,
        store: QueueStore,
        conn: Connector,
        playlist: PlaylistReader,
        songbot: SongbotProcess,
    ):
        self.cfg = cfg
        self.store = store
        self.conn = conn
        self.playlist = playlist
        self.songbot = songbot
        self.rows = int(cfg["rows"])
        self._stop = threading.Event()

    # ---------- 小工具 ----------
    @staticmethod
    def _track_id(obj) -> str:
        return str((obj or {}).get("id") or "")

    @staticmethod
    def _play_track(entry: dict) -> dict:
        return {
            "id": str(entry.get("netease_id")),
            "title": entry.get("title") or entry.get("song"),
            "artist": entry.get("artist") or "",
            "album": entry.get("album") or "",
            "nativeData": "",
            "coverUrl": "",
            "displayName": entry.get("title") or entry.get("song"),
        }

    def _resolve(self, entry: dict) -> tuple[str, str]:
        """返回 (ok|notfound|error, 说明)。搜不到就跳过，网络错误要重试。"""
        query = f"{entry['song']} {entry.get('artist') or ''}".strip()
        try:
            tracks = self.conn.search(query)
        except ConnectorError as exc:
            log(f"[X] 搜索失败《{entry['song']}》：{exc}")
            return "error", f"搜索失败：{exc}"
        best = self._pick(tracks, entry["song"], entry.get("artist") or "")
        if best is None:
            log(f"[X] 搜不到《{entry['song']}》，跳过")
            return "notfound", "搜不到这首歌"
        entry["netease_id"] = str(best.get("id"))
        entry["title"] = best.get("title") or entry["song"]
        entry["artist"] = best.get("artist") or entry.get("artist") or ""
        entry["album"] = best.get("album") or ""
        return "ok", ""

    @classmethod
    def _pick(cls, tracks: list[dict], song: str, artist: str) -> dict | None:
        """挑最像的一条；低于门槛就当作搜不到，别硬塞一首不相干的。

        观众常写成「歌名 歌手」；歌手要被当成一个**独立的词**才认，
        否则「不存在的歌名xyz」会正好后缀撞上歌手 xyz。
        """
        if not tracks:
            return None
        want = norm(song)
        who = norm(artist)
        tokens = [norm(t) for t in re.split(r"[\s/、,，]+", song) if len(norm(t)) >= 2]

        def score(t: dict) -> int:
            title = norm(t.get("title"))
            a = norm(t.get("artist"))
            s = 0
            if title and title == want:
                s = 100
            elif want and want in title:
                s = 90
            elif title and want and title in want:
                coverage = len(title) / max(1, len(want))
                if coverage >= 0.5:          # 观众把歌手也打进去了，标题占一半以上
                    s = int(70 + 20 * coverage)
            if s < 50 and a:
                for tok in tokens:
                    if tok in a or (len(a) >= 4 and a in tok):
                        s = max(s, 80)       # 歌手名作为独立词出现
                        break
            if s < 50 and title and want:
                common = len(set(want) & set(title))
                if common / max(1, len(want)) >= 0.6:
                    s = 50
            if who and a and (who in a or a in who):
                s += 10
            return s

        best = max(tracks, key=score)
        return best if score(best) >= 50 else None

    # ---------- 状态推进 ----------
    def _advance(self, snap: dict) -> None:
        # 没连上时 current 是空的，别把正在播的误判成"已播完"
        if not snap.get("connected"):
            return
        current = self._track_id(snap.get("current"))
        playing = self.store.playing()
        if playing and playing.get("netease_id"):
            if current != playing["netease_id"]:
                self.store.set_state(playing, DONE, played_at=time.time())
                playing = None
        if not playing:
            head = self.store.head()
            if head and head.get("netease_id") and current == head["netease_id"]:
                self.store.set_state(head, PLAYING)

    def _ensure_head(self, snap: dict) -> None:
        if self.store.playing():
            return
        if not snap.get("connected"):
            return
        head = self.store.head()
        if head is None:
            return

        # 还没解析出歌曲信息：等后台解析线程；一直搜不到才放弃
        if not head.get("netease_id"):
            if head.get("attempts", 0) >= 6:
                log(f"[X] 搜不到《{head['song']}》，跳过")
                self.store.set_state(head, SKIPPED, note=head.get("note") or "搜不到这首歌",
                                     played_at=time.time())
            return

        # 插队失败过的稍等再试，别每 0.5 秒打一次连接器
        if head.get("insert_failures", 0) > 0 and time.time() - head.get("last_insert_try", 0) < 5:
            return
        head["last_insert_try"] = time.time()
        current = self._track_id(snap.get("current"))
        nxt = self._track_id(snap.get("next"))
        if current == head["netease_id"]:
            return
        if nxt == head["netease_id"]:
            return
        try:
            result = self.conn.execute("InsertNext", track=self._play_track(head))
            outcome = str(result.get("outcome", "")).lower()
            if outcome in ("verified", "accepted", "applied"):
                head["inserted_at"] = time.time()
                head["insert_failures"] = 0
                head["note"] = ""
                self.store.save()
                log(f"[+] 已插队：{head['user']} 点《{head['title'] or head['song']}》")
            else:
                head["insert_failures"] = head.get("insert_failures", 0) + 1
                head["note"] = str(result.get("message") or outcome)
                self.store.save()
                log(f"[X] 插队未成功（{outcome}）：{result.get('message')}")
        except ConnectorError as exc:
            head["insert_failures"] = head.get("insert_failures", 0) + 1
            head["note"] = f"插队异常：{exc}"
            self.store.save()
            log(f"[X] 插队异常：{exc}")

    # ---------- 后台解析：一进队就搜索，别等轮到了才搜 ----------
    def resolve_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with LOCK:
                    if self.conn.status().get("alive"):
                        for entry in self.store.ordered():
                            if entry.get("netease_id"):
                                continue
                            if entry.get("attempts", 0) >= 6:
                                continue
                            if time.time() - entry.get("last_resolve_try", 0) < 5:
                                continue
                            entry["last_resolve_try"] = time.time()
                            status, note = self._resolve(entry)
                            if status == "ok":
                                entry["attempts"] = 0
                                entry["note"] = ""
                                log(f"[search] {entry['user']} 点《{entry['song']}》"
                                    f" → {entry.get('title')} - {entry.get('artist')}")
                            elif status == "notfound":
                                # 搜不到就别占队列位置了，直接标掉；主播在面板能看到原因
                                log(f"[search] 搜不到《{entry['song']}》，已跳过")
                                self.store.rejects.append(
                                    f"搜不到：{entry['user']} 点《{entry['song']}》"
                                )
                                self.store.rejects = self.store.rejects[-20:]
                                self.store.set_state(
                                    entry, SKIPPED, note=note, played_at=time.time()
                                )
                                continue
                            else:
                                entry["attempts"] = entry.get("attempts", 0) + 1
                                entry["note"] = note
                                log(f"[search] 搜索失败《{entry['song']}》：{note}")
                            self.store.save()
                            break
            except Exception as exc:  # noqa: BLE001
                log(f"[warn] 解析线程异常：{exc}")
            time.sleep(1.0)

    # ---------- 面板动作 ----------
    def action(self, name: str, entry_id: str = "", extra: dict | None = None) -> tuple[bool, str]:
        extra = extra or {}
        if name == "enqueue":
            song = str(extra.get("song") or "").strip()
            artist = str(extra.get("artist") or "").strip()
            link = str(extra.get("link") or "").strip()
            user = str(extra.get("user") or "主播").strip() or "主播"

            if link:
                match = re.search(r"[?&]id=(\d+)", link)
                if not match:
                    return False, "链接里没找到歌曲 id（形如 music.163.com/song?id=1359818025）"
                song_id = match.group(1)
                detail = fetch_song(song_id) or {}
                title = detail.get("title") or song or f"歌曲 {song_id}"
                who = detail.get("artist") or artist
                ok, message = self.store.add(
                    user, title, who, force=True, netease_id=song_id
                )
                if ok:
                    self.store.save()
                    log(f"[手动] {user} 点了链接：{title} - {who}（id={song_id}）")
                    return True, f"已加入队列：{title} - {who}"
                return False, message

            if not song:
                return False, "歌名不能是空的（或者直接粘贴网易云链接）"
            ok, message = self.store.add(user, song, artist, force=True)
            if ok:
                self.store.save()
                log(f"[手动] {user} 点《{song}》{artist}")
                return True, f"已加入队列：{song}" + (f" - {artist}" if artist else "")
            return False, message
        if name == "settings":
            return self._save_settings(extra)
        if name == "pause":
            self.store.set_paused(True)
            return True, "已暂停接单"
        if name == "resume":
            self.store.set_paused(False)
            return True, "已恢复接单"
        if name == "clear":
            n = self.store.clear()
            return True, f"已清空 {n} 首待播"
        if name == "pin":
            return self.store.pin(entry_id)
        if name == "remove":
            return self.store.remove(entry_id)
        if name == "skip":
            return self._skip()
        if name == "refresh":
            self.conn.probe()
            return True, "已刷新"
        if name == "songbot-start":
            return self.songbot.start()
        if name == "songbot-stop":
            return self.songbot.stop()
        if name == "songbot-restart":
            return self.songbot.restart()
        return False, f"未知动作：{name}"

    def _save_settings(self, extra: dict) -> tuple[bool, str]:
        """面板上调规则：写回 config.json 并立即生效。"""
        def num(key: str, low: float, high: float, cast=float, default=None):
            raw = extra.get(key, default)
            try:
                value = cast(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{key} 不是数字")
            if value < low or value > high:
                raise ValueError(f"{key} 要在 {low}~{high} 之间")
            return value

        try:
            cooldown = num("cooldown_seconds", 0, 3600, float, 10)
            max_pending = num("max_pending_per_user", 1, 100, int, 5)
            duplicate = num("duplicate_window_seconds", 0, 86400, float, 900)
            expire = num("expire_seconds", 60, 86400, float, 1800)
        except ValueError as exc:
            return False, str(exc)

        rules = {
            "cooldown_seconds": cooldown,
            "max_pending_per_user": max_pending,
            "duplicate_window_seconds": duplicate,
            "expire_seconds": expire,
        }
        self.store.rules.update(rules)
        self.cfg["rules"] = rules
        save_config(self.cfg)
        log(
            f"[设置] 冷却 {cooldown:.0f}s · 每人 {max_pending} 首 · "
            f"同歌 {duplicate / 60:.0f} 分钟 · 过期 {expire / 60:.0f} 分钟"
        )
        return True, "设置已保存并生效"

    def _skip(self) -> tuple[bool, str]:
        snap = self.conn.snapshot()
        current = self._track_id(snap.get("current"))
        playing = self.store.playing()
        if playing and playing.get("netease_id") == current:
            self.store.set_state(playing, SKIPPED, played_at=time.time())
            head = self.store.head()
            if head:
                if not head.get("netease_id"):
                    self._resolve(head)
                if head.get("netease_id"):
                    try:
                        r = self.conn.execute("InterruptSelected", track=self._play_track(head))
                        outcome = str(r.get("outcome", "")).lower()
                        if outcome in ("verified", "accepted", "applied"):
                            log(f"[skip] 直接播下一首点歌：{head['song']}（{outcome}）")
                            return True, "已跳过，直接播下一首点歌"
                        log(f"[skip] InterruptSelected 未成功：{outcome} {r.get('message')}")
                    except ConnectorError as exc:
                        log(f"[skip] InterruptSelected 失败：{exc}")
            try:
                r = self.conn.execute("Next")
            except ConnectorError as exc:
                return False, f"跳过失败：{exc}"
            outcome = str(r.get("outcome", "")).lower()
            if outcome in ("verified", "accepted", "applied"):
                return True, "已跳过"
            return False, f"跳过没生效：{r.get('message') or outcome}"
        try:
            r = self.conn.execute("Next")
        except ConnectorError as exc:
            return False, f"跳过失败：{exc}"
        outcome = str(r.get("outcome", "")).lower()
        if outcome in ("verified", "accepted", "applied"):
            return True, "已跳到下一首"
        return False, f"跳过没生效：{r.get('message') or outcome}"

    # ---------- 展示 ----------
    def _payload(self, snap: dict) -> dict:
        pending = self.store.ordered()
        playing = self.store.playing()

        rows: list[dict] = []
        for entry in pending[: self.rows]:
            resolved = bool(entry.get("netease_id"))
            subtitle = entry.get("artist") or ""
            if not resolved:
                subtitle = entry.get("note") or "搜索中…"
            rows.append(
                {
                    "kind": "request",
                    "name": entry.get("title") or entry["song"],
                    "artist": subtitle,
                    "note": entry.get("note", ""),
                    "requester": entry.get("user") or "",
                }
            )

        synced = True
        if len(rows) < self.rows:
            current = snap.get("current") or {}
            exclude_ids = {str(e.get("netease_id")) for e in pending if e.get("netease_id")}
            exclude_names = {norm(e["song"]) for e in pending}
            extra, synced = self.playlist.window(
                self._track_id(current),
                str(current.get("title") or ""),
                exclude_ids,
                exclude_names,
                self.rows - len(rows),
            )
            for song in extra:
                rows.append(
                    {
                        "kind": "playlist",
                        "name": song["name"],
                        "artist": song["artist"],
                        "requester": "",
                    }
                )

        toast = None
        now = time.time()
        for entry in sorted(self.store.entries, key=lambda e: e.get("created_at", 0), reverse=True):
            if now - entry.get("created_at", 0) <= 3.0 and entry["state"] in (PENDING, PLAYING):
                toast = {"user": entry.get("user"), "song": entry.get("song")}
                break

        total = len(pending) + (1 if playing else 0)
        return {
            "title": self.cfg["title"],
            "rows": rows,
            "total": total,
            "toast": toast,
            "synced": synced,
            "paused": self.store.paused,
        }

    def _admin(self, snap: dict) -> dict:
        current = snap.get("current") or {}
        nxt = snap.get("next") or {}
        queue = []
        head = self.store.head()
        for entry in self.store.ordered():
            queue.append(
                {
                    "id": entry["id"],
                    "user": entry.get("user"),
                    "song": entry.get("title") or entry["song"],
                    "raw": entry["song"],
                    "artist": entry.get("artist") or "",
                    "state": entry["state"],
                    "is_head": entry is head,
                    "inserted": bool(entry.get("inserted_at")),
                    "note": entry.get("note", ""),
                    "at": time.strftime("%H:%M:%S", time.localtime(entry.get("created_at", 0))),
                }
            )
        return {
            "paused": self.store.paused,
            "queue": queue,
            "playing": self.store.playing(),
            "connector": self.conn.status(),
            "songbot": self.songbot.status(),
            "current": {"title": current.get("title"), "artist": current.get("artist")},
            "next": {"title": nxt.get("title"), "artist": nxt.get("artist")},
            "rejects": self.store.rejects[-10:],
            "rules": dict(self.store.rules),
            "lan": bool(self.cfg["lan"].get("enabled")),
            "token": self.cfg["lan"].get("token") if self.cfg["lan"].get("enabled") else "",
            "lan_url": LAN_URL,
        }

    # ---------- 主循环 ----------
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                with LOCK:
                    self.store.ingest()
                    snap = self.conn.snapshot()
                    self._advance(snap)
                    self._ensure_head(snap)
                    payload = self._payload(snap)
                    admin = self._admin(snap)
                    STATE["payload"] = payload
                    STATE["admin"] = admin
                    STATE["health"] = {
                        "connector": self.conn.status(),
                        "queue": len(self.store.entries),
                        "pending": self.store.count_pending(),
                        "paused": self.store.paused,
                        "synced": payload.get("synced"),
                        "last_tick": time.strftime("%H:%M:%S"),
                    }
            except Exception as exc:  # noqa: BLE001
                log(f"[warn] 调度异常：{exc}")
            time.sleep(0.5)

    def stop(self) -> None:
        self._stop.set()


# ------------------------------------------------------------------ HTTP
def auth_ok(handler, cfg: dict) -> bool:
    if not cfg["lan"].get("enabled"):
        return True
    token = cfg["lan"].get("token") or ""
    query = parse_qs(urlparse(handler.path).query)
    if token and (query.get("token") or [""])[0] == token:
        handler._set_cookie = token
        return True
    cookies = handler.headers.get("Cookie") or ""
    return f"n5token={token}" in cookies


def make_handler(cfg: dict, scheduler: Scheduler):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Next5Overlay/2.0"

        def log_message(self, *args):
            pass

        def _send(self, status: int, body: bytes, ctype: str, cookie: str = "") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", f"n5token={cookie}; Path=/; SameSite=Lax")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _file(self, name: str) -> None:
            try:
                body = (WEB / name).read_bytes()
            except OSError:
                self._send(404, b"missing", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._file("index.html")
                return
            if path == "/admin":
                if not auth_ok(self, cfg):
                    self._file("login.html")
                    return
                self._file("admin.html")
                return
            if path == "/api/queue":
                self._json(STATE.get("payload") or {})
                return
            if path == "/api/health":
                self._json(STATE.get("health") or {})
                return
            if path == "/api/state":
                if not auth_ok(self, cfg):
                    self._json({"error": "unauthorized"}, 401)
                    return
                self._json(STATE.get("admin") or {})
                return
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if path != "/api/action":
                self._json({"error": "not found"}, 404)
                return
            if not auth_ok(self, cfg):
                self._json({"error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400)
                return
            name = str(body.get("action") or "")
            entry_id = str(body.get("id") or "")
            try:
                with LOCK:
                    ok, message = scheduler.action(name, entry_id, body)
            except Exception as exc:  # noqa: BLE001
                ok, message = False, f"出错：{exc}"
            log(f"[admin] {name} {entry_id} -> {message}")
            self._json({"ok": ok, "message": message,
                        "state": STATE.get("admin") or {}})

    return Handler


class Server(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def main() -> None:
    cfg = load_config()
    store = QueueStore(
        expand(cfg["queue_file"]),
        expand(cfg["inbox_file"]),
        cfg["rules"],
        logger=log,
    )
    store.ingest(initial=True)
    dropped = store.expire()
    if dropped:
        log(f"[i] 丢弃 {dropped} 条超过 30 分钟的点歌")

    proxy_keys = [k for k in os.environ if k.upper().endswith("_PROXY")]
    if proxy_keys:
        log(f"[i] 检测到代理变量 {', '.join(proxy_keys)}，已对连接器禁用（搜索直连更快）")

    conn = Connector(expand(cfg["connector"]), __import__("logging").getLogger("songbot"))
    conn.start()
    try:
        conn.subscribe()
        log("[i] 已订阅连接器快照事件")
    except ConnectorError as exc:
        log(f"[warn] 订阅失败，改用轮询：{exc}")

    playlist = PlaylistReader(expand(cfg["playing_list"]))
    songbot = SongbotProcess(cfg, logger=log)
    songbot.configure(cfg, HERE)
    scheduler = Scheduler(cfg, store, conn, playlist, songbot)
    threading.Thread(target=scheduler.run, daemon=True).start()
    threading.Thread(target=scheduler.resolve_loop, daemon=True).start()

    if songbot.auto_start:
        ok, message = songbot.start()
        log(f"[songbot] {message}")

    lan = bool(cfg["lan"].get("enabled"))
    host = "0.0.0.0" if lan else "127.0.0.1"
    base = int(cfg["port"])
    server = None
    port = base
    for port in range(base, base + 20):
        try:
            server = Server((host, port), make_handler(cfg, scheduler))
            break
        except OSError:
            continue
    if server is None:
        log(f"[X] {base}~{base + 19} 端口都被占用")
        return

    url = f"http://127.0.0.1:{port}/"
    # 开了局域网口令后，本机打开面板也带上 token，省得再输一次
    token_q = f"?token={cfg['lan']['token']}" if lan else ""
    admin_url = f"{url}admin{token_q}"

    global LAN_URL
    if lan:
        ip = lan_ip()
        LAN_URL = f"http://{ip}:{port}/admin?token={cfg['lan']['token']}"
        ensure_firewall(port)

    print("=" * 60)
    print("  接下来 5 首 · 点歌调度 + OBS 叠加层")
    print("=" * 60)
    print(f"  OBS 浏览器源： {url}")
    print(f"  控制面板：     {admin_url}")
    if lan:
        print(f"  手机访问：     {LAN_URL}")
        print("  （手机和电脑要连同一个 Wi-Fi / 热点）")
    print(f"  点歌收件箱：   {store.inbox_path}")
    print("  Ctrl+C 停止")
    print("=" * 60)

    if cfg.get("open_browser", True):
        def _open():
            import webbrowser

            webbrowser.open(url)
            if cfg.get("open_admin", True):
                webbrowser.open(admin_url)

        threading.Timer(1.0, _open).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        scheduler.stop()
        songbot.stop()
        conn.close()


if __name__ == "__main__":
    main()
