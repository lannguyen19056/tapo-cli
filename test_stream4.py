#!/usr/bin/env python3
"""
Key insight: The relay server pairs by deviceId. The camera connects to relay_X.
We must also connect to relay_X. We need to call CIPC AFTER camera has started
connecting, so we're assigned to the same relay server.

Flow:
 1. Pre-connect MQTT
 2. Send MQTT signal to camera (start the clock)
 3. Wait N seconds (camera receives signal, calls CIPC, connects to relay_X)
 4. Call CIPC ourselves (should get relay_X since camera reserved it)
 5. Connect to relay_X immediately
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

def get_jwt():
    r = requests.post(
        f'{SECURITY_URL}/v2/auth/app',
        headers={'app-cid': CLIENT_ID, 'Content-Type': 'application/json'},
        json={'appType': APP_TYPE, 'terminalUUID': APP_UUID, 'token': f'ut|{TOKEN}'},
        verify=False, timeout=10
    )
    return r.json()['jwt']

def get_relay():
    sig = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
    r = requests.post(
        f'{CIPC_URL}/v1/relay/request?source=tapo-app',
        headers={
            'Authorization': TOKEN, 'X-Source': 'tapo-app',
            'X-Ca-Type': 'cloud-self', 'X-Request-Signature': sig,
            'Content-Type': 'application/json'
        },
        json={'deviceId': DEVICE_ID, 'streamType': 0, 'cloudType': 1,
              'rootCaVer': '1', 'preConnection': 0, 'resolution': 'HD'},
        verify=False, timeout=10
    )
    result = r.json().get('result', r.json())
    return result['relayUrl'], result['relayToken']

def connect_relay_and_read(relay_url, relay_token, timeout_s=35):
    from urllib.parse import urlparse
    parsed = urlparse(relay_url)
    host = parsed.hostname
    path = parsed.path + '?' + parsed.query + '&retryTime=0'
    boundary = str(uuid.uuid4()).replace('-', '')
    
    req = (
        f'POST {path} HTTP/1.1\r\n'
        f'Host: {host}\r\n'
        f'User-Agent: Client=tapo-app/1.3\r\n'
        f'Content-Type: multipart/mixed;boundary={boundary}\r\n'
        f'Content-Length: 9223372036854775807\r\n'
        f'X-token: {relay_token}\r\n'
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
    sock = socket.create_connection((host, 443), timeout=15)
    tls = ctx.wrap_socket(sock, server_hostname=host)
    tls.sendall(req.encode())
    print(f'  Connected to {host}')
    
    tls.settimeout(timeout_s)
    data = bytearray()
    t0 = time.time()
    
    while time.time() - t0 < timeout_s:
        try:
            chunk = tls.recv(65536)
            if not chunk:
                print('  Server closed connection')
                break
            data.extend(chunk)
            
            if data[:4] == b'HTTP':
                hdr_end = bytes(data).find(b'\r\n\r\n')
                if hdr_end > 0:
                    status = bytes(data[:hdr_end]).decode(errors='replace').split('\r\n')[0]
                    body = bytes(data[hdr_end+4:hdr_end+300])
                    print(f'  Status: {status}')
                    try:
                        err_data = json.loads(body)
                        print(f'  Response: {err_data}')
                        code = err_data.get('errorCode', 0)
                        reason = err_data.get('closeReason', '')
                        if code != 0:
                            return False, reason, data
                    except:
                        pass
                    if '200' in status:
                        print('  *** HTTP 200 - Stream active! ***')
                        # Keep reading
                        tls.settimeout(60)
                        while len(data) < 10 * 1024 * 1024:
                            try:
                                more = tls.recv(65536)
                                if not more:
                                    break
                                data.extend(more)
                                print(f'  Received: {len(data)} bytes', end='\r')
                            except socket.timeout:
                                break
                        return True, '200_ok', data
                    break
        except socket.timeout:
            print(f'  Read timeout after {len(data)} bytes')
            break
    
    tls.close()
    return False, 'timeout', data

# ─── Main ────────────────────────────────────────────────────────────────────
print('[1] Getting JWT...')
JWT_TOKEN = get_jwt()
print('JWT OK')

print('[2] Pre-connecting MQTT...')
mqtt_ready = threading.Event()
pub_acked = threading.Event()

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        RELAY_REPLY = f'$tpiot/things/{DEVICE_ID_MD5}/relay/request/get/reply'
        client.subscribe(RELAY_REPLY, qos=0)
        mqtt_ready.set()

def on_subscribe(client, userdata, mid, granted_qos):
    print(f'  Subscribe ack mid={mid} qos={granted_qos}')

def on_message(client, userdata, msg):
    print(f'\n[MQTT msg] {msg.topic}: {msg.payload[:300]}')

def on_publish(client, userdata, mid):
    print(f'  MQTT PUBACK mid={mid}')
    pub_acked.set()

mqttc = mqtt.Client(client_id=CLIENT_ID, transport='websockets', protocol=mqtt.MQTTv311)
mqttc.username_pw_set(username=MQTT_USERNAME, password=JWT_TOKEN)
mqttc.ws_set_options(path=MQTT_PATH, headers={'Host': MQTT_HOST})
mqttc.tls_set(cert_reqs=ssl.CERT_NONE)
mqttc.tls_insecure_set(True)
mqttc.on_connect = on_connect
mqttc.on_message = on_message
mqttc.on_subscribe = on_subscribe
mqttc.on_publish = on_publish
mqttc.connect(MQTT_HOST, MQTT_PORT, keepalive=235)
mqttc.loop_start()
mqtt_ready.wait(timeout=10)
print('MQTT ready')

# Try different wait times between MQTT signal and CIPC call
for attempt, wait_s in enumerate([2, 4, 6, 8]):
    print(f'\n=== Attempt {attempt+1}: wait {wait_s}s after MQTT signal ===')
    pub_acked.clear()
    
    # Build MQTT payload
    mqtt_payload = json.dumps({
        'userToken': f'ut|{TOKEN}',
        'timestamp': int(time.time() * 1000),
        'requestRelayParams': {
            'relayParamsJson': {
                'deviceId': DEVICE_ID, 'streamType': 0, 'cloudType': 1,
                'playerId': str(uuid.uuid4()), 'rootCaVer': '1',
                'preConnection': 0, 'resolution': 'HD'
            }
        },
        'clientToken': None
    }, separators=(',',':'))
    
    # Step 1: Send MQTT signal
    RELAY_PUB = f'$tpiot/things/{DEVICE_ID_MD5}/relay/request/get'
    pub_res = mqttc.publish(RELAY_PUB, mqtt_payload, qos=1)
    t_signal = time.time()
    print(f'  MQTT signal sent at t=0 (rc={pub_res.rc})')
    
    # Wait for PUBACK
    puback = pub_acked.wait(timeout=5)
    print(f'  PUBACK: {"YES" if puback else "NO (5s timeout)"}')
    
    # Wait N seconds for camera to receive, call CIPC, connect to relay
    elapsed = time.time() - t_signal
    remaining = wait_s - elapsed
    if remaining > 0:
        print(f'  Waiting {remaining:.1f}s more (total {wait_s}s from signal)...')
        time.sleep(remaining)
    
    # Step 2: Call CIPC (should get same relay server as camera)
    print(f'  Calling CIPC at t={time.time()-t_signal:.1f}s...')
    relay_url, relay_token = get_relay()
    print(f'  Relay: {relay_url.split("/relayservice")[0]}')
    
    # Step 3: Connect to relay immediately
    print(f'  Connecting to relay at t={time.time()-t_signal:.1f}s...')
    success, reason, data = connect_relay_and_read(relay_url, relay_token, timeout_s=20)
    
    if success:
        print(f'\n*** SUCCESS! Got {len(data)} bytes ***')
        with open('/workspaces/tapo-cli/stream_raw.bin', 'wb') as f:
            f.write(bytes(data))
        print('Saved to stream_raw.bin')
        break
    else:
        print(f'  Result: {reason}')
    
    # Wait before retry
    if attempt < 3:
        print(f'  Retrying in 3s...')
        time.sleep(3)

mqttc.loop_stop()
mqttc.disconnect()
print('\nDone')
