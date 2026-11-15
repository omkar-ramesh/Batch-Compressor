import io

from PIL import Image, ImageFilter

from compressor import quality
from compressor.engine import compress_file, preview, STATUS_COMPRESSED, STATUS_COPIED
from compressor.settings import Settings
from test_engine import noisy_image, big_jpeg  # noqa: F401  (fixture)


def jpeg_roundtrip(img, q):
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=q); buf.seek(0)
    return Image.open(buf).convert("RGB")


def test_identical_is_one():
    img = noisy_image(800, 600)
    assert quality.ssim(img, img.copy()) == 1.0


def test_score_drops_with_quality():
    img = noisy_image(1200, 800)
    hi, lo = quality.ssim(img, jpeg_roundtrip(img, 95)), quality.ssim(img, jpeg_roundtrip(img, 20))
    assert 0 < lo < hi < 1


def test_blur_is_penalised():
    img = noisy_image(1000, 700)
    assert quality.ssim(img, img.filter(ImageFilter.GaussianBlur(3))) < 0.8


def test_size_mismatch_returns_none():
    assert quality.ssim(noisy_image(100, 100), noisy_image(120, 100)) is None


def test_compressed_result_has_ssim(big_jpeg):
    r = compress_file(str(big_jpeg), Settings(max_mb=1.5))
    assert r.status == STATUS_COMPRESSED and 0.5 < r.ssim <= 1.0


def test_copied_file_has_no_ssim(tmp_path):
    p = tmp_path / "s.jpg"; Image.new("RGB", (100, 100)).save(p, "JPEG")
    r = compress_file(str(p), Settings())
    assert r.status == STATUS_COPIED and r.ssim is None


def test_measure_quality_off(big_jpeg):
    assert compress_file(str(big_jpeg), Settings(max_mb=1.5, measure_quality=False)).ssim is None


def test_preview_writes_nothing(big_jpeg):
    data, r = preview(str(big_jpeg), Settings(max_mb=1.5))
    assert len(data) == r["new_bytes"] <= Settings(max_mb=1.5).limit_bytes
    assert r["destination"] is None and r["ssim"] is not None
    assert not (big_jpeg.parent / "compressed").exists()
    assert Image.open(io.BytesIO(data)).format == "JPEG"
