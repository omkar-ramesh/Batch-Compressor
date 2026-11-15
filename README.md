# Batch Compressor

Shrink hundreds of large photos under a size limit (e.g. 8 MB) while keeping as much quality as possible. Runs locally in your browser; originals are never modified.

## Run

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
- **Grid / List** toggle, thumbnail size slider, sort (name, largest, most saved, needs attention) and filters for *Compressed*, *Over limit*, *Failed*.
- **Select photos** (hover a frame for the checkbox, Shift-click for a range, Ctrl+A for all) to compress just those, **Save to…** a folder of your choice, or remove them from the batch.
- **Click any photo** for a before/after slider (drag to compare, *100%* for pixel-level); *Save to…* there saves that one photo wherever you like.
- **New batch** clears the photos and results so the next set starts clean.
- *Open the output folder automatically* (side panel) opens Explorer when a batch finishes.
- Refreshing the page mid-batch reattaches to the running job; nothing is lost.
- Deep link: `http://127.0.0.1:8765/#add=E:\Photos\Trip` pre-loads a folder (`&go=1` starts immediately, `&view=list` opens in list view).

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
