import os
import sys
from pathlib import Path
from loguru import logger

from conf import BASE_DIR


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# 注册所有业务日志的相对路径（用于启动时统一探测“目录+既有文件”是否都可写）
_BUSINESS_LOG_RELPATHS: tuple[str, ...] = (
    "logs/douyin.log",
    "logs/tencent.log",
    "logs/xhs.log",
    "logs/tiktok.log",
    "logs/bilibili.log",
    "logs/kuaishou.log",
    "logs/baijiahao.log",
    "logs/xiaohongshu.log",
    "logs/youtube.log",
)


def _iter_base_candidates(preferred: Path) -> list[Path]:
    """构造候选日志根目录列表，按优先级排序。"""
    candidates: list[Path] = [preferred]
    # 候选 2：项目源根目录（开发模式），通过 conf.py 的 __file__ 推导
    try:
        import conf as _conf  # type: ignore[import-not-found]
        src_root = Path(_conf.__file__).parent.resolve()
        if src_root.resolve() != preferred.resolve():
            candidates.append(src_root)
    except Exception:
        pass
    # 候选 3：用户本地 AppData（Windows 安装模式下非管理员的可写路径）
    local_appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "SAU")
    # 候选 4：用户临时目录（兜底）
    candidates.append(Path(os.environ.get("TEMP", "/tmp")) / "SAU")
    return candidates


def _try_fix_existing_log_file(path: Path) -> bool:
    """
    针对「目录可写、但既有日志文件自身 ACL 不可写」的场景尝试自愈。
    常见根因：该文件先前由 SYSTEM/管理员身份创建，ACL 未继承目录对 Users 的写权限。

    策略：
    1. 先尝试「追加写」探测（append 一个空字节不会破坏日志内容）；
    2. 若失败，尝试删除原文件（若目录有写权限通常可删），删除成功即可重新创建
       （新建文件会继承目录 ACL，从而 Users 可写）。
    返回 True 代表该路径可用，False 代表仍不可写。
    """
    if not path.exists():
        # 文件不存在，只要父目录可写就能创建，无需此函数介入
        return True
    # 1. 探测能否追加写入
    try:
        with path.open("ab"):
            pass
        return True
    except (OSError, PermissionError):
        pass
    # 2. 无法写入：尝试删除以便重新创建（创建时会继承父目录 ACL）
    try:
        path.unlink()
        sys.stderr.write(
            f"[utils/log] 已移除不可写的旧日志文件 {path}，"
            "后续将以继承目录 ACL 的新文件替换\n"
        )
        sys.stderr.flush()
        return True
    except (OSError, PermissionError):
        return False


def _probe_log_dir_usable(base: Path) -> tuple[bool, Exception | None]:
    """
    探测 base/logs 是否满足「所有已知业务日志文件都可打开写入」的条件。
    只探测、不注册 loguru handler，避免失败时产生脏状态。
    """
    logs_dir = base / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as e:
        return False, e

    # A) 探针文件：确认目录本身的创建/删除能力
    try:
        probe = logs_dir / f".write_test_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except (OSError, PermissionError) as e:
        return False, e

    # B) 逐个检查既有日志文件：目录可写 ≠ 已有文件可写（ACL 继承缺失场景）
    last_err: Exception | None = None
    all_ok = True
    for rel in _BUSINESS_LOG_RELPATHS:
        target = base / rel
        if not _try_fix_existing_log_file(target):
            last_err = PermissionError(f"cannot fix file ACL: {target}")
            all_ok = False
            # 不立即 break：让用户看到哪些文件有问题
            sys.stderr.write(
                f"[utils/log] 候选目录 {base} 下的日志文件 {target} 仍不可写，跳过该目录\n"
            )
            sys.stderr.flush()
    return all_ok, last_err


def _resolve_writable_base_dir(preferred: Path) -> Path | None:
    """
    解析实际可写的日志根目录。
    优先使用 preferred（通常是 conf.BASE_DIR），依次回退到项目源码目录、
    LOCALAPPDATA\\SAU、%TEMP%\\SAU，避免 PermissionError 导致进程无法启动。

    为什么不直接报错：日志模块 import 时就会创建 handler，失败会让整个业务进程
    在启动阶段直接崩溃；开发模式和非管理员运行安装版时都应保证可用性。
    """
    candidates = _iter_base_candidates(preferred)
    chosen: Path | None = None
    last_err: Exception | None = None
    for base in candidates:
        ok, err = _probe_log_dir_usable(base)
        if ok:
            chosen = base
            break
        last_err = err

    if chosen is None:
        sys.stderr.write(
            "[utils/log] 所有候选日志目录均不可用，日志将只输出到控制台。"
            f" 最后一个错误: {last_err}\n"
        )
        sys.stderr.flush()
        return None

    if chosen.resolve() != preferred.resolve():
        sys.stderr.write(
            f"[utils/log] 首选日志根目录 {preferred} 不可写，"
            f"已回退到 {chosen}\n"
        )
        sys.stderr.flush()
    return chosen


_RESOLVED_BASE: Path | None = _resolve_writable_base_dir(BASE_DIR)


def log_formatter(record: dict) -> str:
    """
    Formatter for log records.
    :param dict record: Log object containing log metadata & message.
    :returns: str
    """
    colors = {
        "TRACE": "#cfe2f3",
        "INFO": "#9cbfdd",
        "DEBUG": "#8598ea",
        "WARNING": "#dcad5a",
        "SUCCESS": "#3dd08d",
        "ERROR": "#ae2c2c"
    }
    color = colors.get(record["level"].name, "#b3cfe7")
    return f"<fg #70acde>{{time:YYYY-MM-DD HH:mm:ss}}</fg #70acde> | <fg {color}>{{level}}</fg {color}>: <light-white>{{message}}</light-white>\n"


def create_logger(log_name: str, file_path: str):
    """
    Create custom logger for different business modules.
    :param str log_name: name of log
    :param str file_path: Optional path to log file (relative to BASE_DIR)
    :returns: Configured logger
    """
    def filter_record(record):
        return record["extra"].get("business_name") == log_name

    base = _RESOLVED_BASE
    if base is not None:
        try:
            target = Path(base) / file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            # 启动前再做一次文件级自愈（处理运行时动态新增的 logger）
            _try_fix_existing_log_file(target)
            logger.add(
                target,
                filter=filter_record,
                level="INFO",
                rotation="10 MB",
                retention="10 days",
                backtrace=True,
                diagnose=True,
                # loguru 内部写文件失败时静默降级，不让业务线程崩溃
                catch=True,
            )
        except (OSError, PermissionError) as e:
            sys.stderr.write(
                f"[utils/log] 无法为 {log_name} 注册文件日志 {base / file_path}: {e}，"
                "将只输出到控制台\n"
            )
            sys.stderr.flush()
    return logger.bind(business_name=log_name)


# Remove all existing handlers
logger.remove()
# Add a standard console handler
logger.add(sys.stdout, colorize=True, format=log_formatter)

douyin_logger = create_logger('douyin', 'logs/douyin.log')
tencent_logger = create_logger('tencent', 'logs/tencent.log')
xhs_logger = create_logger('xhs', 'logs/xhs.log')
tiktok_logger = create_logger('tiktok', 'logs/tiktok.log')
bilibili_logger = create_logger('bilibili', 'logs/bilibili.log')
kuaishou_logger = create_logger('kuaishou', 'logs/kuaishou.log')
baijiahao_logger = create_logger('baijiahao', 'logs/baijiahao.log')
xiaohongshu_logger = create_logger('xiaohongshu', 'logs/xiaohongshu.log')
youtube_logger = create_logger('youtube', 'logs/youtube.log')
