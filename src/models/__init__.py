"""模型汇总入口：确保 Base.metadata 包含全部表（供 Alembic / create_all 使用）。"""
from __future__ import annotations

from src.core.database import Base  # noqa: F401
from src.models.aftersale import AfterSaleCase, EvidenceObject  # noqa: F401
from src.models.catalog import (  # noqa: F401
    InventorySnapshot,
    Product,
    ProductSKU,
    ProductSearchIndex,
    Review,
)
from src.models.conversation import Conversation, Message  # noqa: F401
from src.models.identity import Shop, User  # noqa: F401
from src.models.ops import AuditLog, Job, OutboxEvent, ToolCallRecord  # noqa: F401
from src.models.order import Order, OrderItem, RefundItem  # noqa: F401
from src.models.policy import PolicyChunk, PolicyDocument, PolicyVersion  # noqa: F401
from src.models.profile import BuyerProfile  # noqa: F401

__all__ = [
    "Base",
    "Shop",
    "User",
    "Product",
    "ProductSKU",
    "ProductSearchIndex",
    "InventorySnapshot",
    "Review",
    "Order",
    "OrderItem",
    "RefundItem",
    "AfterSaleCase",
    "EvidenceObject",
    "PolicyDocument",
    "PolicyVersion",
    "PolicyChunk",
    "Conversation",
    "Message",
    "Job",
    "OutboxEvent",
    "ToolCallRecord",
    "AuditLog",
    "BuyerProfile",
]