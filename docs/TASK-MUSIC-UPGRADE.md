# Task: upgrade music generation to ACE-Step 1.5

Status: **implemented** (2026-11-17). Researched and verified against the live
host on 2026-11-16; implemented and validated on 2026-11-17.

## What was done

- `app/tools/comfyui.py`: `_music_workflow()` rebuilt as the 1.5 core graph
  (`CheckpointLoaderSimple`, `TextEncodeAceStepAudio1.5`,
  `EmptyAceStep1.5LatentAudio`, `ModelSamplingAuraFlow`, `ConditioningZeroOut`,
  `KSampler`, `VAEDecodeAudio`, `SaveAudioMP3`).
- `_music_text_nodes()` now returns a `(node id, field)` pair per target, so the
  style and the lyrics are distinguished by **field** within one node. A v1
  two-node graph still injects correctly.
- `app/comfyui_provision.py`: `KNOWN_MODELS["ace-step"]` is the 10 GB
  `Comfy-Org/ACE-Step-1.5` all-in-one checkpoint; `KNOWN_NODES` no longer lists
  any music pack; `DIR_MODEL_COMPONENTS` is empty.
- Duration ceiling raised to 600 s (1.5's supported maximum) and steps to 50.
- Tests updated; both suites pass.

Validated against the **live** ComfyUI: every node class, every input name,
every link target/slot and every value was checked against the running server's
`/object_info` (1,926 node classes). All pass. The only unmet requirement is the
10 GB checkpoint, which was deliberately not downloaded.

## First: music is broken right now

Verified on the running host, not inferred:

- The host ComfyUI (v0.37.0) exposes **all** the ACE-Step 1.5 core nodes:
  `TextEncodeAceStepAudio1.5`, `EmptyAceStep1.5LatentAudio`, `ReferenceTimbreAudio`.
- It exposes **none** of the v1 pack nodes the app currently depends on:
  `ACEModelLoader`, `MultiLinePromptACES`, `MultiLineLyrics`,
  `GenerationParameters`, `ACEStepGen`.
- Submitting the app's built-in v1 music graph is rejected:

  ```json
  {"error": {"type": "missing_node_type",
             "message": "Node 'ACEModelLoader' not found.
                         The custom node may not be installed."}}
  ```

So a music request from chat fails today. **This upgrade is also the fix** --
the machine is already set up for 1.5 and has moved past v1. That reorders the
work: this is not a nice-to-have quality bump, it is a repair.

## Why

Sister Rexen ships ACE-Step **v1 (3.5B)** as its built-in music graph. The
successor, **ACE-Step 1.5**, is the highest-rated open music model available and
is a large jump, not a small one.

### The numbers

| Model | Likes (HF) | License | Notes |
|---|---|---|---|
| **ACE-Step/Ace-Step1.5** | **971** | **MIT** | the upgrade target |
| ACE-Step/ACE-Step-v1-3.5B | 757 | Apache-2.0 | **what we ship today** |
| m-a-p/YuE2-3B | 1263 | CC-BY-NC-4.0 | more likes, **non-commercial** |
| stabilityai/stable-audio-3-medium | 615 | other (gated) | gated, needs a token |

YuE2 has more likes, but its CC-BY-NC-4.0 licence forbids commercial use and it
is a heavier multi-stage pipeline. ACE-Step 1.5 is **MIT** and is the only
top-tier option that is unambiguously safe to use commercially -- which matches
how the rest of this project is written.

### What 1.5 actually adds

- **Under 4 GB VRAM**, and a full song in under 10 s on an RTX 3090. This box
  has a 12 GB RTX 5070, so it is comfortable.
- **10 s to 10 minutes** per track (v1 is far shorter).
- **50+ languages** (v1: 19).
- **Commercial-ready by construction**: trained on licensed, royalty-free and
  synthetic data rather than an ambiguous scrape.
- Extras v1 has no equivalent for: cover generation, repainting, vocal-to-BGM,
  and LoRA fine-tuning.
- Musical coherence score 4.72, which the authors pitch as beyond most
  commercial models.

## The reason this is cheap: ComfyUI core supports it natively

This is the key finding. **No custom node pack is needed.**

- ACE-Step 1.5 runs on ComfyUI core (`comfy_extras/nodes_ace.py`), through
  `TextEncodeAceStepAudio1.5`, `EmptyAceStep1.5LatentAudio` and
  `ReferenceTimbreAudio`.
- Comfy-Org publishes the weights already packaged for ComfyUI.

That matters because the current integration depends on
`billwuhao/ComfyUI_ACE-Step`, which is **v1-only** -- its README tops out at
v1.0.0 and it has no 1.5 support. The official `ace-step/ACE-Step-ComfyUI` pack
does exist, but it is a **server/client** design (cloud mode by default, local
mode needs a separate inference server), so it does not fit this app's
"submit a graph to `/prompt`" architecture. Core support avoids both problems.

### The graph to build

The official template is `audio_ace_step_1_5_checkpoint.json` from
`Comfy-Org/workflow_templates`. Its nodes:

```
CheckpointLoaderSimple      ace_step_1.5_turbo_aio.safetensors
TextEncodeAceStepAudio1.5   tags + lyrics
EmptyAceStep1.5LatentAudio  duration, batch
ModelSamplingAuraFlow       shift = 3
KSampler                    seed, 8 steps, cfg 1, euler, simple
ConditioningZeroOut         (negative)
VAEDecodeAudio
SaveAudioAdvanced
```

Note the KSampler settings are **8 steps at cfg 1** -- the turbo model is
step-distilled. Do not carry over v1's `guidance_scale: 15.0` / `cfg_type: apg`
block; those belong to a different sampler chain.

## Model files

All-in-one is the simplest route: one file, no wiring.

```
models/checkpoints/ace_step_1.5_turbo_aio.safetensors
```

Split alternative, if the AIO is ever too coarse:

```
split_files/diffusion_models/acestep_v1.5_turbo.safetensors
split_files/text_encoders/qwen_0.6b_ace15.safetensors   (or 1.7b / 4b)
split_files/vae/ace_1.5_vae.safetensors
```

Repo: `Comfy-Org/ACE-Step-1.5` (also mirrored as
`Comfy-Org/ace_step_1.5_ComfyUI_files`).

## Work

Done:

1. **Provision the weights.** `KNOWN_MODELS["ace-step"]` is now the AIO
   checkpoint, with an empty subdir because the filename carries its own
   `checkpoints/` path.
2. **Swap the built-in music graph.** `_music_workflow()` builds the 1.5 core
   graph.
3. **Drop the v1 node pack from provisioning.** `KNOWN_NODES["ace-step"]` is
   gone; no music pack is listed, because 1.5 needs none.
4. **Update `_TEXT_INPUT_FIELDS` and `_music_text_nodes()`.** The subtle part
   flagged here was real: both targets are now `(node id, field)` pairs, because
   one node carries both `tags` and `lyrics`. A v1 two-node graph is still
   handled, and there is a test for each shape.
5. **Keep `kind="audio"` working** for a bare request.
6. **Tests updated** in the same change.

Outstanding:

7. **Download the 10 GB checkpoint** (`tools.comfyui_models = ["ace-step"]`).
   Not done here -- it is a large download and a decision for the machine's
   owner, not something to start unprompted.
8. **Prove it end to end** once the weights are present: request a song from the
   chat, confirm real audio comes back, and confirm two requests do not return
   the same file. The graph is already validated against the live server's
   `/object_info`, so this step is about the weights, not the graph.

## Deciding how far to go

Two honest options:

- **Minimum**: swap the graph and weights, keep the same prompt+lyrics
  interface. Sister gains 1.5 quality immediately.
- **Fuller**: expose the new capabilities as tool parameters -- `duration` up to
  600 s, and reference-audio inputs for cover / repaint. These are the features
  that make 1.5 feel different from v1, but they are new UI surface, not a swap.

Recommend doing the minimum first and proving it, then adding capabilities.

## On "comparable to Suno v6"

Suno's v6 family (flagship / wild / mini) is closed and hosted; there is no
open model that is simply equivalent, and any claim otherwise should be treated
with suspicion. The strongest evidence found says 1.5 does **not** reach v6:

- ACE-Step's own README claims quality "between Suno v4.5 and Suno v5", not v6.
  That is the vendor grading its own product, and it places the target a
  generation short of what was asked for.
- ACE-Step 1.5 is still the strongest **open, commercially usable** option: 971
  likes, ~15k GitHub stars, MIT licensed, and it is what ComfyUI chose to
  support natively. The alternatives are worse on licensing or are gated
  (YuE2-3B is CC-BY-NC, so not commercially usable at all).

So the honest position is: this is the best local model available, and it is a
generation behind Suno v6 on the vendor's own numbers. Whether it *sounds* close
enough is a listening question, not a spec question. Settle it by generating the
same prompt through both and comparing -- the same way the two image paths were
compared. Do not promise v6 parity; measure it.

## Sources

- https://hf.co/ACE-Step/Ace-Step1.5
- https://hf.co/Comfy-Org/ACE-Step-1.5
- https://github.com/ace-step/ACE-Step-1.5
- https://github.com/Comfy-Org/docs/blob/main/tutorials/audio/ace-step/ace-step-v1-5.mdx
- https://github.com/Comfy-Org/workflow_templates/blob/main/templates/audio_ace_step_1_5_checkpoint.json
