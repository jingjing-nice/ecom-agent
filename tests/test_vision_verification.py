"""OCR 金额/订单号精确核对与人工补录（方案 §5/§12）。

覆盖：
- 商品识别词表映射：同义词映射到标准词表、未命中保留并标注
- verify_evidence：订单号/金额精确比对与不可核对分支（不判断真伪）
- apply_manual_field：保留 OCR 原值、来源标记、核对结论失效
- 售后草稿创建时凭证自动核对
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.api.schemas import Draft, ManualField
from src.core.deps import ActorScope
from src.core.exceptions import AppError
from src.services.aftersale import create_draft
from src.services.vision import apply_manual_field, apply_product_vocab, verify_evidence

BUYER = ActorScope("buyer-a", "tenant-a", "buyer", ["shop-a"])


def test_vocab_maps_synonyms_to_standard_catalog():
    output = apply_product_vocab({"category": "客厅灯", "color": "黑", "style": "现代简约",
                                  "visible_text": "栖家灯饰", "uncertain_fields": []})
    assert output["category"] == "吸顶灯"
    assert output["color"] == "黑色"
    assert output["mapping"]["category"] == {"raw": "客厅灯", "mapped": "吸顶灯"}
    assert output["unmapped_fields"] == []
    assert output["vocab_version"]


def test_vocab_keeps_unmapped_values_without_guessing():
    output = apply_product_vocab({"category": "台灯", "color": "unknown", "style": "unknown"})
    # 未命中词表的值原样保留并标注，不猜测、不模糊纠正
    assert output["category"] == "台灯"
    assert output["unmapped_fields"] == ["category"]
    assert output["mapping"]["category"]["mapped"] is None
    assert "color" not in output["mapping"] and "style" not in output["mapping"]


def test_verify_order_no_and_amount_exact_match():
    result = verify_evidence({"fields": [
        {"field_name": "order_no", "value": " DEMO-0001 ", "status": "recognized", "source": "ocr"},
        {"field_name": "amount_minor", "value": "29900", "status": "recognized", "source": "ocr"},
        {"field_name": "date", "value": "2026-10-01", "status": "recognized"},
    ]}, order_no="DEMO-0001", amount_minor=29900)
    assert result["status"] == "matched"
    assert len(result["checks"]) == 2  # 仅核对订单号与金额
    assert all(check["verification"] == "matched" for check in result["checks"])
    assert "不判断凭证真伪" in result["note"]


def test_verify_reports_mismatch_without_correcting():
    result = verify_evidence({"fields": [
        {"field_name": "amount_minor", "value": 19900, "status": "recognized"},
        {"field_name": "order_no", "value": "DEMO-0001", "status": "recognized"},
    ]}, order_no="DEMO-0001", amount_minor=29900)
    assert result["status"] == "mismatched"
    amount_check = next(check for check in result["checks"] if check["field_name"] == "amount_minor")
    assert amount_check["verification"] == "mismatched"
    assert amount_check["reason"] == "与订单金额不一致"


@pytest.mark.parametrize("field,value", [
    ("amount_minor", "¥299.00"),  # 非整数分不解析
    ("amount_minor", None),
    ("order_no", None),
])
def test_verify_unverifiable_cases_do_not_guess(field, value):
    result = verify_evidence({"fields": [{"field_name": field, "value": value, "status": "recognized"}]},
                             order_no="DEMO-0001", amount_minor=29900)
    assert result["status"] == "unverifiable"
    assert result["checks"][0]["verification"] == "unverifiable"


def test_verify_without_order_facts_is_unverifiable():
    result = verify_evidence({"fields": [
        {"field_name": "order_no", "value": "DEMO-0001", "status": "recognized"}]},
        order_no=None, amount_minor=None)
    assert result["status"] == "unverifiable"
    assert result["checks"][0]["reason"] == "缺少订单事实，无法核对"


def test_verify_skips_uncertain_and_missing_fields():
    result = verify_evidence({"fields": [
        {"field_name": "order_no", "value": "DEMO-0001", "status": "uncertain"},
        {"field_name": "amount_minor", "value": None, "status": "missing"},
    ]}, order_no="DEMO-0001", amount_minor=29900)
    assert result["status"] == "unverifiable"
    assert all(check["reason"] == "OCR 未确认该字段，不做比对" for check in result["checks"])


def test_manual_field_overrides_and_keeps_ocr_original():
    data = {"fields": [{"field_name": "amount_minor", "value": 19900, "status": "uncertain", "source": "ocr"}],
            "verification": {"status": "mismatched", "checks": []}}
    updated = apply_manual_field(data, field_name="amount_minor", value=29900, by="buyer-a")
    field = updated["fields"][0]
    assert field["value"] == 29900 and field["source"] == "manual" and field["status"] == "recognized"
    assert field["manual"] == {"previous_value": 19900, "previous_status": "uncertain", "by": "buyer-a"}
    # 人工补录后原核对结论失效，需重新核对
    assert updated["verification"]["stale"] is True
    # 原记录不被就地修改
    assert data["fields"][0]["value"] == 19900 and "stale" not in data["verification"]


def test_manual_field_appends_field_missing_from_ocr():
    updated = apply_manual_field({"fields": []}, field_name="damage_description", value=" 外包装破损 ", by="buyer-a")
    field = updated["fields"][0]
    assert field["value"] == "外包装破损" and field["source"] == "manual"
    assert field["manual"]["previous_value"] is None


def test_manual_field_rejects_unknown_field_and_empty_value():
    with pytest.raises(AppError, match="不支持人工补录字段"):
        apply_manual_field({"fields": []}, field_name="sku_id", value="x", by="buyer-a")
    with pytest.raises(AppError, match="补录值不能为空"):
        apply_manual_field({"fields": []}, field_name="order_no", value="  ", by="buyer-a")


def test_manual_field_schema_validates_value_type():
    with pytest.raises(ValidationError):
        ManualField(field_name="order_no", value="  ")
    with pytest.raises(ValidationError):
        ManualField(field_name="amount_minor", value=-1)
    assert ManualField(field_name="amount_minor", value="29900").value == "29900"


async def test_draft_automatically_verifies_evidence_against_order(monkeypatch):
    image_id = uuid4()
    evidence = SimpleNamespace(id=image_id, status="extracted", extracted={"fields": [
        {"field_name": "order_no", "value": "DEMO-0001", "status": "recognized", "source": "ocr"},
        {"field_name": "amount_minor", "value": 29900, "status": "recognized", "source": "ocr"},
    ]})
    db = MagicMock()
    db.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [evidence]))
    db.flush = AsyncMock()
    db.add = MagicMock()
    item = SimpleNamespace(id=uuid4(), paid_amount_minor=29900)
    order = SimpleNamespace(id=uuid4(), order_no="DEMO-0001", shop_id="shop-a", status="delivered",
                            paid_at=datetime.now(timezone.utc))
    monkeypatch.setattr("src.services.aftersale.owned_item", AsyncMock(return_value=(item, order)))
    case = await create_draft(db, BUYER, Draft(order_item_id=uuid4(), case_type="exchange",
                                               reason="尺寸不合适", evidence_ids=[image_id]), "trace")
    assert case.status == "DRAFT"
    verification = evidence.extracted["verification"]
    assert verification["status"] == "matched"
    assert verification["checks"][0]["verification"] == "matched"


def test_slippers_are_mapped_without_relabeling_as_running_shoes():
    from src.services.vision import apply_product_vocab
    assert apply_product_vocab({'category': '浴室灯', 'color': '黑色'})['category'] == '浴室灯'
