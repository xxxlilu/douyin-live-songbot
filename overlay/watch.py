"""盯着网易云的 playingList：看它什么时候才真的写文件。

用法（在 next5-overlay 目录）：
    ..\\douyin-songbot\\.venv\\Scripts\\python.exe watch.py

然后在网易云里依次做这些动作，看哪一步会让这里打印「文件更新」：
    1. 在队列里拖动排序
    2. 右键某首歌 → 下一首播放
    3. 手动切歌（下一首）
    4. 暂停 / 继续

Ctrl+C 结束。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

PLAYING_LIST = Path.home() / "AppData/Local/Netease/CloudMusic/webdata/file/playingList"


def names(path: Path, limit: int = 3) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ["<解析失败>"]
    out = []
    for item in data.get("list", [])[:limit]:
        track = item.get("track") or {}
        out.append(str(track.get("name") or "?"))
    return out


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def main() -> None:
    if not PLAYING_LIST.is_file():
        raise SystemExit(f"找不到 {PLAYING_LIST}")
    st = PLAYING_LIST.stat()
    print(f"{stamp()}  开始监听（初读 {st.st_size} 字节）")
    print(f"          前三首：{names(PLAYING_LIST)}")
    print("          现在去网易云里改队列，看哪一步会触发更新…\n")

    last_mtime = st.st_mtime
    last_size = st.st_size
    try:
        while True:
            time.sleep(0.5)
            try:
                st = PLAYING_LIST.stat()
            except OSError:
                continue
            if st.st_mtime != last_mtime or st.st_size != last_size:
                print(
                    f"{stamp()}  ⚡ 文件更新  size {last_size} -> {st.st_size}  "
                    f"前三首：{names(PLAYING_LIST)}"
                )
                last_mtime, last_size = st.st_mtime, st.st_size
    except KeyboardInterrupt:
        print("\n结束。")


if __name__ == "__main__":
    main()
