# -*- coding: utf-8 -*-
"""机器码生成（现状文档 §5.7，与现状实现算法一致）。

算法：``SHA-256(MachineGuid | 系统盘卷序列号 | CPU_ID)`` 取十六进制摘要前 32 位（小写）。

- ``MachineGuid``：注册表 ``HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid``；
- 系统盘卷序列号：``GetVolumeInformation`` 取 ``%SystemDrive%``（缺省 ``C:``）；
- ``CPU_ID``：``ProcessorId``，先 ``wmic``，失败回退 PowerShell ``Get-CimInstance``。

任一因子获取失败 → 抛 ``RuntimeError``（调用方负责友好提示，禁止静默降级）。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import winreg

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


def _machine_guid() -> str:
    """读取注册表 MachineGuid。"""
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, winreg.KEY_READ
    ) as key:
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
    guid = str(value).strip()
    if not guid:
        raise RuntimeError("MachineGuid 为空")
    return guid


def _volume_serial() -> str:
    """系统盘卷序列号（GetVolumeInformation，格式化为 8 位大写十六进制）。"""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    system_drive = os.environ.get("SystemDrive", "C:")
    root = system_drive.rstrip("/\\") + "\\"
    volume_serial = ctypes.c_ulong()
    ok = kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root),
        None,
        0,
        ctypes.byref(volume_serial),
        None,
        None,
        None,
        0,
    )
    if not ok:
        raise RuntimeError(f"GetVolumeInformation({root}) 失败: {ctypes.get_last_error()}")
    return f"{volume_serial.value:08X}"


_CPUID_POWERSHELL = (
    "(Get-CimInstance Win32_Processor | Select-Object -First 1).ProcessorId"
)


def _cpu_id() -> str:
    """CPU ProcessorId：wmic 优先，PowerShell Get-CimInstance 回退。"""
    for cmd in (
        ["wmic", "cpu", "get", "ProcessorId", "/value"],
        ["powershell", "-NoProfile", "-Command", _CPUID_POWERSHELL],
    ):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            # wmic /value 形态: ProcessorId=XXXX；PowerShell 直接输出 XXXX
            if "=" in line:
                line = line.split("=", 1)[1].strip()
            if re.fullmatch(r"[0-9A-Fa-f]{8,32}", line):
                return line.upper()
    raise RuntimeError("无法获取 CPU ProcessorId（wmic 与 PowerShell 均失败）")


def get_machine_code() -> str:
    """生成 32 位机器码（小写十六进制）。任一因子失败抛 RuntimeError。"""
    raw = "|".join([_machine_guid(), _volume_serial(), _cpu_id()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def is_valid_machine_code(code: str) -> bool:
    """校验机器码格式（32 位十六进制，服务端 ^[0-9a-fA-F]{32}$ 归一前的形态）。"""
    return bool(_HEX32_RE.match((code or "").strip().lower()))
