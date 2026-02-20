#!/usr/bin/env python3
"""tapo_drive_recorder.py — Continuous Tapo C202 Live Stream + Google Drive Upload

Flow:
  1. Connect to Tapo camera via Cloud Relay.
  2. Record video in chunks (e.g., 60 seconds).
  3. When a chunk finishes, spawn a background thread to:
     a. Upload the video chunk to Google Drive.
     b. Delete the local video chunk on successful upload.
  4. Repeat indefinitely.

Google Drive auth (recommended for VPS/headless): Service Account
- Put the JSON key at ./gdrive_service_account.json (or set GOOGLE_DRIVE_SA_FILE)
- Share your target Drive folder with the service account email.
- Set GOOGLE_DRIVE_FOLDER_ID to the target folder's ID.
"""

import base64
import json
import os
import socket
import ssl
import threading
import time
from datetime import datetime

import requests
import urllib3

urllib3.disable_warnings()

# ─── Configuration ────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tapo_config.json")
config = json.loads(open(CONFIG_PATH).read())
TOKEN = config["token"]

DEVICE_ID = "80213E8CD1A65A20075764F44AFEB832236DB935"
APP_UUID = "a8b3c4d5-e6f7-89ab-cdef-012345678901"
APP_VER = "3.17.109"
UA = f"tapo/SM-G950F/{APP_VER}_Android/Android 13"
CIPC_URL = "https://aps1-cipc-api.i.tplinkcloud.com"

# Recording Configuration
CHUNK_DURATION = 60  # seconds per video chunk
SAVE_DIR = "/workspaces/tapo-cli/stream_output"
os.makedirs(SAVE_DIR, exist_ok=True)

# Google Drive upload configuration (via env vars)
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
GOOGLE_DRIVE_SA_FILE = os.environ.get(
    "GOOGLE_DRIVE_SA_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "gdrive_service_account.json"),
).strip()

# Multipart boundaries
CLIENT_BOUNDARY = "--client-stream-boundary--"
FRAME_BOUNDARY = "----client-stream-boundary--"
DEVICE_BOUNDARY = "--device-stream-boundary--"


# ─── Google Drive Upload ─────────────────────────────────────────────────────

_DRIVE_FOLDER_CACHE: dict[tuple[str, str], str] = {}


def _build_drive_service():
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as e:
        raise RuntimeError(
            "Missing Google Drive dependencies. Install: pip install -r requirements.txt"
        ) from e

    if not os.path.exists(GOOGLE_DRIVE_SA_FILE):
        raise RuntimeError(
            f"Google Drive service account JSON not found: {GOOGLE_DRIVE_SA_FILE}\n"
            "Create/download a service account key JSON and set GOOGLE_DRIVE_SA_FILE or place it next to this script."
        )

    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(GOOGLE_DRIVE_SA_FILE, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _escape_drive_query_value(value: str) -> str:
    return value.replace("'", "\\'")


def _get_or_create_folder(service, folder_name: str, parent_id: str) -> str:
    key = (parent_id, folder_name)
    if key in _DRIVE_FOLDER_CACHE:
        return _DRIVE_FOLDER_CACHE[key]

    q = (
        "mimeType='application/vnd.google-apps.folder' "
        "and trashed=false "
        f"and name='{_escape_drive_query_value(folder_name)}' "
        f"and '{parent_id}' in parents"
    )
    res = service.files().list(q=q, spaces="drive", fields="files(id,name)").execute()
    files = res.get("files", [])
    if files:
        folder_id = files[0]["id"]
        _DRIVE_FOLDER_CACHE[key] = folder_id
        return folder_id

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    created = service.files().create(body=metadata, fields="id").execute()
    folder_id = created["id"]
    _DRIVE_FOLDER_CACHE[key] = folder_id
    return folder_id


def upload_to_gdrive(filepath: str) -> bool:
    """Uploads a file to Google Drive under a date subfolder."""
    if not GOOGLE_DRIVE_FOLDER_ID:
        print("[GDRIVE] Skipping upload: GOOGLE_DRIVE_FOLDER_ID not set.")
        return False

    # Build a per-upload client: httplib2 is not guaranteed thread-safe.
    service = _build_drive_service()

    path_parts = filepath.split(os.sep)
    if len(path_parts) >= 2:
        date_folder_name = path_parts[-2]
    else:
        date_folder_name = datetime.now().strftime("%Y-%m-%d")

    date_folder_id = _get_or_create_folder(service, date_folder_name, GOOGLE_DRIVE_FOLDER_ID)

    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as e:
        raise RuntimeError(
            "Missing Google Drive dependencies. Install: pip install -r requirements.txt"
        ) from e

    file_name = os.path.basename(filepath)
    metadata = {"name": file_name, "parents": [date_folder_id]}

    print(f"[GDRIVE] Uploading {date_folder_name}/{file_name}...")
    media = MediaFileUpload(filepath, mimetype="video/MP2T", resumable=True)
    created = (
        service.files()
        .create(body=metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )
    print(f"[GDRIVE] Upload successful: id={created.get('id')} link={created.get('webViewLink')}")
    return True


def process_video_chunk(filepath: str):
    """Uploads to Google Drive, then deletes the local file on success."""
    print(f"\n[PROCESS] Starting upload for {filepath}")

    try:
        upload_success = upload_to_gdrive(filepath)
    except Exception as e:
        print(f"[GDRIVE] Upload failed: {e}")
        upload_success = False

    if not upload_success:
        print(f"[CLEANUP] Keeping local file (upload failed): {filepath}")
        return

    try:
        os.remove(filepath)
        print(f"[CLEANUP] Deleted local file: {filepath}")
    except Exception as e:
        print(f"[CLEANUP] Failed to delete {filepath}: {e}")


# ─── Tapo Stream Protocol ─────────────────────────────────────────────────────

def preconn():
    body = {
        "playerId": APP_UUID,
        "requestList": [
            {
                "deviceId": DEVICE_ID,
                "streamType": 0,
                "cloudType": 2,
                "rootCaVer": "1",
                "preConnection": 1,
                "resolution": "HD",
            }
        ],
    }
    r = requests.post(
        f"{CIPC_URL}/v1/relay/preConnectionBatchRequest?source=tapo-app",
        json=body,
        headers={"Authorization": TOKEN, "User-Agent": UA, "Content-Type": "application/json"},
        verify=False,
        timeout=15,
    )
    d = r.json()

    result_list = (
        d.get("result", {}).get("requestResultList")
        or d.get("result", {}).get("resultList")
        or d.get("preConnRespInfoList")
    )
    if not result_list or d.get("errorCode", -1) != 0:
        raise RuntimeError(f"preconn failed: {d}")

    item = result_list[0]
    access = item.get("relayAccessInfo", item)
    return access.get("relayUrl") or access.get("url"), access.get("relayToken") or access.get("token")


def make_frame(json_body: dict, cid: str, seq: int = 1) -> bytes:
    wrapper = {"type": "request", "seq": seq, "params": json_body}
    body_bytes = json.dumps(wrapper, separators=(",", ":")).encode()
    part = (
        f"{FRAME_BOUNDARY}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"X-Cid: {cid}\r\n\r\n"
    ).encode() + body_bytes + b"\r\n"
    return part


def make_preview_frame(cid: str, seq: int = 1) -> bytes:
    return make_frame(
        {
            "method": "get",
            "preview": {"channels": [0], "streams": [0], "resolutions": ["HD"], "audio": ["default"]},
        },
        cid,
        seq,
    )


def parse_frames(buf: bytearray, video_file):
    """Parse --device-stream-boundary-- frames from buffer."""
    pos = 0
    while True:
        idx = bytes(buf).find(b"--device-stream-boundary--", pos)
        if idx == -1:
            break
        line_end = bytes(buf).find(b"\r\n", idx)
        if line_end == -1:
            break
        header_start = line_end + 2
        header_end = bytes(buf).find(b"\r\n\r\n", header_start)
        if header_end == -1:
            break

        headers_block = bytes(buf[header_start:header_end]).decode(errors="replace")
        hdr_map = {
            k.lower(): v.strip()
            for k, v in (
                ln.split(": ", 1) for ln in headers_block.split("\r\n") if ": " in ln
            )
        }

        body_start = header_end + 4
        content_len = int(hdr_map.get("content-length", 0))
        if content_len == 0:
            pos = body_start
            continue
        if len(buf) < body_start + content_len:
            break

        body = bytes(buf[body_start : body_start + content_len])
        content_type = hdr_map.get("content-type", "application/json")

        if "application/json" not in content_type:
            video_file.write(body)
            video_file.flush()

        pos = body_start + content_len
        if bytes(buf[pos : pos + 2]) == b"\r\n":
            pos += 2

    return bytearray(buf[pos:])


def connect_and_stream():
    print("\n[1] Requesting Relay Connection...")
    relay_url, relay_token = preconn()

    from urllib.parse import urlparse

    parsed = urlparse(relay_url)
    host, port = parsed.hostname, parsed.port or 443
    path = parsed.path + ("?" + parsed.query + "&retryTime=0" if parsed.query else "?retryTime=0")

    http_req = (
        f"POST {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: Client/{APP_VER}/1.3\r\n"
        f"Content-Type: multipart/mixed;boundary={CLIENT_BOUNDARY}\r\n"
        f"Content-Length: 9223372036854775807\r\nKeep-Relay: 120\r\n"
        f"X-token: {relay_token}\r\nX-Pull-Mode: 1\r\nX-Version: 1.0\r\n"
        f"X-Redirect-Times: 0\r\nX-Arrive-Latency: 0\r\nX-Client-Model: SM-G950F\r\n"
        f"X-Client-UUID: {APP_UUID}\r\nConnection: keep-alive\r\n\r\n"
    )

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock = socket.create_connection((host, port), timeout=15)
    tls = ctx.wrap_socket(sock, server_hostname=host)
    tls.sendall(http_req.encode())

    raw = bytearray()
    tls.settimeout(20)
    while b"\r\n\r\n" not in raw:
        chunk = tls.recv(4096)
        if not chunk:
            raise ConnectionError("Server closed before headers")
        raw.extend(chunk)

    hdr_end = bytes(raw).index(b"\r\n\r\n")
    headers_raw = bytes(raw[:hdr_end]).decode(errors="replace")
    leftover = bytes(raw[hdr_end + 4 :])

    headers = {
        k.lower(): v.strip()
        for k, v in (
            ln.split(": ", 1) for ln in headers_raw.split("\r\n")[1:] if ": " in ln
        )
    }
    cid = (
        base64.b64decode(headers.get("x-client-id", "")).decode()
        if headers.get("x-client-id")
        else str(APP_UUID)
    )

    print("[2] Connected! Sending Preview Request...")
    tls.sendall(make_preview_frame(cid, seq=1))

    print(f"[3] Streaming started. Chunking every {CHUNK_DURATION} seconds.")
    buf = bytearray(leftover)
    tls.settimeout(5)

    chunk_start = time.time()
    now = datetime.now()
    date_folder = now.strftime("%Y-%m-%d")
    file_name = now.strftime("%Y-%m-%d_%H-%M-%S.ts")

    chunk_dir = os.path.join(SAVE_DIR, date_folder)
    os.makedirs(chunk_dir, exist_ok=True)

    ts_path = os.path.join(chunk_dir, file_name)
    video_file = open(ts_path, "wb")

    try:
        while True:
            try:
                chunk = tls.recv(65536)
                if not chunk:
                    print("\n[stream] Connection closed by server. Reconnecting...")
                    break
                buf.extend(chunk)
            except socket.timeout:
                pass

            buf = parse_frames(buf, video_file)

            if time.time() - chunk_start >= CHUNK_DURATION:
                video_file.close()

                threading.Thread(target=process_video_chunk, args=(ts_path,), daemon=True).start()

                chunk_start = time.time()
                now = datetime.now()
                date_folder = now.strftime("%Y-%m-%d")
                file_name = now.strftime("%Y-%m-%d_%H-%M-%S.ts")

                chunk_dir = os.path.join(SAVE_DIR, date_folder)
                os.makedirs(chunk_dir, exist_ok=True)

                ts_path = os.path.join(chunk_dir, file_name)
                video_file = open(ts_path, "wb")
                print(f"\n[stream] Started new chunk: {date_folder}/{file_name}")

    except KeyboardInterrupt:
        print("\n[stream] Interrupted by user")
    finally:
        try:
            video_file.close()
        except Exception:
            pass
        tls.close()


# ─── Main Loop ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 70)
    print("Tapo Live Stream + Google Drive")
    print("=" * 70)

    while True:
        try:
            connect_and_stream()
        except Exception as e:
            print(f"\n[ERROR] Stream disconnected: {e}")
            print("[INFO] Reconnecting in 5 seconds...")
            time.sleep(5)
