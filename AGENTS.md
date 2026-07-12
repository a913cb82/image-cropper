# Image Crop

Batch-crops images to 9:20 portrait ratio via a localhost web UI.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install Flask Pillow
```

## Usage

```bash
.venv/bin/python web_crop.py <input_folder>
```

Open http://localhost:5000 in a browser. Cropped images save to `<input_folder>/cropped_9_20/`.

## Controls

- **Left-Click + Drag**: Move crop box
- **Scroll Wheel**: Zoom crop box (accelerating speed with rapid scrolls)
- **Space / Enter**: Approve crop and advance to next image
- **Left / Right arrows**: Navigate without saving

A green checkmark (top-right) indicates the current image already has a saved crop.

## Architecture

Single-file Flask app (`web_crop.py`) with inline HTML/JS.

### Backend (Flask)
- Serves images as cached JPEG thumbnails for fast delivery
- Crop state lives client-side; server only handles save/navigate
- Saves crops in a background thread (fire-and-forget) so the UI never blocks
- Preserves original format and compression settings (JPEG quality/subsampling, PNG compress_level, WebP quality)
- `_info_cache` / `_image_cache` dicts avoid re-encoding on repeated requests

### Frontend (Canvas)
- All crop positioning (cx, cy, cw) is normalized 0.0-1.0 relative to image dimensions
- `constrainCrop()` clamps crop box to image boundaries client-side (no server round-trip during drag/zoom)
- Prefetch cache (`Map<idx, {img, info, baseCanvas}>`) holds ±5 images around current index
- Inflight request tracking prevents duplicate fetches when scrolling to a partially-loaded image
- OffscreenCanvas pre-rendering: each cached image is pre-rendered into a dimmed base canvas and a full-res image canvas at display size, so `draw()` just blits pre-rendered buffers + draws the crop box overlay

### API Endpoints
- `GET /api/info?idx=N` — image metadata (dimensions, max crop width, has_crop)
- `GET /api/image?idx=N` — JPEG thumbnail for display
- `POST /api/approve` — save crop in background thread, advance index
- `POST /api/navigate` — move index by direction (-1 or +1)

## Files

- `web_crop.py` — main application (Flask backend + inline frontend)
- `bench.py` — API benchmarking script (requires `requests`)
