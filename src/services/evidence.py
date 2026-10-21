"""售后凭证批量读取；授权与原始 OCR 状态始终由服务端过滤。"""
from uuid import UUID

from sqlalchemy import select

from src.models import EvidenceObject


async def load_refund_evidence(db, actor, evidence_ids, *, lock=False):
    ids = list(dict.fromkeys(UUID(str(value)) for value in evidence_ids))
    if not ids:
        return []
    query = select(EvidenceObject).where(
        EvidenceObject.id.in_(ids), EvidenceObject.tenant_id == actor.tenant_id,
        EvidenceObject.owner_id == actor.user_id, EvidenceObject.kind == "refund_evidence",
        EvidenceObject.status == "extracted")
    if lock:
        query = query.order_by(EvidenceObject.id).with_for_update()
    return list((await db.scalars(query)).all())
