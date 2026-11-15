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
