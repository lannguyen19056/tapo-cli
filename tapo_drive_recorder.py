#!/usr/bin/env python3
from __future__ import annotations
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
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timezone, timedelta

import requests
import urllib3

# Vietnam timezone (UTC+7)
VN_TZ = timezone(timedelta(hours=7))


def vn_now() -> datetime:
    """Get current time in Vietnam timezone."""
    return datetime.now(VN_TZ)

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

# ─── Multi-Account OAuth2 Support ────────────────────────────────────────────
# Place multiple token files:
#   gdrive_oauth_token.json        (account 1 — 15 GB)
#   gdrive_oauth_token_2.json      (account 2 — 15 GB)
#   gdrive_oauth_token_3.json      (account 3 — 15 GB)
#   ...
# Each account gets its own folder_id mapping in gdrive_accounts.json (optional).
# If no accounts config, all accounts use GOOGLE_DRIVE_FOLDER_ID env var.
#
# Setup extra accounts:
#   python3 oauth_exchange.py              # follow prompts, save as gdrive_oauth_token_2.json
#   Then share your Drive folder with each Google account.

SCRIPT_DIR_PATH = os.path.dirname(os.path.abspath(__file__))

GOOGLE_DRIVE_ACCOUNTS_FILE = os.path.join(SCRIPT_DIR_PATH, "gdrive_accounts.json")

# Storage threshold: switch account when free space < this (bytes)
STORAGE_MIN_FREE_BYTES = int(os.environ.get("STORAGE_MIN_FREE_MB", "500")) * 1024 * 1024  # default 500 MB

# Multipart boundaries
CLIENT_BOUNDARY = "--client-stream-boundary--"
FRAME_BOUNDARY = "----client-stream-boundary--"
DEVICE_BOUNDARY = "--device-stream-boundary--"


# ─── Google Drive Upload ─────────────────────────────────────────────────────

_DRIVE_FOLDER_CACHE: dict[tuple[str, str], str] = {}


def _discover_oauth_tokens() -> list[dict]:
    """Find all gdrive_oauth_token*.json files and load their config."""
    import glob

    pattern = os.path.join(SCRIPT_DIR_PATH, "gdrive_oauth_token*.json")
    token_files = sorted(glob.glob(pattern))

    if not token_files:
        return []

    # Load optional accounts config (maps token file → folder_id)
    accounts_config = {}
    if os.path.exists(GOOGLE_DRIVE_ACCOUNTS_FILE):
        with open(GOOGLE_DRIVE_ACCOUNTS_FILE) as f:
            accounts_config = json.load(f)

    accounts = []
    for tf in token_files:
        name = os.path.basename(tf)
        folder_id = accounts_config.get(name, {}).get("folder_id", GOOGLE_DRIVE_FOLDER_ID)
        accounts.append({
            "token_file": tf,
            "name": name,
            "folder_id": folder_id,
        })

    return accounts


def _build_drive_service_from_token(token_file: str):
    """Build a Drive API service from a specific OAuth2 token file."""
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials as UserCredentials

    with open(token_file) as f:
        token_data = json.load(f)

    creds = UserCredentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes", ["https://www.googleapis.com/auth/drive"]),
    )

    # Refresh if expired
    if creds.expired or not creds.token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(token_file, "w") as f:
            json.dump(token_data, f, indent=2)

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _get_storage_info(service) -> tuple[int, int, int]:
    """Returns (total_bytes, used_bytes, free_bytes) for a Drive account."""
    try:
        about = service.about().get(fields="storageQuota").execute()
        quota = about.get("storageQuota", {})
        total = int(quota.get("limit", 0))
        used = int(quota.get("usage", 0))
        free = total - used if total > 0 else float("inf")
        return total, used, free
    except Exception as e:
        print(f"[GDRIVE] Warning: could not get storage info: {e}")
        return 0, 0, float("inf")


def _format_bytes(b) -> str:
    if b == float("inf"):
        return "unlimited"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


# Track which account index to use (round-robin on quota full)
_current_account_idx = 0
_account_list: list[dict] | None = None


def _get_active_account() -> dict | None:
    """Get the current active account with enough free space."""
    global _current_account_idx, _account_list

    if _account_list is None:
        _account_list = _discover_oauth_tokens()
        if _account_list:
            names = ", ".join(a["name"] for a in _account_list)
            total_gb = len(_account_list) * 15
            print(f"[GDRIVE] Found {len(_account_list)} account(s): {names}")
            print(f"[GDRIVE] Total potential storage: ~{total_gb} GB")

    if not _account_list:
        return None

    # Try each account starting from current index
    tried = 0
    while tried < len(_account_list):
        idx = _current_account_idx % len(_account_list)
        account = _account_list[idx]

        try:
            service = _build_drive_service_from_token(account["token_file"])
            total, used, free = _get_storage_info(service)

            if free > STORAGE_MIN_FREE_BYTES:
                print(f"[GDRIVE] Using {account['name']} "
                      f"(used: {_format_bytes(used)}/{_format_bytes(total)}, "
                      f"free: {_format_bytes(free)})")
                return {**account, "service": service}

            print(f"[GDRIVE] {account['name']} nearly full "
                  f"(free: {_format_bytes(free)}), trying next...")
            _current_account_idx += 1
            tried += 1

        except Exception as e:
            print(f"[GDRIVE] {account['name']} error: {e}, trying next...")
            _current_account_idx += 1
            tried += 1

    print("[GDRIVE] ALL accounts are full or unavailable!")
    return None


def _find_oldest_date_folder(service, parent_folder_id: str) -> tuple[str, str, str] | None:
    """Find the oldest date-named subfolder (YYYY-MM-DD) in a Drive folder.
    Returns (folder_id, folder_name, parent_id) or None."""
    try:
        q = (
            "mimeType='application/vnd.google-apps.folder' "
            "and trashed=false "
            f"and '{parent_folder_id}' in parents"
        )
        res = service.files().list(
            q=q, spaces="drive",
            fields="files(id,name)",
            orderBy="name",  # YYYY-MM-DD sorts chronologically
            pageSize=100,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        folders = res.get("files", [])

        # Filter only date-formatted folders (YYYY-MM-DD)
        date_folders = [f for f in folders if re.match(r"^\d{4}-\d{2}-\d{2}$", f["name"])]

        if date_folders:
            oldest = date_folders[0]  # Already sorted by name (date)
            return oldest["id"], oldest["name"], parent_folder_id
    except Exception as e:
        print(f"[CLEANUP] Error listing folders: {e}")
    return None


def _delete_folder_contents(service, folder_id: str, folder_name: str) -> int:
    """Delete all files in a folder, then the folder itself. Returns bytes freed."""
    freed = 0
    try:
        # List all files in the folder
        q = f"'{folder_id}' in parents and trashed=false"
        res = service.files().list(
            q=q, spaces="drive",
            fields="files(id,name,size)",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = res.get("files", [])

        for f in files:
            try:
                size = int(f.get("size", 0))
                service.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
                freed += size
                print(f"[CLEANUP] Deleted {folder_name}/{f['name']} ({_format_bytes(size)})")
            except Exception as e:
                print(f"[CLEANUP] Failed to delete {f['name']}: {e}")

        # Delete the folder itself
        service.files().delete(fileId=folder_id, supportsAllDrives=True).execute()
        print(f"[CLEANUP] Removed folder {folder_name}")

        # Clear folder cache
        keys_to_remove = [k for k in _DRIVE_FOLDER_CACHE if _DRIVE_FOLDER_CACHE[k] == folder_id]
        for k in keys_to_remove:
            del _DRIVE_FOLDER_CACHE[k]

    except Exception as e:
        print(f"[CLEANUP] Error deleting folder {folder_name}: {e}")

    return freed


def _free_space_by_deleting_oldest() -> bool:
    """Delete the oldest date folder across all accounts to free space.
    Returns True if space was freed successfully."""
    global _account_list

    if not _account_list:
        return False

    print("\n[CLEANUP] All accounts full — looking for oldest recordings to delete...")

    # Find the globally oldest date folder across all accounts
    oldest_info = None  # (date_name, account_idx, folder_id, service)

    for idx, account in enumerate(_account_list):
        try:
            folder_id = account.get("folder_id", GOOGLE_DRIVE_FOLDER_ID)
            if not folder_id:
                continue
            service = _build_drive_service_from_token(account["token_file"])
            result = _find_oldest_date_folder(service, folder_id)
            if result:
                fid, fname, pid = result
                if oldest_info is None or fname < oldest_info[0]:
                    oldest_info = (fname, idx, fid, service)
        except Exception as e:
            print(f"[CLEANUP] Error checking {account['name']}: {e}")

    if oldest_info is None:
        print("[CLEANUP] No date folders found to delete!")
        return False

    date_name, acct_idx, folder_id, service = oldest_info
    account = _account_list[acct_idx]
    print(f"[CLEANUP] Deleting oldest day: {date_name} from {account['name']}...")

    freed = _delete_folder_contents(service, folder_id, date_name)
    print(f"[CLEANUP] Freed {_format_bytes(freed)} from {account['name']}")

    # Also delete same date from other accounts (same day, different uploaders)
    for idx, other_account in enumerate(_account_list):
        if idx == acct_idx:
            continue
        try:
            other_folder_id = other_account.get("folder_id", GOOGLE_DRIVE_FOLDER_ID)
            if not other_folder_id:
                continue
            other_service = _build_drive_service_from_token(other_account["token_file"])
            result = _find_oldest_date_folder(other_service, other_folder_id)
            if result and result[1] == date_name:
                extra_freed = _delete_folder_contents(other_service, result[0], date_name)
                freed += extra_freed
                print(f"[CLEANUP] Also freed {_format_bytes(extra_freed)} from {other_account['name']}")
        except Exception as e:
            pass

    print(f"[CLEANUP] Total freed: {_format_bytes(freed)}")
    return freed > 0


def _build_drive_service():
    """Build Drive service — multi-account aware."""
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise RuntimeError(
            "Missing Google Drive dependencies. Install: pip install -r requirements.txt"
        ) from e

    # Try multi-account OAuth2 first
    account = _get_active_account()
    if account:
        return account["service"], account.get("folder_id", GOOGLE_DRIVE_FOLDER_ID)

    # All accounts full — try to free space by deleting oldest day
    if _account_list:
        MAX_CLEANUP_ATTEMPTS = 3
        for attempt in range(1, MAX_CLEANUP_ATTEMPTS + 1):
            print(f"[CLEANUP] Attempt {attempt}/{MAX_CLEANUP_ATTEMPTS}...")
            if _free_space_by_deleting_oldest():
                # Re-check after cleanup
                account = _get_active_account()
                if account:
                    print("[CLEANUP] Space freed successfully, resuming upload")
                    return account["service"], account.get("folder_id", GOOGLE_DRIVE_FOLDER_ID)
            else:
                break
        print("[CLEANUP] Could not free enough space after cleanup attempts")

    # Fallback: Service Account
    if os.path.exists(GOOGLE_DRIVE_SA_FILE):
        from google.oauth2.service_account import Credentials

        scopes = ["https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(GOOGLE_DRIVE_SA_FILE, scopes=scopes)
        print("[GDRIVE] Using Service Account credentials")
        return build("drive", "v3", credentials=creds, cache_discovery=False), GOOGLE_DRIVE_FOLDER_ID

    raise RuntimeError(
        "No Google Drive credentials found.\n"
        "Run: python3 setup_gdrive_oauth.py  (for personal account)\n"
        "Or place gdrive_service_account.json (for service account/Shared Drive)"
    )


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
    res = service.files().list(q=q, spaces="drive", fields="files(id,name)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
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
    created = service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    folder_id = created["id"]
    _DRIVE_FOLDER_CACHE[key] = folder_id
    return folder_id


def upload_to_gdrive(filepath: str) -> bool:
    """Uploads a file to Google Drive under a date subfolder. Multi-account aware."""
    if not GOOGLE_DRIVE_FOLDER_ID and not os.path.exists(GOOGLE_DRIVE_ACCOUNTS_FILE):
        print("[GDRIVE] Skipping upload: GOOGLE_DRIVE_FOLDER_ID not set.")
        return False

    # Build a per-upload client (multi-account: picks account with free space)
    service, folder_id = _build_drive_service()

    if not folder_id:
        print("[GDRIVE] Skipping upload: no folder_id for active account.")
        return False

    path_parts = filepath.split(os.sep)
    if len(path_parts) >= 2:
        date_folder_name = path_parts[-2]
    else:
        date_folder_name = vn_now().strftime("%Y-%m-%d")

    date_folder_id = _get_or_create_folder(service, date_folder_name, folder_id)

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
        .create(body=metadata, media_body=media, fields="id, webViewLink", supportsAllDrives=True)
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
    now = vn_now()
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
                now = vn_now()
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
