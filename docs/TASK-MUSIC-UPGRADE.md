# Task: upgrade music generation to ACE-Step 1.5

Status: **planned, not implemented.** Researched and verified against the live
host on 2026-11-16.

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

1. **Provision the weights.** `app/comfyui_provision.py` holds `KNOWN_NODES`
   and `DIR_MODEL_COMPONENTS`. 1.5 is a single checkpoint, not a directory of
   components, so it needs a file-level entry rather than a `DIR_MODEL_COMPONENTS`
   tuple. Extend the existing mechanism instead of adding a parallel one.
   Nothing named `ace*` is on disk yet, so this download has to happen.
2. **Swap the built-in music graph.** `_music_workflow()` in
   `app/tools/comfyui.py` builds the v1 graph (`ACEModelLoader`,
   `MultiLinePromptACES`, `MultiLineLyrics`, `GenerationParameters`,
   `ACEStepGen`, `SaveAudioMP3`). Replace it with the 1.5 core graph above.
3. **Drop the v1 node pack from provisioning.** `KNOWN_NODES["ace-step"]`
   points at `billwuhao/ComfyUI_ACE-Step`, which is no longer installed and no
   longer needed. Keeping it would re-clone a pack the graph does not use.
4. **Update `_TEXT_INPUT_FIELDS` and `_music_text_nodes()`.** The prompt/lyrics
   split currently keys off `MultiLinePromptACES` / `MultiLineLyrics`. In 1.5
   both live in **one** node, `TextEncodeAceStepAudio1.5`, with separate `tags`
   and `lyrics` fields. This is the subtle part: a single node now carries both,
   so the "which node is which" logic has to become "which *field* is which".
   Get it wrong and the style description silently becomes the lyrics.
5. **Keep `kind="audio"` working** for a bare request, since the built-in graph
   is what makes music work with no workflow file present.
6. **Test it.** `scripts/test_comfyui_provision.py` and
   `scripts/test_comfyui_workflow.py` both assert on the current graph and node
   names; they will need updating in the same change, not afterwards.
7. **Prove it end to end**, the same way image generation was proven: request a
   song from the chat interface, confirm real audio comes back, and confirm two
   requests do not return the same file.

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
with suspicion. What can be said honestly:

- ACE-Step 1.5 is the strongest **open, commercially usable** option, and the
  project claims quality "beyond most commercial music models".
- The independent signals agree it is top-tier: 971 likes, ~15k GitHub stars,
  and it is what ComfyUI chose to support natively.
- Whether it *sounds* like v6 is a listening question, not a spec question.
  Settle it by generating the same prompt through both and comparing -- the same
  way the two image paths were compared.

Do not promise v6 parity. Promise the best local model, and measure it.

## Sources

- https://hf.co/ACE-Step/Ace-Step1.5
- https://hf.co/Comfy-Org/ACE-Step-1.5
- https://github.com/ace-step/ACE-Step-1.5
- https://github.com/Comfy-Org/docs/blob/main/tutorials/audio/ace-step/ace-step-v1-5.mdx
- https://github.com/Comfy-Org/workflow_templates/blob/main/templates/audio_ace_step_1_5_checkpoint.json
