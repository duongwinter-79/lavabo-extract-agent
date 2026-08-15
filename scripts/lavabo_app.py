#!/usr/bin/env python3
"""Lavabo — one-screen app: copy orders from Zalo, press 1, get an Excel file.

Everything the CLI does in five commands, reduced to the two things an operator
actually does. Capturing happens on its own in the background whenever something is
copied; the only decision left is when to produce the workbook.

First run asks for a Gemini API key and the name to record as Người chốt đơn, writes
them to .env and config/config.yaml, and does not ask again.

    python scripts/lavabo_app.py
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from lavabo.config import REPO_ROOT, Config  # noqa: E402

BOLD, DIM, GREEN, YELLOW, RED, CYAN, OFF = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[0m"
)

ENV_PATH = REPO_ROOT / ".env"
CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"
SCHEMA_PATH = REPO_ROOT / "config" / "schema.yaml"
SCHEMA_SOURCE = REPO_ROOT / "config" / "schema.senkahomes.yaml"

MONTHS_VI = "tháng"


def clear() -> None:
    print("\033[2J\033[H", end="")


def banner() -> None:
    print(f"{BOLD}{CYAN}╔════════════════════════════════════════════════════════╗{OFF}")
    print(f"{BOLD}{CYAN}║   LAVABO — Trích xuất đơn hàng Zalo sang Excel          ║{OFF}")
    print(f"{BOLD}{CYAN}╚════════════════════════════════════════════════════════╝{OFF}")


# --------------------------------------------------------------- first run

def ask(prompt: str, *, secret: bool = False, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        if secret:
            import getpass
            value = getpass.getpass(f"{prompt}{suffix}: ").strip()
        else:
            value = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    return value or default


def write_env(key: str) -> None:
    lines = []
    if ENV_PATH.exists():
        lines = [ln for ln in ENV_PATH.read_text(encoding="utf-8").splitlines()
                 if not ln.strip().startswith("GEMINI_API_KEY=")]
    lines.append(f"GEMINI_API_KEY={key}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_config(closer: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        "# Written by lavabo_app.py on first run. Safe to edit by hand.\n"
        "meta:\n  platforms: []\n\n"
        "zalo:\n"
        '  inbox_dir: "data/inbox/zalo"\n'
        '  timezone: "Asia/Ho_Chi_Minh"\n'
        "  own_names: []\n\n"
        "extract:\n"
        '  provider: "gemini"\n'
        '  model: "gemini-3.1-flash-lite"\n'
        "  concurrency: 1\n"
        "  temperature: 0.0\n\n"
        "app:\n"
        f'  closer: "{closer}"\n\n'
        'db_path: "data/staging.db"\n'
        'output_dir: "data/out"\n'
        'schema_path: "config/schema.yaml"\n',
        encoding="utf-8",
    )


def read_closer() -> str:
    if not CONFIG_PATH.exists():
        return ""
    import yaml
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return ((data.get("app") or {}).get("closer") or "").strip()


def first_run_setup() -> bool:
    """Collect the two things the app cannot infer. Returns False if the user quits."""
    from lavabo.extract.base import extractor_class

    if not SCHEMA_PATH.exists() and SCHEMA_SOURCE.exists():
        SCHEMA_PATH.write_text(SCHEMA_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")

    needs_key = extractor_class("gemini").key_problem() is not None
    needs_config = not CONFIG_PATH.exists()

    if not needs_key and not needs_config:
        return True

    clear()
    banner()
    print(f"\n{BOLD}Cài đặt lần đầu{OFF}  (chỉ hỏi một lần)\n")

    if needs_key:
        print("Cần một Gemini API key — miễn phí, không cần thẻ.")
        print(f"{DIM}Lấy tại: https://aistudio.google.com/apikey{OFF}\n")
        while True:
            key = ask("Dán Gemini API key vào đây", secret=True)
            if not key:
                print(f"{RED}Chưa có key thì không chạy được. Thoát.{OFF}")
                return False
            os.environ["GEMINI_API_KEY"] = key
            problem = extractor_class("gemini").key_problem()
            if problem:
                print(f"{RED}  {problem}{OFF}\n")
                continue
            print(f"{DIM}  Đang kiểm tra với Google...{OFF}")
            ok, detail = extractor_class("gemini").verify_api_key()
            if not ok:
                print(f"{RED}  {detail}{OFF}\n")
                continue
            write_env(key)
            print(f"{GREEN}  Key hợp lệ, đã lưu vào .env{OFF}\n")
            break

    if needs_config:
        closer = ask("Tên người chốt đơn (ghi vào cột 'Người chốt đơn')", default="")
        write_config(closer)
        print(f"{GREEN}  Đã lưu cấu hình{OFF}")

    print(f"\n{DIM}Xong. Nhấn Enter để bắt đầu.{OFF}")
    ask("")
    return True


# ------------------------------------------------------------ background capture

class Capturer(threading.Thread):
    """Watches the clipboard and files any orders it sees, without blocking the menu."""

    daemon = True

    def __init__(self, cfg: Config, month: int, year: int) -> None:
        super().__init__()
        self.cfg, self.month, self.year = cfg, month, year
        self.saved = 0
        self.error: str | None = None
        self._lines: list[str] = []

    def run(self) -> None:
        import zalo_capture as zc

        try:
            read_clipboard = zc.make_clipboard_reader()
        except RuntimeError as exc:
            self.error = str(exc)
            return

        last = read_clipboard() or ""
        while True:
            time.sleep(0.5)
            try:
                current = read_clipboard() or ""
            except Exception:
                continue
            if current == last or not current:
                continue
            last = current

            if not any(zc.ORDER_HEADER.match(ln.strip()) for ln in current.splitlines()):
                continue
            try:
                saved, _, _ = zc.handle_orders(current, self.cfg, self.month, self.year,
                                               all_months=False, trim=True)
            except Exception as exc:                      # keep the app alive
                self._lines.append(f"{RED}lỗi khi lưu: {exc}{OFF}")
                continue
            if saved:
                self.saved += saved

    def drain(self) -> list[str]:
        lines, self._lines = self._lines, []
        return lines


# ------------------------------------------------------------------ the one action

def export(cfg: Config, month: int, year: int, closer: str) -> None:
    from lavabo.cli import cmd_extract, cmd_ingest, cmd_load

    class A:                                              # stand-in for parsed argv
        pass

    print(f"\n{BOLD}[1/3] Đọc các đơn đã lưu…{OFF}")
    a = A(); a.source = "zalo"; a.full = False
    # The CLI prints counters and a JSON dump here, which is noise for someone who
    # only wants the spreadsheet. The useful number is printed below instead.
    with io.StringIO() as sink, redirect_stdout(sink):
        cmd_ingest(a, cfg)
    from lavabo.store import Store
    with Store(cfg.db_path) as store:
        total = store.stats()["conversations"]
    print(f"  {total} đơn sẵn sàng")

    print(f"\n{BOLD}[2/3] Trích xuất bằng AI…{OFF}  {DIM}(bản miễn phí chạy chậm, "
          f"gặp giới hạn sẽ tự chờ){OFF}")
    a = A(); a.source = "zalo"; a.limit = None; a.force = False
    a.dry_run = False; a.strict = False
    a.provider = a.model = a.api_key = None
    code = cmd_extract(a, cfg)
    if code:
        print(f"{YELLOW}Một số đơn trích xuất chưa xong — vẫn xuất file với phần đã có.{OFF}")

    out = cfg.output_dir / f"donhang-{year}{month:02d}.xlsx"
    print(f"\n{BOLD}[3/3] Ghi file Excel…{OFF}")
    a = A(); a.out = str(out); a.layout = "senkahomes"
    a.sheet = None; a.year = year; a.status = "New"; a.closer = closer or None
    a.provider = a.model = a.api_key = None
    cmd_load(a, cfg)

    print(f"\n{GREEN}{BOLD}Xong.{OFF}  File: {BOLD}{out}{OFF}")
    print(f"{DIM}Mở thư mục: {out.parent}{OFF}")


# -------------------------------------------------------------------------- main

def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        pass

    if not first_run_setup():
        return 1

    cfg = Config.load()
    closer = read_closer()
    today = date.today()
    month, year = today.month, today.year

    capturer = Capturer(cfg, month, year)
    capturer.start()
    time.sleep(0.6)                                       # let it report a clipboard error

    while True:
        clear()
        banner()

        inbox = cfg.zalo.inbox_dir
        on_disk = len(list(inbox.glob("*.txt"))) if inbox.exists() else 0

        if capturer.error:
            print(f"\n{RED}Không đọc được clipboard: {capturer.error}{OFF}")
            print(f"{DIM}Cài đặt: pip install pyperclip{OFF}")
        else:
            print(f"\n{GREEN}● Đang theo dõi clipboard{OFF} — chỉ nhận đơn "
                  f"{MONTHS_VI} {month:02d}/{year}")
            print(f"{DIM}  Mở Zalo, bôi đen đoạn chat rồi Copy. Chương trình tự lưu.{OFF}")

        print(f"\n  Đã lưu: {BOLD}{on_disk}{OFF} đơn"
              + (f"   {GREEN}(+{capturer.saved} phiên này){OFF}" if capturer.saved else ""))

        for line in capturer.drain():
            print("  " + line)

        print(f"\n{BOLD}  [1]{OFF}  Xuất file Excel")
        print(f"{DIM}  [r]  Làm mới    [q]  Thoát{OFF}\n")

        try:
            choice = input("Chọn: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice in ("q", "quit", "thoat", "thoát"):
            return 0
        if choice in ("", "r"):
            continue
        if choice == "1":
            if not on_disk:
                print(f"\n{YELLOW}Chưa có đơn nào. Copy đoạn chat từ Zalo trước đã.{OFF}")
            else:
                try:
                    export(cfg, month, year, closer)
                except Exception as exc:
                    print(f"\n{RED}Lỗi: {type(exc).__name__}: {exc}{OFF}")
            input(f"\n{DIM}Nhấn Enter để quay lại…{OFF}")


if __name__ == "__main__":
    raise SystemExit(main())
