"""Self-updater that pulls the latest code from https://github.com/ligon/ExerciseMonitor (even without Git installed)."""

import io
import os
import shutil
import urllib.request
import zipfile

REPO_ZIP_URL = "https://github.com/ligon/ExerciseMonitor/archive/refs/heads/main.zip"
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# Only overwrite code & docs — never touch data/ or .venv/
SYNC_PREFIXES = ("app/", "tests/", "README.md", "requirements.txt", "run.bat", "run.sh", "update.py", ".gitignore")


def update_from_github() -> None:
    print(f"[Update] Downloading latest release from {REPO_ZIP_URL} ...")
    req = urllib.request.Request(REPO_ZIP_URL, headers={"User-Agent": "ExerciseMonitor-Updater/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw_zip = resp.read()

    updated_count = 0
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            # Strip top-level folder 'ExerciseMonitor-main/'
            parts = member.filename.split("/", 1)
            if len(parts) < 2:
                continue
            rel_path = parts[1]
            if not any(rel_path == p or rel_path.startswith(p) for p in SYNC_PREFIXES):
                continue

            dest_path = os.path.join(ROOT_DIR, rel_path)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with zf.open(member) as src, open(dest_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            updated_count += 1

    print(f"[Update] Successfully updated {updated_count} files from GitHub (data/monitor.db preserved).")


if __name__ == "__main__":
    update_from_github()
