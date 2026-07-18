"""
config.py — configuration for File-Saver-Wydia-Bot.
Provides the names expected by main.py while keeping backward-compatible aliases.
"""

import argparse
import logging
import os

from dotenv import load_dotenv
from googleapiclient.discovery import build
from google.oauth2 import service_account

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_API_ID = os.getenv("API_ID") or os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("API_HASH") or os.getenv("TELEGRAM_API_HASH")
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID") or os.getenv("TELEGRAM_OWNER_CHAT_ID") or 0)
SEEDR_TOKEN = os.getenv("SEEDR_TOKEN")

# Backward-compatible aliases for older code/commits.
BOT_TOKEN = TELEGRAM_BOT_TOKEN

# main.py expects a list of drive dicts, not a dict keyed by numbers.
DRIVES = [
    {"name": "Depot7_Open_Paper", "folder_id": os.getenv("FOLDER_ID_1")},
    {"name": "Depot6_Index_Paper", "folder_id": os.getenv("FOLDER_ID_2")},
    {"name": "Depot26_Deep_Sea", "folder_id": os.getenv("FOLDER_ID_3")},
    {"name": "Depot2_Common_Drive", "folder_id": os.getenv("FOLDER_ID_4")},
]
FIXED_DRIVE = os.getenv("FIXED_DRIVE")

parser = argparse.ArgumentParser(description="File-Saver-Wydia-Bot")
parser.add_argument("--drive", default=None)
parser.add_argument("--auto-stop-minutes", type=float, default=None)
parser.add_argument("--auto-stop-mode", choices=["hard", "soft"], default="soft")
parser.add_argument("--notify-startup", dest="notify_startup", action="store_true", default=True)
parser.add_argument("--no-notify-startup", dest="notify_startup", action="store_false")
ARGS = parser.parse_args()

SERVICE_ACCOUNT_FILE = "credentials.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def _init_drive():
    try:
        if os.path.exists(SERVICE_ACCOUNT_FILE):
            creds = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_FILE, scopes=SCOPES
            )
            return build("drive", "v3", credentials=creds)
    except Exception as e:
        log.warning(f"Drive initialization failed: {e}")
    return None


drive_service = _init_drive()
LOCAL_DOWNLOAD_PATH = os.getenv("LOCAL_DOWNLOAD_PATH", "downloads")
os.makedirs(LOCAL_DOWNLOAD_PATH, exist_ok=True)


def is_admin(chat_id) -> bool:
    return bool(OWNER_CHAT_ID) and str(chat_id) == str(OWNER_CHAT_ID)
