"""人工交接快照：只摘录已有记录，不让模型推断工单结论。"""
from datetime import datetime, timezone


def build_handoff_summary(messages, order, item_id):
    ordered = sorted(messages, key=lambda message: message.seq)
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "order_id": str(order.id),
        "order_item_id": str(item_id),
        "shop_id": order.shop_id,
        "recent_messages": [
            {"seq": message.seq, "role": message.role, "excerpt": message.content[:500]}
            for message in ordered[-6:]
        ],
        "last_message_seq": ordered[-1].seq if ordered else None,
        "note": "交接时的历史摘录，不代表政策核验、审批或用户确认；请结合原始消息和工单核实。",
    }
