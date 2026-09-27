"""Run ONCE per channel on your PC to get that channel's YouTube refresh token.
Needs client_secret.json (downloaded from Google Cloud) in this folder.

Several channels on one email? When the browser opens, pick the Google account, then pick the
CHANNEL (brand account) you want this bot to upload to.
"""
from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json", ["https://www.googleapis.com/auth/youtube.upload"])
creds = flow.run_local_server(port=0, prompt="select_account consent", access_type="offline")
print("\nYT_CLIENT_ID     =", creds.client_id)
print("YT_CLIENT_SECRET =", creds.client_secret)
print("YT_REFRESH_TOKEN =", creds.refresh_token)
print("\nCopy these into this bot's GitHub secrets. Do NOT share them with anyone.")
