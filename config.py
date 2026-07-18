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
if not (TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_BOT_TOKEN):
    raise SystemExit("Missing TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_BOT_TOKEN in .env")
TELEGRAM_API_ID = int(TELEGRAM_API_ID)
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID") or os.getenv("TELEGRAM_OWNER_CHAT_ID") or 0)
SEEDR_TOKEN = os.getenv("SEEDR_TOKEN")

# Backward-compatible aliases for older code/commits.
BOT_TOKEN = TELEGRAM_BOT_TOKEN

# main.py expects a list of drive dicts, not a dict keyed by numbers.
DRIVES = []

_raw_drives = os.getenv("GOOGLE_DRIVES", "")
for entry in _raw_drives.split(","):
    entry = entry.strip()
    if not entry:
        continue
    name, folder_id = entry.split(":", 1)
    DRIVES.append({
        "name": name.strip(),
        "folder_id": folder_id.strip(),
    })

parser = argparse.ArgumentParser(description="File-Saver-Wydia-Bot")
parser.add_argument("--drive", default=None)
parser.add_argument("--auto-stop-minutes", type=float, default=None)
parser.add_argument("--auto-stop-mode", choices=["hard", "soft"], default="soft")
parser.add_argument("--notify-startup", dest="notify_startup", action="store_true", default=True)
parser.add_argument("--no-notify-startup", dest="notify_startup", action="store_false")
ARGS = parser.parse_args()

FIXED_DRIVE = None
if len(DRIVES) == 1:
    FIXED_DRIVE = DRIVES[0]
    log.info(f"Only one drive configured — using '{FIXED_DRIVE['name']}' for all uploads.")
elif ARGS.drive is not None:
    try:
        drive_idx = int(ARGS.drive)
    except ValueError:
        raise SystemExit(f"--drive must be a number between 1 and {len(DRIVES)}")
    if not (1 <= drive_idx <= len(DRIVES)):
        raise SystemExit(f"--drive must be between 1 and {len(DRIVES)}")
    FIXED_DRIVE = DRIVES[drive_idx - 1]
    log.info(f"Using fixed drive: '{FIXED_DRIVE['name']}' (no prompting)")
else:
    log.info("No --drive given. Will prompt per file: " + ", ".join(
        f"{i+1}={d['name']}" for i, d in enumerate(DRIVES)
    ))

SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
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

