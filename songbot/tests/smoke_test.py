"""不联网、不动窗口的核心逻辑冒烟测试。

    python tests/smoke_test.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# Windows 控制台默认 GBK，中文/emoji 的测试标签会炸
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from songbot.danmaku.extract import extract, parse_line  # noqa: E402
from songbot.models import Danmaku  # noqa: E402
from songbot.music.netease_ipc import Track, pick_best  # noqa: E402
from songbot.parser import RequestParser, split_artist  # noqa: E402
from songbot.queue import SongQueue, normalize_song  # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got={got!r} want={want!r}")


def dm(content: str, user: str = "观众A") -> Danmaku:
    return Danmaku(user=user, content=content, ts=datetime.now())


def test_parser() -> None:
    print("parser:")
    p = RequestParser(["点歌", "点一首", "来一首"], max_len=40)
    cases = [
        ("点歌 晴天", ("晴天", "")),
        ("点歌晴天", ("晴天", "")),
        ("点歌：稻香", ("稻香", "")),
        ("点歌 《起风了》", ("起风了", "")),
        ("@张三 点歌 起风了", ("起风了", "")),
        ("我想点歌 孤勇者", ("孤勇者", "")),
        ("点一首 平凡之路", ("平凡之路", "")),
        ("来一首 海阔天空。", ("海阔天空", "")),
        ("点歌点歌 夜曲", ("夜曲", "")),
        # 带歌手的分隔写法
        ("点歌 晴天 - 周杰伦", ("晴天", "周杰伦")),
        ("点歌 晴天｜周杰伦", ("晴天", "周杰伦")),
        ("点歌 晴天 / 周杰伦", ("晴天", "周杰伦")),
        ("点歌 晴天 by 周杰伦", ("晴天", "周杰伦")),
        # 空格分隔不拆，整串去搜（命中率也够）
        ("点歌 晴天 周杰伦", ("晴天 周杰伦", "")),
        # 「7-11」不能被拆成歌名 7
        ("点歌 7-11", ("7-11", "")),
        ("点歌", None),
        ("点歌 。", None),
        ("今天天气不错", None),
        ("点歌 " + "长" * 60, None),
        ("点歌 🎵", None),
    ]
    for text, want in cases:
        got = p.parse(dm(text))
        value = (got.song, got.artist) if got else None
        check(text, value, want)


def test_strict() -> None:
    print("strict / require_artist:")
    strict = RequestParser(["点歌"], strict=True)
    check("严格：开头可以", strict.parse(dm("点歌 晴天")) is not None, True)
    check("严格：中间出现不行", strict.parse(dm("我想点歌 晴天")), None)
    check("严格：夹带聊天词不行", strict.parse(dm("点歌 晴天 能不能唱")), None)
    check("严格：@前缀仍可用", strict.parse(dm("@小明 点歌 晴天")) is not None, True)

    need = RequestParser(["点歌"], require_artist=True)
    check("必须带歌手：没带", need.parse(dm("点歌 晴天")), None)
    check("必须带歌手：带了", need.parse(dm("点歌 晴天 - 周杰伦")) is not None, True)


def test_split_artist() -> None:
    print("split_artist:")
    check("斜杠", split_artist("晴天/周杰伦"), ("晴天", "周杰伦"))
    check("普通歌名", split_artist("晴天"), ("晴天", ""))
    check("7-11", split_artist("7-11"), ("7-11", ""))


def test_ranking() -> None:
    print("pick_best:")
    tracks = [
        Track("1", "晴天", "翻唱歌手"),
        Track("2", "晴天", "周杰伦"),
        Track("3", "晴天 (Live)", "周杰伦"),
        Track("4", "无关", "某人"),
    ]
    best = pick_best(tracks, "晴天 周杰伦")
    check("优先带歌手的", best.id if best else None, "2")


def test_extract() -> None:
    print("extract:")
    line = '{"type": "WebcastChatMessage", "content": "点歌 晴天", "user": {"nickname": "小明"}}'
    item = extract(parse_line(line))
    check("json 行", (item.user, item.content), ("小明", "点歌 晴天"))

    line2 = "{'type': 'WebcastChatMessage', 'content': '点歌 稻香', 'nickname': '阿花'}"
    item2 = extract(parse_line(line2))
    check("dict 行", (item2.user, item2.content), ("阿花", "点歌 稻香"))

    line3 = '{"type": "WebcastMemberMessage", "user": {"nickname": "路人"}}'
    check("进场消息", extract(parse_line(line3)), None)
    check("启动日志", parse_line("start websocket ..."), None)


def test_queue() -> None:
    print("queue:")
    q = SongQueue(per_user_cooldown=0, per_user_max_pending=1, global_cooldown=0,
                  duplicate_window=600, max_queue=10)
    p = RequestParser(["点歌"])
    r1 = p.parse(dm("点歌 晴天", "小明"))
    check("首次接受", q.check(r1)[0], True)
    q.accept(r1, pushed=False)

    check("同一人排满", q.check(p.parse(dm("点歌 稻香", "小明")))[1].startswith("这个人"), True)
    check("另一人可点", q.check(p.parse(dm("点歌 稻香", "小刚")))[0], True)
    check("重复歌拦截", q.check(p.parse(dm("点歌 晴天", "小红")))[0], False)
    check("归一化", normalize_song("晴天 (Live)"), normalize_song("晴天"))


if __name__ == "__main__":
    test_parser()
    test_strict()
    test_split_artist()
    test_ranking()
    test_extract()
    test_queue()
    print()
    print("失败数：", FAILED)
    sys.exit(1 if FAILED else 0)
