"""Redis 缓存：客户端单例、JSON 缓存助手、计数限流。

缓存键约定：所有 key 均包含租户标识（方案 §7）。
"""
from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from src.core.config import get_settings

_redis_client: aioredis.Redis | None = None


async def get_redis_client() -> aioredis.Redis:
    """返回 Redis 异步客户端单例。"""
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
    return _redis_client


async def close_redis_client() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


async def cache_get_json(key: str) -> Any | None:
    """读取 JSON 缓存；Redis 故障时静默降级为 None。"""
    try:
        client = await get_redis_client()
        raw = await client.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def cache_set_json(key: str, value: Any, ttl_seconds: int = 300) -> None:
    """写入 JSON 缓存；Redis 故障时静默忽略。"""
    try:
        client = await get_redis_client()
        await client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_seconds)
    except Exception:
        pass


async def acquire_slot(key: str, limit: int, ttl_seconds: int = 60) -> bool:
    """基于 INCR 的并发槽位限制（如 NL2SQL 每用户 2 个在途查询）。

    返回 False 表示超限。需要调用方在结束时 release_slot。
    Redis 故障时放行（可用性优先），由数据库侧超时兜底。
    """
    try:
        client = await get_redis_client()
        current = await client.incr(key)
        if current == 1:
            await client.expire(key, ttl_seconds)
        if current > limit:
            await client.decr(key)
            return False
        return True
    except Exception:
        return True


async def release_slot(key: str) -> None:
    """释放并发槽位。"""
    try:
        client = await get_redis_client()
        value = await client.decr(key)
        if value < 0:
            await client.set(key, 0)
    except Exception:
        pass