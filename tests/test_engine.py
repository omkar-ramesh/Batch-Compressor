import io
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from compressor.engine import compress_file, STATUS_COMPRESSED, STATUS_COPIED, STATUS_OVER_LIMIT, STATUS_FAILED, STATUS_SKIPPED
from compressor.batch import scan_paths
from compressor.settings import Settings


def noisy_image(w, h, mode="RGB", seed=0):
    """Photo-like test image: smooth gradients plus film-grain noise. Behaves like a real
    high-detail photo under JPEG (big at q100, shrinks well with quality/resolution)."""
    rng = np.random.default_rng(seed)
    ch = len(mode)
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.stack([
        (xx / w) * 255,
        (yy / h) * 255,
        ((np.sin(xx / 37.0) + np.cos(yy / 53.0)) * 0.25 + 0.5) * 255,
        np.full((h, w), 200.0),
    ][:ch], axis=-1)
    grain = rng.normal(0, 28, (h, w, ch))
    arr = np.clip(base + grain, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, mode)


@pytest.fixture
def big_jpeg(tmp_path):
    p = tmp_path / "big.jpg"
    noisy_image(2400, 1600).save(p, "JPEG", quality=100)
    assert p.stat().st_size > 3 * 1024 * 1024
    return p


def test_output_under_limit(big_jpeg):
    s = Settings(max_mb=1.5)
    r = compress_file(str(big_jpeg), s)
    assert r.status in (STATUS_COMPRESSED, STATUS_OVER_LIMIT)
    assert r.status == STATUS_COMPRESSED
    assert r.new_bytes <= s.limit_bytes
    assert Path(r.destination).exists()
    assert Path(r.destination).parent.name == "compressed"
    assert r.quality is not None and s.quality_min <= r.quality <= s.quality_max


def test_small_file_copied(tmp_path):
    p = tmp_path / "small.jpg"
    Image.new("RGB", (200, 200), (120, 30, 30)).save(p, "JPEG", quality=90)
    r = compress_file(str(p), Settings(max_mb=8))
    assert r.status == STATUS_COPIED
    assert r.new_bytes == r.original_bytes
    assert Path(r.destination).read_bytes() == p.read_bytes()


def test_orientation_applied(tmp_path):
    p = tmp_path / "rot.jpg"
    img = Image.new("RGB", (400, 200), "red")
    exif = img.getexif()
    exif[0x0112] = 6  # rotate 90 CW
    img.save(p, "JPEG", exif=exif.tobytes(), quality=95)
    r = compress_file(str(p), Settings(max_mb=8))
    out = Image.open(r.destination)
    assert out.size == (200, 400)
    assert out.getexif().get(0x0112, 1) == 1


def test_png_alpha_flattened(tmp_path):
    p = tmp_path / "alpha.png"
    noisy_image(1200, 800, "RGBA").save(p, "PNG")
    s = Settings(max_mb=0.5, min_long_edge=300)
    r = compress_file(str(p), s)
    out = Image.open(r.destination)
    assert out.mode == "RGB"
    assert r.destination.endswith(".jpg")
    assert r.status == STATUS_COMPRESSED
    assert r.new_bytes <= s.limit_bytes


def test_resize_kicks_in(big_jpeg):
    s = Settings(max_mb=0.6, min_long_edge=400)
    r = compress_file(str(big_jpeg), s)
    assert r.status == STATUS_COMPRESSED
    assert r.width < r.orig_width and r.height < r.orig_height
    assert r.new_bytes <= s.limit_bytes


def test_over_limit_when_resize_blocked(big_jpeg):
    s = Settings(max_mb=0.3, allow_resize=False)
    r = compress_file(str(big_jpeg), s)
    assert r.status == STATUS_OVER_LIMIT
    assert r.width == r.orig_width
    assert Path(r.destination).exists()


def test_over_limit_when_min_edge_reached(big_jpeg):
    s = Settings(max_mb=0.05, min_long_edge=2200)
    r = compress_file(str(big_jpeg), s)
    assert r.status == STATUS_OVER_LIMIT
    assert max(r.width, r.height) >= 2200


def test_conflict_suffix(tmp_path):
    p = tmp_path / "a.jpg"
    Image.new("RGB", (100, 100)).save(p, "JPEG")
    r1 = compress_file(str(p), Settings())
    r2 = compress_file(str(p), Settings())
    assert r1.destination != r2.destination
    assert r2.destination.endswith("a_1.jpg")
    r3 = compress_file(str(p), Settings(on_conflict="skip"))
    assert r3.status == STATUS_SKIPPED


def test_absolute_output_folder(tmp_path):
    """'Save to…' passes an absolute folder; output must land there, not beside the source."""
    src = tmp_path / "src" / "a.jpg"; src.parent.mkdir()
    Image.new("RGB", (100, 100), "green").save(src, "JPEG")
    dest_dir = tmp_path / "elsewhere" / "picked"
    r = compress_file(str(src), Settings(output_folder=str(dest_dir)))
    assert Path(r.destination).parent == dest_dir
    assert Path(r.destination).exists()
    assert not (src.parent / "compressed").exists()


def test_corrupt_file_fails_gracefully(tmp_path):
    p = tmp_path / "junk.jpg"
    p.write_bytes(b"definitely not an image" * 100)
    r = compress_file(str(p), Settings())
    assert r.status == STATUS_FAILED
    assert r.error


def test_strip_metadata(tmp_path):
    p = tmp_path / "meta.jpg"
    img = Image.new("RGB", (300, 300), "blue")
    exif = img.getexif(); exif[0x010F] = "CameraCo"
    img.save(p, "JPEG", exif=exif.tobytes(), quality=100)
    r = compress_file(str(p), Settings(strip_metadata=True))
    assert 0x010F not in Image.open(r.destination).getexif()


def test_webp_output(big_jpeg):
    s = Settings(max_mb=1.0, output_format="webp")
    r = compress_file(str(big_jpeg), s)
    assert r.destination.endswith(".webp") and r.new_bytes <= s.limit_bytes


def test_scan_paths_excludes_output_folder(tmp_path):
    (tmp_path / "x.jpg").write_bytes(b"0")
    (tmp_path / "sub").mkdir(); (tmp_path / "sub" / "y.png").write_bytes(b"0")
    (tmp_path / "compressed").mkdir(); (tmp_path / "compressed" / "x.jpg").write_bytes(b"0")
    (tmp_path / "notes.txt").write_bytes(b"0")
    names = sorted(f["name"] for f in scan_paths([str(tmp_path)]))
    assert names == ["x.jpg", "y.png"]
    assert [f["name"] for f in scan_paths([str(tmp_path)], recursive=False)] == ["x.jpg"]
