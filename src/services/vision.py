"""不可信图片解码、去元数据和视觉结构化输出验证。"""
import copy
import io
import warnings
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from PIL import Image, UnidentifiedImageError
from src.core.exceptions import AppError
from src.infra.storage import MAX_FILE_SIZE_BYTES

MAX_PIXELS = 20_000_000


class EvidenceField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_name: Literal["order_no", "amount_minor", "date", "damage_description"]
    value: str | int | None = None
    # bbox 属非核心展示字段，宽松接收（任意列表）；归一化校验在 _clean_evidence_bbox 完成，
    # 非法坐标（如像素值）丢弃置 None，不阻断字段取值提取。
    bbox: list[Any] | None = Field(default=None, max_length=4)
    status: Literal["recognized", "uncertain", "missing"]

    @model_validator(mode="after")
    def validate_value(self):
        if self.status == "missing":
            self.value = None
        if self.status == "recognized" and self.value is None:
            raise ValueError("已识别字段必须有值")
        return self


class EvidenceExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[EvidenceField] = Field(max_length=4)


class ProductRecognition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = "unknown"
    color: str = "unknown"
    style: str = "unknown"
    visible_text: str = ""
    uncertain_fields: list[str] = Field(default_factory=list, max_length=10)


# 商品识别词表映射（方案 §12）：视觉模型输出自然语言，映射到内部标准词表。
# 未命中的值原样保留并标注，不猜测、不模糊纠正。
PRODUCT_VOCAB: dict[str, dict[str, str]] = {
    "category": {
        "吸顶灯": "吸顶灯", "客厅灯": "吸顶灯",
        "壁灯": "壁灯", "床头灯": "壁灯",
        "吊灯": "吊灯", "装饰吊灯": "吊灯", "餐厅吊灯": "吊灯",
        "浴室灯": "浴室灯", "卫生间灯": "浴室灯",
        "感应灯": "感应灯", "玄关感应灯": "感应灯",
        "筒灯": "筒灯", "嵌入式筒灯": "筒灯",
    },
    "color": {
        "黑色": "黑色", "黑": "黑色", "白色": "白色", "白": "白色",
        "蓝色": "蓝色", "蓝": "蓝色", "红色": "红色", "红": "红色",
    },
}
VOCAB_VERSION = "home-lighting-v1"

# 可人工补录的证据字段（与 EvidenceField.field_name 契约一致）
MANUAL_FIELDS = ("order_no", "amount_minor", "date", "damage_description")


def apply_product_vocab(recognition: dict) -> dict:
    """把商品识别结果映射到内部标准词表（方案 §12）。

    只做词表内有依据的映射；unknown 与未命中值原样保留，
    并写入 mapping（原始值 → 标准值）与 unmapped_fields 供人工确认。
    """
    mapped = dict(recognition)
    mapping: dict[str, dict] = {}
    unmapped: list[str] = []
    for field, table in PRODUCT_VOCAB.items():
        raw = recognition.get(field)
        if not raw or raw == "unknown":
            continue
        standard = table.get(str(raw).strip())
        if standard is None:
            unmapped.append(field)
            mapping[field] = {"raw": raw, "mapped": None}
        else:
            if standard != raw:
                mapping[field] = {"raw": raw, "mapped": standard}
            mapped[field] = standard
    mapped["mapping"] = mapping
    mapped["unmapped_fields"] = unmapped
    mapped["vocab_version"] = VOCAB_VERSION
    return mapped


def _parse_ocr_amount(value) -> int | None:
    """OCR 金额按“整数分”契约解析；非整数分不猜测，返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def verify_evidence(extracted: dict, *, order_no: str | None, amount_minor: int | None) -> dict:
    """OCR 金额/订单号与订单事实的精确核对（方案 §5）。

    只做精确比对：订单号仅去首尾空白后必须完全一致，金额按整数分精确相等；
    不做模糊纠正，不判断凭证真伪与责任归属。OCR 未确认的字段、缺少订单事实
    或金额非整数分时标注 unverifiable，不参与匹配结论。
    """
    checks: list[dict] = []
    expected = {"order_no": order_no, "amount_minor": amount_minor}
    for field in extracted.get("fields", []):
        name = field.get("field_name")
        if name not in expected:
            continue
        target = expected[name]
        ocr_value = field.get("value")
        check: dict = {"field_name": name, "ocr_value": ocr_value, "expected": target,
                       "source_status": field.get("status"), "source": field.get("source", "ocr")}
        if target is None:
            check.update(verification="unverifiable", reason="缺少订单事实，无法核对")
        elif field.get("status") != "recognized" or ocr_value is None:
            check.update(verification="unverifiable", reason="OCR 未确认该字段，不做比对")
        elif name == "amount_minor":
            parsed = _parse_ocr_amount(ocr_value)
            if parsed is None:
                check.update(verification="unverifiable", reason="金额不是整数分，不做比对")
            else:
                match = parsed == target
                check.update(verification="matched" if match else "mismatched",
                             reason="与订单金额精确一致" if match else "与订单金额不一致")
        else:
            match = str(ocr_value).strip() == str(target).strip()
            check.update(verification="matched" if match else "mismatched",
                         reason="与订单号精确一致" if match else "与订单号不一致")
        checks.append(check)
    if any(item["verification"] == "mismatched" for item in checks):
        status = "mismatched"
    elif any(item["verification"] == "matched" for item in checks):
        status = "matched"
    else:
        status = "unverifiable"
    return {"status": status, "checks": checks,
            "note": "仅与订单记录精确比对，不判断凭证真伪与责任归属。"}


def apply_manual_field(extracted: dict, *, field_name: str, value: str | int, by: str) -> dict:
    """人工补录 OCR 字段：写入人工值并保留 OCR 原值与操作人（方案 §5）。

    补录值进入 source=manual，原值与原状态保存在 manual 子对象中；
    已有核对结论标记 stale，需重新核对（人工值不代表 OCR 原值）。
    """
    if field_name not in MANUAL_FIELDS:
        raise AppError(f"不支持人工补录字段：{field_name}")
    cleaned = value.strip() if isinstance(value, str) else value
    if cleaned is None or cleaned == "":
        raise AppError("补录值不能为空")
    updated = copy.deepcopy(extracted)
    fields = updated.setdefault("fields", [])
    for field in fields:
        if field.get("field_name") == field_name:
            field["manual"] = {"previous_value": field.get("value"),
                               "previous_status": field.get("status"), "by": by}
            field.update(value=cleaned, status="recognized", source="manual")
            break
    else:
        fields.append({"field_name": field_name, "value": cleaned, "bbox": None,
                       "status": "recognized", "source": "manual",
                       "manual": {"previous_value": None, "previous_status": None, "by": by}})
    if isinstance(updated.get("verification"), dict):
        updated["verification"]["stale"] = True
        updated["verification"]["stale_reason"] = "字段经人工补录，原核对结论需重新核对"
    return updated


def stamp_verification(extracted: dict, result: dict, *, order_item_id=None) -> dict:
    """把核对结论固化进证据记录（含时间与关联订单），供核对与补录接口复用。"""
    stamped = {**result, "verified_at": datetime.now(timezone.utc).isoformat(),
               "order_item_id": str(order_item_id) if order_item_id else None}
    return {**extracted, "verification": stamped}


def sanitize_image(data: bytes, file_name: str) -> bytes:
    if len(data) > MAX_FILE_SIZE_BYTES:
        raise AppError("图片超过 10MB 限制")
    formats = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
    ext = file_name.rsplit(".", 1)[-1].lower()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != formats.get(ext) or image.width*image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
                    raise AppError("图片格式、像素或帧数不符合限制")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                clean = Image.new("RGB", image.size, "white")
                clean.paste(image.convert("RGB"))
                output = io.BytesIO()
                clean.save(output, format="JPEG", quality=90)
                return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise AppError("图片无法安全解码，请重新上传") from exc


def _clean_evidence_bbox(extraction: EvidenceExtraction) -> EvidenceExtraction:
    """模型输出属不可信数据：qwen-vl-ocr 等可能返回像素坐标或多于 4 元素的定位框。

    bbox 仅用于前端高亮，非核心事实，无法归一化时丢弃（置 None），不影响字段
    取值的核对；schema 的业务校验（归一化范围/顺序）仍对合法 bbox 生效。
    """
    for field in extraction.fields:
        box = field.bbox
        valid = (box is not None and len(box) == 4
                 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in box)
                 and box[0] <= box[2] and box[1] <= box[3])
        if not valid:
            field.bbox = None
    return extraction


async def extract_image(data, kind, image_id):
    from src.infra.model_gateway import get_model_gateway
    from src.core.config import get_settings
    schema = ProductRecognition if kind == "product_image" else EvidenceExtraction
    prompt = ("只描述可见商品特征，无法确定时使用 unknown，不推断真伪、精确型号或材质。" if kind == "product_image" else
              "提取订单号、金额(整数分)、日期与可见破损说明。模糊字段标为 uncertain 或 missing，不纠正成匹配值，不判断凭证真伪。bbox 使用归一化 [x1,y1,x2,y2]。")
    # 输出格式由 schema 约束（网关 with_structured_output 官方解析链路），不再手工拼 schema JSON。
    response = await get_model_gateway().vision_structured(
        data, prompt, schema,
        clean=_clean_evidence_bbox if schema is EvidenceExtraction else None,
        purpose=kind)
    output = response.response_metadata["parsed"].model_dump()
    output.update(source_image_id=str(image_id), model_version=get_settings().VISION_MODEL)
    if "fields" in output:
        for field in output["fields"]:
            field.update(source_image_id=str(image_id), model_version=get_settings().VISION_MODEL, source="ocr")
    else:
        # 商品识别输出经内部词表映射；未命中值保留原样供人工确认（方案 §12）。
        output = apply_product_vocab(output)
    return output
