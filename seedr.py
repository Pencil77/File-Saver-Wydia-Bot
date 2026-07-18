"""
seedr.py — Magnet link handling via Seedr.cc REST API.
Fully compatible with main.py's expected call signature.
"""

import os
import re
import time
import httpx
from config import LOCAL_DOWNLOAD_PATH, log

BASE_URL = "https://www.seedr.cc/rest"

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}

def _get(path: str, token: str, **params) -> dict:
    resp = httpx.get(f"{BASE_URL}/{path}", headers=_headers(token), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

def _post(path: str, token: str, data: dict) -> dict:
    resp = httpx.post(f"{BASE_URL}/{path}", headers=_headers(token), data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()

def _delete(path: str, token: str) -> dict:
    resp = httpx.delete(f"{BASE_URL}/{path}", headers=_headers(token), timeout=30)
    resp.raise_for_status()
    return resp.json()

def add_magnet(magnet: str, token: str) -> int:
    hash_match = re.search(r"xt=urn:btih:([a-zA-Z0-9]+)", magnet, re.IGNORECASE)
    if hash_match:
        magnet = f"magnet:?xt=urn:btih:{hash_match.group(1)}"
    result = _post("transfer/magnet", token, {"magnet": magnet})
    if result.get("error"):
        raise RuntimeError(f"Seedr API Error: {result.get('error')}")
    return result["user_torrent_id"]

def poll_torrent(torrent_id: int, token: str, progress_cb=None) -> dict:
    while True:
        status = _get(f"torrent/{torrent_id}", token)
        if progress_cb:
            progress_cb(status.get("name", "torrent"), status.get("progress", 0), status.get("size", 0))
        if status.get("progress", 0) >= 100:
            return status
        time.sleep(5)

def list_files(folder_id: int, token: str) -> list[dict]:
    result = _get(f"folder/{folder_id}", token)
    files = []
    for f in result.get("files", []):
        files.append({"id": f.get("id") or f.get("folder_file_id"), "name": f["name"], "size": f["size"], "folder_id": folder_id})
    for subfolder in result.get("folders", []):
        files.extend(list_files(subfolder["id"], token))
    return files

# Signature now matches what main.py is calling: (id, name, token, dest_dir, cb)
def download_file(file_id: int, file_name: str, token: str, dest_dir: str = LOCAL_DOWNLOAD_PATH, progress_cb=None) -> str:
    local_path = os.path.join(dest_dir, file_name)
    for attempt in range(3):
        try:
            url = _get(f"file/{file_id}", token).get("url")
            with httpx.stream("GET", url, headers=_headers(token), timeout=None, follow_redirects=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                with open(local_path, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1024*1024):
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            progress_cb(downloaded, total)
            return local_path
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
                continue
            raise e
    return local_path

def delete_folder(folder_id: int, token: str):
    _delete(f"folder/{folder_id}", token)

def delete_torrent(torrent_id: int, token: str):
    _delete(f"torrent/{torrent_id}", token)
