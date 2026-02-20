#!/usr/bin/env python3
"""
Fast-coordination Tapo stream capture.
The relay server pairs camera + app by deviceId.
Steps:
 1. Connect MQTT (pre-connect)
 2. Call CIPC to get relay URL
 3. Send MQTT signal to camera (to tell it to connect)
 4. IMMEDIATELY connect to relay URL from step 2
 5. Read video data
"""

import json, time, uuid, ssl, socket, requests, warnings, threading, sys, hashlib, random, string
import paho.mqtt.client as mqtt

warnings.filterwarnings('ignore')

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

DEVICE_ID_MD5 = hashlib.md5(DEVICE_ID.encode()).hexdigest()
CLIENT_ID     = f'app:{APP_TYPE}:{APP_UUID}'
MQTT_USERNAME = f'{APP_TYPE}:v{APP_VERSION}:Android:en_US'

# ─── Get JWT ─────────────────────────────────────────────────────────────────
print('[1] Getting JWT...')
r = requests.post(
    f'{SECURITY_URL}/v2/auth/app',
    headers={'app-cid': CLIENT_ID, 'Content-Type': 'application/json'},
    json={'appType': APP_TYPE, 'terminalUUID': APP_UUID, 'token': f'ut|{TOKEN}'},
    verify=False, timeout=10
)
JWT_TOKEN = r.json()['jwt']
print(f'JWT OK')

# ─── Pre-connect MQTT ────────────────────────────────────────────────────────
print('[2] Pre-connecting MQTT...')
mqtt_ready = threading.Event()
mqtt_messages = []

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        # Subscribe to relay reply with QoS 0
        RELAY_REPLY = f'$tpiot/things/{DEVICE_ID_MD5}/relay/request/get/reply'
        client.subscribe(RELAY_REPLY, qos=0)
        mqtt_ready.set()

def on_message(client, userdata, msg):
    print(f'\n[MQTT] {msg.topic}: {msg.payload[:200]}')
    try:
        mqtt_messages.append(json.loads(msg.payload))
    except:
        mqtt_messages.append({'raw': msg.payload.decode(errors='replace')})

mqttc = mqtt.Client(client_id=CLIENT_ID, transport='websockets', protocol=mqtt.MQTTv311)
mqttc.username_pw_set(username=MQTT_USERNAME, password=JWT_TOKEN)
mqttc.ws_set_options(path=MQTT_PATH, headers={'Host': MQTT_HOST})
mqttc.tls_set(cert_reqs=ssl.CERT_NONE)
mqttc.tls_insecure_set(True)
mqttc.on_connect = on_connect
mqttc.on_message = on_message
mqttc.connect(MQTT_HOST, MQTT_PORT, keepalive=235)
mqttc.loop_start()
mqtt_ready.wait(timeout=10)
print('MQTT ready')

# Try multiple relay attempts
for attempt in range(3):
    print(f'\n=== Attempt {attempt+1} ===')
    
    # ─── Get fresh relay URL ──────────────────────────────────────────────────
    sig = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
    headers_cipc = {
        'Authorization': TOKEN,
        'X-Source': 'tapo-app',
        'X-Ca-Type': 'cloud-self',
        'X-Request-Signature': sig,
        'Content-Type': 'application/json',
    }
    body = {'deviceId': DEVICE_ID, 'streamType': 0, 'cloudType': 1, 'rootCaVer': '1', 'preConnection': 0, 'resolution': 'HD'}
    r = requests.post(f'{CIPC_URL}/v1/relay/request?source=tapo-app', headers=headers_cipc, json=body, verify=False, timeout=10)
    cipc = r.json().get('result', r.json())
    RELAY_URL = cipc['relayUrl']
    RELAY_TOKEN = cipc['relayToken']
    print(f'Relay URL: {RELAY_URL}')
    print(f'Relay Token: {RELAY_TOKEN[:50]}')
    
    from urllib.parse import urlparse
    parsed = urlparse(RELAY_URL)
    RELAY_HOST = parsed.hostname
    RELAY_PATH_FULL = parsed.path + '?' + parsed.query + '&retryTime=0'
    
    # ─── Send MQTT signal to camera ───────────────────────────────────────────
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
    pub_result = mqttc.publish(RELAY_PUB_TOPIC, json.dumps(mqtt_payload, separators=(',',':')), qos=1)
    print(f'MQTT signal sent (rc={pub_result.rc}), waiting 1.5s for camera to connect to relay...')
    time.sleep(1.5)  # wait for camera to receive signal, call CIPC, connect to relay
    
    # ─── Connect to relay immediately ─────────────────────────────────────────
    BOUNDARY = str(uuid.uuid4()).replace('-', '')
    req_http = (
        f'POST {RELAY_PATH_FULL} HTTP/1.1\r\n'
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
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    try:
        sock = socket.create_connection((RELAY_HOST, 443), timeout=15)
        tls = ctx.wrap_socket(sock, server_hostname=RELAY_HOST)
        tls.sendall(req_http.encode())
        print('Relay HTTP request sent, reading response (30s)...')
        
        tls.settimeout(30)
        received = bytearray()
        t0 = time.time()
        success = False
        
        while time.time() - t0 < 30:
            try:
                chunk = tls.recv(65536)
                if not chunk:
                    print('Server closed connection')
                    break
                received.extend(chunk)
                
                if len(received) <= 2000:
                    print(f'  chunk {len(chunk)}b: {repr(bytes(chunk[:100]))}')
                    
                # Check for error response
                if len(received) >= 12 and received[:4] == b'HTTP':
                    hdr_end = bytes(received).find(b'\r\n\r\n')
                    if hdr_end > 0:
                        hdr = bytes(received[:hdr_end]).decode(errors='replace')
                        body_start = bytes(received[hdr_end+4:hdr_end+200])
                        status_line = hdr.split('\r\n')[0]
                        print(f'  Status: {status_line}')
                        try:
                            err = json.loads(body_start)
                            print(f'  Body: {err}')
                            if err.get('errorCode') not in (0, None) or err.get('closeReason'):
                                print(f'  Error from relay, trying next attempt...')
                                break
                        except:
                            pass
                        if '200' in status_line:
                            print('  GOT 200 - video stream active!')
                            success = True
                
                if success and len(received) > 1024:
                    print(f'\nSaving {len(received)} bytes...')
                    with open('/workspaces/tapo-cli/stream.bin', 'wb') as f:
                        f.write(bytes(received))
                    # Continue reading until 10MB or 60s
                    
            except socket.timeout:
                print(f'Read timeout after {len(received)} bytes')
                break
                
        tls.close()
        
        if success:
            print(f'\nStream received! Total: {len(received)} bytes')
            break
            
    except Exception as e:
        print(f'Relay connect error: {e}')
    
    print(f'Waiting 3s before retry...')
    time.sleep(3)

mqttc.loop_stop()
mqttc.disconnect()
print('Done')
