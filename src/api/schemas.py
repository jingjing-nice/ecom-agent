from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from src.modules.aftersale.automation import AutomaticPolicy


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=72)


class Draft(Input):
    order_item_id: UUID
    case_type: Literal["refund", "exchange", "return_refund"]
    reason: str = Field(min_length=1, max_length=512)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=10)


class Submit(Input):
    expected_version: int = Field(ge=1)
    confirmed: bool


class Review(Input):
    expected_version: int = Field(ge=1)
    decision: Literal["REVIEWING", "NEED_MORE_INFO", "APPROVED", "REJECTED"]
    comment: str = Field(default="", max_length=2000)
    policy_version: str | None = Field(default=None, max_length=64)
    appeal_path: str = Field(default="", max_length=1000)


class AnalyticsQuery(Input):
    question: str = Field(min_length=1, max_length=2000)
    semantic_plan: dict | None = None


class Chat(Input):
    _inherited_slots: set[str] = PrivateAttr(default_factory=set)
    inherit_context: bool = False
    conversation_id: UUID
    client_message_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    order_item_id: UUID | None = None
    image_id: UUID | None = None
    category: str | None = Field(default=None, max_length=64)
    budget_minor: int | None = Field(default=None, ge=0)
    target_size: str | None = Field(default=None, max_length=32)


class Takeover(Input):
    active: bool


class ChatApproval(Input):
    conversation_id: UUID
    client_message_id: str = Field(min_length=1, max_length=64)
    decision: Literal["APPROVED", "REJECTED"]


class Presign(Input):
    file_name: str = Field(min_length=1, max_length=255)
    kind: Literal["product_image", "refund_evidence"]


class VerifyEvidence(Input):
    """凭证 OCR 与订单事实的精确核对请求；可选关联订单明细。"""

    order_item_id: UUID | None = None


class KnowledgeImport(Input):
    """知识文本导入：创建 draft 版本并异步构建索引（方案 §4）。"""

    doc_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    scope: Literal["after_sale", "payment", "logistics", "membership"]
    version: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=200_000)
    source: str = Field(default="运营导入", max_length=255)
    description: str = Field(default="", max_length=2000)
    automation_rules: AutomaticPolicy | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None


class ManualField(Input):
    """人工补录 OCR 字段：只允许证据字段契约内的字段名。"""

    field_name: Literal["order_no", "amount_minor", "date", "damage_description"]
    value: str | int
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_value(self):
        if isinstance(self.value, bool):
            raise ValueError("补录值类型不支持")
        if isinstance(self.value, str) and not self.value.strip():
            raise ValueError("补录值不能为空")
        if isinstance(self.value, int) and self.value < 0:
            raise ValueError("补录值不能为负数")
        return self


class CancelCase(Input):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=512)


class SupplementCase(Input):
    expected_version: int = Field(ge=1)
    comment: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=10)


class EscalateConversation(Input):
    order_item_id: UUID


class StaffReply(Input):
    content: str = Field(min_length=1, max_length=4000)
