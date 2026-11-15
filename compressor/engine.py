"""Single-image compression engine. Pure functions, no UI, safe to run in worker processes."""
from __future__ import annotations

import io
import os
import shutil
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

from . import quality
from .settings import Settings, SUBSAMPLING_CODES

Image.MAX_IMAGE_PIXELS = None  # we trust local files; 4K+ panoramas exceed the default bomb check

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}

STATUS_COMPRESSED = "compressed"
STATUS_COPIED = "copied"
STATUS_OVER_LIMIT = "over_limit"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


@dataclass
class Result:
    source: str
    destination: Optional[str]
    original_bytes: int
    new_bytes: int
    quality: Optional[int]
    width: int
    height: int
    orig_width: int
    orig_height: int
    status: str
    error: Optional[str] = None
    ssim: Optional[float] = None       # perceptual similarity to the source, 1.0 = identical

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- helpers

def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


def _format_for(src: Path, settings: Settings) -> tuple[str, str]:
    """Return (PIL format name, file extension) for the output."""
    if settings.output_format == "webp":
        return "WEBP", ".webp"
    if settings.output_format == "keep":
        ext = src.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            return "JPEG", ext
        if ext == ".webp":
            return "WEBP", ".webp"
        if ext == ".png":
            return "PNG", ".png"
        # formats without a useful quality knob fall back to JPEG
        return "JPEG", ".jpg"
    return "JPEG", ".jpg"


def _encode(img: Image.Image, fmt: str, quality: int, settings: Settings, exif: bytes | None) -> bytes:
    buf = io.BytesIO()
    kwargs: dict = {}
    # Pillow sizes its JPEG output buffer at w*h bytes when optimize/progressive are on; a
    # high-entropy image at 4:4:4 can exceed that and fail with "broken data stream".
    # Give the encoder a buffer that can hold the worst case.
    ImageFile.MAXBLOCK = max(ImageFile.MAXBLOCK, img.width * img.height * 3 + (len(exif) if exif else 0) + (1 << 20))
    if fmt == "JPEG":
        kwargs.update(
            quality=quality,
            optimize=True,
            progressive=settings.progressive,
            subsampling=SUBSAMPLING_CODES[settings.subsampling],
        )
        if exif:
            kwargs["exif"] = exif
    elif fmt == "WEBP":
        kwargs.update(quality=quality, method=4)
        if exif:
            kwargs["exif"] = exif
    elif fmt == "PNG":
        # PNG is lossless: quality is ignored, we only get size from resizing.
        kwargs.update(optimize=True, compress_level=9)
    img.save(buf, fmt, **kwargs)
    return buf.getvalue()


def _prepare(img: Image.Image, fmt: str) -> Image.Image:
    """Flatten alpha / palette for formats that need it."""
    if fmt == "JPEG":
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            return bg
        if img.mode not in ("RGB", "L"):
            return img.convert("RGB")
    elif fmt == "WEBP":
        if img.mode not in ("RGB", "RGBA"):
            return img.convert("RGBA" if "A" in img.mode or "transparency" in img.info else "RGB")
    return img


def _search_quality(img: Image.Image, fmt: str, settings: Settings, exif: bytes | None, target: int):
    """Binary search the highest quality in [min, max] whose encoding fits target.
    Returns (bytes, quality) of the best fit, or (bytes_at_min, None) if even min is too big."""
    lo, hi = settings.quality_min, settings.quality_max
    best: tuple[bytes, int] | None = None
    # Fast path: try the ceiling first; many images already fit.
    data = _encode(img, fmt, hi, settings, exif)
    if len(data) <= target:
        return data, hi
    hi -= 1
    data_min = None
    while lo <= hi:
        mid = (lo + hi) // 2
        data = _encode(img, fmt, mid, settings, exif)
        if len(data) <= target:
            best = (data, mid)
            lo = mid + 1
        else:
            if mid == settings.quality_min:
                data_min = data
            hi = mid - 1
    if best:
        return best
    if data_min is None:
        data_min = _encode(img, fmt, settings.quality_min, settings, exif)
    return data_min, None


def _resolve_destination(src: Path, ext: str, settings: Settings) -> Path | None:
    out_dir = src.parent / settings.output_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / (src.stem + ext)
    if not dest.exists():
        return dest
    if settings.on_conflict == "overwrite":
        return dest
    if settings.on_conflict == "skip":
        return None
    n = 1
    while True:
        cand = out_dir / f"{src.stem}_{n}{ext}"
        if not cand.exists():
            return cand
        n += 1


def _atomic_write(dest: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp_", suffix=dest.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load(src: Path, fmt: str, settings: Settings):
    """Open, bake in orientation, flatten for the output format.
    Returns (image, exif_bytes, orig_w, orig_h, source_format, needs_orientation)."""
    with Image.open(src) as im:
        orig_w, orig_h = im.size
        src_format = (im.format or "").upper()
        needs_orientation = im.getexif().get(0x0112, 1) not in (None, 1)
        im.load()
        img = ImageOps.exif_transpose(im)  # bake orientation in first (reads the tag)
        exif_bytes = None
        if not settings.strip_metadata:
            exif = im.getexif()
            exif[0x0112] = 1  # orientation is now baked in, so reset the tag
            try:
                exif_bytes = exif.tobytes() if len(exif) else None
            except Exception:
                exif_bytes = None
        img = _prepare(img, fmt)
    return img, exif_bytes, orig_w, orig_h, src_format, needs_orientation


def _fit(img: Image.Image, fmt: str, settings: Settings, exif: bytes | None):
    """Quality search, then shrink in steps until it fits. Returns (data, quality, encoded_from, status)."""
    target = settings.target_bytes
    scale = 1.0
    step = settings.resize_step_pct / 100.0
    cur = img
    status = STATUS_COMPRESSED
    while True:
        if fmt == "PNG":
            data, q = _encode(cur, fmt, 100, settings, exif), None
            fits = len(data) <= target
        else:
            data, q = _search_quality(cur, fmt, settings, exif, target)
            fits = q is not None
        if fits:
            break
        long_edge = max(img.size)
        next_scale = scale * (1 - step)
        if not settings.allow_resize or long_edge * next_scale < settings.min_long_edge:
            status = STATUS_OVER_LIMIT
            q = q if q is not None else settings.quality_min
            break
        scale = next_scale
        new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        cur = img.resize(new_size, Image.LANCZOS)
    return data, q, cur, status


def _score(reference: Image.Image, data: bytes, fmt: str, settings: Settings) -> float | None:
    if not settings.measure_quality or fmt == "PNG":
        return None  # PNG is lossless; resolution loss is reported via dimensions
    try:
        with Image.open(io.BytesIO(data)) as enc:
            enc.load()
            return quality.ssim(reference, enc)
    except Exception:  # noqa: BLE001 - a missing score must never fail a file
        return None


def preview(src_path: str, settings: Settings) -> tuple[bytes, dict]:
    """Compress one image in memory with the given settings. Nothing is written.
    Returns (encoded bytes, result dict without destination)."""
    src = Path(src_path)
    original_bytes = src.stat().st_size
    fmt, _ = _format_for(src, settings)
    img, exif, ow, oh, _, _ = _load(src, fmt, settings)
    data, q, cur, status = _fit(img, fmt, settings, exif)
    r = Result(str(src), None, original_bytes, len(data), q, cur.width, cur.height, ow, oh, status,
               ssim=_score(cur, data, fmt, settings))
    return data, r.to_dict()


# --------------------------------------------------------------------------- main entry

def compress_file(src_path: str, settings: Settings) -> Result:
    src = Path(src_path)
    try:
        original_bytes = src.stat().st_size
    except OSError as e:
        return Result(str(src), None, 0, 0, None, 0, 0, 0, 0, STATUS_FAILED, f"cannot stat: {e}")

    fmt, ext = _format_for(src, settings)

    try:
        # Cheap peek (header only) to decide on pass-through before decoding pixels.
        with Image.open(src) as im:
            orig_w, orig_h = im.size
            same_format = (im.format or "").upper() == fmt
            needs_orientation = im.getexif().get(0x0112, 1) not in (None, 1)

        if original_bytes <= settings.limit_bytes and same_format and not (settings.strip_metadata or needs_orientation):
            dest = _resolve_destination(src, src.suffix, settings)
            if dest is None:
                return Result(str(src), None, original_bytes, 0, None, orig_w, orig_h, orig_w, orig_h, STATUS_SKIPPED)
            shutil.copy2(src, dest)
            return Result(str(src), str(dest), original_bytes, original_bytes, None, orig_w, orig_h, orig_w, orig_h, STATUS_COPIED)

        img, exif_bytes, orig_w, orig_h, _, _ = _load(src, fmt, settings)
        dest = _resolve_destination(src, ext, settings)
        if dest is None:
            return Result(str(src), None, original_bytes, 0, None, orig_w, orig_h, orig_w, orig_h, STATUS_SKIPPED)

        data, q, cur, status = _fit(img, fmt, settings, exif_bytes)
        _atomic_write(dest, data)
        return Result(
            source=str(src), destination=str(dest),
            original_bytes=original_bytes, new_bytes=len(data),
            quality=q, width=cur.width, height=cur.height,
            orig_width=orig_w, orig_height=orig_h, status=status,
            ssim=_score(cur, data, fmt, settings),
        )
    except UnidentifiedImageError:
        return Result(str(src), None, original_bytes, 0, None, 0, 0, 0, 0, STATUS_FAILED, "not a readable image")
    except Exception as e:  # noqa: BLE001 - we want every failure recorded, never a crashed batch
        return Result(str(src), None, original_bytes, 0, None, 0, 0, 0, 0, STATUS_FAILED, f"{type(e).__name__}: {e}")


def compress_file_dict(src_path: str, settings_dict: dict) -> dict:
    """Picklable wrapper for ProcessPoolExecutor."""
    return compress_file(src_path, Settings.from_dict(settings_dict)).to_dict()
