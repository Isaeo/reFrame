"""
eink-frame — server.py
======================
Flask server that polls a shared Google Drive folder and serves
processed images to ESPHome e-ink frames.

Dependencies: requirements.txt
Configuration: .env in the project root (see .env.example)
"""

import os
import io
import time
import random
import threading
import logging
from flask import Flask, request, jsonify, Response
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account
import numpy as np
from PIL import Image, ImageFilter
import pillow_heif
pillow_heif.register_heif_opener()  # adds HEIC/HEIF support to Pillow

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Config (all values come from .env via Docker or environment) ──────────────
#
#   FOLDER_ID        — Google Drive folder ID (last segment of the folder URL)
#   REFRESH_SECONDS  — how often the server re-polls Drive (default 1800 = 30 min)
#   SERVER_BASE_URL  — public URL of THIS server, no trailing slash
#                      e.g. http://YOUR_ORACLE_IP:5000
#   CREDS_FILE       — path to service_account.json inside the container
#
FOLDER_ID        = os.environ["FOLDER_ID"]                          # ← EDIT in .env
REFRESH_SECONDS  = int(os.environ.get("REFRESH_SECONDS", "1800"))
ROTATION_SECONDS = int(os.environ.get("ROTATION_SECONDS", "600"))  # how often to advance to next image
POOL_SIZE        = int(os.environ.get("POOL_SIZE", "20"))           # max images kept in memory
SERVER_BASE_URL  = os.environ["SERVER_BASE_URL"].rstrip("/")        # ← EDIT in .env
CREDS_FILE       = os.environ.get("CREDS_FILE", "/app/service_account.json")

DISPLAY_W, DISPLAY_H = 800, 480   # EE04 + 7.3" six-colour panel resolution

# ── 6-colour e-ink palette ────────────────────────────────────────────────────
# Calibrated to the actual pigment colours of the Seeed 7.3" ACeP display.
# Pure 255/0 values look desaturated on the physical panel — these values
# are tuned to match what the display actually renders.
EINK_PALETTE_RGB = [
    0,   0,   0,    # black
    255, 255, 255,  # white
    180,  40,  40,  # red   — physical pigment is darker/less saturated than 255,0,0
    0,   140,  80,  # green — physical pigment skews teal
    30,   60, 180,  # blue  — physical pigment skews indigo
    200, 185,   0,  # yellow — physical pigment is slightly warm/dim
]
# PIL palette buffer is always 256 colours × 3 channels
_full_palette = EINK_PALETTE_RGB + [0] * (256 * 3 - len(EINK_PALETTE_RGB))

# ── Bayer ordered dither matrix ───────────────────────────────────────────────
# 8×8 Bayer matrix gives 64 threshold levels — finer dot pattern than 4×4.
# Produces a regular grid that better approximates how ACeP displays render,
# avoids the directional streaks of Floyd-Steinberg, and eliminates bloom.
_BAYER_8x8 = np.array([
    [ 0, 32,  8, 40,  2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44,  4, 36, 14, 46,  6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [ 3, 35, 11, 43,  1, 33,  9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47,  7, 39, 13, 45,  5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
], dtype=np.float32) / 64.0  # normalise to 0–1

# ── Shared state ──────────────────────────────────────────────────────────────
image_pool:     list[bytes] = []   # processed PNG bytes, newest first
pool_lock       = threading.Lock()
last_fetch_time: float = 0.0
manual_offset:  int = 0            # incremented by /next to manually advance images
last_top_id:    str = ""           # Drive file ID of the most recent image in the pool


# ── Google Drive helpers ──────────────────────────────────────────────────────

def get_drive_service():
    """Build an authenticated Drive v3 client from the service account file."""
    creds = service_account.Credentials.from_service_account_file(
        CREDS_FILE,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_drive_images(service) -> list[dict]:
    """Return up to 10 image files from the Drive folder, newest first."""
    result = service.files().list(
        q=(
            f"'{FOLDER_ID}' in parents"
            " and trashed=false"
            " and mimeType contains 'image/'"
        ),
        orderBy="modifiedTime desc",
        pageSize=POOL_SIZE,
        fields="files(id, name, mimeType, modifiedTime)",
    ).execute()
    return result.get("files", [])


def download_file(service, file_id: str) -> bytes:
    """Stream-download a Drive file and return its raw bytes."""
    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl  = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


# ── Image processing ──────────────────────────────────────────────────────────

def process_image(raw: bytes) -> bytes:
    """
    1. Decode the raw image (JPEG, PNG, WebP, HEIC, …).
    2. Fit inside 800×480 preserving aspect ratio.
    3. Letterbox onto a white canvas.
    4. Light Gaussian blur to suppress lens flare lines and high-freq noise.
    5. Bayer ordered dithering to the 6-colour palette — produces a regular
       dot pattern that avoids Floyd-Steinberg's directional streaks and bloom.
    6. Return as PNG bytes ready to serve.
    """
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img.thumbnail((DISPLAY_W, DISPLAY_H), Image.LANCZOS)

    canvas = Image.new("RGB", (DISPLAY_W, DISPLAY_H), (255, 255, 255))
    x = (DISPLAY_W - img.width)  // 2
    y = (DISPLAY_H - img.height) // 2
    canvas.paste(img, (x, y))

    # Suppress high-frequency artifacts (lens flares, sensor noise)
    canvas = canvas.filter(ImageFilter.GaussianBlur(radius=0.8))

    # Bayer ordered dither — tile the 8×8 matrix to fill the canvas,
    # add structured noise, then snap each pixel to the nearest palette colour
    arr = np.array(canvas, dtype=np.float32)
    h, w = arr.shape[:2]
    bayer = np.tile(_BAYER_8x8, (h // 8 + 1, w // 8 + 1))[:h, :w]
    # ±12 range — subtle dithering, preserves natural colours
    arr = np.clip(arr + (bayer[:, :, np.newaxis] - 0.5) * 24, 0, 255)
    canvas = Image.fromarray(arr.astype(np.uint8))

    palette_img = Image.new("P", (1, 1))
    palette_img.putpalette(_full_palette)

    dithered = canvas.quantize(
        palette=palette_img,
        dither=Image.Dither.NONE,  # Bayer noise already applied above
    )

    out = io.BytesIO()
    dithered.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


# ── Background polling loop ───────────────────────────────────────────────────

def fetch_and_process():
    """Poll Drive, process up to 5 images, update the shared pool."""
    global last_fetch_time
    log.info("Polling Google Drive folder %s …", FOLDER_ID)
    try:
        service = get_drive_service()
        files   = list_drive_images(service)

        if not files:
            log.info("  No images found — nothing to update.")
            return

        pool = []
        for f in files[:POOL_SIZE]:
            try:
                raw       = download_file(service, f["id"])
                processed = process_image(raw)
                pool.append(processed)
                log.info("  ✓ %s", f["name"])
            except Exception as exc:
                log.warning("  ✗ %s  (%s)", f["name"], exc)

        if pool:
            new_top_id = files[0]["id"]
            with pool_lock:
                global manual_offset, last_top_id
                image_pool.clear()
                image_pool.extend(pool)
                if new_top_id != last_top_id:
                    manual_offset = 0
                    last_top_id = new_top_id
                    log.info("New image detected — resetting to top of queue.")
            last_fetch_time = time.time()
            log.info("Pool updated — %d image(s) ready.", len(pool))

    except Exception as exc:
        log.error("Drive poll failed: %s", exc)


def poll_loop():
    """Fetch immediately at startup, then sleep and repeat."""
    fetch_and_process()
    while True:
        time.sleep(REFRESH_SECONDS)
        fetch_and_process()


# ── Image selection ───────────────────────────────────────────────────────────

def current_image() -> bytes | None:
    """Return the current image for all frames, rotating every ROTATION_SECONDS."""
    with pool_lock:
        if not image_pool:
            return None
        idx = (int(time.time() / ROTATION_SECONDS) + manual_offset) % len(image_pool)
        return image_pool[idx]


# ── TRMNL firmware API endpoints ──────────────────────────────────────────────
#
# The firmware calls exactly two endpoints:
#   POST /api/setup   — first boot registration
#   GET  /api/display — every wake cycle, asks "what should I show?"
#
# Everything else below supports those two calls.

@app.route("/api/setup", methods=["POST"])
def api_setup():
    """
    Called once when the device first connects to a custom server.
    Returns a local api_key and friendly_id — the firmware stores these
    and sends api_key in the Access-Token header on every subsequent call.
    """
    data = request.get_json(silent=True) or {}
    mac  = data.get("id", request.headers.get("ID", "unknown"))
    log.info("Device registered: %s", mac)
    return jsonify({
        "status":      0,
        "api_key":     f"frame-{mac}",          # arbitrary — firmware just echoes it back
        "friendly_id": mac.replace(":", "")[:8].upper(),
        "image_url":   f"{SERVER_BASE_URL}/placeholder.png",
        "message":     "Connected to trmnl-frame server.",
    })


@app.route("/api/display", methods=["GET"])
def api_display():
    """
    Main polling endpoint.  The firmware wakes up, calls this, gets back a
    JSON blob with image_url and refresh_rate, downloads the image, draws it,
    then sleeps for refresh_rate seconds.
    """
    mac = request.headers.get("ID", "unknown").upper().replace(":", "")
    img = current_image()

    if img is None:
        # Server just started — tell device to retry in 60 s
        log.info("  [%s] Pool not ready, asking device to retry.", mac)
        return jsonify({
            "status":          202,
            "image_url":       f"{SERVER_BASE_URL}/placeholder.png",
            "filename":        "warming-up",
            "refresh_rate":    "60",
            "update_firmware": False,
            "firmware_url":    None,
            "reset_firmware":  False,
        })

    log.info("  [%s] Serving image slot.", mac)
    return jsonify({
        "status":          0,
        "image_url":       f"{SERVER_BASE_URL}/image/{mac}.png",
        "filename":        f"frame-{mac[:6]}",
        "refresh_rate":    str(REFRESH_SECONDS),
        "update_firmware": False,
        "firmware_url":    None,
        "reset_firmware":  False,
    })


@app.route("/image/<mac>.png")
def serve_image(mac):
    """Serve the processed PNG for a specific device."""
    img = current_image()
    if img is None:
        return "Image not ready yet", 503
    return Response(img, mimetype="image/png")


@app.route("/frame/<int:index>/current.png")
def serve_frame(index):
    """Serve the current image to all frames — used by the ESPHome online_image component."""
    img = current_image()
    if img is None:
        return "Image not ready yet", 503
    return Response(img, mimetype="image/png")


@app.route("/frame/<int:index>/tag")
def frame_tag(index):
    """Lightweight tag endpoint — returns a short string identifying the current image.
    ESPHome fetches this first and only downloads the full image if the tag changed."""
    with pool_lock:
        if not image_pool:
            return "none", 503
        idx = (int(time.time() / ROTATION_SECONDS) + manual_offset) % len(image_pool)
    return f"{idx}:{last_top_id}", 200


@app.route("/placeholder.png")
def serve_placeholder():
    """Blank white image returned while the pool is warming up."""
    buf = io.BytesIO()
    Image.new("RGB", (DISPLAY_W, DISPLAY_H), (255, 255, 255)).save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png")


@app.route("/health")
def health():
    """Simple health-check — useful for monitoring and debugging."""
    return jsonify({
        "ok":              True,
        "pool_size":       len(image_pool),
        "pool_max":        POOL_SIZE,
        "last_fetch":      last_fetch_time,
        "next_fetch_in":   max(0, int(REFRESH_SECONDS - (time.time() - last_fetch_time))),
        "folder_id":       FOLDER_ID,
        "refresh_every":   REFRESH_SECONDS,
        "rotation_every":  ROTATION_SECONDS,
    })


@app.route("/refresh", methods=["POST"])
def manual_refresh():
    """Trigger an immediate Drive poll without restarting the server."""
    threading.Thread(target=fetch_and_process, daemon=True).start()
    return jsonify({"ok": True, "message": "Drive poll started."})


@app.route("/next", methods=["POST"])
def next_image():
    """Advance all frames to the next image immediately."""
    global manual_offset
    with pool_lock:
        if not image_pool:
            return jsonify({"ok": False, "message": "Pool is empty."}), 503
        manual_offset = (manual_offset + 1) % len(image_pool)
    log.info("Manual advance — now at offset %d", manual_offset)
    return jsonify({"ok": True, "offset": manual_offset})


# ── Entry point ───────────────────────────────────────────────────────────────

# ── Start polling thread ──────────────────────────────────────────────────────
# Runs at import time so the poller starts whether invoked via gunicorn or directly.
log.info("Starting Drive poller — folder: %s  refresh: %ds", FOLDER_ID, REFRESH_SECONDS)
threading.Thread(target=poll_loop, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), threaded=True)
