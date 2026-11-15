"""Compression settings shared by the engine, batch runner and API."""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Literal

OutputFormat = Literal["jpeg", "webp", "keep"]
Subsampling = Literal["4:4:4", "4:2:2", "4:2:0"]
OnConflict = Literal["suffix", "overwrite", "skip"]

SUBSAMPLING_CODES = {"4:4:4": 0, "4:2:2": 1, "4:2:0": 2}


@dataclass
class Settings:
    max_mb: float = 8.0
    margin_pct: float = 3.0            # aim this % below the limit
    output_format: OutputFormat = "jpeg"
    quality_max: int = 95
    quality_min: int = 85
    subsampling: Subsampling = "4:4:4"
    allow_resize: bool = True
    resize_step_pct: float = 5.0
    min_long_edge: int = 1920
    progressive: bool = True
    strip_metadata: bool = False
    output_folder: str = "compressed"
    on_conflict: OnConflict = "suffix"
    workers: int = 0                   # 0 = cpu count
    measure_quality: bool = True       # compute SSIM vs source for each output

    @property
    def target_bytes(self) -> int:
        return int(self.max_mb * 1024 * 1024 * (1 - self.margin_pct / 100))

    @property
    def limit_bytes(self) -> int:
        return int(self.max_mb * 1024 * 1024)

    def validated(self) -> "Settings":
        s = Settings(**asdict(self))
        s.max_mb = max(0.05, float(s.max_mb))
        s.margin_pct = min(max(0.0, float(s.margin_pct)), 50.0)
        s.quality_max = min(max(int(s.quality_max), 1), 100)
        s.quality_min = min(max(int(s.quality_min), 1), s.quality_max)
        s.resize_step_pct = min(max(float(s.resize_step_pct), 1.0), 50.0)
        s.min_long_edge = max(16, int(s.min_long_edge))
        if s.output_format not in ("jpeg", "webp", "keep"):
            s.output_format = "jpeg"
        if s.subsampling not in SUBSAMPLING_CODES:
            s.subsampling = "4:4:4"
        if s.on_conflict not in ("suffix", "overwrite", "skip"):
            s.on_conflict = "suffix"
        s.output_folder = (s.output_folder or "compressed").strip() or "compressed"
        s.workers = max(0, int(s.workers))
        return s

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Settings":
        d = d or {}
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known).validated()


def auto_defaults() -> Settings:
    """Auto mode: everything tuned for 'as good as possible under the limit'."""
    return Settings()
