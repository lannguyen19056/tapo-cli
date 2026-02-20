#!/usr/bin/env python3
"""Direct OAuth2 token exchange - no Flow state mismatch issues."""
import json, sys, requests
from urllib.parse import urlparse, parse_qs

# Load client credentials
with open("gdrive_oauth_client.json") as f:
    cdata = json.load(f)["installed"]

CLIENT_ID = cdata["client_id"]
CLIENT_SECRET = cdata["client_secret"]
REDIRECT_URI = "http://localhost"
SCOPES = "https://www.googleapis.com/auth/drive"

if len(sys.argv) > 1:
    # Code provided as argument
    redirect_url = sys.argv[1]
else:
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
with open("gdrive_oauth_token.json", "w") as f:
    json.dump(token_data, f, indent=2)

print("Token saved to gdrive_oauth_token.json!")
