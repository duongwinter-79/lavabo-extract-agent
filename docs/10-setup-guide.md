# Setup guide — from nothing to a URL that always works

Follow this once on the computer that will hold the orders. Windows commands are given
first, macOS in the same step. Roughly 20 minutes, most of it waiting on downloads.

At the end you will have: the app running on its own whenever the computer is on, a Gemini
key configured, and a URL your staff can open from any phone anywhere.

**One thing to be clear about before you start:** this runs on *your* computer. It is
reachable whenever that computer is awake, and not when it is off or asleep. Step 5 turns
off sleep. If the machine is a laptop that goes home with you, staff lose access while it
is closed — that is the point at which renting a small server starts to be worth it
(see [docs/09](09-cloud-architecture.md) §6).

---

## Step 1 — Install Python

Only needed once, and skip it if `python --version` already prints 3.11 or newer.

Download from [python.org/downloads](https://www.python.org/downloads/). **During the
installer, tick "Add python.exe to PATH"** — the setup script cannot find Python without it,
and this is the single most common reason step 2 fails.

macOS: `brew install python@3.12`, or the same installer.

---

## Step 2 — Get the code and install it

```powershell
git clone https://github.com/duongwinter-79/lavabo-extract-agent.git
cd lavabo-extract-agent
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

macOS / Linux:

```bash
git clone https://github.com/duongwinter-79/lavabo-extract-agent.git
cd lavabo-extract-agent
bash scripts/setup.sh
```

This creates `.venv`, installs the dependencies, and seeds `config/config.yaml`,
`config/schema.yaml` and `.env` from the examples. It is safe to re-run — it never
overwrites files you have edited.

It finishes with a preflight check. `FAIL llm: ... is not set` is expected at this point;
that is the key you add in step 3.

---

## Step 3 — Gemini key

The config already selects Gemini with a free-tier-friendly model
(`gemini-3.1-flash-lite`, `concurrency: 1`), so the only thing missing is the key.

1. Open [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and sign in with a
   Google account.
2. **Create API key**, then copy it. It starts with `AIza` and is about 39 characters.
3. Paste it into the app rather than a file — start the app (step 4), open the **gear icon**,
   paste into **API key**, press **Kiểm tra key**, and once it comes back green press
   **Lưu và áp dụng**.

*Kiểm tra key* asks Google whether the key actually works before you commit to it. That
matters: an unverified key looks fine and then fails on your first real export, after
you have already spent time capturing.

If you would rather edit the file, put it in `.env` instead:

```
GEMINI_API_KEY=AIza...your-real-key...
```

**Free tier notes.** No card required. It is rate-limited to a few requests per minute,
which is why `concurrency: 1` is set — the app waits and retries rather than failing. About
90 orders takes a few minutes to extract. Leave it running.

---

## Step 4 — Start the web app

Double-click **`web.bat`** (Windows) or **`web.command`** (macOS). A window opens showing:

```
  Lavabo — mở trong trình duyệt:

    máy này:      http://127.0.0.1:8765

  (dùng --lan để mở được từ điện thoại)
```

Open `http://127.0.0.1:8765`. You should see the order counter, the month selector and the
paste box.

To reach it from another device you need `--lan`, which double-clicking cannot pass. From a
terminal in the project folder:

```powershell
web.bat --lan
```

```bash
bash web.sh --lan
```

Now it also prints a `192.168.x.x` address, which works from a phone **on the same wifi**.
Step 6 removes the same-wifi limitation.

> **Windows Firewall** will ask to allow Python the first time you use `--lan`. Say yes. If
> you dismissed it, other devices will time out while `127.0.0.1` still works — add an
> inbound rule for TCP 8765 on the private profile.

Close the window to stop the app. Step 5 means you will not need to open it again.

---

## Step 5 — Make it start itself

So you never have to remember. Two parts: start on login, and never sleep.

### Windows — start on login

Run this once, from the project folder, in PowerShell:

```powershell
$exe = "$PWD\.venv\Scripts\pythonw.exe"
$app = "$PWD\scripts\lavabo_web.py"
schtasks /Create /TN "Lavabo Web" /TR "`"$exe`" `"$app`" --lan" /SC ONLOGON /RL LIMITED /F
```

`pythonw.exe` runs it with no console window, so it is simply there, invisible, from the
moment you log in.

Check it, or remove it later:

```powershell
schtasks /Run /TN "Lavabo Web"        # start it now, without logging out
schtasks /Query /TN "Lavabo Web"      # is it registered?
schtasks /Delete /TN "Lavabo Web" /F  # undo all of this
```

### macOS — start on login

Save as `~/Library/LaunchAgents/com.lavabo.web.plist`, replacing `/path/to` with your real
project path:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.lavabo.web</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/lavabo-extract-agent/.venv/bin/python</string>
    <string>/path/to/lavabo-extract-agent/scripts/lavabo_web.py</string>
    <string>--lan</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/lavabo-extract-agent</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

Then `launchctl load ~/Library/LaunchAgents/com.lavabo.web.plist`. `KeepAlive` also restarts
it if it ever crashes.

### Never sleep

A sleeping computer serves nothing, so this step is not optional if staff rely on the URL.

- **Windows:** Settings → System → Power → *Screen and sleep* → set **Sleep** to *Never*
  when plugged in. Turning the screen off is fine.
- **macOS:** System Settings → Displays → Advanced → *Prevent automatic sleeping on power
  adapter when the display is off*.

---

## Step 6 — Tailscale, for access from anywhere

Steps 4 and 5 give you a URL that works on the shop wifi. Tailscale makes it work from 4G,
from a customer's house, from anywhere — as a private network between **your own devices**.
Nothing is published and no router port is opened.

1. **On this computer:** install from [tailscale.com/download](https://tailscale.com/download)
   and sign in (Google or Microsoft account is fine).
2. **On each phone:** Tailscale from the App Store or Play Store, **signed into the same
   account**, toggled on.
3. **Find this computer's address:**
   ```powershell
   tailscale ip -4        # e.g. 100.101.102.103
   ```
   It is stable — write it down once. The tray icon and the
   [admin console](https://login.tailscale.com/admin/machines) also show it.
4. **On the phone, open** `http://100.101.102.103:8765`

That is the URL to give your staff. It keeps working as they move between networks.

Prefer a public URL your staff can open with **nothing installed**? Use a tunnel instead —
ngrok's free static domain or Cloudflare Tunnel, both with a login in front. See
[docs/08-tailscale.md](08-tailscale.md) §Route C — a tunnel needs no app on the phone, but
because the URL is public the login is not optional.

For a proper HTTPS name on Tailscale — `https://your-pc.tailnet-name.ts.net` — see §Route B
there. It is a little more setup and lets you drop `--lan`, which also stops the page being
reachable from the shop wifi at all.

**Do not use `tailscale funnel`.** It looks similar and publishes the page to the entire
internet. The app has no password, and *Thêm vào file quản lý* writes into your real
workbook.

---

## Step 7 — Your first real run

1. In Zalo, select an order conversation and copy it.
2. In the app, check **Tháng đang nhập** is the right month.
3. Pick **Người chốt đơn** — tap a name, or **+ Thêm tên** for a new one. This fills the
   column your revenue split reports on, so it matters more than it looks.
4. Paste into the box, press **Lưu đơn**. It reports how many orders it found and saved.
5. Repeat for the month's conversations. Duplicates are ignored, so re-pasting is safe.
6. Press **Xuất file Excel mới** and download the result. **Check this file before trusting
   it** — the AI columns are the part worth reviewing.
7. Once it looks right, set your workbook path in the gear screen and use **Thêm vào file
   quản lý** from then on. It backs the file up before every write.

---

## If something is wrong

| Symptom | Fix |
|---|---|
| `setup.ps1` fails immediately | Python is missing from PATH — reinstall it with that box ticked |
| `ZoneInfoNotFoundError: 'No time zone found with key Asia/Ho_Chi_Minh'` | Windows has no timezone database. `.venv\Scripts\python.exe -m pip install tzdata`, then restart the app. Installs from this commit on already include it |
| Page loads, extraction fails | Key not set or wrong — gear icon, paste it, **Kiểm tra key** |
| "Không tìm thấy đơn nào" | The paste must start with a header like `15/8 đơn 1 - Tên KH` or `2/7 đơn 2 (Tên KH)` |
| Phone times out, `127.0.0.1` works | Started without `--lan`, or Windows Firewall is blocking 8765 |
| Phone cannot reach `100.x` at all | Tailscale off on one device, or different accounts — check the admin console lists both |
| Worked yesterday, not today | The computer is asleep or off — revisit step 5 |
| **Thêm vào file quản lý** greyed out | No workbook set, or the path does not exist — gear icon |
| Rate-limit warnings during extraction | Normal on the free tier. It waits and retries; leave it |

## Worth doing once your orders live here

Back up `data/staging.db` and your workbook somewhere off this machine — OneDrive, iCloud, a
USB drive, anything. The app keeps timestamped `.backup-*.xlsx` copies beside the workbook
before each write, but those sit on the same disk, which is not a backup.
