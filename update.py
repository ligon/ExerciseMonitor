"""Self-updater that pulls the latest code from https://github.com/ligon/ExerciseMonitor (even without Git installed)."""

import io
import os
import shutil
import ssl
import urllib.request
import zipfile

REPO_ZIP_URL = "https://github.com/ligon/ExerciseMonitor/archive/refs/heads/main.zip"
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# Only overwrite code & docs — never touch data/ or .venv/
SYNC_PREFIXES = ("app/", "tests/", "README.md", "requirements.txt", "run.bat", "run.sh", "update.py", ".gitignore")


def _download_zip_bytes(url: str) -> bytes:
    """
    Download `url` using `requests` (which bundles Mozilla CA certificates via `certifi` on Windows),
    with fallbacks to `urllib` + `certifi` and unverified SSL context if the Windows root store is incomplete.
    """
    try:
        import requests

        resp = requests.get(
            url,
            headers={"User-Agent": "ExerciseMonitor-Updater/1.1"},
            timeout=25,
        )
        resp.raise_for_status()
        return resp.content
    except Exception:
        pass

    req = urllib.request.Request(url, headers={"User-Agent": "ExerciseMonitor-Updater/1.1"})
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(req, timeout=25, context=ctx) as resp:
            return resp.read()
    except Exception:
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=25, context=ctx) as resp:
            return resp.read()


def update_from_github() -> int:
    print(f"[Update] Downloading latest release from {REPO_ZIP_URL} ...")
    raw_zip = _download_zip_bytes(REPO_ZIP_URL)

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
    return updated_count


if __name__ == "__main__":
    update_from_github()
