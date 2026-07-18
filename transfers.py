"""
transfers.py — Active transfer registry shared by all upload paths.
Tracks progress, speed, ETA so /status and console logs all read from one place.
"""

import time
import uuid

# transfer_id -> dict
active_transfers: dict = {}


def format_bytes(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def format_eta(seconds) -> str:
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


def new_transfer(file_name: str, stage: str, total_bytes: int, chat_id) -> str:
    """
    stage: one of "downloading" | "seedr_downloading" | "seedr_to_shell" | "uploading"
    """
    tid = uuid.uuid4().hex[:8]
    active_transfers[tid] = {
        "file_name": file_name,
        "stage": stage,
        "current": 0,
        "total": total_bytes or 1,
        "speed": 0.0,
        "start_time": time.monotonic(),
        "last_time": time.monotonic(),
        "last_bytes": 0,
        "chat_id": chat_id,
    }
    return tid


def update_transfer(tid: str, current: int):
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


STAGE_LABELS = {
    "downloading":       "⏬ Telegram → App",
    "seedr_downloading": "🌱 Seedr downloading torrent",
    "seedr_to_shell":    "⏬ Seedr → App",
    "uploading":         "☁️ App → Drive",
}


def transfer_summary(t: dict) -> str:
    pct = int(t["current"] / t["total"] * 100) if t["total"] else 0
    speed = t["speed"]
    remaining = t["total"] - t["current"]
    eta = remaining / speed if speed > 0 else None
    label = STAGE_LABELS.get(t["stage"], t["stage"])
    return (
        f"{label}: `{t['file_name']}`\n"
        f"   {pct}% ({format_bytes(t['current'])}/{format_bytes(t['total'])}) "
        f"@ {format_bytes(speed)}/s, ETA {format_eta(eta)}"
    )


def remove_transfer(tid: str):
    active_transfers.pop(tid, None)

