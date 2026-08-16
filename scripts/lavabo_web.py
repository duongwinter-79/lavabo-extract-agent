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
    <span style="color:var(--muted)">đơn đã lưu</span></div>
  <label class="lbl" style="margin-top:14px" for="period">Tháng đang nhập</label>
  <select id="period"></select>
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
  <button id="export" class="ghost">Xuất file Excel mới</button>
  <button id="append" class="ghost">Thêm vào file quản lý</button>
  <div class="hint" id="wbhint"></div>
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

function drawPeriods(periods, current) {
  const sel = $('period');
  if (sel.dataset.touched === '1' && sel.value === current) return;
  sel.innerHTML = '';
  for (const p of periods) {
    const o = document.createElement('option'); o.value = p; o.textContent = 'tháng ' + p;
    sel.appendChild(o);
  }
  sel.value = current;
}

$('period').onchange = async () => {
  const sel = $('period'); sel.dataset.touched = '1';
  await fetch('/api/period?value=' + encodeURIComponent(sel.value), {method:'POST'});
  refresh();
};

async function refresh() {
  try {
    const r = await fetch('/api/status'); const s = await r.json();
    $('count').textContent = s.orders;
    drawPeriods(s.periods || [s.period], s.period);
    $('append').disabled = !s.workbook;
    $('wbhint').textContent = s.workbook
      ? 'File quản lý: ' + s.workbook + ' — luôn sao lưu trước khi ghi.'
      : 'Chưa đặt file quản lý. Mở config/config.yaml và thêm app.workbook: đường dẫn tới file .xlsx.';
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

async function runJob(mode) {
  $('export').disabled = $('append').disabled = true; $('dl').innerHTML = '';
  show($('m2'),'ok','Đang xử lý… bản Gemini miễn phí chạy chậm, vui lòng đợi.');
  try {
    await fetch('/api/export?mode=' + mode, {method:'POST'});
    const poll = setInterval(async () => {
      const r = await fetch('/api/export/status'); const s = await r.json();
      if (s.state === 'running') { show($('m2'),'ok', s.step || 'Đang xử lý…'); return; }
      clearInterval(poll); $('export').disabled = false; refresh();
      if (s.state === 'error') { show($('m2'),'err', s.message); return; }
      show($('m2'), s.warning ? 'warn' : 'ok', s.message);
      // Appending writes into the shop's own workbook in place; there is nothing to
      // download, and offering a copy would invite editing the wrong file.
      if (s.file) $('dl').innerHTML =
        `<a class="dl" href="/download/${encodeURIComponent(s.file)}">Tải file Excel</a>`;
    }, 1500);
  } catch (e) {
    show($('m2'),'err','Lỗi: ' + e); $('export').disabled = false; refresh();
  }
}

$('export').onclick = () => runJob('new');
$('append').onclick = () => {
  if (!confirm('Thêm các đơn của tháng này vào file quản lý?\n\nFile sẽ được sao lưu trước khi ghi.')) return;
  runJob('append');
};

refresh(); setInterval(refresh, 5000);
</script>
</div></body></html>
"""

JOB: dict = {"state": "idle", "step": "", "message": "", "file": "", "warning": False}
JOB_LOCK = threading.Lock()


def recent_periods(month: int, year: int, count: int = 6) -> list[str]:
    """This month and the five before it. The shop closes a month a few days late, so
    early September still sees August orders arriving."""
    out = []
    m, y = month, year
    for _ in range(count):
        out.append(f"{m:02d}/{y}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def lan_ip() -> str:
    """Best-effort LAN address, so the phone gets a URL that actually resolves."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))          # no packet is sent; picks the route
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def run_export(cfg: Config, month: int, year: int, closer: str,
               *, mode: str = "new", workbook: Path | None = None) -> None:
    """Extract everything captured, then either write a fresh month file or add the
    month's orders into the shop's own workbook."""
    from lavabo import pipeline

    def step(text: str) -> None:
        with JOB_LOCK:
            JOB["step"] = text

    try:
        total, failed = pipeline.ingest_and_extract(cfg, step)

        if mode == "append":
            if not workbook:
                raise RuntimeError("Chưa đặt file quản lý (app.workbook trong config.yaml).")
            summary = pipeline.append_to_workbook(cfg, workbook, month, year, closer,
                                                  progress=step)
            if summary["collision"]:
                rows = ", ".join(str(r) for r in summary["collision"])
                raise RuntimeError(
                    f"Sheet {summary['sheet']}: dòng {rows} đang có nội dung khác "
                    "(thường là bảng tổng kết). Chuyển bảng đó xuống rồi thử lại."
                )
            bits = [f"Xong. Đã thêm {summary['added']} đơn vào sheet {summary['sheet']}."]
            if summary["already_present"]:
                bits.append(f"{summary['already_present']} đơn đã có sẵn.")
            if summary["unextracted"]:
                bits.append(f"{summary['unextracted']} đơn chỉ có ngày và tên khách.")
            if summary["backup"]:
                bits.append(f"Sao lưu: {Path(summary['backup']).name}")
            with JOB_LOCK:
                JOB.update(state="done", step="", message=" ".join(bits), file="",
                           warning=bool(summary["unextracted"]))
            return

        out = pipeline.write_workbook(cfg, month, year, closer, step)
        message = f"Xong. {total} đơn."
        if failed:
            message += f" {failed} đơn trích xuất lỗi — các cột AI sẽ trống."
        with JOB_LOCK:
            JOB.update(state="done", step="", message=message,
                       file=out.name, warning=bool(failed))
    except Exception as exc:
        # RuntimeError is ours and already phrased for the operator; anything else is a
        # surprise, and its type is the useful half of the message.
        detail = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
        with JOB_LOCK:
            JOB.update(state="error", step="", message=detail, file="", warning=True)


class Handler(BaseHTTPRequestHandler):
    cfg: Config
    month: int
    year: int
    closer: str
    workbook: Path | None = None

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
                        "periods": recent_periods(self.month, self.year),
                        "workbook": self.workbook.name if self.workbook else "",
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
        elif path == "/api/period":
            self._set_period()
        else:
            self._json({"error": "not found"}, 404)

    def _set_period(self) -> None:
        """Change which month new pastes are filtered to.

        Set on the class, not the instance: every request gets a fresh handler, and the
        phone changing the month must apply to the next paste from the laptop too.
        """
        raw = parse_qs(urlparse(self.path).query).get("value", [""])[0]
        try:
            month, year = (int(part) for part in raw.split("/", 1))
            if not 1 <= month <= 12 or not 2000 <= year <= 2100:
                raise ValueError(raw)
        except ValueError:
            self._json({"error": f"tháng không hợp lệ: {raw!r}"}, 400)
            return
        Handler.month, Handler.year = month, year
        self._json({"period": f"{month:02d}/{year}"})

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
        mode = parse_qs(urlparse(self.path).query).get("mode", ["new"])[0]
        if mode not in ("new", "append"):
            self._json({"error": f"unknown mode {mode!r}"}, 400)
            return
        if mode == "append" and not self.workbook:
            self._json({"error": "Chưa đặt file quản lý trong config.yaml (app.workbook)."},
                       400)
            return

        with JOB_LOCK:
            if JOB["state"] == "running":
                self._json(dict(JOB))
                return
            JOB.update(state="running", step="Bắt đầu…", message="", file="", warning=False)
        threading.Thread(
            target=run_export,
            args=(self.cfg, self.month, self.year, self.closer),
            kwargs={"mode": mode, "workbook": self.workbook},
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
    closer, workbook = "", ""
    if config_path.exists():
        app = (yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}).get("app") or {}
        closer = str(app.get("closer") or "").strip()
        workbook = str(app.get("workbook") or "").strip()

    target = Path(workbook).expanduser() if workbook else None
    if target and not target.exists():
        print(f"\n  ! app.workbook không tồn tại: {target}")
        print("    Nút 'Thêm vào file quản lý' sẽ bị tắt cho đến khi sửa lại.")
        target = None

    Handler.cfg = cfg
    Handler.month = args.month or today.month
    Handler.year = args.year or today.year
    Handler.closer = closer
    Handler.workbook = target

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
