# Sister RexenIOT on a NixOS desktop, reachable from your own devices without
# exposing it to the network.
#
# The shape is: the service listens on loopback (the default), and Tailscale
# publishes it to your tailnet. Nothing is ever bound to a network interface, so
# there is no port to scan and no token to leak. `tailscale serve` is the only
# thing that can reach the app, and only your own devices are on the tailnet.
#
# This is a fragment to merge into your existing configuration.nix, not a
# complete system config. See README.md next to this file for the flake-based
# variant.

{ config, pkgs, ... }:

{
  # ------------------------------------------------------------------ Tailscale
  #
  # One-time setup after the first rebuild:
  #   sudo tailscale up
  #   tailscale status            # note this machine's 100.x.y.z address
  #   tailscale serve --bg 8070   # publish the loopback port to the tailnet
  #
  # `--bg` persists across reboots and `tailscale up/down`, so it is a one-time
  # command, not something to re-run. Undo it with `tailscale serve reset`.
  #
  # Before this works, HTTPS certificates must be enabled for the tailnet:
  # in the admin console, enable MagicDNS and HTTPS Certificates. Without them
  # `serve` has no certificate to terminate TLS with and the node URL will not
  # come up. Verify with `tailscale serve status`.
  services.tailscale.enable = true;

  # ------------------------------------------------------------------ RexenIOT
  services.rexeniot = {
    enable = true;

    # `host` is deliberately left at its default, 127.0.0.1. This is the whole
    # point: the service is not reachable from the LAN at all, so the token and
    # firewall questions do not arise. `tailscale serve` reaches it from
    # loopback, and only your tailnet can reach that.
    #
    # If you ever change this to 0.0.0.0 you must also set a token, or the
    # service will refuse to start (and the NixOS module will refuse to
    # evaluate). See docs/SECURITY.md.

    # Speech stack. `light` is CPU-only; `stt` adds local dictation; `full` adds
    # the 1.7B voice model and voice cloning (~2.5 GB, CUDA PyTorch on Linux).
    tier = "stt";

    # Secrets live here, never in extraEnvironment -- the Nix store is
    # world-readable and extraEnvironment would put the values in it.
    #
    # Create the file before the first start, readable only by root:
    #   sudo install -m 0400 /dev/null /run/secrets/rexeniot.env
    #   sudo sh -c 'printf "MQTT_PASSWORD=%s\n" "$(openssl rand -hex 16)" \
    #     >> /run/secrets/rexeniot.env'
    #
    # On loopback a token is not required. Add one anyway if you want the extra
    # layer:
    #   REXENIOT_API_TOKEN=<openssl rand -hex 32>
    #
    # /run is a tmpfs, so this file disappears on reboot; recreate it from
    # whatever you manage secrets with (sops-nix, agenix, or a systemd unit that
    # writes it from another secret store).
    environmentFile = "/run/secrets/rexeniot.env";

    # Where downloaded speech models live. Point this at bulk storage to keep
    # them off the root filesystem; the path must exist and be readable by the
    # service user.
    # modelsDir = "/mnt/bulk-storage/MODELS/TTS";

    # The model backend. This is declarative: these three fields are re-applied
    # on every start, and everything else the admin panel changes is preserved.
    llm = {
      provider = "openai";                      # any OpenAI-compatible server
      baseUrl = "http://127.0.0.1:8080/v1";     # llama-swap / llama.cpp / vLLM
      model = "qwen3-8b";
      # afterLlamaSwap = true;                  # only if you run llama-swap
    };
  };

  # Do NOT set services.rexeniot.openFirewall = true. It would open the port on
  # every interface, which is exactly what this setup avoids. It is only
  # meaningful together with a non-loopback `host`, and that combination needs a
  # token.
}
