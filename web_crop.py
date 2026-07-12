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


def _save_crop(idx, cx, cy, cw, rotation=0):
    original = Image.open(images[idx])
    fmt = original.format
    img = ImageOps.exif_transpose(original)
    if rotation:
        img = img.rotate(-rotation, expand=True)
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
    threading.Thread(target=_save_crop, args=(save_idx, data["cx"], data["cy"], data["cw"], data.get("rotation", 0)), daemon=True).start()
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
const CROP_FRAC = 0.95;

let info = null;
let img = null;
let dragging = false, dragSX = 0, dragSY = 0;

// View state: zoom level, pan offset (canvas pixels), rotation (degrees)
let zoom = 1.0, panX = 0, panY = 0, rotation = 0;

// Display geometry (recalculated each draw)
let pw = 0, ph = 0, imgScale = 0, imgOffX = 0, imgOffY = 0;
let cropPx = 0, cropPy = 0, rx = 0, ry = 0;
let rotW = 0, rotH = 0;

// Pre-fetch cache
const prefetchCache = new Map();
const inflight = new Map();
const PREFETCH_RANGE = 5;

// Scroll acceleration
let scrollCount = 0;
let scrollTimer = null;

function calcGeometry() {
  pw = canvas.parentElement.clientWidth;
  ph = canvas.parentElement.clientHeight;
  canvas.width = pw;
  canvas.height = ph;
  if (!img) return;
  // Rotated bounding box
  const rad = rotation * Math.PI / 180;
  const cos = Math.abs(Math.cos(rad)), sin = Math.abs(Math.sin(rad));
  rotW = cos * img.width + sin * img.height;
  rotH = sin * img.width + cos * img.height;
  const baseScale = Math.min(pw / rotW, ph / rotH);
  imgScale = baseScale * zoom;
  const imgW = rotW * imgScale;
  const imgH = rotH * imgScale;
  imgOffX = (pw - imgW) / 2 + panX;
  imgOffY = (ph - imgH) / 2 + panY;
  // Size crop box to fit within canvas in both dimensions
  cropPx = Math.min(pw * CROP_FRAC, ph * RATIO * CROP_FRAC);
  cropPy = cropPx / RATIO;
  rx = (pw - cropPx) / 2;
  ry = (ph - cropPy) / 2;
}

function constrainView() {
  if (!img) return;
  // Use rotated bounds for min zoom
  const baseScale = Math.min(pw / rotW, ph / rotH);
  // Zoom: image must be large enough that crop box fits inside it
  const minZoomX = cropPx / (baseScale * rotW);
  const minZoomY = cropPy / (baseScale * rotH);
  const minZoom = Math.max(minZoomX, minZoomY);
  if (zoom < minZoom) zoom = minZoom;
  // Recalc image size after zoom clamp
  const imgW2 = rotW * baseScale * zoom;
  const imgH2 = rotH * baseScale * zoom;
  const maxPanX = imgW2 / 2 - cropPx / 2;
  const maxPanY = imgH2 / 2 - cropPy / 2;
  panX = Math.max(-maxPanX, Math.min(maxPanX, panX));
  panY = Math.max(-maxPanY, Math.min(maxPanY, panY));
}

function resetView() {
  zoom = 1.0;
  panX = 0;
  panY = 0;
  rotation = 0;
  calcGeometry();
  constrainView();
  calcGeometry();
}

function getCropParams() {
  const cx_img = (pw / 2 - imgOffX) / imgScale;
  const cy_img = (ph / 2 - imgOffY) / imgScale;
  const cw_img = cropPx / imgScale;
  return {
    cx: cx_img / rotW,
    cy: cy_img / rotH,
    cw: cw_img / rotW,
    rotation: rotation,
  };
}

async function loadState() {
  const r = await fetch("/api/info");
  info = await r.json();
  const cached = prefetchCache.get(info.index);
  if (cached) {
    img = cached.img;
    resetView();
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
      i.onload = () => { URL.revokeObjectURL(url); prefetchCache.set(idx, { img: i, info: infoData }); inflight.delete(idx); resolve(); };
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
  for (const [k] of prefetchCache) {
    if (Math.abs(k - cur) > PREFETCH_RANGE + 2) prefetchCache.delete(k);
  }
}

async function loadImage() {
  if (inflight.has(info.index)) {
    await inflight.get(info.index);
    const cached = prefetchCache.get(info.index);
    if (cached) { img = cached.img; resetView(); draw(); return; }
  }
  if (prefetchCache.has(info.index)) {
    const cached = prefetchCache.get(info.index);
    img = cached.img;
    resetView();
    draw();
    return;
  }
  const r = await fetch("/api/image");
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const i = new Image();
  return new Promise(resolve => {
    i.onload = () => { URL.revokeObjectURL(url); img = i; prefetchCache.set(info.index, { img: i, info }); resetView(); draw(); resolve(); };
    i.src = url;
  });
}

function drawRotated(imgEl, imgScale, imgOffX, imgOffY, rotW, rotH, rotation) {
  const rad = rotation * Math.PI / 180;
  const cx = imgOffX + rotW * imgScale / 2;
  const cy = imgOffY + rotH * imgScale / 2;
  const dw = imgEl.width * imgScale;
  const dh = imgEl.height * imgScale;
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(rad);
  ctx.drawImage(imgEl, -dw / 2, -dh / 2, dw, dh);
  ctx.restore();
}

function draw() {
  if (!img || !info) return;
  calcGeometry();

  const imgW = rotW * imgScale;
  const imgH = rotH * imgScale;

  // Dark background
  ctx.fillStyle = "#121212";
  ctx.fillRect(0, 0, pw, ph);

  // Rotated image
  drawRotated(img, imgScale, imgOffX, imgOffY, rotW, rotH, rotation);

  // Dim overlay
  ctx.fillStyle = "rgba(0,0,0,0.6)";
  ctx.fillRect(0, 0, pw, ph);

  // Punch out crop region
  ctx.save();
  ctx.beginPath();
  ctx.rect(rx, ry, cropPx, cropPy);
  ctx.clip();
  drawRotated(img, imgScale, imgOffX, imgOffY, rotW, rotH, rotation);
  ctx.restore();

  // Yellow border
  ctx.strokeStyle = "#ffcc00";
  ctx.lineWidth = 2;
  ctx.strokeRect(rx, ry, cropPx, cropPy);

  // Guide lines
  ctx.strokeStyle = "rgba(255,255,255,0.5)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(rx + cropPx / 2, ry);
  ctx.lineTo(rx + cropPx / 2, ry + cropPy);
  ctx.stroke();
  ctx.strokeStyle = "rgba(255,255,255,0.25)";
  ctx.beginPath();
  ctx.moveTo(rx + cropPx / 3, ry);
  ctx.lineTo(rx + cropPx / 3, ry + cropPy);
  ctx.moveTo(rx + cropPx * 2 / 3, ry);
  ctx.lineTo(rx + cropPx * 2 / 3, ry + cropPy);
  ctx.moveTo(rx, ry + cropPy / 3);
  ctx.lineTo(rx + cropPx, ry + cropPy / 3);
  ctx.moveTo(rx, ry + cropPy * 2 / 3);
  ctx.lineTo(rx + cropPx, ry + cropPy * 2 / 3);
  ctx.stroke();

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
    `File: ${info.filename}\n` +
    (rotation ? `Rotation: ${rotation.toFixed(1)}°\n` : '') +
    `\n` +
    `Controls:\n` +
    `  [Left-Click + Drag] : Pan Image\n` +
    `  [Scroll Wheel]      : Zoom In/Out\n` +
    `  [Shift + Scroll]    : Rotate\n` +
    `  [Space] / [Enter]   : Approve & Save\n` +
    `  [Left] / [Right]    : Navigate without saving`;
}

async function approve() {
  const cp = getCropParams();
  const nextIdx = info.index + 1;
  fetch("/api/approve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cp),
  });
  if (nextIdx >= info.total) {
    hud.textContent = "Processing complete!";
    return;
  }
  const cached = prefetchCache.get(nextIdx);
  if (cached) {
    img = cached.img;
    info = cached.info;
    resetView();
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
    resetView();
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
  panX += e.clientX - dragSX;
  panY += e.clientY - dragSY;
  dragSX = e.clientX;
  dragSY = e.clientY;
  constrainView();
  draw();
});

window.addEventListener("mouseup", () => { dragging = false; });

canvas.addEventListener("wheel", e => {
  e.preventDefault();
  scrollCount++;
  clearTimeout(scrollTimer);
  scrollTimer = setTimeout(() => { scrollCount = 0; }, 300);
  const speed = Math.min(3, 0.5 + scrollCount * 0.5);
  if (e.shiftKey) {
    const deg = speed * (e.deltaY < 0 ? 1 : -1);
    rotation += deg;
    calcGeometry();
    constrainView();
    draw();
  } else {
    const factor = e.deltaY < 0 ? Math.pow(1 / 0.95, speed) : Math.pow(0.95, speed);
    panX *= factor;
    panY *= factor;
    zoom *= factor;
    constrainView();
    draw();
  }
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
    # Start at first image without an existing crop
    for i, path in enumerate(images):
        if not os.path.exists(os.path.join(output_dir, os.path.basename(path))):
            current_idx = i
            break
    print(f"Found {len(images)} images. Opening http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
