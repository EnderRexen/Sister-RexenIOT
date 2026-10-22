#!/usr/bin/env python3
"""Static checks for the launchers.

cmd.exe is not available on the machines this runs on, so these are the mistakes
that can be caught without executing the script. Each check below maps to a
failure that is otherwise silent on Windows -- batch reports very little when it
mis-parses a file.

The last two checks are about drift rather than syntax. The launchers used to
carry their own copy of the startup pipeline, and the copies fell out of step:
the GPU advice was added to the main launchers but never reached the separate
portable launchers that once existed, so an install on a machine with a discrete
GPU was told nothing about using it. Those steps now live in scripts/bootstrap.py, and the checks
below assert that each launcher still delegates to it instead of growing a
private copy again.

    python scripts/check_launchers.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The two user-facing entry points. Both must hand off to bootstrap.py.
BATCH = ["start.cmd"]
SHELL = ["start.sh"]

#: The sandbox's own supervisor, which is not a user entry point: it runs the
#: server directly rather than through bootstrap, so the delegation checks below
#: deliberately do not apply to it.
EXTRA_SHELL = ["scripts/keep-alive.sh"]

#: Steps that must exist in exactly one place -- bootstrap.py -- and never be
#: reimplemented in a launcher. Each entry is (label, pattern).
SHARED_STEPS = [
    ("dependency install", r"pip\s+install\s+-r"),
    ("speech package install", r"kittenml=="),
    ("TTS warm-up", r"--prepare-tts"),
    ("GPU advice", r"torch_gpu_advice|directml_advice"),
]

#: Content that must never appear in a launcher. Checked separately from
#: SHARED_STEPS because these are the *effects* of the shared steps: a launcher
#: could reproduce the advice verbatim without calling any of the functions
#: above, which is precisely how the removed portable launchers fell behind. `app.main` is
#: included because only bootstrap.py may start the server.
LAUNCHER_FORBIDDEN = [
    (r"pip\s+install", "runs pip; the dependency step belongs in bootstrap.py"),
    (r"kittenml==", "installs the speech package; that belongs in bootstrap.py"),
    (r"--prepare-tts", "warms up speech; that belongs in bootstrap.py"),
    (r"requirements-(gpu|directml)", "carries GPU install advice; that belongs in bootstrap.py"),
    (r"cu130", "names a CUDA wheel index; that belongs in bootstrap.py"),
    (r"app\.main", "starts the server directly; only bootstrap.py may do that"),
    (r"torch_gpu_advice|directml_advice", "calls the GPU advice directly; use bootstrap.py"),
]

#: The GPU advice content, which belongs to app/hardware.py alone. Checked
#: separately because bootstrap.py calls into it rather than restating it.
GPU_ADVICE_OWNER = "app/hardware.py"
GPU_ADVICE_MARKERS = ["requirements-gpu", "requirements-directml", "cu130"]

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


def _code_lines(text: str, *, batch: bool) -> str:
    """The executable lines of a launcher, with comments stripped.

    A launcher's header explains what bootstrap.py does, and those mentions are
    prose, not a hand-off. Matching the raw text would let a launcher that never
    calls bootstrap pass the delegation check on a comment alone.
    """
    if batch:
        kept = [ln for ln in text.splitlines()
                if not re.match(r"\s*(REM\b|::)", ln, re.I)]
    else:
        kept = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(kept)


def check_forbidden(name: str, text: str, *, batch: bool) -> None:
    """A launcher must not reimplement any of bootstrap.py's steps."""
    code = _code_lines(text, batch=batch)
    for pattern, message in LAUNCHER_FORBIDDEN:
        if re.search(pattern, code, re.I):
            report(name, message)


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

    # 7. Sanity: the file should actually launch something. It does that by
    #    handing off to bootstrap.py, which owns the pipeline. Only real code
    #    counts -- a comment naming bootstrap.py is not a hand-off.
    if "bootstrap.py" not in _code_lines(text, batch=True):
        report(name, "does not invoke scripts/bootstrap.py; nothing would start")

    # 8. No step may be reimplemented here. Each one lives in bootstrap.py, and
    #    a second copy is how the two launcher families drifted apart before.
    check_forbidden(name, text, batch=True)

    # 9. A variable holding a command plus arguments must not be quoted.
    #    `set "PYEXE=py -3"` then `"%PYEXE%"` makes cmd.exe look for one program
    #    named `py -3`, which does not exist; the same variable unquoted works.
    #    A variable holding a bare path must be quoted, or a folder with a space
    #    in it breaks. So the rule is decided by the value, not by style.
    for var, value in re.findall(r'set\s+"(\w+)=([^"]*)"', text, re.I):
        if re.search(r"\s", value) and "\\" not in value and "/" not in value:
            if f'"%{var}%"' in text:
                report(name, f'"{var}" holds "{value}" (a command with arguments) '
                             f"but is used quoted; cmd.exe would look for a program "
                             f'literally named "{value}". Use %{var}% unquoted.')

    notes.append(f"{name}: {len(lines)} lines, CRLF ok, "
                 f"{len(re.findall(r'goto', text))} goto(s), {len(labels)} label(s)")


def check_shell(name: str, *, delegates: bool = True) -> None:
    path = ROOT / name
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    if raw.count(b"\r\n"):
        report(name, "contains CRLF; bash needs LF or the shebang fails")
    if not raw.startswith(b"#!"):
        report(name, "missing shebang")
    if delegates:
        if "bootstrap.py" not in _code_lines(text, batch=False):
            report(name, "does not invoke scripts/bootstrap.py; nothing would start")
        check_forbidden(name, text, batch=False)
        notes.append(f"{name}: LF ok, shebang ok, delegates to bootstrap.py")
    else:
        if "app.main" not in text:
            report(name, "never runs app.main; it does not start the app")
        notes.append(f"{name}: LF ok, shebang ok")


def check_bootstrap() -> None:
    """bootstrap.py is the one file allowed to contain the shared steps.

    If a step is missing from here it exists nowhere, which is the failure mode
    the delegation checks above would not catch.
    """
    name = "scripts/bootstrap.py"
    path = ROOT / name
    if not path.exists():
        report(name, "file is missing; no launcher can start the app")
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    for label, pattern in SHARED_STEPS:
        if not re.search(pattern, text, re.I):
            report(name, f"does not implement the {label}; no launcher does now")
    notes.append(f"{name}: owns {len(SHARED_STEPS)} shared step(s)")


def check_gpu_advice_owner() -> None:
    """The GPU advice must live in app/hardware.py, where the panel reads it.

    bootstrap.py and the admin panel both call these functions, so the rule has
    one definition. If the content is ever moved out of hardware.py, one of the
    two readers is left with a stale copy -- which is the drift this whole check
    exists to prevent.
    """
    path = ROOT / GPU_ADVICE_OWNER
    if not path.exists():
        report(GPU_ADVICE_OWNER, "file is missing; GPU advice has no owner")
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    missing = [m for m in GPU_ADVICE_MARKERS if m not in text]
    if missing:
        report(GPU_ADVICE_OWNER,
               f"no longer defines the GPU advice ({', '.join(missing)}); "
               "bootstrap.py and the admin panel both read it from here")
    for fn in ("torch_gpu_advice", "directml_advice"):
        if f"def {fn}" not in text:
            report(GPU_ADVICE_OWNER, f"missing {fn}()")
    notes.append(f"{GPU_ADVICE_OWNER}: owns the GPU advice")


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
    for name in EXTRA_SHELL:
        if not (ROOT / name).exists():
            report(name, "file is missing")
            continue
        check_shell(name, delegates=False)
    check_bootstrap()
    check_gpu_advice_owner()

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
