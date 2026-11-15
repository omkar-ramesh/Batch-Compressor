import pytest
from fastapi.testclient import TestClient
from PIL import Image, features

import server
from compressor import quality
from compressor.engine import compress_file, load_pair, HEIC_OK, STATUS_COMPRESSED
from compressor.settings import Settings, auto_defaults
from test_engine import noisy_image, big_jpeg  # noqa: F401  (fixture)

avif = pytest.mark.skipif(not features.check("avif"), reason="Pillow built without AVIF")
heic = pytest.mark.skipif(not HEIC_OK, reason="pillow-heif not installed")


@pytest.fixture
def client():
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def test_auto_defaults_unchanged():
    """Auto mode must stay plain JPEG; the new formats are Manual-only."""
    s = auto_defaults()
    assert (s.output_format, s.quality_max, s.quality_min, s.subsampling) == ("jpeg", 95, 85, "4:4:4")


@avif
def test_avif_output(big_jpeg):
    s = Settings(max_mb=0.5, output_format="avif", quality_min=40, quality_max=80, avif_speed=9)
    r = compress_file(str(big_jpeg), s)
    assert r.status == STATUS_COMPRESSED and r.format == "AVIF"
    assert r.destination.endswith(".avif") and r.new_bytes <= s.limit_bytes
    assert Image.open(r.destination).format == "AVIF" and r.ssim is not None


@heic
def test_heic_input(tmp_path):
    p = tmp_path / "phone.heic"
    noisy_image(1600, 1200).save(p, "HEIF", quality=95)
    s = Settings(max_mb=0.3, min_long_edge=400)
    r = compress_file(str(p), s)
    assert r.status == STATUS_COMPRESSED and r.destination.endswith(".jpg") and r.new_bytes <= s.limit_bytes


@heic
def test_heic_served_as_png(client, tmp_path):
    p = tmp_path / "phone.heic"
    Image.new("RGB", (64, 48), "teal").save(p, "HEIF")
    r = client.get("/api/file", params={"path": str(p)})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_analyze_stats(big_jpeg):
    r = compress_file(str(big_jpeg), Settings(max_mb=0.6, min_long_edge=400))
    ref, enc = load_pair(str(big_jpeg), r.destination)
    assert ref.size == enc.size == (r.width, r.height)
    stats, diff = quality.analyze(ref, enc)
    assert len(stats["tiles"]) == stats["cols"] * stats["rows"]
    worst = stats["tiles"][stats["worst"]]
    assert worst["ssim"] == min(t["ssim"] for t in stats["tiles"]) < 1
    assert stats["psnr"] > 10 and stats["changed_pct"] > 0
    assert diff.mode == "L" and max(diff.size) <= quality.DIFF_EDGE


def test_analyze_identical():
    img = noisy_image(400, 300)
    stats, diff = quality.analyze(img, img.copy())
    assert stats["psnr"] is None and stats["max_diff"] == 0 and stats["changed_pct"] == 0


def test_analyze_endpoints(client, big_jpeg):
    import json
    r = compress_file(str(big_jpeg), Settings(max_mb=1.5))
    a = client.post("/api/analyze", json={"path": str(big_jpeg), "destination": r.destination}).json()
    assert a["diff"].startswith("data:image/png;base64,") and a["tiles"]
    p = client.post("/api/preview", json={"path": str(big_jpeg), "settings": {"max_mb": 1.5}})
    pid = json.loads(p.headers["x-result"])["preview_id"]
    assert client.post("/api/analyze", json={"path": str(big_jpeg), "preview_id": pid}).status_code == 200
    assert client.post("/api/analyze", json={"path": str(big_jpeg), "preview_id": "gone"}).status_code == 404
    assert client.post("/api/analyze", json={"path": str(big_jpeg)}).status_code == 404
    f = client.get("/api/defaults").json()["formats"]
    assert set(f) == {"avif", "heic", "analysis"}
