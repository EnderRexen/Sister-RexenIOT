# Task: run the agent sandbox fully locally

Status: **planned, not implemented.** Written from a survey of `rexen-nixos-dt`
on 2026-11-16.

## Goal

Run an OpenHands agent sandbox on this machine that is **completely local**: the
model, the sandbox, and the storage all stay on `rexen-nixos-dt`. No cloud LLM,
no telemetry, and it keeps working with the network unplugged.

## What already exists

More of this is built than it looks. The survey found:

| Piece | State |
|---|---|
| Container | `openhands`, image `ghcr.io/openhands/agent-canvas:latest` |
| Port | `127.0.0.1:8000` (loopback only -- good) |
| Managed by | `/etc/nixos/modules/openhands.nix` (NixOS `oci-containers`) |
| State | named volume `openhands-data` -> `/home/openhands/.openhands` |
| Projects | `/home/ender/projects` -> `/projects` |
| Host access | `host.docker.internal:host-gateway` already added |
| Telemetry | already off (`VITE_DO_NOT_TRACK`, `AGENT_CANVAS_DISABLE_TELEMETRY`) |
| GPU in container | **none** (`DeviceRequests=null`) |
| Start | `autoStart = true`, so it comes up with the system |

The container, its module, its storage and its privacy switches are done. The
gap is narrow.

## The actual gap

The container's LLM is still a **managed cloud profile**. Going local means
pointing it at a model server on this host. The host already runs two:

- **Ollama** on `:11434` -- e.g. `gemma3:12b`, already pulled (8.2 GB)
- **llama-swap** on `:8080` -- e.g. `glm-4.7-flash`, `hermes-3-3b`, `lexi-llama-3-8b`

From inside the container those are `http://host.docker.internal:11434/v1` and
`http://host.docker.internal:8080/v1`.

## Steps

1. **Add a local LLM profile** in the canvas settings, with the base URL above
   and an OpenAI-compatible model id.
2. **Confirm tool calling works.** This is the make-or-break step, not the
   model's chat quality: the harness is useless if the model cannot emit tool
   calls. Sister RexenIOT hit exactly this -- `llama-server` only accepts the
   `tools` field with `--jinja`, and without it the request fails with HTTP 500.
   Test a real tool call before trusting the setup.
3. **Check the context window.** Agent harnesses burn context fast. A model with
   a small window truncates tool output and loops.
4. **Prove it is offline.** Unplug the network and confirm a full conversation
   still runs. Telemetry being off is not the same as the model being local.
5. **Decide on GPU.** The container has none today and needs none while
   inference happens on the host. Only add passthrough if the container itself
   must run a model.

## Things to settle before building

- **Which model.** Needs tool calling *and* a large window. Candidates already on
  the box: `glm-4.7-flash` (llama-swap), `gemma3:12b` (Ollama). Test the chosen
  one against a real tool call first.
- **Memory budget.** 30 GB usable RAM, 12 GB VRAM. `gemma3:12b` is 8.2 GB of
  weights on its own. A large agent context plus a second model resident will not
  both fit comfortably -- check `ollama ps` / llama-swap residency.
- **Which image.** `agent-canvas:latest` is a moving tag. Pinning a digest would
  make this reproducible, matching how the rest of the flake is written.
- **Where state lives.** The `openhands-data` named volume is fine, but a local
  setup may want it on disk for backup. If so, note the module comment: a
  host-owned bind mount fails with "unable to open database file" because the
  container runs as UID 10001.

## Definition of done

- A conversation completes with the network unplugged, using a model on this host.
- A tool call succeeds end to end, not just a chat reply.
- No request leaves the machine: verified by observation, not by assumption.
- The config lives in `/etc/nixos/modules/openhands.nix`, so a rebuild reproduces it.
