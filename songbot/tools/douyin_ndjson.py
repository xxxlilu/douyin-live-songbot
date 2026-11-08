"""把 DouyinLiveWebFetcher 的输出转成逐行 JSON，喂给 songbot。

用法：
    python tools/douyin_ndjson.py https://live.douyin.com/123456789

设计要点：
- 只在 stdout 打 JSON（一行一条）；弹幕库自己的中文日志全部改道 stderr，
  主程序就能干净地按行解析，出问题时 stderr 也看得到。
- 房间号从地址里抠：库要的是纯数字 web_rid，不是整条 URL。
- 库要跑 sign.js 生成签名。它默认用 py_mini_racer，但那个包在新 Python 上
  常常没有 wheel；本脚本检测到缺了就改用本机 Node 执行同一份 JS，
  所以只要能 `node -v`，就不必装 py_mini_racer / PyExecJS。
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "DouyinLiveWebFetcher"
if not VENDOR.is_dir():
    raise SystemExit(
        f"没找到弹幕库：{VENDOR}\n"
        "先执行：git clone https://github.com/saermart/DouyinLiveWebFetcher vendor/DouyinLiveWebFetcher"
    )
sys.path.insert(0, str(VENDOR))

# 库里的 print 会写满屏，全部改道 stderr；stdout 留给 JSON
_json_out = sys.stdout
sys.stdout = sys.stderr


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


# ------------------------------------------------------------------ JS 引擎兜底
class _NodeJS:
    """用 node 执行一段 JS，再调用其中某个全局函数；接口仿 py_mini_racer。

    注意：sign.js 有 480KB，塞不进命令行（Windows 上限 32KB），
    所以先落盘成临时文件，再 `node file.js <json>`。
    """

    _WORKDIR = Path(__file__).resolve().parent.parent / ".tmp" / "nodejs"

    def __init__(self, code: str = "") -> None:
        self._code = code

    def eval(self, code: str) -> None:
        self._code = code

    def call(self, name: str, *args):
        node = shutil.which("node")
        if not node:
            raise RuntimeError("缺 Node.js：装个 Node，或 pip install py-mini-racer")

        if not re.fullmatch(r"[A-Za-z_$][\w$]*", name):
            raise ValueError(f"函数名不合法：{name!r}")
        self._WORKDIR.mkdir(parents=True, exist_ok=True)
        # Node 会把脚本包进模块作用域，顶层 function 不在 globalThis 上，
        # 所以直接按名字调用，别走 globalThis[name]。
        script = (
            self._code
            + "\nconst __args = JSON.parse(process.argv[2] || '[]');\n"
            + f"process.stdout.write(JSON.stringify({name}(...__args)));"
        )
        import tempfile

        handle, path = tempfile.mkstemp(suffix=".js", dir=str(self._WORKDIR))
        try:
            with open(handle, "w", encoding="utf-8") as fh:
                fh.write(script)
            proc = subprocess.run(
                [node, path, json.dumps(list(args), ensure_ascii=False)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
        finally:
            try:
                Path(path).unlink()
            except OSError:
                pass

        if proc.returncode != 0:
            raise RuntimeError(f"node 执行失败：{proc.stderr[-600:]}")
        return json.loads(proc.stdout or "null")


def _install_compat() -> None:
    """补齐 liveMan 需要的 `py_mini_racer` / `execjs`，优先用现成的，其次 Node。"""
    have_racer = _has("py_mini_racer")
    have_js = _has("execjs")

    if not have_racer:
        module = types.ModuleType("py_mini_racer")
        module.MiniRacer = _NodeJS  # type: ignore[attr-defined]
        sys.modules["py_mini_racer"] = module
        print("[i] 没装 py_mini_racer，改用 Node 执行签名 JS", file=sys.stderr)

    if not have_js:
        module = types.ModuleType("execjs")
        module.compile = lambda code: _NodeJS(code)  # type: ignore[attr-defined]
        sys.modules["execjs"] = module
        print("[i] 没装 PyExecJS，改用 Node", file=sys.stderr)


# ------------------------------------------------------------------ 主流程
def _missing_deps() -> list[str]:
    names = {"requests": "requests", "websocket": "websocket-client", "betterproto": "betterproto"}
    return [pkg for mod, pkg in names.items() if not _has(mod)]


def chat_record(message) -> dict:
    return {
        "type": "WebcastChatMessage",
        "user": {
            "uid": str(getattr(getattr(message, "user", None), "id", "")),
            "nickname": getattr(getattr(message, "user", None), "nick_name", ""),
        },
        "content": getattr(message, "content", ""),
    }


class ChatOnlyMixin:
    """只把聊天消息转成 JSON；其余消息静音。

    原库会把礼物/排行榜整个对象 print 出来（一条排行榜十几 KB），
    进到主程序日志里会把文件刷爆。
    """

    def _parseChatMsg(self, payload):  # type: ignore[no-untyped-def]
        from liveMan import ChatMessage

        message = ChatMessage().parse(payload)
        print(json.dumps(chat_record(message), ensure_ascii=False), file=_json_out, flush=True)

    def _parseRankMsg(self, payload):  # type: ignore[no-untyped-def]
        pass

    def _parseRoomUserSeqMsg(self, payload):  # type: ignore[no-untyped-def]
        pass

    def _parseRoomStatsMsg(self, payload):  # type: ignore[no-untyped-def]
        pass

    def _parseMemberMsg(self, payload):  # type: ignore[no-untyped-def]
        pass

    def _parseLikeMsg(self, payload):  # type: ignore[no-untyped-def]
        pass

    def _parseGiftMsg(self, payload):  # type: ignore[no-untyped-def]
        pass


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法：python tools/douyin_ndjson.py <直播间地址>")

    missing = _missing_deps()
    if missing:
        print(
            "[X] 缺依赖：" + ", ".join(missing) + "\n"
            "    在项目目录执行：.\\setup.ps1\n"
            "    或手动：python -m pip install " + " ".join(missing),
            file=sys.stderr,
        )
        raise SystemExit(2)

    raw = sys.argv[1]
    match = re.search(r"live\.douyin\.com/(\d+)", raw) or re.search(r"(\d{6,})", raw)
    if not match:
        raise SystemExit(f"从 {raw!r} 里看不出房间号，格式应该是 https://live.douyin.com/123456789")
    live_id = match.group(1)

    _install_compat()

    # 库用相对路径打开 sign.js / a_bogus.js，必须切到它自己目录
    import os

    os.chdir(VENDOR)

    try:
        from liveMan import DouyinLiveWebFetcher
    except Exception as exc:  # noqa: BLE001
        print(f"[X] 导入弹幕库失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc

    class JsonEmitter(ChatOnlyMixin, DouyinLiveWebFetcher):  # type: ignore[misc]
        pass

    fetcher = JsonEmitter(live_id)
    try:
        # 先解析 roomId；房间没开播/房间号不对时这里就没有 roomId
        _ = fetcher.room_id
    except Exception:  # noqa: BLE001
        print(
            "[X] 这个直播间当前不在播（或房间号不对）。开播后再跑，"
            f"地址：https://live.douyin.com/{live_id}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    print(f"[i] 连接直播间 {live_id} …", file=sys.stderr, flush=True)
    fetcher.start()


if __name__ == "__main__":
    main()
