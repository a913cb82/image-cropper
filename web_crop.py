import os
import io
import threading
from flask import Flask, jsonify, request, send_file
from PIL import Image, ImageOps

app = Flask(__name__)

RATIO_W, RATIO_H = 9, 20  # portrait 9:20 (width:height)
ratio = RATIO_W / RATIO_H  # 0.45

images = []
current_idx = 0
input_dir = None
output_dir = None

# Cache: idx -> (img_w, img_h, max_cw, jpeg_bytes)
_info_cache = {}
_image_cache = {}


@app.route("/")
def index():
    return HTML


def _save_crop(idx, cx, cy, cw):
    original = Image.open(images[idx])
    fmt = original.format
    img = ImageOps.exif_transpose(original)
    img_w, img_h = img.size
    crop_w = cw * img_w
    crop_h = crop_w / ratio
    cx_px = cx * img_w
    cy_px = cy * img_h
    x1 = int(cx_px - crop_w / 2)
    y1 = int(cy_px - crop_h / 2)
    x2 = int(x1 + crop_w)
    y2 = int(y1 + crop_h)
    cropped = img.crop((x1, y1, x2, y2))
    os.makedirs(output_dir, exist_ok=True)
    dest = os.path.join(output_dir, os.path.basename(images[idx]))
    save_kwargs = {"format": fmt}
    if fmt in ("JPEG", "JPG"):
        save_kwargs["quality"] = original.info.get("quality", 95)
        save_kwargs["subsampling"] = original.info.get("subsampling", 0)
    elif fmt == "WEBP":
        save_kwargs["quality"] = original.info.get("quality", 90)
    elif fmt == "PNG":
        save_kwargs["compress_level"] = original.info.get("compress_level", 6)
    cropped.save(dest, **save_kwargs)
    _info_cache.pop(idx, None)


def _get_info_for(idx):
    if idx in _info_cache:
        return _info_cache[idx]
    img = Image.open(images[idx])
    img_w, img_h = img.size
    max_cw = min(1.0, (img_h * ratio) / img_w)
    dest = os.path.join(output_dir, os.path.basename(images[idx]))
    entry = {
        "index": idx,
        "total": len(images),
        "filename": os.path.basename(images[idx]),
        "img_w": img_w,
        "img_h": img_h,
        "max_cw": max_cw,
        "has_crop": os.path.exists(dest),
    }
    _info_cache[idx] = entry
    return entry


def _get_image_bytes(idx):
    if idx in _image_cache:
        return _image_cache[idx]
    img = Image.open(images[idx])
    img = ImageOps.exif_transpose(img).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    data = buf.read()
    _image_cache[idx] = data
    return data


@app.route("/api/info")
def get_info():
    if not images:
        return jsonify({"error": "no images"}), 404
    idx = int(request.args.get("idx", current_idx))
    idx = max(0, min(len(images) - 1, idx))
    return jsonify(_get_info_for(idx))


@app.route("/api/image")
def get_image():
    if not images:
        return "", 404
    idx = int(request.args.get("idx", current_idx))
    idx = max(0, min(len(images) - 1, idx))
    data = _get_image_bytes(idx)
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


@app.route("/api/approve", methods=["POST"])
def approve():
    global current_idx
    if not images:
        return "", 404
    data = request.json
    save_idx = current_idx
    threading.Thread(target=_save_crop, args=(save_idx, data["cx"], data["cy"], data["cw"]), daemon=True).start()
    current_idx += 1
    if current_idx >= len(images):
        return jsonify({"done": True})
    return jsonify({"done": False})


@app.route("/api/navigate", methods=["POST"])
def navigate():
    global current_idx
    direction = request.json.get("direction", 1)
    current_idx += direction
    current_idx = max(0, min(len(images) - 1, current_idx))
    return jsonify({"ok": True})


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Interactive 9:20 Cropper</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #121212; color: #fff; font-family: Consolas, monospace; overflow: hidden; height: 100vh; }
  #wrap { position: relative; width: 100%; height: 100vh; display: flex; align-items: center; justify-content: center; }
  canvas { display: block; }
  #hud { position: absolute; top: 16px; left: 16px; pointer-events: none; font-size: 13px; line-height: 1.6; white-space: pre; text-shadow: 0 1px 4px #000; }
</style>
</head>
<body>
<div id="wrap">
  <canvas id="c"></canvas>
  <div id="hud"></div>
</div>
<script>
const canvas = document.getElementById("c");
const ctx = canvas.getContext("2d");
const hud = document.getElementById("hud");
const RATIO = 9 / 20;

let info = null;
let img = null;
let dispW = 0, dispH = 0, offX = 0, offY = 0;
let dragging = false, dragSX = 0, dragSY = 0;

// Client-side crop state
let cx = 0.5, cy = 0.5, cw = 0.8;

// Pre-fetch cache: Map<idx, { img: Image, info: Object, baseCanvas: OffscreenCanvas|null }>
const prefetchCache = new Map();
const inflight = new Map();
const PREFETCH_RANGE = 5;
let lastCanvasW = 0, lastCanvasH = 0;

// Scroll acceleration
let scrollCount = 0;
let scrollTimer = null;

function constrainCrop() {
  if (!info) return;
  const max_cw = Math.min(1.0, info.max_cw);
  cw = Math.max(0.02, Math.min(max_cw, cw));
  const hw = cw / 2;
  const hh = (cw * info.img_w / RATIO) / info.img_h / 2;
  cx = Math.max(hw, Math.min(1.0 - hw, cx));
  cy = Math.max(hh, Math.min(1.0 - hh, cy));
}

function resetCrop() {
  cx = 0.5;
  cy = 0.5;
  cw = info.max_cw * 0.9;
  constrainCrop();
}

function renderBaseCanvas(image, pw, ph) {
  const oc = new OffscreenCanvas(pw, ph);
  const octx = oc.getContext("2d");
  const scale = Math.min(pw / image.width, ph / image.height);
  const dW = Math.round(image.width * scale);
  const dH = Math.round(image.height * scale);
  const oX = Math.round((pw - dW) / 2);
  const oY = Math.round((ph - dH) / 2);
  // Full image at display size (used for crop punch-out)
  const imgCanvas = new OffscreenCanvas(pw, ph);
  const imgCtx = imgCanvas.getContext("2d");
  imgCtx.drawImage(image, oX, oY, dW, dH);
  // Dimmed version (base layer)
  octx.drawImage(imgCanvas, 0, 0);
  octx.fillStyle = "rgba(0,0,0,0.6)";
  octx.fillRect(oX, oY, dW, dH);
  return { oc, imgCanvas, dW, dH, oX, oY };
}

function invalidateBaseCache() {
  for (const entry of prefetchCache.values()) { entry.baseCanvas = null; entry.imgCanvas = null; }
}

function getBase(idx, pw, ph) {
  const entry = prefetchCache.get(idx);
  if (!entry) return null;
  if (entry.baseCanvas && entry.baseW === pw && entry.baseH === ph) {
    return entry.baseCanvas;
  }
  const { oc, imgCanvas, dW, dH, oX, oY } = renderBaseCanvas(entry.img, pw, ph);
  entry.baseCanvas = oc;
  entry.imgCanvas = imgCanvas;
  entry.baseW = pw;
  entry.baseH = ph;
  entry._dW = dW;
  entry._dH = dH;
  entry._oX = oX;
  entry._oY = oY;
  return oc;
}

async function loadState() {
  const r = await fetch("/api/info");
  info = await r.json();
  resetCrop();
  const cached = prefetchCache.get(info.index);
  if (cached) {
    img = cached.img;
    draw();
  } else {
    await loadImage();
  }
  startPrefetch();
}

function loadImageFromIdx(idx) {
  if (prefetchCache.has(idx)) return Promise.resolve();
  if (inflight.has(idx)) return inflight.get(idx);
  const p = (async () => {
    const [infoR, imgR] = await Promise.all([
      fetch(`/api/info?idx=${idx}`),
      fetch(`/api/image?idx=${idx}`),
    ]);
    const infoData = await infoR.json();
    const blob = await imgR.blob();
    const url = URL.createObjectURL(blob);
    return new Promise(resolve => {
      const i = new Image();
      i.onload = () => { URL.revokeObjectURL(url); prefetchCache.set(idx, { img: i, info: infoData, baseCanvas: null, baseW: 0, baseH: 0 }); inflight.delete(idx); resolve(); };
      i.src = url;
    });
  })().catch(() => { inflight.delete(idx); });
  inflight.set(idx, p);
  return p;
}

function startPrefetch() {
  if (!info) return;
  const cur = info.index;
  for (let d = -PREFETCH_RANGE; d <= PREFETCH_RANGE; d++) {
    const idx = cur + d;
    if (idx >= 0 && idx < info.total && idx !== cur && !prefetchCache.has(idx)) {
      loadImageFromIdx(idx);
    }
  }
  // Prune entries outside range to limit memory
  for (const [k] of prefetchCache) {
    if (Math.abs(k - cur) > PREFETCH_RANGE + 2) prefetchCache.delete(k);
  }
}

async function loadImage() {
  if (inflight.has(info.index)) {
    await inflight.get(info.index);
    const cached = prefetchCache.get(info.index);
    if (cached) { img = cached.img; draw(); return; }
  }
  if (prefetchCache.has(info.index)) {
    const cached = prefetchCache.get(info.index);
    img = cached.img;
    draw();
    return;
  }
  const r = await fetch("/api/image");
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const i = new Image();
  return new Promise(resolve => {
    i.onload = () => { URL.revokeObjectURL(url); img = i; prefetchCache.set(info.index, { img: i, info, baseCanvas: null, baseW: 0, baseH: 0 }); draw(); resolve(); };
    i.src = url;
  });
}

function draw() {
  if (!img || !info) return;
  const pw = canvas.parentElement.clientWidth;
  const ph = canvas.parentElement.clientHeight;
  const sizeChanged = (canvas.width !== pw || canvas.height !== ph);
  if (sizeChanged) {
    canvas.width = pw;
    canvas.height = ph;
    invalidateBaseCache();
  }

  const base = getBase(info.index, pw, ph);
  if (!base) return;
  const entry = prefetchCache.get(info.index);
  dispW = entry._dW;
  dispH = entry._dH;
  offX = entry._oX;
  offY = entry._oY;

  ctx.clearRect(0, 0, pw, ph);
  ctx.drawImage(base, 0, 0);

  // Crop box in canvas pixels
  const cropPx = cw * dispW;
  const cropPy = cropPx / RATIO;
  const cropCx = cx * dispW + offX;
  const cropCy = cy * dispH + offY;
  const rx = cropCx - cropPx / 2;
  const ry = cropCy - cropPy / 2;

  // Punch out crop region (draw pre-rendered image in clip)
  ctx.save();
  ctx.beginPath();
  ctx.rect(rx, ry, cropPx, cropPy);
  ctx.clip();
  ctx.drawImage(entry.imgCanvas, 0, 0);
  ctx.restore();

  // Yellow border
  ctx.strokeStyle = "#ffcc00";
  ctx.lineWidth = 2;
  ctx.strokeRect(rx, ry, cropPx, cropPy);

  // Green checkmark if crop already saved
  if (info.has_crop) {
    const s = 28, pad = 16;
    const cx2 = pw - pad - s / 2, cy2 = pad + s / 2;
    ctx.beginPath();
    ctx.arc(cx2, cy2, s / 2, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(34,180,34,0.9)";
    ctx.fill();
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(cx2 - 7, cy2);
    ctx.lineTo(cx2 - 2, cy2 + 6);
    ctx.lineTo(cx2 + 8, cy2 - 6);
    ctx.stroke();
  }

  hud.textContent =
    `Image ${info.index + 1}/${info.total}\n` +
    `File: ${info.filename}\n\n` +
    `Controls:\n` +
    `  [Left-Click + Drag] : Slide Crop Box\n` +
    `  [Scroll Wheel]      : Zoom Crop Box\n` +
    `  [Space] / [Enter]   : Approve & Save\n` +
    `  [Left] / [Right]    : Navigate without saving`;
}

async function approve() {
  const saveCx = cx, saveCy = cy, saveCw = cw;
  const nextIdx = info.index + 1;
  fetch("/api/approve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cx: saveCx, cy: saveCy, cw: saveCw }),
  });
  if (nextIdx >= info.total) {
    hud.textContent = "Processing complete!";
    return;
  }
  const cached = prefetchCache.get(nextIdx);
  if (cached) {
    img = cached.img;
    info = cached.info;
    resetCrop();
    draw();
    startPrefetch();
  } else {
    await loadState();
  }
}

async function navigate(dir) {
  const nextIdx = info.index + dir;
  if (nextIdx < 0 || nextIdx >= info.total) return;
  fetch("/api/navigate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ direction: dir }),
  });
  const cached = prefetchCache.get(nextIdx);
  if (cached) {
    img = cached.img;
    info = cached.info;
    resetCrop();
    draw();
    startPrefetch();
  } else {
    await loadState();
  }
}

// --- Mouse ---
canvas.addEventListener("mousedown", e => {
  dragging = true;
  dragSX = e.clientX;
  dragSY = e.clientY;
});

window.addEventListener("mousemove", e => {
  if (!dragging) return;
  const dx = e.clientX - dragSX;
  const dy = e.clientY - dragSY;
  cx += dx / dispW;
  cy += dy / dispH;
  dragSX = e.clientX;
  dragSY = e.clientY;
  constrainCrop();
  draw();
});

window.addEventListener("mouseup", () => { dragging = false; });

canvas.addEventListener("wheel", e => {
  e.preventDefault();
  // Accelerating zoom: 1x, 1.5x, 2x, 2.5x, 3x capped
  scrollCount++;
  clearTimeout(scrollTimer);
  scrollTimer = setTimeout(() => { scrollCount = 0; }, 300);
  const speed = Math.min(3, 0.5 + scrollCount * 0.5);
  const base = 0.95;
  const factor = e.deltaY < 0 ? Math.pow(base, speed) : Math.pow(1 / base, speed);
  cw *= factor;
  constrainCrop();
  draw();
}, { passive: false });

// --- Keyboard ---
window.addEventListener("keydown", e => {
  if (e.key === " " || e.key === "Enter") { e.preventDefault(); approve(); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); navigate(-1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); navigate(1); }
});

window.addEventListener("resize", draw);
loadState();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python web_crop.py <input_folder>")
        sys.exit(1)
    input_dir = sys.argv[1]
    output_dir = os.path.join(input_dir, "cropped_9_20")
    valid_exts = {".jpg", ".jpeg", ".png", ".webp", ".tiff"}
    images = sorted([
        os.path.join(input_dir, f) for f in os.listdir(input_dir)
        if os.path.splitext(f)[1].lower() in valid_exts
    ])
    if not images:
        print("No images found.")
        sys.exit(1)
    print(f"Found {len(images)} images. Opening http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
