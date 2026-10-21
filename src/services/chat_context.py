"""多轮导购只继承服务端上一轮核验的筛选槽位，不继承订单、图片或业务动作。"""
import re
from decimal import Decimal

CATEGORIES = ("吸顶灯", "壁灯", "吊灯", "浴室灯", "感应灯", "筒灯")
ALIASES = {"客厅灯": "吸顶灯", "床头灯": "壁灯", "装饰吊灯": "吊灯", "卫生间灯": "浴室灯", "玄关灯": "感应灯"}


def message_category(message):
    return next((c for c in CATEGORIES if c in message), None) or next(
        (value for word, value in ALIASES.items() if word in message), None)


def message_budget(message):
    match = re.search(r"(?<![\d.,-])(\d+(?:\.\d{1,2})?)\s*元", message)
    return int(Decimal(match.group(1)) * 100) if match else None


def resolve_shopping_context(payload, previous):
    # 只提取明确的换货目标，不把“原来41厘米”误当成目标，也不继承旧订单尺寸。
    if not payload.target_size:
        sizes = set(re.findall(
            r"(?:换成|换到|换为|换\s*成|目标(?:尺寸)?[为是：:]?|需要|想要)\s*(\d{2}(?:\.5)?)\s*(?:厘米|cm)",
            payload.message))
        if len(sizes) == 1:
            payload = payload.model_copy(update={"target_size": sizes.pop()})
    if not payload.inherit_context or not previous or previous.get("agent") != "shopping":
        return payload
    if payload.order_item_id or any(word in payload.message for word in ("换货", "退款", "退货", "售后")):
        return payload
    slots = previous.get("slot_sources") or {}
    updates = {}
    category = payload.category or message_category(payload.message)
    old_category = slots.get("category", {}).get("value")
    # 切换品类时不沿用上一品类的尺寸和预算。
    if category and old_category and category != old_category:
        return payload
    for name in ("category", "budget_minor", "target_size"):
        if getattr(payload, name) is not None:
            continue
        if name == "category" and category:
            continue
        if name == "budget_minor" and (message_budget(payload.message) is not None or "不限预算" in payload.message):
            continue
        if name == "target_size":
            match = re.search(r"(?<!\d)(\d{2}(?:\.5)?)\s*(?:厘米|cm)", payload.message)
            if match:
                updates[name] = match.group(1)
                continue
        value = slots.get(name, {}).get("value")
        if value is not None:
            updates[name] = value
    resolved = payload.model_copy(update=updates)
    resolved._inherited_slots = set(updates)
    return resolved
