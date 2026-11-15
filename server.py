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

_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def local_only(request, call_next):
    """This server reads local files on request, so only our own page may talk to it.
    - Host check blocks DNS rebinding (evil.com resolving to 127.0.0.1).
    - Sec-Fetch-Site blocks other sites embedding e.g. <img src=".../api/file?path=\\\\host\\share\\x.jpg">,
      which would make Windows send NTLM credentials to that host. Browsers set it on every request;
      'none' = typed URL / bookmark, absent = non-browser client.
    """
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
    return await call_next(request)

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
    from PIL import features
    from compressor import engine, quality
    return {"settings": auto_defaults().to_dict(), "cpu_count": os.cpu_count(),
            "formats": {"avif": bool(features.check("avif")), "heic": engine.HEIC_OK, "analysis": quality.available()}}


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
                    filetypes=[("Images", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.avif *.heic *.heif"), ("All files", "*.*")],
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
                  ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp", ".avif": "image/avif"}

_render_cache: "OrderedDict[tuple, bytes]" = OrderedDict()    # PNG renders of HEIC/TIFF for the compare screen
_preview_cache: "OrderedDict[str, tuple]" = OrderedDict()     # preview_id -> (reference image, encoded bytes)
_analysis_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_put(cache: OrderedDict, key, value, limit: int):
    with _cache_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)


def _cache_get(cache: OrderedDict, key):
    with _cache_lock:
        return cache.get(key)


@app.get("/api/file")
def raw_file(path: str):
    """The image's real bytes, for pixel-accurate before/after. Images only."""
    p = Path(path)
    from compressor.engine import is_image
    if not is_image(p):
        raise HTTPException(415, "not an image")
    if not p.is_file():
        raise HTTPException(404, "file not found")
    mt = _BROWSER_TYPES.get(p.suffix.lower())
    if mt is not None:
        return FileResponse(p, media_type=mt, headers={"Cache-Control": "no-cache"})
    # HEIC / TIFF: browsers can't show these, so render losslessly to PNG at full size.
    key = (str(p).lower(), p.stat().st_mtime_ns)
    data = _cache_get(_render_cache, key)
    if data is None:
        try:
            with Image.open(p) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode not in ("RGB", "RGBA", "L"):
                    im = im.convert("RGBA" if "A" in im.mode else "RGB")
                buf = io.BytesIO()
                im.save(buf, "PNG", compress_level=1)
                data = buf.getvalue()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(415, f"cannot render: {e}")
        _cache_put(_render_cache, key, data, 3)
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-cache"})


class PreviewBody(BaseModel):
    path: str
    settings: dict = {}


@app.post("/api/preview")
def preview(body: PreviewBody):
    """Compress one photo in memory with these settings and return the result. Writes nothing."""
    import json
    import uuid
    from compressor.engine import preview_full as engine_preview, _format_for
    p = Path(body.path)
    if not p.is_file():
        raise HTTPException(404, "file not found")
    settings = Settings.from_dict(body.settings)
    try:
        data, result, ref = engine_preview(str(p), settings)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(415, f"cannot preview: {e}")
    result["preview_id"] = uuid.uuid4().hex[:12]
    _cache_put(_preview_cache, result["preview_id"], (ref, data), 2)
    _, ext = _format_for(p, settings)
    return Response(data, media_type=_BROWSER_TYPES.get(ext, "image/jpeg"),
                    headers={"X-Result": json.dumps(result), "Cache-Control": "no-store"})


class AnalyzeBody(BaseModel):
    path: str
    destination: str | None = None
    preview_id: str | None = None


@app.post("/api/analyze")
def analyze(body: AnalyzeBody):
    """Detailed comparison for the compare screen: stats, tile map and a difference image."""
    import base64
    from compressor import quality
    from compressor.engine import load_pair
    if not quality.available():
        raise HTTPException(501, "analysis needs numpy")
    if body.preview_id:
        key = ("preview", body.preview_id)
    elif body.destination and Path(body.destination).is_file() and Path(body.path).is_file():
        key = (body.path.lower(), body.destination.lower(), Path(body.destination).stat().st_mtime_ns)
    else:
        raise HTTPException(404, "nothing to compare")
    out = _cache_get(_analysis_cache, key)
    if out is None:
        try:
            if body.preview_id:
                hit = _cache_get(_preview_cache, body.preview_id)
                if hit is None:
                    raise HTTPException(404, "preview expired, run it again")
                ref, data = hit
                with Image.open(io.BytesIO(data)) as e:
                    enc = e.convert("RGB")
            else:
                ref, enc = load_pair(body.path, body.destination)
            out, diff = quality.analyze(ref, enc)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(415, f"cannot analyze: {e}")
        buf = io.BytesIO()
        diff.save(buf, "PNG", compress_level=3)
        out["diff"] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        _cache_put(_analysis_cache, key, out, 8)
    return out


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
