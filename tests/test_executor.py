import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.deps import ActorScope
from src.nl2sql import executor

ACTOR = ActorScope("ops-user", "tenant-test", "ops", ["shop-a"])


@pytest.fixture
def connection(monkeypatch):
    conn = SimpleNamespace(execute=AsyncMock(), scalar=AsyncMock(return_value=None))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=conn)
    context.__aexit__ = AsyncMock(return_value=False)
    engine = MagicMock()
    engine.begin.return_value = context
    monkeypatch.setattr(executor, "analytics_engine", engine)
    monkeypatch.setattr(executor, "get_settings", lambda: SimpleNamespace(
        SQL_MAX_ROWS=2, SQL_STATEMENT_TIMEOUT_MS=5000, SQL_MAX_CONCURRENT_PER_USER=1))
    return conn


async def test_fetches_one_extra_row_and_marks_truncation(connection):
    result = MagicMock()
    result.keys.return_value = ["sku_id"]
    result.fetchmany.return_value = [SimpleNamespace(_mapping={"sku_id":str(i)}) for i in range(3)]
    connection.execute.side_effect = [None, None, None, result]
    output = await executor.execute_readonly("SELECT sku_id FROM dim_sku_inventory", actor=ACTOR)
    assert output.truncated and output.row_count == 2
    assert output.rows == [{"sku_id":"0"}, {"sku_id":"1"}]
    statement, params = connection.execute.call_args.args
    assert "LIMIT 3" in str(statement)
    assert params["scope_tenant"] == ACTOR.tenant_id
    assert (ACTOR.tenant_id, ACTOR.user_id) not in executor._active


async def test_cancelled_query_releases_concurrency_slot(connection):
    connection.execute.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await executor.execute_readonly("SELECT sku_id FROM dim_sku_inventory", actor=ACTOR)
    assert (ACTOR.tenant_id, ACTOR.user_id) not in executor._active


@pytest.mark.parametrize("limit", [0, -2, True])
async def test_invalid_result_limits_do_not_open_connection(connection, limit):
    from src.core.exceptions import AppError
    with pytest.raises(AppError):
        await executor.execute_readonly("SELECT sku_id FROM dim_sku_inventory", actor=ACTOR, max_rows=limit)
    connection.execute.assert_not_awaited()


async def test_result_includes_authorized_sync_watermark(connection):
    from datetime import datetime, timezone
    watermark = datetime(2026, 10, 18, tzinfo=timezone.utc)
    connection.scalar.side_effect = ['analytics_sync_state', watermark]
    result = MagicMock()
    result.keys.return_value = ['sku_id']
    result.fetchmany.return_value = []
    connection.execute.side_effect = [None, None, None, result]
    output = await executor.execute_readonly('SELECT sku_id FROM dim_sku_inventory', actor=ACTOR)
    assert output.data_as_of == watermark.isoformat()
    statement, params = connection.scalar.call_args.args
    assert params['tenant'] == ACTOR.tenant_id and params['shop_count'] == 1
    assert 'HAVING count(DISTINCT shop_id)' in str(statement)
