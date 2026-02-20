#!/usr/bin/env python3
"""Test script to call the CIPC relay API and get the live stream URL."""

import json, os, random, string, uuid, requests, urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Config ---
config = json.loads(open(os.path.expanduser('~/.tapo-cli/.config')).read())
token = config['token']
app_server_url = config['appServerUrl']
email = config['email']

DEVICE_ID = "80213E8CD1A65A20075764F44AFEB832236DB935"

# --- Helpers ---

def rand_sig_key(n=10):
    """Random alphanumeric string used as X-Request-Signature tracking key."""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=n))

def get_service_url(service_id):
    """Get a service URL from TP-Link cloud."""
    endpoint = '/api/v2/common/getAppServiceUrl'
    url = f"{app_server_url}{endpoint}?token={token}"
    content = json.dumps({"serviceIds": [service_id]})
    
    import hashlib, hmac, base64, time
    now = str(int(time.time()))
    nonce = str(uuid.uuid1())
    access_key = '4d11b6b9d5ea4d19a829adbb9714b057'
    secret = '6ed7d97f3e73467f8a5bab90b577ba4c'
    
    content_md5 = base64.b64encode(hashlib.md5(content.encode()).digest()).decode()
    payload = f"{content_md5}\n{now}\n{nonce}\n{endpoint}".encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha1).digest().hex()
    x_auth = f"Timestamp={now}, Nonce={nonce}, AccessKey={access_key}, Signature={sig}"
    
    headers = {
        'Content-Md5': content_md5,
        'X-Authorization': x_auth,
        'Content-Type': 'application/json; charset=UTF-8',
    }
    r = requests.post(url, data=content, headers=headers, verify=False)
    res = r.json()
    if res.get('error_code') == 0:
        urls = res['result'].get('serviceUrls', {})
        if service_id in urls:
            return urls[service_id].rstrip('/')
    return None

# Get CIPC API URL dynamically
print("Getting CIPC API URL...")
cipc_url = get_service_url("cipc.api.cloud")
if not cipc_url:
    cipc_url = get_service_url("cipc.api")
print(f"CIPC URL: {cipc_url}")

# Build request body
player_id = str(uuid.uuid4())
body = {
    "deviceId": DEVICE_ID,
    "streamType": 0,       # 0 = preview/live stream
    "cloudType": 1,        # 1 = cloud
    "deviceType": "IPCAMERA",
    "playerId": player_id,
    "rootCaVer": "1",
    "preConnection": 0,    # 0 = normal (not pre-connection)
    "resolution": "HD",
    "channelId": 0,
}

sig_key = rand_sig_key(10)
user_agent = "tapo/SM-G950F/3.17.109_Android/Android 13"

# --- Try V2 first ---
print("\n--- Trying V2 (POST /v2/relay/request) ---")
headers_v2 = {
    "Authorization": token,           # no ut| prefix based on interceptor
    "X-Request-Signature": sig_key,
    "User-Agent": user_agent,
    "X-Source": "tapo-app",
    "X-Ca-Type": "cloud-self",
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "*/*",
    "Connection": "Keep-alive",
}
r = requests.post(f"{cipc_url}/v2/relay/request", json=body, headers=headers_v2, verify=False)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:500]}")

# --- Try V2 with ut| prefix ---
print("\n--- Trying V2 with ut| Authorization ---")
headers_v2b = dict(headers_v2)
headers_v2b["Authorization"] = f"ut|{token}"
r = requests.post(f"{cipc_url}/v2/relay/request", json=body, headers=headers_v2b, verify=False)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:500]}")

# --- Try V1 ---
print("\n--- Trying V1 (POST /v1/relay/request?source=tapo-app) ---")
headers_v1 = {
    "Authorization": token,
    "X-Request-Signature": sig_key,
    "User-Agent": user_agent,
    "X-Source": "tapo-app",
    "X-Ca-Type": "cloud-self",
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "*/*",
    "Connection": "Keep-alive",
}
r = requests.post(f"{cipc_url}/v1/relay/request", params={"source": "tapo-app"}, json=body, headers=headers_v1, verify=False)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:500]}")

# --- Try V1 with ut| prefix ---
print("\n--- Trying V1 with ut| Authorization ---")
headers_v1b = dict(headers_v1)
headers_v1b["Authorization"] = f"ut|{token}"
r = requests.post(f"{cipc_url}/v1/relay/request", params={"source": "tapo-app"}, json=body, headers=headers_v1b, verify=False)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:500]}")
