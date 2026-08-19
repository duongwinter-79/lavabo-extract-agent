"""Command line entry point.

    lavabo check                       preflight credentials and paths
    lavabo ingest --source meta|zalo   pull/parse into the SQLite staging db
    lavabo extract [--limit N] [--dry-run] [--provider gemini --api-key AIza...]
    lavabo load --out report.xlsx      write a separate workbook
    lavabo append --into yours.xlsx   add into the shop's own workbook
    lavabo run --out report.xlsx       ingest + extract + load
    lavabo inspect                     show stored extractions, including failures
    lavabo config                      show effective settings + drift from the example
    lavabo models                      list models this key can use
    lavabo verify
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .models import Source
from .store import Store

log = logging.getLogger("lavabo")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def add_llm_args(parser: argparse.ArgumentParser) -> None:
    """Provider/model/key overrides, shared by the commands that talk to an LLM."""
    parser.add_argument("--provider", choices=["anthropic", "gemini"],
                        help="override extract.provider from config.yaml")
    parser.add_argument("--model", help="override extract.model from config.yaml")
    parser.add_argument("--api-key", metavar="KEY",
                        help="API key for the selected provider, instead of reading it "
                             "from .env. Note: this lands in your shell history and is "
                             "visible in the process list, so .env is safer for repeat use")


def _apply_llm_overrides(args, cfg: Config) -> None:
    """Fold --provider/--model/--api-key into the config before anything reads it."""
    if getattr(args, "provider", None):
        cfg.extract.provider = args.provider
    if getattr(args, "model", None):
        cfg.extract.model = args.model

    key = getattr(args, "api_key", None)
    if not key:
        return

    # The SDKs read their key from the environment, so the override is applied there
    # rather than threaded through every call site. Set the primary variable for
    # whichever provider is now selected.
    from .extract.base import extractor_class

    try:
        variables = extractor_class(cfg.extract.provider).API_KEY_VARS
    except ValueError:
        return
    if variables:
        os.environ[variables[0]] = key.strip()


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


# --------------------------------------------------------------------- ingest

def _meta_connectors(cfg: Config, store: Store, full: bool):
    from .connectors.meta_graph import MetaGraphConnector

    for platform in cfg.meta.platforms:
        since = None
        if not full:
            if watermark := store.get_state(f"meta:{platform}:watermark"):
                since = datetime.fromisoformat(watermark)
                log.info("%s: incremental since %s", platform, since)
        yield platform, MetaGraphConnector(cfg.meta, platform=platform, since=since)


def cmd_ingest(args, cfg: Config) -> int:
    sources = ["meta", "zalo", "oa"] if args.source == "all" else [args.source]
    total_conv = total_msg = 0

    with Store(cfg.db_path) as store:
        if "meta" in sources:
            for platform, conn in _meta_connectors(cfg, store, args.full):
                started = datetime.now(timezone.utc)
                for conv in conn.fetch():
                    total_msg += store.upsert_conversation(conv)
                    total_conv += 1
                store.set_state(f"meta:{platform}:watermark", started.isoformat())

        if "zalo" in sources:
            from .connectors.zalo_export import ZaloExportConnector

            done = set(json.loads(store.get_state("zalo:files") or "[]"))
            conn = ZaloExportConnector(cfg.zalo, processed=set() if args.full else done)
            for conv in conn.fetch():
                total_msg += store.upsert_conversation(conv)
                total_conv += 1
            store.set_state("zalo:files", json.dumps(sorted(done | conn.seen_hashes)))

        if "oa" in sources:
            from .connectors.zalo_oa import ZaloOAConnector

            events = store.oa_events()
            conn = ZaloOAConnector(events)
            for conv in conn.fetch():
                total_msg += store.upsert_conversation(conv)
                total_conv += 1

        print(f"ingested {total_conv} conversation(s), {total_msg} new message(s)")
        print(json.dumps(store.stats(), indent=2))
    return 0


# -------------------------------------------------------------------- extract

def cmd_extract(args, cfg: Config) -> int:
    from .extract.base import build_extractor
    from .extract.prompt import PROMPT_VERSION, build_user_prompt

    schema = cfg.load_schema()

    with Store(cfg.db_path) as store:
        conversations = store.conversations()
        if args.source:
            conversations = [c for c in conversations if c.source.value.startswith(args.source)]
        if args.limit:
            conversations = conversations[: args.limit]

        if args.dry_run:
            for conv in conversations:
                prompt = build_user_prompt(conv, schema,
                                           max_chars=cfg.extract.max_transcript_chars,
                                           display_timezone=cfg.extract.display_timezone)
                print(f"\n{'=' * 70}\n{conv.source.value} {conv.conversation_id}"
                      f" — {len(prompt)} chars ≈ {len(prompt) // 4} tokens\n{'=' * 70}")
                print(prompt[:2000])
            est = sum(len(build_user_prompt(c, schema,
                                            max_chars=cfg.extract.max_transcript_chars,
                                            display_timezone=cfg.extract.display_timezone))
                      for c in conversations) // 4
            print(f"\n{len(conversations)} conversation(s), ~{est:,} input tokens total. "
                  "No API calls made.")
            return 0

        pending, cached = [], 0

        for conv in conversations:
            hit = store.cached_extraction(
                conv, schema_version=schema.version, schema_hash=schema.fingerprint(),
                prompt_version=PROMPT_VERSION, model=cfg.extract.model,
            )
            if hit and not args.force:
                cached += 1
            else:
                pending.append(conv)

        log.info("%d cached, %d to extract", cached, len(pending))

        if not pending:
            # Nothing to ask the model, so nothing should require an API key or the
            # provider SDK. Building the extractor up front made a fully-cached run
            # fail on a machine that only ever needs to re-write the workbook.
            print(f"\nextracted 0, cached {cached}, failed 0")
            return 0

        extractor = build_extractor(cfg.extract, schema)
        failures = 0
        with ThreadPoolExecutor(max_workers=cfg.extract.concurrency) as pool:
            for conv, res in zip(pending, pool.map(extractor.extract, pending)):
                store.save_extraction(res, conv.content_hash())
                if res.error:
                    failures += 1
                    print(f"  FAIL {conv.conversation_id}: {res.error}")
                else:
                    filled = sum(1 for v in res.values.values() if v is not None)
                    print(f"  ok   {conv.conversation_id} ({filled}/{len(schema.names)} fields)")

        print(f"\nextracted {len(pending) - failures}, cached {cached}, failed {failures}")
    return 1 if failures and args.strict else 0


# ----------------------------------------------------------------------- load

def cmd_load(args, cfg: Config) -> int:
    from .extract.prompt import PROMPT_VERSION
    from .load.excel import write_workbook

    schema = cfg.load_schema()
    out = Path(args.out) if args.out else cfg.output_dir / f"lavabo-{datetime.now():%Y%m%d-%H%M}.xlsx"

    with Store(cfg.db_path) as store:
        conversations = store.conversations()

        # Orders from several months can sit in the store at once -- a backfill of
        # Oct 2025 alongside the current month. Without this filter they would all
        # land in one workbook, under a sheet named after whichever came first.
        if args.month:
            wanted_year = args.year
            kept = []
            for conv in conversations:
                if conv.raw.get("order_month") != args.month:
                    continue
                if wanted_year and (conv.raw.get("order_year") or wanted_year) != wanted_year:
                    continue
                kept.append(conv)
            dropped = len(conversations) - len(kept)
            conversations = kept
            if dropped:
                print(f"({dropped} order(s) from other months excluded)")

        results = {}
        for conv in conversations:
            hit = store.cached_extraction(
                conv, schema_version=schema.version, schema_hash=schema.fingerprint(),
                prompt_version=PROMPT_VERSION, model=cfg.extract.model,
            )
            if hit:
                results[conv.conversation_id] = hit

        missing = len(conversations) - len(results)
        if missing:
            # Loud, because the workbook still writes: those rows carry only the
            # fields derived from the note header, which reads as "mostly empty"
            # rather than as a failure.
            print(f"\n!! {missing} of {len(conversations)} order(s) have no usable "
                  f"extraction for the current schema.")
            print("   Their rows will contain only date, order number and customer.")
            print("   Run `lavabo inspect` to see why, then `lavabo extract`.\n")
            log.warning("%d conversation(s) have no extraction for schema v%d",
                        missing, schema.version)

        if args.layout == "senkahomes":
            from .load.senkahomes import missing_schema_fields, write_orders_workbook

            if _schema_mismatch(schema, missing_schema_fields(schema)):
                return 1

            write_orders_workbook(
                out, conversations, results,
                sheet_name=args.sheet,
                default_year=args.year,
                default_status=args.status,
                closer=args.closer,
            )
        else:
            write_workbook(out, schema, conversations, results,
                           run_meta={"provider": cfg.extract.provider},
                           display_timezone=cfg.extract.display_timezone)

    print(f"wrote {out}")
    return 0


# ---------------------------------------------------------------------- other

def _schema_mismatch(schema, missing: list[str]) -> bool:
    """True (and explains) when the active schema cannot feed the senkahomes layout."""
    if not missing:
        return False
    print(f"\nconfig/schema.yaml (v{schema.version}) does not define: {', '.join(missing)}")
    print(f"  It has: {', '.join(schema.names)}")
    print("  Those look like the placeholder columns from schema.example.yaml. The")
    print("  senkahomes layout cannot fill Địa chỉ, Tên sản phẩm, Tổng or Cọc from them,")
    print("  so the workbook would come out with only dates and names.")
    print("\n  Fix: cp config/schema.senkahomes.yaml config/schema.yaml && lavabo extract")
    return True


def cmd_check(args, cfg: Config) -> int:
    ok = True

    with Store(cfg.db_path) as store:
        print(f"staging db: {cfg.db_path}")
        print(json.dumps(store.stats(), indent=2))

        try:
            schema = cfg.load_schema()
            print(f"schema:     v{schema.version}, {len(schema.names)} columns "
                  f"({', '.join(schema.names[:6])}{'...' if len(schema.names) > 6 else ''})")
            from .load.senkahomes import REQUIRED_FIELDS, missing_schema_fields

            # Not a failure -- the generic layout is a legitimate use. But an install
            # left on the placeholder schema fails much later, at load, so say it now.
            if len(missing_schema_fields(schema)) == len(REQUIRED_FIELDS):
                print("            note: none of the senkahomes fields are defined. If you "
                      "want that layout,\n                  copy config/schema.senkahomes.yaml "
                      "to config/schema.yaml.")
        except Exception as exc:
            print(f"schema:     NOT READY — {exc}")
            ok = False

        # Checked here because the failure otherwise lands at the end of an export, after
        # a capture session, rather than before one.
        from .tz import problem as tz_problem

        zones = {cfg.zalo.timezone, cfg.extract.display_timezone}
        if troubles := [t for z in sorted(zones) if (t := tz_problem(z))]:
            for trouble in troubles:
                print(f"FAIL tz:    {trouble}")
            ok = False
        else:
            print(f"timezone:   {', '.join(sorted(zones))}")

        # LLM key: only needed for `extract`, so report it without failing the preflight.
        try:
            from .extract.base import extractor_class
            cls = extractor_class(cfg.extract.provider)
            try:
                cls.check_model_matches_provider(cfg.extract.provider, cfg.extract.model)
            except RuntimeError as exc:
                print(f"FAIL llm: {exc}")
                return 1
            good, detail = cls.verify_api_key() if not args.offline else (
                cls.key_problem() is None,
                cls.key_problem() or "key present (not verified, --offline)",
            )
            mark = "OK  " if good else "FAIL"
            print(f"{mark} llm: {cfg.extract.provider} / {cfg.extract.model} — {detail}")
            ok &= good
        except Exception as exc:
            print(f"llm:        NOT READY — {exc}")

        from .connectors.zalo_export import ZaloExportConnector
        for good, msg in [ZaloExportConnector(cfg.zalo).check()]:
            print(f"{'OK  ' if good else 'FAIL'} {msg}")
            ok &= good

        # Not a pass/fail condition -- an empty store is normal before the first paste.
        # Reported so it is visible that the pastes are being kept, since the whole value
        # of keeping them is that they are there on the day something needs re-reading.
        from . import rawpaste
        if pastes := rawpaste.load_index(cfg.zalo.inbox_dir):
            periods = {(e.get("year"), e.get("month")) for e in pastes}
            print(f"raw pastes: {len(pastes)} kept across {len(periods)} month(s) "
                  f"in {rawpaste.store_dir(cfg.zalo.inbox_dir)}")

        try:
            for _, conn in _meta_connectors(cfg, store, full=True):
                good, msg = conn.check()
                print(f"{'OK  ' if good else 'FAIL'} {msg}")
                ok &= good
        except Exception as exc:
            print(f"FAIL meta: {exc}")
            ok = False

    return 0 if ok else 1


def cmd_append(args, cfg: Config) -> int:
    """Insert orders into the shop's own workbook, after a backup."""
    from .load.append import append_orders
    from .load.senkahomes import missing_schema_fields
    from .pipeline import stored_for_month

    schema = cfg.load_schema()
    if _schema_mismatch(schema, missing_schema_fields(schema)):
        return 1
    target = Path(args.into)

    conversations, results = stored_for_month(cfg, args.month, args.year)
    if not conversations:
        print(f"No orders stored for {args.month:02d}/{args.year}. Nothing to add.")
        return 1

    unextracted = len(conversations) - len(results)
    if unextracted:
        print(f"!! {unextracted} of {len(conversations)} order(s) have no usable "
              "extraction; their rows would carry only date and customer.")
        if not args.force:
            print("   Run `lavabo extract` first, or pass --force to add them anyway.")
            return 1

    summary = append_orders(
        target, conversations, results,
        month=args.month, year=args.year, sheet=args.sheet,
        status=args.status, closer=args.closer,
        dry_run=args.dry_run, mark_new=not args.no_highlight,
    )

    if summary["collision"]:
        rows = ", ".join(str(r) for r in summary["collision"])
        print(f"\nsheet {summary['sheet']}: refusing to write.")
        print(f"  Rows {rows} already contain something, and the {summary['added']} new "
              "order(s) would land on top of them.")
        print("  That is usually a summary or totals block below the data. Move it down, "
              "or pass --sheet to write elsewhere.")
        return 1

    verb = "would add" if args.dry_run else "added"
    print(f"\nsheet {summary['sheet']}"
          + ("  (created)" if summary["created_sheet"] else ""))
    print(f"  {verb} {summary['added']} order(s), {summary['rows_written']} row(s)"
          + (f", starting at row {summary['start_row']}" if summary["start_row"] else ""))
    if summary["already_present"]:
        names = ", ".join(n or "?" for n in summary["already_names"][:6])
        print(f"  skipped {summary['already_present']} already in the sheet: {names}"
              + ("…" if summary["already_present"] > 6 else ""))
    if summary["backup"]:
        print(f"  backup: {Path(summary['backup']).name}")
    if args.dry_run:
        print("\n  (dry run — nothing was written)")
    return 0


def cmd_inspect(args, cfg: Config) -> int:
    """Show what was actually stored for each conversation, errors and all.

    An output that looks merely incomplete usually means extraction failed and the
    writer fell back to the fields it can derive without a model. This makes the
    difference visible.
    """
    from .extract.prompt import PROMPT_VERSION

    schema = cfg.load_schema()
    fingerprint = schema.fingerprint()

    with Store(cfg.db_path) as store:
        conversations = store.conversations()
        if args.limit:
            conversations = conversations[: args.limit]

        print(f"schema v{schema.version} fingerprint {fingerprint}, "
              f"model {cfg.extract.model}\n")

        usable = failed = stale = absent = 0
        reusable_models: set[str] = set()
        for conv in conversations:
            rows = store.latest_extraction_rows(conv.conversation_id)
            current = [r for r in rows
                       if r["schema_hash"] == fingerprint and r["model"] == cfg.extract.model
                       and r["prompt_version"] == PROMPT_VERSION]

            print(f"--- {conv.conversation_id}")
            if not rows:
                absent += 1
                print("    no extraction stored at all")
            elif not current:
                stale += 1
                other = rows[0]
                # Distinguish "extracted under a different model" from "extracted for a
                # different schema". The first is reusable by pointing the config at that
                # model; the second genuinely has to be redone.
                if (other["schema_hash"] == fingerprint
                        and other["prompt_version"] == PROMPT_VERSION):
                    reusable_models.add(other["model"])
                    print(f"    same schema, different model ({other['model']})"
                          " — reusable, see the note below")
                else:
                    print(f"    stale: schema_hash={other['schema_hash'] or '(none)'} "
                          f"model={other['model']} — re-run `lavabo extract`")
            else:
                row = current[0]
                if row["error"]:
                    failed += 1
                    print(f"    FAILED: {row['error'][:300]}")
                else:
                    usable += 1
                    values = json.loads(row["values_json"])
                    filled = [k for k, v in values.items() if v not in (None, "", [], {})]
                    print(f"    ok, {len(filled)}/{len(schema.names)} fields filled")
                    if args.values:
                        print("    " + json.dumps(values, ensure_ascii=False)[:600])
                    elif empty := [k for k in schema.names if k not in filled]:
                        print(f"    empty: {', '.join(empty)}")

        print(f"\nusable {usable}, failed {failed}, stale {stale}, none {absent}")
        if failed or stale or absent:
            print("Anything not 'usable' contributes only its header-derived fields "
                  "(date, order no, customer) to the workbook.")

        if reusable_models:
            names = ", ".join(sorted(reusable_models))
            print(f"\nNOTHING NEEDS RE-EXTRACTING. Those rows were produced under model "
                  f"{names} for this exact schema.\nThe model is part of the cache key "
                  "because different models give different answers, so pointing the config "
                  "back\nat the one that produced them makes them usable again:\n"
                  f"\n    extract.model: \"{sorted(reusable_models)[0]}\"   "
                  "in config/config.yaml\n"
                  f"\nor pass --model {sorted(reusable_models)[0]} to load. "
                  f"Re-extracting under {cfg.extract.model} instead is also valid, just "
                  "not free.")
    return 0


def cmd_resegment(args, cfg: Config) -> int:
    """Replay stored pastes through today's capture code.

    The counterpart to what the extraction cache already does. Bumping PROMPT_VERSION or
    editing schema.yaml re-extracts every order, because the cache is keyed on both --
    but an order's .txt is the output of whatever SPLITTING code ran the day it was
    captured, and no key covers that. Fixing a header pattern or a trim leaves the orders
    already on disk exactly as the old code left them, and re-pasting does not correct
    them either: a corrected body that is SHORTER loses to the stored one, by the same
    rule that rescues an order from a scroll that was cut short.
    """
    from . import resegment

    # Taken BEFORE the replay writes anything, and only when it is going to write. These
    # are the shop's orders; a maintenance command should not be the reason any go missing.
    if args.apply and not args.no_backup and cfg.zalo.inbox_dir.exists():
        print(f"inbox copied to {resegment.backup(cfg.zalo.inbox_dir)}")

    result = resegment.run(cfg, month=args.month, year=args.year, apply=args.apply)
    if not result.pastes:
        print("no stored pastes to replay"
              + (f" for {args.month:02d}/{args.year}" if args.month and args.year else "")
              + f" (looked in {rawpaste_dir(cfg)})")
        return 0

    for change in result.changes:
        print(f"  {change}")
    print(result.summary())

    if not args.apply:
        if result.of("added") or result.of("changed"):
            print("\nNothing was written. Re-run with --apply to keep these corrections;"
                  "\nthe inbox is copied aside first unless you pass --no-backup.")
        return 0

    print("corrections written. Run `lavabo ingest --source zalo` and `lavabo extract` "
          "to carry them through — a changed order re-extracts by itself, since the "
          "cache is keyed on the text.")
    return 0


def rawpaste_dir(cfg: Config):
    from . import rawpaste
    return rawpaste.store_dir(cfg.zalo.inbox_dir)


def cmd_config(args, cfg: Config) -> int:
    """Show the effective settings and where they drift from the shipped example.

    config/config.yaml is gitignored, so `git pull` never touches it. When the example
    gains a new default the working copy silently keeps the old one, which is how a
    Gemini provider ends up still pointing at a Claude model.
    """
    import yaml

    from .config import REPO_ROOT
    from .extract.base import _provider_for_model

    path = args.config or REPO_ROOT / "config" / "config.yaml"
    example = REPO_ROOT / "config" / "config.example.yaml"

    print(f"config file: {path}{'' if path.exists() else '   (MISSING — copy the example)'}")
    print(f"schema file: {cfg.schema_path}\n")

    print("effective extract settings:")
    for key in ("provider", "model", "concurrency", "temperature", "max_tokens"):
        print(f"  {key:16} {getattr(cfg.extract, key)}")

    owner = _provider_for_model(cfg.extract.model)
    if owner and owner != cfg.extract.provider.lower():
        print(f"\n  MISMATCH: model {cfg.extract.model!r} is a {owner} model but provider "
              f"is {cfg.extract.provider!r}")

    if not example.exists():
        return 0

    theirs = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    shipped = yaml.safe_load(example.read_text(encoding="utf-8")) or {}

    conflicts, absent = [], []
    for section, values in shipped.items():
        if not isinstance(values, dict):
            continue
        mine = theirs.get(section) or {}
        for key, want in values.items():
            if key not in mine:
                absent.append(f"{section}.{key}")
            elif mine[key] != want:
                conflicts.append((f"{section}.{key}", mine[key], want))

    if conflicts:
        print("\nset differently from config.example.yaml (yours -> example):")
        for key, got, want in conflicts:
            print(f"  {key:28} {got!r}  ->  {want!r}")
        print("\nNot all of these are wrong — own_names, page_id and paths are meant to "
              "differ.\nThe ones that usually matter are extract.provider and extract.model.")
    else:
        print("\nno conflicting keys against config.example.yaml.")

    if absent:
        print(f"\nnot present in your file, so built-in defaults apply ({len(absent)}):")
        print("  " + ", ".join(absent))

    print(f"\nEdit {path} to change any of these — it is gitignored, so `git pull` "
          "never updates it.")
    return 0


def cmd_models(args, cfg: Config) -> int:
    """Ask the provider which models this key can use.

    Model names change faster than any list kept in this repo, so this is the only
    trustworthy source when `extract` reports an unknown model.
    """
    from .extract.base import extractor_class

    cls = extractor_class(cfg.extract.provider)
    good, detail = cls.verify_api_key()
    if not good:
        print(f"cannot list models: {detail}")
        return 1

    try:
        names = cls.list_models()
    except Exception as exc:
        print(f"could not list models: {type(exc).__name__}: {exc}\n"
              "The key looked usable, so this is most likely a network or proxy problem "
              "rather than a bad key.")
        return 1

    if not names:
        print(f"{cfg.extract.provider} returned no usable models for this key.")
        return 1

    print(f"{cfg.extract.provider} models available to this key:\n")
    for name in names:
        mark = "  <- configured" if name == cfg.extract.model else ""
        print(f"  {name}{mark}")

    if cfg.extract.model not in names:
        print(f"\nWARNING: configured model {cfg.extract.model!r} is NOT in this list. "
              "Set extract.model in config.yaml to one of the above, or pass --model.")
        return 1
    return 0


def cmd_verify(args, cfg: Config) -> int:
    from .extract.prompt import PROMPT_VERSION

    schema = cfg.load_schema()
    problems: list[str] = []

    with Store(cfg.db_path) as store:
        conversations = store.conversations()
        if not conversations:
            problems.append("staging db is empty — nothing was ingested")

        required = [c.name for c in schema.columns if c.required]
        missing_extraction = 0
        null_counts = {name: 0 for name in required}

        for conv in conversations:
            if not conv.messages:
                problems.append(f"{conv.conversation_id}: ingested with zero messages")
            hit = store.cached_extraction(
                conv, schema_version=schema.version, schema_hash=schema.fingerprint(),
                prompt_version=PROMPT_VERSION, model=cfg.extract.model,
            )
            if not hit:
                missing_extraction += 1
                continue
            for name in required:
                if hit.values.get(name) is None:
                    null_counts[name] += 1

        if missing_extraction:
            problems.append(f"{missing_extraction} conversation(s) not extracted at schema v{schema.version}")

        extracted = max(len(conversations) - missing_extraction, 1)
        for name, n in null_counts.items():
            if n / extracted > args.null_threshold:
                problems.append(
                    f"required column {name!r} is null in {n}/{extracted} rows "
                    f"({n / extracted:.0%} > {args.null_threshold:.0%}) — "
                    "the column description probably needs sharpening"
                )

    if problems:
        print("VERIFY FAILED")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"VERIFY OK — {len(conversations)} conversation(s), schema v{schema.version}")
    return 0


def cmd_run(args, cfg: Config) -> int:
    for step in (cmd_ingest, cmd_extract, cmd_load):
        if code := step(args, cfg):
            return code
    return 0


# ----------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    _load_dotenv()

    ap = argparse.ArgumentParser(prog="lavabo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", type=Path, help="path to config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="preflight credentials, paths and schema")
    p.add_argument("--offline", action="store_true",
                   help="skip verifying the API key against the provider")
    add_llm_args(p)

    p = sub.add_parser("ingest", help="pull/parse conversations into staging")
    p.add_argument("--source", choices=["meta", "zalo", "oa", "all"], default="all")
    p.add_argument("--full", action="store_true", help="ignore watermarks, re-ingest everything")

    p = sub.add_parser("extract", help="run the LLM extraction step")
    p.add_argument("--source", choices=["messenger", "instagram", "zalo"])
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true", help="ignore the extraction cache")
    p.add_argument("--dry-run", action="store_true", help="print prompts and token estimate only")
    p.add_argument("--strict", action="store_true", help="exit non-zero if any extraction failed")
    add_llm_args(p)

    p = sub.add_parser("load", help="write the Excel workbook")
    p.add_argument("--out", help="output .xlsx path")
    p.add_argument("--layout", choices=["generic", "senkahomes"], default="generic",
                   help="'senkahomes' writes the QUẢN LÝ ĐƠN columns, one row per line "
                        "item; 'generic' writes one row per record from schema.yaml")
    p.add_argument("--sheet", help="sheet name (senkahomes layout; default MMYYYY)")
    p.add_argument("--month", type=int, metavar="M",
                   help="only include orders from this month (1-12). Without it, every "
                        "stored order is written, which mixes months after a backfill")
    p.add_argument("--year", type=int,
                   help="year for NGÀY CHỐT, since headers carry only day/month "
                        "(default: this year); also narrows --month")
    p.add_argument("--status", default="New", help="value for Trạng thái (default: New)")
    p.add_argument("--closer", help="value for Người chốt đơn, e.g. \"Trà My\" — the note "
                                    "does not record who sent it")
    add_llm_args(p)

    p = sub.add_parser("append", help="add orders into an existing workbook, after a backup")
    p.add_argument("--into", required=True, metavar="FILE.xlsx",
                   help="the workbook to add to, e.g. 'QUẢN LÝ ĐƠN SENKAHOMES.xlsx'")
    p.add_argument("--month", type=int, required=True, metavar="M")
    p.add_argument("--year", type=int, required=True, metavar="Y")
    p.add_argument("--sheet", help="sheet name (default: MMYYYY)")
    p.add_argument("--status", default="New")
    p.add_argument("--closer")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be added, write nothing")
    p.add_argument("--force", action="store_true",
                   help="add orders even if they have no usable extraction")
    p.add_argument("--no-highlight", action="store_true",
                   help="do not tint the rows this run added")
    add_llm_args(p)

    p = sub.add_parser("inspect", help="show stored extractions, including failures")
    p.add_argument("--limit", type=int)
    p.add_argument("--values", action="store_true", help="print the extracted values too")
    add_llm_args(p)

    p = sub.add_parser(
        "resegment",
        help="re-capture the stored pastes with today's code, after fixing capture logic")
    p.add_argument("--month", type=int, help="only pastes captured for this month")
    p.add_argument("--year", type=int)
    p.add_argument("--apply", action="store_true",
                   help="write the corrections (default is to only report them)")
    p.add_argument("--no-backup", action="store_true",
                   help="skip copying the inbox aside before writing")
    add_llm_args(p)

    p = sub.add_parser("config", help="show effective settings and drift from the example")
    add_llm_args(p)

    p = sub.add_parser("models", help="list the models this API key can use")
    add_llm_args(p)

    p = sub.add_parser("verify", help="sanity-check the staged data and extractions")
    p.add_argument("--null-threshold", type=float, default=0.5)
    add_llm_args(p)

    p = sub.add_parser("run", help="ingest + extract + load")
    p.add_argument("--source", choices=["meta", "zalo", "all"], default="all")
    p.add_argument("--out", help="output .xlsx path")
    p.add_argument("--full", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--layout", choices=["generic", "senkahomes"], default="generic")
    p.add_argument("--sheet")
    p.add_argument("--year", type=int)
    p.add_argument("--status", default="New")
    p.add_argument("--closer")
    add_llm_args(p)

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        cfg = Config.load(args.config)
    except ValueError as exc:          # a broken config file, not a broken program
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    _apply_llm_overrides(args, cfg)

    handlers = {"check": cmd_check, "ingest": cmd_ingest, "extract": cmd_extract,
                "load": cmd_load, "verify": cmd_verify, "run": cmd_run,
                "models": cmd_models, "config": cmd_config,
                "inspect": cmd_inspect, "append": cmd_append,
                "resegment": cmd_resegment}
    try:
        return handlers[args.command](args, cfg)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
