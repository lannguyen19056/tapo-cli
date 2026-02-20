#!/usr/bin/env python3
"""
test_stream5.py — Send GetPreviewRequest over relay to trigger live stream.

Protocol (reverse-engineered from Tapo 3.17.109 APK):
  1. POST /v1/relay/preConnectionBatchRequest → relayUrl + relayToken
  2. TCP+TLS to relay: POST with multipart/mixed boundary
  3. After 200 OK → send GetPreviewRequest as first multipart frame
  4. Camera replies GetPreviewResponse (error_code=0, session_id=...)
  5. Camera starts sending video frames (multipart parts with binary data)

Frame format (client → camera):
  ----client-stream-boundary--\r\n
  Content-Type: application/json\r\n
  Content-Length: {n}\r\n
  X-Cid: {cid}\r\n
  \r\n
  {json body}\r\n

StreamControlRequest JSON wrapper:
  {
    "type": "request",
    "seq": {N},
    "params": {
      "method": "get",
      "preview": {
        "channels": [0],
        "streams": [0],
        "resolutions": ["HD"],
        "audio": ["default"]
      }
    }
  }
"""

import json, os, ssl, socket, time, uuid, base64, sys
import requests, urllib3

urllib3.disable_warnings()

# ─── Config ───────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.expanduser("~/.tapo-cli/.config")
config      = json.loads(open(CONFIG_PATH).read())
TOKEN       = config["token"]

DEVICE_ID = "80213E8CD1A65A20075764F44AFEB832236DB935"
APP_UUID  = "a8b3c4d5-e6f7-89ab-cdef-012345678901"
APP_VER   = "3.17.109"
UA        = f"tapo/SM-G950F/{APP_VER}_Android/Android 13"
CIPC_URL  = "https://aps1-cipc-api.i.tplinkcloud.com"

# Multipart boundaries (exactly as in the APK)
CLIENT_BOUNDARY  = "--client-stream-boundary--"      # in Content-Type header
FRAME_BOUNDARY   = "----client-stream-boundary--"   # the per-part separator (4 dashes)
DEVICE_BOUNDARY  = "--device-stream-boundary--"     # camera response boundary

SAVE_DIR = "/workspaces/tapo-cli/stream_output"
os.makedirs(SAVE_DIR, exist_ok=True)

# ─── Step 1: Pre-connection ───────────────────────────────────────────────────
def preconn():
    body = {
        "playerId": APP_UUID,
        "requestList": [{
            "deviceId":      DEVICE_ID,
            "streamType":    0,
            "cloudType":     2,          # IoT cloud devices = 2 (CRITICAL)
            "rootCaVer":     "1",
            "preConnection": 1,
            "resolution":    "HD"
        }]
    }
    r = requests.post(
        f"{CIPC_URL}/v1/relay/preConnectionBatchRequest?source=tapo-app",
        json=body,
        headers={
            "Authorization": TOKEN,
            "User-Agent":    UA,
            "Content-Type":  "application/json",
        },
        verify=False, timeout=15
    )
    d = r.json()
    print(f"[preconn] status={r.status_code}  body={json.dumps(d)[:300]}")

    # Try multiple possible response structures
    result_list = (
        d.get("result", {}).get("requestResultList")
        or d.get("result", {}).get("resultList")
        or d.get("preConnRespInfoList")
    )
    if not result_list or d.get("errorCode", -1) != 0:
        raise RuntimeError(f"preconn failed: {d}")

    item = result_list[0]
    # relayAccessInfo wrapper (newer API format)
    access = item.get("relayAccessInfo", item)
    relay_url   = access.get("relayUrl") or access.get("url")
    relay_token = access.get("relayToken") or access.get("token")
    print(f"[preconn] relayUrl={relay_url}")
    print(f"[preconn] relayToken={relay_token[:40]}...")
    return relay_url, relay_token


# ─── Step 2: Build multipart frame ───────────────────────────────────────────
def make_frame(json_body: dict | str, cid: str, seq: int = 1) -> bytes:
    """Wrap a dict/string in a StreamControlRequest and encode as multipart part."""
    if isinstance(json_body, dict):
        params = json_body
    else:
        params = json_body  # already a str, but we'd need to decode — keep as is
        
    wrapper = {
        "type": "request",
        "seq":  seq,
        "params": params
    }
    body_str = json.dumps(wrapper, separators=(',', ':'))
    body_bytes = body_str.encode()

    part = (
        f"{FRAME_BOUNDARY}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"X-Cid: {cid}\r\n"
        f"\r\n"
    ).encode() + body_bytes + b"\r\n"
    return part


def make_preview_frame(cid: str, seq: int = 1, resolution: str = "HD") -> bytes:
    params = {
        "method": "get",
        "preview": {
            "channels":    [0],
            "streams":     [0],
            "resolutions": [resolution],
            "audio":       ["default"]
        }
    }
    return make_frame(params, cid, seq)


# ─── Step 3: Relay connection + stream read ───────────────────────────────────
def connect_and_stream(relay_url: str, relay_token: str, duration: int = 120):
    from urllib.parse import urlparse
    parsed = urlparse(relay_url)
    host   = parsed.hostname
    port   = parsed.port or 443
    path   = parsed.path
    if parsed.query:
        path += "?" + parsed.query
    # Append retryTime=0 (required)
    sep = "&" if "?" in path else "?"
    path += f"{sep}retryTime=0"

    print(f"\n[relay] Connecting to {host}:{port}{path}")

    # HTTP request headers (exact values from APK analysis)
    http_req = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"User-Agent: Client/{APP_VER}/1.3\r\n"
        f"Accept: */*\r\n"
        f"Content-Type: multipart/mixed;boundary={CLIENT_BOUNDARY}\r\n"
        f"Content-Length: 9223372036854775807\r\n"
        f"Keep-Relay: 120\r\n"
        f"X-token: {relay_token}\r\n"
        f"X-Pull-Mode: 1\r\n"
        f"X-Version: 1.0\r\n"
        f"X-Redirect-Times: 0\r\n"
        f"X-Arrive-Latency: 0\r\n"
        f"X-Client-Model: SM-G950F\r\n"
        f"X-Client-UUID: {APP_UUID}\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
    )

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    sock = socket.create_connection((host, port), timeout=15)
    tls  = ctx.wrap_socket(sock, server_hostname=host)

    print(f"[relay] TLS connected, sending HTTP request...")
    tls.sendall(http_req.encode())

    # ── Read HTTP response headers ────────────────────────────────────────────
    raw = bytearray()
    tls.settimeout(20)
    while b"\r\n\r\n" not in raw:
        chunk = tls.recv(4096)
        if not chunk:
            raise ConnectionError("Server closed before sending headers")
        raw.extend(chunk)

    hdr_end = bytes(raw).index(b"\r\n\r\n")
    headers_raw = bytes(raw[:hdr_end]).decode(errors="replace")
    leftover    = bytes(raw[hdr_end + 4:])

    lines   = headers_raw.split("\r\n")
    status  = lines[0]
    headers = {}
    for ln in lines[1:]:
        if ": " in ln:
            k, v = ln.split(": ", 1)
            headers[k.lower()] = v.strip()

    print(f"[relay] {status}")
    for k, v in headers.items():
        print(f"[relay]   {k}: {v}")

    if "200" not in status:
        print(f"\n[ERROR] Expected 200 OK, got: {status}")
        # Try to parse error body
        err_body = leftover[:500].decode(errors="replace")
        print(f"[ERROR] Body: {err_body}")
        tls.close()
        return

    # ── Extract x-client-id (our CID for frames) ─────────────────────────────
    x_client_id_b64 = headers.get("x-client-id", "")
    if x_client_id_b64:
        try:
            cid = base64.b64decode(x_client_id_b64).decode()
        except Exception:
            cid = x_client_id_b64
    else:
        cid = str(APP_UUID)
    
    is_preconn = headers.get("pre_connection", "0") == "1"
    print(f"\n[relay] x-client-id decoded: {cid}")
    print(f"[relay] pre_connection: {is_preconn}")

    # ── Send GetPreviewRequest ────────────────────────────────────────────────
    preview_frame = make_preview_frame(cid, seq=1)
    print(f"\n[stream] Sending GetPreviewRequest ({len(preview_frame)} bytes):")
    print(preview_frame.decode(errors="replace"))
    tls.sendall(preview_frame)

    # ── Read camera stream ────────────────────────────────────────────────────
    print(f"\n[stream] Reading camera response for {duration}s...")
    t0   = time.time()
    buf  = bytearray(leftover)
    seq  = 2
    frames_received = 0
    ts_path      = f"{SAVE_DIR}/recording_{int(t0)}.ts"
    video_file   = open(ts_path, "wb")

    # Retry sending requests every 5 seconds if nothing received
    last_send = time.time()

    tls.settimeout(5)

    try:
        while time.time() - t0 < duration:
            try:
                chunk = tls.recv(65536)
                if not chunk:
                    print("\n[stream] Connection closed by server")
                    break
                buf.extend(chunk)
                elapsed = time.time() - t0
                print(f"\r[stream] t={elapsed:.1f}s  total_bytes={len(buf)}  frames={frames_received}", end="", flush=True)
            except socket.timeout:
                elapsed = time.time() - t0
                print(f"\r[stream] t={elapsed:.1f}s  waiting...  buf={len(buf)}", end="", flush=True)

                # Re-send preview request if nothing has come back
                if frames_received == 0 and time.time() - last_send > 5:
                    print(f"\n[stream] Resending GetPreviewRequest (attempt)...")
                    preview_frame = make_preview_frame(cid, seq=seq)
                    seq += 1
                    try:
                        tls.sendall(preview_frame)
                    except Exception as e:
                        print(f"\n[stream] Send error: {e}")
                        break
                    last_send = time.time()
                continue

            # Parse multipart frames from buffer
            buf, frames_received = parse_frames(buf, video_file, cid, frames_received)

    except KeyboardInterrupt:
        print("\n[stream] Interrupted by user")
    finally:
        video_file.close()
        tls.close()
        size = os.path.getsize(ts_path)
        print(f"\n[stream] Done. Saved {size:,} bytes ({size/1024/1024:.2f} MB)")
        print(f"[stream] File: {ts_path}")
        if size > 0:
            print(f"[stream] Play with:  vlc '{ts_path}'")
            print(f"[stream]        or:  mpv '{ts_path}'")
            print(f"[stream]        or:  ffplay '{ts_path}'")
        else:
            print("[stream] No data received (0 bytes).")


def parse_frames(buf: bytearray, video_file, cid: str, frames_in: int):
    """Parse --device-stream-boundary-- frames from buffer, return (remaining_buf, frame_count)."""
    frames = frames_in
    boundary_bytes = f"\r\n{DEVICE_BOUNDARY}\r\n".encode()
    start_boundary = f"{DEVICE_BOUNDARY}\r\n".encode()
    
    # Look for boundary markers
    pos = 0
    while True:
        # Find next boundary
        idx = bytes(buf).find(b"--device-stream-boundary--", pos)
        if idx == -1:
            break

        # Skip the boundary line
        line_end = bytes(buf).find(b"\r\n", idx)
        if line_end == -1:
            break  # incomplete frame

        header_start = line_end + 2
        # Find end of headers (blank line)
        header_end = bytes(buf).find(b"\r\n\r\n", header_start)
        if header_end == -1:
            break  # incomplete headers

        # Parse headers
        headers_block = bytes(buf[header_start:header_end]).decode(errors="replace")
        hdr_map = {}
        for ln in headers_block.split("\r\n"):
            if ": " in ln:
                k, v = ln.split(": ", 1)
                hdr_map[k.lower()] = v.strip()

        body_start = header_end + 4
        content_len = int(hdr_map.get("content-length", 0))
        
        if content_len == 0:
            pos = body_start
            continue

        if len(buf) < body_start + content_len:
            break  # incomplete body

        body = bytes(buf[body_start: body_start + content_len])
        content_type = hdr_map.get("content-type", "application/json")
        frame_cid    = hdr_map.get("x-cid", "")

        frames += 1
        print(f"\n[frame #{frames}] type={content_type}  len={content_len}  cid={frame_cid}")

        if "application/json" in content_type:
            try:
                j = json.loads(body)
                print(f"[frame #{frames}] JSON: {json.dumps(j)[:300]}")
                # Check for stream params - camera may send codec info
                handle_control_frame(j)
            except Exception:
                print(f"[frame #{frames}] (non-JSON body): {body[:100]}")
        else:
            # Binary video data — save it
            video_file.write(body)
            video_file.flush()
            print(f"[frame #{frames}] binary video: {content_len} bytes written")

        pos = body_start + content_len
        # Skip trailing \r\n after body
        if bytes(buf[pos:pos+2]) == b"\r\n":
            pos += 2

    # Remove consumed data from buffer
    remaining = bytearray(buf[pos:])
    return remaining, frames


def handle_control_frame(j: dict):
    """Handle JSON control frames from camera (response, notification, error)."""
    frame_type = j.get("type", "")
    seq        = j.get("seq", 0)
    params     = j.get("params", {})
    
    if frame_type == "response":
        err = params.get("error_code", -1)
        session_id = params.get("session_id", "")
        print(f"  → Response seq={seq}  error_code={err}  session_id={session_id!r}")
        if err == 0:
            print("  → Stream request accepted! Video data should follow.")
        else:
            print(f"  → ERROR from camera: {params}")
    
    elif frame_type == "notification":
        print(f"  → Notification: {json.dumps(params)[:200]}")
    
    elif frame_type == "error":
        print(f"  → Error frame: {params}")
    
    else:
        print(f"  → Unknown frame type: {frame_type!r}")


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 120

    print("=" * 60)
    print("Tapo C202 Live Stream — test_stream5.py")
    print("=" * 60 + "\n")

    print("[1] Pre-connection request (cloudType=2)...")
    relay_url, relay_token = preconn()

    print(f"\n[2] Connecting to relay and starting stream ({duration}s)...")
    connect_and_stream(relay_url, relay_token, duration=duration)
