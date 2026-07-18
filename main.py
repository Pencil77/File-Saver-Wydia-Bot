"""
File Saver Wydia Bot — main.py
-------------------------------
Telegram bot that saves files to Google Drive.
Two intake paths:
  • Direct file/video  → download from Telegram → upload to Drive
  • Magnet link        → queue on Seedr → download from Seedr → upload to Drive

Run:
    python main.py
    python main.py --drive 2
    python main.py --auto-stop-minutes 30 --auto-stop-mode hard
    python main.py --no-notify-startup
"""

import asyncio
import os
import secrets
import time

from pyrogram import Client, filters
from pyrogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from tqdm import tqdm

# Local modules
from config import (
    ARGS, DRIVES, FIXED_DRIVE, LOCAL_DOWNLOAD_PATH,
    OWNER_CHAT_ID, SEEDR_TOKEN,
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN,
    is_admin, log,
)
from drive import upload_to_drive
from seedr import (
    add_magnet, delete_folder, delete_torrent,
    download_file, list_files, poll_torrent, clear_seedr,
)
from transfers import (
    active_transfers,
    format_bytes, format_eta,
    new_transfer, remove_transfer,
    transfer_summary, update_transfer,
)

# ---------------------------------------------------------------------------
# Pyrogram client
# ---------------------------------------------------------------------------
app = Client(
    "file2link_session",
    api_id=TELEGRAM_API_ID,
    api_hash=TELEGRAM_API_HASH,
    bot_token=TELEGRAM_BOT_TOKEN,
    ipv6=False,
    max_concurrent_transmissions=6,
)

# Pending drive-choice tokens
pending_uploads: dict = {}   # token -> pyrogram Message
pending_magnets: dict = {}   # token -> magnet string

# ---------------------------------------------------------------------------
# Shutdown state
# ---------------------------------------------------------------------------
shutdown_event = asyncio.Event()
shutting_down = False

magnet_queue = asyncio.Queue()


async def initiate_shutdown(mode: str, notify_chat_id=None):
    global shutting_down
    shutting_down = True
    log.info(f"Shutdown requested (mode={mode}). Active: {len(active_transfers)}")

    if notify_chat_id:
        try:
            if mode == "hard" and active_transfers:
                await app.send_message(
                    notify_chat_id,
                    f"🔴 **Hard stop:** terminating now, {len(active_transfers)} transfer(s) interrupted.",
                )
            elif mode == "soft" and active_transfers:
                await app.send_message(
                    notify_chat_id,
                    f"🟢 **Soft stop:** waiting for {len(active_transfers)} transfer(s) to finish...",
                )
            else:
                await app.send_message(notify_chat_id, "🛑 Stopping bot now.")
        except Exception:
            pass

    if mode == "hard":
        log.warning("Hard stop: terminating immediately.")
        async def _force():
            await asyncio.sleep(1)
            os._exit(0)
        asyncio.create_task(_force())
        shutdown_event.set()
        return

    while active_transfers:
        log.info(f"Soft stop: waiting on {len(active_transfers)} transfer(s)...")
        await asyncio.sleep(5)

    if notify_chat_id:
        try:
            await app.send_message(notify_chat_id, "✅ All transfers done. Stopping now.")
        except Exception:
            pass
    shutdown_event.set()


async def auto_stop_watchdog():
    if ARGS.auto_stop_minutes is None:
        return
    await asyncio.sleep(ARGS.auto_stop_minutes * 60)
    log.info(f"Auto-stop timer reached ({ARGS.auto_stop_minutes} min, {ARGS.auto_stop_mode})")
    await initiate_shutdown(ARGS.auto_stop_mode, notify_chat_id=OWNER_CHAT_ID)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def safe_edit(msg, text: str):
    try:
        await msg.edit_text(text)
    except Exception:
        pass


class _ReplyProxy:
    """Lets upload_local_to_drive() send its final message to a chat_id
    directly, for callers (like the magnet flow) that don't have a
    pyrogram Message to reply to."""
    def __init__(self, chat_id, bot):
        self._cid = chat_id
        self._bot = bot

    async def reply_text(self, text):
        await self._bot.send_message(self._cid, text)


def drive_choice_buttons(token: str) -> InlineKeyboardMarkup:
    rows = []
    for i, d in enumerate(DRIVES):
        if i % 2 == 0:
            rows.append([])
        rows[-1].append(InlineKeyboardButton(d["name"], callback_data=f"drv:{token}:{i}"))
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Core upload path: local file → Drive
# ---------------------------------------------------------------------------

async def upload_local_to_drive(
    local_path: str,
    file_name: str,
    file_size: int,
    drive: dict,
    chat_id,
    status_msg,
    reply_target,
):
    ul_tid = new_transfer(file_name, "uploading", file_size, chat_id)
    loop = asyncio.get_event_loop()
    last_edit = time.monotonic()
    last_log = time.monotonic()

    def upload_progress(current, total):
        nonlocal last_edit, last_log
        total = total or file_size
        update_transfer(ul_tid, current)
        now = time.monotonic()
        if now - last_log > 5:
            last_log = now
            log.info(transfer_summary(active_transfers[ul_tid]).replace("\n   ", " "))
        if now - last_edit > 4:
            last_edit = now
            t = active_transfers[ul_tid]
            pct = int(current / total * 100) if total else 0
            eta = format_eta((total - current) / t["speed"]) if t["speed"] > 0 else "—"
            asyncio.run_coroutine_threadsafe(
                safe_edit(
                    status_msg,
                    f"⚡ **Processing:** `{file_name}`\n"
                    f"☁️ Uploading to '{drive['name']}': {pct}% "
                    f"@ {format_bytes(t['speed'])}/s, ETA {eta}"
                ),
                loop,
            )

    try:
        link = await loop.run_in_executor(
            None,
            lambda: upload_to_drive(local_path, file_name, drive["folder_id"], upload_progress),
        )

        await safe_edit(status_msg, f"🏁 **Task Finished:** `{file_name}`")
        await reply_target.reply_text(
            f"✅ **Saved to Drive!**\n\n"
            f"📂 **Drive:** `{drive['name']}`\n"
            f"💾 **File:** `{file_name}`\n"
            f"🔗 **Link:** {link}"
        )
    finally:
        remove_transfer(ul_tid)
        if os.path.exists(local_path):
            os.remove(local_path)


# ---------------------------------------------------------------------------
# Path A: Telegram file → local → Drive
# ---------------------------------------------------------------------------

async def process_tg_upload(message, drive: dict, status_msg=None):
    media = message.video or message.document
    file_name = getattr(media, "file_name", None) or "file.mp4"
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
        await upload_local_to_drive(
            local_path, file_name, file_size,
            drive, chat_id, status_msg, message,
        )

    except Exception as e:
        pbar.close()
        remove_transfer(dl_tid)
        log.exception(f"TG upload failed: {file_name}")
        if os.path.exists(local_path):
            os.remove(local_path)
        try:
            await message.reply_text(f"❌ **Failed:** {e}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Path B: Magnet → Seedr → local → Drive
# ---------------------------------------------------------------------------

async def process_magnet(magnet: str, drive: dict, chat_id, status_msg):
    if not SEEDR_TOKEN:
        await safe_edit(
            status_msg,
            "❌ SEEDR_TOKEN not set in .env — magnet links not available."
        )
        return

    loop = asyncio.get_event_loop()
    
    # State tracking variables for the finally block
    torrent_id = None
    folder_id = None

    try:
        # 1. Submit magnet to Seedr
        await safe_edit(status_msg, "🌱 **Submitting magnet to Seedr...**")
        torrent_id = await loop.run_in_executor(
            None, lambda: add_magnet(magnet, SEEDR_TOKEN)
        )

        # 2. Poll until Seedr finishes downloading the torrent
        await safe_edit(status_msg, "🌱 **Seedr is downloading the torrent...**\n0%")
        last_edit = time.monotonic()

        def poll_progress(name, pct, size):
            nonlocal last_edit
            now = time.monotonic()
            if now - last_edit > 4:
                last_edit = now
                asyncio.run_coroutine_threadsafe(
                    safe_edit(status_msg,
                        f"🌱 **Seedr downloading:** `{name[:50]}`\n{pct}%"
                    ),
                    loop,
                )

        torrent_status = await loop.run_in_executor(
            None, lambda: poll_torrent(torrent_id, SEEDR_TOKEN, poll_progress)
        )

        folder_id = torrent_status.get("folder_id")
        if not folder_id:
            raise RuntimeError(f"Seedr torrent finished but no folder_id in response: {torrent_status}")

        # 3. List files in the completed folder
        await safe_edit(status_msg, "🌱 **Seedr done! Listing files...**")
        files = await loop.run_in_executor(
            None, lambda: list_files(folder_id, SEEDR_TOKEN)
        )

        if not files:
            raise RuntimeError("Seedr folder is empty after torrent completed.")

        log.info(f"Seedr folder {folder_id} has {len(files)} file(s): {[f['name'] for f in files]}")

        # 4. Download each file from Seedr → local → Drive
        for idx, file_info in enumerate(files):
            file_name = file_info["name"]
            file_size = file_info["size"] or 1
            local_path = os.path.join(LOCAL_DOWNLOAD_PATH, file_name)

            if os.path.exists(local_path):
                os.remove(local_path)

            dl_tid = new_transfer(file_name, "seedr_to_shell", file_size, chat_id)
            pbar = tqdm(total=file_size, desc=f"⏬ Seedr {file_name}", unit="B", unit_scale=True)
            last_dl_edit = time.monotonic()
            last_dl_log = time.monotonic()

            def dl_progress(current, total):
                nonlocal last_dl_edit, last_dl_log
                pbar.update(current - pbar.n)
                update_transfer(dl_tid, current)
                now = time.monotonic()
                if now - last_dl_log > 5:
                    last_dl_log = now
                    log.info(transfer_summary(active_transfers[dl_tid]).replace("\n   ", " "))
                if now - last_dl_edit > 4:
                    last_dl_edit = now
                    t = active_transfers[dl_tid]
                    pct = int(current / total * 100) if total else 0
                    eta = format_eta((total - current) / t["speed"]) if t["speed"] > 0 else "—"
                    asyncio.run_coroutine_threadsafe(
                        safe_edit(status_msg,
                            f"⏬ **Seedr → App** ({idx+1}/{len(files)}): `{file_name}`\n"
                            f"{pct}% @ {format_bytes(t['speed'])}/s, ETA {eta}"
                        ),
                        loop,
                    )

            try:
                await loop.run_in_executor(
                    None,
                    lambda fi=file_info: download_file(
                        fi["id"], fi["name"], SEEDR_TOKEN, LOCAL_DOWNLOAD_PATH, dl_progress
                    ),
                )
            except Exception as e:
                log.error(f"DOWNLOAD FAILED: {file_name}: {e}")
                continue
            finally:
                pbar.close()
                remove_transfer(dl_tid)

            await safe_edit(
                status_msg,
                f"⚡ **Processing:** `{file_name}`\n☁️ Uploading to '{drive['name']}'..."
            )
            
            # The Drive upload can fail. Since it's inside the try block, it will jump 
            # to except, and then finally, ensuring Seedr is cleaned up.
            await upload_local_to_drive(
                local_path, file_name, file_size,
                drive, chat_id, status_msg,
                _ReplyProxy(chat_id, app),
            )

    except Exception as e:
        log.exception("Magnet upload failed")
        try:
            await app.send_message(chat_id, f"❌ **Magnet failed:** {e}")
        except Exception:
            pass
            
    finally:
        # 5. Guaranteed Cleanup of Seedr to free quota
        if folder_id:
            try:
                await loop.run_in_executor(
                    None,
                    lambda: delete_folder(folder_id, SEEDR_TOKEN)
                )
                log.info(f"Seedr cleanup done for folder {folder_id}")
            except Exception as e:
                log.error(f"Failed to delete folder {folder_id} from Seedr: {e}")
        elif torrent_id:
            # If it timed out or failed before becoming a folder, delete the torrent
            try:
                await loop.run_in_executor(None, lambda: delete_torrent(torrent_id, SEEDR_TOKEN))
                log.info(f"Seedr cleanup done for torrent {torrent_id}")
            except Exception as e:
                log.error(f"Failed to delete torrent {torrent_id} from Seedr: {e}")


async def magnet_worker():
    while True:
        magnet, drive, chat_id, status_msg = await magnet_queue.get()
        try:
            await process_magnet(magnet, drive, chat_id, status_msg)
        finally:
            magnet_queue.task_done()


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

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
    if active_transfers:
        text = (
            f"⚠️ {len(active_transfers)} transfer(s) in progress.\n\n"
            f"🟢 **Soft stop** — wait for them to finish\n"
            f"🔴 **Hard stop** — stop immediately"
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
    "/status — live progress (%, speed, ETA)\n"
    "/stop — stop the bot (admin only)\n"
    "/magnet <link> — save a torrent via Seedr → Drive\n"
    "/id — get your chat ID\n"
    "/help — this list"
)

# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

@app.on_message(filters.command("start"))
async def cmd_start(client, message):
    await message.reply_text(
        "Hi I am File Saver Bot! 👋\n\n"
        "Send me a video or document and I'll upload it to Drive.\n"
        "Or send `/magnet <link>` to save a torrent via Seedr.\n\n"
        "**Commands:**\n"
        "/status — see progress of active transfers\n"
        "/stop — stop the bot (admin only)",
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


@app.on_message(filters.command("magnet"))
async def cmd_magnet(client, message):
    if shutting_down:
        await message.reply_text("🛑 Bot is shutting down.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("magnet:"):
        await message.reply_text(
            "⚠️ Usage: `/magnet magnet:?xt=urn:btih:...`\n"
            "Paste the full magnet link after the command."
        )
        return

    magnet = parts[1].strip()

    if FIXED_DRIVE is not None:
        status_msg = await message.reply_text("📥 Added to queue...")
        await magnet_queue.put((magnet, FIXED_DRIVE, message.chat.id, status_msg))
        return

    # Ask which drive
    token = secrets.token_hex(4)
    pending_magnets[token] = magnet
    await message.reply_text(
        f"📂 **Choose destination for magnet:**\n`{magnet[:60]}...`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(d["name"], callback_data=f"mdrv:{token}:{i}")]
            for i, d in enumerate(DRIVES)
        ]),
    )


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------

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
        await message.reply_text("🛑 Bot is shutting down.")
        return

    if FIXED_DRIVE is not None:
        await process_tg_upload(message, FIXED_DRIVE)
        return

    token = secrets.token_hex(4)
    pending_uploads[token] = message
    media = message.video or message.document
    file_name = getattr(media, "file_name", None) or "video.mp4"
    await message.reply_text(
        f"📂 **Choose destination for:**\n`{file_name}`",
        reply_markup=drive_choice_buttons(token),
    )


@app.on_callback_query(filters.regex(r"^drv:"))
async def handle_drive_choice(client, callback_query):
    _, token, idx_str = callback_query.data.split(":")
    original_message = pending_uploads.pop(token, None)
    if original_message is None:
        await callback_query.answer("Request expired — please resend the file.", show_alert=True)
        return
    if shutting_down:
        await callback_query.answer("Bot is shutting down.", show_alert=True)
        return

    drive = DRIVES[int(idx_str)]
    await callback_query.answer(f"Uploading to {drive['name']}...")
    status_msg = callback_query.message
    await status_msg.edit_reply_markup(reply_markup=None)
    await process_tg_upload(original_message, drive, status_msg=status_msg)


@app.on_callback_query(filters.regex(r"^mdrv:"))
async def handle_magnet_drive_choice(client, callback_query):
    _, token, idx_str = callback_query.data.split(":")
    magnet = pending_magnets.pop(token, None)
    if magnet is None:
        await callback_query.answer("Request expired — please resend the magnet.", show_alert=True)
        return
    if shutting_down:
        await callback_query.answer("Bot is shutting down.", show_alert=True)
        return

    drive = DRIVES[int(idx_str)]
    await callback_query.answer(f"Processing via Seedr → {drive['name']}...")
    status_msg = callback_query.message
    await status_msg.edit_reply_markup(reply_markup=None)
    await magnet_queue.put((magnet, drive, callback_query.message.chat.id, status_msg))
    await status_msg.edit_text("📥 Added to queue...")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    log.info("Bot is starting...")
    async with app:
        me = await app.get_me()
        log.info(f"Authenticated as: @{me.username} (id={me.id})")

        await app.set_bot_commands([
            BotCommand("start",  "Welcome message and quick-action buttons"),
            BotCommand("status", "See progress of active transfers"),
            BotCommand("magnet", "Save a torrent via Seedr → Drive"),
            BotCommand("stop",   "Stop the bot (admin only)"),
            BotCommand("id",     "Get your chat ID"),
            BotCommand("help",   "List available commands"),
        ])

        if SEEDR_TOKEN:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: clear_seedr(SEEDR_TOKEN)
                )
                log.info("Seedr cleaned on startup.")
            except Exception as e:
                log.warning(f"Startup Seedr cleanup failed: {e}")

        asyncio.create_task(magnet_worker())
        log.info("Magnet worker started.")
        log.info("Bot is listening...")

        if ARGS.notify_startup and OWNER_CHAT_ID:
            try:
                drive_note = FIXED_DRIVE["name"] if FIXED_DRIVE else "prompted per file"
                seedr_note = "✅ configured" if SEEDR_TOKEN else "❌ not configured (magnet links disabled)"
                await app.send_message(
                    OWNER_CHAT_ID,
                    f"✅ **Hi, I'm up!**\n\n"
                    f"📂 Drive: `{drive_note}`\n"
                    f"🌱 Seedr: {seedr_note}\n"
                    f"⏱️ Auto-stop: "
                    f"{f'{ARGS.auto_stop_minutes} min ({ARGS.auto_stop_mode})' if ARGS.auto_stop_minutes else 'disabled'}\n\n"
                    f"Send a file or `/magnet <link>` to get started!",
                )
            except Exception as e:
                log.warning(f"Startup notification failed: {e}")
        elif ARGS.notify_startup and not OWNER_CHAT_ID:
            log.warning("notify-startup is on but TELEGRAM_OWNER_CHAT_ID is not set.")

        watchdog = asyncio.create_task(auto_stop_watchdog())
        await shutdown_event.wait()
        watchdog.cancel()
        log.info("Shutting down...")

    log.info("Bot stopped.")


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        log.info("Interrupted by user (Ctrl+C).")

