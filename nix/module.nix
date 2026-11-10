# NixOS module for Sister RexenIOT.
#
# `tier` selects which build of the Python environment to run. The tiers are
# separate packages rather than runtime flags because the dependency sets
# genuinely differ -- shipping PyTorch to a machine that cannot use it would
# defeat the point of the light tier.
{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  inherit (lib)
    mkEnableOption
    mkIf
    mkOption
    optional
    types
    ;

  cfg = config.services.rexeniot;
  system = pkgs.stdenv.hostPlatform.system;

  packageForTier =
    tier:
    {
      light = self.packages.${system}.rexeniot;
      stt = self.packages.${system}.rexeniot-stt;
      full = self.packages.${system}.rexeniot-full;
    }
    .${tier};

  # A partial config.json used to point the app at a local model on first
  # start. Built with toJSON so the quoting is handled by Nix, and with nulls
  # filtered out because the schema types `base_url` and `model` as `str` --
  # a literal `null` would be rejected and the app would silently fall back to
  # its own defaults. Only evaluated when the operator sets one of the options.
  llmSeed = pkgs.writeText "rexeniot-llm-seed.json" (
    builtins.toJSON {
      llm = lib.filterAttrs (_: v: v != null) {
        provider = cfg.llm.provider;
        base_url = cfg.llm.baseUrl;
        model = cfg.llm.model;
      };
    }
  );

  # Re-applies the three LLM fields on every start. The merge is recursive
  # (`jq`'s `*`), so unrelated settings the operator changed in the panel --
  # temperature, voice, wake words -- are preserved, and only provider,
  # base_url and model are made authoritative. Those three therefore revert to
  # this configuration on restart, which is the point: it is what makes the
  # model selection declarative instead of a one-shot first-boot seed.
  llmSeedApply = pkgs.writeShellScript "rexeniot-llm-seed" ''
    set -eu
    cfg=/var/lib/rexeniot/data/config.json
    # Absolute store paths: a systemd unit on NixOS has no /usr/bin, so a bare
    # `mktemp` or `mv` would not be found.
    ${pkgs.coreutils}/bin/mkdir -p /var/lib/rexeniot/data
    if [ -e "$cfg" ]; then
      tmp=$(${pkgs.coreutils}/bin/mktemp)
      ${pkgs.jq}/bin/jq -s '.[0] * .[1]' "$cfg" ${llmSeed} > "$tmp"
      ${pkgs.coreutils}/bin/mv "$tmp" "$cfg"
    else
      ${pkgs.coreutils}/bin/cp ${llmSeed} "$cfg"
    fi
  '';
in
{
  options.services.rexeniot = {
    enable = mkEnableOption "Sister RexenIOT, a local-first voice assistant";

    tier = mkOption {
      type = types.enum [
        "light"
        "stt"
        "full"
      ];
      default = "light";
      description = ''
        Which build of the speech stack to run.

        - `light`: CPU only, ONNX speech synthesis, no PyTorch.
        - `stt`: adds local dictation via faster-whisper.
        - `full`: adds the 1.7B KittenTTS 2 model, voice cloning and emotion
          tags. Adds roughly 2.5 GB and pulls a CUDA-linked PyTorch on Linux.
      '';
    };

    package = mkOption {
      type = types.package;
      default = packageForTier cfg.tier;
      defaultText = lib.literalExpression "the build selected by services.rexeniot.tier";
      description = "The RexenIOT package to run.";
    };

    host = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = ''
        Address to bind. The default keeps the admin panel on loopback; set
        `openFirewall` to expose it to your network instead.
      '';
    };

    port = mkOption {
      type = types.port;
      default = 8070;
      description = "Port to listen on. Defaults to the app's own default.";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open `port` in the firewall. Only meaningful with a non-loopback `host`.";
    };

    modelsDir = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/mnt/bulk-storage/MODELS/TTS";
      description = ''
        Where downloaded speech models should live. The default keeps them in
        the state directory; point this at bulk storage to keep them off the
        root filesystem. The path is symlinked into the state directory on
        start, so it must exist and be readable by the service user.

        If the drive is owned by a normal user, set `user` as well -- a
        DynamicUser cannot read it. `modelsDir` is also added to
        `ReadWritePaths`, because `ProtectSystem = "strict"` would otherwise
        make downloads into it fail.
      '';
    };

    user = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "ender";
      description = ''
        Run the service as this user instead of a systemd DynamicUser. Required
        when `modelsDir` points at a drive owned by a normal user: a DynamicUser
        gets an unmapped uid and cannot read it, which is the same wall
        llama-swap hits on a `/run/media` mount.
      '';
    };

    group = mkOption {
      type = types.str;
      default = "users";
      description = "Group to run as when `user` is set.";
    };

    environmentFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = ''
        File of `KEY=value` lines loaded into the service environment, for
        secrets such as MQTT credentials or model API keys. Keep it outside the
        Nix store so the values do not end up world-readable.
      '';
    };

    extraEnvironment = mkOption {
      type = types.attrsOf types.str;
      default = { };
      example = { REXENIOT_NO_STT = "1"; };
      description = "Additional environment variables for the service.";
    };

    llm = {
      afterLlamaSwap = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Start after `llama-swap.service`.

          This only orders the start jobs; it does not wait for llama-swap to be
          ready to accept connections, and llama-swap itself loads a model on
          first request. It narrows the window in which the app probes before
          the model server is listening -- the app also retries discovery in the
          background, which is what actually closes it.

          Off by default because the unit is only meaningful on a machine that
          runs the llama-swap NixOS service; ordering against a unit that does
          not exist is a no-op, but declaring it is the job of the config that
          knows llama-swap is there.
        '';
      };

      provider = mkOption {
        type = types.enum [
          "auto"
          "openai"
          "anthropic"
          "ollama"
          "offline"
        ];
        default = "openai";
        description = ''
          Wire format for the chat backend. `openai` is the right choice for any
          OpenAI-compatible server, which includes llama.cpp, llama-swap, vLLM,
          LM Studio and Ollama. `auto` sniffs it from the URL, but setting it
          explicitly avoids a mis-detection turning into a confusing error.
        '';
      };

      baseUrl = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "http://127.0.0.1:8080/v1";
        description = ''
          OpenAI-compatible endpoint. The service runs on the host, so a
          llama-swap listening on the loopback needs no Docker hostname here.
        '';
      };

      model = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "qwen3-8b";
        description = ''
          Model name exactly as the server advertises it. For llama-swap this is
          the key in its `models` attribute, not a filename or an Ollama tag.
        '';
      };
    };
  };

  config = mkIf cfg.enable {
    # The app refuses a non-loopback bind with no token (app/security.py).
    # Catching it here turns a service that dies at startup into an evaluation
    # error that names the option to set.
    assertions = [
      {
        assertion = cfg.host == "127.0.0.1" || cfg.host == "::1" || cfg.host == "localhost" || cfg.environmentFile != null;
        message = ''
          services.rexeniot.host is set to "${cfg.host}", which is reachable from
          your network, but no API token is configured. Every endpoint that can
          drive the agent would be open to anyone who can reach this port.

          Either keep the default loopback binding, or set
          services.rexeniot.environmentFile to a file containing
          REXENIOT_API_TOKEN=... (mode 0400, not in the world-readable Nix store).

          For remote access, prefer a private overlay and leave the server on
          loopback -- see docs/REMOTE-ACCESS.md.
        '';
      }
    ];

    systemd.services.rexeniot = {
      description = "Sister RexenIOT voice assistant";
      wantedBy = [ "multi-user.target" ];

      # [Unit] directive, so it belongs in unitConfig. Without it systemd may
      # start the service before the bulk-storage drive is mounted, and the
      # models symlink below would point at an empty directory.
      unitConfig.RequiresMountsFor = optional (cfg.modelsDir != null) (
        toString cfg.modelsDir
      );

      # `after` only orders start jobs; it does not wait for the model server to
      # be ready to accept connections. It narrows the window rather than closing
      # it, which is why the app also retries discovery in the background.
      after = [ "network.target" ] ++ optional cfg.llm.afterLlamaSwap "llama-swap.service";

      environment = {
        REXENIOT_HOME = "/var/lib/rexeniot";
      }
      // cfg.extraEnvironment;

      # StateDirectory is created before ExecStartPre runs, so the symlink below
      # always has a writable parent.
      serviceConfig = {
        ExecStartPre =
          optional (cfg.modelsDir != null) (
            "mkdir -p /var/lib/rexeniot/data && "
            + "ln -sfn ${cfg.modelsDir} /var/lib/rexeniot/data/models"
          )
          ++ optional (cfg.llm.baseUrl != null || cfg.llm.model != null) "${llmSeedApply}";

        ExecStart = lib.escapeShellArgs [
          "${cfg.package}/bin/rexeniot"
          "--host"
          cfg.host
          "--port"
          (toString cfg.port)
          "--no-browser"
        ];

        # DynamicUser is the default and the safer choice, but it gives the
        # process an unmapped uid that cannot read a drive owned by a normal
        # user. mkForce is needed because the value is set here either way.
        DynamicUser = lib.mkForce (cfg.user == null);
        User = lib.mkIf (cfg.user != null) cfg.user;
        Group = lib.mkIf (cfg.user != null) cfg.group;

        StateDirectory = "rexeniot";
        WorkingDirectory = "/var/lib/rexeniot";

        EnvironmentFile = optional (cfg.environmentFile != null) cfg.environmentFile;

        Restart = "on-failure";
        RestartSec = 5;

        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;

        # ProtectSystem=strict mounts everything read-only, so model downloads
        # into modelsDir would fail without this. PrivateTmp is set above, so
        # /tmp is already writable for the seed script's mktemp.
        ReadWritePaths = optional (cfg.modelsDir != null) (toString cfg.modelsDir);
      };
    };

    networking.firewall.allowedTCPPorts = optional cfg.openFirewall cfg.port;
  };
}
