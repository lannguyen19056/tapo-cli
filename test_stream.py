#!/usr/bin/env python3
"""
Coordinated Tapo camera stream capture:
1. Get JWT token
2. Get relay URL from CIPC
3. Open relay connection (as viewer/app)
4. THEN send MQTT signal to camera to connect to same relay
5. Receive video data and save to file
"""

import json, time, uuid, ssl, socket, requests, warnings, threading, sys, hashlib, random, string
import paho.mqtt.client as mqtt

warnings.filterwarnings('ignore')

# ─── Constants ───────────────────────────────────────────────────────────────
TOKEN        = 'e1c57f0f-CTRXwLqa1CVSF0Y7LQIvKL0'
DEVICE_ID    = '80213E8CD1A65A20075764F44AFEB832236DB935'
APP_UUID     = 'a8b3c4d5-e6f7-89ab-cdef-012345678901'
APP_TYPE     = 'TP-Link_Tapo_Android'
APP_VERSION  = '3.17.109'
SECURITY_URL = 'https://aps1-security.iot.i.tplinknbu.com'
MQTT_HOST    = 'aps1-app-cloudgateway.iot.i.tplinknbu.com'
MQTT_PORT    = 443
MQTT_PATH    = '/mqtt'
CIPC_URL     = 'https://aps1-cipc-api.i.tplinkcloud.com'
OUTPUT_FILE  = '/workspaces/tapo-cli/stream_output.bin'

DEVICE_ID_MD5 = hashlib.md5(DEVICE_ID.encode()).hexdigest()
CLIENT_ID    = f'app:{APP_TYPE}:{APP_UUID}'
MQTT_USERNAME = f'{APP_TYPE}:v{APP_VERSION}:Android:en_US'

print(f'Device MD5: {DEVICE_ID_MD5}')

# ─── Step 1: Get JWT ─────────────────────────────────────────────────────────
print('\n[1] Getting JWT...')
r = requests.post(
    f'{SECURITY_URL}/v2/auth/app',
    headers={'app-cid': f'app:{APP_TYPE}:{APP_UUID}', 'Content-Type': 'application/json'},
    json={'appType': APP_TYPE, 'terminalUUID': APP_UUID, 'token': f'ut|{TOKEN}'},
    verify=False, timeout=10
)
if r.status_code != 200:
    print(f'JWT failed: {r.text}'); sys.exit(1)
JWT_TOKEN = r.json()['jwt']
print(f'JWT OK: {JWT_TOKEN[:50]}...')

# ─── Step 2: Get relay URL ────────────────────────────────────────────────────
print('\n[2] Getting relay URL from CIPC...')
sig = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
headers = {
    'Authorization': TOKEN,
    'X-Source': 'tapo-app',
    'X-Ca-Type': 'cloud-self',
    'X-Request-Signature': sig,
    'Content-Type': 'application/json',
}
body = {'deviceId': DEVICE_ID, 'streamType': 0, 'cloudType': 1, 'rootCaVer': '1', 'preConnection': 0, 'resolution': 'HD'}
r = requests.post(f'{CIPC_URL}/v1/relay/request?source=tapo-app', headers=headers, json=body, verify=False, timeout=10)
if r.status_code != 200:
    print(f'CIPC failed: {r.text}'); sys.exit(1)
cipc = r.json().get('result', r.json())
RELAY_URL = cipc['relayUrl']
RELAY_TOKEN = cipc['relayToken']
print(f'Relay URL: {RELAY_URL}')
print(f'Relay Token: {RELAY_TOKEN}')

# Parse host + path from relay URL
from urllib.parse import urlparse
parsed = urlparse(RELAY_URL)
RELAY_HOST = parsed.hostname
RELAY_PATH = parsed.path + '?' + parsed.query + '&retryTime=0'

# ─── Step 3: Setup MQTT client (connect but don't publish yet) ───────────────
print('\n[3] Connecting to MQTT...')
mqtt_connected = threading.Event()
mqtt_relay_sent = threading.Event()

def on_connect(client, userdata, flags, rc):
    print(f'MQTT rc={rc}')
    if rc == 0:
        mqtt_connected.set()

def on_message(client, userdata, message):
    print(f'MQTT msg on {message.topic}:')
    try:
        print(json.dumps(json.loads(message.payload), indent=2)[:300])
    except:
        print(repr(message.payload[:200]))

mqttc = mqtt.Client(client_id=CLIENT_ID, transport='websockets', protocol=mqtt.MQTTv311)
mqttc.username_pw_set(username=MQTT_USERNAME, password=JWT_TOKEN)
mqttc.ws_set_options(path=MQTT_PATH, headers={'Host': MQTT_HOST})
mqttc.tls_set(cert_reqs=ssl.CERT_NONE)
mqttc.tls_insecure_set(True)
mqttc.on_connect = on_connect
mqttc.on_message = on_message
mqttc.connect(MQTT_HOST, MQTT_PORT, keepalive=235)
mqttc.loop_start()
if not mqtt_connected.wait(timeout=10):
    print('MQTT connect timeout'); sys.exit(1)
print('MQTT connected!')

# ─── Step 4: Open relay connection and simultaneously send MQTT signal ────────
print('\n[4] Opening relay connection + sending MQTT signal simultaneously...')

BOUNDARY = str(uuid.uuid4()).replace('-', '')

req_http = (
    f'POST {RELAY_PATH} HTTP/1.1\r\n'
    f'Host: {RELAY_HOST}\r\n'
    f'User-Agent: Client=tapo-app/1.3\r\n'
    f'Content-Type: multipart/mixed;boundary={BOUNDARY}\r\n'
    f'Content-Length: 9223372036854775807\r\n'
    f'X-token: {RELAY_TOKEN}\r\n'
    f'X-Pull-Mode: DIRECT\r\n'
    f'X-Version: 1.0\r\n'
    f'X-Arrive-Latency: 0\r\n'
    f'Keep-Relay: 60\r\n'
    f'X-Redirect-Times: 0\r\n'
    f'Connection: keep-alive\r\n'
    f'\r\n'
)

# Connect to relay
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
relay_sock = socket.create_connection((RELAY_HOST, 443), timeout=15)
tls_relay = ctx.wrap_socket(relay_sock, server_hostname=RELAY_HOST)
tls_relay.sendall(req_http.encode())
print(f'Relay request sent to {RELAY_HOST}')

# Immediately send MQTT signal to camera
mqtt_payload = {
    'userToken': f'ut|{TOKEN}',
    'timestamp': int(time.time() * 1000),
    'requestRelayParams': {
        'relayParamsJson': {
            'deviceId': DEVICE_ID,
            'streamType': 0,
            'cloudType': 1,
            'playerId': str(uuid.uuid4()),
            'rootCaVer': '1',
            'preConnection': 0,
            'resolution': 'HD'
        }
    },
    'clientToken': None
}
RELAY_PUB_TOPIC = f'$tpiot/things/{DEVICE_ID_MD5}/relay/request/get'
result = mqttc.publish(RELAY_PUB_TOPIC, json.dumps(mqtt_payload, separators=(',',':')), qos=0)
print(f'MQTT relay signal published: rc={result.rc}')

# ─── Step 5: Read relay response ─────────────────────────────────────────────
print('\n[5] Waiting for relay response...')
tls_relay.settimeout(30)

data_received = bytearray()
output_file = open(OUTPUT_FILE, 'wb')
start_time = time.time()
total_bytes = 0

try:
    while time.time() - start_time < 60:  # 60 second max
        try:
            chunk = tls_relay.recv(65536)
            if not chunk:
                print('Connection closed by server')
                break
            data_received.extend(chunk)
            output_file.write(chunk)
            output_file.flush()
            total_bytes += len(chunk)
            
            if total_bytes <= 2000:
                # Print headers/initial response
                print(f'Received {len(chunk)} bytes: {repr(chunk[:200])}')
            else:
                print(f'Total received: {total_bytes} bytes', end='\r')
                
            # Stop if we've received enough data
            if total_bytes >= 1024 * 1024 * 50:  # 50MB limit
                print(f'\nReceived 50MB, stopping')
                break
        except socket.timeout:
            print(f'\nTimeout after {total_bytes} bytes received')
            break
        except Exception as e:
            print(f'\nError: {e}')
            break
except KeyboardInterrupt:
    print(f'\nInterrupted - received {total_bytes} bytes')
finally:
    output_file.close()
    tls_relay.close()
    mqttc.loop_stop()
    mqttc.disconnect()

print(f'\nTotal data received: {total_bytes} bytes')
print(f'Saved to: {OUTPUT_FILE}')

if total_bytes > 0:
    print(f'\nFirst 500 bytes:')
    print(repr(bytes(data_received[:500])))
    
    # Check if it looks like HTTP response
    if data_received[:4] == b'HTTP':
        header_end = bytes(data_received).find(b'\r\n\r\n')
        if header_end > 0:
            print('\nHTTP Headers:')
            print(bytes(data_received[:header_end]).decode(errors='replace'))
            print('\nBody start:')
            print(repr(bytes(data_received[header_end+4:header_end+200])))
