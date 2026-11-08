"""网易云音乐客户端的原生 IPC（Orpheus）。

不抢焦点、不依赖界面元素、不需要 keep 客户端在前台。做法来自开源项目
BiliNCM-TS / 嗷呜点歌机（MIT）的 NetEase 适配器，原理：

1. 找窗口：客户端主进程 cloudmusic.exe 下，类名 `OrpheusBrowserHost` 的窗口。
2. 发指令：把 JSON 包成 `orpheus://<base64>`，放进一块命名共享内存
   `orpheus_ipc_<pid>_<tick>`，再给窗口发消息 `WM_APP(0x8001)`，
   wParam = pid，lParam = tick。客户端自己去读共享内存。
   消息体：`{"id": 3, "data": "orpheus://..."}`。
3. 播放控制：客户端注册了原子热键（`prev_local` / `next_local` /
   `play_pause_local`），给它 PostMessage(WM_HOTKEY, atom, Ctrl+VK) 即可。
4. 搜索走公开接口 music.163.com/api/search/get/web，拿精确歌曲 id。

用到的都是 ctypes，零第三方依赖。
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_HOTKEY = 0x0312
WM_ORPHEUS_IPC = 0x8001
MOD_CONTROL = 0x0002
VK_LEFT, VK_RIGHT, VK_P = 0x25, 0x27, 0x50

PAGE_READWRITE = 0x04
FILE_MAP_WRITE = 0x0002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1)
ERROR_ALREADY_EXISTS = 183

SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_size_t),
]
user32.SendMessageTimeoutW.restype = ctypes.c_size_t
kernel32.GlobalFindAtomW.argtypes = [wintypes.LPCWSTR]
kernel32.GlobalFindAtomW.restype = ctypes.c_ushort

kernel32.CreateFileMappingW.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPCWSTR,
]
kernel32.CreateFileMappingW.restype = wintypes.HANDLE
kernel32.MapViewOfFile.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_size_t,
]
kernel32.MapViewOfFile.restype = ctypes.c_void_p
kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
kernel32.UnmapViewOfFile.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


@dataclass(slots=True)
class ClientWindow:
    pid: int
    ipc_hwnd: int
    title_hwnd: int
    title: str = ""
    version: str = ""


@dataclass(slots=True)
class Track:
    id: str
    title: str
    artists: str
    album: str = ""

    @property
    def display(self) -> str:
        return f"{self.title} - {self.artists}" if self.artists else self.title


_last_tick = 0


# ------------------------------------------------------------------ 窗口查找
def _exe_name(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value.strip()


def _window_size(hwnd: int) -> tuple[int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0)
    return (rect.right - rect.left, rect.bottom - rect.top)


def _iter_windows() -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []

    @EnumWindowsProc
    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append((hwnd, pid.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def find_client() -> ClientWindow | None:
    """找网易云客户端窗口；没开就返回 None。"""
    candidates: list[ClientWindow] = []
    for hwnd, pid in _iter_windows():
        if _window_class(hwnd) != "OrpheusBrowserHost":
            continue
        if not _exe_name(pid).lower().endswith("cloudmusic.exe"):
            continue
        width, height = _window_size(hwnd)
        title = _window_title(hwnd)
        candidates.append(
            ClientWindow(
                pid=pid,
                ipc_hwnd=hwnd,
                title_hwnd=hwnd,
                title=title,
                version="",
            )
        )
        # 大窗口优先，其次是能拿到歌名的窗口
        _ = (width, height)

    if not candidates:
        return None

    def rank(c: ClientWindow) -> tuple[int, int]:
        width, height = _window_size(c.ipc_hwnd)
        big = 1 if width >= 400 and height >= 300 else 0
        minimized = 1 if user32.IsIconic(c.ipc_hwnd) else 0
        return (big + minimized, len(c.title))

    best = max(candidates, key=rank)
    if not best.title:
        for c in candidates:
            if c.title:
                best.title = c.title
                best.title_hwnd = c.ipc_hwnd
                break
    return best


def now_playing(client: ClientWindow | None = None) -> str:
    client = client or find_client()
    if client is None:
        return ""
    title = _window_title(client.title_hwnd) if client.title_hwnd else ""
    if title in ("网易云音乐", "NetEase Cloud Music"):
        return ""
    return title


def client_version(client: ClientWindow | None = None) -> str:
    client = client or find_client()
    if client is None:
        return ""
    try:
        exe = _exe_name(client.pid)
        if not exe:
            return ""
        return subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{exe}').VersionInfo.FileVersion"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


# ------------------------------------------------------------------ 发指令
def _next_tick() -> int:
    global _last_tick
    tick = int(kernel32.GetTickCount()) & 0xFFFFFFFF
    if tick == _last_tick:
        tick = (tick + 1) & 0xFFFFFFFF
    _last_tick = tick
    return tick


LAST_ERROR = 0
LAST_STAGE = ""
LAST_RESULT = 0


def _send(command_id: int, data: str, client: ClientWindow) -> bool:
    global LAST_ERROR, LAST_STAGE, LAST_RESULT
    LAST_ERROR = 0
    LAST_RESULT = 0
    body = json.dumps({"id": command_id, "data": data}, ensure_ascii=False)
    payload = body.encode("utf-8") + b"\0"
    size = len(payload)

    mapping = None
    tick = 0
    for attempt in range(8):
        tick = _next_tick()
        name = f"orpheus_ipc_{client.pid}_{tick}"
        mapping = kernel32.CreateFileMappingW(
            INVALID_HANDLE_VALUE, None, PAGE_READWRITE, 0, size, name
        )
        if not mapping:
            LAST_STAGE, LAST_ERROR = "CreateFileMapping", ctypes.get_last_error()
            return False
        if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
            break
        kernel32.CloseHandle(mapping)
        mapping = None

    if not mapping:
        LAST_STAGE, LAST_ERROR = "mapping-name", 183
        return False

    view = None
    try:
        view = kernel32.MapViewOfFile(mapping, FILE_MAP_WRITE, 0, 0, size)
        if not view:
            LAST_STAGE, LAST_ERROR = "MapViewOfFile", ctypes.get_last_error()
            return False
        ctypes.memmove(view, payload, size)
        result = ctypes.c_size_t(0)
        ctypes.set_last_error(0)
        ok = user32.SendMessageTimeoutW(
            client.ipc_hwnd,
            WM_ORPHEUS_IPC,
            client.pid,
            tick,
            SMTO_BLOCK | SMTO_ABORTIFHUNG,
            1500,
            ctypes.byref(result),
        )
        if not ok:
            LAST_STAGE = "SendMessageTimeout"
            LAST_ERROR = ctypes.get_last_error()
        else:
            LAST_RESULT = int(result.value)
        return bool(ok)
    finally:
        if view:
            kernel32.UnmapViewOfFile(view)
        kernel32.CloseHandle(mapping)


def send_web_command(payload: dict, client: ClientWindow | None = None) -> bool:
    """把客户端自己的 web 指令（play / pause / playingList ...）投递进去。"""
    client = client or find_client()
    if client is None:
        return False
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return _send(3, f"orpheus://{encoded}", client)


def send_hotkey(atom_name: str, vk: int, client: ClientWindow | None = None) -> bool:
    client = client or find_client()
    if client is None:
        return False
    atom = kernel32.GlobalFindAtomW(atom_name)
    if not atom:
        return False
    lparam = (vk << 16) | MOD_CONTROL
    return bool(user32.PostMessageW(client.ipc_hwnd, WM_HOTKEY, atom, lparam))


def next_track(client: ClientWindow | None = None) -> bool:
    return send_hotkey("next_local", VK_RIGHT, client)


def previous_track(client: ClientWindow | None = None) -> bool:
    return send_hotkey("prev_local", VK_LEFT, client)


def toggle_pause(client: ClientWindow | None = None) -> bool:
    return send_hotkey("play_pause_local", VK_P, client)


def insert_next(song_id: str, client: ClientWindow | None = None) -> bool:
    """「下一首播放」——就是把歌插到当前播放项后面。"""
    return send_web_command(
        {"cmd": "playingList", "type": "addToNext", "value": str(song_id)}, client
    )


def play_song(song_id: str, client: ClientWindow | None = None) -> bool:
    return send_web_command({"cmd": "play", "type": "song", "id": str(song_id)}, client)


# ------------------------------------------------------------------ 搜索
SEARCH_URL = "https://music.163.com/api/search/get/web"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Cookie": "os=pc; appver=3.1.37;",
    "Accept": "application/json",
    "Referer": "https://music.163.com/",
}


class SearchError(RuntimeError):
    pass


def search(query: str, limit: int = 20, timeout: float = 10.0) -> list[Track]:
    import requests

    data = {"s": query, "type": "1", "limit": str(limit), "offset": "0"}
    try:
        resp = requests.post(SEARCH_URL, data=data, headers=_HEADERS, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise SearchError(f"请求失败：{exc}") from exc
    if resp.status_code != 200:
        raise SearchError(f"HTTP {resp.status_code}")
    try:
        obj = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise SearchError(f"返回不是 JSON：{resp.text[:120]}") from exc

    result = obj.get("result") or {}
    songs = result.get("songs") or []
    tracks: list[Track] = []
    for song in songs:
        artists = song.get("artists") or song.get("ar") or []
        album = song.get("album") or song.get("al") or {}
        tracks.append(
            Track(
                id=str(song.get("id", "")),
                title=str(song.get("name", "")),
                artists="/".join(
                    str(a.get("name", "")) for a in artists if a.get("name")
                ),
                album=str(album.get("name", "")) if isinstance(album, dict) else "",
            )
        )
    return [t for t in tracks if t.id and t.title]


def _score(track: Track, keyword: str) -> int:
    title = "".join(track.title.split()).lower()
    want = "".join(keyword.split()).lower()
    if not want or not title:
        return 0
    score = 0
    if title == want:
        score += 6
    elif want in title:
        score += 3
    else:
        for i in range(len(want) - 1):
            if want[i : i + 2] in title:
                score += 1
                break
    artist = "".join(track.artists.split()).lower()
    for i in range(len(want) - 1):
        if want[i : i + 2] in artist:
            score += 1
            break
    return score


def pick_best(tracks: list[Track], keyword: str) -> Track | None:
    if not tracks:
        return None
    scored = [(_score(t, keyword), index, t) for index, t in enumerate(tracks)]
    scored.sort(key=lambda item: (-item[0], item[1]))
    best = scored[0]
    return best[2] if best[0] > 0 else tracks[0]


def _artist_matches(track: Track, artist: str) -> bool:
    want = "".join(artist.split()).lower()
    have = "".join(track.artists.split()).lower()
    if not want or not have:
        return True
    return want in have or have in want


# ------------------------------------------------------------------ 队列校验
def playing_list_path() -> Path:
    return (
        Path.home()
        / "AppData"
        / "Local"
        / "NetEase"
        / "CloudMusic"
        / "webdata"
        / "file"
        / "playingList"
    )


def playing_list_ids(path: Path | None = None) -> list[str]:
    """读客户端正在播放列表的歌曲 id（用来确认「真的插进去了」）。"""
    p = path or playing_list_path()
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
        obj = json.loads(raw)
    except Exception:
        return []
    items = obj.get("list") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        return []
    ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        track = item.get("track") if isinstance(item.get("track"), dict) else item
        tid = track.get("id")
        if tid is not None:
            ids.append(str(tid))
    return ids


def wait_in_queue(song_id: str, timeout: float = 2.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if str(song_id) in playing_list_ids():
            return True
        time.sleep(0.15)
    return False


def describe(logger: logging.Logger | None = None) -> str:
    client = find_client()
    if client is None:
        return "没找到网易云客户端（cloudmusic.exe 没开？）"
    title = now_playing(client) or "（当前没有歌曲信息）"
    ids = playing_list_ids()
    version = client_version(client)
    lines = [
        f"进程 {client.pid}  版本 {version or '未知'}",
        f"窗口标题：{title}",
        f"播放列表：{len(ids)} 首",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ 控制器
class NeteaseIpcController:
    def __init__(self, cfg, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.logger = logger
        self._client: ClientWindow | None = None

    def _ensure_client(self) -> ClientWindow | None:
        if self._client is None:
            client = find_client()
            if client is not None and is_mini_window(client):
                # 迷你播放条不是主窗口，IPC 指令发过去客户端不认
                self.logger.error(
                    "网易云现在是「迷你播放条」（窗口只有 %dx%d）。"
                    "请点一下切回主窗口再试。",
                    *_window_size(client.ipc_hwnd),
                )
                return None
            self._client = client
        return self._client

    def add_to_next(
        self, query: str, match: str | None = None, artist: str = "", user: str = ""
    ) -> bool:
        client = self._ensure_client()
        if client is None:
            self.logger.error("没找到网易云客户端：先打开网易云并随便播一首歌")
            return False

        text = f"{query}{self.cfg.netease.search_suffix or ''}".strip()
        try:
            tracks = search(text)
        except SearchError as exc:
            self.logger.error("搜歌失败：%s", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            self.logger.error("搜歌异常：%s: %s", type(exc).__name__, exc)
            return False

        best = pick_best(tracks, match or query)
        if best is None:
            self.logger.warning("搜不到《%s》", text)
            return False

        if artist and not _artist_matches(best, artist):
            self.logger.warning(
                "没找到「%s」的原唱，插的是：%s（网易云可能没这首的版权）",
                artist,
                best.display,
            )

        if not insert_next(best.id, client):
            hint = ""
            if LAST_ERROR == 5:
                hint = "（权限被拒：网易云可能是管理员权限启动的，或你的终端权限更低）"
            elif LAST_ERROR == 0:
                hint = "（客户端没响应：确认网易云没卡死、且不是迷你播放条）"
            self.logger.error(
                "指令投递失败：阶段=%s Win32=%s%s",
                LAST_STAGE,
                LAST_ERROR,
                hint,
            )
            return False

        verified = wait_in_queue(best.id, timeout=2.0)
        if verified:
            self.logger.debug("播放列表已确认：%s", best.display)
        else:
            self.logger.debug("已投递，但播放列表暂未刷新（可能延迟）")

        extra = "" if verified else "（未确认）"
        self.logger.info("命中：%s%s", best.display, extra)
        return True

    def close(self) -> None:
        self._client = None


# ------------------------------------------------------------------ 诊断
_TOKEN_ELEVATION = 20


def _elevation(pid: int) -> str:
    """返回 '管理员' / '普通' / '打不开'。打不开通常意味着对方权限比我们高。"""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return f"打不开(错误{ctypes.get_last_error()})"
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
            return f"无令牌(错误{ctypes.get_last_error()})"
        try:
            value = wintypes.DWORD(0)
            size = wintypes.DWORD(0)
            ok = advapi32.GetTokenInformation(
                token, _TOKEN_ELEVATION, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)
            )
            if not ok:
                return f"查询失败(错误{ctypes.get_last_error()})"
            return "管理员" if value.value else "普通"
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def is_mini_window(client: ClientWindow) -> bool:
    """迷你播放条（很小的一条）——IPC 通常要主窗口，迷你模式容易失败。"""
    width, height = _window_size(client.ipc_hwnd)
    return width < 400 or height < 300


def _integrity(pid: int) -> str:
    """查进程的完整性级别（Low/Medium/High/System）。UIPI 就是按它拦消息的。"""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "?"
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
            return "?"
        try:
            needed = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, 25, None, 0, ctypes.byref(needed))
            if not needed.value:
                return "?"
            buf = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(token, 25, buf, needed.value, ctypes.byref(needed)):
                return "?"
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            count = advapi32.GetSidSubAuthorityCount(sid)[0]
            rid = advapi32.GetSidSubAuthority(sid, count - 1)[0]
            return {
                0x1000: "Low",
                0x2000: "Medium",
                0x3000: "High",
                0x4000: "System",
            }.get(rid, hex(rid))
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def probe_messages(client: ClientWindow) -> list[str]:
    """逐条测试窗口消息，看是「一律被拒」还是「只拦我们的指令」。"""
    lines: list[str] = []
    ok = user32.PostMessageW(client.ipc_hwnd, 0x0000, 0, 0)  # WM_NULL
    lines.append(f"PostMessage(WM_NULL)   -> {'ok' if ok else 'fail'} Win32={ctypes.get_last_error()}")

    result = ctypes.c_size_t(0)
    ctypes.set_last_error(0)
    ok2 = user32.SendMessageTimeoutW(
        client.ipc_hwnd, 0x0000, 0, 0, SMTO_BLOCK | SMTO_ABORTIFHUNG, 1500, ctypes.byref(result)
    )
    lines.append(f"SendMessageTimeout(WM_NULL) -> {'ok' if ok2 else 'fail'} Win32={ctypes.get_last_error()}")

    ctypes.set_last_error(0)
    ok3 = user32.SendMessageTimeoutW(
        client.ipc_hwnd, WM_ORPHEUS_IPC, client.pid, 1, SMTO_BLOCK | SMTO_ABORTIFHUNG, 1500,
        ctypes.byref(result),
    )
    lines.append(f"SendMessageTimeout(0x8001) -> {'ok' if ok3 else 'fail'} Win32={ctypes.get_last_error()}")
    return lines


def diagnose(keyword: str | None = None, do_insert: bool = False, brute: bool = False) -> str:
    """把这条链路每一步都打出来，方便定位。"""
    import subprocess

    lines: list[str] = []
    me = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$id=[Security.Principal.WindowsIdentity]::GetCurrent();"
         "(New-Object Security.Principal.WindowsPrincipal($id)).IsInRole("
         "[Security.Principal.WindowsBuiltInRole]::Administrator)"],
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    lines.append(f"本进程：{'管理员' if me.lower().startswith('true') else '普通用户'} / 完整性 {_integrity(os.getpid())}")

    pids = sorted({p for _, p in _iter_windows() if _exe_name(p).lower().endswith("cloudmusic.exe")})
    lines.append(f"cloudmusic 进程：{pids or '没找到'}")
    for pid in pids:
        lines.append(f"  pid {pid}: {_elevation(pid)} / 完整性 {_integrity(pid)}")

    windows = [
        (hwnd, pid) for hwnd, pid in _iter_windows()
        if _exe_name(pid).lower().endswith("cloudmusic.exe")
        and _window_class(hwnd) == "OrpheusBrowserHost"
    ]
    lines.append(f"OrpheusBrowserHost 窗口：{len(windows)} 个")
    for hwnd, pid in windows:
        width, height = _window_size(hwnd)
        lines.append(
            f"  hwnd=0x{hwnd:X} pid={pid} {width}x{height} 标题={_window_title(hwnd)!r}"
        )

    client = find_client()
    if client is None:
        lines.append("❌ 选不到可用窗口，后面没法测")
        return "\n".join(lines)
    lines.append(f"选中：hwnd=0x{client.ipc_hwnd:X} pid={client.pid}")
    if is_mini_window(client):
        lines.append(
            "  ⚠️ 这是迷你播放条（窗口很小）。请把网易云切回主窗口后再试，"
            "IPC 对迷你模式支持不稳定。"
        )
    lines.append(f"当前播放：{now_playing(client) or '（空）'}")
    lines.append(f"播放列表：{len(playing_list_ids())} 首")
    lines.append("消息探测：")
    for row in probe_messages(client):
        lines.append("  " + row)

    if keyword:
        lines.append(f"搜索「{keyword}」…")
        try:
            tracks = search(keyword, limit=5)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  ❌ 搜索异常：{type(exc).__name__}: {exc}")
            return "\n".join(lines)
        if not tracks:
            lines.append("  ❌ 搜不到任何结果")
            return "\n".join(lines)
        for t in tracks:
            lines.append(f"  {t.id}  {t.display}")
        best = pick_best(tracks, keyword)
        lines.append(f"选中：{best.id} {best.display}")

        if do_insert:
            before = set(playing_list_ids())
            ok = insert_next(best.id, client)
            lines.append(
                f"投递：{'成功' if ok else '失败'}  阶段={LAST_STAGE or '-'} "
                f"Win32={LAST_ERROR} 接收方返回={LAST_RESULT}"
            )
            verified = ok and wait_in_queue(best.id, 2.5)
            if ok:
                lines.append(f"播放列表确认：{'是' if verified else '没有变化'}")
            else:
                after = set(playing_list_ids())
                lines.append(f"播放列表变化：{len(after - before)} 首新增")
                if LAST_ERROR == 5:
                    mine = _integrity(os.getpid())
                    theirs = _integrity(client.pid)
                    lines.append(f"权限对比：本进程 {mine} / 客户端 {theirs}")
                    if mine != theirs:
                        lines.append(
                            "  ❌ 两边完整性级别不一样，系统按 UIPI 拦掉了窗口消息。"
                            "把网易云和这个终端用同一种权限启动（都普通，或都管理员）。"
                        )
                    else:
                        lines.append(
                            "  ❌ 级别相同仍被拒：客户端可能带了 UIAccess/安全软件保护，"
                            "试试用管理员身份开一个终端再跑这条命令。"
                        )

            if ok and not verified:
                lines.append("消息送到了但队列没变 → 试指令变体（会动到播放，这只是诊断）：")
                # A. value 用数字
                before_a = set(playing_list_ids())
                ok_a = send_web_command(
                    {"cmd": "playingList", "type": "addToNext", "value": int(best.id)}
                )
                time.sleep(1.2)
                lines.append(
                    f"  A. value=数字  投递={'ok' if ok_a else 'fail'} "
                    f"返回={LAST_RESULT} 新增={len(set(playing_list_ids()) - before_a)}"
                )

                # B. play 指令：能播就说明通道正常，只是 addToNext 的写法不对
                ok_b = send_web_command({"cmd": "play", "type": "song", "id": str(best.id)})
                time.sleep(2.0)
                lines.append(
                    f"  B. play 指令   投递={'ok' if ok_b else 'fail'} "
                    f"返回={LAST_RESULT} 当前播放={now_playing(client)!r}"
                )

                if brute:
                    lines.append("  C. 暴力试外层命令号（每试一次会真的切歌）：")
                    payload = json.dumps(
                        {"cmd": "play", "type": "song", "id": str(best.id)},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    data = "orpheus://" + base64.b64encode(payload.encode()).decode()
                    for cid in (1, 2, 4, 5, 6, 7, 8):
                        ok_c = _send(cid, data, client)
                        time.sleep(1.5)
                        title = now_playing(client)
                        hit = "  ← 命中！" if best.title in title else ""
                        lines.append(
                            f"     id={cid} 投递={'ok' if ok_c else 'fail'} "
                            f"返回={LAST_RESULT} 播放={title!r}{hit}"
                        )
                        if hit:
                            break

    return "\n".join(lines)
