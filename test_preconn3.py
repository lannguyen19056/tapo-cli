#!/usr/bin/env python3
"""
Test preconn with longer hold:
- Call preConnectionBatchRequest
- Connect to relay at t=2s (give camera time to react but catch the window)
- Wait UP TO 60s for relay to respond with 200
- If we get 200, save the stream
"""

import json, os, sys, uuid, time, socket, ssl, hashlib, hmac, base64
import requests, urllib3, threading

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONFIG_PATH = os.path.expanduser("~/.tapo-cli/.config")
config = json.loads(open(CONFIG_PATH).read())
token    = config["token"]
app_url  = config["appServerUrl"]

DEVICE_ID   = "80213E8CD1A65A20075764F44AFEB832236DB935"
APP_VER     = "3.17.109"
APP_UUID    = "a8b3c4d5-e6f7-89ab-cdef-012345678901"
USER_AGENT  = f"tapo/SM-G950F/{APP_VER}_Android/Android 13"

# ── helpers ───────────────────────────────────────────────────────────

def get_service_url(service_id):
    ep = "/api/v2/common/getAppServiceUrl"
    content = json.dumps({"serviceIds": [service_id]})
    now = str(int(time.time())); nonce = str(uuid.uuid1())
    ak = "4d11b6b9d5ea4d19a829adbb9714b057"; sk = "6ed7d97f3e73467f8a5bab90b577ba4c"
    md5 = base64.b64encode(hashlib.md5(content.encode()).digest()).decode()
    pay = f"{md5}\n{now}\n{nonce}\n{ep}".encode()
    sig = hmac.new(sk.encode(), pay, hashlib.sha1).digest().hex()
    xa  = f"Timestamp={now}, Nonce={nonce}, AccessKey={ak}, Signature={sig}"
    r = requests.post(f"{app_url}{ep}?token={token}", data=content,
                      headers={"Content-Md5": md5, "X-Authorization": xa,
                               "Content-Type": "application/json; charset=UTF-8"},
                      verify=False, timeout=15)
    d = r.json()
    if d.get("error_code") == 0:
        return d["result"].get("serviceUrls", {}).get(service_id, "").rstrip("/") or None

def do_preconn(cipc_url):
    body = {"playerId": str(uuid.uuid4()), "requestList": [{
        "deviceId": DEVICE_ID, "streamType": 0, "cloudType": 1,
        "rootCaVer": "1", "preConnection": 1, "resolution": "HD"}]}
    r = requests.post(f"{cipc_url}/v1/relay/preConnectionBatchRequest",
                      params={"source": "tapo-app"}, json=body,
                      headers={"Authorization": token, "User-Agent": USER_AGENT,
                               "Content-Type": "application/json; charset=UTF-8"},
                      verify=False, timeout=20)
    d = r.json()
    if d.get("errorCode") == 0:
        items = d.get("preConnRespInfoList", [])
        if items and items[0].get("errorCode") == 0:
            info = items[0]["relayAccessInfo"]
            return info.get("relayUrl"), info.get("relayToken"), info
    return None, None, d

def connect_and_wait(relay_url, relay_token, save_path, pull_mode="1",
                     header_timeout=60, stream_duration=60):
    """
    Connect to relay and wait up to header_timeout seconds for 200 OK.
    Then read stream data for stream_duration seconds.
    """
    from urllib.parse import urlparse
    parsed = urlparse(relay_url)
    host   = parsed.hostname
    port   = parsed.port or 443
    path   = (parsed.path or "/")
    if parsed.query:
        path += "?" + parsed.query + "&retryTime=0"
    else:
        path += "?retryTime=0"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    print(f"  Connecting {host}:{port} ...", flush=True)
    try:
        raw  = socket.create_connection((host, port), timeout=15)
        sock = ctx.wrap_socket(raw, server_hostname=host)
    except Exception as e:
        return False, {"error": str(e)}

    boundary = str(uuid.uuid4()).replace("-", "")
    req = "\r\n".join([
        f"POST {path} HTTP/1.1",
        f"Host: {host}",
        f"User-Agent: Client/{APP_VER}/1.3",
        f"Accept: */*",
        f"Content-Type: multipart/mixed;boundary={boundary}",
        f"Content-Length: 9223372036854775807",
        f"Keep-Relay: {stream_duration}",
        f"X-token: {relay_token}",
        f"X-Pull-Mode: {pull_mode}",
        f"X-Version: 1.0",
        f"X-Redirect-Times: 0",
        f"X-Arrive-Latency: 0",
        f"X-Client-Model: SM-G950F",
        f"X-Client-UUID: {APP_UUID}",
        f"Connection: keep-alive",
        "", ""
    ])
    t_send = time.time()
    sock.sendall(req.encode())
    print(f"  Sent HTTP POST. Waiting up to {header_timeout}s for 200 OK ...", flush=True)

    # Wait for HTTP response headers (with LONG timeout)
    sock.settimeout(header_timeout)
    resp = b""
    try:
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                print("  Connection closed by server (no data).")
                sock.close()
                return False, {"error": "connection_closed_early"}
            resp += chunk
            elapsed = time.time() - t_send
            if elapsed > header_timeout:
                break
    except socket.timeout:
        print(f"  Timed out after {header_timeout}s waiting for response headers.")
        sock.close()
        return False, {"error": "header_timeout", "recv_bytes": len(resp)}

    t_resp = time.time() - t_send
    status_line = resp.split(b"\r\n")[0].decode("utf-8", errors="replace")
    headers_end = resp.find(b"\r\n\r\n")
    initial_body = resp[headers_end + 4:] if headers_end != -1 else b""

    print(f"  Got response at t+{t_resp:.1f}s: {status_line}")
    if initial_body.startswith(b"{"):
        try:
            err_json = json.loads(initial_body.split(b"\x00")[0].decode("utf-8", errors="ignore").strip())
            print(f"  Body: {err_json}")
        except Exception:
            print(f"  Body bytes: {initial_body[:200]}")
        sock.close()
        return False, {"status_line": status_line, "body": initial_body[:200].decode("utf-8", errors="replace")}

    if "200" not in status_line:
        sock.close()
        return False, {"status_line": status_line, "body": initial_body[:200].decode("utf-8", errors="replace")}

    # ── Got 200 OK — read stream ──
    print(f"  ✅ 200 OK at t+{t_resp:.1f}s! Saving stream to {save_path} ...")
    sock.settimeout(5)
    start = time.time()
    total = len(initial_body)
    with open(save_path, "wb") as f:
        if initial_body:
            f.write(initial_body)
        while time.time() - start < stream_duration:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                if total % (100 * 1024) < 65536:
                    print(f"    {total // 1024} KB received ...", flush=True)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"    Socket: {e}")
                break

    sock.close()
    return total > 0, {"bytes": total}


# ══ MAIN ═════════════════════════════════════════════════════════════

print("STEP 1: Get CIPC URL")
cipc_url = get_service_url("cipc.api.cloud") or "https://aps1-cipc-api.i.tplinkcloud.com"
print(f"  {cipc_url}")

print("\nSTEP 2: Pre-connection request")
relay_url, relay_token, raw_info = do_preconn(cipc_url)
if not relay_url:
    sys.exit(f"❌ Pre-connection failed: {raw_info}")
print(f"  relayUrl:   {relay_url}")
print(f"  relayToken: {relay_token}")

# ── Try 3 different start delays, each with 60s header wait ──────────

for delay in [2, 5, 10]:
    print(f"\n{'='*60}")
    print(f"Attempt with {delay}s start delay + 60s header wait (mode=1)")
    if delay > 0:
        print(f"  Sleeping {delay}s ...")
        time.sleep(delay)
    ok, info = connect_and_wait(relay_url, relay_token,
                                f"stream_delay{delay}.bin",
                                pull_mode="1",
                                header_timeout=60,
                                stream_duration=60)
    if ok:
        sz = info.get("bytes", 0)
        print(f"\n✅ Stream saved! {sz//1024} KB in stream_delay{delay}.bin")
        print("  To play: ffmpeg -i stream_delay{d}.bin output.mp4".format(d=delay))
        sys.exit(0)
    else:
        print(f"  Failed: {info}")
        # Try re-preconn for next iteration (pre-conn expired)
        if delay < 10:
            print(f"  Re-requesting pre-connection for next iteration...")
            relay_url2, relay_token2, raw2 = do_preconn(cipc_url)
            if relay_url2:
                relay_url   = relay_url2
                relay_token = relay_token2
                print(f"  New relayUrl: {relay_url}")
            else:
                print(f"  Re-preconn failed: {raw2}")

print("\n❌ All attempts failed.")
print("   The camera may be genuinely offline or the relay requires MQTT PUBACK.")
