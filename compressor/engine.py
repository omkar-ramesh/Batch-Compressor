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


# --------------------------------------------------------------------------- main entry

def compress_file(src_path: str, settings: Settings) -> Result:
    src = Path(src_path)
    try:
        original_bytes = src.stat().st_size
    except OSError as e:
        return Result(str(src), None, 0, 0, None, 0, 0, 0, 0, STATUS_FAILED, f"cannot stat: {e}")

    fmt, ext = _format_for(src, settings)
    target = settings.target_bytes

    try:
        with Image.open(src) as im:
            orig_w, orig_h = im.size
            same_format = (im.format or "").upper() == fmt
            needs_orientation = im.getexif().get(0x0112, 1) not in (None, 1)

            # Pass-through: already small and nothing to change.
            if original_bytes <= settings.limit_bytes and same_format and not (settings.strip_metadata or needs_orientation):
                dest = _resolve_destination(src, src.suffix, settings)
                if dest is None:
                    return Result(str(src), None, original_bytes, 0, None, orig_w, orig_h, orig_w, orig_h, STATUS_SKIPPED)
                shutil.copy2(src, dest)
                return Result(str(src), str(dest), original_bytes, original_bytes, None, orig_w, orig_h, orig_w, orig_h, STATUS_COPIED)

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

        dest = _resolve_destination(src, ext, settings)
        if dest is None:
            return Result(str(src), None, original_bytes, 0, None, orig_w, orig_h, orig_w, orig_h, STATUS_SKIPPED)

        scale = 1.0
        step = settings.resize_step_pct / 100.0
        cur = img
        status = STATUS_COMPRESSED
        while True:
            if fmt == "PNG":
                data, q = _encode(cur, fmt, 100, settings, exif_bytes), None
                fits = len(data) <= target
            else:
                data, q = _search_quality(cur, fmt, settings, exif_bytes, target)
                fits = q is not None
            if fits:
                break
            # Try shrinking
            long_edge = max(img.size)
            next_scale = scale * (1 - step)
            if not settings.allow_resize or long_edge * next_scale < settings.min_long_edge:
                status = STATUS_OVER_LIMIT
                q = q if q is not None else settings.quality_min
                break
            scale = next_scale
            new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
            cur = img.resize(new_size, Image.LANCZOS)

        _atomic_write(dest, data)
        return Result(
            source=str(src), destination=str(dest),
            original_bytes=original_bytes, new_bytes=len(data),
            quality=q, width=cur.width, height=cur.height,
            orig_width=orig_w, orig_height=orig_h, status=status,
        )
    except UnidentifiedImageError:
        return Result(str(src), None, original_bytes, 0, None, 0, 0, 0, 0, STATUS_FAILED, "not a readable image")
    except Exception as e:  # noqa: BLE001 - we want every failure recorded, never a crashed batch
        return Result(str(src), None, original_bytes, 0, None, 0, 0, 0, 0, STATUS_FAILED, f"{type(e).__name__}: {e}")


def compress_file_dict(src_path: str, settings_dict: dict) -> dict:
    """Picklable wrapper for ProcessPoolExecutor."""
    return compress_file(src_path, Settings.from_dict(settings_dict)).to_dict()
