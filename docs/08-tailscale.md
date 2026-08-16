# Reaching the app from anywhere — Tailscale

`web.sh --lan` / `web.bat --lan` serves the page to **the same wifi**. That covers the shop.
It does not cover a phone on 4G, at a customer's house, or in another city.

Tailscale closes that gap. It is a private network between **your own devices** — install it
on the computer and the phone, sign both into the same account, and they can reach each
other over the internet as if they were on one wifi. Nothing is published: there is no
public address, and no port is opened on your router.

Two things stay true whichever route you take:

- **The computer must be awake with the app running.** Your Excel file lives on it. A
  sleeping laptop is unreachable — this is remote access, not a cloud service.
- **The page still has no password.** On a tailnet that is fine, because only your own
  signed-in devices can reach it. It is *not* fine to combine with Funnel (below).

---

## Route A — the private IP, in three minutes

Simplest, and enough for most people. Gives an `http://100.x.y.z:8765` address.

### 1. Install Tailscale on the computer

- **Windows / macOS:** download from [tailscale.com/download](https://tailscale.com/download)
- Sign in — Google or Microsoft account is fine. **Use the same account on every device.**

### 2. Install it on the phone

Tailscale from the App Store or Play Store, signed into that same account. Toggle it on.

### 3. Find the computer's Tailscale address

```bash
tailscale ip -4          # prints something like 100.101.102.103
```

On Windows, the Tailscale tray icon shows it too, and the
[admin console](https://login.tailscale.com/admin/machines) lists every device.

Addresses in `100.x.y.z` are the tailnet's own range. They are stable — worth writing down
once.

### 4. Start the app so it listens beyond localhost

```bash
web.bat --lan             # Windows, from a terminal
bash web.sh --lan         # macOS / Linux
```

`--lan` is required. Without it the app binds to `127.0.0.1` and refuses every connection
that is not from the computer itself, Tailscale included.

### 5. Open it on the phone

```
http://100.101.102.103:8765
```

Tailscale must be toggled on on the phone. If it loads, you are done — this works on 4G,
on someone else's wifi, anywhere.

---

## Route B — a proper HTTPS name

Better, and only a little more work. `tailscale serve` proxies the app itself, which means:

- a real name and a valid certificate — `https://your-pc.tailnet-name.ts.net`, no warnings
- **the app stays on `127.0.0.1`** — so drop `--lan`, and the page stops being reachable
  from the shop wifi at all. Strictly tighter than Route A.

### 1. Enable HTTPS for your tailnet, once

In the [admin console DNS page](https://login.tailscale.com/admin/dns): turn on **MagicDNS**,
then under **HTTPS Certificates** select **Enable HTTPS**.

### 2. Start the app normally — no `--lan`

```bash
web.bat                   # Windows
bash web.sh               # macOS / Linux
```

### 3. Put Tailscale in front of it

```bash
tailscale serve --bg 8765
```

`--bg` keeps it running in the background. It prints the URL to open, of the form
`https://<machine-name>.<tailnet-name>.ts.net`.

To check or undo:

```bash
tailscale serve status
tailscale serve --https=443 off
```

---

## If it does not connect

| Symptom | Cause |
|---|---|
| Phone cannot reach `100.x` at all | Tailscale toggled off on one device, or the two are signed into different accounts — check the [machines list](https://login.tailscale.com/admin/machines) shows both |
| `tailscale ip` works, browser times out | The app was started without `--lan` (Route A needs it) |
| Times out on Windows only | Windows Firewall is blocking inbound 8765 — allow Python when prompted, or add an inbound rule for TCP 8765 on the private profile |
| Page loads but shows no orders | Reached a different machine — confirm the address belongs to the computer running the app |
| Worked yesterday, not today | The computer is asleep or the app was closed |

---

## Do not use Funnel

`tailscale funnel` looks like the same thing and is not: it publishes the page **to the
public internet**. This app has no login, and appending writes into the shop's real
workbook. Anyone who found the URL could add rows to it.

Serve (Route B) is the private one. Funnel is the public one. Only Serve belongs here.
