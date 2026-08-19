"""Reading and writing the settings the web page exposes.

The operator should not have to open a YAML file to change a model or paste a key, so
the settings screen writes both `config/config.yaml` and `.env` on their behalf. Two
rules shape everything here:

**The API key is written, never read back.** The page shows whether a key is stored, a
masked hint, and whatever `key_problem()` says about it — but the value itself stays on
the machine. A page with no login should not hand out the one secret it holds, and the
UI has no honest need for it: an empty field on save means "keep what is stored".

**Writing config.yaml rewrites it.** yaml.safe_dump cannot preserve comments, so the
explanatory ones in config.example.yaml are lost the first time the screen saves. That
is an acceptable trade for a shop operator who will never open the file — the example
keeps the documentation — but it is the reason a header is written back in.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .config import REPO_ROOT, Config, _read_yaml, _segmentation_mode
from .extract.base import extractor_class

log = logging.getLogger(__name__)

CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"
ENV_PATH = REPO_ROOT / ".env"

PROVIDERS = ("gemini", "anthropic")
# Who splits a paste into orders -- see src/lavabo/segment.py. Exposed here because the
# choice has a running cost the operator is the one to weigh: off is free and needs no
# key, while shadow and on spend a call per paste and stop capture working offline.
SEGMENTATION_MODES = ("off", "shadow", "on")
HEADER = "# Written by the Lavabo settings screen. Safe to edit by hand.\n"


# ------------------------------------------------------------------------- .env

def _env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def read_env_file() -> dict[str, str]:
    """Parse .env well enough for the keys we manage. Not a full dotenv parser."""
    out: dict[str, str] = {}
    for line in _env_lines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        out[name.strip()] = value.strip().strip('"').strip("'")
    return out


def write_env_var(name: str, value: str) -> None:
    """Set one variable, leaving every other line of .env untouched.

    Rewriting the file from a dict would drop the user's own comments and any variable
    this screen does not know about.
    """
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(name)}\s*=")
    lines = _env_lines()
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    os.environ[name] = value          # live, so a verify right after save sees it


def load_env_into_process() -> None:
    """Apply .env to this process without a restart.

    Values already in the real environment win: someone who exported a key in the shell
    meant that key, and a stale line in .env should not quietly override it.
    """
    for name, value in read_env_file().items():
        os.environ.setdefault(name, value)


# ------------------------------------------------------------------ key status

def _mask(key: str) -> str:
    if len(key) <= 12:
        return "•" * len(key)
    return f"{key[:6]}…{key[-4:]}"


def key_status(provider: str) -> dict[str, Any]:
    """What the page may know about a stored key: present, masked, and any complaint."""
    cls = extractor_class(provider)
    key = cls.api_key()
    if not key:
        return {"set": False, "ok": False, "hint": "", "problem": ""}
    problem = cls.key_problem()
    return {"set": True, "ok": problem is None, "hint": _mask(key), "problem": problem or ""}


def all_key_status() -> dict[str, dict[str, Any]]:
    return {p: key_status(p) for p in PROVIDERS}


def env_var_for(provider: str) -> str:
    """The variable a key is written to — the first one the extractor looks at."""
    return extractor_class(provider).API_KEY_VARS[0]


# ----------------------------------------------------------------- config.yaml

def read_settings() -> dict[str, Any]:
    data = _read_yaml(CONFIG_PATH) if CONFIG_PATH.exists() else {}
    extract = data.get("extract") or {}
    app = data.get("app") or {}
    cfg_defaults = Config()
    # Through the same coercion the loader uses, so a hand-written `ai_segmentation: on`
    # -- which YAML hands over as the boolean True -- shows on the screen as the mode the
    # file plainly says, rather than silently reading as "off".
    mode = (_segmentation_mode(extract["ai_segmentation"])
            if "ai_segmentation" in extract else cfg_defaults.extract.ai_segmentation)
    return {
        "provider": str(extract.get("provider") or cfg_defaults.extract.provider),
        "model": str(extract.get("model") or cfg_defaults.extract.model),
        "segmentation": mode,
        "workbook": str(app.get("workbook") or ""),
        "closer": str(app.get("closer") or ""),
        "keys": all_key_status(),
    }


def write_settings(*, provider: str, model: str, api_key: str,
                   workbook: str, closer: str, segmentation: str = "") -> list[str]:
    """Persist the screen's fields. Returns warnings worth showing, never raises for
    a merely questionable value — the operator can save a path before creating the file."""
    if provider not in PROVIDERS:
        raise ValueError(f"nhà cung cấp không hợp lệ: {provider!r}")

    warnings: list[str] = []
    data = _read_yaml(CONFIG_PATH) if CONFIG_PATH.exists() else {}

    data.setdefault("extract", {})
    data["extract"]["provider"] = provider
    if model:
        data["extract"]["model"] = model
    else:
        warnings.append("Chưa chọn model — giữ nguyên model cũ.")

    if segmentation:
        if segmentation not in SEGMENTATION_MODES:
            raise ValueError(f"chế độ tách đơn không hợp lệ: {segmentation!r}")
        data["extract"]["ai_segmentation"] = segmentation

    workbook = workbook.strip()
    if workbook and not Path(workbook).expanduser().exists():
        warnings.append(f"Chưa thấy file quản lý: {workbook} — nút thêm vào file sẽ bị tắt.")
    data.setdefault("app", {})
    data["app"]["workbook"] = workbook
    data["app"]["closer"] = closer.strip()

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # An empty field means "keep the stored key", so a save from a page that never
    # showed the key cannot wipe it.
    if api_key.strip():
        write_env_var(env_var_for(provider), api_key.strip())

    status = key_status(provider)
    if not status["set"]:
        warnings.append(f"Chưa có API key cho {provider} — trích xuất sẽ báo lỗi.")
    elif status["problem"]:
        warnings.append(status["problem"])

    # Worth saying at the moment of choosing rather than discovering a week later: these
    # modes move an API call into the paste itself, which until now was local and free.
    if segmentation in ("shadow", "on") and not status["ok"]:
        warnings.append(
            "Chế độ tách đơn cần API key hoạt động. Chưa có key thì lưu đơn vẫn chạy "
            + ("bằng quy tắc cũ." if segmentation == "shadow"
               else 'bằng quy tắc cũ và đơn sẽ bị đánh dấu "chưa qua AI".'))
    elif segmentation == "on":
        warnings.append("Từ giờ mỗi lần dán sẽ gọi AI một lần để tách đơn.")

    return warnings


# -------------------------------------------------------------------- probing

def verify_key(provider: str, api_key: str = "") -> tuple[bool, str]:
    """Ask the provider whether a key works, without storing it.

    A key typed into the box is checked as typed, so it can be rejected before it
    replaces a working one.
    """
    cls = extractor_class(provider)
    var = env_var_for(provider)
    previous = os.environ.get(var)
    try:
        if api_key.strip():
            os.environ[var] = api_key.strip()
        return cls.verify_api_key()
    finally:
        if previous is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = previous


def list_models(provider: str, api_key: str = "") -> list[str]:
    cls = extractor_class(provider)
    var = env_var_for(provider)
    previous = os.environ.get(var)
    try:
        if api_key.strip():
            os.environ[var] = api_key.strip()
        return cls.list_models()
    finally:
        if previous is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = previous
