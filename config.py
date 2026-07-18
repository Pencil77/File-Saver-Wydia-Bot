"""
config.py — Finalized configuration matching main.py imports.
"""

import os
import argparse
from dotenv import load_dotenv
from googleapiclient.discovery import build
from google.oauth2 import service_account

# Load .env
load_dotenv()

# --- Required imports by main.py ---
TELEGRAM_API_ID = os.getenv("API_ID")
TELEGRAM_API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", 0))
SEEDR_TOKEN = os.getenv("SEEDR_TOKEN")

DRIVES = {
    "1": {"name": "Depot7_Open_Paper", "folder_id": os.getenv("FOLDER_ID_1")},
    "2": {"name": "Depot6_Index_Paper", "folder_id": os.getenv("FOLDER_ID_2")},
    "3": {"name": "Depot26_Deep_Sea", "folder_id": os.getenv("FOLDER_ID_3")},
    "4": {"name": "Depot2_Common_Drive", "folder_id": os.getenv("FOLDER_ID_4")},
}
FIXED_DRIVE = os.getenv("FIXED_DRIVE")

# Argument Parsing
parser = argparse.ArgumentParser()
parser.add_argument("--drive", default=None)
ARGS = parser.parse_args()

# Drive Initialization
SERVICE_ACCOUNT_FILE = 'credentials.json'
SCOPES = ['https://www.googleapis.com/auth/drive']

def _init_drive():
    try:
        if os.path.exists(SERVICE_ACCOUNT_FILE):
            creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
            return build('drive', 'v3', credentials=creds)
    except Exception:
        pass
    return None

drive_service = _init_drive()
LOCAL_DOWNLOAD_PATH = "downloads"
