"""用户确认后的自动售后审核，审核、审计与外发任务在同一事务提交。"""
from datetime import datetime, timezone

from sqlalchemy import or_, select

from src.core.deps import ActorScope
from src.models import AfterSaleCase, Order, PolicyVersion, RefundItem
from src.modules.aftersale.automation import AutomaticPolicy, evaluate_rule
from src.modules.aftersale.state_machine import transition_case
from src.services.vision import verify_evidence
from src.services.jobs import enqueue_job
from src.services.evidence import load_refund_evidence


def enqueue_review(db, case):
    return enqueue_job(db, tenant_id=case.tenant_id, job_type='automatic_case_review',
        payload={'case_id': str(case.id), 'owner_id': case.buyer_id, 'expected_version': case.version})


async def automatic_review(db, case, trace_id, *, create_job=True):
    from src.services.aftersale import audit, enqueue_creation
    from src.services.orders import owned_item

    if case.status not in {'SUBMITTED', 'REVIEWING'}:
        return
    actor = ActorScope(case.buyer_id, case.tenant_id, 'buyer', [case.shop_id])
    # 串行化同一订单的申请，避免两份工单同时通过。
    await db.scalar(select(Order).where(Order.id == case.order_id,
        Order.tenant_id == case.tenant_id).with_for_update())
    item, order = await owned_item(db, actor, case.order_item_id)
    now = datetime.now(timezone.utc)
    if case.status == 'SUBMITTED':
        transition_case(case, 'REVIEWING')
    policies = list((await db.scalars(select(PolicyVersion).where(
        PolicyVersion.tenant_id == case.tenant_id, PolicyVersion.scope == 'after_sale',
        PolicyVersion.status == 'published', PolicyVersion.effective_from <= now,
        or_(PolicyVersion.effective_to.is_(None), PolicyVersion.effective_to > now)).with_for_update())).all())
    status, reason = 'NEED_MORE_INFO', '当前没有唯一有效的结构化售后政策，自动处理暂不可用；请稍后重试'
    policy = policies[0] if len(policies) == 1 else None
    if policy and policy.automation_rules:
        try:
            rules = AutomaticPolicy.model_validate(policy.automation_rules).rules
        except ValueError:
            rules = []
        matched = [rule for rule in rules if rule.case_type == case.case_type]
        if len(matched) == 1:
            evidence_matches = False
            for evidence in await load_refund_evidence(db, actor, case.evidence_ids or []):
                fields = (evidence.extracted or {}).get('fields', [])
                result = verify_evidence({'fields': fields}, order_no=order.order_no, amount_minor=item.paid_amount_minor)
                # 必须同时精确匹配两字段；买家补录不作为原始凭证自动通过依据。
                checks = result['checks']
                if (checks and all(c['verification'] == 'matched' and c['source'] == 'ocr' for c in checks)
                    and {c['field_name'] for c in checks if c['verification'] == 'matched' and c['source'] == 'ocr'}
                    == {'order_no', 'amount_minor'}):
                    evidence_matches = True
                    break
            status, reason = evaluate_rule(matched[0], case, item, order, now, evidence_matches=evidence_matches)
        else:
            reason = '当前申请类型未配置唯一自动处理规则，请查询支持的售后类型后重新申请'
    # 已退款或另一份已确认申请占用同一明细时，禁止重复创建外部售后单。
    existing = await db.scalar(select(AfterSaleCase.id).where(AfterSaleCase.tenant_id == case.tenant_id,
        AfterSaleCase.order_item_id == case.order_item_id, AfterSaleCase.id != case.id,
        AfterSaleCase.status.in_(['APPROVED', 'CREATING', 'CREATED', 'UNKNOWN'])).limit(1))
    refunded = await db.scalar(select(RefundItem.id).where(RefundItem.tenant_id == case.tenant_id,
        RefundItem.order_item_id == case.order_item_id, RefundItem.status == 'completed').limit(1))
    if existing or refunded:
        status, reason = 'REJECTED', '该订单明细已有退款或正在执行的售后申请，不能重复申请'
    transition_case(case, status)
    case.reviewer_id, case.reviewed_at, case.review_comment = 'system:automatic-policy-v1', now, reason
    case.policy_version = policy.version if policy else None
    case.draft = {**(case.draft or {}), 'requires_review': False, 'review_mode': 'automatic',
        'automatic_decision': {'policy_id': str(policy.id) if policy else None,
            'rule_version': 'automatic-policy-v1', 'status': status, 'reason': reason}}
    if status == 'REJECTED':
        case.draft = {**case.draft, 'appeal_path': '修正申请条件后重新提交，系统将按最新订单事实和政策重新核验'}
    if status == 'APPROVED' and create_job:
        enqueue_creation(db, case)
    audit(db, ActorScope('system:automatic-policy-v1', case.tenant_id, 'staff', [case.shop_id]),
        'case.automatically_reviewed', case, trace_id)
