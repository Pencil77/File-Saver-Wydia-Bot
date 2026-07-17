"""
File2Link Telegram Bot
----------------------
Listens for videos/documents sent to a Telegram bot, downloads them locally,
then uploads them to a Google Drive Shared Drive folder using a service
account (no OAuth browser flow, no rclone).

Features:
- Multiple destination Shared Drives (pick via --drive N or Telegram buttons)
- Live progress tracking (console logs + on-demand /status in Telegram)
- "I'm up" startup notification (on by default; disable with --no-notify-startup)
- Auto-stop after N minutes, hard or soft (--auto-stop-minutes / --auto-stop-mode)
- /stop command with confirmation + hard/soft choice, for manual shutdown

Run:
    python3 main.py
    python3 main.py --drive 2
    python3 main.py --notify-startup
    python3 main.py --auto-stop-minutes 120 --auto-stop-mode soft
"""

import argparse
import asyncio
import logging
import os
import secrets
import signal
import sys
import time
import uuid

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from tqdm import tqdm

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
_owner_chat_id_raw = os.environ.get("TELEGRAM_OWNER_CHAT_ID")  # for startup notification + admin-only commands
OWNER_CHAT_ID = int(_owner_chat_id_raw) if _owner_chat_id_raw else None


def is_admin(chat_id) -> bool:
    if not OWNER_CHAT_ID:
        return False  # no admin configured -> nobody can use admin-only commands
    return str(chat_id) == str(OWNER_CHAT_ID)

SERVICE_ACCOUNT_FILE = os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]

_raw_drives = os.environ["GOOGLE_DRIVES"]
DRIVES = []
for entry in _raw_drives.split(","):
    name, folder_id = entry.strip().split(":", 1)
    DRIVES.append({"name": name.strip(), "folder_id": folder_id.strip()})
if not DRIVES:
    raise SystemExit("No drives configured in GOOGLE_DRIVES (.env)")

LOCAL_DOWNLOAD_PATH = os.environ.get("LOCAL_DOWNLOAD_PATH", "/tmp/file2link_downloads/")
os.makedirs(LOCAL_DOWNLOAD_PATH, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("file2link")

# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="File2Link Telegram Bot")
parser.add_argument(
    "--drive", type=int, default=None,
    help=f"Drive number to always upload to (1-{len(DRIVES)}). Omit to choose per-file via buttons.",
)
parser.add_argument(
    "--notify-startup", dest="notify_startup", action="store_true", default=True,
    help="Send an 'I'm up' message to TELEGRAM_OWNER_CHAT_ID on startup. On by default.",
)
parser.add_argument(
    "--no-notify-startup", dest="notify_startup", action="store_false",
    help="Disable the startup notification.",
)
parser.add_argument(
    "--auto-stop-minutes", type=float, default=None,
    help="Automatically stop the bot after this many minutes. Disabled by default.",
)
parser.add_argument(
    "--auto-stop-mode", choices=["hard", "soft"], default="soft",
    help="How to auto-stop: 'hard' stops immediately, 'soft' waits for active transfers to finish. Default: soft.",
)
args = parser.parse_args()

FIXED_DRIVE = None
if len(DRIVES) == 1:
    FIXED_DRIVE = DRIVES[0]
    log.info(f"Only one drive configured — using '{FIXED_DRIVE['name']}' for all uploads.")
elif args.drive is not None:
    if not (1 <= args.drive <= len(DRIVES)):
        raise SystemExit(f"--drive must be between 1 and {len(DRIVES)}")
    FIXED_DRIVE = DRIVES[args.drive - 1]
    log.info(f"Using fixed drive: '{FIXED_DRIVE['name']}' (all uploads, no prompting)")
else:
    log.info("No --drive given. Will prompt per file: " + ", ".join(
        f"{i+1}={d['name']}" for i, d in enumerate(DRIVES)
    ))

# token -> pyrogram Message, for pending drive-choice buttons
pending_uploads = {}

# ---------------------------------------------------------------------------
# Transfer tracking (shared by console logs, /status, and progress edits)
# ---------------------------------------------------------------------------
active_transfers = {}  # transfer_id -> dict


def format_bytes(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def format_eta(seconds):
    if seconds is None or seconds <= 0 or seconds == float("inf"):
        return "—"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def new_transfer(file_name, stage, total_bytes, chat_id):
    tid = uuid.uuid4().hex[:8]
    active_transfers[tid] = {
        "file_name": file_name,
        "stage": stage,             # "downloading" or "uploading"
        "current": 0,
        "total": total_bytes,
        "speed": 0.0,
        "start_time": time.monotonic(),
        "last_time": time.monotonic(),
        "last_bytes": 0,
        "chat_id": chat_id,
    }
    return tid


def update_transfer(tid, current):
    t = active_transfers.get(tid)
    if not t:
        return
    now = time.monotonic()
    dt = now - t["last_time"]
    if dt > 0:
        t["speed"] = (current - t["last_bytes"]) / dt
    t["current"] = current
    t["last_time"] = now
    t["last_bytes"] = current


def transfer_summary(t):
    pct = int(t["current"] / t["total"] * 100) if t["total"] else 0
    speed = t["speed"]
    remaining = t["total"] - t["current"]
    eta = remaining / speed if speed > 0 else None
    stage_label = "⏬ Telegram → App" if t["stage"] == "downloading" else "☁️ App → Drive"
    return (
        f"{stage_label}: `{t['file_name']}`\n"
        f"   {pct}% ({format_bytes(t['current'])}/{format_bytes(t['total'])}) "
        f"@ {format_bytes(speed)}/s, ETA {format_eta(eta)}"
    )


def remove_transfer(tid):
    active_transfers.pop(tid, None)

# ---------------------------------------------------------------------------
# Shutdown control
# ---------------------------------------------------------------------------
shutdown_event = asyncio.Event()
shutting_down = False  # once True, stop accepting new files


async def initiate_shutdown(mode: str, notify_chat_id=None):
    global shutting_down
    shutting_down = True
    log.info(f"Shutdown requested (mode={mode}). Active transfers: {len(active_transfers)}")

    if notify_chat_id:
        try:
            if mode == "hard" and active_transfers:
                await app.send_message(
                    notify_chat_id,
                    f"🔴 **Hard stop:** terminating now, {len(active_transfers)} transfer(s) will be interrupted.",
                )
            elif mode == "soft" and active_transfers:
                await app.send_message(
                    notify_chat_id,
                    f"🟢 **Soft stop:** waiting for {len(active_transfers)} active transfer(s) to finish...",
                )
            else:
                await app.send_message(notify_chat_id, "🛑 Stopping bot now.")
        except Exception:
            pass

    if mode == "hard":
        log.warning("Hard stop: terminating immediately.")

        async def _force_exit():
            await asyncio.sleep(1)  # brief grace period to flush the notify message
            os._exit(0)

        asyncio.create_task(_force_exit())
        shutdown_event.set()
        return

    # soft stop: wait until no active transfers remain
    while active_transfers:
        log.info(f"Soft stop waiting on {len(active_transfers)} active transfer(s)...")
        await asyncio.sleep(5)

    log.info("Soft stop: no active transfers remain, shutting down.")
    if notify_chat_id:
        try:
            await app.send_message(notify_chat_id, "✅ All transfers finished. Bot is stopping now.")
        except Exception:
            pass
    shutdown_event.set()


async def auto_stop_watchdog():
    if args.auto_stop_minutes is None:
        return
    await asyncio.sleep(args.auto_stop_minutes * 60)
    log.info(f"Auto-stop timer reached ({args.auto_stop_minutes} min, mode={args.auto_stop_mode}).")
    await initiate_shutdown(args.auto_stop_mode, notify_chat_id=OWNER_CHAT_ID)

# ---------------------------------------------------------------------------
# Google Drive client (service account, no browser auth needed)
# ---------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/drive"]

def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

drive_service = get_drive_service()


def upload_to_drive(local_path: str, file_name: str, folder_id: str, progress_cb=None) -> str:
    """Uploads a file to the given Drive folder. Returns the webViewLink."""
    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(local_path, resumable=True, chunksize=50 * 1024 * 1024)

    request = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status and progress_cb:
            progress_cb(status.resumable_progress, status.total_size)

    return response.get("webViewLink")

# ---------------------------------------------------------------------------
# Telegram bot
# ---------------------------------------------------------------------------
app = Client(
    "file2link_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    ipv6=False,
    max_concurrent_transmissions=6,  # parallel download/upload chunks (default: 1)
)


async def safe_edit(msg, text):
    try:
        await msg.edit_text(text)
    except Exception:
        pass


async def process_upload(message, drive: dict, status_msg=None):
    media = message.video or message.document
    file_name = getattr(media, "file_name", None) or "video.mp4"
    file_size = getattr(media, "file_size", 0) or 1
    local_path = os.path.join(LOCAL_DOWNLOAD_PATH, file_name)
    chat_id = message.chat.id

    if os.path.exists(local_path):
        os.remove(local_path)

    if status_msg is None:
        status_msg = await message.reply_text(f"⚡ **Started:** `{file_name}`\n⏬ Downloading...")
    else:
        await status_msg.edit_text(f"⚡ **Started:** `{file_name}`\n⏬ Downloading...")

    dl_tid = new_transfer(file_name, "downloading", file_size, chat_id)
    pbar = tqdm(total=file_size, desc=f"⏬ {file_name}", unit="B", unit_scale=True)
    last_edit = time.monotonic()
    last_log = time.monotonic()

    async def dl_progress(current, total):
        nonlocal last_edit, last_log
        pbar.update(current - pbar.n)
        update_transfer(dl_tid, current)
        now = time.monotonic()
        if now - last_log > 5:
            last_log = now
            log.info(transfer_summary(active_transfers[dl_tid]).replace("\n   ", " "))
        if now - last_edit > 4:
            last_edit = now
            t = active_transfers[dl_tid]
            pct = int(current / total * 100) if total else 0
            eta = format_eta((total - current) / t["speed"]) if t["speed"] > 0 else "—"
            try:
                await status_msg.edit_text(
                    f"⚡ **Downloading:** `{file_name}`\n"
                    f"⏬ {pct}% @ {format_bytes(t['speed'])}/s, ETA {eta}"
                )
            except Exception:
                pass

    try:
        await message.download(file_name=local_path, progress=dl_progress)
        pbar.close()
        remove_transfer(dl_tid)

        await status_msg.edit_text(
            f"⚡ **Processing:** `{file_name}`\n☁️ Uploading to '{drive['name']}'..."
        )

        ul_tid = new_transfer(file_name, "uploading", file_size, chat_id)
        loop = asyncio.get_event_loop()
        last_upload_edit = time.monotonic()
        last_upload_log = time.monotonic()

        def upload_progress(current, total):
            nonlocal last_upload_edit, last_upload_log
            total = total or file_size
            update_transfer(ul_tid, current)
            now = time.monotonic()
            if now - last_upload_log > 5:
                last_upload_log = now
                log.info(transfer_summary(active_transfers[ul_tid]).replace("\n   ", " "))
            if now - last_upload_edit > 4:
                last_upload_edit = now
                t = active_transfers[ul_tid]
                pct = int(current / total * 100) if total else 0
                eta = format_eta((total - current) / t["speed"]) if t["speed"] > 0 else "—"
                asyncio.run_coroutine_threadsafe(
                    safe_edit(
                        status_msg,
                        f"⚡ **Processing:** `{file_name}`\n"
                        f"☁️ Uploading to '{drive['name']}': {pct}% @ {format_bytes(t['speed'])}/s, ETA {eta}"
                    ),
                    loop,
                )

        link = await loop.run_in_executor(
            None,
            lambda: upload_to_drive(local_path, file_name, drive["folder_id"], upload_progress),
        )
        remove_transfer(ul_tid)
        os.remove(local_path)

        await status_msg.edit_text(f"🏁 **Task Finished:** `{file_name}`")
        await message.reply_text(
            f"✅ **Saved to Drive!**\n\n"
            f"📂 **Drive:** `{drive['name']}`\n"
            f"💾 **File:** `{file_name}`\n"
            f"🔗 **Link:** {link}"
        )
        log.info(f"Uploaded: {file_name} -> {drive['name']} -> {link}")

    except Exception as e:
        pbar.close()
        remove_transfer(dl_tid)
        remove_transfer(locals().get("ul_tid", ""))
        log.exception(f"Failed on {file_name}")
        if os.path.exists(local_path):
            os.remove(local_path)
        try:
            await message.reply_text(f"❌ **Failed:** {e}")
        except Exception:
            pass


def status_text() -> str:
    if not active_transfers:
        return "💤 No active transfers right now."
    lines = [transfer_summary(t) for t in active_transfers.values()]
    return "📊 **Active transfers:**\n\n" + "\n\n".join(lines)


def main_menu_buttons(chat_id) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("📊 Status", callback_data="quick:status")]]
    if is_admin(chat_id):
        rows.append([InlineKeyboardButton("🛑 Stop", callback_data="quick:stop")])
    rows.append([InlineKeyboardButton("❓ Help", callback_data="quick:help")])
    return InlineKeyboardMarkup(rows)


def stop_menu(chat_id):
    """Returns (text, InlineKeyboardMarkup) for the stop confirmation prompt."""
    if active_transfers:
        text = (
            f"⚠️ {len(active_transfers)} transfer(s) currently in progress.\n"
            f"How would you like to stop?\n\n"
            f"🟢 **Soft stop** — wait for them to finish, then stop\n"
            f"🔴 **Hard stop** — stop immediately, interrupting them"
        )
        buttons = [
            [
                InlineKeyboardButton("🟢 Soft Stop", callback_data="stop:soft"),
                InlineKeyboardButton("🔴 Hard Stop", callback_data="stop:hard"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="stop:cancel")],
        ]
    else:
        text = "No active transfers. Stop the bot now?"
        buttons = [
            [InlineKeyboardButton("🛑 Stop", callback_data="stop:hard")],
            [InlineKeyboardButton("❌ Cancel", callback_data="stop:cancel")],
        ]
    return text, InlineKeyboardMarkup(buttons)


HELP_TEXT = (
    "**Available commands:**\n"
    "/status — see live progress (%, speed, ETA) of any transfer in progress\n"
    "/stop — stop the bot (admin only; asks Soft/Hard if a transfer is active)"
)


@app.on_message(filters.command("start"))
async def cmd_start(client, message):
    await message.reply_text(
        f"Hi I am File Saver Bot! 👋\n\n"
        f"Send me a video or document and I'll upload it to Drive.\n\n"
        f"**Commands:**\n"
        f"/status — see progress of active transfers\n"
        f"/stop — stop the bot (admin only)",
        reply_markup=main_menu_buttons(message.chat.id),
    )


@app.on_message(filters.command("id"))
async def cmd_id(client, message):
    await message.reply_text(f"Your chat ID is `{message.chat.id}`.")


@app.on_message(filters.command("help"))
async def cmd_help(client, message):
    await message.reply_text(HELP_TEXT, reply_markup=main_menu_buttons(message.chat.id))


@app.on_message(filters.command("status"))
async def cmd_status(client, message):
    await message.reply_text(status_text())


@app.on_message(filters.command("stop"))
async def cmd_stop(client, message):
    if not is_admin(message.chat.id):
        await message.reply_text("🚫 Only the admin can stop this bot.")
        return
    text, markup = stop_menu(message.chat.id)
    await message.reply_text(text, reply_markup=markup)


@app.on_callback_query(filters.regex(r"^quick:"))
async def handle_quick_action(client, callback_query):
    _, action = callback_query.data.split(":")
    chat_id = callback_query.message.chat.id

    if action == "status":
        await callback_query.answer()
        await callback_query.message.reply_text(status_text())
    elif action == "help":
        await callback_query.answer()
        await callback_query.message.reply_text(HELP_TEXT, reply_markup=main_menu_buttons(chat_id))
    elif action == "stop":
        if not is_admin(chat_id):
            await callback_query.answer("Only the admin can stop this bot.", show_alert=True)
            return
        await callback_query.answer()
        text, markup = stop_menu(chat_id)
        await callback_query.message.reply_text(text, reply_markup=markup)


@app.on_callback_query(filters.regex(r"^stop:"))
async def handle_stop_choice(client, callback_query):
    if not is_admin(callback_query.message.chat.id):
        await callback_query.answer("Only the admin can stop this bot.", show_alert=True)
        return

    _, mode = callback_query.data.split(":")
    if mode == "cancel":
        await callback_query.answer("Cancelled.")
        await callback_query.message.edit_text("❌ Stop cancelled.")
        return

    await callback_query.answer("Stopping...")
    await callback_query.message.edit_text(
        f"{'🔴 Hard' if mode == 'hard' else '🟢 Soft'} stop initiated..."
    )
    asyncio.create_task(initiate_shutdown(mode, notify_chat_id=callback_query.message.chat.id))


@app.on_message(filters.video | filters.document)
async def handle_media(client, message):
    if shutting_down:
        await message.reply_text("🛑 Bot is shutting down and not accepting new files right now.")
        return

    if FIXED_DRIVE is not None:
        await process_upload(message, FIXED_DRIVE)
        return

    token = secrets.token_hex(4)
    pending_uploads[token] = message
    
    # Build 2-column button grid
    buttons = []
    for i, d in enumerate(DRIVES):
        if i % 2 == 0:  # start a new row every 2 buttons
            buttons.append([])
        buttons[-1].append(InlineKeyboardButton(d["name"], callback_data=f"drv:{token}:{i}"))
    
    media = message.video or message.document
    file_name = getattr(media, "file_name", None) or "video.mp4"
    await message.reply_text(
        f"📂 **Choose destination for:**\n`{file_name}`",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@app.on_callback_query(filters.regex(r"^drv:"))
async def handle_drive_choice(client, callback_query):
    _, token, idx_str = callback_query.data.split(":")
    idx = int(idx_str)
    original_message = pending_uploads.pop(token, None)
    if original_message is None:
        await callback_query.answer("This request expired — please resend the file.", show_alert=True)
        return

    if shutting_down:
        await callback_query.answer("Bot is shutting down, try again after it restarts.", show_alert=True)
        return

    drive = DRIVES[idx]
    await callback_query.answer(f"Uploading to {drive['name']}...")
    status_msg = callback_query.message
    await status_msg.edit_reply_markup(reply_markup=None)
    await process_upload(original_message, drive, status_msg=status_msg)


async def main():
    log.info("Bot is starting...")
    async with app:
        me = await app.get_me()
        log.info(f"Authenticated as: @{me.username} (id={me.id})")

        await app.set_bot_commands([
            BotCommand("start", "Show welcome message and quick-action buttons"),
            BotCommand("status", "See progress of active transfers"),
            BotCommand("stop", "Stop the bot (admin only)"),
            BotCommand("id", "Get your chat ID"),
            BotCommand("help", "List available commands"),
        ])

        log.info("Bot is listening...")

        if args.notify_startup:
            if OWNER_CHAT_ID:
                try:
                    drive_note = FIXED_DRIVE["name"] if FIXED_DRIVE else "prompted per file"
                    await app.send_message(
                        OWNER_CHAT_ID,
                        f"✅ **Hi, I'm up!**\n\n"
                        f"📂 Drive: `{drive_note}`\n"
                        f"⏱️ Auto-stop: "
                        f"{f'{args.auto_stop_minutes} min ({args.auto_stop_mode})' if args.auto_stop_minutes else 'disabled'}\n\n"
                        f"**Available Commands:**\n"
                        f"/start — welcome message\n"
                        f"/status — live progress of active transfers\n"
                        f"/id — get your chat ID\n"
                        f"/stop — stop the bot (admin only)\n\n"
                        f"Send me a video or document to get started!",
                    )
                except Exception as e:
                    log.warning(f"notify-startup was set but sending the message failed: {e}")
            else:
                log.warning("--notify-startup was set but TELEGRAM_OWNER_CHAT_ID is not set in .env. Skipping.")

        watchdog_task = asyncio.create_task(auto_stop_watchdog())
        await shutdown_event.wait()
        watchdog_task.cancel()
        log.info("Shutting down bot client...")

    log.info("Bot stopped.")


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        log.info("Interrupted by user (Ctrl+C).")

