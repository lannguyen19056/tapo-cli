#!/usr/bin/env python3
"""
Test CIPC pre-connection endpoint:
  POST {cipcUrl}/v1/relay/preConnectionBatchRequest?source=tapo-app
  POST {cipcUrl}/v2/relay/preConnectionBatchRequest (User-Agent header)

These tell the cloud to signal the camera to pre-connect to a relay slot.
The response contains relayUrl + relayToken already embedded in it.
Then we connect to that relay and receive the stream.
"""

import json, os, sys, uuid, time, socket, ssl, hashlib, hmac, base64, random, string
import requests, urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ──────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.expanduser("~/.tapo-cli/.config")
config = json.loads(open(CONFIG_PATH).read())
token    = config["token"]
app_url  = config["appServerUrl"]
email    = config["email"]

DEVICE_ID  = "80213E8CD1A65A20075764F44AFEB832236DB935"
APP_TYPE   = "TP-Link_Tapo_Android"
APP_VER    = "3.17.109"
PLATFORM   = "Android"
LOCALE     = "en_US"
APP_UUID   = "a8b3c4d5-e6f7-89ab-cdef-012345678901"
USER_AGENT = f"tapo/SM-G950F/{APP_VER}_Android/{PLATFORM} 13"

# ── Helpers ──────────────────────────────────────────────────────────────

def get_service_url(service_id):
    endpoint = "/api/v2/common/getAppServiceUrl"
    url      = f"{app_url}{endpoint}?token={token}"
    content  = json.dumps({"serviceIds": [service_id]})
    now      = str(int(time.time()))
    nonce    = str(uuid.uuid1())
    access_key = "4d11b6b9d5ea4d19a829adbb9714b057"
    secret     = "6ed7d97f3e73467f8a5bab90b577ba4c"
    content_md5 = base64.b64encode(hashlib.md5(content.encode()).digest()).decode()
    payload = f"{content_md5}\n{now}\n{nonce}\n{endpoint}".encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha1).digest().hex()
    x_auth = f"Timestamp={now}, Nonce={nonce}, AccessKey={access_key}, Signature={sig}"
    headers = {
        "Content-Md5":     content_md5,
        "X-Authorization": x_auth,
        "Content-Type":    "application/json; charset=UTF-8",
    }
    r = requests.post(url, data=content, headers=headers, verify=False, timeout=15)
    d = r.json()
    if d.get("error_code") == 0:
        urls = d["result"].get("serviceUrls", {})
        if service_id in urls:
            return urls[service_id].rstrip("/")
    return None

def try_login():
    """Re-login to get fresh token."""
    import re
    pw = config.get("password", "")
    r = requests.post(
        f"https://wap.tplinkcloud.com",
        json={"method": "login", "params": {
            "appType": APP_TYPE, "cloudUserName": email, "cloudPassword": pw,
            "terminalUUID": APP_UUID}},
        verify=False, timeout=15)
    d = r.json()
    if d.get("error_code") == 0:
        new_token = d["result"]["token"]
        config["token"] = new_token
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
        print(f"  Re-logged in, new token: {new_token[:20]}...")
        return new_token
    print(f"  Login failed: {d}")
    return None

def connect_relay_and_stream(relay_url: str, relay_token: str, save_path: str = "stream_output.bin",
                              duration: int = 30, pre_conn: bool = True):
    """
    Connect to a CIPC relay URL using the relay protocol headers.
    The camera should already be connected (pre-connected) on the other side.
    
    Protocol (from AbstractRelayClient.N()):
      - X-Pull-Mode: "enable" for pre-connection, "2" for direct forwarding
      - Content-Type: multipart/mixed;boundary={uuid}
      - User-Agent: Client={appVersion}/1.3
      - Content-Length: 9223372036854775807
      - X-token: {relayToken}
      - X-Version: 1.0 (or 2.0 for devices that support it)
      - Keep-Relay: {duration}
      - X-Redirect-Times: 0
      - X-Arrive-Latency: 0
      - X-Client-Model: {model}
      - X-Client-UUID: {uuid}
    """
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
    parsed = urlparse(relay_url)
    host   = parsed.hostname
    port   = parsed.port or 443
    path   = parsed.path or "/"
    # Append &retryTime=0 to relay path (from AbstractRelayClient.Y())
    if parsed.query:
        path += "?" + parsed.query + "&retryTime=0"
    else:
        path += "?retryTime=0"

    print(f"\n  Connecting relay: {host}:{port}{path}")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE

    raw = socket.create_connection((host, port), timeout=15)
    sock = ctx.wrap_socket(raw, server_hostname=host)

    # PullMode: "1" = PRE_CONNECTION, "2" = DIRECT_FORWARDING
    pull_mode   = "1" if pre_conn else "2"
    boundary    = str(uuid.uuid4()).replace("-", "")
    client_uuid = str(uuid.uuid4())

    # HTTP/1.1 POST with streaming Content-Length (relay protocol from AbstractRelayClient.N())
    req  = (
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
        f"X-Client-UUID: {client_uuid}\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
    )
    sock.sendall(req.encode())

    # Read response status
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            break
        resp += chunk

    status_line = resp.split(b"\r\n")[0].decode("utf-8", errors="replace")
    print(f"  Status: {status_line}")

    # Check for error JSON in initial bytes
    body_start = resp.find(b"\r\n\r\n")
    if body_start != -1:
        initial_body = resp[body_start + 4:]
        if initial_body.startswith(b"{"):
            try:
                err = json.loads(initial_body.split(b"\r\n")[0])
                print(f"  Response JSON: {err}")
                sock.close()
                return False, err
            except Exception:
                pass

    if "200" not in status_line:
        print(f"  Non-200 response, not reading stream.")
        sock.close()
        return False, {"status": status_line}

    print(f"  Stream started! Saving to {save_path} for {duration}s...")
    sock.settimeout(5)
    start = time.time()
    total = 0
    with open(save_path, "wb") as f:
        # Write any body bytes already received
        if body_start != -1 and len(resp) > body_start + 4:
            data = resp[body_start + 4:]
            f.write(data)
            total += len(data)
        while time.time() - start < duration:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                if total % (100 * 1024) < 65536:
                    print(f"    {total//1024} KB received...", flush=True)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"    Socket error: {e}")
                break

    sock.close()
    print(f"  Total received: {total//1024} KB")
    return total > 0, {"bytes": total}


# ── Main ──────────────────────────────────────────────────────────────────

print("=" * 60)
print("STEP 1: Get CIPC URL")
print("=" * 60)
cipc_url = get_service_url("cipc.api.cloud")
if not cipc_url:
    cipc_url = get_service_url("cipc.api")
if not cipc_url:
    cipc_url = "https://aps1-cipc-api.i.tplinkcloud.com"
print(f"CIPC URL: {cipc_url}")

player_id = str(uuid.uuid4())
request_item = {
    "deviceId":     DEVICE_ID,
    "streamType":   0,      # STREAM_TYPE_PREVIEW
    "cloudType":    1,      # cloud relay
    "rootCaVer":    "1",
    "preConnection": 1,     # 1 = new pre-connection request
    "resolution":   "HD",
}
body = {
    "playerId":    player_id,
    "requestList": [request_item],
}

print("\n" + "=" * 60)
print("STEP 2: Call pre-connection V1")
print(f"  POST {cipc_url}/v1/relay/preConnectionBatchRequest?source=tapo-app")
print("=" * 60)

relay_url   = None
relay_token = None

for auth_format in [token, f"ut|{token}"]:
    print(f"\n  Auth: {auth_format[:25]}...")
    headers = {
        "Authorization": auth_format,
        "User-Agent":    USER_AGENT,
        "Content-Type":  "application/json; charset=UTF-8",
        "Accept":        "*/*",
    }
    r = requests.post(
        f"{cipc_url}/v1/relay/preConnectionBatchRequest",
        params={"source": "tapo-app"},
        json=body,
        headers=headers,
        verify=False,
        timeout=20,
    )
    print(f"  Status: {r.status_code}")
    print(f"  Response: {r.text[:500]}")
    if r.status_code == 200:
        d = r.json()
        if d.get("errorCode") == 0:
            items = d.get("preConnRespInfoList", [])
            if items and items[0].get("errorCode") == 0:
                info = items[0].get("relayAccessInfo", {})
                relay_url   = info.get("relayUrl")
                relay_token = info.get("relayToken") or info.get("sessionToken")
                print(f"\n  ✅ Pre-connection V1 succeeded!")
                print(f"  relayUrl:   {relay_url}")
                print(f"  relayToken: {relay_token}")
                break
            else:
                print(f"  Pre-conn item error: {items[0].get('errorCode') if items else 'no items'}")
        else:
            print(f"  errorCode: {d.get('errorCode')}")

if not relay_url:
    print("\n--- V1 failed, trying V2 ---")
    player_id = str(uuid.uuid4())
    body["playerId"] = player_id

    for auth_format in [token, f"ut|{token}"]:
        print(f"\n  Auth: {auth_format[:25]}...")
        headers = {
            "Authorization": auth_format,
            "User-Agent":    USER_AGENT,
            "Content-Type":  "application/json; charset=UTF-8",
            "Accept":        "*/*",
        }
        r = requests.post(
            f"{cipc_url}/v2/relay/preConnectionBatchRequest",
            json=body,
            headers=headers,
            verify=False,
            timeout=20,
        )
        print(f"  Status: {r.status_code}")
        print(f"  Response: {r.text[:500]}")
        if r.status_code == 200:
            d = r.json()
            if d.get("errorCode") == 0:
                items = d.get("preConnRespInfoList", [])
                if items and items[0].get("errorCode") == 0:
                    info        = items[0].get("relayAccessInfo", {})
                    relay_url   = info.get("relayUrl")
                    relay_token = info.get("relayToken") or info.get("sessionToken")
                    print(f"\n  ✅ Pre-connection V2 succeeded!")
                    print(f"  relayUrl:   {relay_url}")
                    print(f"  relayToken: {relay_token}")
                    break

if not relay_url:
    print("\n❌ Pre-connection failed. Trying fallback: get relay URL via /v1/relay/request then connect.")
    # Fallback: request relay URL with preConnection=1 and hope the camera connects
    fb_body = dict(request_item)
    fb_body["playerId"] = str(uuid.uuid4())
    fb_body["preConnection"] = 1
    for auth_format in [token, f"ut|{token}"]:
        h = {
            "Authorization": auth_format,
            "User-Agent":    USER_AGENT,
            "X-Request-Signature": "preconn-test",
            "Content-Type":  "application/json; charset=UTF-8",
        }
        r = requests.post(f"{cipc_url}/v1/relay/request",
                          params={"source": "tapo-app"},
                          json=fb_body, headers=h, verify=False, timeout=20)
        print(f"  Status: {r.status_code}  Body: {r.text[:300]}")
        if r.status_code == 200:
            d = r.json()
            result = d.get("result") or d
            relay_url   = result.get("relayUrl")
            relay_token = result.get("relayToken") or result.get("sessionToken")
            if relay_url:
                print(f"\n  Got relay URL via fallback: {relay_url}")
                break

if not relay_url:
    sys.exit("❌ Could not obtain relay URL by any method.")

print("\n" + "=" * 60)
print("STEP 3: Connect to relay and stream")
print("=" * 60)
# Wait a moment for camera to connect on the relay side
print("  Waiting 3s for camera to connect to relay...")
time.sleep(3)

ok, info = connect_relay_and_stream(relay_url, relay_token, "stream_preconn.bin", duration=30, pre_conn=True)
if ok:
    print(f"\n✅ Stream captured! File: stream_preconn.bin ({info.get('bytes',0)//1024} KB)")
    print("  Convert to video: ffmpeg -i stream_preconn.bin output.mp4")
else:
    print(f"\n❌ Stream failed: {info}")
