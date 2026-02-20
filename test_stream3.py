#!/usr/bin/env python3
"""
Strategy: Connect to relay FIRST to hold the slot, then signal camera via MQTT.
If camera connects to relay, they get paired and video flows.
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

def connect_relay(relay_url, relay_token):
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
    return tls, host

print('[1] Getting JWT...')
JWT_TOKEN = get_jwt()
print('JWT OK')

print('[2] Pre-connecting MQTT...')
mqtt_ready = threading.Event()

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        RELAY_REPLY = f'$tpiot/things/{DEVICE_ID_MD5}/relay/request/get/reply'
        res = client.subscribe(RELAY_REPLY, qos=0)
        print(f'Subscribe requested: {res}')
        mqtt_ready.set()

def on_subscribe(client, userdata, mid, granted_qos):
    print(f'  Subscribe ack mid={mid} qos={granted_qos}')

def on_message(client, userdata, msg):
    print(f'\n[MQTT msg] {msg.topic}: {msg.payload[:300]}')

def on_publish(client, userdata, mid):
    print(f'  MQTT publish acked mid={mid}')

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

# Multiple attempts with different timing strategies
for attempt in range(4):
    wait_before_relay = [0.0, 1.0, 2.0, 3.0][attempt]
    print(f'\n=== Attempt {attempt+1} (wait_before_relay={wait_before_relay}s) ===')
    
    # Get relay URL
    relay_url, relay_token = get_relay()
    print(f'Relay: {relay_url.split("?")[0]}')
    print(f'Token: {relay_token[:50]}')
    
    # Send MQTT signal to camera
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
    
    RELAY_PUB = f'$tpiot/things/{DEVICE_ID_MD5}/relay/request/get'
    pub_res = mqttc.publish(RELAY_PUB, mqtt_payload, qos=1)
    print(f'MQTT signal published (rc={pub_res.rc}), waiting {wait_before_relay}s...')
    time.sleep(wait_before_relay)
    
    # Connect to relay
    try:
        tls, relay_host = connect_relay(relay_url, relay_token)
        print(f'Connected to relay {relay_host}')
        print('Waiting for response...')
        
        tls.settimeout(30)
        data = bytearray()
        t0 = time.time()
        
        while time.time() - t0 < 30:
            try:
                chunk = tls.recv(32768)
                if not chunk:
                    break
                data.extend(chunk)
                
                text = bytes(data).decode(errors='replace')
                if data[:4] == b'HTTP' and '\r\n\r\n' in text:
                    header_end = text.index('\r\n\r\n')
                    status = text.split('\r\n')[0]
                    body = text[header_end+4:header_end+300]
                    print(f'Status: {status}')
                    print(f'Body start: {repr(body[:200])}')
                    
                    if '200' in status:
                        print('\n*** GOT 200! VIDEO STREAM ACTIVE! ***')
                        # Read more data
                        print(f'Reading video data...')
                        with open('/workspaces/tapo-cli/stream_raw.bin', 'wb') as f:
                            f.write(bytes(data))
                            while True:
                                try:
                                    more = tls.recv(65536)
                                    if not more:
                                        break
                                    f.write(more)
                                    data.extend(more)
                                    print(f'  Total: {len(data)} bytes', end='\r')
                                    if len(data) > 5 * 1024 * 1024:  # 5MB
                                        break
                                except socket.timeout:
                                    break
                        print(f'\nSaved {len(data)} bytes to stream_raw.bin')
                        mqttc.loop_stop()
                        sys.exit(0)
                    else:
                        # Parse error
                        try:
                            err = json.loads(body.strip())
                            print(f'Error: {err}')
                        except:
                            print(f'Non-JSON response')
                    break
                    
            except socket.timeout:
                print(f'Timeout ({time.time()-t0:.1f}s)')
                break
        
        tls.close()
    except Exception as e:
        print(f'Error: {e}')
    
    print('Waiting 2s...')
    time.sleep(2)

mqttc.loop_stop()
mqttc.disconnect()
print('Done')
