# -*- coding: utf-8 -*-
"""安装包哈希发布闭环（设计文档 §8.5，实施计划 S8）。

构建后自动计算安装包 **64 位小写 SHA-256**，输出发布单——运营只搬运、
不手填哈希（§8.5），杜绝誊抄错误：

1. 本脚本产出发布单（版本号 / 安装包路径 / sha256 / 构建时间 / 构建机 /
   ``download_url`` 占位）；
2. 运营按发布单上传安装包到 HTTPS 白名单存储；
3. 管理端 ``PUT /sau/upgrade-config`` 按发布单填三字段（版本号 /
   download_url / sha256）→ 保存即广播 ``upgrade_notice``。

用法::

    python -m sau_wrap.packaging.hash_release <安装包路径>
    # 缺省自动找 installer/Output/sau-{version}.exe

产物：安装包同目录 ``release-manifest.json`` + 控制台打印发布单。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
OUTPUT_DIR: Path = _HERE / "installer" / "Output"


def sha256_of(path: Path) -> str:
    """64 位小写 hex SHA-256（1MB 分块，大安装包不驻留内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def resolve_version() -> str:
    env_v = os.environ.get("SAU_VERSION", "").strip()
    if env_v:
        return env_v
    from sau_wrap.version import APP_VERSION  # noqa: PLC0415

    return APP_VERSION


def find_installer(version: str) -> Path | None:
    cand = OUTPUT_DIR / f"sau-{version}.exe"
    if cand.is_file():
        return cand
    if OUTPUT_DIR.is_dir():
        hits = sorted(OUTPUT_DIR.glob("sau-*.exe"),
                      key=lambda p: p.stat().st_mtime)  # 按修改时间取最新
        if hits:                                        # （字典序会误判 10.x < 9.x）
            return hits[-1]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装包哈希发布闭环（§8.5）")
    parser.add_argument("installer", nargs="?", default=None,
                        help="安装包路径（缺省自动查找 installer/Output/）")
    args = parser.parse_args(argv)

    version = resolve_version()
    if args.installer:
        installer = Path(args.installer).resolve()
    else:
        installer = find_installer(version)
    if installer is None or not installer.is_file():
        print(f"[hash_release] 未找到安装包（期望 {OUTPUT_DIR}/sau-{version}.exe）；"
              "请先用 ISCC 编译 sau.iss", file=sys.stderr)
        return 1

    digest = sha256_of(installer)
    manifest = {
        "version": version,
        "installer_file": installer.name,
        "installer_path": str(installer),
        "size_bytes": installer.stat().st_size,
        "sha256": digest,
        "build_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "build_host": socket.gethostname(),
        "build_os": platform.platform(),
        # 占位：运营上传到白名单存储后替换为真实地址（客户端强制
        # https + 域名白名单校验，见 updater.validate_notice）
        "download_url": "https://<白名单域名>/sau/" + installer.name,
    }
    out = installer.with_name("release-manifest.json")
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("==== 发布单（§8.5）====")
    for k, v in manifest.items():
        print(f"  {k}: {v}")
    print(f"\n发布单已写入：{out}")
    print("运营流程：按发布单上传安装包 → 管理端 PUT /sau/upgrade-config "
          "填三字段（版本号/download_url/sha256）→ 保存即广播。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
