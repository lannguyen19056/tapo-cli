#!/usr/bin/env python3
"""Direct OAuth2 token exchange - no Flow state mismatch issues.

Usage:
  python3 oauth_exchange.py                           # Account 1 → gdrive_oauth_token.json
  python3 oauth_exchange.py --account 2               # Account 2 → gdrive_oauth_token_2.json
  python3 oauth_exchange.py --account 3               # Account 3 → gdrive_oauth_token_3.json
  python3 oauth_exchange.py 'http://localhost/?code=...'                    # with code
  python3 oauth_exchange.py --account 2 'http://localhost/?code=...'        # account 2 + code
"""
import json, sys, os, requests
from urllib.parse import urlparse, parse_qs

# Parse arguments
account_num = 1
redirect_url = None

args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] == "--account" and i + 1 < len(args):
        account_num = int(args[i + 1])
        i += 2
    elif args[i].startswith("http"):
        redirect_url = args[i]
        i += 1
    else:
        i += 1

# Token filename
if account_num == 1:
    TOKEN_FILE = "gdrive_oauth_token.json"
else:
    TOKEN_FILE = f"gdrive_oauth_token_{account_num}.json"

# Load client credentials
with open("gdrive_oauth_client.json") as f:
    cdata = json.load(f)["installed"]

CLIENT_ID = cdata["client_id"]
CLIENT_SECRET = cdata["client_secret"]
REDIRECT_URI = "http://localhost"
SCOPES = "https://www.googleapis.com/auth/drive"

print(f"=== Setting up Account #{account_num} → {TOKEN_FILE} ===")
print()

if redirect_url is None:
    # Generate auth URL
    auth_url = (
        f"https://accounts.google.com/o/oauth2/auth"
        f"?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={SCOPES}"
        f"&prompt=consent"
        f"&access_type=offline"
    )
    print("Open this URL in your browser:")
    print()
    print(f"  {auth_url}")
    print()
    print("After authorizing, copy the FULL URL from browser address bar.")
    print()
    redirect_url = input("Paste URL here: ").strip()

# Extract code
parsed = urlparse(redirect_url)
params = parse_qs(parsed.query)
code = params["code"][0] if "code" in params else redirect_url

# Exchange code for token
resp = requests.post("https://oauth2.googleapis.com/token", data={
    "code": code,
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "redirect_uri": REDIRECT_URI,
    "grant_type": "authorization_code",
})

if resp.status_code != 200:
    print(f"ERROR: {resp.status_code} {resp.text}")
    sys.exit(1)

token = resp.json()
token_data = {
    "token": token["access_token"],
    "refresh_token": token.get("refresh_token"),
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "scopes": [SCOPES],
}
with open(TOKEN_FILE, "w") as f:
    json.dump(token_data, f, indent=2)

print(f"\nToken saved to {TOKEN_FILE}!")
print(f"\nTotal accounts configured: ", end="")

# Count existing token files
import glob
count = len(glob.glob("gdrive_oauth_token*.json"))
print(f"{count} (~{count * 15} GB total storage)")
