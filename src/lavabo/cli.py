"""Command line entry point.

    lavabo check                       preflight credentials and paths
    lavabo ingest --source meta|zalo   pull/parse into the SQLite staging db
    lavabo extract [--limit N] [--dry-run] [--provider gemini --api-key AIza...]
    lavabo load --out report.xlsx
    lavabo run --out report.xlsx       ingest + extract + load
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
    sources = ["meta", "zalo"] if args.source == "all" else [args.source]
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

        extractor = build_extractor(cfg.extract, schema)
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
            log.warning("%d conversation(s) have no extraction for schema v%d — "
                        "run `lavabo extract` first", missing, schema.version)

        if args.layout == "senkahomes":
            from .load.senkahomes import write_orders_workbook

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

def cmd_check(args, cfg: Config) -> int:
    ok = True

    with Store(cfg.db_path) as store:
        print(f"staging db: {cfg.db_path}")
        print(json.dumps(store.stats(), indent=2))

        try:
            schema = cfg.load_schema()
            print(f"schema:     v{schema.version}, {len(schema.names)} columns "
                  f"({', '.join(schema.names[:6])}{'...' if len(schema.names) > 6 else ''})")
        except Exception as exc:
            print(f"schema:     NOT READY — {exc}")
            ok = False

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

        try:
            for _, conn in _meta_connectors(cfg, store, full=True):
                good, msg = conn.check()
                print(f"{'OK  ' if good else 'FAIL'} {msg}")
                ok &= good
        except Exception as exc:
            print(f"FAIL meta: {exc}")
            ok = False

    return 0 if ok else 1


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
    p.add_argument("--source", choices=["meta", "zalo", "all"], default="all")
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
    p.add_argument("--year", type=int,
                   help="year for NGÀY CHỐT, since headers carry only day/month "
                        "(default: this year)")
    p.add_argument("--status", default="New", help="value for Trạng thái (default: New)")
    p.add_argument("--closer", help="value for Người chốt đơn, e.g. \"Trà My\" — the note "
                                    "does not record who sent it")
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
    cfg = Config.load(args.config)
    _apply_llm_overrides(args, cfg)

    handlers = {"check": cmd_check, "ingest": cmd_ingest, "extract": cmd_extract,
                "load": cmd_load, "verify": cmd_verify, "run": cmd_run,
                "models": cmd_models}
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
