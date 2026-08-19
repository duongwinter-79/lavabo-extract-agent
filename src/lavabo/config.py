"""Config + column-schema loading.

`schema.yaml` is the single source of truth for the output columns: it drives both the
JSON schema sent to the LLM and the Excel header row. Adding a column is a YAML edit.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_yaml(path: Path) -> dict[str, Any]:
    """Parse a config file, turning YAML's own errors into something actionable.

    The trap this exists for is a Windows path in double quotes: YAML reads "\\U" in
    "C:\\Users\\..." as the start of an escape, and the whole file fails to load with a
    scanner error pointing at a column number. Somebody editing config.yaml to name
    their workbook should not have to know that.
    """
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        hint = ""
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        if "\\" in path.read_text(encoding="utf-8"):
            hint = ("\n  A Windows path in \"double quotes\" is the usual cause: YAML "
                    "treats \\ as an escape.\n"
                    "  Use forward slashes — C:/Users/You/file.xlsx — or 'single quotes'.")
        raise ValueError(f"{path.name} is not valid YAML{where}.{hint}") from None

SEGMENTATION_MODES = ("off", "shadow", "on")


def _segmentation_mode(value: Any) -> str:
    """Read ai_segmentation, surviving YAML's opinion about the words "on" and "off".

    YAML 1.1 -- which PyYAML implements -- resolves bare `on` to True and `off` to False.
    So a perfectly natural hand edit,

        ai_segmentation: on

    arrives here as the boolean True, matches none of the mode names, and would silently
    leave segmentation switched off while the file plainly says it is on. That is the
    exact failure this whole feature exists to stamp out, so the booleans are accepted as
    the words they were written as. An unrecognised value warns rather than passing
    through, since a typo must not read as a deliberate "off".
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    text = str(value or "").strip().lower()
    if text in SEGMENTATION_MODES:
        return text
    if text:
        import logging
        logging.getLogger(__name__).warning(
            "ai_segmentation: %r is not one of %s — treating it as 'off'",
            value, ", ".join(SEGMENTATION_MODES))
    return "off"


JSON_TYPES = {"string": "string", "number": "number", "integer": "integer",
              "boolean": "boolean", "date": "string", "array": "array"}


@dataclass(slots=True)
class ItemProperty:
    """One field inside an object_array entry, e.g. a line item's name or quantity."""
    name: str
    type: str = "string"
    description: str = ""


@dataclass(slots=True)
class Column:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    enum: list[str] | None = None
    examples: list[str] = field(default_factory=list)
    # Only for type: object_array -- the shape of each entry.
    item_properties: list[ItemProperty] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.item_properties = [
            p if isinstance(p, ItemProperty) else ItemProperty(**p)
            for p in self.item_properties
        ]

    def json_property(self) -> dict[str, Any]:
        # object_array carries structure a flat array cannot: line items pair a name
        # with a quantity, and two parallel string arrays would let them drift apart.
        if self.type == "object_array":
            if not self.item_properties:
                raise ValueError(f"column {self.name!r}: object_array needs item_properties")
            return {
                "type": ["array", "null"],
                "description": self.description,
                "items": {
                    "type": "object",
                    "properties": {
                        p.name: {"type": [JSON_TYPES.get(p.type, "string"), "null"],
                                 "description": p.description}
                        for p in self.item_properties
                    },
                    "required": [p.name for p in self.item_properties],
                    "additionalProperties": False,
                },
            }

        if self.type not in JSON_TYPES:
            raise ValueError(f"column {self.name!r}: unknown type {self.type!r}")
        prop: dict[str, Any] = {"description": self.description}
        # Nullable everywhere: the model must be able to say "not stated" instead of guessing.
        prop["type"] = [JSON_TYPES[self.type], "null"]
        if self.type == "date":
            prop["description"] += " Format: YYYY-MM-DD. Null if not stated."
        if self.type == "array":
            prop["items"] = {"type": "string"}
        if self.enum:
            prop["enum"] = [*self.enum, None]
        return prop


@dataclass(slots=True)
class ExtractionSchema:
    version: int
    columns: list[Column]
    instructions: str = ""

    @classmethod
    def load(cls, path: Path) -> "ExtractionSchema":
        data = _read_yaml(path)
        cols = [Column(**c) for c in data.get("columns", [])]
        if not cols:
            raise ValueError(f"{path}: no columns defined")
        names = [c.name for c in cols]
        if len(names) != len(set(names)):
            raise ValueError(f"{path}: duplicate column names")
        return cls(
            version=int(data.get("schema_version", 1)),
            columns=cols,
            instructions=(data.get("instructions") or "").strip(),
        )

    def json_schema(self) -> dict[str, Any]:
        """The object schema handed to Claude (as tool input_schema) or Gemini."""
        return {
            "type": "object",
            "properties": {c.name: c.json_property() for c in self.columns},
            "required": [c.name for c in self.columns],  # present-but-null, never missing
            "additionalProperties": False,
        }

    def fingerprint(self) -> str:
        """Hash of what the model is actually asked for.

        schema_version alone is not a safe cache key: it is a number a human maintains,
        and two entirely different schema files can both sit at version 1. Swapping
        schema.yaml then silently served results extracted for the previous columns.
        Hashing the real definitions -- names, types, descriptions, instructions --
        means any change that could alter an answer invalidates the cache by itself.
        """
        payload = json.dumps(
            {"schema": self.json_schema(), "instructions": self.instructions},
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass(slots=True)
class MetaConfig:
    page_id: str = ""
    instagram_id: str = ""
    api_version: str = "v21.0"
    platforms: list[str] = field(default_factory=lambda: ["messenger"])
    page_size: int = 50
    message_page_size: int = 100
    max_conversations: int | None = None

    @property
    def access_token(self) -> str:
        token = os.environ.get("META_PAGE_TOKEN", "")
        if not token:
            raise RuntimeError("META_PAGE_TOKEN is not set (see .env.example)")
        return token


@dataclass(slots=True)
class ZaloConfig:
    inbox_dir: Path = REPO_ROOT / "data" / "inbox" / "zalo"
    timezone: str = "Asia/Ho_Chi_Minh"
    own_names: list[str] = field(default_factory=list)
    # Tune against a real export -- see docs/03-zalo-runbook.md.
    line_patterns: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExtractConfig:
    provider: str = "anthropic"                 # anthropic | gemini
    model: str = "claude-opus-5"
    max_tokens: int = 4096
    temperature: float = 0.0
    concurrency: int = 4
    max_transcript_chars: int = 120_000
    # Transcripts are stored in UTC but shown to the model in this zone, so extracted
    # times match what a human sees in Zalo / Business Suite.
    display_timezone: str = "Asia/Ho_Chi_Minh"
    # off | shadow | on -- see src/lavabo/segment.py.
    #   off     the regexes in scripts/zalo_capture.py decide, alone. Capture stays
    #           local, instant and free, and needs no API key.
    #   shadow  both run, the REGEX RESULT IS STILL USED, and differences are reported.
    #           Costs one call per paste and changes no output, so it answers "how often
    #           does the model disagree with us, on our own chat" before anything relies
    #           on the answer.
    #   on      the model decides, with the regexes kept as a fallback and a cross-check.
    ai_segmentation: str = "off"


@dataclass(slots=True)
class Config:
    meta: MetaConfig = field(default_factory=MetaConfig)
    zalo: ZaloConfig = field(default_factory=ZaloConfig)
    extract: ExtractConfig = field(default_factory=ExtractConfig)
    db_path: Path = REPO_ROOT / "data" / "staging.db"
    output_dir: Path = REPO_ROOT / "data" / "out"
    schema_path: Path = REPO_ROOT / "config" / "schema.yaml"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or REPO_ROOT / "config" / "config.yaml"
        if not path.exists():
            return cls()
        data = _read_yaml(path)
        cfg = cls()
        if m := data.get("meta"):
            cfg.meta = MetaConfig(**m)
        if z := data.get("zalo"):
            z = dict(z)
            if "inbox_dir" in z:
                z["inbox_dir"] = Path(z["inbox_dir"])
            cfg.zalo = ZaloConfig(**z)
        if e := data.get("extract"):
            e = dict(e)
            if "ai_segmentation" in e:
                e["ai_segmentation"] = _segmentation_mode(e["ai_segmentation"])
            cfg.extract = ExtractConfig(**e)
        for key in ("db_path", "output_dir", "schema_path"):
            if key in data:
                setattr(cfg, key, Path(data[key]))
        return cfg

    def load_schema(self) -> ExtractionSchema:
        if not self.schema_path.exists():
            raise FileNotFoundError(
                f"{self.schema_path} not found. Copy config/schema.example.yaml to "
                "config/schema.yaml and define your columns (docs/05-schema-guide.md)."
            )
        return ExtractionSchema.load(self.schema_path)
