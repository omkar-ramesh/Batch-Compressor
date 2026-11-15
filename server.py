"""Batch Compressor local server. Run: python server.py  ->  http://127.0.0.1:8765"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import io
from collections import OrderedDict

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, ImageOps
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from compressor.batch import BatchRunner, scan_paths
from compressor.settings import Settings, auto_defaults

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
HOST, PORT = "127.0.0.1", 8765

app = FastAPI(title="Batch Compressor")
runner = BatchRunner()

# The desktop app (app.py) replaces this with a native WebView2 dialog; the browser
# version falls back to tkinter.
picker = None


class PathsBody(BaseModel):
    paths: list[str]
    recursive: bool = True


class JobBody(BaseModel):
    files: list[str]
    settings: dict = {}


class BrowseBody(BaseModel):
    kind: str = "folder"  # folder | files


class OpenBody(BaseModel):
    path: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/defaults")
def defaults():
    return {"settings": auto_defaults().to_dict(), "cpu_count": os.cpu_count()}


@app.post("/api/scan")
def scan(body: PathsBody):
    files = scan_paths(body.paths, recursive=body.recursive)
    return {"files": files, "total_bytes": sum(f["bytes"] for f in files)}


@app.post("/api/browse")
def browse(body: BrowseBody):
    """Open a native picker on the host machine (we're local-only, so this is fine)."""
    result: dict = {"paths": []}
    if picker is not None:
        try:
            result["paths"] = picker(body.kind)
        except Exception as e:  # noqa: BLE001
            result["error"] = str(e)
        return result

    def pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            if body.kind == "files":
                paths = filedialog.askopenfilenames(
                    title="Select images",
                    filetypes=[("Images", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff"), ("All files", "*.*")],
                )
                result["paths"] = list(paths)
            else:
                p = filedialog.askdirectory(title="Select a folder of images")
                result["paths"] = [p] if p else []
            root.destroy()
        except Exception as e:  # noqa: BLE001
            result["error"] = str(e)

    t = threading.Thread(target=pick)
    t.start()
    t.join()
    return result


@app.post("/api/jobs")
def create_job(body: JobBody):
    if not body.files:
        raise HTTPException(400, "no files")
    settings = Settings.from_dict(body.settings)
    job = runner.start(body.files, settings)
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, since: int = 0):
    job = runner.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    snap = job.snapshot()
    # only send results the client hasn't seen yet
    snap["results"] = snap["results"][since:]
    snap["since"] = since
    return snap


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = runner.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job.cancel()
    return {"ok": True}


_thumb_cache: "OrderedDict[tuple, bytes]" = OrderedDict()
_THUMB_CACHE_MAX = 1500
_thumb_lock = threading.Lock()


def _make_thumb(path: Path, size: int) -> bytes:
    with Image.open(path) as im:
        # JPEG sources decode at 1/2..1/8 scale for free; huge speedup on 4K files.
        im.draft("RGB", (size, size))
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size), Image.BILINEAR if size <= 400 else Image.LANCZOS)
        if im.mode not in ("RGB", "L"):
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82 if size <= 400 else 90, optimize=False)
        return buf.getvalue()


@app.get("/api/thumb")
def thumb(path: str, size: int = 320):
    """Thumbnail of any local image. Cached in memory keyed by (path, mtime, size)."""
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, "file not found")
    size = max(32, min(int(size), 2400))
    key = (str(p).lower(), p.stat().st_mtime_ns, size)
    with _thumb_lock:
        data = _thumb_cache.get(key)
        if data:
            _thumb_cache.move_to_end(key)
    if data is None:
        try:
            data = _make_thumb(p, size)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(415, f"cannot render: {e}")
        with _thumb_lock:
            _thumb_cache[key] = data
            while len(_thumb_cache) > _THUMB_CACHE_MAX:
                _thumb_cache.popitem(last=False)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


_BROWSER_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                  ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}


@app.get("/api/file")
def raw_file(path: str):
    """The image's real bytes, for pixel-accurate before/after. Images only."""
    p = Path(path)
    mt = _BROWSER_TYPES.get(p.suffix.lower())
    if mt is None:
        raise HTTPException(415, "not a browser-viewable image")
    if not p.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(p, media_type=mt, headers={"Cache-Control": "no-cache"})


class PreviewBody(BaseModel):
    path: str
    settings: dict = {}


@app.post("/api/preview")
def preview(body: PreviewBody):
    """Compress one photo in memory with these settings and return the result. Writes nothing."""
    import json
    from compressor.engine import preview as engine_preview, _format_for
    p = Path(body.path)
    if not p.is_file():
        raise HTTPException(404, "file not found")
    settings = Settings.from_dict(body.settings)
    try:
        data, result = engine_preview(str(p), settings)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(415, f"cannot preview: {e}")
    _, ext = _format_for(p, settings)
    return Response(data, media_type=_BROWSER_TYPES.get(ext, "image/jpeg"),
                    headers={"X-Result": json.dumps(result), "Cache-Control": "no-store"})


@app.post("/api/reset")
def reset():
    """Forget finished jobs and drop the thumbnail cache. Running jobs are left alone."""
    with _thumb_lock:
        thumbs = len(_thumb_cache)
        _thumb_cache.clear()
    gone = [jid for jid, j in runner.jobs.items() if j.state != "running"]
    for jid in gone:
        del runner.jobs[jid]
    return {"ok": True, "thumbnails_cleared": thumbs, "jobs_cleared": len(gone)}


@app.post("/api/open")
def open_folder(body: OpenBody):
    p = Path(body.path)
    if not p.exists():
        raise HTTPException(404, "path not found")
    target = p if p.is_dir() else p.parent
    try:
        if sys.platform.startswith("win"):
            # Explorer with the file highlighted; falls back to plain folder open.
            if p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                subprocess.Popen(["explorer", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if p.is_file() else str(target)] + ([str(p)] if p.is_file() else []))
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"couldn't open folder: {e}")
    return {"ok": True, "folder": str(target)}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Batch Compressor running at {url}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
