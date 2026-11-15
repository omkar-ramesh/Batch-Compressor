"""Batch Compressor desktop app: the local server inside a native window.

Run from source:   python app.py
Build an .exe:     build.bat   (uses PyInstaller; output in dist/)
"""
from __future__ import annotations

import multiprocessing
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path


def resource_path(rel: str) -> Path:
    """Works both from source and from a PyInstaller bundle."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / rel


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    import uvicorn
    import webview

    import server

    server.STATIC = resource_path("static")
    # Re-point the already-mounted static route at the bundled folder.
    for route in server.app.routes:
        if getattr(route, "name", "") == "static":
            route.app.directory = str(server.STATIC)

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning")
    http = uvicorn.Server(config)
    threading.Thread(target=http.run, daemon=True).start()

    # Wait until the server answers before showing the window (avoids a blank first paint).
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/api/defaults", timeout=0.2)
            break
        except Exception:
            time.sleep(0.05)

    window = webview.create_window(
        "Batch Compressor", url,
        width=1440, height=900, min_size=(960, 640),
        background_color="#141416",
    )

    # Native pickers through the window instead of a stray tkinter dialog.
    def picker(kind: str) -> list[str]:
        if kind == "files":
            res = window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=("Images (*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.tif;*.tiff)", "All files (*.*)"),
            )
        else:
            res = window.create_file_dialog(webview.FOLDER_DIALOG)
        return [str(p) for p in (res or [])]

    server.picker = picker

    def on_closed():
        http.should_exit = True

    window.events.closed += on_closed
    webview.start(debug="--debug" in sys.argv)
    http.should_exit = True


if __name__ == "__main__":
    multiprocessing.freeze_support()  # required: the worker pool re-launches this exe
    main()
