"""Perceptual quality score (SSIM) between a reference image and its encoded version.

Measured at the encoded image's own resolution on a grid of tiles, so JPEG/WebP
artifacts are visible to the metric (downscaling first would average them away)
while the cost stays around one megapixel per image regardless of source size.
Luma only: it catches blocking, ringing and blur, not chroma subsampling loss.
"""
from __future__ import annotations

from PIL import Image

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is optional at runtime
    np = None

TILE = 256
GRID = 4          # up to GRID x GRID tiles
WIN = 7           # SSIM window (box)
_C1 = (0.01 * 255) ** 2
_C2 = (0.03 * 255) ** 2


def available() -> bool:
    return np is not None


def _window_mean(x):
    """Mean over every WIN x WIN window ('valid' region) using an integral image."""
    c = np.zeros((x.shape[0] + 1, x.shape[1] + 1), dtype=np.float64)
    c[1:, 1:] = x.cumsum(0).cumsum(1)
    k = WIN
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return s / (k * k)


def _ssim_gray(a, b) -> float:
    mu_a, mu_b = _window_mean(a), _window_mean(b)
    saa = _window_mean(a * a) - mu_a * mu_a
    sbb = _window_mean(b * b) - mu_b * mu_b
    sab = _window_mean(a * b) - mu_a * mu_b
    num = (2 * mu_a * mu_b + _C1) * (2 * sab + _C2)
    den = (mu_a * mu_a + mu_b * mu_b + _C1) * (saa + sbb + _C2)
    return float((num / den).mean())


def _tiles(w: int, h: int):
    """Evenly spread TILE-sized boxes covering the image; the whole image if it is small."""
    if w <= TILE * 2 and h <= TILE * 2:
        return [(0, 0, w, h)]
    tw, th = min(TILE, w), min(TILE, h)
    nx, ny = min(GRID, max(1, w // tw)), min(GRID, max(1, h // th))
    xs = [round(i * (w - tw) / max(1, nx - 1)) for i in range(nx)]
    ys = [round(j * (h - th) / max(1, ny - 1)) for j in range(ny)]
    return [(x, y, x + tw, y + th) for y in ys for x in xs]


def ssim(reference: Image.Image, encoded: Image.Image) -> float | None:
    """SSIM in [0, 1] (1 = identical). None if numpy is missing or sizes differ."""
    if np is None or reference.size != encoded.size:
        return None
    ref = reference.convert("L")
    enc = encoded.convert("L")
    scores = []
    for box in _tiles(*ref.size):
        a = np.asarray(ref.crop(box), dtype=np.float64)
        b = np.asarray(enc.crop(box), dtype=np.float64)
        if a.shape[0] < WIN or a.shape[1] < WIN:
            continue
        scores.append(_ssim_gray(a, b))
    if not scores:
        return None
    # Report the mean, but let one bad tile pull it down: artifacts are local.
    return round(min(sum(scores) / len(scores), min(scores) + 0.01), 4)


# --------------------------------------------------------------------------- detailed analysis

MAP_COLS = 12       # tile map resolution (columns; rows follow the aspect ratio)
DIFF_EDGE = 2048    # long edge of the difference map sent to the UI


def analyze(reference: Image.Image, encoded: Image.Image) -> tuple[dict, Image.Image]:
    """Full comparison for the compare screen.
    Returns (stats, diff) where diff is an 'L' image: per-pixel max channel difference,
    max-pooled down to DIFF_EDGE so single-pixel artifacts survive the downscale."""
    if np is None:
        raise RuntimeError("numpy is not available")
    if reference.size != encoded.size:
        raise ValueError("size mismatch")
    w, h = reference.size
    a = np.asarray(reference.convert("RGB"))
    b = np.asarray(encoded.convert("RGB"))
    d = np.empty((h, w), dtype=np.uint8)
    sq = 0.0
    for y in range(0, h, 512):  # strips keep peak memory flat on huge images
        diff = a[y:y + 512].astype(np.int16) - b[y:y + 512]
        sq += float(np.square(diff, dtype=np.int32).sum())
        d[y:y + 512] = np.abs(diff).max(axis=2)
    mse = sq / (w * h * 3)
    k = max(1, -(-max(w, h) // DIFF_EDGE))
    ph, pw = h // k, w // k
    pooled = d[:ph * k, :pw * k].reshape(ph, k, pw, k).max(axis=(1, 3)) if k > 1 else d

    ga = np.asarray(reference.convert("L"))
    gb = np.asarray(encoded.convert("L"))
    cols = max(1, min(MAP_COLS, w // 64))
    rows = max(1, round(cols * h / w))
    cw, ch = w / cols, h / rows
    tiles = []
    for r in range(rows):
        for c in range(cols):
            x0, y0 = int(c * cw), int(r * ch)
            x1, y1 = int((c + 1) * cw), int((r + 1) * ch)
            # score the centre TILE x TILE of each cell: full coverage would cost seconds on 4K+
            tw, th = min(TILE, x1 - x0), min(TILE, y1 - y0)
            sx, sy = x0 + (x1 - x0 - tw) // 2, y0 + (y1 - y0 - th) // 2
            if tw < WIN or th < WIN:
                sc = 1.0
            else:
                sc = _ssim_gray(ga[sy:sy + th, sx:sx + tw].astype(np.float64), gb[sy:sy + th, sx:sx + tw].astype(np.float64))
            tiles.append({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "ssim": round(sc, 4)})
    worst = min(range(len(tiles)), key=lambda i: tiles[i]["ssim"])
    stats = {
        "width": w, "height": h, "cols": cols, "rows": rows, "tiles": tiles, "worst": worst,
        "tile_mean": round(sum(t["ssim"] for t in tiles) / len(tiles), 4),
        "psnr": round(10 * float(np.log10(255 * 255 / mse)), 2) if mse > 0 else None,  # None = identical
        "mean_diff": round(float(d.mean()), 3),
        "max_diff": int(d.max()),
        "changed_pct": round(float((d > 2).mean()) * 100, 2),
        "diff_scale": k,
    }
    return stats, Image.fromarray(pooled, "L")
