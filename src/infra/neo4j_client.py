"""Neo4j 客户端：异步驱动单例与健康检查。

图谱服务端只开放参数化查询模板，不允许模型生成任意 Cypher（方案 §13）。
"""
from __future__ import annotations

from neo4j import AsyncDriver, AsyncGraphDatabase

from src.core.config import get_settings

_driver: AsyncDriver | None = None


def get_neo4j_driver() -> AsyncDriver:
    """返回 Neo4j 异步驱动单例。"""
    global _driver
    if _driver is None:
        settings = get_settings()
        _driver = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            connection_timeout=3.0,
            connection_acquisition_timeout=3.0,
            max_transaction_retry_time=0.0,
        )
    return _driver


async def check_neo4j_health() -> None:
    """连通性检查，失败抛异常。"""
    driver = get_neo4j_driver()
    await driver.verify_connectivity()


async def close_neo4j_driver() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None