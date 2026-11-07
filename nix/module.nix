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
      default = 8765;
      description = "Port to listen on.";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open `port` in the firewall. Only meaningful with a non-loopback `host`.";
    };

    modelsDir = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/run/media/ender/5TB_AI/MODELS/TTS";
      description = ''
        Where downloaded speech models should live. The default keeps them in
        the state directory; point this at bulk storage to keep them off the
        root filesystem. The path is symlinked into the state directory on
        start, so it must exist and be readable by the service user.
      '';
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
  };

  config = mkIf cfg.enable {
    systemd.services.rexeniot = {
      description = "Sister RexenIOT voice assistant";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];

      environment = {
        REXENIOT_HOME = "/var/lib/rexeniot";
      }
      // cfg.extraEnvironment;

      # StateDirectory is created before ExecStartPre runs, so the symlink below
      # always has a writable parent.
      serviceConfig = {
        ExecStartPre = optional (cfg.modelsDir != null) (
          "mkdir -p /var/lib/rexeniot/data && "
          + "ln -sfn ${cfg.modelsDir} /var/lib/rexeniot/data/models"
        );

        ExecStart = lib.escapeShellArgs [
          "${cfg.package}/bin/rexeniot"
          "--host"
          cfg.host
          "--port"
          (toString cfg.port)
          "--no-browser"
        ];

        DynamicUser = true;
        StateDirectory = "rexeniot";
        WorkingDirectory = "/var/lib/rexeniot";

        EnvironmentFile = optional (cfg.environmentFile != null) cfg.environmentFile;

        Restart = "on-failure";
        RestartSec = 5;

        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
      };
    };

    networking.firewall.allowedTCPPorts = optional cfg.openFirewall cfg.port;
  };
}
