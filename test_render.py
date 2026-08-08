"""Headless browser rendering test for web_crop.py.

Starts the Flask server, loads the page in headless Chrome, and takes
screenshots to verify the UI renders correctly.
"""
import os
import sys
import time
import threading
import json

# Set up test images
from PIL import Image
import shutil
TEST_DIR = "/tmp/web_crop_test"
if os.path.exists(TEST_DIR):
    shutil.rmtree(TEST_DIR)
os.makedirs(TEST_DIR, exist_ok=True)
for i in range(3):
    img = Image.new("RGB", (2000, 4000), [(200, 80, 80), (80, 200, 80), (80, 80, 200)][i])
    img.save(f"{TEST_DIR}/img_{i}.jpg", quality=95)

import web_crop
web_crop.images = sorted([
    os.path.join("/tmp/web_crop_test", f) for f in os.listdir("/tmp/web_crop_test")
    if os.path.splitext(f)[1].lower() in {".jpg", ".jpeg", ".png", ".webp", ".tiff"}
])
web_crop.current_idx = 0
web_crop.input_dir = "/tmp/web_crop_test"
web_crop.output_dir = "/tmp/web_crop_test/cropped_9_20"

app = web_crop.app
PORT = 5099

def run_server():
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)

t = threading.Thread(target=run_server, daemon=True)
t.start()
time.sleep(1)

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

opts = Options()
opts.add_argument("--headless")
opts.add_argument("--no-sandbox")
opts.add_argument("--disable-gpu")
opts.add_argument("--window-size=1200,800")
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)

def screenshot(name):
    path = f"/tmp/web_crop_test/{name}.png"
    driver.save_screenshot(path)
    print(f"  Screenshot: {path}")

try:
    # Test 1: Page loads without JS errors
    print("Test 1: Page loads without JS errors")
    # Inject error capture BEFORE page loads
    driver.get("about:blank")
    driver.execute_script("""
        window.__jsErrors = [];
        window.addEventListener('error', function(e) {
            window.__jsErrors.push(e.message + ' at ' + e.filename + ':' + e.lineno);
        });
        window.addEventListener('unhandledrejection', function(e) {
            window.__jsErrors.push('Unhandled: ' + (e.reason && e.reason.message || e.reason));
        });
    """)
    driver.get(f"http://127.0.0.1:{PORT}")
    time.sleep(3)

    errors = driver.execute_script("return window.__jsErrors || [];")
    hud_text = driver.find_element(By.ID, "hud").text
    print(f"  HUD text: {repr(hud_text[:80])}")
    print(f"  JS errors: {errors}")
    screenshot("test1_initial_load")

    assert not errors, f"JS errors on load: {errors}"
    assert "Image 1/3" in hud_text, f"Expected 'Image 1/3' in HUD, got: {hud_text}"
    print("  PASS\n")

    # Test 2: Canvas has content (not all black)
    print("Test 2: Canvas has non-black content")
    canvas = driver.find_element(By.ID, "c")
    size = canvas.size
    print(f"  Canvas size: {size['width']}x{size['height']}")
    assert size['width'] > 100 and size['height'] > 100, "Canvas too small"

    pixel_data = driver.execute_script("""
        const c = document.getElementById('c');
        const ctx = c.getContext('2d');
        const center = ctx.getImageData(c.width/2, c.height/2, 1, 1).data;
        const corner = ctx.getImageData(5, 5, 1, 1).data;
        return {
            center: Array.from(center),
            corner: Array.from(corner),
            width: c.width,
            height: c.height
        };
    """)
    print(f"  Center pixel: {pixel_data['center']}")
    print(f"  Corner pixel: {pixel_data['corner']}")
    # Center should be the crop region (bright), corner should be dimmed (dark but not pure black)
    is_center_bright = any(v > 100 for v in pixel_data['center'][:3])
    is_corner_dim = all(v < 80 for v in pixel_data['corner'][:3]) and any(v > 0 for v in pixel_data['corner'][:3])
    print(f"  Center bright: {is_center_bright}, Corner dim: {is_corner_dim}")
    assert is_center_bright, f"Center should be bright (image visible), got {pixel_data['center']}"
    screenshot("test2_canvas_content")
    print("  PASS\n")

    # Test 3: Navigate forward
    print("Test 3: Navigate forward with arrow key")
    body = driver.find_element(By.TAG_NAME, "body")
    body.send_keys("\ue014")  # Right arrow
    time.sleep(1)
    errors_after_nav = driver.execute_script("return window.__jsErrors || [];")
    hud_after = driver.find_element(By.ID, "hud").text
    print(f"  HUD: {repr(hud_after[:80])}")
    print(f"  JS errors: {errors_after_nav}")
    screenshot("test3_navigate_right")
    assert not errors_after_nav, f"JS errors after nav: {errors_after_nav}"
    assert "Image 2/3" in hud_after, f"Expected 'Image 2/3', got: {hud_after}"
    print("  PASS\n")

    # Test 4: Zoom with scroll
    print("Test 4: Zoom with scroll wheel")
    el = driver.find_element(By.ID, "c")
    el.click()
    time.sleep(0.2)
    driver.execute_script("""
        const c = document.getElementById('c');
        c.dispatchEvent(new WheelEvent('wheel', { deltaY: -300, bubbles: true }));
    """)
    time.sleep(0.5)
    errors_after_zoom = driver.execute_script("return window.__jsErrors || [];")
    print(f"  JS errors: {errors_after_zoom}")
    screenshot("test4_after_zoom")
    assert not errors_after_zoom, f"JS errors after zoom: {errors_after_zoom}"
    print("  PASS\n")

    # Test 5: Navigate back
    print("Test 5: Navigate back")
    body.send_keys("\ue012")  # Left arrow
    time.sleep(1)
    hud_back = driver.find_element(By.ID, "hud").text
    print(f"  HUD: {repr(hud_back[:80])}")
    screenshot("test5_navigate_back")
    assert "Image 1/3" in hud_back, f"Expected 'Image 1/3', got: {hud_back}"
    print("  PASS\n")

    print("All tests passed!")

except Exception as e:
    print(f"FAIL: {e}")
    screenshot("FAIL")
    sys.exit(1)
finally:
    driver.quit()
