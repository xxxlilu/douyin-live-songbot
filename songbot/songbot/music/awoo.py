"""复用嗷呜点歌机的网易云连接器（Awoo.Connector.Netease.exe）。

为什么不用自己那套 `orpheus://` 窗口消息：网易云 3.1.41+ 已经把那条通道废弃了，
新版点歌走「进程内 CEF 桥」——连接器会往客户端里注入 AwooNcmCefBridge.dll
去操作它的 Redux store。这套东西有版本锁和 ABI 校验，自己复刻不现实，
所以直接驱动它：连接器用 NDJSON 走 stdin/stdout，协议是：

    {"id":"1","action":"ping"}                          -> 能力
    {"id":"2","action":"search","query":"起风了"}        -> 曲目列表
    {"id":"3","action":"execute","command":"InsertNext","track":{...}}
    {"id":"9","action":"shutdown"}
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
from pathlib import Path

DEFAULT_EXE = (
    Path("tools") / "awoo-connector" / "extracted" / "Awoo.Connector.Netease.exe"
)


class ConnectorError(RuntimeError):
    pass


class AwooConnector:
    def __init__(self, exe: Path, logger: logging.Logger) -> None:
        self.exe = Path(exe)
        self.logger = logger
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str] = queue.Queue()
        self._seq = 0
        # 可重入：_request 里可能要调 _start，而 _start 又会发 ping
        self._lock = threading.RLock()

    # ---------------- 进程 ----------------
    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start(self) -> None:
        if self._alive():
            return
        if not self.exe.is_file():
            raise ConnectorError(
                f"找不到连接器：{self.exe}\n"
                "在项目目录执行：python tools\\install_connector.py"
            )
        self.logger.info("启动网易云连接器：%s", self.exe.name)
        self._proc = subprocess.Popen(
            [str(self.exe)],
            cwd=str(self.exe.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._lines = queue.Queue()

        def pump() -> None:
            assert self._proc is not None and self._proc.stdout is not None
            for line in self._proc.stdout:
                self._lines.put(line.rstrip())
            self._lines.put("")

        threading.Thread(target=pump, daemon=True).start()
        self._request("ping", timeout=10)

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.stdin.write(json.dumps({"id": "bye", "action": "shutdown"}) + "\n")
                proc.stdin.flush()
                proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---------------- 请求 ----------------
    def _request(self, action: str, timeout: float = 20.0, **fields) -> dict:
        with self._lock:
            if not self._alive():
                self._start()
            assert self._proc is not None and self._proc.stdin is not None
            self._seq += 1
            rid = str(self._seq)
            payload = {"id": rid, "action": action, **fields}
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()

            deadline = timeout
            while True:
                try:
                    line = self._lines.get(timeout=deadline)
                except queue.Empty:
                    raise ConnectorError(f"{action} 超时（{timeout:.0f}s）") from None
                if line == "":
                    raise ConnectorError(f"{action} 时连接器退出了")
                if not line.startswith("{"):
                    self.logger.debug("[connector] %s", line[:300])
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    self.logger.debug("[connector] 非 JSON：%s", line[:200])
                    continue
                if str(obj.get("id")) != rid:
                    self.logger.debug("[connector] 忽略未知响应 id=%s", obj.get("id"))
                    continue
                if not obj.get("ok"):
                    raise ConnectorError(str(obj.get("error") or "连接器返回失败"))
                return obj.get("result")

    # ---------------- 能力 ----------------
    def capabilities(self) -> dict:
        return self._request("ping", timeout=10)

    def probe(self) -> dict:
        return self._request("probe", timeout=20)

    def search(self, query: str) -> list[dict]:
        result = self._request("search", timeout=25, query=query)
        return result or []

    def execute(self, command: str, track: dict | None = None,
                track_id: str | None = None) -> dict:
        fields = {"command": command}
        if track is not None:
            fields["track"] = track
        elif track_id is not None:
            fields["trackId"] = track_id
        # 第一次执行要装 CEF 桥，给足时间
        return self._request("execute", timeout=60, **fields)


# ------------------------------------------------------------------ 控制器
class AwooController:
    def __init__(self, cfg, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.logger = logger
        exe = getattr(cfg.netease, "connector", "") or DEFAULT_EXE
        path = Path(exe)
        if not path.is_absolute():
            path = cfg.root / path
        self.connector = AwooConnector(path, logger)

    def _pick(self, tracks: list[dict], keyword: str, artist: str) -> dict | None:
        if not tracks:
            return None

        want = "".join(keyword.split()).lower()
        artist_key = "".join(artist.split()).lower() if artist else ""

        def score(t: dict) -> tuple[int, int]:
            title = "".join(str(t.get("title", "")).split()).lower()
            who = "".join(str(t.get("artist", "")).split()).lower()
            s = 0
            if title == want:
                s += 6
            elif want and want in title:
                s += 3
            else:
                for i in range(len(want) - 1):
                    if want[i : i + 2] in title:
                        s += 1
                        break
            if artist_key and (artist_key in who or who in artist_key):
                s += 4
            return s

        ranked = sorted(range(len(tracks)), key=lambda i: (-score(tracks[i]), i))
        return tracks[ranked[0]]

    def add_to_next(
        self, query: str, match: str | None = None, artist: str = "", user: str = ""
    ) -> bool:
        try:
            info = self.connector.probe()
        except ConnectorError as exc:
            self.logger.error("连接器不可用：%s", exc)
            return False

        if not info.get("connected"):
            self.logger.error("网易云没连上：%s", info.get("status") or "未知原因")
            self.logger.error(
                "（连接器要求客户端在运行，且首次使用可能要装 CEF 桥；"
                "管理员权限运行本程序更稳）"
            )
            return False

        text = f"{query}{self.cfg.netease.search_suffix or ''}".strip()
        try:
            tracks = self.connector.search(text)
        except ConnectorError as exc:
            self.logger.error("搜歌失败：%s", exc)
            return False

        best = self._pick(tracks, match or query, artist)
        if best is None:
            self.logger.warning("搜不到《%s》", text)
            return False

        try:
            result = self.connector.execute("InsertNext", track=best)
        except ConnectorError as exc:
            # 有些版本只要 trackId
            try:
                result = self.connector.execute("InsertNext", track_id=str(best.get("id")))
            except ConnectorError as exc2:
                self.logger.error("插入失败：%s / %s", exc, exc2)
                return False

        outcome = str(result.get("outcome", "")).lower()
        message = str(result.get("message") or "")
        name = best.get("displayName") or best.get("title")
        if outcome in ("verified", "accepted", "applied"):
            self.logger.info("命中：%s（%s）", name, outcome)
            return True

        self.logger.error("插入未成功（%s）：%s", outcome or "未知", message)
        return False

    def close(self) -> None:
        self.connector.close()


# ------------------------------------------------------------------ 诊断
def _connector_path(cfg) -> Path:
    exe = getattr(cfg.netease, "connector", "") or DEFAULT_EXE
    path = Path(exe)
    return path if path.is_absolute() else cfg.root / path


def diagnose_awoo(cfg, keyword: str | None = None, do_insert: bool = False) -> str:
    lines: list[str] = []
    conn = AwooConnector(_connector_path(cfg), __import__("logging").getLogger("songbot"))
    try:
        caps = conn.capabilities()
        ability = caps.get("capabilities") or {}
        lines.append(
            f"连接器：{caps.get('connectorId')} {caps.get('connectorVersion')} "
            f"协议 v{caps.get('protocolVersion')}"
        )
        lines.append(
            f"能力：插入下一首={ability.get('insertNext')} "
            f"（{ability.get('insertNextLevel')}）"
        )

        info = conn.probe()
        lines.append(f"连接：{info.get('connected')}  {info.get('status') or ''}")
        if info.get("current"):
            cur = info["current"]
            lines.append(f"当前播放：{cur.get('displayName') or cur.get('title')}")
        if not info.get("connected"):
            lines.append(
                "  ❌ 连接器连不上客户端：确认网易云在运行；"
                "首次使用可能要装 CEF 桥，用管理员终端更稳。"
            )
            return "\n".join(lines)

        if keyword:
            tracks = conn.search(keyword)
            lines.append(f"搜索「{keyword}」：{len(tracks)} 条")
            for t in tracks[:5]:
                lines.append(f"  {t.get('id')}  {t.get('displayName') or t.get('title')}")
            if not tracks:
                return "\n".join(lines)

            best = tracks[0]
            lines.append(f"选第一条：{best.get('id')} {best.get('displayName')}")
            if do_insert:
                result = conn.execute("InsertNext", track=best)
                lines.append(
                    f"插入：{result.get('outcome')}  {result.get('message') or ''}"
                )
    except ConnectorError as exc:
        lines.append(f"❌ {exc}")
    finally:
        conn.close()
    return "\n".join(lines)
