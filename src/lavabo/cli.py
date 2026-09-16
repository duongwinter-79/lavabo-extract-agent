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
    lavabo kb init                     write the blank intake pack for the shop
    lavabo kb check                    validate the filled-in pack before uploading
    lavabo kb init --one-file x.xlsx   the whole pack as ONE workbook, for Google Sheets
    lavabo kb media --from <dump|xlsx> phone photos/videos, or photos pasted into a
                                      workbook -> named images/ + mapping
    lavabo kb publish --to drive/      filled pack -> the folder Meta's Drive connector reads
    lavabo kb contact-sheet --from <folder>   number unnamed photos so the shop can
                                      name them all in one message
    lavabo kb from-images --from <folder>     read size/colour/price OFF the pictures
                                      into a draft a human confirms
    lavabo kb feed                     catalog.xlsx -> Meta Commerce product feed
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
            staged = {c.conversation_id for c in store.conversations(source=Source.ZALO)}
            conn = ZaloExportConnector(cfg.zalo,
                                       processed=set() if args.full else done,
                                       staged=staged)
            for conv in conn.fetch():
                # A Zalo order file IS the whole conversation, so a file that shrank must
                # not leave the lines it lost staged behind it.
                total_msg += store.upsert_conversation(conv, replace_messages=True)
                total_conv += 1
            store.set_state("zalo:files", json.dumps(sorted(done | conn.seen_hashes)))
            # Drop anything staged with no file behind it any more: deleted by hand,
            # renamed when a transposed date was corrected, or left by the older id
            # scheme that minted a new conversation every time a file was rewritten.
            if dropped := store.prune_conversations(
                    Source.ZALO, conn.current_stems(), identify=conn.file_stem):
                print(f"removed {dropped} staged order(s) with no file in the inbox")

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


def cmd_kb(args, cfg: Config) -> int:
    """The knowledge pack: hand out the blank forms, then refuse the bad ones.

    Deliberately independent of the staging db and of any API key -- this runs on a
    laptop belonging to whoever is chasing the shop for their price list.
    """
    from .kb.check import check_intake, check_one_file, report
    from .kb.onefile import write_one_file
    from .kb.templates import write_intake, write_zip

    if args.kb_command == "contact-sheet":
        return _kb_contact_sheet(args, cfg)

    if args.kb_command == "from-images":
        return _kb_from_images(args, cfg)

    directory = Path(args.dir)

    if args.kb_command == "media":
        return _kb_media(args, cfg, directory)

    if args.kb_command == "init" and args.one_file:
        out = Path(args.one_file)
        try:
            write_one_file(out, force=args.force)
        except FileExistsError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"  tạo  {out}")
        print("\n  Tải file này lên Google Drive rồi mở bằng Google Sheets, "
              "chia sẻ link cho shop.")
        print(f"  Điền xong, tải về .xlsx rồi chạy: lavabo kb check --file {out}")
        return 0

    if args.kb_command == "init":
        written, skipped = write_intake(directory, force=args.force)
        for path in written:
            print(f"  tạo  {path}")
        for path in skipped:
            print(f"  giữ  {path} (đã có sẵn)")
        if args.zip:
            archive = write_zip(directory, Path(args.zip) if isinstance(args.zip, str)
                                else directory.with_suffix(".zip"))
            print(f"  gói {archive}")
        if skipped and not args.force:
            print("\n  Những file đã có được giữ nguyên. --force để ghi đè.")
        print(f"\n  Gửi thư mục {directory} cho shop. "
              f"Điền xong thì chạy: lavabo kb check --dir {directory}")
        return 0

    # Before the generic --file branch below, which is `check`'s and returns early.
    if args.kb_command == "publish" and getattr(args, "file", None):
        return _kb_publish_one_file(args, cfg)

    if getattr(args, "file", None):
        problems = check_one_file(Path(args.file))
        print(report(problems))
        fatal = [p for p in problems if p.fatal]
        if fatal:
            print(f"\nCHƯA ĐẠT — {len(fatal)} lỗi phải sửa.")
            return 1
        if args.strict and problems:
            print(f"\nCHƯA ĐẠT (--strict) — {len(problems)} cảnh báo.")
            return 1
        print("\nĐẠT — pack sẵn sàng để tải lên.")
        return 0

    if not directory.is_dir():
        print(f"Không tìm thấy thư mục {directory}. "
              f"Tạo bằng: lavabo kb init --dir {directory}", file=sys.stderr)
        return 1

    problems = check_intake(directory)
    fatal = [p for p in problems if p.fatal]

    if args.kb_command == "feed":
        return _kb_feed(args, cfg, directory, fatal)

    if args.kb_command == "publish":
        return _kb_publish(args, cfg, directory, fatal)

    print(report(problems))
    if fatal:
        print(f"\nCHƯA ĐẠT — {len(fatal)} lỗi phải sửa.")
        return 1
    if args.strict and problems:
        print(f"\nCHƯA ĐẠT (--strict) — {len(problems)} cảnh báo.")
        return 1
    print("\nĐẠT — pack sẵn sàng để tải lên.")
    return 0


def _kb_media(args, cfg: Config, directory: Path) -> int:
    """Turn a phone dump into the images/ folder the pack expects."""
    from .kb.check import read_catalog
    from .kb.media import organise, organise_workbook, write_mapping

    source = Path(args.source)
    workbook = source.is_file() and source.suffix.lower() == ".xlsx"
    if not source.is_dir() and not workbook:
        print(f"Không tìm thấy {source} (cần một thư mục ảnh, hoặc file .xlsx có ảnh dán sẵn)",
              file=sys.stderr)
        return 1

    mapping = None
    if args.map:
        from .kb.contact import read_mapping
        try:
            mapping = read_mapping(Path(args.map))
        except (ValueError, KeyError) as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"  dùng bảng đặt tên: {len(mapping)} ảnh đã có mã")

    known = None
    catalog = directory / "catalog.xlsx"
    if catalog.exists():
        known = {str(r.get("ma_sp", "")).strip().lower() for r in read_catalog(directory)}

    images = directory / "images"
    report = (organise_workbook(source, images, known_skus=known) if workbook
              else organise(source, images, known_skus=known, frames=args.frames,
                            mapping=mapping))

    print(f"  {report.photo_count} ảnh cho {len(report.products)} sản phẩm -> {images}")
    if report.videos:
        print(f"  {len(report.videos)} video, đã lấy khung hình rõ nhất làm ảnh")
    for sku, names in sorted(report.products.items()):
        if not names:
            print(f"  THIẾU ẢNH: {sku} — thư mục rỗng hoặc không đọc được file nào")

    if report.heic:
        print(f"\n  {len(report.heic)} ảnh định dạng HEIC chưa đọc được:")
        for name in report.heic[:5]:
            print(f"    - {name}")
        print("    iPhone: Cài đặt > Camera > Định dạng > chọn 'Tương thích nhất',")
        print("    rồi chụp lại, hoặc gửi qua Zalo/Messenger (tự đổi sang JPG).")
    if report.unmapped:
        print(f"\n  {len(report.unmapped)} file không biết thuộc sản phẩm nào — "
              "để trong thư mục mang tên mã SP:")
        for name in report.unmapped[:5]:
            print(f"    - {name}")
    if report.unknown_sku:
        print(f"\n  CẢNH BÁO: {len(report.unknown_sku)} mã không có trong catalog.xlsx: "
              + ", ".join(report.unknown_sku[:5]))

    if not args.no_mapping and report.products:
        write_mapping(report, directory / "images.xlsx")
        print(f"\n  đã ghi {directory / 'images.xlsx'}")
    print(f"\n  Kiểm tra lại: lavabo kb check --dir {directory}")
    return 0


def _kb_from_images(args, cfg: Config) -> int:
    """Read what the pictures say, into a draft nobody may mistake for a price list."""
    from .extract.base import build_extractor
    from .kb.fromimages import read_folder, write_draft

    source = Path(args.source)
    if not source.is_dir():
        print(f"Không tìm thấy thư mục {source}", file=sys.stderr)
        return 1

    schema = cfg.load_schema()
    extractor = build_extractor(cfg.extract, schema)
    report = read_folder(source, extractor, limit=args.limit, mode=args.mode)
    if not report.readings:
        print(f"Không có ảnh nào trong {source}", file=sys.stderr)
        return 1

    target = write_draft(report, Path(args.out))
    print(f"  {target}")
    if args.mode == "chat":
        print(f"  {len(report.readings)} ảnh · {len(report.products)} có nói về giá · "
              f"{len(report.with_price)} đọc được số · "
              f"{len(report.quoted_by_shop)} do SHOP báo giá")
    else:
        print(f"  {len(report.readings)} ảnh · {len(report.products)} là ảnh sản phẩm · "
              f"{len(report.with_price)} có giá đọc được")
    if report.failed:
        print(f"  {len(report.failed)} ảnh không đọc được")
    print(f"  tokens: {report.input_tokens} vào / {report.output_tokens} ra")
    print("\n  ĐÂY LÀ BẢN NHÁP. Giá trên ảnh có thể cũ, hoặc là giá của nơi khác.")
    print("  Đối chiếu với shop, điền cột ma_sp và da_kiem_tra, rồi mới chuyển sang "
          "catalog.xlsx.")
    return 0


def _kb_contact_sheet(args, cfg: Config) -> int:
    """Turn a pile of nameless photos into one question the shop can answer."""
    from .kb.contact import build

    source = Path(args.source)
    if not source.is_dir():
        print(f"Không tìm thấy thư mục {source}", file=sys.stderr)
        return 1

    try:
        sheet = build(source, Path(args.out))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"  {sheet.count} ảnh, {len(sheet.pages)} trang")
    for page in sheet.pages:
        print(f"    {page}")
    print(f"    {sheet.mapping}")
    print("\n  Gửi các trang ảnh cho shop, hỏi: ảnh số mấy là mẫu nào.")
    print(f"  Điền mã vào cột ma_sp trong {sheet.mapping.name}, rồi chạy:")
    print(f"    lavabo kb media --from {source} --map {sheet.mapping} --dir intake")
    return 0


def _kb_publish_one_file(args, cfg: Config) -> int:
    """Publish straight from the one-file workbook.

    `publish` reads a folder of six files; a shop working from a phone filled one
    workbook. Making them assemble that folder by hand is the exact step the workbook
    exists to avoid, so the workbook is spread into a throwaway pack and published from
    there. The published folder is the deliverable; the pack is scaffolding and does not
    outlive the command.
    """
    import tempfile

    from .kb.check import check_intake
    from .kb.onefile import write_pack

    workbook = Path(args.file)
    if not workbook.is_file():
        print(f"Không tìm thấy {workbook}", file=sys.stderr)
        return 1

    images = Path(args.images) if getattr(args, "images", None) else None
    if images and not images.is_dir():
        print(f"Không tìm thấy thư mục ảnh {images}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="lavabo-pack-") as tmp:
        pack = write_pack(workbook, Path(tmp) / "pack", images=images)
        fatal = [p for p in check_intake(pack) if p.fatal]
        if not images:
            log.info("không có --images: xuất bản kiến thức, không kèm ảnh")
        return _kb_publish(args, cfg, pack, fatal)


def _kb_publish(args, cfg: Config, directory: Path, fatal: list) -> int:
    """Publish only what a customer may be told, and only from a pack that passes."""
    from .kb.publish import KNOWLEDGE_DIR, blocking, instruction_gaps, publish
    from .kb.check import report as render

    stoppers = blocking(fatal)
    if stoppers:
        print(render(stoppers))
        print(f"\nKhông xuất bản — sửa {len(stoppers)} lỗi trên trước đã.", file=sys.stderr)
        return 1

    out = Path(args.to)
    result = publish(directory, out, image_base=args.image_base)

    print(f"  {out}")
    for name in result.written:
        print(f"    {KNOWLEDGE_DIR}/{name}")
    if result.images:
        print(f"    02-ANH-SAN-PHAM/ — {result.images} ảnh")
    if result.skipped_examples:
        print(f"  bỏ {result.skipped_examples} dòng ví dụ mẫu")
    if result.excluded:
        print("\n  KHÔNG xuất bản (đúng như thiết kế):")
        for line in result.excluded:
            print(f"    - {line}")
    gaps = instruction_gaps(fatal)
    if gaps:
        # Publishing is safe with these unanswered; switching the agent on is not.
        print("\n  CHƯA BẬT ĐƯỢC AI — phần Hướng dẫn còn thiếu:")
        for problem in gaps:
            print(f"    - {problem}")
        print("    Kiến thức vẫn xuất bản được; đừng bật AI trước khi điền xong.")

    print(f"\n  Tải {out / KNOWLEDGE_DIR} lên Drive và NỐI ĐÚNG thư mục đó.")
    print("  Hai thư mục còn lại không được nối.")
    return 0


def _kb_feed(args, cfg: Config, directory: Path, fatal: list) -> int:
    """A feed is where a spreadsheet becomes something the Page says out loud, so a
    catalogue that fails `kb check` never reaches one."""
    from .kb.check import read_catalog, report
    from .kb.feed import build_feed

    if fatal:
        print(report(fatal))
        print(f"\nKhông tạo feed — sửa {len(fatal)} lỗi trên trước đã "
              f"(lavabo kb check --dir {directory}).", file=sys.stderr)
        return 1

    if not args.link and not args.link_template:
        print("Feed của Meta bắt buộc có cột link. Truyền --link <URL trang Facebook>, "
              "hoặc --link-template 'https://.../{ma_sp}' nếu shop có web riêng. "
              "Xem docs/13 §6.2.", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else cfg.output_dir / "meta-catalog-feed.csv"
    result = build_feed(
        read_catalog(directory), out,
        link=args.link, link_template=args.link_template,
        image_base=args.image_base, brand=args.brand,
        timezone_name=cfg.zalo.timezone,
    )

    print(f"  {result.path}")
    print(f"  {result.rows} sản phẩm, {result.on_sale} đang khuyến mãi")
    if not args.brand:
        print("  CẢNH BÁO: chưa có --brand. Cột mpn đã điền bằng mã sản phẩm, "
              "nhưng nên truyền tên shop.")
    if result.without_image:
        # Not a failure: the CSV is still worth reading, and hosting images is a
        # deployment decision the shop has not made yet. But Meta will drop these rows.
        print(f"  CẢNH BÁO: {result.without_image}/{result.rows} dòng chưa có ảnh công khai. "
              "Meta sẽ từ chối đúng những dòng đó.")
        print("  Cách xử lý: --image-base <URL thư mục ảnh đã host>, hoặc thêm sản phẩm "
              "thủ công trong Commerce Manager (docs/13 §6.3).")
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

    p = sub.add_parser("kb", help="the shop's knowledge pack: blank forms, then validation")
    kb = p.add_subparsers(dest="kb_command", required=True)
    q = kb.add_parser("init", help="write the blank intake workbooks and text templates")
    q.add_argument("--dir", default="intake")
    q.add_argument("--force", action="store_true", help="overwrite files that already exist")
    q.add_argument("--zip", nargs="?", const=True, default=False,
                   help="also write a .zip of the pack, for forwarding it in one piece")
    q.add_argument("--one-file", metavar="PATH",
                   help="write the whole pack as ONE workbook instead, for Google Sheets")
    add_llm_args(q)
    q = kb.add_parser("check", help="validate a filled-in pack before it is uploaded")
    q.add_argument("--dir", default="intake")
    q.add_argument("--file", help="check a one-file workbook instead of a folder")
    q.add_argument("--strict", action="store_true", help="treat warnings as failures too")
    add_llm_args(q)
    q = kb.add_parser("media", help="phone photos and demo videos into the pack's images/")
    q.add_argument("--from", dest="source", required=True,
                   help="a folder of phone media (one subfolder per mã SP), or an .xlsx "
                        "with photos pasted next to the products")
    q.add_argument("--dir", default="intake")
    q.add_argument("--frames", type=int, default=3,
                   help="stills to pull from each demo video (default 3)")
    q.add_argument("--no-mapping", action="store_true", help="do not rewrite images.xlsx")
    q.add_argument("--map", help="a filled anh-can-dat-ten.xlsx from `kb contact-sheet`, "
                                 "for photos that arrived with no product name")
    add_llm_args(q)

    q = kb.add_parser("contact-sheet",
                      help="number unnamed photos so the shop can name them in one message")
    q.add_argument("--from", dest="source", required=True, help="folder of unnamed photos")
    q.add_argument("--out", default="contact-sheet", help="where to write the pages")
    add_llm_args(q)

    q = kb.add_parser("from-images",
                      help="read size/colour/price off product pictures into a draft")
    q.add_argument("--from", dest="source", required=True, help="folder of product images")
    q.add_argument("--out", default="catalog-draft.xlsx", help="draft .xlsx to write")
    q.add_argument("--limit", type=int, help="stop after N images — use it first")
    q.add_argument("--mode", choices=["product", "chat"], default="product",
                   help="product: a marketing image. chat: a screenshot of the Page inbox, "
                        "where who said the price decides whether it is the shop's")
    add_llm_args(q)

    q = kb.add_parser("publish",
                      help="a passing pack -> the folder Meta's Drive connector reads")
    q.add_argument("--dir", default="intake")
    q.add_argument("--file", help="publish a one-file workbook instead of a folder")
    q.add_argument("--images", help="product photos to publish (use with --file, "
                                    "which carries none of its own)")
    q.add_argument("--image-base", default="",
                   help="public URL the photos are hosted under — adds a link_anh column "
                        "to the price list so the agent can point a customer at the photo")
    q.add_argument("--to", default="drive", help="output folder (default: drive/)")
    add_llm_args(q)

    q = kb.add_parser("feed", help="turn a passing catalog.xlsx into a Meta Commerce feed")
    q.add_argument("--dir", default="intake")
    q.add_argument("--out", help="output .csv path (default: <output_dir>/meta-catalog-feed.csv)")
    q.add_argument("--link", default="",
                   help="URL for every product — the Page URL when there is no website")
    q.add_argument("--link-template", default="",
                   help="per-product URL with {ma_sp} substituted")
    q.add_argument("--image-base", default="",
                   help="public URL the image filenames hang off; without it rows ship "
                        "with no image and Meta rejects them")
    q.add_argument("--brand", default="", help="shop or manufacturer name")
    add_llm_args(q)
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
                "resegment": cmd_resegment, "kb": cmd_kb}
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
