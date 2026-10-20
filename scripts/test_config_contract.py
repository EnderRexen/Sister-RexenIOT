"""Verify every frontend setting maps onto a real backend config field.

The admin page sends a patch built from its ``data-cfg`` attributes, and
``AppConfig`` ignores unknown keys. A typo therefore saves "successfully" and
does nothing, with no error anywhere. This walks the HTML, resolves each key
against the real schema, and reports anything that would be silently dropped.

It also checks each ``<select>``'s option values against the field's type, since
a value the schema rejects is a 400 the operator only sees on save.

Run: PYTHONPATH=. ./.venv/bin/python scripts/test_config_contract.py
"""
from __future__ import annotations

import re
import sys
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import AppConfig  # noqa: E402

HTML = ROOT / "web" / "admin.html"

#: Keys the frontend sends that are deliberately not config fields: the token is
#: handled specially by put_config, and "-list" suffixes are stripped before the
#: patch is built.
IGNORED = {"admin_token"}

failures: list[str] = []
notes: list[str] = []


def field_map(model: type, prefix: str = "") -> dict[str, typing.Any]:
    """Flatten a pydantic model into {"a.b": annotation}."""
    out: dict[str, typing.Any] = {}
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        out[path] = info.annotation
        ann = info.annotation
        if isinstance(ann, type) and hasattr(ann, "model_fields"):
            out.update(field_map(ann, f"{path}."))
    return out


def literal_values(ann: typing.Any) -> set[str] | None:
    """Extract allowed values from a Literal, unwrapping Optional/Union."""
    origin = typing.get_origin(ann)
    if origin is typing.Union:
        for arg in typing.get_args(ann):
            found = literal_values(arg)
            if found:
                return found
        return None
    if origin is typing.Literal:
        return {str(v) for v in typing.get_args(ann)}
    return None


def main() -> int:
    html = HTML.read_text(encoding="utf-8")
    fields = field_map(AppConfig)

    keys = sorted(set(re.findall(r'data-cfg="([^"]+)"', html)))
    print(f"frontend data-cfg keys: {len(keys)}")
    print(f"backend config fields : {len(fields)}\n")

    for key in keys:
        bare = key.replace("-list", "")
        if bare in IGNORED:
            notes.append(f"{key}: handled specially, not a config field")
            continue
        if bare not in fields:
            failures.append(
                f"{key}: no such config field - the patch is silently dropped"
            )
        elif bare != key:
            notes.append(f"{key}: '-list' suffix stripped to {bare} (valid)")

    # Select options vs the field's Literal.
    print("checking <select> options against field types")
    for m in re.finditer(r"<select\b([^>]*)>(.*?)</select>", html, re.S):
        attrs, body = m.group(1), m.group(2)
        cfg = re.search(r'data-cfg="([^"]+)"', attrs)
        if not cfg:
            continue
        key = cfg.group(1).replace("-list", "")
        if key not in fields:
            continue
        allowed = literal_values(fields[key])
        if allowed is None:
            continue
        for val in re.findall(r'<option\s+value="([^"]*)"', body):
            if val and val not in allowed:
                failures.append(
                    f"{key}: option {val!r} not in allowed {sorted(allowed)}"
                )
        print(f"  {key:32} options ok (allowed: {sorted(allowed)})")

    print()
    for n in notes:
        print(f"  note: {n}")
    if failures:
        print()
        for f in failures:
            print(f"  FAIL: {f}")
        print(f"\n{len(failures)} problem(s)")
        return 1
    print(f"\nPASS: all {len(keys)} frontend keys resolve to real config fields")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
