# Reaching the app from anywhere

`web.sh --lan` / `web.bat --lan` serves the page to **the same wifi**. That covers the shop.
It does not cover a phone on 4G, at a customer's house, or in another city.

Three routes close that gap, and they differ in one thing — whether the app stays private:

| | What you get | Who can reach it |
|---|---|---|
| **A / B — Tailscale** | A private network between your own devices | Only your signed-in devices |
| **C — a tunnel** | An ordinary public `https://…` URL, nothing to install | Anyone with the link, so **add the login** |

Two things stay true whichever you pick:

- **The computer must be awake with the app running.** Your Excel file lives on it. A
  sleeping laptop is unreachable — this is remote access, not a cloud service.
- **The app itself has no password.** On a tailnet that is fine, because only your own
  devices can reach it. On a public URL it is not, which is why Route C puts a login in
  front — and why Funnel, which does not, is the one option to avoid.

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

## Route C — a public URL from a tunnel, no VPN on the phone

Tailscale needs its app installed and signed in on every device. A **tunnel** instead gives
you an ordinary `https://…` address that any browser opens with nothing installed. The
computer still has to be awake and running the app; the tunnel only carries traffic to it.

**Understand what you are giving up.** A random-looking hostname is not a password. Links
leak — through referrer headers, browser sync, chat apps that fetch previews, and anyone who
forwards it. This app has no login of its own, holds an API key on the settings screen, and
writes into the shop's real workbook. **On a public URL, put a login in front of it.** Both
options below include one at no cost, and neither needs a change to the app.

### C1 — ngrok, if you have no domain

Free tier gives **one static domain** — a hostname that does not change between restarts,
which is what makes it usable with autostart.

**The domain is claimed in the dashboard, not from the CLI.** There is no `ngrok` command
that generates one; the agent only *uses* a domain that already exists on your account.

1. **Sign up** at [dashboard.ngrok.com](https://dashboard.ngrok.com) — free, no card.
2. **Claim the domain:** in the dashboard go to **Universal Edge → Domains** (older accounts
   show **Cloud Edge → Domains**) and press **+ New Domain**. Free accounts get one. You can
   type a subdomain you like, or accept the name it offers; either way you end up with
   something of the form `<name>.ngrok-free.app`. Copy it.
3. **Install the agent and save your token** — the token is on the dashboard's *Your
   Authtoken* page:
   ```bash
   ngrok config add-authtoken <your-token>
   ```
4. **Run it against that domain:**
   ```bash
   ngrok http 8765 --url https://<name>.ngrok-free.app \
                   --oauth google --oauth-allow-email you@gmail.com
   ```
   On ngrok agents older than v3.19 the flag is `--domain=<name>.ngrok-free.app` instead of
   `--url`. If one is rejected, use the other; `ngrok --version` tells you which you have.

`--oauth google` is available on the free plan and is the login: a Google account is
required, restricted to the addresses you list — repeat `--oauth-allow-email` per person.
`--basic-auth "user:password"` works instead if you prefer one shared password.

**Keep the flags in a config file rather than the command.** Cleaner, and it is what the
autostart step below runs. Put this in ngrok's config file — `ngrok config check` prints its
path and validates the file:

```yaml
version: "2"
authtoken: <your-token>
tunnels:
  lavabo:
    proto: http
    addr: 8765
    domain: <name>.ngrok-free.app
    oauth:
      provider: google
      allow_emails:
        - you@gmail.com
        - staff@gmail.com
```

Then the whole thing is `ngrok start lavabo`.

**Make the tunnel start itself too.** The app coming back after a reboot is no use if the
tunnel does not. On Windows, alongside the task from [docs/10](10-setup-guide.md) §5:

```powershell
schtasks /Create /TN "Lavabo Tunnel" /TR "ngrok start lavabo" /SC ONLOGON /RL LIMITED /F
```

On macOS, a second LaunchAgent with `ProgramArguments` of `ngrok`, `start`, `lavabo` and
`KeepAlive` set — ngrok reconnects on its own, but `KeepAlive` covers the process dying.

With the tunnel running, the app does **not** need `--lan`: ngrok connects to it on
`127.0.0.1:8765`, so you can drop that flag and stop the page being reachable from the shop
wifi at all.

Two free-tier facts to plan around:

- **20,000 HTTP requests a month**, 1 GB of transfer, 3 endpoints. The app is built to fit:
  status refreshes on load, on tab focus and after actions, plus a one-a-minute heartbeat
  while visible — about 14k a month for a tab open eight hours a day, every day. Leaving
  several tabs open all day on several phones will exceed it.
- **An interstitial warning page** appears before the app on free HTTP endpoints. Harmless —
  a human clicks through once — but it is the main reason to prefer C2.

### C2 — Cloudflare Tunnel + Access, if you have a domain (recommended)

Better in every way that matters, and the only cost is a domain (~$10/year) — bought from
any registrar, with its nameservers pointed at Cloudflare's free plan. No interstitial, no
request cap, and a real login.

**Cloudflare has no equivalent of ngrok's free static domain.** Its persistent hostnames bind
to a zone *you* control, so a named tunnel needs a domain; the account-less quick tunnel is
the only no-domain option and its hostname rotates. If you have no domain, C1 is the choice.

```bash
cloudflared tunnel login
cloudflared tunnel create lavabo
cloudflared tunnel route dns lavabo don.yourdomain.com
cloudflared tunnel run --url http://localhost:8765 lavabo
```

Then in the Zero Trust dashboard add an **Access** application for that hostname, with a
policy allowing only your staff's email addresses. Access is **free for up to 50 users** and
gives them a Google sign-in or an emailed code — no VPN, no shared password.

Install it as a service (`cloudflared service install`) so it comes back with the machine,
the same way step 5 of [docs/10](10-setup-guide.md) handles the app itself.

> **`cloudflared tunnel --url http://localhost:8765` with no account** also works and needs
> nothing at all — but it mints a **new random `*.trycloudflare.com` hostname every time it
> starts**. Fine for showing someone something once; useless as the URL your staff save,
> because autostart will change it on every reboot.

### Choosing

| | Tailscale (A/B) | ngrok (C1) | Cloudflare (C2) |
|---|---|---|---|
| App on the phone | **Required** | No | No |
| Stable URL | Yes | Yes (static domain) | Yes |
| Needs a domain | No | No | **Yes** |
| Login included | Not needed — private | Google OAuth, free | Access, free ≤50 users |
| Metered | No | 20k req, 1 GB/month | No |
| Reachable by strangers | **No** | Only past the login | Only past the login |

Tailscale stays the safest because the app is never exposed at all. Pick a tunnel when
installing an app on every phone is the bigger problem — and then do not skip the login.

---

## If it does not connect

| Symptom | Cause |
|---|---|
| Phone cannot reach `100.x` at all | Tailscale toggled off on one device, or the two are signed into different accounts — check the [machines list](https://login.tailscale.com/admin/machines) shows both |
| `tailscale ip` works, browser times out | The app was started without `--lan` (Route A needs it) |
| Times out on Windows only | Windows Firewall is blocking inbound 8765 — allow Python when prompted, or add an inbound rule for TCP 8765 on the private profile |
| Page loads but shows no orders | Reached a different machine — confirm the address belongs to the computer running the app |
| Worked yesterday, not today | The computer is asleep or the app was closed |
| Tunnel URL changed by itself | A Cloudflare *quick* tunnel — use a named tunnel (C2) or ngrok's static domain (C1) |
| ngrok stopped serving mid-month | The 20k request or 1 GB allowance ran out — close idle tabs, or move to C2 |
| ngrok: "domain not found" / not authorized | The domain was never claimed in the dashboard, or belongs to another account — see C1 step 2 |
| ngrok: unknown flag `--url` | An agent older than v3.19 — use `--domain=<name>.ngrok-free.app` |
| Tunnel up, but 502 from ngrok | The app is not running, or is on a different port than the tunnel's `addr` |

## Do not use Funnel

`tailscale funnel` looks like Serve and does the opposite: it publishes the page **to the
public internet** with no login in front of it. Anyone who found the URL could append rows
to the shop's workbook.

Serve (Route B) is the private one. Funnel is the public one **with nothing guarding it** —
which is the one difference between Funnel and Route C above. If you want a public URL, use
C1 or C2, where the login comes included.
