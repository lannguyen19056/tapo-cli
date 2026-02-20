#!/usr/bin/env python3
"""
Test pre-connection with multiple strategies:
1. Verify camera is online via device status API
2. Call preConnectionBatchRequest 
3. Try connecting to relay at 0s, 5s, 10s, 20s waits
4. Also try relayBusinessUrl as alternate
"""

import json, os, sys, uuid, time, socket, ssl, hashlib, hmac, base64
import requests, urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONFIG_PATH = os.path.expanduser("~/.tapo-cli/.config")
config = json.loads(open(CONFIG_PATH).read())
token    = config["token"]
app_url  = config["appServerUrl"]
email    = config["email"]

DEVICE_ID  = "80213E8CD1A65A20075764F44AFEB832236DB935"
APP_TYPE   = "TP-Link_Tapo_Android"
APP_VER    = "3.17.109"
APP_UUID   = "a8b3c4d5-e6f7-89ab-cdef-012345678901"
USER_AGENT = f"tapo/SM-G950F/{APP_VER}_Android/Android 13"

def req_sig(endpoint, content):
    now   = str(int(time.time()))
    nonce = str(uuid.uuid1())
    ak    = "4d11b6b9d5ea4d19a829adbb9714b057"
    sk    = "6ed7d97f3e73467f8a5bab90b577ba4c"
    md5   = base64.b64encode(hashlib.md5(content.encode()).digest()).decode()
    pay   = f"{md5}\n{now}\n{nonce}\n{endpoint}".encode()
    sig   = hmac.new(sk.encode(), pay, hashlib.sha1).digest().hex()
    return md5, f"Timestamp={now}, Nonce={nonce}, AccessKey={ak}, Signature={sig}"

def get_service_url(service_id):
    ep      = "/api/v2/common/getAppServiceUrl"
    content = json.dumps({"serviceIds": [service_id]})
    md5, xa = req_sig(ep, content)
    r = requests.post(f"{app_url}{ep}?token={token}",
                      data=content,
                      headers={"Content-Md5": md5, "X-Authorization": xa,
                               "Content-Type": "application/json; charset=UTF-8"},
                      verify=False, timeout=15)
    d = r.json()
    if d.get("error_code") == 0:
        urls = d["result"].get("serviceUrls", {})
        return urls.get(service_id, "").rstrip("/") or None
    return None

# ── Helpers ────────────────────────────────────────────────────────────

def check_device_online():
    """Check if device is online via TP-Link IoT API."""
    ep      = "/api/v2/common/getDeviceListByPage"
    content = json.dumps({"pageSize": 50, "pageNum": 1})
    md5, xa = req_sig(ep, content)
    r = requests.post(f"{app_url}{ep}?token={token}",
                      data=content,
                      headers={"Content-Md5": md5, "X-Authorization": xa,
                               "Content-Type": "application/json; charset=UTF-8"},
                      verify=False, timeout=15)
    d = r.json()
    if d.get("error_code") == 0:
        for dev in d["result"].get("deviceList", []):
            if dev.get("deviceId") == DEVICE_ID:
                status = dev.get("status", "unknown")
                return status, dev
    return "unknown", {}

def connect_relay(relay_url: str, relay_token: str, save_path: str,
                  duration: int = 20, pull_mode: str = "1"):
    """Low-level relay connect. pull_mode: '1'=pre_conn, '2'=direct."""
    from urllib.parse import urlparse
    parsed = urlparse(relay_url)
    host   = parsed.hostname
    port   = parsed.port or 443
    path   = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query + "&retryTime=0"
    else:
        path += "?retryTime=0"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        raw  = socket.create_connection((host, port), timeout=12)
        sock = ctx.wrap_socket(raw, server_hostname=host)
    except Exception as e:
        return False, {"error": str(e)}

    boundary = str(uuid.uuid4()).replace("-", "")
    req = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"User-Agent: Client={APP_VER}/1.3\r\n"
        f"Accept: */*\r\n"
        f"Content-Type: multipart/mixed;boundary={boundary}\r\n"
        f"Content-Length: 9223372036854775807\r\n"
        f"Keep-Relay: {duration}\r\n"
        f"X-token: {relay_token}\r\n"
        f"X-Pull-Mode: {pull_mode}\r\n"
        f"X-Version: 1.0\r\n"
        f"X-Redirect-Times: 0\r\n"
        f"X-Arrive-Latency: 0\r\n"
        f"X-Client-Model: SM-G950F\r\n"
        f"X-Client-UUID: {APP_UUID}\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
    )
    sock.sendall(req.encode())

    resp = b""
    sock.settimeout(8)
    try:
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                break
            resp += chunk
    except socket.timeout:
        pass

    status_line = resp.split(b"\r\n")[0].decode("utf-8", errors="replace")
    headers_end = resp.find(b"\r\n\r\n")
    initial_body = resp[headers_end + 4:] if headers_end != -1 else b""

    print(f"    Status: {status_line}")
    if initial_body and initial_body.startswith(b"{"):
        try:
            err_json = json.loads(initial_body.split(b"\x00")[0].decode("utf-8", errors="ignore").strip())
            print(f"    Body: {err_json}")
            sock.close()
            return False, err_json
        except Exception:
            pass

    if "200" not in status_line:
        sock.close()
        return False, {"status": status_line, "body": initial_body[:200].decode("utf-8", errors="replace")}

    # Stream is open — read and save
    print(f"    Stream open! Saving to {save_path} for {duration}s ...")
    sock.settimeout(3)
    start = time.time()
    total  = len(initial_body)
    with open(save_path, "wb") as f:
        if initial_body:
            f.write(initial_body)
        while time.time() - start < duration:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                if total % (100 * 1024) < 65536:
                    print(f"      {total // 1024} KB...", flush=True)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"      Socket: {e}")
                break

    sock.close()
    print(f"    Total: {total // 1024} KB")
    return total > 0, {"bytes": total}


# ══ MAIN ═══════════════════════════════════════════════════════════════

print("=" * 60)
print("STEP 0: Check device online status")
status, dev_info = check_device_online()
print(f"  Device status: {status}")
if dev_info:
    print(f"  Device alias:  {dev_info.get('alias', '?')}")
    print(f"  Device model:  {dev_info.get('deviceModel', '?')}")
    print(f"  FW version:    {dev_info.get('fwVer', '?')}")

if status not in ("online", "1", 1):
    print(f"\n⚠️  Device may be offline (status={status!r}). Continuing anyway...")

print("\n" + "=" * 60)
print("STEP 1: Get CIPC URL")
cipc_url = get_service_url("cipc.api.cloud") or get_service_url("cipc.api") or \
           "https://aps1-cipc-api.i.tplinkcloud.com"
print(f"  CIPC URL: {cipc_url}")

# ── Pre-connection request ─────────────────────────────────────────────
print("\n" + "=" * 60)
print("STEP 2: Pre-connection request")

player_id = str(uuid.uuid4())
body = {
    "playerId": player_id,
    "requestList": [{
        "deviceId":      DEVICE_ID,
        "streamType":    0,       # STREAM_TYPE_PREVIEW
        "cloudType":     1,       # cloud
        "rootCaVer":     "1",
        "preConnection": 1,       # new pre-connection
        "resolution":    "HD",
    }]
}

relay_url      = None
relay_token    = None
relay_biz_url  = None

for auth in [token, f"ut|{token}"]:
    r = requests.post(
        f"{cipc_url}/v1/relay/preConnectionBatchRequest",
        params={"source": "tapo-app"},
        json=body,
        headers={"Authorization": auth, "User-Agent": USER_AGENT,
                 "Content-Type": "application/json; charset=UTF-8"},
        verify=False, timeout=20)
    d = r.json()
    print(f"  [{auth[:15]}...] Status={r.status_code}  errorCode={d.get('errorCode')}")
    if d.get("errorCode") == 0:
        items = d.get("preConnRespInfoList", [])
        if items and items[0].get("errorCode") == 0:
            info           = items[0].get("relayAccessInfo", {})
            relay_url      = info.get("relayUrl")
            relay_token    = info.get("relayToken") or info.get("sessionToken")
            relay_biz_url  = info.get("relayBusinessUrl") or info.get("relayBussinessUrl")
            print(f"  ✅  relayUrl:   {relay_url}")
            print(f"      relayToken: {relay_token}")
            print(f"      bizUrl:     {relay_biz_url}")
            break
        else:
            print(f"  Item error: {items[0].get('errorCode') if items else 'empty'}")

if not relay_url:
    sys.exit("❌ Failed to get relay URL via pre-connection.")

# ── Try relay connection at multiple wait points ───────────────────────
print("\n" + "=" * 60)
print("STEP 3: Try relay connections at various wait intervals")

wait_times = [0, 3, 8, 15, 30]
urls_to_try = [(relay_url, "1", "primary-preconn"),
               (relay_url, "2", "primary-direct"),]
if relay_biz_url:
    urls_to_try += [(relay_biz_url, "1", "biz-preconn"),
                    (relay_biz_url, "2", "biz-direct")]

t_start = time.time()
connected = False

for wait in wait_times:
    elapsed = time.time() - t_start
    remaining = wait - elapsed
    if remaining > 0:
        print(f"\n  ── Waiting until t={wait}s (sleeping {remaining:.1f}s) ──")
        time.sleep(remaining)

    actual_wait = time.time() - t_start
    print(f"\n  [t={actual_wait:.1f}s] Trying relay connection...")

    for url, mode, label in urls_to_try[:2]:  # try primary URL with both modes
        print(f"    [{label}] mode={mode}  url={url[:60]}...")
        ok, info = connect_relay(url, relay_token, f"stream_{label}_t{wait}.bin",
                                 duration=5, pull_mode=mode)
        if ok:
            print(f"\n✅ STREAM CONNECTED! Saving full stream now...")
            ok2, info2 = connect_relay(url, relay_token, "stream_final.bin",
                                       duration=60, pull_mode=mode)
            print(f"Final: {info2}")
            connected = True
            break
    if connected:
        break

if not connected:
    print("\n❌ All attempts failed.")
    print("   Trying bizUrl as last resort...")
    if relay_biz_url:
        for mode in ["1", "2"]:
            ok, info = connect_relay(relay_biz_url, relay_token, f"stream_biz_m{mode}.bin",
                                     duration=10, pull_mode=mode)
            print(f"  biz mode={mode}: {info}")
            if ok:
                print("✅ BizUrl worked!")
                break
