#!/usr/bin/env python3
"""
Test MQTT connection to Tapo cloud gateway and request relay URL for camera.
Flow:
  1. Get JWT from security server: POST /v2/auth/app
  2. Connect to MQTT: wss://aps1-app-cloudgateway.iot.i.tplinknbu.com/mqtt:443
  3. Subscribe to $tpiot/things/{deviceIdMD5}/relay/request/get/reply
  4. Publish relay request to $tpiot/things/{deviceIdMD5}/relay/request/get
  5. Receive relay URL + token
  6. Open HTTP relay connection to get video stream
"""

import json, time, uuid, ssl, requests, warnings, hashlib, threading, sys
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

# Derived
DEVICE_ID_MD5 = hashlib.md5(DEVICE_ID.encode()).hexdigest()
CLIENT_ID    = f'app:{APP_TYPE}:{APP_UUID}'
# Username format: appType:v{version}:{platform}:{locale}
MQTT_USERNAME = f'{APP_TYPE}:v{APP_VERSION}:Android:en_US'

print(f'Device ID MD5: {DEVICE_ID_MD5}')
print(f'MQTT ClientId: {CLIENT_ID}')
print(f'MQTT Username: {MQTT_USERNAME}')

# ─── Step 1: Get JWT ─────────────────────────────────────────────────────────
print('\n=== Step 1: Get JWT token ===')
r = requests.post(
    f'{SECURITY_URL}/v2/auth/app',
    headers={'app-cid': f'app:{APP_TYPE}:{APP_UUID}', 'Content-Type': 'application/json'},
    json={'appType': APP_TYPE, 'terminalUUID': APP_UUID, 'token': f'ut|{TOKEN}'},
    verify=False, timeout=10
)
print(f'Status: {r.status_code}')
if r.status_code != 200:
    print(f'Failed: {r.text}')
    sys.exit(1)
jwt_token = r.json()['jwt']
print(f'JWT: {jwt_token[:50]}...')

# ─── Step 2: MQTT connection ──────────────────────────────────────────────────
print('\n=== Step 2: Connect to MQTT ===')

import paho.mqtt.client as mqtt

relay_event = threading.Event()
relay_response = {}

RELAY_REPLY_TOPIC = f'$tpiot/things/{DEVICE_ID_MD5}/relay/request/get/reply'
RELAY_PUB_TOPIC   = f'$tpiot/things/{DEVICE_ID_MD5}/relay/request/get'
API_REPLY_TOPIC   = f'$tpiot/apps/{APP_TYPE}/{APP_UUID}/api/reply'
REFRESH_REPLY_TOPIC = f'$tpiot/app/{CLIENT_ID}/refresh/reply'

print(f'Subscribe: {RELAY_REPLY_TOPIC}')
print(f'Publish:   {RELAY_PUB_TOPIC}')

def on_connect(client, userdata, flags, rc):
    print(f'MQTT connected rc={rc}')
    if rc == 0:
        # Subscribe to reply topics
        client.subscribe(RELAY_REPLY_TOPIC, qos=1)
        client.subscribe(API_REPLY_TOPIC, qos=1)
        client.subscribe(REFRESH_REPLY_TOPIC, qos=1)
        print(f'Subscribed to relay reply topic')
        
        # Build relay request payload
        player_id = str(uuid.uuid4())
        payload = {
            'userToken': f'ut|{TOKEN}',
            'timestamp': int(time.time() * 1000),
            'requestRelayParams': {
                'relayParamsJson': {
                    'deviceId': DEVICE_ID,
                    'streamType': 0,
                    'cloudType': 1,
                    'playerId': player_id,
                    'rootCaVer': '1',
                    'preConnection': 0,
                    'resolution': 'HD'
                }
            },
            'clientToken': None
        }
        msg = json.dumps(payload, separators=(',', ':'))
        print(f'\nPublishing relay request...')
        print(f'Payload: {msg[:200]}')
        result = client.publish(RELAY_PUB_TOPIC, msg, qos=1)
        print(f'Publish result: {result.rc}')
    else:
        print(f'MQTT connection failed: rc={rc}')
        relay_event.set()

def on_message(client, userdata, message):
    topic = message.topic
    payload = message.payload.decode('utf-8', errors='replace')
    print(f'\nReceived message on {topic}:')
    try:
        data = json.loads(payload)
        print(json.dumps(data, indent=2)[:500])
        relay_response['data'] = data
        relay_response['topic'] = topic
    except:
        print(repr(payload[:200]))
    relay_event.set()

def on_disconnect(client, userdata, rc):
    print(f'MQTT disconnected rc={rc}')

def on_subscribe(client, userdata, mid, granted_qos):
    print(f'Subscribed mid={mid} qos={granted_qos}')

# Create client
mqttc = mqtt.Client(
    client_id=CLIENT_ID,
    transport='websockets',
    protocol=mqtt.MQTTv311
)
mqttc.username_pw_set(username=MQTT_USERNAME, password=jwt_token)
mqttc.ws_set_options(path=MQTT_PATH, headers={
    'Host': MQTT_HOST,
})
mqttc.tls_set(cert_reqs=ssl.CERT_NONE)
mqttc.tls_insecure_set(True)

mqttc.on_connect = on_connect
mqttc.on_message = on_message
mqttc.on_disconnect = on_disconnect
mqttc.on_subscribe = on_subscribe

# Keep-alive 235s as seen in app code
mqttc.connect(MQTT_HOST, MQTT_PORT, keepalive=235)
mqttc.loop_start()

# Wait up to 30s for relay response
print('Waiting for relay response (30s timeout)...')
got_response = relay_event.wait(timeout=30)

if not got_response:
    print('\nTimeout - No relay response received')
else:
    print('\n=== Got relay response ===')
    data = relay_response.get('data', {})
    
    # Check for relay URL in response
    relay_url = data.get('relayUrl') or data.get('result', {}).get('relayUrl') if isinstance(data.get('result'), dict) else None
    relay_token = data.get('relayToken') or data.get('result', {}).get('relayToken') if isinstance(data.get('result'), dict) else None
    
    print(f'Relay URL: {relay_url}')
    print(f'Relay Token: {relay_token}')
    
    if relay_url:
        print('\n=== Step 3: Connect to relay for video ===')
        print(f'Relay URL: {relay_url}')
        print('\nUse next script to connect to relay and save video')

mqttc.loop_stop()
mqttc.disconnect()
