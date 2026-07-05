# File2Link-Wydia-Telegram-Bot-
Telegram bot to save the files from telegram to google drive or share somewhere else

# File2Link — Ubuntu / Cloud Shell version

Migrates the old Colab notebook bot to a plain Python app. Downloads
videos/documents sent to your Telegram bot and uploads them straight to a
Google Drive **Shared Drive** folder using a service account — no rclone,
no OAuth browser flow.

## 1. One-time Google Cloud setup

1. Go to console.cloud.google.com → pick/create a project.
2. Enable the **Google Drive API** (APIs & Services → Library).
3. Create a **Service Account** (APIs & Services → Credentials → Create
   Credentials → Service Account). Give it any name, no roles needed.
4. Open the service account → **Keys** → Add Key → JSON. This downloads
   `service-account.json` — save it, you'll upload it to Cloud Shell.
5. Note the service account's email address, e.g.
   `file2link@your-project.iam.gserviceaccount.com`.

## 2. Share the Drive folder with the service account

1. In Google Drive, open the Shared Drive you were using before
   (e.g. `Depot6_Index_Paper`), or the specific folder inside it.
2. Share it with the service account's email address, giving it
   **Content Manager** (or higher) access.
3. Grab the **folder ID** from the folder's URL:
   `https://drive.google.com/drive/folders/<THIS_IS_THE_FOLDER_ID>`
4. Grab the **Shared Drive ID** the same way from the Shared Drive's URL.

## 3. Cloud Shell setup (persists in $HOME across sessions)

```bash
cd ~
git clone <your-repo-url> file2link   # or just upload these files
cd file2link

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Upload service-account.json into ~/file2link/ via Cloud Shell's upload button
cp .env.example .env
nano .env   # fill in your Telegram + Drive values
```

## 4. Running it

Cloud Shell VMs are ephemeral and time-limited, so use `tmux` so the bot
keeps running if your browser tab disconnects, and so you can reattach
after Cloud Shell resets:

```bash
tmux new -s file2link
source venv/bin/activate
python3 main.py
# detach with: Ctrl+B then D
```

To reattach later: `tmux attach -t file2link`

Since Cloud Shell recycles the VM, you'll need to re-run
`tmux new -s file2link && source venv/bin/activate && python3 main.py`
each time you start a new session — but `venv/`, `.env`, and
`service-account.json` all persist in `$HOME` so you won't need to
reinstall anything.

## 5. Pushing to Git

The `.gitignore` in this repo excludes `.env`, `service-account.json`,
Pyrogram `*.session` files, and `venv/` — none of these should ever be
committed since they all contain live credentials.

```bash
cd ~/file2link
git init
git add .
git commit -m "Initial commit: File2Link bot"
git remote add origin <your-repo-url>
git push -u origin main
```

Only `.env.example` (a template with placeholder values) is tracked —
each machine you run this on needs its own `.env` and
`service-account.json` created locally, never pulled from git.

## Notes vs. the original Colab version

- No Google Drive **mount** needed — uploads go directly through the
  Drive API, so this also works from any machine, not just one with
  Drive mounted.
- Files download to `/tmp` (fast, ephemeral) instead of a local Colab
  folder, and are deleted immediately after a successful upload.
- Progress edits to the Telegram status message are throttled (every ~4s)
  to avoid Telegram rate limits during upload.
- If you outgrow Cloud Shell's weekly free-tier limit, this same script
  runs unmodified on any Ubuntu VM (e.g. a free-tier GCE e2-micro) — at
  that point you could switch to a proper `systemd` service for
  always-on behavior instead of `tmux`.
