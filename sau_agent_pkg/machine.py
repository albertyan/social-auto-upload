"""
sau_agent_pkg.machine
~~~~~~~~~~~~~~~~~~~~~
机器码采集模块。

机器码 = SHA-256(MachineGuid + 系统盘卷序列号 + CPU ID) 的前 32 位。
三者均不随应用重装变化，重装系统会改变（符合"换机/重装需重新绑定"预期）。
"""
from __future__ import annotations

import ctypes
import hashlib
import subprocess
import winreg
from ctypes import wintypes
from typing import Optional


def _get_machine_guid() -> str:
    """
    读取 Windows MachineGuid（注册表）。
    路径：HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid
    """
    key_path = r"SOFTWARE\Microsoft\Cryptography"
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
        guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        return str(guid)
    except OSError:
        return ""


def _get_volume_serial(drive: str = "C:\\") -> int:
    """
    获取系统盘卷序列号（GetVolumeInformationW）。
    """
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    serial_number = wintypes.DWORD()
    # GetVolumeInformationW(
    #   lpRootPathName, lpVolumeNameBuffer, nVolumeNameSize,
    #   lpVolumeSerialNumber, lpMaximumComponentLength, lpFileSystemFlags,
    #   lpFileSystemNameBuffer, nFileSystemNameSize
    # )
    result = kernel32.GetVolumeInformationW(
        drive,
        None, 0,
        ctypes.byref(serial_number),
        None, None,
        None, 0,
    )
    if result:
        return serial_number.value
    return 0


def _get_cpu_id_via_wmic() -> str:
    """通过 wmic 获取 CPU ID（Win11 新版本已移除 wmic，可能失败）。"""
    try:
        output = subprocess.check_output(
            ["wmic", "cpu", "get", "ProcessorId"],
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        # 输出格式：
        # ProcessorId
        # BFEBFBFF000906A2
        lines = output.decode("utf-8", errors="ignore").strip().split()
        # 跳过标题行，取第一个非空值
        for line in lines[1:]:
            line = line.strip()
            if line:
                return line
    except (subprocess.SubprocessError, OSError):
        pass
    return ""


def _get_cpu_id_via_powershell() -> str:
    """通过 PowerShell（Get-CimInstance）获取 CPU ID，作为 wmic 的回退方案。"""
    try:
        output = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Processor | Select-Object -ExpandProperty ProcessorId",
            ],
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        # 输出每行一个 ProcessorId（Windows 换行为 \r\n，需 strip \r）
        for line in output.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line:
                return line
    except (subprocess.SubprocessError, OSError):
        pass
    return ""


def _get_cpu_id() -> str:
    """
    获取 CPU ID（ProcessorId）。
    优先使用 wmic 命令（避免额外 WMI 库依赖），失败后回退 PowerShell。
    """
    cpu_id = _get_cpu_id_via_wmic()
    if cpu_id:
        return cpu_id
    return _get_cpu_id_via_powershell()


def get_machine_code() -> str:
    """
    采集机器指纹并返回 SHA-256 哈希的前 32 位。

    组合三个硬件标识：
    - MachineGuid（注册表）
    - 系统盘卷序列号（GetVolumeInformationW）
    - CPU ID（wmic cpu get ProcessorId）

    Returns:
        32 位十六进制字符串作为机器码。

    Raises:
        RuntimeError: 任一指纹组件采集失败（避免静默拼空串导致机器码不稳定）。
    """
    guid = _get_machine_guid()
    if not guid:
        raise RuntimeError("机器指纹采集失败：MachineGuid 组件不可用")

    serial = _get_volume_serial()
    if serial == 0:
        raise RuntimeError("机器指纹采集失败：系统盘卷序列号组件不可用")

    cpu_id = _get_cpu_id()
    if not cpu_id:
        raise RuntimeError("机器指纹采集失败：CPU ID 组件不可用")

    # 组合三个标识，用 | 分隔
    fingerprint = f"{guid}|{serial}|{cpu_id}"
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32]


if __name__ == "__main__":
    # 方便调试：直接运行打印机器码
    print(f"Machine Code: {get_machine_code()}")
