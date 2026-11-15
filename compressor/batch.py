"""Batch runner: fans images out over a process pool and tracks job state."""
from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, Future
from pathlib import Path
from typing import Iterable

from .engine import compress_file_dict, is_image, STATUS_FAILED, STATUS_OVER_LIMIT, STATUS_COMPRESSED, STATUS_COPIED, STATUS_SKIPPED
from .settings import Settings


def scan_paths(paths: Iterable[str], recursive: bool = True, exclude_folder: str = "compressed") -> list[dict]:
    """Expand files/folders into a de-duplicated list of image files with sizes."""
    seen: set[str] = set()
    out: list[dict] = []

    def add(p: Path):
        key = str(p.resolve()).lower()
        if key in seen:
            return
        seen.add(key)
        try:
            size = p.stat().st_size
        except OSError:
            return
        out.append({"path": str(p), "name": p.name, "bytes": size})

    for raw in paths:
        p = Path(raw.strip().strip('"'))
        if not p.exists():
            continue
        if p.is_file():
            if is_image(p):
                add(p)
        elif p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in it:
                if f.is_file() and is_image(f) and exclude_folder not in {part.lower() for part in f.relative_to(p).parts[:-1]}:
                    add(f)
    out.sort(key=lambda d: d["path"].lower())
    return out


class Job:
    def __init__(self, files: list[str], settings: Settings):
        self.id = uuid.uuid4().hex[:12]
        self.files = files
        self.settings = settings
        self.results: list[dict] = []
        self.started = time.time()
        self.finished: float | None = None
        self.cancelled = False
        self._lock = threading.Lock()
        self._futures: list[Future] = []
        self.state = "running"
        self.workers = max(1, min(settings.workers or os.cpu_count() or 4, len(files) or 1))

    # -- progress -------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            results = list(self.results)
        done = len(results)
        counts = {k: 0 for k in (STATUS_COMPRESSED, STATUS_COPIED, STATUS_OVER_LIMIT, STATUS_SKIPPED, STATUS_FAILED)}
        orig = new = 0
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            if r["status"] in (STATUS_COMPRESSED, STATUS_COPIED, STATUS_OVER_LIMIT):
                orig += r["original_bytes"]
                new += r["new_bytes"]
        end = self.finished or time.time()
        return {
            "id": self.id,
            "state": self.state,
            "total": len(self.files),
            "done": done,
            "counts": counts,
            "original_bytes": orig,
            "new_bytes": new,
            "elapsed": round(end - self.started, 2),
            "workers": self.workers,
            "results": results,
            "settings": self.settings.to_dict(),
        }

    def cancel(self):
        self.cancelled = True
        for f in self._futures:
            f.cancel()


class BatchRunner:
    def __init__(self):
        self.jobs: dict[str, Job] = {}

    def start(self, files: list[str], settings: Settings) -> Job:
        job = Job(files, settings)
        self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: Job):
        workers = job.workers
        sdict = job.settings.to_dict()
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                job._futures = [pool.submit(compress_file_dict, f, sdict) for f in job.files]
                for fut in job._futures:
                    if fut.cancelled():
                        continue
                    try:
                        res = fut.result()
                    except Exception as e:  # noqa: BLE001
                        res = {
                            "source": "?", "destination": None, "original_bytes": 0, "new_bytes": 0,
                            "quality": None, "width": 0, "height": 0, "orig_width": 0, "orig_height": 0,
                            "status": STATUS_FAILED, "error": f"worker error: {e}",
                        }
                    with job._lock:
                        job.results.append(res)
        finally:
            job.finished = time.time()
            job.state = "cancelled" if job.cancelled else "finished"

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)
