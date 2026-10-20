"""Pin the contract of ``TTSEngine._resolve_device``.

``auto`` deliberately returns ``(None, None)`` -- "let the library choose".
That is correct rather than lazy: kittenml's ONNX loader forwards
``backend=None`` to onnxruntime as ``providers=[]``, and onnxruntime reads an
empty provider list as "use every available provider in default precedence",
so a box with onnxruntime-gpu already reaches CUDA without being told. The
torch loader picks ``cuda`` itself when ``device`` is None (see
``kittentts2/model.py``). Pinning this keeps a future reader from "fixing"
``auto`` into passing ``cuda`` explicitly, which would only add a way to fail
when the driver is missing.

NOT VERIFIED HERE: the ``providers=[]`` claim above is read from onnxruntime's
source, not measured -- this machine exposes no CUDA provider, so the two paths
cannot be distinguished locally. See the daily log before trusting it.

Run: PYTHONPATH=. .venv/bin/python scripts/test_tts_device.py
"""
import sys
import types

# A fake onnxruntime that *claims* CUDA, to prove the claim does not leak into
# the auto path.
fake = types.ModuleType("onnxruntime")
fake.get_available_providers = lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"]
sys.modules["onnxruntime"] = fake

from app.tts.engine import TTSEngine  # noqa: E402

ONNX_TIER = "KittenML/kitten-tts-mini-0.8"
TORCH_TIER = "KittenML/kitten-tts-2"

cases = [
    ("cpu", ONNX_TIER, ("cpu", "cpu"), "explicit cpu pins both kwargs"),
    ("cuda", ONNX_TIER, ("cuda", "cuda"), "explicit cuda honoured when reachable"),
    ("auto", ONNX_TIER, (None, None), "auto defers to the library, even with CUDA present"),
    ("auto", TORCH_TIER, (None, None), "auto defers on the torch path too"),
]

bad = 0
for requested, model_id, want, why in cases:
    got = TTSEngine._resolve_device(requested, model_id)
    ok = got == want
    bad += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  device={requested:<5} {model_id.split('/')[-1]:<22} -> {got}  {why}")

# An explicit cuda on a box without it must degrade instead of raising.
fake.get_available_providers = lambda: ["CPUExecutionProvider"]
got = TTSEngine._resolve_device("cuda", ONNX_TIER)
ok = got == (None, None)
bad += not ok
print(f"  {'PASS' if ok else 'FAIL'}  device=cuda  no CUDA anywhere        -> {got}  degrades instead of raising")

raise SystemExit(1 if bad else 0)
