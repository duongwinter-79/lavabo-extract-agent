#!/usr/bin/env python3
"""Lavabo web — paste orders from any device, get the Excel file.

Same pipeline as the desktop app, reached through a browser so a phone works as well
as the laptop. Deliberately built on the standard library: adding a web framework
would mean another dependency for someone whose whole install is a double-click.

    python scripts/lavabo_web.py            # laptop only, http://127.0.0.1:8765
    python scripts/lavabo_web.py --lan      # also reachable from the phone

Binding to the LAN is opt-in. The page has no login, so anyone on the same network
can reach it while it runs -- fine on a shop's own wifi, not on open networks.
"""

from __future__ import annotations

import argparse
import io
import json
import socket
import sys
import threading
from contextlib import redirect_stdout
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from lavabo.config import Config  # noqa: E402

PAGE = """<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Lavabo — Đơn hàng Zalo</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --ink:#16181d; --muted:#6b7280;
          --line:#e5e7eb; --brand:#1f3864; --ok:#15803d; --warn:#b45309; --err:#b91c1c; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f1115; --card:#171a21; --ink:#e8eaed; --muted:#9aa1ac;
            --line:#272b33; --brand:#7aa2e3; --ok:#4ade80; --warn:#fbbf24; --err:#f87171; }
  }
  * { box-sizing:border-box; -webkit-text-size-adjust:100%; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .wrap { max-width:680px; margin:0 auto; padding:16px 16px 48px; }
  h1 { font-size:19px; margin:12px 0 4px; }
  .sub { color:var(--muted); font-size:14px; margin:0 0 18px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px;
          padding:16px; margin-bottom:14px; }
  textarea { width:100%; min-height:190px; padding:12px; font-size:16px;
             font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
             border:1px solid var(--line); border-radius:10px;
             background:var(--bg); color:var(--ink); resize:vertical; }
  button { width:100%; padding:15px; font-size:17px; font-weight:600; border:0;
           border-radius:11px; background:var(--brand); color:#fff; cursor:pointer;
           margin-top:10px; }
  button.ghost { background:transparent; color:var(--brand);
                 border:1.5px solid var(--brand); }
  button:disabled { opacity:.5; cursor:default; }
  .count { font-size:34px; font-weight:700; line-height:1.1; }
  .row { display:flex; align-items:baseline; gap:10px; }
  .msg { margin-top:12px; padding:11px 13px; border-radius:9px; font-size:14.5px;
         white-space:pre-wrap; }
  .msg.ok { background:color-mix(in srgb,var(--ok) 14%,transparent); color:var(--ok); }
  .msg.warn { background:color-mix(in srgb,var(--warn) 16%,transparent); color:var(--warn); }
  .msg.err { background:color-mix(in srgb,var(--err) 14%,transparent); color:var(--err); }
  a.dl { display:block; text-align:center; padding:15px; margin-top:10px;
         border-radius:11px; background:var(--ok); color:#fff; font-weight:600;
         text-decoration:none; }
  code { background:var(--bg); padding:1px 5px; border-radius:5px; font-size:13px; }
  .hint { color:var(--muted); font-size:13.5px; margin-top:10px; }
  .lbl { display:block; font-size:13.5px; font-weight:600; color:var(--muted);
         margin-bottom:7px; }
  select { width:100%; padding:13px 12px; font-size:16px; border-radius:10px;
           border:1px solid var(--line); background:var(--bg); color:var(--ink); }
</style></head><body><div class="wrap">

<h1>Trích xuất đơn hàng Zalo</h1>
<p class="sub">Dán đoạn chat vào ô dưới, bấm Lưu. Xong hết thì bấm Xuất Excel.</p>

<div class="card">
  <div class="row"><span class="count" id="count">–</span>
    <span style="color:var(--muted)">đơn đã lưu — tháng <span id="period"></span></span></div>
</div>

<div class="card">
  <label class="lbl" for="closer">Người chốt đơn</label>
  <select id="closer"></select>
  <div class="hint">Áp dụng cho các đơn dán bên dưới. Dán lại cùng đơn với tên khác thì tên mới thay tên cũ.</div>
</div>

<div class="card">
  <textarea id="paste" placeholder="Mở Zalo, bôi đen đoạn chat, Copy rồi dán vào đây…"></textarea>
  <button id="save">Lưu đơn</button>
  <div class="hint">Chỉ nhận tin nhắn bắt đầu bằng <code>15/8 đơn 1 - Tên KH</code>.
    Dán trùng nhau không sao, mỗi đơn chỉ lưu một lần.</div>
  <div id="m1"></div>
</div>

<div class="card">
  <button id="export" class="ghost">Xuất file Excel</button>
  <div id="m2"></div>
  <div id="dl"></div>
</div>

<script>
const $ = i => document.getElementById(i);
const show = (el, kind, text) => { el.innerHTML = ''; if(!text) return;
  const d = document.createElement('div'); d.className = 'msg ' + kind; d.textContent = text;
  el.appendChild(d); };

const NEW_NAME = '__them_ten_moi__';
let closers = [];

// The picked name is remembered on the device, so the phone and the laptop can be two
// different people capturing their own orders without resetting each other.
const remembered = () => localStorage.getItem('lavabo.closer') || '';

function drawClosers(names, current) {
  closers = names;
  const sel = $('closer');
  sel.innerHTML = '';
  if (!names.length && !current) {
    const o = document.createElement('option');
    o.value = ''; o.textContent = '— chưa có tên, chọn "Tên khác…" —';
    sel.appendChild(o);
  }
  for (const n of names) {
    const o = document.createElement('option'); o.value = n; o.textContent = n;
    sel.appendChild(o);
  }
  const other = document.createElement('option');
  other.value = NEW_NAME; other.textContent = 'Tên khác…';
  sel.appendChild(other);
  if (current && names.includes(current)) sel.value = current;
}

$('closer').onchange = () => {
  const sel = $('closer');
  if (sel.value !== NEW_NAME) { localStorage.setItem('lavabo.closer', sel.value); return; }
  const typed = (prompt('Tên người chốt đơn') || '').trim();
  if (!typed) { sel.value = remembered(); return; }
  if (!closers.includes(typed)) closers.unshift(typed);
  localStorage.setItem('lavabo.closer', typed);
  drawClosers(closers, typed);
};

async function refresh() {
  try {
    const r = await fetch('/api/status'); const s = await r.json();
    $('count').textContent = s.orders; $('period').textContent = s.period;
    const keep = $('closer').value === NEW_NAME ? remembered() : ($('closer').value || remembered());
    const names = s.closers || [];
    if (keep && !names.includes(keep)) names.unshift(keep);
    drawClosers(names, keep);
  } catch (e) { /* server restarting */ }
}

$('save').onclick = async () => {
  const text = $('paste').value;
  if (!text.trim()) { show($('m1'),'warn','Chưa có nội dung nào.'); return; }
  const closer = $('closer').value === NEW_NAME ? remembered() : $('closer').value;
  if (!closer) { show($('m1'),'warn','Chọn người chốt đơn trước đã.'); return; }
  $('save').disabled = true; show($('m1'),'ok','Đang lưu…');
  try {
    const r = await fetch('/api/capture?closer=' + encodeURIComponent(closer),
                          {method:'POST', body:text});
    const s = await r.json();
    if (s.error) show($('m1'),'err',s.error);
    else if (!s.found) show($('m1'),'warn',
      'Không tìm thấy đơn nào. Tin nhắn phải bắt đầu bằng ngày và số đơn, ví dụ "15/8 đơn 1 - Tên KH".');
    else {
      let t = `Tìm thấy ${s.found} đơn — lưu mới ${s.saved}`;
      if (s.duplicates) t += `, đã có ${s.duplicates}`;
      if (s.other_month) t += `, ${s.other_month} khác tháng`;
      show($('m1'), s.saved ? 'ok' : 'warn', t);
      if (s.saved) $('paste').value = '';
    }
  } catch (e) { show($('m1'),'err','Lỗi kết nối: ' + e); }
  $('save').disabled = false; refresh();
};

$('export').onclick = async () => {
  $('export').disabled = true; $('dl').innerHTML = '';
  show($('m2'),'ok','Đang xử lý… bản Gemini miễn phí chạy chậm, vui lòng đợi.');
  try {
    await fetch('/api/export', {method:'POST'});
    const poll = setInterval(async () => {
      const r = await fetch('/api/export/status'); const s = await r.json();
      if (s.state === 'running') { show($('m2'),'ok', s.step || 'Đang xử lý…'); return; }
      clearInterval(poll); $('export').disabled = false;
      if (s.state === 'error') { show($('m2'),'err', s.message); return; }
      show($('m2'), s.warning ? 'warn' : 'ok', s.message);
      $('dl').innerHTML = `<a class="dl" href="/download/${encodeURIComponent(s.file)}">Tải file Excel</a>`;
    }, 1500);
  } catch (e) { show($('m2'),'err','Lỗi: ' + e); $('export').disabled = false; }
};

refresh(); setInterval(refresh, 5000);
</script>
</div></body></html>
"""

JOB: dict = {"state": "idle", "step": "", "message": "", "file": "", "warning": False}
JOB_LOCK = threading.Lock()


def lan_ip() -> str:
    """Best-effort LAN address, so the phone gets a URL that actually resolves."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))          # no packet is sent; picks the route
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def run_export(cfg: Config, month: int, year: int, closer: str) -> None:
    from lavabo.cli import cmd_extract, cmd_ingest, cmd_load
    from lavabo.store import Store

    def step(text: str) -> None:
        with JOB_LOCK:
            JOB["step"] = text

    class A:
        pass

    try:
        step("Đang đọc các đơn đã lưu…")
        a = A(); a.source = "zalo"; a.full = False
        with io.StringIO() as sink, redirect_stdout(sink):
            cmd_ingest(a, cfg)

        with Store(cfg.db_path) as store:
            total = store.stats()["conversations"]
        if not total:
            raise RuntimeError("Chưa có đơn nào để xuất.")

        step(f"Đang trích xuất {total} đơn bằng AI…")
        a = A(); a.source = "zalo"; a.limit = None; a.force = False
        a.dry_run = False; a.strict = False
        a.provider = a.model = a.api_key = None
        with io.StringIO() as sink, redirect_stdout(sink):
            cmd_extract(a, cfg)
            extract_log = sink.getvalue()
        failed = extract_log.count("  FAIL ")

        step("Đang ghi file Excel…")
        out = cfg.output_dir / f"donhang-{year}{month:02d}.xlsx"
        a = A(); a.out = str(out); a.layout = "senkahomes"
        a.sheet = None; a.year = year; a.status = "New"; a.closer = closer or None
        a.provider = a.model = a.api_key = None
        with io.StringIO() as sink, redirect_stdout(sink):
            cmd_load(a, cfg)

        message = f"Xong. {total} đơn."
        if failed:
            message += f" {failed} đơn trích xuất lỗi — các cột AI sẽ trống."
        with JOB_LOCK:
            JOB.update(state="done", step="", message=message,
                       file=out.name, warning=bool(failed))
    except Exception as exc:
        with JOB_LOCK:
            JOB.update(state="error", step="", message=f"{type(exc).__name__}: {exc}",
                       file="", warning=True)


class Handler(BaseHTTPRequestHandler):
    cfg: Config
    month: int
    year: int
    closer: str

    def log_message(self, *args) -> None:          # quiet; the page is the interface
        pass

    # ---------------------------------------------------------------- helpers

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _orders_on_disk(self) -> int:
        inbox = self.cfg.zalo.inbox_dir
        return len(list(inbox.glob("*.txt"))) if inbox.exists() else 0

    # ------------------------------------------------------------------ routes

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            from lavabo import closers

            names = closers.known_names(self.cfg.zalo.inbox_dir)
            if self.closer and self.closer not in names:
                names.append(self.closer)          # the config default, as a fallback
            self._json({"orders": self._orders_on_disk(),
                        "period": f"{self.month:02d}/{self.year}",
                        "closers": names})
        elif path == "/api/export/status":
            with JOB_LOCK:
                self._json(dict(JOB))
        elif path.startswith("/download/"):
            self._download(unquote(path[len("/download/"):]))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/capture":
            self._capture()
        elif path == "/api/export":
            self._start_export()
        else:
            self._json({"error": "not found"}, 404)

    def _capture(self) -> None:
        import zalo_capture as zc

        length = int(self.headers.get("Content-Length") or 0)
        text = self.rfile.read(length).decode("utf-8", errors="replace")
        query = parse_qs(urlparse(self.path).query)
        closer = (query.get("closer", [""])[0] or self.closer or "").strip()

        blocks = zc.split_orders(text)
        if not blocks:
            self._json({"found": 0, "saved": 0, "duplicates": 0, "other_month": 0})
            return
        try:
            with io.StringIO() as sink, redirect_stdout(sink):
                saved, duplicates, other = zc.handle_orders(
                    text, self.cfg, self.month, self.year, all_months=False, trim=True,
                    closer=closer)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        self._json({"found": len(blocks), "saved": saved,
                    "duplicates": duplicates, "other_month": other})

    def _start_export(self) -> None:
        with JOB_LOCK:
            if JOB["state"] == "running":
                self._json(dict(JOB))
                return
            JOB.update(state="running", step="Bắt đầu…", message="", file="", warning=False)
        threading.Thread(
            target=run_export,
            args=(self.cfg, self.month, self.year, self.closer),
            daemon=True,
        ).start()
        self._json({"state": "running"})

    def _download(self, name: str) -> None:
        # Resolve inside the output directory: a crafted name must not escape it.
        target = (self.cfg.output_dir / name).resolve()
        if not str(target).startswith(str(self.cfg.output_dir.resolve())) or not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        self._send(
            200, target.read_bytes(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            {"Content-Disposition": f'attachment; filename="{target.name}"'},
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true",
                    help="also serve on the local network so a phone can reach it")
    ap.add_argument("--month", type=int)
    ap.add_argument("--year", type=int)
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    cfg = Config.load()
    today = date.today()

    import yaml
    config_path = ROOT / "config" / "config.yaml"
    closer = ""
    if config_path.exists():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        closer = ((data.get("app") or {}).get("closer") or "").strip()

    Handler.cfg = cfg
    Handler.month = args.month or today.month
    Handler.year = args.year or today.year
    Handler.closer = closer

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), Handler)

    print(f"\n  Lavabo — mở trong trình duyệt:\n")
    print(f"    máy này:      http://127.0.0.1:{args.port}")
    if args.lan:
        print(f"    điện thoại:   http://{lan_ip()}:{args.port}")
        print(f"\n  (điện thoại phải dùng chung wifi; trang không có mật khẩu)")
    else:
        print(f"\n  (dùng --lan để mở được từ điện thoại)")
    print(f"\n  Ctrl+C để dừng.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
