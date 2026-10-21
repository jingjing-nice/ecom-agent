"""已发布政策的确定性自动审核契约，不从自然语言推断执行权限。"""
from datetime import timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AutomaticRule(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    case_type: Literal['refund', 'return_refund']
    order_statuses: list[Literal['paid', 'shipped', 'delivered', 'completed']] = Field(min_length=1)
    max_days_since_payment: int = Field(ge=1, le=365)
    max_amount_minor: int = Field(ge=1)
    require_evidence: bool = True


class AutomaticPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    rules: list[AutomaticRule] = Field(min_length=1, max_length=2)

    @model_validator(mode='after')
    def unique_case_types(self):
        if len({rule.case_type for rule in self.rules}) != len(self.rules):
            raise ValueError('每种售后类型只能配置一条规则')
        return self



def evaluate_rule(rule, case, item, order, now, *, evidence_matches):
    paid = order.paid_at
    if paid and paid.tzinfo is None:
        paid = paid.replace(tzinfo=timezone.utc)
    if order.currency != 'CNY':
        return 'REJECTED', '当前自动规则仅支持人民币订单'
    if not paid or paid > now or order.status not in rule.order_statuses:
        return 'REJECTED', '订单支付时间或当前状态不符合已发布的自动售后规则'
    # 退款不退货只支持未发货；发货后的退款走退货退款流程。
    if case.case_type == 'refund' and (order.status != 'paid' or order.shipped_at or order.delivered_at):
        return 'REJECTED', '订单已发货，请选择退货退款'
    if now > paid + timedelta(days=rule.max_days_since_payment):
        return 'REJECTED', '申请已超出政策规定的售后期限'
    if item.quantity <= 0 or item.paid_amount_minor <= 0 or item.paid_amount_minor > rule.max_amount_minor:
        return 'REJECTED', '订单明细金额或数量不符合自动售后规则'
    if rule.require_evidence and not evidence_matches:
        return 'NEED_MORE_INFO', '请上传能清晰识别订单号和实付金额的原始凭证，系统将重新核验'
    return 'APPROVED', '订单状态、期限、金额及所需凭证符合已发布规则，自动审核通过'
