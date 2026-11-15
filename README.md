# Batch Compressor

Shrink hundreds of large photos under a size limit (e.g. 8 MB) while keeping as much quality as possible. Runs locally in your browser; originals are never modified.

## Run

**Download (Windows):** grab `BatchCompressor.exe` from the [Releases page](https://github.com/omkar-ramesh/Batch-Compressor/releases/latest) and double-click it.

**Desktop app:** double-click `dist\BatchCompressor.exe` (build it once with `build.bat`), or from source `python app.py`.

**In the browser:** `run.bat` (or `python server.py`) and open `http://127.0.0.1:8765`.

```
pip install -r requirements.txt
```

## Use

1. **Photos** — *Add folder* / *Add files* (native picker), or paste paths.
2. **Max size** at the top. **Auto** does the rest; **Manual** opens every knob in the side panel.
3. **Compress all** — results land in a `compressed/` folder next to each original.

While it runs, each frame shows an amber "safelight" sweep as it's being worked on, then snaps into full color with its new size. Afterwards:

- A summary at the top: total saved, before/after bar, counts, quality range used, how many were resized.
- **Quality score** on every compressed photo: a *match* value (SSIM vs. the original, 1.000 = identical) shown on each card, in the list and as *Lowest match* in the summary. Photos below 0.93 land in the *Check quality* filter.
- **Grid / List** toggle, thumbnail size slider, sort (name, largest, most saved, needs attention, lowest match; click list column headers to sort) and filters for *Compressed*, *Over limit*, *Check quality*, *Failed*.
- **Presets** in the side panel: built-ins for email, web gallery (WebP) and archive, plus your own (*Save* the current settings under a name).
- When a batch finishes in the background, the taskbar button flashes (desktop app) or a notification appears (browser).
- **Select photos** (hover a frame for the checkbox, Shift-click for a range, Ctrl+A for all) to compress just those, **Save to…** a folder of your choice, or remove them from the batch.
- **Click any photo** to open the compare screen on the real files:
  - **Slider**, **Flicker** (flip in place with Space, or *Auto flip*) and **Difference** (a heat map of where pixels changed, with adjustable amplification and an *Over photo* overlay).
  - **Zoom and pan:** scroll to zoom up to 800%, drag to pan, double-click for 100%. Both versions stay locked together.
  - **Worst area** jumps to the part of the photo that lost the most.
  - **Details** panel: sizes, formats, quality used, SSIM, worst-area score, PSNR, share of pixels changed, and a clickable map of where the losses are.
  - **‹ ›** or the arrow keys step through the photos, keeping your zoom.
  - *Preview with current settings* compresses that one photo in memory so you can judge settings before running the batch; nothing is saved. *Save to…* saves that one photo wherever you like.
- **New batch** clears the photos and results so the next set starts clean.
- *Open the output folder automatically* (side panel) opens Explorer when a batch finishes.
- Refreshing the page mid-batch reattaches to the running job; nothing is lost.
- Deep link: `http://127.0.0.1:8765/#add=E:\Photos\Trip` pre-loads a folder (`&go=1` starts immediately, `&view=list` opens in list view).

## Formats

Reads JPEG, PNG, WebP, AVIF, HEIC/HEIF (iPhone photos), TIFF, BMP and GIF. **Auto** always writes JPEG. In **Manual** you can choose JPEG, WebP, AVIF (smallest files, slower, with an effort setting) or *Keep original*.

## How it compresses

For each image, highest quality first:

1. Already under the limit? Copied untouched.
2. Binary-search JPEG quality between 95 and 85 for the highest value that fits.
3. Still too big at 85? Shrink by 5% and search again, down to a 1920 px long edge.
4. Still too big? Saved at best effort and marked **over limit** so you can decide.

EXIF orientation is applied; other metadata is kept unless you strip it. PNG/transparent images are flattened onto white. Files are written atomically; all CPU cores are used.

## Manual settings

| Setting | Default | Notes |
|---|---|---|
| Safety margin | 3 % | Aim this far under the limit so uploads never land a few KB over |
| Output format | JPEG | WebP is ~25 % smaller at equal quality but less universally accepted; *Keep original* respects JPEG/PNG/WebP sources |
| Quality ceiling / floor | 95 / 85 | Lower the floor (e.g. 80) if you'd rather lose quality than resolution |
| Chroma subsampling | 4:4:4 | 4:2:0 saves ~15–25 % with slight color softening on hard edges |
| Allow resize / step / min edge | on / 5 % / 1920 px | Resolution is only reduced after quality options are exhausted |
| Progressive JPEG | on | Slightly smaller, loads nicely on the web |
| Strip metadata | off | Removes EXIF incl. GPS and camera info |
| If output exists | suffix | `name_1.jpg`, or overwrite / skip |
| Workers | 0 (= all cores) | Lower if you want to keep the PC responsive while it runs |

## Tests

```
python -m pytest tests -q
```
