"""Check model recommendations against a simulated RTX 5070 + 32 GB machine."""
from app import hardware, model_catalog


def profile(ram_gb, vram_gb, gpu=True):
    return {
        "cpu": {"logical_cores": 16, "physical_cores": 8, "model": "sim"},
        "memory": {"total_gb": ram_gb, "available_gb": ram_gb * 0.7, "used_gb": ram_gb * 0.3},
        "disk": {"free_gb": 900, "total_gb": 2000},
        "gpu": [{"name": "NVIDIA RTX 5070", "vram_gb": vram_gb, "vendor": "nvidia"}] if gpu else [],
        "runtimes": {"torch": True, "onnxruntime": True},
    }


for label, prof in [
    ("RTX 5070 12GB / 32GB RAM", profile(32, 12)),
    ("CPU only / 32GB RAM", profile(32, 0, gpu=False)),
]:
    rec = hardware.recommend(prof)
    print(f"=== {label} ===")
    print(f"  llm: {rec['recommended_llm']}  tts: {rec['recommended_tts_tier']}")

# Specifically: does qwen3.8:27b fit a 5070, and is it flagged as spilling?
rec = hardware.recommend(profile(32, 12))
by_repo = {m["repo"]: m for m in rec["models"]}
for repo in ["qwen3.8:27b", "qwen3:8b", "qwen2.5:14b-instruct-q4_K_M"]:
    m = by_repo.get(repo)
    if not m:
        print(f"  {repo}: NOT IN CATALOG")
        continue
    print(f"  {repo}: fits={m['fits']} score={m['score']}")
    print(f"      caveats={m['caveats']}")
    print(f"      blockers={m['blockers']}")

# And on a 24 GB card it should fit with no spill caveat.
print("=== RTX 4090-ish 24GB ===")
m = {x["repo"]: x for x in hardware.recommend(profile(64, 24))["models"]}["qwen3.8:27b"]
print(f"  qwen3.8:27b fits={m['fits']} caveats={m['caveats']} blockers={m['blockers']}")

# On a 16 GB machine it must be blocked on RAM.
print("=== 16GB RAM / 12GB card ===")
m = {x["repo"]: x for x in hardware.recommend(profile(16, 12))["models"]}["qwen3.8:27b"]
print(f"  qwen3.8:27b fits={m['fits']} blockers={m['blockers']}")

print(f"\ncatalog llm count: {len(model_catalog.LLM_MODELS)}")
