"""Check that the speech engine claims a reachable GPU under device=auto."""
import sys
import types

# Pretend onnxruntime-gpu is installed and sees a CUDA card.
fake = types.ModuleType("onnxruntime")
fake.get_available_providers = lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"]
sys.modules["onnxruntime"] = fake

from app.tts.engine import TTSEngine  # noqa: E402

ONNX_TIER = "KittenML/kitten-tts-mini-0.8"
TORCH_TIER = "KittenML/kitten-tts-2"

cases = [
    ("cpu", ONNX_TIER, ("cpu", "cpu"), "explicit cpu stays on cpu"),
    ("cuda", ONNX_TIER, ("cuda", "cuda"), "explicit cuda honoured when reachable"),
    ("auto", ONNX_TIER, ("cuda", "cuda"), "auto claims the reachable GPU"),
    ("auto", TORCH_TIER, (None, None), "torch tier, no torch installed -> library decides"),
]

bad = 0
for requested, model_id, want, why in cases:
    got = TTSEngine._resolve_device(requested, model_id)
    ok = got == want
    bad += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  device={requested:<5} {model_id.split('/')[-1]:<22} -> {got}  {why}")

fake.get_available_providers = lambda: ["CPUExecutionProvider"]
got = TTSEngine._resolve_device("auto", ONNX_TIER)
ok = got == (None, None)
bad += not ok
print(f"  {'PASS' if ok else 'FAIL'}  device=auto  no CUDA anywhere        -> {got}  falls back to cpu")

raise SystemExit(1 if bad else 0)
