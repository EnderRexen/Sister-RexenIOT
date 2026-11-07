#!/usr/bin/env python3
"""Prompt injection into an operator-supplied ComfyUI workflow.

An operator can point the app at their own exported workflow instead of the
built-in graph, and the model only supplies a prompt string. Splicing that
string into the right node is the whole job, and getting it wrong is silent: the
image still renders, just from the wrong prompt.

The trap is that a workflow exported before the user typed anything has *both*
text nodes empty, so the nodes cannot be told apart by their contents. The only
reliable signal is which node the sampler reads: ComfyUI wires ``KSampler`` to
its two text nodes through ``inputs.positive`` / ``inputs.negative``.

    PYTHONPATH=. python scripts/test_comfyui_workflow.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools.comfyui import _inject_prompt  # noqa: E402

PROMPT = "a red fox in snow"
NEGATIVE = "blurry, watermark"

#: Deliberately not the built-in graph's node ids ("4".."9"). The old code fell
#: back to that graph whenever it could not identify a positive node, and using
#: the same ids let a test pass on the fallback instead of on the splice.
POS_ID = "11"
NEG_ID = "12"
SAMPLER_ID = "13"


def text_of(graph: dict, node_id: str) -> str:
    return graph[node_id]["inputs"]["text"]


def case_two_nodes_empty_positive() -> str | None:
    """Both fields empty -- the case that made the old heuristic swap them."""
    graph = {
        POS_ID: {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        NEG_ID: {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        SAMPLER_ID: {
            "class_type": "KSampler",
            "inputs": {"positive": [POS_ID, 0], "negative": [NEG_ID, 0]},
        },
    }
    out = _inject_prompt(graph, PROMPT, NEGATIVE)
    if POS_ID not in out or NEG_ID not in out:
        return "the operator's own graph was discarded"
    if text_of(out, POS_ID) != PROMPT:
        return f"positive node got {text_of(out, POS_ID)!r}, wanted {PROMPT!r}"
    if text_of(out, NEG_ID) != NEGATIVE:
        return f"negative node got {text_of(out, NEG_ID)!r}, wanted {NEGATIVE!r}"
    return None


def case_empty_positive_filled_negative() -> str | None:
    """Empty positive, pre-filled negative: the swap seen in practice."""
    graph = {
        POS_ID: {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        NEG_ID: {"class_type": "CLIPTextEncode", "inputs": {"text": "lowres, jpeg"}},
        SAMPLER_ID: {
            "class_type": "KSampler",
            "inputs": {"positive": [POS_ID, 0], "negative": [NEG_ID, 0]},
        },
    }
    out = _inject_prompt(graph, PROMPT, NEGATIVE)
    if text_of(out, POS_ID) != PROMPT:
        return f"positive node got {text_of(out, POS_ID)!r}, wanted {PROMPT!r}"
    if text_of(out, NEG_ID) != NEGATIVE:
        return f"negative node got {text_of(out, NEG_ID)!r}, wanted {NEGATIVE!r}"
    return None


def case_wiring_beats_declaration_order() -> str | None:
    """The positive node is declared second; the wiring must win over order."""
    graph = {
        NEG_ID: {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        POS_ID: {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        SAMPLER_ID: {
            "class_type": "KSampler",
            "inputs": {"positive": [POS_ID, 0], "negative": [NEG_ID, 0]},
        },
    }
    out = _inject_prompt(graph, PROMPT, NEGATIVE)
    if text_of(out, POS_ID) != PROMPT or text_of(out, NEG_ID) != NEGATIVE:
        return f"wiring ignored: {POS_ID}={text_of(out, POS_ID)!r} {NEG_ID}={text_of(out, NEG_ID)!r}"
    return None


def case_placeholder_text() -> str | None:
    """A template that ships the literal words 'positive' / 'negative'."""
    graph = {
        POS_ID: {"class_type": "CLIPTextEncode", "inputs": {"text": "positive"}},
        NEG_ID: {"class_type": "CLIPTextEncode", "inputs": {"text": "negative"}},
        SAMPLER_ID: {
            "class_type": "KSampler",
            "inputs": {"positive": [POS_ID, 0], "negative": [NEG_ID, 0]},
        },
    }
    out = _inject_prompt(graph, PROMPT, NEGATIVE)
    if text_of(out, POS_ID) != PROMPT or text_of(out, NEG_ID) != NEGATIVE:
        return f"placeholder not replaced: {POS_ID}={text_of(out, POS_ID)!r} {NEG_ID}={text_of(out, NEG_ID)!r}"
    return None


def case_no_sampler_falls_back_to_order() -> str | None:
    """No KSampler to follow, so declaration order decides."""
    graph = {
        POS_ID: {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        NEG_ID: {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    }
    out = _inject_prompt(graph, PROMPT, NEGATIVE)
    if text_of(out, POS_ID) != PROMPT or text_of(out, NEG_ID) != NEGATIVE:
        return f"order fallback failed: {POS_ID}={text_of(out, POS_ID)!r} {NEG_ID}={text_of(out, NEG_ID)!r}"
    return None


def case_single_text_node() -> str | None:
    """One text node cannot carry both prompts; it must not raise."""
    graph = {POS_ID: {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}}
    out = _inject_prompt(graph, PROMPT, NEGATIVE)
    if text_of(out, POS_ID) != PROMPT:
        return f"single node got {text_of(out, POS_ID)!r}"
    return None


def case_no_text_node_uses_builtin() -> str | None:
    """Nothing to splice into, so the built-in graph is returned."""
    graph = {SAMPLER_ID: {"class_type": "KSampler", "inputs": {}}}
    out = _inject_prompt(graph, PROMPT, NEGATIVE)
    if out.get("6", {}).get("inputs", {}).get("text") != PROMPT:
        return "did not fall back to the built-in graph"
    return None


def case_missing_inputs_key() -> str | None:
    """A node with no 'inputs' at all must be given one, not crash."""
    graph = {
        POS_ID: {"class_type": "CLIPTextEncode"},
        SAMPLER_ID: {"class_type": "KSampler", "inputs": {"positive": [POS_ID, 0]}},
    }
    out = _inject_prompt(graph, PROMPT, NEGATIVE)
    if text_of(out, POS_ID) != PROMPT:
        return "node without 'inputs' was not populated"
    return None


CASES = (
    ("both text nodes empty", case_two_nodes_empty_positive),
    ("empty positive, filled negative", case_empty_positive_filled_negative),
    ("wiring beats declaration order", case_wiring_beats_declaration_order),
    ("placeholder text replaced", case_placeholder_text),
    ("no sampler -> order fallback", case_no_sampler_falls_back_to_order),
    ("single text node", case_single_text_node),
    ("no text node -> built-in graph", case_no_text_node_uses_builtin),
    ("node missing 'inputs'", case_missing_inputs_key),
)


def main() -> int:
    failures: list[str] = []
    for name, fn in CASES:
        try:
            problem = fn()
        except Exception as exc:  # noqa: BLE001 - report and keep going
            problem = f"raised {type(exc).__name__}: {exc}"
        if problem:
            print(f"  FAIL: {name}: {problem}")
            failures.append(name)
        else:
            print(f"  ok:   {name}")

    print()
    if failures:
        print(f"{len(failures)} problem(s)")
        return 1
    print(f"PASS: all {len(CASES)} workflow injection cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
