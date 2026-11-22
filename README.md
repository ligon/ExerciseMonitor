# Stelo + Garmin 24/7 Metabolic & Exercise Monitor

A local, privacy-first 24/7 timeline dashboard combining:
- **Dexcom Stelo Blood Glucose** (5-minute readings via automated Dexcom Clarity Share Code pull, Clarity CSV upload, or Zukka/xDrip+ live webhook)
- **Garmin Watch 24/7 Heart Rate & Workouts** (per-second `.fit` activity decoding for Speed, Elevation/Climb, Grade %, Cadence, Power, and Heart Rate)
- **Meal Logging & 2-Hour Postprandial Excursion Analytics** (plus automatic unlogged glucose spike detection and Garmin Connect / MyFitnessPal nutrition sync)

---

## Quick Start on Windows

1. **Install Python 3.11+** (if not already installed):
   - Download from [python.org/downloads](https://www.python.org/downloads/) (check **"Add python.exe to PATH"** in the installer), or install **Python 3.12** from the Microsoft Store.
2. **Double-click `run.bat`**:
   - On the first run, `run.bat` automatically creates a `.venv` folder, installs all dependencies from `requirements.txt` (including `tzdata` for Windows timezones), opens **http://localhost:8000** in your browser, and starts the local server.

---

## Quick Start on Linux / macOS / Android Laptop

```bash
./run.sh
```
Then open **http://localhost:8000** in your browser.

---

## One-Time Setup Inside the Dashboard (`⚙️ Setup / Import` or `🔄 Sync Now`)

1. **Dexcom Stelo (Android or iPhone):**
   - Install the official **Dexcom Clarity** app on your phone and log in with your Stelo account.
   - Go to **Profile → Authorize Sharing → Generate Code (12 Months)**.
   - Paste the 12-character code (`XXXX-XXXX-XXXX`) into the dashboard's **Unified One-Button & Auto-Polling Setup**.
2. **Garmin Watch:**
   - Enter your **Garmin Connect** email and password in the same setup dialog.
   - Click **🔄 Save & Sync Both Now**.
3. **Automatic Polling:**
   - While the dashboard tab is open, it automatically polls both Dexcom Clarity and Garmin Connect every 15 minutes (configurable from 5m to 60m in the top bar).
