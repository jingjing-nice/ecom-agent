"""入口及工具共用的订单授权查询。未授权对象统一返回不存在。"""
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.deps import ActorScope
from src.core.exceptions import NotFound, PermissionDenied
from src.models.order import Order, OrderItem


def order_scope(actor: ActorScope):
    if actor.is_buyer:
        return (Order.tenant_id == actor.tenant_id, Order.buyer_id == actor.user_id)
    if actor.is_staff or actor.is_ops:
        return (Order.tenant_id == actor.tenant_id, Order.shop_id.in_(actor.shop_ids))
    raise PermissionDenied("该角色没有订单读取权限")


async def owned_order(db: AsyncSession, actor: ActorScope, order_id: UUID) -> Order:
    order = await db.scalar(select(Order).where(Order.id == order_id, *order_scope(actor)))
    if order is None:
        raise NotFound("订单不存在")
    return order


async def owned_item(db: AsyncSession, actor: ActorScope, item_id: UUID):
    row = (await db.execute(select(OrderItem, Order).join(Order, OrderItem.order_id == Order.id).where(
        OrderItem.id == item_id, OrderItem.tenant_id == actor.tenant_id, *order_scope(actor)
    ))).first()
    if row is None:
        raise NotFound("订单明细不存在")
    return row


def order_data(order: Order):
    return {"id": str(order.id), "order_no": order.order_no, "shop_id": order.shop_id,
            "status": order.status, "paid_amount_minor": order.paid_amount_minor,
            "currency": order.currency, "paid_at": order.paid_at,
            "items": [{"id": str(i.id), "sku_id": i.sku_id, "product_id": i.product_id,
                       "title": i.title_snapshot, "quantity": i.quantity,
                       "paid_amount_minor": i.paid_amount_minor, "specs": i.specs_snapshot}
                      for i in order.items if i.tenant_id == order.tenant_id]}
