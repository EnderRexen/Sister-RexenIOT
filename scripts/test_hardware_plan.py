"""Check hardware.plan() verifies a machine and proposes settings that fit it.

Run against a simulated profile so the same assertions hold on any machine,
including the ones with no GPU. Each case is chosen because the plan must say
something specific and non-obvious about it.
"""
from app import config, hardware

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def profile(
    ram=32,
    vram=0,
    *,
    gpu=None,
    torch_cuda=None,
    torch_alloc=None,
    ort_cuda=None,
    vllm=False,
    driver_only=False,
    espeak_path=False,
    espeakng_loader=True,
    cores=16,
    free_disk=900,
):
    """A simulated hardware profile. Defaults describe a healthy CPU-only box."""
    if gpu is None:
        gpu = vram > 0
    tc = (vram > 0) if torch_cuda is None else torch_cuda
    ta = tc if torch_alloc is None else torch_alloc
    oc = (vram > 0) if ort_cuda is None else ort_cuda
    gpus = []
    if gpu:
        gpus.append({"name": "NVIDIA RTX 5070", "vram_gb": vram, "vendor": "nvidia"})
    if driver_only:
        gpus.append({
            "name": "NVIDIA GPU (driver present)", "vram_gb": 0.0,
            "vendor": "nvidia", "driver_only": True, "driver": "550.54",
        })
    return {
        "profile_version": hardware.PROFILE_VERSION,
        "scanned_at": 0,
        "hostname": "sim",
        "cpu": {"logical_cores": cores, "physical_cores": cores // 2, "brand": "SIM", "model": "sim"},
        "memory": {"total_gb": ram, "available_gb": ram * 0.7, "used_gb": ram * 0.3},
        "disk": {"free_gb": free_disk, "total_gb": 2000},
        "gpu": gpus,
        "runtimes": {
            "torch": True, "onnxruntime": True,
            "espeak": espeak_path, "espeakng_loader": espeakng_loader,
            "phonemiser": espeak_path or espeakng_loader,
        },
        "accelerators": {
            "torch": {
                "installed": True, "cuda": tc, "allocatable": ta,
                "version": "2.14.1", "cuda_version": "13.0",
                "device_name": "RTX 5070" if tc else None,
                "devices": ["RTX 5070"] if tc else [],
            },
            "onnxruntime": {
                "installed": True, "cuda": oc,
                "providers": ["CUDAExecutionProvider"] if oc else ["CPUExecutionProvider"],
                "device": "GPU" if oc else "CPU", "version": "1.31.0",
            },
            "vllm": {"installed": vllm, "cuda": vllm and tc, "version": "0.6" if vllm else None},
            "driver_only_gpus": [g for g in gpus if g.get("driver_only")],
            "gpu_seen_but_unusable": driver_only,
        },
    }


def steps_of(plan):
    return {s["key"]: s for s in plan["steps"]}


def cfg_with(**overrides):
    """A copy of the live config with dotted-path overrides, for explicit cases."""
    data = config.load().model_dump()
    for path, value in overrides.items():
        section, key = path.split(".")
        data[section][key] = value
    return config.AppConfig.model_validate(data)


print("=== a 5070-class machine is pointed at the GPU paths ===")
cfg = config.load()
p = hardware.plan(profile(32, 12), cfg)
s = steps_of(p)
check("gpu step reports the card", s["gpu"]["status"] == "ok" and "RTX 5070" in s["gpu"]["detail"],
      s["gpu"]["detail"])
check("picks the full speech tier", p["tier"] == "full", p["tier"])
check("picks cuda", p["device"] == "cuda", p["device"])
check("suggests vLLM as an install", s["vllm"]["actionable"] and "vllm" in s["vllm"]["command"],
      s["vllm"]["command"])
check("onnx is reported usable", s["onnxruntime"]["status"] == "ok")
check("the model matches the tier", p["patch"]["tts"]["model_id"] == "KittenML/kitten-tts-2",
      p["patch"]["tts"]["model_id"])

print("\n=== the patch is actually applicable to the live config ===")
# Not a string check: validate the exact object the endpoint would persist, so a
# bad literal or a renamed field fails here instead of at apply time.
try:
    merged = config._deep_merge(cfg.model_dump(), p["patch"])
    validated = config.AppConfig.model_validate(merged)
    check("patch validates against AppConfig", True)
    check("patch round-trips the tier", validated.tts.tier == p["tier"])
    check("patch round-trips the device", validated.tts.device == p["device"])
except Exception as exc:
    check("patch validates against AppConfig", False, f"{type(exc).__name__}: {exc}")

print("\n=== a machine with no GPU is not told to chase a GPU ===")
p = hardware.plan(profile(32, 0, gpu=False), cfg)
s = steps_of(p)
check("gpu step is informational", s["gpu"]["status"] == "info", s["gpu"]["detail"])
check("gpu step offers no action", not s["gpu"]["actionable"])
check("torch step offers no CUDA reinstall", not s["torch"]["actionable"],
      s["torch"].get("command", ""))
check("onnx CPU is not a problem", s["onnxruntime"]["status"] == "ok")
check("picks a CPU tier", p["tier"] in ("nano", "micro", "mini"), p["tier"])

print("\n=== 'auto' is left alone when it already means CPU ===")
# Rewriting auto -> cpu changes nothing at runtime, but it does change the
# config, which drops the shipped 'balanced' profile out of effect. A no-op
# change must not be proposed.
p = hardware.plan(profile(32, 0, gpu=False), cfg_with(**{"tts.device": "auto"}))
check("device stays auto", p["device"] == "auto", p["device"])
check("no device change is proposed", "tts.device" not in p["changed"], str(p["changed"]))

print("\n=== an explicit 'cuda' that cannot work is corrected ===")
# The opposite case: the user asked for a GPU this box cannot reach. 'auto' is
# the right answer because it picks the GPU up once the runtime is fixed.
p = hardware.plan(profile(32, 0, gpu=False), cfg_with(**{"tts.device": "cuda"}))
check("device is corrected to auto", p["device"] == "auto", p["device"])
check("the correction is proposed", "tts.device" in p["changed"], str(p["changed"]))

print("\n=== the shipped 'balanced' profile stays in effect after applying ===")
# Guards the regression above: if the plan rewrote device, no profile would match.
from app import profiles  # noqa: E402
p = hardware.plan(profile(32, 0, gpu=False), cfg_with(**{"tts.device": "auto"}))
after = config.AppConfig.model_validate(
    config._deep_merge(config.load().model_dump(), p["patch"]))
check("balanced still matches the applied config", profiles.matches(
    profiles.get("balanced"), after), f"device={after.tts.device}")

print("\n=== a GPU the runtime cannot reach is called out, not hidden ===")
# This is the case that silently costs speed: a card is installed, the runtime
# cannot use it, and the app quietly runs on CPU unless it says so.
p = hardware.plan(profile(32, 0, gpu=True, torch_cuda=False, ort_cuda=False), cfg)
s = steps_of(p)
check("onnx reports CPU-only with the provider list",
      s["onnxruntime"]["status"] == "warn" and "CPUExecutionProvider" in s["onnxruntime"]["detail"],
      s["onnxruntime"]["detail"])
check("the GPU install is offered", "requirements-gpu" in s["onnxruntime"]["command"],
      s["onnxruntime"]["command"])
check("torch says CUDA build without a GPU",
      "CUDA 13.0 build" in s["torch"]["detail"], s["torch"]["detail"])
check("device stays 'auto' so the GPU is picked up once the runtime is fixed",
      p["device"] == "auto", p["device"])
# The explanation comes from the recommendation, which is a pure function of the
# profile -- unlike the notes, which also depend on the current config value.
reason = hardware.recommend(profile(32, 0, gpu=True, torch_cuda=False, ort_cuda=False))["device_reason"]
check("the device reason explains the auto fallback", "fall back to CPU" in reason, reason)

print("\n=== a CUDA build whose allocation fails is distinguished ===")
# cuda.is_available() can be True while a real allocation fails. The two need
# different advice, so they must not collapse into one message.
p = hardware.plan(profile(32, 12, torch_cuda=True, torch_alloc=False), cfg)
s = steps_of(p)
check("torch warns rather than claims success", s["torch"]["status"] == "warn", s["torch"]["detail"])
check("it mentions the failed allocation", "allocation failed" in s["torch"]["detail"])
check("it gives a command to reproduce", "torch.zeros" in s["torch"]["command"])

print("\n=== a driver-backed GPU with no runtime is reported as such ===")
p = hardware.plan(profile(32, 0, gpu=False, driver_only=True), cfg)
s = steps_of(p)
check("gpu step warns", s["gpu"]["status"] == "warn", s["gpu"]["detail"])
check("it says the driver is present", "driver is present" in s["gpu"]["detail"])
check("it suggests passing the GPU through", "gpus all" in s["gpu"]["detail"])
check("it is actionable", s["gpu"]["actionable"])

print("\n=== the bundled phonemiser counts as present ===")
# The espeakng_loader wheel ships its own binary; a missing PATH entry is normal
# and must not be reported as a fault.
p = hardware.plan(profile(32, 12, espeak_path=False, espeakng_loader=True), cfg)
s = steps_of(p)
check("no false warning when the wheel is installed", s["espeak"]["status"] == "ok",
      s["espeak"]["detail"])
check("it says where it came from", "espeakng_loader" in s["espeak"]["detail"])
p = hardware.plan(profile(32, 12, espeak_path=False, espeakng_loader=False), cfg)
s = steps_of(p)
check("a real absence is still a warning",
      steps_of(p)["espeak"]["status"] == "warn" and s["espeak"]["actionable"])

print("\n=== thin memory and disk are flagged ===")
p = hardware.plan(profile(8, 0, gpu=False, free_disk=3), cfg)
s = steps_of(p)
check("low memory warns", s["memory"]["status"] == "warn", s["memory"]["detail"])
check("low disk warns", s["disk"]["status"] == "warn", s["disk"]["detail"])

print("\n=== nothing is written by plan() ===")
before = config.HARDWARE_PATH.read_bytes() if config.HARDWARE_PATH.exists() else None
hardware.plan(profile(32, 12), cfg)
after = config.HARDWARE_PATH.read_bytes() if config.HARDWARE_PATH.exists() else None
check("plan() leaves the cached profile untouched", before == after)

print("\n=== a profile missing the phonemiser key is not read as a fault ===")
# A cache written before the bundled wheel was recognised has only the old
# `espeak` flag. Reading a missing key as "absent" would show a false warning.
legacy = profile(32, 12, espeak_path=True, espeakng_loader=True)
del legacy["runtimes"]["phonemiser"]
del legacy["runtimes"]["espeakng_loader"]
s = steps_of(hardware.plan(legacy, cfg))
check("an old profile with espeak on PATH reads as available",
      s["espeak"]["status"] == "ok", s["espeak"]["detail"])

print("\n=== the cached-profile schema version guards the readers ===")
check("PROFILE_VERSION is set", isinstance(hardware.PROFILE_VERSION, int)
      and hardware.PROFILE_VERSION >= 2, str(hardware.PROFILE_VERSION))
check("the simulated profile uses the current version",
      profile()["profile_version"] == hardware.PROFILE_VERSION)

print("\n=== changed-settings detection is not vacuous ===")
# If _changed_settings always returned [], the Apply buttons would be dead and
# every "nothing to change" message would be a lie.
p_match = hardware.plan(profile(32, 0, gpu=False), cfg)
p_diff = hardware.plan(profile(32, 12), cfg)
check("a differing machine reports changes", bool(p_diff["changed"]), str(p_diff["changed"]))
check("the change list names real fields",
      all(c.split(".")[0] in {"tts", "llm"} for c in p_diff["changed"]))
check("dotted paths resolve on the live config",
      all(hasattr(cfg, c.split(".")[0]) for c in p_diff["changed"]))

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S)'}")
for f in FAILS:
    print("  -", f)
raise SystemExit(1 if FAILS else 0)
