# Batch Compressor — Design

## Goal
Compress hundreds of large (4K, ~25 MB) photos down to a user-set size limit (e.g. 8 MB) with the least possible quality loss, in one batch, fast. Originals are never modified.

## Form
Local web app. Python backend (FastAPI + Pillow) serves a single-page UI at `http://127.0.0.1:8765`. No uploads: the user points the app at folders/files on disk; output is written to a `compressed/` subfolder beside the originals.

## Modes
- **Auto** — only input: max size in MB. Uses tuned defaults below.
- **Manual** — exposes every knob:
  - Target max size (MB) and safety margin (% below limit to aim for, default 3%)
  - Output format: JPEG (default), WebP, or keep original format where possible
  - Quality ceiling / floor (default 95 / 85)
  - Chroma subsampling: 4:4:4 (default), 4:2:2, 4:2:0
  - Allow resize (default on); resize step (default 5%); minimum long edge (default 1920 px)
  - Progressive JPEG (default on), strip metadata (default off; EXIF orientation is always applied)
  - Output folder name (default `compressed`), on-conflict: suffix / overwrite / skip
  - Worker count (default = CPU count)

## Engine (`compressor/engine.py`, no UI code)
For each image:
1. Open, apply EXIF orientation, flatten alpha to white if saving JPEG.
2. If already ≤ target and format matches, copy as-is (status `copied`).
3. Binary-search quality in [floor, ceiling] encoding to memory; keep the highest quality that fits.
4. If floor quality still exceeds target and resizing is allowed, scale by `(1 - step)` repeatedly (LANCZOS; use `Image.draft` on JPEG sources for fast decode) and redo the search, until it fits or long edge would drop below the minimum.
5. If still too big, save best effort and mark `over_limit`.
6. Write file atomically (temp then rename).

Returns a result record: source, destination, original bytes, new bytes, quality used, final dimensions, status, error, ssim.

### Quality score (`compressor/quality.py`)
After encoding, the output is decoded and compared with the image it was encoded from (the resized source, if resized) using luma SSIM with a 7×7 box window, on up to 4×4 tiles of 256 px at the output's resolution (~1 MP, ~100 ms per 4K file). Reported as the tile mean, capped at worst tile + 0.01 so local artifacts aren't averaged away. Resolution loss is reported separately via dimensions. Skipped for PNG (lossless) and copied files. `measure_quality` setting (default on). numpy is optional; without it the score is `null`.
UI bands: ≥ 0.97 excellent, ≥ 0.93 good, below = "Check quality" filter.

### Formats
Input: JPEG, PNG, WebP, AVIF, HEIC/HEIF (via optional `pillow-heif`), TIFF, BMP, GIF. Output: Auto is always JPEG; Manual adds WebP, AVIF (`avif_speed` effort) and keep-original. `GET /api/file` renders formats browsers can't show (HEIC, TIFF) to lossless PNG.

### Compare screen analysis
`POST /api/analyze` (source + destination, or a `preview_id`) returns PSNR, mean/max difference, share of pixels changed, a 12-column tile map of SSIM scores with the worst tile, and a max-pooled per-pixel difference image (≤ 2048 px). The UI colours and amplifies the difference image client-side. Zoom/pan applies one shared CSS transform to both layers.

### Preview
`engine.preview(path, settings)` runs the same load/fit pipeline in memory and writes nothing. Exposed as `POST /api/preview` (image bytes, result JSON in `X-Result`). `GET /api/file` serves real image bytes so the before/after slider compares true pixels, not re-encoded thumbnails.

## Batch runner (`compressor/batch.py`)
`ProcessPoolExecutor` with N workers. Job state (queue, per-file results, totals, elapsed, cancel flag) held in memory per job id. Cancel stops dispatching new files; in-flight files finish.

## API (`server.py`)
- `GET /` — UI
- `POST /api/browse` — opens a native folder/file picker on the host (tkinter) and returns the chosen paths
- `POST /api/scan` — given paths, returns the list of images found (recursive) with sizes
- `POST /api/jobs` — body: `{files, settings}`; returns `job_id`
- `GET /api/jobs/{id}` — progress + results so far
- `POST /api/jobs/{id}/cancel`
- `GET /api/defaults` — Auto-mode defaults for the Manual panel

## UI (`static/index.html`)
Single page: pick sources (Browse / paste paths / drag-drop of folders where the browser supplies paths), Auto/Manual toggle, settings panel, file table (name, original size, new size, saving %, quality, dims, status), progress bar, summary (done / failed / over limit / total saved / time), Cancel, Open output folder.

## Error handling
Unreadable/corrupt files → `failed` with message; batch continues. Permission errors on write → `failed`. Summary always shown.

## Testing
`pytest` on the engine: output ≤ limit; small files copied unchanged; orientation preserved; PNG alpha flattened; over_limit path when min edge reached; conflict suffixing.
