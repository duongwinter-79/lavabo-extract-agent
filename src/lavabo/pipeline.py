"""The steps behind the two buttons, shared by the terminal app and the web page.

Both front ends do the same three things — read the saved orders, extract them, write
them somewhere — and both need to say where they are while it happens. Keeping that here
means a fix to the sequence reaches both, and the CLI keeps its own thin wrappers around
the same calls.

`progress` is how a front end reports a step; it gets a short Vietnamese phrase, since
that is what both surfaces show.
"""

from __future__ import annotations

import io
import logging
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .models import Conversation
from .store import Store

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


class _Args:
    """Stand-in for parsed argv — the CLI commands take an argparse namespace.

    Unset flags read as None, the way argparse itself behaves: it defines every declared
    argument whether or not it was given. Without this, forgetting one field here raises
    AttributeError from inside a CLI command, which is how `write_workbook` shipped
    without passing `month` — a crash at the end of an export, far from the cause.
    """

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, name: str) -> None:
        return None


def _silent(_: str) -> None:
    pass


def ingest_and_extract(cfg: Config, progress: Progress = _silent) -> tuple[int, int]:
    """Read the inbox and run the model over anything not already extracted.

    Returns (orders stored, extractions that failed). The CLI's own output is captured
    rather than printed: it is a counter dump and a JSON blob, which is noise to someone
    who only wants the spreadsheet.
    """
    from .cli import cmd_extract, cmd_ingest

    progress("Đang đọc các đơn đã lưu…")
    with io.StringIO() as sink, redirect_stdout(sink):
        cmd_ingest(_Args(source="zalo", full=False), cfg)

    with Store(cfg.db_path) as store:
        total = store.stats()["conversations"]
    if not total:
        raise RuntimeError("Chưa có đơn nào để xuất.")

    progress(f"Đang trích xuất {total} đơn bằng AI…")
    with io.StringIO() as sink, redirect_stdout(sink):
        cmd_extract(_Args(source="zalo", limit=None, force=False,
                          dry_run=False, strict=False), cfg)
        failed = sink.getvalue().count("  FAIL ")
    return total, failed


def write_workbook(cfg: Config, month: int, year: int, closer: str | None,
                   progress: Progress = _silent) -> Path:
    """Produce a fresh month workbook and return where it landed."""
    from .cli import cmd_load

    progress("Đang ghi file Excel…")
    out = cfg.output_dir / f"donhang-{year}{month:02d}.xlsx"
    with io.StringIO() as sink, redirect_stdout(sink):
        # month and year both matter: the store holds every month ever captured, and
        # without the filter one file would contain all of them under a sheet named
        # after whichever order came out first.
        cmd_load(_Args(out=str(out), layout="senkahomes", sheet=None,
                       month=month, year=year,
                       status="New", closer=closer or None), cfg)
    return out


def stored_for_month(cfg: Config, month: int, year: int
                     ) -> tuple[list[Conversation], dict[str, Any]]:
    """Orders belonging to one month, with whatever extraction is cached for each.

    The year test tolerates a missing order_year: the note usually writes "15/8" with no
    year, and those belong to the year being asked about.
    """
    from .extract.prompt import PROMPT_VERSION

    schema = cfg.load_schema()
    with Store(cfg.db_path) as store:
        conversations = [c for c in store.conversations()
                         if c.raw.get("order_month") == month
                         and (not year or (c.raw.get("order_year") or year) == year)]
        results = {}
        for conv in conversations:
            hit = store.cached_extraction(
                conv, schema_version=schema.version, schema_hash=schema.fingerprint(),
                prompt_version=PROMPT_VERSION, model=cfg.extract.model,
            )
            if hit:
                results[conv.conversation_id] = hit
    return conversations, results


def append_to_workbook(cfg: Config, workbook: Path, month: int, year: int,
                       closer: str | None, *, dry_run: bool = False,
                       progress: Progress = _silent) -> dict[str, Any]:
    """Add the month's orders into the shop's own workbook, after a backup.

    Unextracted orders are added rather than refused: the front ends have no --force to
    offer, and a row carrying only date and customer is still worth having — the operator
    can see it and fill the rest, which is strictly better than the order going missing.
    The count comes back in the summary so the caller can say so.
    """
    from .load.append import append_orders

    conversations, results = stored_for_month(cfg, month, year)
    if not conversations:
        raise RuntimeError(f"Không có đơn nào của tháng {month:02d}/{year}.")

    progress(f"Đang thêm {len(conversations)} đơn vào {workbook.name}…")
    summary = append_orders(workbook, conversations, results,
                            month=month, year=year, status="New", closer=closer,
                            dry_run=dry_run)
    summary["unextracted"] = len(conversations) - len(results)
    return summary
