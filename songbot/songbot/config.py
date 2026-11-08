"""读 config.ini，给出带默认值的配置对象。"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from pathlib import Path


def _bool(cp: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    try:
        return cp.getboolean(section, key, fallback=default)
    except ValueError:
        return default


def _int(cp: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    try:
        return cp.getint(section, key, fallback=default)
    except ValueError:
        return default


def _float(cp: configparser.ConfigParser, section: str, key: str, default: float) -> float:
    try:
        return cp.getfloat(section, key, fallback=default)
    except ValueError:
        return default


def _str(cp: configparser.ConfigParser, section: str, key: str, default: str = "") -> str:
    return (cp.get(section, key, fallback=default) or "").strip()


@dataclass(slots=True)
class GeneralConfig:
    room_url: str = ""
    dry_run: bool = False
    log_file: str = "logs/songbot.log"


@dataclass(slots=True)
class DanmakuConfig:
    source: str = "command"
    command: str = ""
    reconnect_seconds: float = 5.0


@dataclass(slots=True)
class SongConfig:
    keywords: list[str] = field(default_factory=lambda: ["点歌"])
    max_len: int = 40
    strict: bool = False
    require_artist: bool = False
    per_user_cooldown_seconds: float = 90.0
    per_user_max_pending: int = 2
    global_cooldown_seconds: float = 3.0
    duplicate_window_seconds: float = 1800.0
    max_queue: int = 50


@dataclass(slots=True)
class NeteaseConfig:
    mode: str = "queue"
    connector: str = ""
    executable: str = r"C:\Program Files\Netease\CloudMusic\cloudmusic.exe"
    search_suffix: str = ""
    prefer_single_tab: bool = True
    action: str = "下一首播放"
    search_wait_seconds: float = 2.5
    fallback: str = "warn"


@dataclass(slots=True)
class QueueConfig:
    inbox: str = "../overlay/queue.inbox.jsonl"


@dataclass(slots=True)
class Config:
    path: Path
    general: GeneralConfig = field(default_factory=GeneralConfig)
    danmaku: DanmakuConfig = field(default_factory=DanmakuConfig)
    song: SongConfig = field(default_factory=SongConfig)
    netease: NeteaseConfig = field(default_factory=NeteaseConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)

    @property
    def root(self) -> Path:
        return self.path.parent


def load_config(path: str | Path) -> Config:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"找不到配置文件：{p}")

    cp = configparser.ConfigParser(interpolation=None)
    cp.read(p, encoding="utf-8")

    raw_keywords = _str(cp, "song", "keywords", "点歌")
    keywords = [k.strip() for k in raw_keywords.replace("，", ",").split(",") if k.strip()]

    cfg = Config(
        path=p,
        general=GeneralConfig(
            room_url=_str(cp, "general", "room_url"),
            dry_run=_bool(cp, "general", "dry_run", False),
            log_file=_str(cp, "general", "log_file", "logs/songbot.log"),
        ),
        danmaku=DanmakuConfig(
            source=_str(cp, "danmaku", "source", "command").lower(),
            command=_str(cp, "danmaku", "command"),
            reconnect_seconds=_float(cp, "danmaku", "reconnect_seconds", 5.0),
        ),
        song=SongConfig(
            keywords=keywords or ["点歌"],
            max_len=_int(cp, "song", "max_len", 40),
            strict=_bool(cp, "song", "strict", False),
            require_artist=_bool(cp, "song", "require_artist", False),
            per_user_cooldown_seconds=_float(cp, "song", "per_user_cooldown_seconds", 90.0),
            per_user_max_pending=_int(cp, "song", "per_user_max_pending", 2),
            global_cooldown_seconds=_float(cp, "song", "global_cooldown_seconds", 3.0),
            duplicate_window_seconds=_float(cp, "song", "duplicate_window_seconds", 1800.0),
            max_queue=_int(cp, "song", "max_queue", 50),
        ),
        netease=NeteaseConfig(
            mode=_str(cp, "netease", "mode", "queue").lower(),
            connector=_str(cp, "netease", "connector"),
            executable=_str(cp, "netease", "executable", r"C:\Program Files\Netease\CloudMusic\cloudmusic.exe"),
            search_suffix=_str(cp, "netease", "search_suffix"),
            prefer_single_tab=_bool(cp, "netease", "prefer_single_tab", True),
            action=_str(cp, "netease", "action", "下一首播放"),
            search_wait_seconds=_float(cp, "netease", "search_wait_seconds", 2.5),
            fallback=_str(cp, "netease", "fallback", "warn"),
        ),
        queue=QueueConfig(
            inbox=_str(cp, "queue", "inbox", "../overlay/queue.inbox.jsonl"),
        ),
    )
    return cfg
