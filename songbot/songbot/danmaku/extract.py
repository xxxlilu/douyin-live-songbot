"""从外部脚本的输出里，宽容地捞出「谁说了什么」。

不同的弹幕库输出字段名不一样（content / text / msg / nickname / user ...），
这里不写死某一家，递归找最像的那个。
"""

from __future__ import annotations

import ast
import json
from typing import Any

CONTENT_KEYS = ("content", "text", "msg", "message", "comment", "danmaku")
USER_KEYS = ("nickname", "nick_name", "nname", "user_name", "username", "name", "user")
TYPE_KEYS = ("type", "method", "msg_type", "msgType")


class ExtractResult:
    __slots__ = ("type_name", "user", "content")

    def __init__(self, type_name: str, user: str, content: str) -> None:
        self.type_name = type_name
        self.user = user
        self.content = content


def parse_line(line: str) -> Any | None:
    """一行文本 → dict。支持 JSON，也支持 Python dict 的 repr。"""
    line = (line or "").strip()
    if not line or not (line.startswith("{") or line.startswith("[")):
        return None
    try:
        return json.loads(line)
    except Exception:
        pass
    try:
        value = ast.literal_eval(line)
        return value if isinstance(value, (dict, list)) else None
    except Exception:
        return None


def _walk(obj: Any):
    """深度优先遍历所有 dict。"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)


def _lookup(d: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        for dk, dv in d.items():
            if str(dk).lower() == k and isinstance(dv, str) and dv.strip():
                return dv.strip()
    return ""


def _type_name(obj: Any) -> str:
    for d in _walk(obj):
        for k in TYPE_KEYS:
            for dk, dv in d.items():
                if str(dk).lower() == k and isinstance(dv, str) and dv:
                    return dv
    return ""


def extract(obj: Any) -> ExtractResult | None:
    """从一条消息里取出 (类型, 用户名, 内容)。取不到内容就返回 None。"""
    content = ""
    user = ""
    # 先按「最外层」找，通常昵称在外层 user 字段里
    for d in _walk(obj):
        if not content:
            content = _lookup(d, CONTENT_KEYS)
        if not user:
            user = _lookup(d, USER_KEYS)
        if content and user:
            break

    if not content:
        return None

    type_name = _type_name(obj)
    if type_name and "chat" not in type_name.lower():
        # 进场、点赞、礼物之类的消息，不是评论
        return None
    return ExtractResult(type_name, user, content)
