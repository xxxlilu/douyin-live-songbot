"""把一条弹幕变成「点歌请求」。

支持两种写法：
    点歌 晴天
    点歌 晴天 周杰伦   /  点歌 晴天 - 周杰伦  /  点歌 晴天｜周杰伦

严格模式（[song] strict = true）下，必须是「点歌」开头、且不带
「能不能/帮我/谢谢」这类聊天词；解析失败直接丢弃，不打扰主播。
"""

from __future__ import annotations

import re
import unicodedata

from .models import Danmaku, SongRequest

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")
_TRAILING = re.compile(r"[\s，,。.！!？?、~～…；;：:]+$")
_HAS_WORD = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
_ONLY_PUNCT = re.compile(r"^[\W_]+$")
_MENTIONS = re.compile(r"^\s*(?:@\S+\s*)+")
_QUOTES = re.compile(r"^[《「『\"'“”【]+|[》」』\"'“”】]+$")
_CHATTY = re.compile(r"(能不能|可不可以|可以吗|帮我|请帮|谢谢|求求|主播|唱一下|来一个|好不好|行不行|安排)")

_SEPS = ("｜", "|", "／", "/", "\\")
_DASH = re.compile(r"\s+[-–—]+\s+")
_DASH_TIGHT = re.compile(r"^(.{2,}?)[-–—](.{1,20})$")
_BY = re.compile(r"\s+by\s+", re.IGNORECASE)


def clean_text(text: str) -> str:
    """去掉零宽字符、emoji、多余空白，统一全角半角。"""
    s = unicodedata.normalize("NFKC", text or "")
    s = _ZERO_WIDTH.sub("", s)
    s = _EMOJI.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def split_artist(text: str) -> tuple[str, str]:
    """从「歌名 歌手」里拆出歌手；拆不出来就整串当歌名。"""
    for sep in _SEPS:
        if sep in text:
            left, right = text.split(sep, 1)
            if left.strip() and right.strip() and len(right.strip()) <= 30:
                return left.strip(), right.strip()

    m = _BY.search(text)
    if m and text[: m.start()].strip() and text[m.end() :].strip():
        return text[: m.start()].strip(), text[m.end() :].strip()

    m = _DASH.search(text)
    if m:
        left, right = text[: m.start()].strip(), text[m.end() :].strip()
        if left and right:
            return left, right

    m = _DASH_TIGHT.match(text)
    if m:
        left, right = m.group(1).strip(), m.group(2).strip()
        # 「7-11」这种左边太短的别拆
        if len(left) >= 2 and right:
            return left, right

    return text, ""


class RequestParser:
    """认「点歌 XXX」这类弹幕。"""

    def __init__(
        self,
        keywords: list[str],
        max_len: int = 40,
        *,
        strict: bool = False,
        require_artist: bool = False,
    ) -> None:
        self.keywords = [k for k in (keywords or []) if k]
        self.max_len = max_len
        self.strict = strict
        self.require_artist = require_artist
        if self.keywords:
            alts = "|".join(re.escape(k) for k in sorted(self.keywords, key=len, reverse=True))
            self._pattern = re.compile(
                rf"(?:{alts})\s*[:：]?\s*(?P<song>.+)",
                re.IGNORECASE | re.DOTALL,
            )
            self._strict_pattern = re.compile(
                rf"^\s*(?:{alts})\s*[:：]?\s*(?P<song>.+)$",
                re.IGNORECASE | re.DOTALL,
            )
        else:
            self._pattern = None
            self._strict_pattern = None

    def parse(self, dm: Danmaku) -> SongRequest | None:
        if self._pattern is None:
            return None

        text = clean_text(dm.content)
        if not text:
            return None

        # 「@张三 点歌 晴天」→ 去掉 @ 前缀
        text = _MENTIONS.sub("", text).strip()

        pattern = self._strict_pattern if self.strict else self._pattern
        m = pattern.search(text)  # type: ignore[union-attr]
        if not m:
            return None

        raw = m.group("song")
        if self.strict and _CHATTY.search(raw):
            return None

        song, artist = split_artist(clean_text(raw))
        song = self._clean_song(song)
        if not song:
            return None
        artist = _TRAILING.sub("", (artist or "").strip())
        if len(artist) > 30:
            artist = ""
        if self.require_artist and not artist:
            return None

        return SongRequest(
            user=(dm.user or "匿名观众").strip(),
            song=song,
            artist=artist,
            ts=dm.ts,
            raw_content=dm.content,
            danmaku=dm,
        )

    def _clean_song(self, raw: str) -> str:
        s = clean_text(raw)
        # 「点歌点歌晴天」这种重复触发词，剥掉前面的
        for _ in range(3):
            stripped = False
            for k in sorted(self.keywords, key=len, reverse=True):
                if s.lower().startswith(k.lower()) and len(s) > len(k):
                    s = s[len(k) :].lstrip(" :：")
                    stripped = True
            if not stripped:
                break
        s = _QUOTES.sub("", s.strip())
        s = _TRAILING.sub("", s).strip()
        if not s or len(s) > self.max_len:
            return ""
        if _ONLY_PUNCT.match(s) or not _HAS_WORD.search(s):
            return ""
        return s
