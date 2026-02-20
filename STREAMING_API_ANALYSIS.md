# Tapo Camera Streaming/Relay/SFU API Analysis

## Complete reverse-engineering findings from TP-Link Tapo Android APK v3.17.109

---

## 1. Architecture Overview

The Tapo app uses **three distinct streaming transport mechanisms** to connect to cameras:

| Transport | Path | When Used |
|-----------|------|-----------|
| **P2P (Direct)** | Device-to-app UDP/TCP | Same LAN or NAT traversable |
| **Relay (CIPC HTTP)** | HTTP → CIPC relay server → device | P2P fails, cloud relay fallback |
| **SFU (WebRTC via MQTT)** | MQTT signaling → WebRTC media | Newer path, canary rollout |

The app makes a decision based on NAT type and connectivity:
- Attempts **P2P** first (`doP2pPreConnection`)
- Falls back to **Relay via CIPC** (`createRelayPreConnection`)
- For SFU/WebRTC: `vodSfuPreConnection` (VOD) or `subscribeWebRtcSfuTopic` (live)

Two optimization strategies exist:
- `RelayOptUtils_CIPC` — HTTP-based relay request via CIPC API
- `RelayOptUtils_MQTT` — MQTT-based relay request via IoT broker

---

## 2. Server Infrastructure

### CIPC API Servers (HTTP relay control plane)
```
# Production
http://aps1-cipc-api.i.tplinkcloud.com     # Asia-Pacific (ap-south-1)
http://euw1-cipc-api.i.tplinkcloud.com     # EU West (eu-west-1)
http://use1-cipc-api.i.tplinkcloud.com     # US East (us-east-1)

# Beta
http://aps1-cipc-api-beta.i.tplinkcloud.com
http://use1-cipc-api-beta.i.tplinkcloud.com
```

### Relay DCIPC Servers (actual media relay data plane)
```
# Production
http://aps1-relay-dcipc.i.tplinkcloud.com
http://euw1-relay-dcipc.i.tplinkcloud.com
http://use1-relay-dcipc.i.tplinkcloud.com

# Staging
http://aps1-relay-dcipc-staging.i.tplinkcloud.com
http://use1-relay-dcipc-staging.i.tplinkcloud.com

# Beta
http://aps1-relay-dcipc-beta.i.tplinkcloud.com
http://aps1-relay-dcipc-beta2.i.tplinkcloud.com
http://use1-relay-dcipc-beta2.i.tplinkcloud.com

# Alpha
http://aps1-relay-dcipc-alpha.i.tplinkcloud.com
```

### WAP Gateway (DCIPC variant)
```
https://n-wap-dcipc.tplinkcloud.com
```

### NBU App Server (used for REST API relay request)
```
https://euw1-app-tapo-care.i.tplinknbu.com  (from existing CLI)
https://api.i.tplinknbu.com                  (generic)
```

### Service URL Discovery
The app discovers CIPC URLs dynamically via:
```
POST /api/v2/common/getAppServiceUrl
Body: {"serviceIds": ["nbu.iot-app-server.app", "nbu.iot-cloud-gateway.app", "nbu.iot-security.appdevice", "cipc.api"]}
```
The `cipc.api` service ID returns the appropriate regional CIPC base URL.

---

## 3. Relay Request API (`/v1/relay/request`)

### Endpoint
```
POST {cipc_url}/v1/relay/request?source={source}
POST {cipc_url}/v2/relay/request
```

### Related Endpoints
```
POST {url}/v1/relay/preConnectionBatchRequest    # Pre-warm connections
POST {url}/v2/relay/preConnectionBatchRequest    # V2 pre-warm
GET  /relayservice?deviceid={deviceId}           # Relay service discovery
```

### Authentication
The CIPC API uses the **same authentication scheme** as the main Tapo cloud API:
- **Authorization header**: `ut|{token}` (for GET requests)
- **X-Authorization header**: HMAC-SHA1 signature (for POST requests)
  - Format: `Timestamp={ts}, Nonce={nonce}, AccessKey={access_key}, Signature={sig}`
  - Signature = HMAC-SHA1(secret, `Content-MD5\n{timestamp}\n{nonce}\n{endpoint}`)
- **Content-Type**: `application/json; charset=UTF-8`
- **Content-MD5**: Base64(MD5(body))

The token is the same `token` obtained from `/api/v2/account/login`.

### Request Body Structure

Based on the `RelayRequest` class (`com.tplink.libtapocameranetwork.model.camerahub.RelayRequest`):

```json
{
  "streamType": "MAIN_HD",        // or "MINOR_SD" 
  "deviceId": "{deviceId}",
  "resolution": "{resolution}",   // e.g. "720", "1080"
  "type": "{type}"                // query param format: ?deviceId=%s&type=%s&resolution=%s
}
```

The `RelayRequest` has a `toString()` pattern: `RelayRequest(streamType=...`

### Stream Types
- `MAIN_HD` — Main stream, high definition (1080p typically)
- `MINOR_SD` — Sub stream, standard definition (360p/480p)
- `MAIN_HIGHEST` — Possible highest resolution variant
- The app uses `bitStreamType` to track main vs sub stream switching
- `device.isForceMainStream and notSupportMinorStream use MAIN_HD` — logic for stream selection

### Response Structure

The response returns a `RelayInfo` object:
```
RelayInfo(elbCookie=..., ...)
```

Key response fields:
- `elbCookie` — ELB (Elastic Load Balancer) session cookie for sticky relay connections
- `relayIp` — IP address of the relay server
- `relayServerPort` / `relayServicePort` — TCP port for the relay media connection
- `relayToken` — Token for authenticating the relay media session
- `relayUrl` — Full URL for the relay connection
- `relayService` — Service identifier
- `relayServiceIPCandidates` — List of candidate relay service IPs
- `relayServiceKubernetesDiscovery` / `relayServiceTargetGroupDiscovery` — Service discovery metadata
- `lsfUrl` — Likely "Live Streaming Feed" URL

### Pre-Connection Flow
1. App calls `buildRelayPreConnection` or `buildRelayPreConnectionV2`
2. This calls `POST {url}/v1/relay/preConnectionBatchRequest` (or v2)
3. Returns `PreConnResult` / `SinglePreConnResult` with `RelayAccessInfo`
4. App opens TCP socket to `relayIp:relayServerPort`
5. Timing tracked: `RELAY_SERVER_SOCKET_CONNECT_START` → `RELAY_SERVER_SOCKET_CONNECT_END`

---

## 4. SFU/WebRTC API (`/v1/sfu/request`)

### Endpoints
```
POST {url}/v1/sfu/request     # Initiate SFU WebRTC session
GET  {url}/v1/sfu/query       # Query SFU status/availability
```

### MQTT-based SFU Signaling

The primary SFU path uses **MQTT** for WebRTC signaling (offer/answer exchange):

#### MQTT Topics
```
$tpiot/things/{thingName}/sfu/request/get           # Publish SFU request to device
$tpiot/things/{thingName}/sfu/request/get/reply      # Subscribe for SFU response from device
$tpiot/things/{thingName}/relay/request/get           # Publish relay request via MQTT
$tpiot/things/{thingName}/relay/request/get/reply     # Subscribe for relay response via MQTT
```

#### MQTT Auth Token
- Uses a dedicated `mqttAuthToken` (JWT)
- Obtained via `appAuth` (`/v2/auth/app`) → JWT result
- Refreshed via `refreshMqttAuthToken`
- Separate from the main cloud API token
- Flow: `refreshMqttAuthToken sdk isMqttAuthTokenExpired` → `refreshMqttAuthToken appAuth jwt result`

#### Key MQTT Data Classes
```
com.tplink.iot.cloud.bean.sfu.MqttOffer        # SDP offer sent to device
com.tplink.iot.cloud.bean.sfu.MqttAnswer        # SDP answer from device
com.tplink.iot.cloud.bean.sfu.MqttResult         # Generic result
com.tplink.iot.cloud.bean.sfu.MqttCommonResult   # Common result wrapper
com.tplink.iot.cloud.bean.sfu.MqttRtcParam       # RTC parameters
com.tplink.iot.cloud.bean.sfu.MqttSdpDetailInfo   # SDP detail information

com.tplink.libtpappcommonmedia.bean.webrtc.MqttSfuOffer    # SFU-specific offer
com.tplink.libtpappcommonmedia.bean.webrtc.MqttSfuAnswer   # SFU-specific answer
com.tplink.libtpappcommonmedia.bean.webrtc.MqttSfuCommonResult  # Common SFU result

com.tplink.iot.cloud.bean.relay.MqttGetRelayParam      # MQTT relay request params
com.tplink.iot.cloud.bean.relay.RelayParam              # Relay parameters
com.tplink.iot.cloud.bean.relay.RelayParamWrapper       # Relay param wrapper

com.tplink.libtpp2prelay.bean.mqtt.MqttGetRelayParam    # P2P relay MQTT params
com.tplink.libtpp2prelay.bean.mqtt.MqttResponse         # MQTT response
com.tplink.libtpp2prelay.bean.mqtt.MqttResultWrapper    # Result wrapper
com.tplink.libtpp2prelay.bean.mqtt.RelayParamWrapper    # Relay param wrapper
```

#### WebRTC SDP Offer
The app constructs a WebRTC SDP offer with:
```
m=video 9 UDP/TLS/RTP/SAVPF {codecs}
m=audio 9 UDP/TLS/RTP/SAVPF {codecs}
m=application 9 UDP/DTLS/SCTP webrtc-datachannel

a=msid-semantic: WMS myKvsVideoStream
a=ssrc:534320794 label:videoTrack
a=ssrc:534320794 mslabel:videoStream
a=ssrc:774875286 label:audioTrack
a=ssrc:774875286 mslabel:audioStream

a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time
```

The app includes:
- Video (H.264/AVC): `video/avc`
- Audio (AAC, PCM, G.722): `AUDIO_AAC`, `AUDIO_RAW`
- Data channel for control signaling
- DTLS-SRTP encryption (`UDP/TLS/RTP/SAVPF`)
- Uses fixed SSRC values (at least in templates)

#### SFU Publish/Subscribe Flow
```
1. subscribeWebRtcSfuTopic       → Subscribe to MQTT topic for SFU responses
2. publishWebRtcSfu              → Publish SDP offer to MQTT
3. publishWebRtcSfuInner         → Inner publish method
4. publishWebRtcSfu publishRelayGetInner → Relay GET via MQTT for SFU
5. sfu/request/get/reply         → Receive SDP answer
6. doOnRtcConnectSuccess         → WebRTC connection established
```

#### SFU Pre-Connection (Canary)
```
isWebRtcSfuPreConnectionCanaryUser → Feature flag check
vodSfuPreConnection                 → VOD playback via SFU
isRelayPreConnCanary                → Relay pre-connection canary flag
```

---

## 5. Camera Device Commands (P2P/Passthrough)

### SetPrepareRelay (sent to camera via passthrough)
```
Command: SetPrepareRelay
Class: com.tplinkra.camera.linkie.api.LinkieCameraCommand$Relay$SetPrepareRelay
Request: com.tplinkra.iot.devices.camera.impl.SetPrepareRelayRequest
Response: com.tplinkra.iot.devices.camera.impl.SetPrepareRelayResponse

Retry: setPrepareRelayRetry
Status: setPrepareRelay finish, response=[statusCode=%s, errorCode=%s]
```

### SetPrepareRTCSession (WebRTC via passthrough)
```
Command: SetPrepareRTCSession
Class: com.tplinkra.camera.linkie.api.LinkieCameraCommand$Rtp$SetPrepareRTCSession
Request: com.tplinkra.iot.devices.camera.impl.SetPrepareRTCSessionRequest
Response: com.tplinkra.iot.devices.camera.impl.SetPrepareRTCSessionResponse
```

### SetRTCSessionStatus
```
Command: SetRTCSessionStatus
Class: com.tplinkra.camera.linkie.api.LinkieCameraCommand$Rtp$SetRTCSessionStatus
Request: com.tplinkra.iot.devices.camera.impl.SetRTCSessionStatusRequest
Response: com.tplinkra.iot.devices.camera.impl.SetRTCSessionStatusResponse
```

### InitiateRTCSession (Google Home integration)
```
Action: action.devices.InitiateRTCSession
Class: com.tplinkra.devicecapability.actions.request.rtcSession.InitiateRTCSessionRequest
Builder: InitiateRTCSessionRequestBuilder
Related: action.devices.ConnectedRTCSession
         action.devices.DisconnectedRTCSession
SDP:     com.tplinkra.iot.devices.common.RTCSessionDescription
Format:  com.tplinkra.iot.devices.common.RTCSessionDescriptionFormat
```

### GetPreviewSnapshot (Relay)
```
Command: GetPreviewSnapshot
Class: com.tplinkra.camera.linkie.api.LinkieCameraCommand$Relay$GetPreviewSnapshot
```

### SetFrameType (Relay)
```
Command: SetFrameType
Class: com.tplinkra.camera.linkie.api.LinkieCameraCommand$Relay$SetFrameType
```

---

## 6. Authentication Deep Dive

### Token Types
1. **Cloud API Token** (`ut|{token}`) — From `/api/v2/account/login`, used for all cloud REST APIs
2. **MQTT Auth Token** (JWT) — From `/v2/auth/app` (`appAuth`), used for MQTT IoT broker connections
3. **Relay Token** (`relayToken`) — Returned from relay request, used for relay media session auth
4. **Video Sharing JWT** — Separate JWT for shared video access (`JwtVideoSharing`)
5. **IoT Token** — Device-specific token for passthrough commands
6. **Access Token** (`com.tplinkra.iot.authentication.model.AccessToken`) — Generic OAuth-like access token

### CIPC Authentication Flow
```
1. Login → Cloud token (ut|{token})
2. getAppServiceUrl(serviceIds: ["cipc.api"]) → CIPC base URL
3. POST {cipc_url}/v1/relay/request (with Cloud token auth + HMAC signature)
4. Response → RelayInfo {relayIp, relayServerPort, relayToken, elbCookie, ...}
5. Connect to relay server with relayToken
```

### MQTT Authentication Flow
```
1. POST /v2/auth/app (appAuth) → JWT result (mqttAuthToken)
2. Connect to MQTT broker using JWT
3. Subscribe to $tpiot/things/{thingName}/[relay|sfu]/request/get/reply
4. Publish to $tpiot/things/{thingName}/[relay|sfu]/request/get
5. Token refresh: refreshMqttAuthToken when isMqttAuthTokenExpired
```

---

## 7. Key Libraries & Classes

### Core Relay Library: `libtpp2prelay`
```
com.tplink.libtpp2prelay.client.AbstractRelayClient     — Base relay client
com.tplink.libtpp2prelay.client.SimpleCipcClient         — Simple HTTP CIPC client
com.tplink.libtpp2prelay.preconn.CameraCipcApi           — CIPC API interface (Retrofit)
com.tplink.libtpp2prelay.preconn.CameraRelayHelper       — Helper for camera relay connections
com.tplink.libtpp2prelay.preconn.model.PreConnParams     — Pre-connection request parameters
com.tplink.libtpp2prelay.preconn.model.PreConnResult     — Pre-connection result
com.tplink.libtpp2prelay.preconn.model.SinglePreConnResult — Single device pre-connection result
com.tplink.libtpp2prelay.preconn.model.RelayAccessInfo   — Relay server access info
com.tplink.libtpp2prelay.bean.RelayRequestLimit          — Rate limiting
com.tplink.libtpp2prelay.bean.RelayRequestCollect        — Request collection/batching
com.tplink.libtpp2prelay.bean.ConnectionParam            — Connection parameters
com.tplink.libtpp2prelay.bean.RequestUrlParam            — URL parameters
com.tplink.libtpp2prelay.bean.CustomParams               — Custom parameters
```

### Stream Pre-Connection Manager
```
com.tplink.libtpstreampreconnect.manager.PreConnectionManager
  — realDoRelayPreConnections
  — doBatCamPreRelay
  — doVideoPreConnection
  — createVodVideoPreConnection
com.tplink.libtpstreampreconnect.bean.StreamType
com.tplink.libtpstreampreconnect.bean.NatBean
com.tplink.libtpstreampreconnect.bean.NatStatistics
```

### Relay Refactor Utilities
```
com.tplink.iot.Utils.RelayRefactorUtilKt
  — getRelayCipcUrl()      → Returns CIPC URL for relay
  — isFFmpegCanaryUser()   → Whether user gets FFmpeg-based relay
```

### Relay URL Repository
```
com.tplink.libtpnetwork.TPCloudNetwork.repository.AppRelayUrlRepository
```

### WebRTC/SFU
```
com.tplink.libtpappcommonmedia.bean.webrtc.MqttSfuOffer
com.tplink.libtpappcommonmedia.bean.webrtc.MqttSfuAnswer
com.tplink.libtpappcommonmedia.bean.webrtc.MqttSfuCommonResult

com.tplink.iot.cloud.bean.sfu.MqttOffer
com.tplink.iot.cloud.bean.sfu.MqttAnswer
com.tplink.iot.cloud.bean.sfu.MqttResult
com.tplink.iot.cloud.bean.sfu.MqttCommonResult
com.tplink.iot.cloud.bean.sfu.MqttRtcParam
com.tplink.iot.cloud.bean.sfu.MqttSdpDetailInfo
```

---

## 8. Database Schema (Local Storage)

### DEVICE_INFO Table
```sql
CREATE TABLE "DEVICE_INFO" (
  "DEVICE_ID_MD5" TEXT PRIMARY KEY NOT NULL,
  "DEVICE_ALIAS" TEXT,
  "RESOLUTION" TEXT,
  "IS_LIVE_MUTE_AUDIO" INTEGER,
  "IS_PLAY_BACK_MUTE_AUDIO" INTEGER,
  "PRE_IMAGE_URL" TEXT,
  "PRE_IMAGE_URL_TIME" INTEGER,
  "PRE_IMAGE_URL_MAIN" TEXT,
  "PRE_IMAGE_URL_MINOR" TEXT,
  "PRE_IMAGE_URL_LDC" TEXT,
  "RELAY_SERVER_PORT" INTEGER,        -- ← Cached relay server port
  "LOCAL_PASSWORD" TEXT,
  "MOTOR_LAST_RESET_TIME" INTEGER,
  "MOTOR_LAST_CRUISE_VERTICAL_TIME" INTEGER,
  "MOTOR_LAST_CRUISE_HORIZONTAL_TIME" INTEGER,
  "AUTO_SWITCH_STREAM" INTEGER,       -- ← Auto stream quality switching
  "QUICK_RESP_ITEM_LIST" TEXT,
  "QUICK_RESP_CAPABILITY" TEXT,
  "VIDEO_RATIO" TEXT
);
```

### DOWNLOAD_BEAN Table
```sql
CREATE TABLE "DOWNLOAD_BEAN" (
  "IS_SUMMARY" INTEGER NOT NULL,
  "IS_TAPO" INTEGER NOT NULL,
  "FILE_NAME" TEXT,
  "CURRENT_CLIP_UUID" TEXT,
  "EVENT_ID" TEXT,
  "TIMESTAMP" INTEGER NOT NULL,
  "START_TIMESTAMP" INTEGER NOT NULL,
  "DEVICE_ALIAS" TEXT,
  "DEVICE_ID" TEXT,
  "DEVICE_MODEL" TEXT,
  "MODEL" TEXT,
  "DEVICE_TYPE" TEXT,
  "SNAPSHOT_URL" TEXT,
  "SNAPSHOT_KEY" TEXT,
  "SNAPSHOT_IV" TEXT,
  "SNAPSHOT_KEY_URI" TEXT,
  "SNAPSHOT_ENCRYPTION_METHOD" TEXT,
  "VIDEO_STREAM_URL" TEXT PRIMARY KEY NOT NULL,  -- ← Stream URL
  "DURATION" INTEGER,
  "VIDEO_URL" TEXT,
  "VIDEO_STATUS" TEXT,
  "VIDEO_ENCRYPTION_METHOD" TEXT,
  "VIDEO_KEY" TEXT,
  "VIDEO_IV" TEXT,
  "VIDEO_KEY_URI" TEXT,
  "ASSOCIATED_ACCOUNT" TEXT,
  "IS_CLOUD_FACE_TRACKING" INTEGER NOT NULL,
  "MULTI_TASK_ID" TEXT,
  "IDENTITY_ID" TEXT
);
```

---

## 9. Complete Endpoint Inventory

### Relay Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `{url}/v1/relay/request` | POST | Request relay connection |
| `{url}/v1/relay/request?source={source}` | POST | Request with source tracking |
| `{url}/v2/relay/request` | POST | V2 relay request |
| `{url}/v1/relay/preConnectionBatchRequest` | POST | Batch pre-warm relay connections |
| `{url}/v2/relay/preConnectionBatchRequest` | POST | V2 batch pre-warm |
| `/relayservice?deviceid={deviceId}` | GET | Relay service discovery |

### SFU Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `{url}/v1/sfu/request` | POST | SFU WebRTC session request |
| `{url}/v1/sfu/query` | GET | Query SFU availability |

### MQTT Topics (Streaming Signaling)
| Topic | Direction | Purpose |
|-------|-----------|---------|
| `$tpiot/things/{thingName}/relay/request/get` | App → Broker | Request relay via MQTT |
| `$tpiot/things/{thingName}/relay/request/get/reply` | Broker → App | Relay response |
| `$tpiot/things/{thingName}/sfu/request/get` | App → Broker | Request SFU via MQTT |
| `$tpiot/things/{thingName}/sfu/request/get/reply` | Broker → App | SFU response (SDP answer) |

### IPC Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/iot/ipc/{...}` | Various | IPC-specific operations |
| `/v1/ipc/recording` | Various | Recording management |
| `/v1/ipc/storage` | Various | Storage management |
| `/v1/activities/tapo/stream` | Various | Stream activity tracking |

### Video Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/video-sharing` | Various | Video sharing |
| `/v1/video-sharing/password` | Various | Sharing password |
| `/v2/videos/list` | GET | List cloud videos |
| `/v2/videos/devices` | GET | List video devices |
| `/v1/videos/timestamps` | GET | Video timestamps |
| `/v1/ai/summary/stream.m3u8` | GET | AI summary HLS stream |

---

## 10. Complete Live Stream Initiation Flow

### Path A: CIPC HTTP Relay (Primary)

```
┌─────────┐                       ┌──────────────┐                    ┌──────────────────┐
│  Tapo   │                       │  CIPC API    │                    │  Relay Server    │
│  App    │                       │  Server      │                    │  (DCIPC)         │
└────┬────┘                       └──────┬───────┘                    └────────┬─────────┘
     │                                    │                                     │
     │  1. POST /api/v2/common/           │                                     │
     │     getAppServiceUrl               │                                     │
     │  {"serviceIds":["cipc.api"]}       │                                     │
     │ ──────────────────────────────────> │                                     │
     │  ← {cipcApiUrl}                    │                                     │
     │                                    │                                     │
     │  2. POST {cipcUrl}/v1/relay/       │                                     │
     │     request?source=live            │                                     │
     │  Auth: ut|{token}                  │                                     │
     │  X-Authorization: HMAC-SHA1        │                                     │
     │  Body: {streamType,deviceId,...}   │                                     │
     │ ──────────────────────────────────> │                                     │
     │  ← RelayInfo{relayIp, relayPort,  │                                     │
     │     relayToken, elbCookie, ...}    │                                     │
     │                                    │                                     │
     │  3. TCP connect to relayIp:port    │                                     │
     │     with relayToken + elbCookie    │                                     │
     │ ──────────────────────────────────────────────────────────────────────> │
     │                                    │                                     │
     │  4. Relay data (H.264 + AAC/PCM)  │                                     │
     │ <══════════════════════════════════════════════════════════════════════> │
     │                                    │                                     │
```

### Path B: MQTT + WebRTC SFU (Newer)

```
┌─────────┐              ┌──────────────┐              ┌──────────────┐
│  Tapo   │              │  MQTT/IoT    │              │  Camera      │
│  App    │              │  Broker      │              │  Device      │
└────┬────┘              └──────┬───────┘              └──────┬───────┘
     │                          │                              │
     │  1. POST /v2/auth/app    │                              │
     │  → mqttAuthToken (JWT)   │                              │
     │                          │                              │
     │  2. MQTT CONNECT         │                              │
     │  (with JWT auth)         │                              │
     │ ────────────────────────>│                              │
     │                          │                              │
     │  3. SUBSCRIBE            │                              │
     │  $tpiot/things/{name}/   │                              │
     │   sfu/request/get/reply  │                              │
     │ ────────────────────────>│                              │
     │                          │                              │
     │  4. PUBLISH              │                              │
     │  $tpiot/things/{name}/   │  FORWARD                     │
     │   sfu/request/get        │ ────────────────────────────>│
     │  Body: MqttSfuOffer      │                              │
     │  {sdpOffer, rtcParams}   │                              │
     │ ────────────────────────>│                              │
     │                          │  5. sfu/request/get/reply    │
     │  6. RECEIVE              │ <────────────────────────────│
     │  MqttSfuAnswer           │                              │
     │  {sdpAnswer}             │                              │
     │ <────────────────────────│                              │
     │                          │                              │
     │  7. WebRTC P2P media     │                              │
     │  (DTLS-SRTP)             │                              │
     │ <═══════════════════════════════════════════════════════>│
     │                          │                              │
```

### Path C: P2P Direct (LAN/NAT Traversal)

```
1. Get device info from cloud (including P2P credentials)
2. NAT type detection (natType, natParams)
3. Direct P2P connection using libtpp2prelay library
4. Stream data directly device ↔ app
```

---

## 11. Summary of Unknowns / Areas for Further Investigation

1. **Exact JSON schema** of the relay request body — decompiling `RelayRequest`, `RelayParam`, `PreConnParams` classes with jadx would reveal all fields
2. **MQTT broker hostname** — likely `mqtt.tplinkcloud.com` or similar, extractable from connection strings
3. **SDP offer exact format** — the `MqttRtcParam` and `MqttSdpDetailInfo` classes would show the complete SDP construction
4. **Relay binary protocol** — after the TCP connection to the relay server, what binary protocol is used for media framing
5. **Rate limiting** — `RelayRequestLimit`, `RelayRequestLimitStatistics` suggest there are relay request limits
6. **elbCookie format** — AWS ELB sticky session cookie used for relay server affinity
7. **appSlaveSubDeviceToken** — NVR/Hub sub-device token authentication details

### Key Classes to Decompile (with jadx)
```
com.tplink.libtpp2prelay.preconn.CameraCipcApi
com.tplink.libtpp2prelay.client.SimpleCipcClient
com.tplink.libtpp2prelay.client.AbstractRelayClient
com.tplink.libtpp2prelay.preconn.model.PreConnParams
com.tplink.libtpp2prelay.preconn.model.RelayAccessInfo
com.tplink.libtpp2prelay.bean.mqtt.MqttGetRelayParam
com.tplink.libtapocameranetwork.model.camerahub.RelayRequest
com.tplink.iot.cloud.bean.relay.RelayParam
com.tplink.iot.cloud.bean.relay.RelayParamWrapper
com.tplink.iot.cloud.bean.sfu.MqttOffer
com.tplink.iot.cloud.bean.sfu.MqttAnswer
com.tplink.iot.cloud.bean.sfu.MqttRtcParam
com.tplink.iot.Utils.RelayRefactorUtilKt
com.tplink.libtpappcommonmedia.bean.webrtc.MqttSfuOffer
```
