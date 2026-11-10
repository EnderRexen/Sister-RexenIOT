"""The desktop tools parse .desktop files and Exec lines the way the spec says.

The tools themselves need a live KDE session, but the part that actually breaks
is the parsing, and that needs no desktop at all. A .desktop file is an INI-ish
format with real traps: `Name[de]=...` localised keys, `NoDisplay=true` entries
that must stay hidden, `Type=Link` entries that are not applications, and Exec
lines carrying `%f`/`%U` field codes that are not real arguments. Passing one
through unchanged makes the launch fail with a confusing error, so this checks
the codes are stripped and that a quoted command with spaces survives intact.

Also covers name resolution, where a partial name must resolve when it is
unambiguous and refuse rather than guess when it is not.

Run: PYTHONPATH=. ./.venv/bin/python scripts/test_desktop_tools.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tools import app_control as ac  # noqa: E402
from app.tools.base import ToolError  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def raises(fn, label: str) -> None:
    try:
        fn()
    except ToolError:
        check(True, label)
        return
    check(False, label)


ENTRIES = {
    "kate.desktop": """[Desktop Entry]
Type=Application
Name=Kate
Name[de]=Kate
Exec=kate %F
Icon=kate
""",
    "spaced.desktop": """[Desktop Entry]
Type=Application
Name=Space App
Exec="/opt/my app/run.sh" --flag %U
""",
    "hidden.desktop": """[Desktop Entry]
Type=Application
Name=Secret Tool
Exec=secret
NoDisplay=true
""",
    "link.desktop": """[Desktop Entry]
Type=Link
Name=A Website
URL=https://example.com
""",
    "broken.desktop": """[Desktop Entry]
Type=Application
Name=No Command
""",
    "two-alpha.desktop": """[Desktop Entry]
Type=Application
Name=Alpha One
Exec=alpha1
""",
    "two-beta.desktop": """[Desktop Entry]
Type=Application
Name=Alpha Two
Exec=alpha2
""",
}

tmp = Path(tempfile.mkdtemp(prefix="sister-desktop-"))
for name, body in ENTRIES.items():
    (tmp / name).write_text(body, encoding="utf-8")

# Point the scanner at the fixture instead of the real application folders.
ac._DESKTOP_DIRS = (str(tmp),)

print("desktop entry parsing")
apps = ac._desktop_entries()
check("kate" in apps, "a plain application is found by its file stem")
check(apps["kate"]["Name"] == "Kate", "its Name is read from the entry")
check("space app" in apps, "an application is also found by its display name")
check("secret tool" not in apps, "a NoDisplay entry is skipped")
check("a website" not in apps, "a Type=Link entry is skipped")
check("no command" not in apps, "an entry with no Exec is skipped")

print("\nExec line handling")
check(ac._exec_argv("kate %F") == ["kate"], "%F is stripped from the command")
check(ac._exec_argv('"/opt/my app/run.sh" --flag %U') == ["/opt/my app/run.sh", "--flag"],
      "a quoted path with spaces survives and %U is stripped")
check(ac._exec_argv("env A=1 app %i") == ["env", "A=1", "app"],
      "an argument containing '=' is kept, not treated as a field code")
raises(lambda: ac._exec_argv("%U"), "a command that is only field codes is rejected")

print("\nname resolution")
slug, entry = ac._resolve_app("Kate")
check(slug == "kate" and entry["Name"] == "Kate", "an exact name resolves")
slug, entry = ac._resolve_app("  SPACE APP ")
check(entry["Name"] == "Space App", "resolution ignores case and surrounding space")
raises(lambda: ac._resolve_app("alpha"), "an ambiguous partial name is refused")
raises(lambda: ac._resolve_app("nothing-here"), "an unknown name is refused")

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("all checks passed")
