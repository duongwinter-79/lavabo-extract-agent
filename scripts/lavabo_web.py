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

PAGE_PATH = ROOT / "scripts" / "web" / "index.html"

JOB: dict = {"state": "idle", "step": "", "message": "", "file": "", "warning": False}
JOB_LOCK = threading.Lock()
# One writer at a time for config.yaml and .env: two tabs saving at once would
# otherwise interleave a read-modify-write and lose one of them.
SETTINGS_LOCK = threading.Lock()


def allowed_years() -> list[int]:
    """Last year and this one.

    Wide enough to reopen a month from the previous year — closing December in January
    crosses the boundary — and narrow enough that a mistyped year cannot quietly file
    orders under 2019, where nobody would look for them.
    """
    this_year = date.today().year
    return [this_year - 1, this_year]


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
            # Read per request rather than cached at startup: editing the page and
            # refreshing is then enough, with no server restart in the loop.
            try:
                body = PAGE_PATH.read_bytes()
            except OSError as exc:
                self._json({"error": f"không đọc được {PAGE_PATH.name}: {exc}"}, 500)
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/api/settings":
            from lavabo import settings

            self._json(settings.read_settings())
        elif path == "/api/status":
            from lavabo import closers
            import zalo_capture as zc

            names = closers.known_names(self.cfg.zalo.inbox_dir)
            if self.closer and self.closer not in names:
                names.append(self.closer)          # the config default, as a fallback
            self._json({"orders": self._orders_on_disk(),
                        "month": self.month, "year": self.year,
                        "period": f"{self.month:02d}/{self.year}",
                        "years": allowed_years(),
                        "workbook": self.workbook.name if self.workbook else "",
                        "closers": names,
                        "gaps": zc.order_gaps(self.cfg.zalo.inbox_dir, self.month)})
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
        elif path == "/api/settings":
            self._save_settings()
        elif path == "/api/settings/verify":
            self._verify_key()
        elif path == "/api/settings/models":
            self._list_models()
        else:
            self._json({"error": "not found"}, 404)

    # ---------------------------------------------------------------- settings

    def _body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_settings(self) -> None:
        from lavabo import settings

        body = self._body_json()
        try:
            with SETTINGS_LOCK:
                warnings = settings.write_settings(
                    provider=str(body.get("provider") or ""),
                    model=str(body.get("model") or ""),
                    api_key=str(body.get("api_key") or ""),
                    workbook=str(body.get("workbook") or ""),
                    closer=str(body.get("closer") or ""),
                )
                self._reload()
        except Exception as exc:
            self._json({"error": str(exc) or type(exc).__name__}, 400)
            return
        self._json({"warnings": warnings, "keys": settings.all_key_status()})

    def _reload(self) -> None:
        """Apply the saved settings to the running server.

        A full process restart would drop the connection answering this request and, on
        a phone, look like the app crashing. Everything the screen can change is read
        from Handler.cfg or the environment at use time, so re-reading both is enough.
        """
        from lavabo import settings

        settings.load_env_into_process()
        Handler.cfg = Config.load()
        app = settings.read_settings()
        Handler.closer = app["closer"]
        target = Path(app["workbook"]).expanduser() if app["workbook"] else None
        Handler.workbook = target if (target and target.exists()) else None

    def _verify_key(self) -> None:
        from lavabo import settings

        body = self._body_json()
        try:
            ok, message = settings.verify_key(str(body.get("provider") or ""),
                                              str(body.get("api_key") or ""))
        except Exception as exc:
            self._json({"error": str(exc) or type(exc).__name__}, 400)
            return
        self._json({"ok": ok, "message": message})

    def _list_models(self) -> None:
        from lavabo import settings

        body = self._body_json()
        try:
            models = settings.list_models(str(body.get("provider") or ""),
                                          str(body.get("api_key") or ""))
        except Exception as exc:
            self._json({"error": str(exc) or type(exc).__name__}, 400)
            return
        self._json({"models": models})

    def _set_period(self) -> None:
        """Change which month new pastes are filtered to.

        Set on the class, not the instance: every request gets a fresh handler, and the
        phone changing the month must apply to the next paste from the laptop too.
        """
        raw = parse_qs(urlparse(self.path).query).get("value", [""])[0]
        try:
            month, year = (int(part) for part in raw.split("/", 1))
        except ValueError:
            self._json({"error": f"tháng không hợp lệ: {raw!r}"}, 400)
            return
        if not 1 <= month <= 12:
            self._json({"error": f"tháng phải từ 1 đến 12, không phải {month}"}, 400)
            return
        years = allowed_years()
        if year not in years:
            # Checked here as well as in the page: the year decides which sheet the
            # orders land in, and a stale tab or a hand-typed URL must not pick one
            # the operator never chose.
            self._json({"error": f"chỉ nhận năm {years[0]} hoặc {years[1]}"}, 400)
            return
        Handler.month, Handler.year = month, year
        self._json({"month": month, "year": year, "period": f"{month:02d}/{year}"})

    def _capture(self) -> None:
        import zalo_capture as zc

        length = int(self.headers.get("Content-Length") or 0)
        text = self.rfile.read(length).decode("utf-8", errors="replace")
        query = parse_qs(urlparse(self.path).query)
        closer = (query.get("closer", [""])[0] or self.closer or "").strip()

        # target_month resolves a hand-typed date like "8/3" against the month being
        # captured (see resolve_swapped_date) -- must match what handle_orders uses
        # below, or an order it swaps into this month would still read as "other month"
        # in the stats and the date-span check computed from these blocks.
        blocks = zc.split_orders(text, target_month=self.month)
        if not blocks:
            self._json({"found": 0, "saved": 0, "duplicates": 0, "other_month": 0})
            return
        try:
            with io.StringIO() as sink, redirect_stdout(sink):
                result = zc.handle_orders(
                    text, self.cfg, self.month, self.year, all_months=False, trim=True,
                    closer=closer)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        # Which days of the *selected* month this paste covered, so a clipboard that
        # only carried part of it is visible immediately — Zalo renders long
        # conversations lazily, so a copy can contain far less than what was selected.
        # Measured over the in-month orders alone: a paste of the whole group chat spans
        # every month in it, and reporting that span said nothing about the month being
        # captured.
        days = sorted(b.day for b in blocks if zc.in_month(b, self.month, self.year))
        self._json({"found": len(blocks), "saved": result.saved,
                    "duplicates": result.duplicates, "other_month": result.out_of_month,
                    "span_days": [days[0], days[-1]] if days else [],
                    "date_swaps": result.date_swaps,
                    # Without these the counts do not add up to `found` on a first
                    # paste, and the orders needing a human decision are never mentioned.
                    "versions": result.versions,
                    "updates": result.updates})

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

    try:
        cfg = Config.load()
    except ValueError as exc:
        print(f"\n{exc}\n")
        input("Nhấn Enter để thoát…")
        return 1
    today = date.today()

    from lavabo import settings

    app = settings.read_settings()
    closer, workbook = app["closer"], app["workbook"]

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
