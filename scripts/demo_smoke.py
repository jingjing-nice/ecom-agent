"""真实服务冒烟：python -m scripts.demo_smoke --base-url http://localhost:8090。

只创建模拟售后草稿并取消，不创建真实交易。配置 Docker 环境后手动运行。
"""
import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from uuid import uuid4
import httpx


async def automatic_aftersale_check(call, token, orders, *, timeout=30):
    """显式开启时创建沙箱工单并检查 Worker 闭环；重复执行改用尚未使用的明细。"""
    used = set()
    offset = 0
    while True:
        page = await call('GET',f'/v1/after-sales?limit=100&offset={offset}',token)
        used.update(case['order_item_id'] for case in page['items']
            if case['status'] not in {'DRAFT','CANCELLED','REJECTED'})
        if len(page['items']) < 100:
            break
        offset += 100
    item = next((item for order in orders if order['status']=='paid'
        for item in order['items'] if item['id'] not in used), None)
    if item is None:
        raise RuntimeError('没有尚未申请售后的已支付订单明细；请准备未发货演示订单（新种子 DEMO-0002）')
    draft = await call('POST','/v1/after-sales/drafts',token,json={
        'order_item_id':item['id'],'case_type':'refund','reason':'自动退款链路冒烟测试'})
    path = f'/v1/after-sales/{draft["id"]}'
    payload = {'expected_version':draft['version'],'confirmed':True}
    key = {'Idempotency-Key':f'smoke-{draft["id"]}'}
    submitted = await call('POST',path+'/submit',token,json=payload,headers=key)
    replayed = await call('POST',path+'/submit',token,json=payload,headers=key)
    if submitted['id'] != replayed['id']:
        raise RuntimeError('幂等提交返回了不同工单')
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        record = await call('GET',path,token)
        if record['status']=='CREATED':
            if not record['external_case_id'] or (record.get('draft') or {}).get('review_mode') != 'automatic':
                raise RuntimeError('工单缺少外部单号或自动审核依据')
            return {'case_id':record['id'],'external_case_id':record['external_case_id']}
        if record['status'] in {'REJECTED','NEED_MORE_INFO','CANCELLED'}:
            raise RuntimeError(f"自动售后未通过：{record['status']}；{record.get('review_comment')}")
        await asyncio.sleep(0.5)
    raise RuntimeError(f'自动售后超时，请确认 Worker 已运行；case_id={draft["id"]}')


async def smoke(base_url, *, automatic_aftersale=False):
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        async def call(method, path, token=None, **kwargs):
            response = await client.request(method,path,headers={**({'Authorization':f'Bearer {token}'} if token else {}), **kwargs.pop('headers',{})},**kwargs)
            response.raise_for_status()
            return response.json()['data']
        health = await client.get('/health')
        health.raise_for_status()
        token = (await call('POST','/v1/auth/login',json={'email':'buyer@demo.local','password':'Demo123!'}))['access_token']
        orders = (await call('GET','/v1/orders',token))['items']
        item = next(i for o in orders if o['paid_at'] and o['status'] in {'paid','shipped','delivered','completed'} for i in o['items'])
        draft = await call('POST','/v1/after-sales/drafts',token,json={'order_item_id':item['id'],'case_type':'exchange','reason':'冒烟测试：尺寸不合适'})
        cancelled = await call('POST',f'/v1/after-sales/{draft["id"]}/cancel',token,json={'expected_version':draft['version'],'reason':'冒烟测试结束'})
        if cancelled['status'] != 'CANCELLED':
            raise RuntimeError('取消状态未持久化')
        stored = await call('GET',f'/v1/after-sales/{draft["id"]}',token)
        if stored['status'] != 'CANCELLED':
            raise RuntimeError('重查结果不一致')
        conv = await call('POST','/v1/conversations',token,json={})
        final = None
        event = None
        async with client.stream('POST','/v1/chat/stream',headers={'Authorization':f'Bearer {token}'},json={
            'conversation_id':conv['id'],'client_message_id':uuid4().hex,'message':'500 元以内的吸顶灯'}) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith('event: '):
                    event = line[7:]
                elif line.startswith('data: '):
                    payload = json.loads(line[6:])
                    if event == 'error':
                        raise RuntimeError(payload['message'])
                    if event == 'answer.completed':
                        final = payload
        if not final or not final.get('cards') or any(c['price_minor']>50000 or c['available_qty']<=0 for c in final['cards']):
            raise RuntimeError('导购未返回符合预算和库存约束的商品')
        history = await call('GET',f'/v1/conversations/{conv["id"]}/messages',token)
        if len(history['items']) != 2:
            raise RuntimeError('会话消息未完整保存')
        other = (await call('POST','/v1/auth/login',json={'email':'buyer2@demo.local','password':'Demo123!'}))['access_token']
        denied = await client.get(f'/v1/after-sales/{draft["id"]}',headers={'Authorization':f'Bearer {other}'})
        if denied.status_code != 404:
            raise RuntimeError('他人售后单访问未被隔离')
        automatic_result = await automatic_aftersale_check(call,token,orders) if automatic_aftersale else None
        return {'automatic_aftersale':automatic_result, 'checked_at':datetime.now(timezone.utc).isoformat(),'passed':True,
            'checks':['health','buyer_login','owned_orders','draft_cancel_persisted','shopping_budget_stock','conversation_history','foreign_case_denied']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url',default='http://localhost:8090')
    parser.add_argument('--automatic-aftersale',action='store_true',help='额外创建沙箱售后单，核验自动审批、幂等提交和 Worker 建单')
    args = parser.parse_args()
    print(json.dumps(asyncio.run(smoke(args.base_url,automatic_aftersale=args.automatic_aftersale)),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
