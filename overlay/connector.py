"""嗷呜网易云连接器的 NDJSON 客户端。

协议（实测）：
  {"id":"1","action":"ping"}
  {"id":"2","action":"probe"}
  {"id":"3","action":"search","query":"..."}
  {"id":"4","action":"execute","command":"InsertNext","track":{"id":"..."}}
  {"id":"5","action":"subscribe","eventProtocolVersion":1}
  {"id":"9","action":"shutdown"}

事件是独立一行：{"type":"event","event":"snapshot","sequence":N,"snapshot":{...}}
snapshot 里有 current / next（都可能是 null）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path


class ConnectorError(RuntimeError):
    pass


def clean_env() -> dict:
    """给子进程一份不带代理的环境。

    网易云搜索走 music.163.com，直连很快；一旦继承了终端里的 HTTP_PROXY，
    .NET 的 HttpClient 就会绕代理，慢到搜索超时、点歌被跳过。
    顺手把 NO_PROXY 设成 *，防止系统级代理再插一脚。
    """
    env = os.environ.copy()
    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
    ):
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


class Connector:
    def __init__(self, exe: Path, logger: logging.Logger) -> None:
        self.exe = Path(exe)
        self.log = logger
        self._proc: subprocess.Popen | None = None
        self._seq = 0
        self._lock = threading.RLock()
        self._waiters: dict[str, deque] = {}
        self._snapshot: dict = {}
        self._snapshot_at = 0.0
        self._events_seen = 0
        self._subscribed = False

    # ---------------------------------------------------------------- 进程
    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self._alive():
            return
        if not self.exe.is_file():
            raise ConnectorError(f"找不到连接器：{self.exe}")
        self.log.info("启动网易云连接器：%s", self.exe.name)
        self._proc = subprocess.Popen(
            [str(self.exe)],
            cwd=str(self.exe.parent),
            env=clean_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        self.request("ping", timeout=10)

    def close(self) -> None:
        proc, self._proc = self._proc, None
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

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                if line:
                    self.log.debug("[connector] %s", line[:300])
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "event" and obj.get("event") == "snapshot":
                self._snapshot = obj.get("snapshot") or {}
                self._snapshot_at = time.time()
                self._events_seen += 1
                continue
            rid = str(obj.get("id"))
            waiter = self._waiters.get(rid)
            if waiter is not None:
                waiter.append(obj)

    # ---------------------------------------------------------------- 请求
    def request(self, action: str, timeout: float = 20.0, **fields) -> dict:
        with self._lock:
            if not self._alive():
                self.start()
            assert self._proc is not None and self._proc.stdin is not None
            self._seq += 1
            rid = str(self._seq)
            self._waiters[rid] = deque()
            payload = {"id": rid, "action": action, **fields}
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()

            deadline = time.monotonic() + timeout
            waiter = self._waiters[rid]
            try:
                while True:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        raise ConnectorError(f"{action} 超时（{timeout:.0f}s）")
                    if waiter:
                        obj = waiter.popleft()
                        if not obj.get("ok"):
                            raise ConnectorError(str(obj.get("error") or f"{action} 失败"))
                        return obj.get("result")
                    time.sleep(0.02)
            finally:
                self._waiters.pop(rid, None)

    def subscribe(self) -> dict:
        result = self.request("subscribe", timeout=10, eventProtocolVersion=1)
        self._subscribed = bool(result.get("subscribed"))
        return result

    # ---------------------------------------------------------------- 便捷
    def probe(self) -> dict:
        snap = self.request("probe", timeout=20)
        self._snapshot = snap
        self._snapshot_at = time.time()
        return snap

    def snapshot(self, max_age: float = 2.0) -> dict:
        if self._subscribed and time.time() - self._snapshot_at <= max_age:
            return self._snapshot
        try:
            return self.probe()
        except ConnectorError as exc:
            self.log.debug("probe 失败：%s", exc)
            return self._snapshot or {}

    def search(self, query: str) -> list[dict]:
        return self.request("search", timeout=25, query=query) or []

    def execute(self, command: str, track: dict | None = None) -> dict:
        fields = {"command": command}
        if track is not None:
            fields["track"] = track
        return self.request("execute", timeout=60, **fields)

    # ---------------------------------------------------------------- 状态
    def status(self) -> dict:
        return {
            "alive": self._alive(),
            "subscribed": self._subscribed,
            "events": self._events_seen,
            "snapshot_age": round(time.time() - self._snapshot_at, 1) if self._snapshot_at else None,
        }
