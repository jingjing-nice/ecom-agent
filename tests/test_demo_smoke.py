"""在线冒烟装置验证：错误状态不能被误报为链路成功。"""
from unittest.mock import AsyncMock
import pytest

from scripts.demo_smoke import automatic_aftersale_check

ORDERS = [{'status':'paid','items':[{'id':'unused'}]}]


async def test_automatic_smoke_checks_submit_replay_and_created_result():
    call = AsyncMock(side_effect=[{'items':[]},{'id':'case','version':1},{'id':'case'},
        {'id':'case'},{'id':'case','status':'CREATED','external_case_id':'SANDBOX-case',
            'draft':{'review_mode':'automatic'}}])
    result = await automatic_aftersale_check(call,'token',ORDERS)
    assert result['external_case_id'] == 'SANDBOX-case'
    first, replay = call.call_args_list[2:4]
    assert first == replay and first.kwargs['headers']['Idempotency-Key']


async def test_automatic_smoke_rejects_failed_review():
    call = AsyncMock(side_effect=[{'items':[]},{'id':'case','version':1},{'id':'case'},
        {'id':'case'},{'status':'NEED_MORE_INFO','review_comment':'缺少政策'}])
    with pytest.raises(RuntimeError,match='缺少政策'):
        await automatic_aftersale_check(call,'token',ORDERS)


async def test_automatic_smoke_does_not_reuse_executing_item():
    call = AsyncMock(return_value={'items':[{'order_item_id':'unused','status':'UNKNOWN'}]})
    with pytest.raises(RuntimeError,match='没有尚未申请'):
        await automatic_aftersale_check(call,'token',ORDERS)
    assert call.await_count == 1
