"""数据库基础设施：业务库（读写）与分析库（只读）两套引擎。"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.core.config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


_settings = get_settings()

# 业务库引擎：应用读写使用
engine: AsyncEngine = create_async_engine(
    _settings.db_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=False,
)

SessionFactory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)

# 分析库引擎：NL2SQL 只读执行使用（低权限账号）
analytics_engine: AsyncEngine = create_async_engine(
    _settings.analytics_db_url,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
    echo=False,
)

AnalyticsSessionFactory = async_sessionmaker(
    analytics_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：业务库会话（请求级）。"""
    async with SessionFactory() as session:
        yield session


async def get_analytics_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：分析库只读会话。"""
    async with AnalyticsSessionFactory() as session:
        yield session


async def dispose_engines() -> None:
    """应用关闭时释放连接池。"""
    await engine.dispose()
    await analytics_engine.dispose()