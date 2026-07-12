import time
import threading
import requests
from PIL import Image
import os

# Generate test images
os.makedirs("/tmp/bench_imgs", exist_ok=True)
for i in range(5):
    img = Image.new("RGB", (4000, 6000), (i * 50, 100, 200))
    img.save(f"/tmp/bench_imgs/img_{i}.jpg", quality=95)

import web_crop
web_crop.input_dir = "/tmp/bench_imgs"
web_crop.output_dir = "/tmp/bench_imgs/cropped_9_20"
valid_exts = {".jpg", ".jpeg", ".png", ".webp", ".tiff"}
web_crop.images = sorted([
    os.path.join("/tmp/bench_imgs", f) for f in os.listdir("/tmp/bench_imgs")
    if os.path.splitext(f)[1].lower() in valid_exts
])
web_crop.current_idx = 0
app = web_crop.app

# Run flask in a thread
def run_app():
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)

t = threading.Thread(target=run_app, daemon=True)
t.start()
time.sleep(1)

BASE = "http://127.0.0.1:5001"
s = requests.Session()

def bench(label, method, path, **kwargs):
    times = []
    for _ in range(3):
        t0 = time.perf_counter()
        r = getattr(s, method)(f"{BASE}{path}", **kwargs)
        elapsed = (time.perf_counter() - t0) * 1000
        times.append(elapsed)
    avg = sum(times) / len(times)
    print(f"{label:40s} avg={avg:7.1f}ms  [{r.status_code}]")
    return r

print("=== Endpoint benchmarks (3 runs each) ===\n")

bench("GET /api/info", "get", "/api/info")
bench("GET /api/info?idx=0", "get", "/api/info?idx=0")
bench("GET /api/image (4000x6000)", "get", "/api/image")
bench("GET /api/image?idx=1", "get", "/api/image?idx=1")

print()
r = bench("POST /api/approve (server saves)", "post", "/api/approve",
           json={"cx": 0.5, "cy": 0.5, "cw": 0.7})
print(f"    Response: {r.json()}")

print()
bench("POST /api/navigate +1", "post", "/api/navigate",
       json={"direction": 1})
bench("POST /api/navigate -1", "post", "/api/navigate",
       json={"direction": -1})

print()
bench("GET /api/info (idx=2)", "get", "/api/info?idx=2")
bench("GET /api/image (idx=2)", "get", "/api/image?idx=2")

print("\n=== Prefetch simulation ===")

# Simulate what the client does: fetch info+image in parallel for next image
t0 = time.perf_counter()
r1, r2 = s.get(f"{BASE}/api/info?idx=1"), s.get(f"{BASE}/api/image?idx=1")
parallel_time = (time.perf_counter() - t0) * 1000
print(f"  Parallel fetch info+image:  {parallel_time:.1f}ms")

t0 = time.perf_counter()
s.get(f"{BASE}/api/info?idx=1")
s.get(f"{BASE}/api/image?idx=1")
serial_time = (time.perf_counter() - t0) * 1000
print(f"  Serial fetch info+image:    {serial_time:.1f}ms")

# Simulate approve + instant prefetch swap
t0 = time.perf_counter()
s.post(f"{BASE}/api/approve", json={"cx": 0.5, "cy": 0.5, "cw": 0.7})
approve_time = (time.perf_counter() - t0) * 1000
print(f"  Approve (fire-and-forget):  {approve_time:.1f}ms")

# Time what prefetch would do in background
t0 = time.perf_counter()
s.get(f"{BASE}/api/info?idx=2")
s.get(f"{BASE}/api/image?idx=2")
prefetch_time = (time.perf_counter() - t0) * 1000
print(f"  Prefetch next image:        {prefetch_time:.1f}ms")
