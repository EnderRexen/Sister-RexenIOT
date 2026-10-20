"""Verify voice profiles are vetted before they can be saved or applied.

The point of a profile is that a saved combination is *known to work*, so the
checks that matter are the ones that would let a broken one through:

- a typo'd key must be rejected, not silently dropped (AppConfig ignores extras,
  so without validation a profile would save cleanly and do nothing)
- a value outside the schema must be rejected
- a profile must not reach outside its own sections, or a saved "voice" profile
  could rewrite the LLM endpoint or the admin token
- a built-in must not be deletable
- applying must not persist a model that cannot load

Run: PYTHONPATH=. ./.venv/bin/python scripts/test_profiles.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, profiles  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


print("built-in profiles")
builtins = [p for p in profiles.load() if p.builtin]
check(len(builtins) >= 5, f"at least five built-ins ({len(builtins)} found)")
check(all(p.id for p in builtins), "every built-in has an id")
for p in builtins:
    problems = profiles.validate(p.settings)
    check(not problems, f"built-in '{p.id}' validates ({problems})")

print("\nevery built-in is a valid patch for the real config")
for p in builtins:
    try:
        base = config.load().model_dump()
        for section, values in p.settings.items():
            base[section] = {**base.get(section, {}), **values}
        config.AppConfig.model_validate(base)
        check(True, f"'{p.id}' can be applied to the current config")
    except Exception as exc:
        check(False, f"'{p.id}' cannot be applied: {exc}")

print("\nvalidation rejects what must not be saved")
cases = [
    ("typo'd key", {"tts": {"voxel": "Bella"}}, False),
    ("bad enum value", {"tts": {"engine": "telepathy"}}, False),
    ("out-of-section key", {"llm": {"api_key": "x"}}, False),
    ("out-of-section tools", {"tools": {"allow_shell": True}}, False),
    ("non-object section", {"tts": "Bella"}, False),
    ("empty patch", {}, False),
    ("valid engine", {"tts": {"engine": "browser"}}, True),
    ("valid voice+speed", {"tts": {"voice": "Kiki", "speed": 0.9}}, True),
    ("valid voice_input", {"voice_input": {"language": "en-GB"}}, True),
]
for label, patch, should_pass in cases:
    problems = profiles.validate(patch)
    ok = (not problems) == should_pass
    detail = "" if ok else f" -> {problems}"
    check(ok, f"{label}{detail}")

print("\nsaving")
try:
    saved = profiles.save("Test profile", {"tts": {"engine": "browser", "voice": "Rosie"}}, "tmp")
    check(saved.id and not saved.builtin, "a custom profile saves and is not marked built-in")
    check(saved.id != "balanced", "a custom id cannot collide with a built-in id")
except Exception as exc:
    check(False, f"saving a valid profile raised: {exc}")
    saved = None

try:
    profiles.save("Bad", {"tts": {"nope": 1}})
    check(False, "saving an invalid profile is rejected")
except ValueError as exc:
    check("nope" in str(exc), "saving an invalid profile raises with the offending key")

try:
    profiles.save("", {"tts": {"engine": "model"}})
    check(False, "a nameless profile is rejected")
except ValueError:
    check(True, "a nameless profile is rejected")

print("\nbuilt-ins are protected")
check(profiles.delete("balanced") is False, "a built-in cannot be deleted")
check(profiles.get("balanced") is not None, "and it is still there afterwards")
if saved:
    check(profiles.delete(saved.id) is True, "a custom profile can be deleted")
    check(profiles.get(saved.id) is None, "and is gone afterwards")

print("\ncapture reflects only what differs from the defaults")
defaults = config.AppConfig()
patch = profiles.capture(defaults)
check(patch == {}, "the stock config captures as an empty patch")
modified = defaults.model_copy(deep=True)
modified.tts.engine = "browser"
modified.tts.voice = "Kiki"
patch = profiles.capture(modified)
check(patch.get("tts", {}).get("engine") == "browser", "a changed engine is captured")
check(patch.get("tts", {}).get("voice") == "Kiki", "a changed voice is captured")
check("model_id" not in patch.get("tts", {}), "an unchanged model_id is not captured")

print("\ncapture round-trips through validate")
check(not profiles.validate(patch), "what capture produces is itself valid")

print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
