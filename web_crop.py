import os
import io
import threading
import numpy as np
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

# Auto-rotation (horizon) detection state
ROT_PREFETCH_AHEAD = 8
ROT_MAX_DIM = 800
_rotation_cache = {}  # idx -> angle (deg, canvas convention) or None
_rotation_computing = set()
_rotation_lock = threading.Lock()


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
        entry = _info_cache[idx]
    else:
        img = Image.open(images[idx])
        img_w, img_h = img.size
        max_cw = min(1.0, (img_h * ratio) / img_w)
        entry = {
            "index": idx,
            "total": len(images),
            "filename": os.path.basename(images[idx]),
            "img_w": img_w,
            "img_h": img_h,
            "max_cw": max_cw,
        }
        _info_cache[idx] = entry
    # has_crop is recomputed on every call so the green tick stays accurate
    # even if the crop file appears after the entry was first cached.
    dest = os.path.join(output_dir, os.path.basename(images[idx]))
    entry["has_crop"] = os.path.exists(dest)
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


def _detect_rotation(idx):
    """Return the rotation (degrees, canvas convention: positive = clockwise)
    that makes the dominant near-horizontal line horizontal, or None if no
    horizon is detected. Hough transform to find candidate angles, RANSAC to
    refine the angle of the strongest supporting line."""
    img = Image.open(images[idx])
    img = ImageOps.exif_transpose(img).convert("L")
    img.thumbnail((ROT_MAX_DIM, ROT_MAX_DIM))
    arr = np.asarray(img, dtype=np.float32)
    h, w = arr.shape
    if h < 50 or w < 50:
        return None

    # Gradient magnitude (central differences) as edge strength
    gx = np.zeros_like(arr)
    gy = np.zeros_like(arr)
    gx[1:-1, 1:-1] = (arr[1:-1, 2:] - arr[1:-1, :-2]) * 0.5
    gy[1:-1, 1:-1] = (arr[2:, 1:-1] - arr[:-2, 1:-1]) * 0.5
    mag = np.hypot(gx, gy)
    mask = mag > max(np.percentile(mag, 96), 0.1 * mag.max())
    if mask.sum() < 300:
        return None
    ys, xs = np.nonzero(mask)
    gxe = gx[mask]; gye = gy[mask]; mg = mag[mask]

    # Line angle (0..180) is perpendicular to the gradient
    g_ang = np.degrees(np.arctan2(gye, gxe))
    line_ang = (g_ang + 90.0) % 180.0

    # 1D Hough: angle accumulator weighted by gradient magnitude, smoothed
    hist, _ = np.histogram(line_ang, bins=180, range=(0, 180), weights=mg)
    kernel = np.array([1, 2, 3, 2, 1], dtype=float)
    hist = np.convolve(hist, kernel / kernel.sum(), mode="same")

    # Strongest peak near horizontal (within 60 deg of 0/180). Accept only if
    # it is a clear dominant orientation (well above the local mean) with real
    # support, so texture/noise without a horizon is rejected.
    near = [i for i in range(180) if min(i + 0.5, 180 - (i + 0.5)) <= 60]
    mean_near = float(np.mean(hist[near]))
    best_score = 0
    best_center = None
    for i in near:
        if hist[i] > best_score:
            best_score = hist[i]
            best_center = i + 0.5
    if best_center is None or best_score < 2.0 * mean_near:
        return None

    # RANSAC: fit best line among edge pixels near the peak angle
    tol = 12
    band = np.abs(((line_ang - best_center + 90) % 180) - 90) <= tol
    xi = xs[band]; yi = ys[band]; ai = line_ang[band]; wi = mg[band]
    if len(xi) < 30:
        return -round(best_center if best_center <= 90 else best_center - 180, 1)
    pts = np.stack([xi.astype(np.float32), yi.astype(np.float32)], axis=1)
    dirs = np.stack([np.cos(np.radians(ai)), np.sin(np.radians(ai))], axis=1)
    n = len(xi)
    rng = np.random.default_rng(0)
    best_inl = 0
    best_ang = best_center
    for _ in range(200):
        j = rng.integers(0, n)
        p0 = pts[j]; d = dirs[j]
        off = pts - p0
        dist = np.abs(off[:, 0] * d[1] - off[:, 1] * d[0])
        inl = dist < 2.5
        cnt = int(inl.sum())
        if cnt > best_inl:
            best_inl = cnt
            a = ai[inl]; w = wi[inl]
            c = float(np.sum(w * np.cos(np.radians(2 * a))))
            s = float(np.sum(w * np.sin(np.radians(2 * a))))
            best_ang = (np.degrees(np.arctan2(s, c)) / 2.0) % 180.0
    if best_inl < 0.005 * mask.sum():
        return None
    return -round(best_ang if best_ang <= 90 else best_ang - 180, 1)


def _compute_rotation(idx):
    try:
        ang = _detect_rotation(idx)
    except Exception:
        ang = None
    with _rotation_lock:
        _rotation_cache[idx] = ang
        _rotation_computing.discard(idx)


def _prefetch_rotations():
    """Kick off background detection for the current image and the next few
    ahead. Returns immediately so the client is never blocked."""
    if not images:
        return
    with _rotation_lock:
        cur = current_idx
        total = len(images)
    start = cur
    end = min(total, cur + ROT_PREFETCH_AHEAD + 1)
    for idx in range(start, end):
        with _rotation_lock:
            if idx in _rotation_cache or idx in _rotation_computing:
                continue
            _rotation_computing.add(idx)
        threading.Thread(target=_compute_rotation, args=(idx,), daemon=True).start()


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


@app.route("/api/rotation")
def get_rotation():
    if not images:
        return jsonify({"error": "no images"}), 404
    idx = int(request.args.get("idx", current_idx))
    idx = max(0, min(len(images) - 1, idx))
    with _rotation_lock:
        computing = idx in _rotation_computing
        cached = idx in _rotation_cache
    if not computing and not cached:
        with _rotation_lock:
            _rotation_computing.add(idx)
        threading.Thread(target=_compute_rotation, args=(idx,), daemon=True).start()
        computing = True
    with _rotation_lock:
        ang = _rotation_cache.get(idx)
    return jsonify({"index": idx, "rotation": ang, "computing": computing})


@app.route("/api/approve", methods=["POST"])
def approve():
    global current_idx
    if not images:
        return "", 404
    data = request.json
    save_idx = current_idx
    threading.Thread(target=_save_crop, args=(save_idx, data["cx"], data["cy"], data["cw"], data.get("rotation", 0)), daemon=True).start()
    current_idx += 1
    _prefetch_rotations()
    if current_idx >= len(images):
        return jsonify({"done": True})
    return jsonify({"done": False})


@app.route("/api/navigate", methods=["POST"])
def navigate():
    global current_idx
    direction = request.json.get("direction", 1)
    current_idx += direction
    current_idx = max(0, min(len(images) - 1, current_idx))
    _prefetch_rotations()
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
  #rotbar { position: absolute; bottom: 20px; left: 50%; transform: translateX(-50%); width: 60%; height: 36px; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.25); border-radius: 18px; cursor: pointer; touch-action: none; user-select: none; }
  #rotbar .center-line { position: absolute; top: 6px; bottom: 6px; left: 50%; width: 1px; background: rgba(255,255,255,0.3); }
  #rotknob { position: absolute; top: 50%; left: 50%; width: 24px; height: 24px; border-radius: 50%; background: #ffcc00; transform: translate(-50%, -50%); pointer-events: none; box-shadow: 0 0 8px rgba(0,0,0,0.6); }
</style>
</head>
<body>
<div id="wrap">
  <canvas id="c"></canvas>
  <div id="hud"></div>
  <div id="rotbar"><div class="center-line"></div><div id="rotknob"></div></div>
</div>
<script>
const canvas = document.getElementById("c");
const ctx = canvas.getContext("2d");
const hud = document.getElementById("hud");
const rotbar = document.getElementById("rotbar");
const rotKnob = document.getElementById("rotknob");
const RATIO = 9 / 20;
const CROP_FRAC = 0.95;
const MAX_ROT = 90;

let info = null;
let img = null;
let dragging = false, dragSX = 0, dragSY = 0;
let rotDragging = false;

// View state: zoom level, pan offset (canvas pixels), rotation (degrees)
let zoom = 1.0, panX = 0, panY = 0, rotation = 0;

// Display geometry (recalculated each draw)
let pw = 0, ph = 0, imgScale = 0, imgOffX = 0, imgOffY = 0;
let cropPx = 0, cropPy = 0, rx = 0, ry = 0;
let rotW = 0, rotH = 0;
let baseScale = 0;

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
  // Scale is defined from the UNROTATED image so zoom is stable while rotating
  baseScale = Math.min(pw / img.width, ph / img.height);
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

function setRotation(deg) {
  rotation = Math.max(-MAX_ROT, Math.min(MAX_ROT, deg));
  fitCropToImage();
}

// Remove black space in the crop: first try panning alone (works when the
// crop box fits inside the rotated image and black is only on one side).
// Only zoom when the crop cannot fit at the current zoom (e.g. black on
// both top and bottom).
function fitCropToImage() {
  if (!img) return;
  calcGeometry();
  const rad = rotation * Math.PI / 180;
  const cosA = Math.abs(Math.cos(rad));
  const sinA = Math.abs(Math.sin(rad));
  // Projected extent of the rotated crop box in image-local axes
  const bw = cropPx * cosA + cropPy * sinA;
  const bh = cropPx * sinA + cropPy * cosA;
  const baseS = baseScale;
  // Zoom only if the crop cannot fit the image at the current zoom
  const needZ = Math.max(bw / (img.width * baseS), bh / (img.height * baseS));
  if (needZ > zoom) zoom = needZ;
  // Recompute geometry, then clamp pan so the crop stays inside the image
  calcGeometry();
  const cos = Math.cos(rad), sin = Math.sin(rad);
  const tx = panX * cos + panY * sin;
  const ty = -panX * sin + panY * cos;
  const iw = img.width * imgScale;
  const ih = img.height * imgScale;
  const maxTx = (iw - bw) / 2;
  const maxTy = (ih - bh) / 2;
  const txc = Math.max(-maxTx, Math.min(maxTx, tx));
  const tyc = Math.max(-maxTy, Math.min(maxTy, ty));
  panX = txc * cos - tyc * sin;
  panY = txc * sin + tyc * cos;
  constrainView();
  calcGeometry();
  draw();
}

function zoomToFitCrop() {
  if (!img) return;
  const rad = rotation * Math.PI / 180;
  const cos = Math.cos(rad), sin = Math.sin(rad);
  const cw = cropPx / 2, ch = cropPy / 2;
  const baseS = baseScale;
  let need = zoom;
  for (const sx of [cw, -cw]) {
    for (const sy of [ch, -ch]) {
      const relx = sx - panX;
      const rely = sy - panY;
      const rx = relx * cos + rely * sin;
      const ry = -relx * sin + rely * cos;
      need = Math.max(need,
        2 * Math.abs(rx) / (img.width * baseS),
        2 * Math.abs(ry) / (img.height * baseS));
    }
  }
  zoom = need;
  constrainView();
  calcGeometry();
  draw();
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

  if (rotDragging) {
    // While rotating, extend guide lines across the whole image
    const imgLeft = imgOffX, imgRight = imgOffX + imgW;
    const imgTop = imgOffY, imgBottom = imgOffY + imgH;
    const vxs = [rx, rx + cropPx / 3, rx + cropPx * 2 / 3, rx + cropPx / 2, rx + cropPx];
    const hys = Array.from({ length: 9 }, (_, i) => ry + cropPy * i / 9);
    ctx.strokeStyle = "rgba(255,255,255,0.25)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const vx of vxs) { ctx.moveTo(vx, imgTop); ctx.lineTo(vx, imgBottom); }
    for (const hy of hys) { ctx.moveTo(imgLeft, hy); ctx.lineTo(imgRight, hy); }
    ctx.stroke();
    ctx.strokeStyle = "rgba(255,255,255,0.5)";
    ctx.beginPath();
    ctx.moveTo(rx + cropPx / 2, imgTop);
    ctx.lineTo(rx + cropPx / 2, imgBottom);
    ctx.stroke();
  } else {
    // Normal guide lines within crop box
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
  }

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

  const rb = rotbar.getBoundingClientRect();
  const frac = Math.max(-1, Math.min(1, rotation / MAX_ROT));
  rotKnob.style.left = `${rb.width / 2 + frac * (rb.width / 2)}px`;

  hud.textContent =
    `Image ${info.index + 1}/${info.total}\n` +
    `File: ${info.filename}\n` +
    (rotation ? `Rotation: ${rotation.toFixed(1)}°\n` : '') +
    `\n` +
    `Controls:\n` +
    `  [Left-Click + Drag] : Pan Image\n` +
    `  [Scroll Wheel]      : Zoom In/Out\n` +
    `  [Rotate Bar]        : Rotate Image\n` +
    `  [R]                 : Auto-Rotate to Horizon\n` +
    `  [Z]                 : Zoom to Remove Black Space\n` +
    `  [Space] / [Enter]   : Approve & Save\n` +
    `  [Left] / [Right]    : Navigate without saving`;
}

async function refreshInfo() {
  const r = await fetch(`/api/info?idx=${info.index}`);
  const fresh = await r.json();
  if (fresh.index === info.index) {
    info.has_crop = fresh.has_crop;
    draw();
  }
}

async function approve() {
  const cp = getCropParams();
  const savedIdx = info.index;
  const nextIdx = savedIdx + 1;
  fetch("/api/approve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cp),
  });
  // The saved image now has a crop; update any cached info so the tick shows
  // when navigating back to it.
  const savedCached = prefetchCache.get(savedIdx);
  if (savedCached) savedCached.info.has_crop = true;
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
  refreshInfo();
}

async function autoRotate() {
  let data = null;
  for (let i = 0; i < 20; i++) {  // poll up to ~6s for background compute
    const r = await fetch(`/api/rotation?idx=${info.index}`);
    data = await r.json();
    if (data.rotation != null) { setRotation(data.rotation); return; }
    if (!data.computing) break;
    await new Promise(res => setTimeout(res, 300));
  }
  hud.textContent = `No horizon detected for this image.`;
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
  refreshInfo();
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
  const factor = e.deltaY < 0 ? Math.pow(1 / 0.95, speed) : Math.pow(0.95, speed);
  panX *= factor;
  panY *= factor;
  zoom *= factor;
  constrainView();
  draw();
}, { passive: false });

// --- Rotate Bar ---
function rotAngleFromX(clientX) {
  const rect = rotbar.getBoundingClientRect();
  const half = rect.width / 2;
  const frac = (clientX - (rect.left + half)) / half;
  return frac * MAX_ROT;
}

rotbar.addEventListener("pointerdown", e => {
  rotDragging = true;
  rotbar.setPointerCapture(e.pointerId);
  setRotation(rotAngleFromX(e.clientX));
});
rotbar.addEventListener("pointermove", e => {
  if (!rotDragging) return;
  setRotation(rotAngleFromX(e.clientX));
});
rotbar.addEventListener("pointerup", () => { rotDragging = false; draw(); });
rotbar.addEventListener("pointercancel", () => { rotDragging = false; draw(); });

// --- Keyboard ---
window.addEventListener("keydown", e => {
  if (e.key === " " || e.key === "Enter") { e.preventDefault(); approve(); }
  else if (e.key === "r" || e.key === "R") { e.preventDefault(); autoRotate(); }
  else if (e.key === "z" || e.key === "Z") { e.preventDefault(); zoomToFitCrop(); }
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
    _prefetch_rotations()
    print(f"Found {len(images)} images. Opening http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
