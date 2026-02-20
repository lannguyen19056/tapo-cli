#!/usr/bin/env python3
"""One-time OAuth2 setup for Google Drive uploads.

This script helps you authenticate with your personal Google account
so the recorder can upload files to YOUR Drive (with your storage quota).

Prerequisites:
  1. Go to https://console.cloud.google.com/apis/credentials
     (project: ttbdtvthueserver001 or create a new one)
  2. Click "+ CREATE CREDENTIALS" → "OAuth client ID"
  3. Application type: "Desktop app", Name: anything
  4. Download the JSON → save as ./gdrive_oauth_client.json
  5. Run: python3 setup_gdrive_oauth.py

After successful auth, a token file (gdrive_oauth_token.json) will be created.
The recorder will use this token for uploads.
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET_FILE = os.path.join(SCRIPT_DIR, "gdrive_oauth_client.json")
TOKEN_FILE = os.path.join(SCRIPT_DIR, "gdrive_oauth_token.json")
SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    if not os.path.exists(CLIENT_SECRET_FILE):
        print("=" * 60)
        print("ERROR: OAuth2 client credentials not found!")
        print("=" * 60)
        print()
        print("Please follow these steps:")
        print()
        print("1. Go to: https://console.cloud.google.com/apis/credentials")
        print("2. Select project 'ttbdtvthueserver001' (or your project)")
        print("3. Click '+ CREATE CREDENTIALS' → 'OAuth client ID'")
        print("4. Application type: 'Desktop app'")
        print("5. Click 'Create' → 'Download JSON'")
        print(f"6. Save the file as: {CLIENT_SECRET_FILE}")
        print("7. Run this script again")
        print()
        print("Also make sure Google Drive API is enabled:")
        print("   https://console.cloud.google.com/apis/library/drive.googleapis.com")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        print("Installing google-auth-oauthlib...")
        os.system(f"{sys.executable} -m pip install google-auth-oauthlib")
        from google_auth_oauthlib.flow import Flow

    print("=" * 60)
    print("Google Drive OAuth2 Setup")
    print("=" * 60)
    print()

    # Use base Flow with explicit redirect_uri (works in headless/codespace)
    REDIRECT_URI = "http://localhost"
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRET_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    auth_url, state = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        include_granted_scopes="true",
    )
    print("Please visit this URL in your browser to authorize:")
    print()
    print(f"  {auth_url}")
    print()
    print("After authorizing, you'll be redirected to a localhost URL.")
    print("The page won't load (that's OK). Copy the FULL URL from your browser's address bar.")
    print("It looks like: http://localhost/?code=4/0A...&scope=...")
    print()
    redirect_url = input("Paste the full redirect URL here: ").strip()

    # Extract code from URL
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(redirect_url)
    params = parse_qs(parsed.query)
    if "code" in params:
        code = params["code"][0]
    else:
        # Maybe user pasted just the code
        code = redirect_url

    flow.fetch_token(code=code)
    creds = flow.credentials

    # Save token
    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else SCOPES,
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)

    print()
    print(f"✓ Token saved to: {TOKEN_FILE}")
    print()
    print("You can now run the recorder:")
    print(f'  GOOGLE_DRIVE_FOLDER_ID="YOUR_FOLDER_ID" python3 tapo_drive_recorder.py')
    print()


if __name__ == "__main__":
    main()
