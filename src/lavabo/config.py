"""Config + column-schema loading.

`schema.yaml` is the single source of truth for the output columns: it drives both the
JSON schema sent to the LLM and the Excel header row. Adding a column is a YAML edit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

JSON_TYPES = {"string": "string", "number": "number", "integer": "integer",
              "boolean": "boolean", "date": "string", "array": "array"}


@dataclass(slots=True)
class Column:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    enum: list[str] | None = None
    examples: list[str] = field(default_factory=list)

    def json_property(self) -> dict[str, Any]:
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
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = cls()
        if m := data.get("meta"):
            cfg.meta = MetaConfig(**m)
        if z := data.get("zalo"):
            z = dict(z)
            if "inbox_dir" in z:
                z["inbox_dir"] = Path(z["inbox_dir"])
            cfg.zalo = ZaloConfig(**z)
        if e := data.get("extract"):
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
