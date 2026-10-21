from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from src.api.schemas import Draft, Chat
from src.agents.supervisor import Supervisor
from src.core.deps import ActorScope
from src.core.exceptions import InvalidState, PermissionDenied
from src.main import app
from src.modules.aftersale.state_machine import ensure_transition


def test_health_and_openapi():
    client = TestClient(app)
    assert client.get("/health").json()["environment"] == "sandbox"
    assert "/v1/after-sales/{case_id}/submit" in client.get("/openapi.json").json()["paths"]
    assert client.get("/v1/orders").status_code == 401


def test_invalid_draft_is_unified_error():
    # Pydantic 校验可独立验证，不依赖数据库启动。
    with pytest.raises(ValidationError):
        Draft(order_item_id="not-uuid", case_type="auto_refund", reason="x")


@pytest.mark.parametrize("role", ["buyer", "staff", "ops", "knowledge_admin"])
def test_shop_scope_is_checked_for_every_role(role):
    with pytest.raises(PermissionDenied):
        ActorScope("user", "tenant", role, ["a"]).ensure_shop_access("b")


@pytest.mark.parametrize("source,target", [("DRAFT", "CREATED"), ("CREATED", "APPROVED"), ("UNKNOWN", "CREATING")])
def test_state_machine_prevents_unsafe_transitions(source, target):
    with pytest.raises(InvalidState):
        ensure_transition(source, target)


def runtime_for_test():
    from src.agents.tools import AgentRuntime
    payload = Chat(conversation_id=uuid4(), client_message_id="m1", message="吸顶灯", target_size="42", budget_minor=10000)
    return AgentRuntime(MagicMock(), ActorScope("buyer", "tenant", "buyer", ["a"]), payload, "trace")


async def test_agent_cannot_invoke_unregistered_write_tool():
    from src.agents.subagents import tool_request
    runtime = runtime_for_test()
    with pytest.raises(PermissionDenied):
        await runtime.invoke("shopping", tool_request("refund"))
    runtime.db.add.assert_not_called()


async def test_agent_role_checked_at_tool_boundary():
    from src.agents.subagents import tool_request
    runtime = runtime_for_test()
    with pytest.raises(PermissionDenied):
        await runtime.invoke("analytics", tool_request("generate_sql"))
    runtime.db.add.assert_not_called()


async def test_shopping_preserves_size_and_tightest_budget(monkeypatch):
    from src.agents.subagents import tool_request
    search = AsyncMock(return_value=[])
    monkeypatch.setattr("src.agents.tools.search_catalog", search)
    runtime = runtime_for_test()
    await runtime.invoke("shopping", tool_request("search_products"))
    assert search.call_args.kwargs["size"] == "42"
    runtime.handoff = {"category":"吸顶灯", "budget_minor":20000, "target_size":"42"}
    await runtime.invoke("shopping", tool_request("search_products", {"budget_minor":30000}))
    assert search.call_args.kwargs["budget_minor"] == 10000
