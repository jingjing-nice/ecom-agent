"""强制 AST 校验、逐数据集限域、只读事务与有界并发。"""
from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from sqlalchemy import text

from src.core.config import get_settings
from src.core.database import analytics_engine
from src.core.deps import ActorScope
from src.core.exceptions import AppError, PermissionDenied, RateLimited
from src.nl2sql.validator import validate_sql


@dataclass
class ExecResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    duration_ms: int
    data_as_of: str | None = None


_active: Counter = Counter()


def scoped_sql(sql: str, actor: ActorScope, max_rows: int) -> tuple[str, dict]:
    if not actor.is_ops or not actor.shop_ids:
        raise PermissionDenied("分析查询需要运营角色及店铺授权")
    checked = validate_sql(sql, max_rows=max_rows)
    if not checked.ok:
        raise AppError(checked.reason)
    ast = sqlglot.parse_one(checked.normalized_sql, dialect="postgres")
    tables = []
    for scope in traverse_scope(ast):
        for _, source in scope.selected_sources.values():
            if isinstance(source, exp.Table):
                tables.append(source)
    for table in tables:
        name, alias = table.name, table.alias_or_name
        filtered = sqlglot.parse_one(
            f"SELECT * FROM {name} WHERE tenant_id = :scope_tenant "
            "AND shop_id IN (SELECT jsonb_array_elements_text(CAST(:scope_shops AS JSONB)))",
            dialect="postgres",
        )
        # 只转换服务端注入的绑定节点，避免替换用户字符串字面量中的同名文本。
        for placeholder in list(filtered.find_all(exp.Placeholder)):
            placeholder.replace(exp.Var(this=f":{placeholder.name}"))
        table.replace(exp.Subquery(this=filtered, alias=exp.TableAlias(this=exp.to_identifier(alias))))
    rendered = ast.sql(dialect="postgres")
    return rendered, {
        "scope_tenant": actor.tenant_id, "scope_shops": json.dumps(actor.shop_ids),
    }


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


async def _run(sql: str, actor: ActorScope, *, explain=False, max_rows=None):
    settings = get_settings()
    requested = settings.SQL_MAX_ROWS if max_rows is None else max_rows
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        raise AppError("结果上限必须是正整数")
    limit = min(requested, settings.SQL_MAX_ROWS)
    # 多读一行才能准确判断是否被服务端截断。
    query, params = scoped_sql(sql, actor, limit + 1)
    key = (actor.tenant_id, actor.user_id)
    if _active[key] >= settings.SQL_MAX_CONCURRENT_PER_USER:
        raise RateLimited("分析查询并发已达上限")
    _active[key] += 1
    try:
        async with analytics_engine.begin() as conn:
            await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            await conn.execute(text("SELECT set_config('statement_timeout', :timeout, true)"),
                               {"timeout": str(settings.SQL_STATEMENT_TIMEOUT_MS)})
            await conn.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": actor.tenant_id})
            data_as_of = None
            if await conn.scalar(text("SELECT to_regclass('public.analytics_sync_state')")):
                watermark = await conn.scalar(text("SELECT min(data_as_of) FROM analytics_sync_state WHERE tenant_id = :tenant "
                    "AND shop_id IN (SELECT jsonb_array_elements_text(CAST(:shops AS JSONB))) "
                    "HAVING count(DISTINCT shop_id) = :shop_count"),
                    {"tenant": actor.tenant_id, "shops": json.dumps(sorted(set(actor.shop_ids))),
                     "shop_count": len(set(actor.shop_ids))})
                data_as_of = watermark.isoformat() if watermark else None
            result = await conn.execute(text(("EXPLAIN " if explain else "") + query), params)
            return list(result.keys()), result.fetchmany(limit + 1), limit, data_as_of
    except AppError:
        raise
    except Exception as exc:
        raise AppError("分析查询失败，请检查指标与查询范围") from exc
    finally:
        _active[key] -= 1
        if not _active[key]:
            del _active[key]


async def execute_readonly(sql: str, *, actor: ActorScope, max_rows: int | None = None) -> ExecResult:
    start = time.perf_counter()
    columns, fetched, limit, data_as_of = await _run(sql, actor, max_rows=max_rows)
    rows = [{col: _json_safe(row._mapping[col]) for col in columns} for row in fetched[:limit]]
    return ExecResult(columns, rows, len(rows), len(fetched) > limit, int((time.perf_counter()-start)*1000), data_as_of)


async def explain_sql(sql: str, *, actor: ActorScope) -> list[dict]:
    _, rows, _, _ = await _run(sql, actor, explain=True)
    return [{"plan": row[0]} for row in rows]
