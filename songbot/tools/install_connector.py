"""下载并校验嗷呜点歌机的网易云连接器（一次性）。

    python tools/install_connector.py

它从官方目录拿 zip，核对 sha256 后解压到 tools/awoo-connector/extracted。
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

CATALOG = "https://app.enkianss.us/connectors/v2/catalog.json"
ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "tools" / "awoo-connector"


def main() -> int:
    print("读取连接器目录…")
    with urllib.request.urlopen(CATALOG, timeout=30) as resp:
        catalog = json.loads(resp.read().decode("utf-8"))

    entry = catalog["connectors"]["netease"]
    url = entry["package"]["downloadUrl"]
    expect = entry["package"]["sha256"]
    print(f"  版本 {entry['version']}（测试版本 {entry['testedPlayerVersion']}，"
          f"策略 {entry['playerVersionPolicy']}）")

    TARGET.mkdir(parents=True, exist_ok=True)
    zip_path = TARGET / "connector.zip"
    if not zip_path.is_file():
        print(f"下载 {url}")
        with urllib.request.urlopen(url, timeout=180) as resp, open(zip_path, "wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if digest != expect:
        print(f"❌ sha256 不匹配\n  实际 {digest}\n  期望 {expect}")
        return 1
    print(f"✅ sha256 校验通过（{zip_path.stat().st_size} 字节）")

    out = TARGET / "extracted"
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out)

    exe = out / "Awoo.Connector.Netease.exe"
    print(f"✅ 解压完成：{exe}")
    if not exe.is_file():
        print("❌ 没找到 Awoo.Connector.Netease.exe")
        return 1
    print("现在跑：.\\run.ps1 netease 起风了 --insert")
    return 0


if __name__ == "__main__":
    sys.exit(main())
