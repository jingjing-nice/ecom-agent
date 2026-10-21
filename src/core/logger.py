"""日志组件：loguru 统一输出到控制台与文件。"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from src.core.config import get_settings

_configured = False


def setup_logger() -> None:
    """初始化日志（幂等）。控制台 + 按天轮转文件。"""
    global _configured
    if _configured:
        return

    settings = get_settings()
    logger.remove()

    # 控制台
    logger.add(
        sys.stdout,
        level=settings.LOG_LEVEL,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        ),
        enqueue=False,
    )

    # 文件（按天轮转，保留 14 天）
    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "app_{time:YYYY-MM-DD}.log",
        level=settings.LOG_LEVEL,
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        enqueue=False,
    )

    _configured = True


__all__ = ["logger", "setup_logger"]