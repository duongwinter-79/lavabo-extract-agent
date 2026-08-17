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

import os
import sys
import threading
import time
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


def read_app_setting(key: str) -> str:
    """Read config.yaml's `app:` block, which Config.load ignores — it belongs to
    the front ends, not the pipeline."""
    if not CONFIG_PATH.exists():
        return ""
    import yaml
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return str((data.get("app") or {}).get(key) or "").strip()


def write_app_setting(key: str, value: str) -> None:
    import yaml
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    data.setdefault("app", {})[key] = value
    CONFIG_PATH.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")


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

    def __init__(self, cfg: Config, month: int, year: int, closer: str) -> None:
        super().__init__()
        self.cfg, self.month, self.year = cfg, month, year
        self.saved = 0
        self.error: str | None = None
        self._lines: list[str] = []
        # Read at each capture, not at start: the operator can change who is chốt
        # partway through a session and the next paste must follow the new answer.
        self.closer = closer

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
                saved, _, _, swaps = zc.handle_orders(current, self.cfg, self.month, self.year,
                                                      all_months=False, trim=True,
                                                      closer=self.closer)
            except Exception as exc:                      # keep the app alive
                self._lines.append(f"{RED}lỗi khi lưu: {exc}{OFF}")
                continue
            if saved:
                self.saved += saved
            for note in swaps:
                self._lines.append(f"{YELLOW}ngày/tháng bị đảo: {note}{OFF}")

    def drain(self) -> list[str]:
        lines, self._lines = self._lines, []
        return lines


# ------------------------------------------------------------- who chốt these orders

def choose_closer(cfg: Config, current: str) -> str:
    """Pick the name recorded in Người chốt đơn for orders captured from now on.

    Offered as a numbered list of names already used, because that column feeds the
    sheet's =SUMIF($L:$L,"Trà My",$G:$G). Re-typing it is how "Trà My" becomes "Tra My"
    and quietly stops matching, moving that revenue out of the total.
    """
    from lavabo import closers

    names = closers.known_names(cfg.zalo.inbox_dir)
    for name in ([current] if current else []):
        if name not in names:
            names.insert(0, name)

    print(f"\n{BOLD}Ai là người chốt các đơn sắp copy?{OFF}")
    for i, name in enumerate(names, start=1):
        mark = f"  {GREEN}← đang dùng{OFF}" if name == current else ""
        print(f"  {BOLD}[{i}]{OFF}  {name}{mark}")
    print(f"  {BOLD}[+]{OFF}  Tên khác…")

    choice = ask("\nChọn", default="")
    if choice == "+":
        typed = ask("Tên người chốt đơn", default="")
        return typed or current
    if choice.isdigit() and 1 <= int(choice) <= len(names):
        return names[int(choice) - 1]
    return current


def choose_month(month: int, year: int) -> tuple[int, int]:
    """Pick which month is being captured — normally this one, but the shop closes a
    month a few days late, so the first days of September still see August orders."""
    options = []
    m, y = month, year
    for _ in range(6):
        options.append((m, y))
        m -= 1
        if m == 0:
            m, y = 12, y - 1

    print(f"\n{BOLD}Đang nhập đơn của tháng nào?{OFF}")
    for i, (mm, yy) in enumerate(options, start=1):
        mark = f"  {GREEN}← đang dùng{OFF}" if (mm, yy) == (month, year) else ""
        print(f"  {BOLD}[{i}]{OFF}  {MONTHS_VI} {mm:02d}/{yy}{mark}")

    choice = ask("\nChọn", default="")
    if choice.isdigit() and 1 <= int(choice) <= len(options):
        return options[int(choice) - 1]
    return month, year


# ------------------------------------------------------------------ the one action

def _step(text: str) -> None:
    print(f"\n{BOLD}{text}{OFF}")


def export(cfg: Config, month: int, year: int, closer: str) -> None:
    from lavabo import pipeline

    total, failed = pipeline.ingest_and_extract(cfg, _step)
    print(f"  {total} đơn")
    if failed:
        print(f"{YELLOW}  {failed} đơn trích xuất lỗi — vẫn xuất file với phần đã có.{OFF}")

    out = pipeline.write_workbook(cfg, month, year, closer, _step)
    print(f"\n{GREEN}{BOLD}Xong.{OFF}  File: {BOLD}{out}{OFF}")
    print(f"{DIM}Mở thư mục: {out.parent}{OFF}")


def append(cfg: Config, month: int, year: int, closer: str) -> None:
    """Add the month's orders into the shop's own workbook, in place."""
    from lavabo import pipeline

    target = Path(read_app_setting("workbook"))
    if not target or not target.exists():
        if target:
            print(f"\n{YELLOW}Không tìm thấy: {target}{OFF}")
        print(f"\n{DIM}Kéo file quản lý vào đây rồi Enter, hoặc dán đường dẫn.{OFF}")
        typed = ask("File quản lý (.xlsx)", default="")
        # A path dragged into a terminal arrives quoted and space-escaped.
        typed = typed.strip().strip("'\"").replace("\\ ", " ")
        if not typed:
            return
        target = Path(typed).expanduser()
        if not target.exists():
            print(f"{RED}Không tìm thấy file: {target}{OFF}")
            return
        write_app_setting("workbook", str(target))

    total, failed = pipeline.ingest_and_extract(cfg, _step)
    print(f"  {total} đơn")
    if failed:
        print(f"{YELLOW}  {failed} đơn trích xuất lỗi{OFF}")

    summary = pipeline.append_to_workbook(cfg, target, month, year, closer, progress=_step)

    if summary["collision"]:
        rows = ", ".join(str(r) for r in summary["collision"])
        print(f"\n{RED}Không ghi được vào sheet {summary['sheet']}.{OFF}")
        print(f"  Dòng {rows} đang có nội dung khác (thường là bảng tổng kết).")
        print("  Chuyển bảng đó xuống dưới rồi thử lại.")
        return

    print(f"\n{GREEN}{BOLD}Xong.{OFF}  Sheet {BOLD}{summary['sheet']}{OFF}"
          + ("  (mới tạo)" if summary["created_sheet"] else ""))
    print(f"  Đã thêm {BOLD}{summary['added']}{OFF} đơn"
          + (f", từ dòng {summary['start_row']}" if summary["start_row"] else ""))
    if summary["already_present"]:
        print(f"  {summary['already_present']} đơn đã có sẵn — bỏ qua")
    if summary["unextracted"]:
        print(f"{YELLOW}  {summary['unextracted']} đơn chỉ có ngày và tên khách "
              f"(chưa trích xuất được){OFF}")
    if summary["backup"]:
        print(f"{DIM}  Bản sao lưu: {Path(summary['backup']).name}{OFF}")


# -------------------------------------------------------------------------- main

def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        pass

    if not first_run_setup():
        return 1

    try:
        cfg = Config.load()
    except ValueError as exc:
        print(f"\n{RED}{exc}{OFF}\n")
        input("Nhấn Enter để thoát…")
        return 1
    closer = read_app_setting("closer")
    today = date.today()
    month, year = today.month, today.year

    capturer = Capturer(cfg, month, year, closer)
    capturer.start()
    time.sleep(0.6)                                       # let it report a clipboard error

    while True:
        clear()
        banner()

        inbox = cfg.zalo.inbox_dir
        on_disk = len(list(inbox.glob("*.txt"))) if inbox.exists() else 0
        workbook = read_app_setting("workbook")

        if capturer.error:
            print(f"\n{RED}Không đọc được clipboard: {capturer.error}{OFF}")
            print(f"{DIM}Cài đặt: pip install pyperclip{OFF}")
        else:
            print(f"\n{GREEN}● Đang theo dõi clipboard{OFF} — chỉ nhận đơn "
                  f"{MONTHS_VI} {month:02d}/{year}")
            print(f"{DIM}  Mở Zalo, bôi đen đoạn chat rồi Copy. Chương trình tự lưu.{OFF}")

        print(f"\n  Đã lưu: {BOLD}{on_disk}{OFF} đơn"
              + (f"   {GREEN}(+{capturer.saved} phiên này){OFF}" if capturer.saved else ""))
        print("  Người chốt đơn: "
              + (f"{BOLD}{capturer.closer}{OFF}" if capturer.closer
                 else f"{YELLOW}chưa chọn{OFF}"))

        if inbox.exists():
            import zalo_capture as zc
            # A day with đơn 2,3,4 but no đơn 1 almost certainly has an order still
            # sitting further up in Zalo -- Ctrl+A/Ctrl+C did not reach that far.
            for gap in zc.order_gaps(inbox, month):
                print(f"  {YELLOW}⚠ có thể sót đơn — {gap}{OFF}")

        for line in capturer.drain():
            print("  " + line)

        print(f"\n{BOLD}  [1]{OFF}  Xuất file Excel mới")
        print(f"{BOLD}  [2]{OFF}  Thêm vào file quản lý"
              + (f"   {DIM}({Path(workbook).name}){OFF}" if workbook else ""))
        print(f"{BOLD}  [3]{OFF}  Đổi người chốt đơn")
        print(f"{BOLD}  [4]{OFF}  Đổi tháng")
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
        if choice == "3":
            capturer.closer = choose_closer(cfg, capturer.closer)
            continue
        if choice == "4":
            month, year = choose_month(month, year)
            capturer.month, capturer.year = month, year
            continue
        if choice in ("1", "2"):
            if not on_disk:
                print(f"\n{YELLOW}Chưa có đơn nào. Copy đoạn chat từ Zalo trước đã.{OFF}")
            else:
                action = export if choice == "1" else append
                try:
                    action(cfg, month, year, capturer.closer)
                except RuntimeError as exc:       # ours, already phrased for the operator
                    print(f"\n{YELLOW}{exc}{OFF}")
                except Exception as exc:
                    print(f"\n{RED}Lỗi: {type(exc).__name__}: {exc}{OFF}")
            input(f"\n{DIM}Nhấn Enter để quay lại…{OFF}")


if __name__ == "__main__":
    raise SystemExit(main())
