#!/bin/bash
###############################################################################
# Tapo Camera Cloud Recorder — One-click VPS Setup
#
# This script does EVERYTHING automatically:
#   1. Install system dependencies (Python 3, pip)
#   2. Clone the repo & install Python packages
#   3. Interactive config: Tapo token, Device ID, Drive folder ID
#   4. Setup Google Drive OAuth2 (interactive browser flow)
#   5. Create systemd service for background recording
#   6. Start the service & enable auto-start on boot
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/lannguyen19056/tapo-cloud/main/vps_setup.sh | bash
#   # or
#   wget -qO- https://raw.githubusercontent.com/lannguyen19056/tapo-cloud/main/vps_setup.sh | bash
#   # or
#   chmod +x vps_setup.sh && ./vps_setup.sh
#
###############################################################################
set -e

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No color

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ─── Configuration ────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/tapo-cloud"
REPO_URL="https://github.com/lannguyen19056/tapo-cloud.git"
SERVICE_NAME="tapo-recorder"
VENV_DIR="$INSTALL_DIR/venv"

# ─── Root check ───────────────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    err "Please run as root: sudo bash vps_setup.sh"
    exit 1
fi

echo ""
echo "============================================================"
echo "  Tapo Camera Cloud Recorder — VPS Setup"
echo "============================================================"
echo ""

# ─── Step 1: System Dependencies ─────────────────────────────────────────────
info "Step 1/6: Installing system dependencies..."

apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl > /dev/null 2>&1

PYTHON=$(command -v python3)
ok "Python3: $($PYTHON --version)"

# ─── Step 2: Clone / Update Repo ─────────────────────────────────────────────
info "Step 2/6: Setting up application..."

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull --ff-only origin main 2>/dev/null || true
else
    info "Cloning repository..."
    rm -rf "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# Create virtual environment
if [ ! -d "$VENV_DIR" ]; then
    info "Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

info "Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet requests urllib3 pycryptodome \
    google-api-python-client google-auth google-auth-httplib2 google-auth-oauthlib

ok "Dependencies installed."

# ─── Step 3: Tapo Camera Configuration ───────────────────────────────────────
info "Step 3/6: Configuring Tapo camera..."

CONFIG_FILE="$INSTALL_DIR/tapo_config.json"

if [ -f "$CONFIG_FILE" ]; then
    EXISTING_TOKEN=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('token',''))" 2>/dev/null || echo "")
    if [ -n "$EXISTING_TOKEN" ]; then
        echo ""
        ok "Existing config found (token: ${EXISTING_TOKEN:0:15}...)"
        read -p "   Keep existing config? [Y/n]: " KEEP_CONFIG
        KEEP_CONFIG=${KEEP_CONFIG:-Y}
    fi
fi

if [ "${KEEP_CONFIG^^}" != "Y" ] && [ "${KEEP_CONFIG^^}" != "" ] || [ ! -f "$CONFIG_FILE" ]; then
    echo ""
    echo "  You need your Tapo Cloud token and Device ID."
    echo "  Get them by logging into Tapo Cloud API or from tapo-cli.py"
    echo ""
    read -p "  Tapo Cloud Token: " TAPO_TOKEN
    read -p "  Device ID: " DEVICE_ID
    read -p "  CIPC API URL [https://aps1-cipc-api.i.tplinkcloud.com]: " CIPC_URL
    CIPC_URL=${CIPC_URL:-"https://aps1-cipc-api.i.tplinkcloud.com"}

    # Create minimal config
    cat > "$CONFIG_FILE" << TAPO_EOF
{
  "token": "$TAPO_TOKEN",
  "errorCode": "0"
}
TAPO_EOF

    # Update device ID in recorder if provided
    if [ -n "$DEVICE_ID" ]; then
        sed -i "s/DEVICE_ID = \".*\"/DEVICE_ID = \"$DEVICE_ID\"/" "$INSTALL_DIR/tapo_drive_recorder.py"
    fi
    if [ "$CIPC_URL" != "https://aps1-cipc-api.i.tplinkcloud.com" ]; then
        sed -i "s|CIPC_URL = \".*\"|CIPC_URL = \"$CIPC_URL\"|" "$INSTALL_DIR/tapo_drive_recorder.py"
    fi

    ok "Tapo config saved."
fi

# Fix SAVE_DIR to use install directory
sed -i "s|SAVE_DIR = .*|SAVE_DIR = \"$INSTALL_DIR/stream_output\"|" "$INSTALL_DIR/tapo_drive_recorder.py"
mkdir -p "$INSTALL_DIR/stream_output"

# ─── Step 4: Google Drive Configuration ──────────────────────────────────────
info "Step 4/6: Configuring Google Drive upload..."

OAUTH_TOKEN_FILE="$INSTALL_DIR/gdrive_oauth_token.json"
OAUTH_CLIENT_FILE="$INSTALL_DIR/gdrive_oauth_client.json"
DRIVE_FOLDER_ID=""

if [ -f "$OAUTH_TOKEN_FILE" ]; then
    ok "OAuth token already exists."
    read -p "   Re-setup OAuth? [y/N]: " REDO_OAUTH
    REDO_OAUTH=${REDO_OAUTH:-N}
fi

if [ "${REDO_OAUTH^^}" == "Y" ] || [ ! -f "$OAUTH_TOKEN_FILE" ]; then
    echo ""
    echo "  ── Google Drive OAuth2 Setup ──"
    echo ""
    echo "  You need OAuth2 Client credentials (Desktop app type)"
    echo "  from Google Cloud Console."
    echo ""
    echo "  If you already have the client_secret JSON file, paste its"
    echo "  FULL path. Otherwise, enter your client_id and client_secret."
    echo ""
    read -p "  Path to OAuth client JSON (or press Enter to input manually): " CLIENT_JSON_PATH

    if [ -n "$CLIENT_JSON_PATH" ] && [ -f "$CLIENT_JSON_PATH" ]; then
        cp "$CLIENT_JSON_PATH" "$OAUTH_CLIENT_FILE"
        ok "Client credentials copied."
    else
        echo ""
        echo "  Get these from: https://console.cloud.google.com/apis/credentials"
        echo "  Create credentials > OAuth client ID > Desktop app"
        echo ""
        read -p "  Client ID: " OAUTH_CLIENT_ID
        read -p "  Client Secret: " OAUTH_CLIENT_SECRET

        cat > "$OAUTH_CLIENT_FILE" << OAUTH_EOF
{"installed":{"client_id":"$OAUTH_CLIENT_ID","project_id":"tapo-recorder","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","auth_provider_x509_cert_url":"https://www.googleapis.com/oauth2/v1/certs","client_secret":"$OAUTH_CLIENT_SECRET","redirect_uris":["http://localhost"]}}
OAUTH_EOF
        ok "Client credentials saved."
    fi

    # Extract client_id and client_secret from JSON
    OAUTH_CLIENT_ID=$("$VENV_DIR/bin/python3" -c "import json; d=json.load(open('$OAUTH_CLIENT_FILE')); print(d.get('installed',d.get('web',{})).get('client_id',''))")
    OAUTH_CLIENT_SECRET=$("$VENV_DIR/bin/python3" -c "import json; d=json.load(open('$OAUTH_CLIENT_FILE')); print(d.get('installed',d.get('web',{})).get('client_secret',''))")

    if [ -z "$OAUTH_CLIENT_ID" ] || [ -z "$OAUTH_CLIENT_SECRET" ]; then
        err "Could not extract client_id/client_secret from JSON"
        exit 1
    fi

    # Generate auth URL
    AUTH_URL="https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=${OAUTH_CLIENT_ID}&redirect_uri=http://localhost&scope=https://www.googleapis.com/auth/drive&prompt=consent&access_type=offline"

    echo ""
    echo "  ════════════════════════════════════════════════════════"
    echo "  Open this URL in your browser:"
    echo ""
    echo -e "  ${CYAN}${AUTH_URL}${NC}"
    echo ""
    echo "  After authorizing, browser redirects to http://localhost/?code=..."
    echo "  The page won't load — that's OK!"
    echo "  Copy the FULL URL from the address bar."
    echo "  ════════════════════════════════════════════════════════"
    echo ""
    read -p "  Paste the full redirect URL: " REDIRECT_URL

    # Exchange code for token
    AUTH_CODE=$("$VENV_DIR/bin/python3" -c "
from urllib.parse import urlparse, parse_qs
import sys
url = '$REDIRECT_URL'
parsed = urlparse(url)
params = parse_qs(parsed.query)
if 'code' in params:
    print(params['code'][0])
else:
    print(url)
")

    "$VENV_DIR/bin/python3" -c "
import json, requests

resp = requests.post('https://oauth2.googleapis.com/token', data={
    'code': '''$AUTH_CODE''',
    'client_id': '$OAUTH_CLIENT_ID',
    'client_secret': '$OAUTH_CLIENT_SECRET',
    'redirect_uri': 'http://localhost',
    'grant_type': 'authorization_code',
})

if resp.status_code != 200:
    print(f'ERROR: {resp.status_code} {resp.text}')
    exit(1)

token = resp.json()
token_data = {
    'token': token['access_token'],
    'refresh_token': token.get('refresh_token'),
    'token_uri': 'https://oauth2.googleapis.com/token',
    'client_id': '$OAUTH_CLIENT_ID',
    'client_secret': '$OAUTH_CLIENT_SECRET',
    'scopes': ['https://www.googleapis.com/auth/drive'],
}
with open('$OAUTH_TOKEN_FILE', 'w') as f:
    json.dump(token_data, f, indent=2)
print('OK')
"

    if [ $? -ne 0 ]; then
        err "OAuth token exchange failed!"
        exit 1
    fi
    ok "OAuth token saved."
fi

# ─── Step 5: Google Drive Folder ID ──────────────────────────────────────────
info "Step 5/6: Setting up Google Drive folder..."

# List existing folders
echo ""
info "Listing your Google Drive root folders..."
"$VENV_DIR/bin/python3" -c "
import json
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

with open('$OAUTH_TOKEN_FILE') as f:
    td = json.load(f)

creds = Credentials(
    token=td['token'], refresh_token=td['refresh_token'],
    token_uri=td['token_uri'], client_id=td['client_id'],
    client_secret=td['client_secret']
)
try:
    if creds.expired or not creds.token:
        creds.refresh(Request())
        td['token'] = creds.token
        with open('$OAUTH_TOKEN_FILE', 'w') as f:
            json.dump(td, f, indent=2)
except:
    creds.refresh(Request())
    td['token'] = creds.token
    with open('$OAUTH_TOKEN_FILE', 'w') as f:
        json.dump(td, f, indent=2)

service = build('drive', 'v3', credentials=creds, cache_discovery=False)
res = service.files().list(
    q=\"mimeType='application/vnd.google-apps.folder' and 'root' in parents and trashed=false\",
    fields='files(id,name)', pageSize=20
).execute()

folders = res.get('files', [])
if folders:
    for i, f in enumerate(folders, 1):
        print(f'  {i}. {f[\"name\"]:30s} (ID: {f[\"id\"]})')
else:
    print('  (no folders found)')
" 2>/dev/null || warn "Could not list folders"

echo ""
read -p "  Enter Drive Folder ID (or press Enter to create 'TapoCamera' folder): " DRIVE_FOLDER_ID

if [ -z "$DRIVE_FOLDER_ID" ]; then
    info "Creating 'TapoCamera' folder on Google Drive..."
    DRIVE_FOLDER_ID=$("$VENV_DIR/bin/python3" -c "
import json
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

with open('$OAUTH_TOKEN_FILE') as f:
    td = json.load(f)

creds = Credentials(
    token=td['token'], refresh_token=td['refresh_token'],
    token_uri=td['token_uri'], client_id=td['client_id'],
    client_secret=td['client_secret']
)
if creds.expired or not creds.token:
    creds.refresh(Request())

service = build('drive', 'v3', credentials=creds, cache_discovery=False)

# Check if TapoCamera folder already exists
res = service.files().list(
    q=\"mimeType='application/vnd.google-apps.folder' and name='TapoCamera' and 'root' in parents and trashed=false\",
    fields='files(id)'
).execute()
if res.get('files'):
    print(res['files'][0]['id'])
else:
    meta = {'name': 'TapoCamera', 'mimeType': 'application/vnd.google-apps.folder'}
    f = service.files().create(body=meta, fields='id').execute()
    print(f['id'])
")
    ok "Folder 'TapoCamera' ready: $DRIVE_FOLDER_ID"
fi

# Save folder ID to env file
cat > "$INSTALL_DIR/.env" << ENV_EOF
GOOGLE_DRIVE_FOLDER_ID=$DRIVE_FOLDER_ID
ENV_EOF

ok "Drive folder ID saved: $DRIVE_FOLDER_ID"

# ─── Step 6: Create systemd Service ──────────────────────────────────────────
info "Step 6/6: Creating systemd service..."

cat > "/etc/systemd/system/${SERVICE_NAME}.service" << SERVICE_EOF
[Unit]
Description=Tapo Camera Cloud Recorder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$VENV_DIR/bin/python3 -u $INSTALL_DIR/tapo_drive_recorder.py
Restart=always
RestartSec=10
StandardOutput=journal+console
StandardError=journal+console
SyslogIdentifier=tapo-recorder

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=$INSTALL_DIR/stream_output $INSTALL_DIR/gdrive_oauth_token.json
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
SERVICE_EOF

# Reload and start
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

sleep 3
STATUS=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || echo "failed")

echo ""
echo "============================================================"
if [ "$STATUS" == "active" ]; then
    ok "Tapo Camera Recorder is RUNNING!"
else
    warn "Service status: $STATUS"
    echo "  Check logs: journalctl -u $SERVICE_NAME -f"
fi
echo "============================================================"
echo ""
echo "  Useful commands:"
echo "    View live logs:    journalctl -u $SERVICE_NAME -f"
echo "    Service status:    systemctl status $SERVICE_NAME"
echo "    Restart:           systemctl restart $SERVICE_NAME"
echo "    Stop:              systemctl stop $SERVICE_NAME"
echo "    Edit config:       nano $CONFIG_FILE"
echo "    Recordings:        ls $INSTALL_DIR/stream_output/"
echo ""
echo "  Install location:    $INSTALL_DIR"
echo "  Drive Folder ID:     $DRIVE_FOLDER_ID"
echo "============================================================"
