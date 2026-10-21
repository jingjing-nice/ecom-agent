from uuid import uuid4
from src.api.schemas import Chat
from src.services.chat_context import resolve_shopping_context


def request(message, **kwargs):
    return Chat(conversation_id=uuid4(), client_message_id='next',message=message,inherit_context=True,**kwargs)


def previous():
    return {'agent':'shopping','slot_sources':{'category':{'value':'吸顶灯'},'budget_minor':{'value':50000},'target_size':{'value':'42'}}}


def test_exchange_target_comes_from_explicit_message_not_original_size():
    payload = request('这笔订单的灯是41厘米，我想换成42厘米。如果缺货推荐其他吸顶灯。', order_item_id=uuid4())
    assert resolve_shopping_context(payload, None).target_size == '42'
    assert resolve_shopping_context(request('原来41厘米，想换大一号'), None).target_size is None
    assert resolve_shopping_context(request('换成42厘米或者换成43厘米'), None).target_size is None
    assert resolve_shopping_context(request('换成42厘米', target_size='44'), None).target_size == '44'
    assert resolve_shopping_context(payload.model_copy(update={'inherit_context': False}), None).target_size == '42'


def test_continuation_inherits_only_shopping_constraints():
    payload = resolve_shopping_context(request('还有别的推荐吗'),previous())
    assert payload.category == '吸顶灯' and payload.budget_minor == 50000 and payload.target_size == '42'
    assert payload.order_item_id is None and payload.image_id is None
    assert payload._inherited_slots == {'category','budget_minor','target_size'}
    assert '_inherited_slots' not in payload.model_dump()


def test_new_budget_size_and_category_override_history():
    payload = resolve_shopping_context(request('改成 300 元以内，43厘米'),previous())
    assert payload.budget_minor is None and payload.target_size == '43'
    payload = resolve_shopping_context(request('壁灯'),previous())
    assert payload.budget_minor is None and payload.target_size is None


def test_reset_and_aftersale_do_not_inherit_context():
    for payload in (request('退款'),request('吸顶灯',order_item_id=uuid4()),request('不限预算'),request('重新开始').model_copy(update={'inherit_context':False})):
        resolved = resolve_shopping_context(payload,previous())
        assert resolved.budget_minor is None


async def test_budget_change_is_routed_to_shopping_not_aftersale(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from src.agents.supervisor import Supervisor
    from src.core.config import Settings
    from src.core.deps import ActorScope
    monkeypatch.setattr('src.agents.tools.get_settings',lambda:Settings(AGENT_ROUTING_MODE='rules',_env_file=None))
    monkeypatch.setattr('src.agents.tools.search_catalog',AsyncMock(return_value=[]))
    answer = await Supervisor(MagicMock(),ActorScope('buyer','demo','buyer',['a']),'trace').run(request('换成 300 元以内的吸顶灯'))
    assert answer['agent'] == 'shopping'


def test_inherited_slots_are_audited_as_conversation_sources(monkeypatch):
    from unittest.mock import MagicMock
    from src.agents.tools import AgentRuntime
    from src.core.config import Settings
    from src.core.deps import ActorScope
    monkeypatch.setattr('src.agents.tools.get_settings',lambda:Settings(AGENT_ROUTING_MODE='rules',_env_file=None))
    resolved = resolve_shopping_context(request('还有别的吗'),previous())
    runtime = AgentRuntime(MagicMock(),ActorScope('buyer','demo','buyer',['a']),resolved,'trace')
    assert runtime.slot_sources()['budget_minor'] == {'value':50000,'source':'conversation'}
