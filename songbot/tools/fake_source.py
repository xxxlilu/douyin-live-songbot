"""假弹幕源：开播前想验证整条链，但不想真去连抖音时用。

    python tools/fake_source.py

每 3 秒吐一条「点歌 XX」，格式和 [danmaku] command 约定的 JSON 行一致。
"""

from __future__ import annotations

import json
import random
import time

SONGS = ["晴天", "稻香", "起风了", "平凡之路", "海阔天空"]
USERS = ["小明", "阿花", "路人甲", "老铁666"]

def main() -> None:
    print(json.dumps({"type": "WebcastChatMessage", "content": "大家好", "user": {"nickname": "路人乙"}},
                     ensure_ascii=False), flush=True)
    for _ in range(100):
        song = random.choice(SONGS)
        user = random.choice(USERS)
        print(json.dumps({"type": "WebcastChatMessage", "content": f"点歌 {song}", "user": {"nickname": user}},
                         ensure_ascii=False), flush=True)
        time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
