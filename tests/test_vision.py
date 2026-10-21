import io
import pytest
from PIL import Image
from pydantic import ValidationError
from src.core.exceptions import AppError
from src.services.vision import sanitize_image, EvidenceExtraction


@pytest.mark.parametrize("format,extension", [("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")])
def test_sanitizes_valid_image(format, extension):
    original = io.BytesIO()
    Image.new("RGB", (16,16), "red").save(original, format=format)
    data = sanitize_image(original.getvalue(), f"test.{extension}")
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "JPEG"
        assert image.size == (16, 16)
        assert not image.getexif()


def test_rejects_animated_webp():
    original = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(
        original, format="WEBP", save_all=True,
        append_images=[Image.new("RGB", (16, 16), "blue")], duration=100, loop=0)
    with pytest.raises(AppError):
        sanitize_image(original.getvalue(), "animated.webp")


def test_rejects_spoofed_extension():
    data = io.BytesIO()
    Image.new("RGB", (16,16)).save(data, format="PNG")
    with pytest.raises(AppError):
        sanitize_image(data.getvalue(), "fake.jpg")


def test_rejects_invalid_bytes():
    with pytest.raises(AppError):
        sanitize_image(b"not an image", "fake.png")


def test_invalid_regions_are_dropped_not_fatal():
    # 非法 bbox（像素坐标）宽松接收后由清洗回调丢弃；字段取值仍保留，不阻断核对。
    from src.services.vision import _clean_evidence_bbox
    extracted = EvidenceExtraction(fields=[{"field_name":"order_no", "value":"1", "status":"recognized", "bbox":[0,0,5,5]}])
    cleaned = _clean_evidence_bbox(extracted)
    assert cleaned.fields[0].bbox is None
    assert cleaned.fields[0].value == "1"
    valid = EvidenceExtraction(fields=[{"field_name":"order_no", "value":"1", "status":"recognized", "bbox":[0.1,0.2,0.5,0.9]}])
    assert _clean_evidence_bbox(valid).fields[0].bbox == [0.1, 0.2, 0.5, 0.9]
    with pytest.raises(ValidationError):
        EvidenceExtraction(fields=[{"field_name":"order_no", "value":"1", "status":"recognized", "bbox":[0,0,1,1,2]}])
    with pytest.raises(ValidationError):
        EvidenceExtraction(fields=[{"field_name":"order_no", "status":"recognized"}])
