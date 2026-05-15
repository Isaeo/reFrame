# eink-frame

A self-hosted system for driving Seeed EE04 (XIAO ESP32-S3 Plus) + 7.3" Spectra6 e-ink displays from a shared Google Drive folder. Drop images into Drive; they appear on every frame within one poll cycle.

```
[ESP32 frame A] ──wifi──▶ ┐
                           ├──▶  server  ──▶  Google Drive folder
[ESP32 frame B] ──wifi──▶ ┘
```

---

## Repo structure

```
eink-frame/
├── server/                 # Flask server — polls Drive, serves images
│   ├── server.py
│   ├── requirements.txt
│   └── Dockerfile
├── frame/                  # ESPHome config — one per physical frame
│   ├── frame.yaml
│   ├── secrets.yaml.example
│   └── secrets.yaml        # ← you create this, gitignored
├── 3d models/              # Enclosure STL/STEP files
├── DrivePoller.gs          # Optional Google Apps Script helper
├── docker-compose.yml      # Runs the server
├── .env.example            # Copy to .env and fill in
└── .gitignore
```

---

## Quick start

### 1. Google Drive setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project.
2. Enable the **Google Drive API** (`APIs & Services → Enable APIs → search "Drive"`).
3. Go to `IAM & Admin → Service Accounts → Create Service Account`. No special roles needed.
4. Under `Keys → Add Key → Create new key → JSON` — download the file.
5. Rename it to `service_account.json` and place it in the repo root.
6. Create a folder in Google Drive. Share it with the service account email (`something@project.iam.gserviceaccount.com`) — **Viewer** access.
7. Copy the **folder ID** from the URL: `https://drive.google.com/drive/folders/`**`THIS_PART`**

### 2. Configure the server

```bash
cp .env.example .env
```

Edit `.env` and fill in `FOLDER_ID` and `SERVER_BASE_URL`. Everything else has sensible defaults.

### 3. Start the server

**Docker (recommended for cloud/Pi):**
```bash
docker compose up -d --build
docker compose logs -f
```

**Local Python (for development):**
```bash
cd server
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cd ..
export $(cat .env | xargs)
python server/server.py
```

Confirm it's working:
```bash
curl http://localhost:5001/health
```

### 4. Configure and flash each frame

```bash
cp frame/secrets.yaml.example frame/secrets.yaml
```

Edit `frame/secrets.yaml` with your wifi credentials and generated keys.

Edit the `substitutions` block at the top of `frame/frame.yaml`:
- `device_name` — unique name for this frame
- `server_url` — URL of your running server
- `update_interval` — how often the frame polls (minimum `5min` — display takes ~30s to refresh)

First flash must be over USB-C:
```bash
esphome run frame/frame.yaml
```

Subsequent updates are OTA over wifi.

---

## Server configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `FOLDER_ID` | — | Google Drive folder ID (**required**) |
| `SERVER_BASE_URL` | — | Public URL of this server, no trailing slash (**required**) |
| `REFRESH_SECONDS` | `1800` | How often to poll Google Drive for new images |
| `ROTATION_SECONDS` | `600` | How often to advance to the next image across all frames |
| `POOL_SIZE` | `20` | Max images loaded from Drive (sliding window of most recent) |
| `CREDS_FILE` | `/app/service_account.json` | Path to service account JSON (overridden by docker-compose) |

---

## Frame configuration (`frame/frame.yaml` substitutions)

| Variable | Default | Description |
|---|---|---|
| `device_name` | `eink-frame-1` | Unique device name — used for OTA and ESPHome discovery |
| `frame_index` | `0` | Reserved for future per-frame image offsets |
| `server_url` | — | Full URL of the server, no trailing slash (**required**) |
| `update_interval` | `5min` | How often the frame fetches a new image from the server |

## Frame secrets (`frame/secrets.yaml`)

| Key | Description |
|---|---|
| `wifi_ssid` | 2.4 GHz network name (5 GHz not supported) |
| `wifi_password` | Wifi password |
| `ap_password` | Password for the fallback hotspot broadcast if wifi fails |
| `api_encryption_key` | 32-byte base64 key — generate with `openssl rand -base64 32` |
| `ota_password` | Password to authorise OTA firmware updates |

---

## Server endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Server status — pool size, last fetch time, config |
| `POST` | `/refresh` | Trigger an immediate Google Drive poll without restarting |
| `POST` | `/next` | Advance all frames to the next image immediately |
| `GET` | `/frame/<n>/current.png` | Current image for frame index `n` — called by ESPHome |
| `GET` | `/image/<mac>.png` | Current image keyed by device MAC (TRMNL firmware compat) |
| `GET` | `/placeholder.png` | Blank white 800×480 image served while pool is warming up |

---

## Sending content

Drop any image (JPEG, PNG, WebP) into the shared Drive folder. The server picks it up within `REFRESH_SECONDS` and all frames display it within one `update_interval` after that.

To force an immediate fetch without restarting:
```bash
curl -X POST http://YOUR_SERVER:5001/refresh
```

**Tip:** Design slides in Google Slides at 800×480 px, export as PNG, drop into Drive.

The server keeps the `POOL_SIZE` most recent images. As you add new ones they slide in and old ones drop off. Images rotate across all frames every `ROTATION_SECONDS`.

---

## Cloud deployment (Oracle Cloud free tier)

1. Create a free [Oracle Cloud](https://cloud.oracle.com) account and provision a **VM.Standard.E2.1.Micro** instance (Ubuntu 22.04).
2. Add an ingress rule in `Networking → VCN → Security Lists`: TCP port 5001, source `0.0.0.0/0`.
3. On the VM, also open the port at the OS level:
   ```bash
   sudo iptables -I INPUT -p tcp --dport 5001 -j ACCEPT
   sudo apt install iptables-persistent -y && sudo netfilter-persistent save
   ```
4. Copy files to the VM:
   ```bash
   scp -r server/ docker-compose.yml .env.example service_account.json ubuntu@YOUR_VM_IP:~/eink-frame/
   ```
5. SSH in, create `.env` with the VM's public IP as `SERVER_BASE_URL`, then:
   ```bash
   docker compose up -d --build
   ```
6. Update `server_url` in `frame/frame.yaml` to the VM's public IP and reflash.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `pool_size: 0` on `/health` | Drive folder is empty, or service account doesn't have Viewer access |
| `Invalid JWT Signature` in logs | Re-download `service_account.json` directly from Google Cloud Console — don't paste |
| Frame can't reach server | Check `SERVER_BASE_URL` in `.env` matches your server IP/port; check firewall |
| `Failed to allocate memory` on frame | PSRAM not enabled — ensure `psram: mode: octal` is in `frame.yaml` |
| Frame stuck on old image | Restart server or `POST /refresh` to force a Drive poll |
| 5 GHz wifi won't connect | ESP32-S3 is 2.4 GHz only — connect to your 2.4 GHz network |
