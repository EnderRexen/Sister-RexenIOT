#!/usr/bin/env python3
"""Static checks for the Windows launchers.

cmd.exe is not available on the machines this runs on, so these are the mistakes
that can be caught without executing the script. Each check below maps to a
failure that is otherwise silent on Windows -- batch reports very little when it
mis-parses a file.

    python scripts/check_launchers.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BATCH = ["start.cmd", "start-portable.cmd"]
SHELL = ["start.sh", "start-portable.sh", "scripts/keep-alive.sh"]

#: Overwriting any of these changes behaviour for child processes: TMP/TEMP
#: redirect where temp files go, PATH breaks every command lookup.
RESERVED = {
    "TMP", "TEMP", "PATH", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "HOMEPATH", "HOMEDRIVE", "COMSPEC", "WINDIR", "SYSTEMROOT", "SYSTEMDRIVE",
    "PROGRAMFILES", "PROGRAMDATA", "CD", "DATE", "TIME", "RANDOM",
}

problems: list[str] = []
notes: list[str] = []


def report(fname: str, msg: str) -> None:
    problems.append(f"{fname}: {msg}")


def check_batch(name: str) -> None:
    path = ROOT / name
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\r\n")

    # 1. CRLF. A LF-only batch file mis-parses goto, blocks and for loops.
    lone_lf = raw.count(b"\n") - raw.count(b"\r\n")
    if lone_lf:
        report(name, f"{lone_lf} LF-only line ending(s); batch files must be CRLF")
    if raw.startswith(b"\xef\xbb\xbf"):
        report(name, "has a UTF-8 BOM; cmd.exe treats it as part of the first command")

    # 2. Reserved variable clobbering.
    for i, line in enumerate(lines, 1):
        m = re.match(r'\s*set\s+"?([A-Za-z_][A-Za-z0-9_]*)=', line, re.I)
        if m and m.group(1).upper() in RESERVED:
            report(name, f"line {i}: overwrites reserved variable {m.group(1).upper()}")

    # 3. goto targets must exist as labels.
    labels = {ln.strip()[1:].strip().lower() for ln in lines if ln.strip().startswith(":")}
    for i, line in enumerate(lines, 1):
        for target in re.findall(r"goto\s+:?([A-Za-z0-9_]+)", line, re.I):
            if target.lower() not in labels:
                report(name, f"line {i}: goto :{target} has no matching label")

    # 4. Caret continuation followed by a trailing space silently breaks the
    #    command, and a caret on the final line swallows the newline.
    for i, line in enumerate(lines, 1):
        if re.search(r"\^\s+$", line):
            report(name, f"line {i}: caret continuation followed by trailing space")
        if line.rstrip().endswith("^") and i == len(lines):
            report(name, f"line {i}: file ends with a caret continuation")

    # 5. Parenthesis balance. `) else (` closes one block and opens another, so
    #    it nets to zero -- counting it as an open would flag every if/else.
    #    While inside a block, *every* unescaped paren is also a hazard: an
    #    unescaped `)` closes the block early and cmd.exe mis-parses the rest of
    #    the file. Microsoft documents this: "When inside a block terminated by
    #    parentheses, both opening and closing parentheses must also be escaped
    #    using the caret". Balance alone does not catch it, so check both.
    depth = 0
    block_start = 0
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("REM") or stripped.startswith("::"):
            continue
        opens = stripped.endswith("(") and not stripped.endswith("^(")
        closes = stripped == ")" or stripped.startswith(") else")
        interior = stripped[:-1] if opens else stripped
        if closes:
            interior = interior.lstrip()[1:]
        # A `for /f ... in ('cmd')` carries its command in parentheses that belong
        # to the for-syntax, not to a block. Everything before `do` is syntax, so
        # scan only the body. Quoted strings are blanked for the same reason.
        if re.match(r"for\b", stripped, re.I):
            interior = re.split(r"\bdo\b", stripped, maxsplit=1)[-1]
        probe = re.sub(r"'[^']*'", "", interior)
        probe = re.sub(r'"[^"]*"', "", probe)
        probe = probe.replace("^(", "").replace("^)", "")
        if depth > 0:
            bad = re.findall(r"(?<!\^)[()]", probe)
            if bad:
                report(name, f"line {i}: unescaped {' '.join(sorted(set(bad)))} "
                             f"inside a parenthesised block (needs ^( ^) )")
        if closes:
            depth -= 1
            if depth < 0:
                report(name, f"line {i}: unmatched closing parenthesis")
                depth = 0
        if opens:
            if depth == 0:
                block_start = i
            depth += 1
    if depth != 0:
        report(name, f"unbalanced parentheses: {depth} block(s) left open (from line {block_start})")

    # 6. setlocal without endlocal leaks the localisation, but worse, an
    #    unpaired setlocal is usually a sign the file was truncated.
    has_setlocal = any(re.match(r"\s*setlocal\b", ln, re.I) for ln in lines)
    has_endlocal = any(re.match(r"\s*endlocal\b", ln, re.I) for ln in lines)
    if has_setlocal and not has_endlocal:
        report(name, "setlocal with no matching endlocal")

    # 7. Sanity: the file should actually launch something.
    if "app.main" not in text:
        report(name, "never runs app.main; it does not start the app")

    notes.append(f"{name}: {len(lines)} lines, CRLF ok, "
                 f"{len(re.findall(r'goto', text))} goto(s), {len(labels)} label(s)")


def check_shell(name: str) -> None:
    path = ROOT / name
    raw = path.read_bytes()
    if raw.count(b"\r\n"):
        report(name, "contains CRLF; bash needs LF or the shebang fails")
    if not raw.startswith(b"#!"):
        report(name, "missing shebang")
    if b"app.main" not in raw:
        report(name, "never runs app.main; it does not start the app")
    notes.append(f"{name}: LF ok, shebang ok")


def main() -> int:
    for name in BATCH:
        if not (ROOT / name).exists():
            report(name, "file is missing")
            continue
        check_batch(name)
    for name in SHELL:
        if not (ROOT / name).exists():
            report(name, "file is missing")
            continue
        check_shell(name)

    for n in notes:
        print(f"  ok   {n}")
    if problems:
        print()
        for p in problems:
            print(f"  FAIL {p}")
        print(f"\n{len(problems)} problem(s) found")
        return 1
    print("\nno problems found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
