#!/usr/bin/env python3
"""
Test: pre-connection + immediate relay connect with 60s hold.
Mode=2 (DIRECT_FORWARDING) since pre-conn mode=2 held the connection earlier.
"""
import json, os, uuid, time, socket, ssl, requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

config = json.loads(open(os.path.expanduser("~/.tapo-cli/.config")).read())
token = config["token"]
DEVICE_ID = "80213E8CD1A65A20075764F44AFEB832236DB935"
APP_VER   = "3.17.109"
APP_UUID  = "a8b3c4d5-e6f7-89ab-cdef-012345678901"
CIPC      = "https://aps1-cipc-api.i.tplinkcloud.com"
UA        = f"tapo/SM-G950F/{APP_VER}_Android/Android 13"

# ── Step 1: Pre-connection ────────────────────────────────────────────
print("Pre-connecting...")
body = {"playerId": str(uuid.uuid4()),
        "requestList": [{"deviceId": DEVICE_ID, "streamType": 0, "cloudType": 1,
                         "rootCaVer": "1", "preConnection": 1, "resolution": "HD"}]}
r = requests.post(f"{CIPC}/v1/relay/preConnectionBatchRequest",
                  params={"source": "tapo-app"}, json=body,
                  headers={"Authorization": token, "User-Agent": UA,
                           "Content-Type": "application/json"},
                  verify=False, timeout=15)
d = r.json()
info       = d["preConnRespInfoList"][0]["relayAccessInfo"]
relay_url  = info["relayUrl"]
relay_token = info["relayToken"]
t_preconn  = time.time()
print(f"  relayUrl:   {relay_url}")
print(f"  relayToken: {relay_token}")

# ── Step 2: Parse URL ─────────────────────────────────────────────────
from urllib.parse import urlparse
p    = urlparse(relay_url)
host = p.hostname
port = p.port or 443
path = p.path + "?" + p.query + "&retryTime=0"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode    = ssl.CERT_NONE

def relay_connect(mode, label, hold=60):
    elapsed = time.time() - t_preconn
    print(f"\n[t+{elapsed:.1f}s] === {label} mode={mode} hold={hold}s ===", flush=True)
    try:
        raw  = socket.create_connection((host, port), timeout=15)
        sock = ctx.wrap_socket(raw, server_hostname=host)
    except Exception as e:
        print(f"  TCP/SSL error: {e}")
        return None, b""
    
    bnd = str(uuid.uuid4()).replace("-", "")
    headers = [
        f"POST {path} HTTP/1.1",
        f"Host: {host}",
        f"User-Agent: Client/{APP_VER}/1.3",
        "Accept: */*",
        f"Content-Type: multipart/mixed;boundary={bnd}",
        "Content-Length: 9223372036854775807",
        f"Keep-Relay: {hold}",
        f"X-token: {relay_token}",
        f"X-Pull-Mode: {mode}",
        "X-Version: 1.0",
        "X-Redirect-Times: 0",
        "X-Arrive-Latency: 0",
        "X-Client-Model: SM-G950F",
        f"X-Client-UUID: {APP_UUID}",
        "Connection: keep-alive",
        "", "",
    ]
    req = "\r\n".join(headers)
    t0  = time.time()
    sock.sendall(req.encode())
    print(f"  Sent. Waiting up to {hold}s for response...", flush=True)

    sock.settimeout(hold + 5)
    resp = b""
    try:
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                print(f"  Server closed at t+{time.time()-t0:.1f}s (recv={len(resp)}B)",
                      flush=True)
                break
            resp += chunk
            et = time.time() - t0
            if len(resp) < 8 and int(et) % 5 == 0 and et > 1:
                print(f"    still waiting t+{et:.0f}s...", flush=True)
    except socket.timeout:
        print(f"  Socket timeout after {time.time()-t0:.1f}s, recv={len(resp)} bytes")

    status = resp.split(b"\r\n")[0].decode("utf-8", "replace")
    end = resp.find(b"\r\n\r\n")
    body_bytes = resp[end + 4:] if end != -1 else b""
    print(f"  Status: {status!r}  t+{time.time()-t0:.1f}s")
    if body_bytes:
        print(f"  Body: {body_bytes[:300]}")
    sock.close()
    return status, body_bytes


# mode=2 immediately
status, body_bytes = relay_connect("2", "Immediate mode=2", hold=60)

if "200" in (status or ""):
    print(f"\n✅ Got 200 OK! Saving stream...")
    # Re-connect with longer hold
    relay_connect("2", "STREAM mode=2", hold=120)
else:
    # Try mode=1 immediately on fresh pre-conn
    print("\nRe-requesting pre-connection for mode=1 attempt...")
    r2 = requests.post(f"{CIPC}/v1/relay/preConnectionBatchRequest",
                       params={"source": "tapo-app"}, json=body,
                       headers={"Authorization": token, "User-Agent": UA,
                                "Content-Type": "application/json"},
                       verify=False, timeout=15)
    d2 = r2.json()
    info2 = d2["preConnRespInfoList"][0]["relayAccessInfo"]
    relay_url   = info2["relayUrl"]
    relay_token = info2["relayToken"]
    p2   = urlparse(relay_url)
    host = p2.hostname; port = p2.port or 443
    path = p2.path + "?" + p2.query + "&retryTime=0"
    t_preconn = time.time()
    print(f"  New relay: {relay_url}")
    
    relay_connect("1", "mode=1 immediate", hold=60)
