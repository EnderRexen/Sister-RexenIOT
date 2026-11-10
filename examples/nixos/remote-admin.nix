# Tailscale + Sister RexenIOT for a machine you administer remotely.
#
# This file exists for one situation: you cannot run sudo, and you need the
# remote access working before you can do anything else. Read README.md next to
# it first -- the ordering matters, and the first step cannot be skipped.
#
# The goal is that the *one* moment of access you get -- a physical keyboard, a
# web console, someone at the machine -- is a single rebuild that makes
# everything after it possible without root again.

{ config, pkgs, ... }:

let
  # Put your own username here. This is the account that will be allowed to run
  # `tailscale` commands (including `tailscale serve`) without sudo, which is
  # the whole point: after this is applied you never need root for the parts
  # that matter.
  operator = "ender";
in
{
  # ------------------------------------------------------------------ Tailscale
  services.tailscale = {
    enable = true;

    # Reads the auth key from this file and connects on boot, with no browser
    # login. This is what makes the setup survive reboots unattended -- without
    # it, a reboot puts the node back to logged-out and you lose remote access
    # again.
    #
    # The file is a secret: keep it out of the Nix store (which is
    # world-readable). Create it on the machine, once, as root:
    #   install -m 0400 /dev/null /run/secrets/tailscale_key
    #   printf '%s' 'tskey-auth-XXXXXXXX' > /run/secrets/tailscale_key
    #
    # /run is a tmpfs, so this does not survive a reboot -- which is fine, the
    # service only reads it at startup. If you want it to persist, use
    # sops-nix/agenix, or move it to a root-only path such as
    # /var/lib/tailscale/authkey and point this at that instead.
    authKeyFile = "/run/secrets/tailscale_key";

    # Allow `operator` to drive tailscaled without sudo. Without this, every
    # `tailscale` command (including `serve`) needs root.
    extraSetFlags = [ "operator=${operator}" ];

    # Do not advertise a subnet route or an exit node. Both widen what the
    # tailnet can reach far beyond this machine -- a subnet router exposes your
    # whole LAN to every device on the tailnet. Add individual nodes instead.
  };

  # ------------------------------------------------------------------ RexenIOT
  services.rexeniot = {
    enable = true;
    tier = "stt";

    # Left at the default 127.0.0.1. `tailscale serve` proxies to loopback, and
    # the Tailscale docs are explicit that only http://127.0.0.1 is supported
    # as a proxy target -- so loopback is both the safe choice and the only one
    # that works here.
    #
    # On loopback the API token is optional. Set it anyway if you like the
    # second layer; a token file also satisfies the module's assertion if you
    # ever change `host`.
    environmentFile = "/run/secrets/rexeniot.env";

    llm = {
      provider = "openai";
      baseUrl = "http://127.0.0.1:8080/v1";
      model = "qwen3-8b";
    };
  };

  # After this is applied, the remaining steps need no root:
  #
  #   tailscale serve --bg 8070        # publish the loopback port
  #   tailscale serve status
  #
  # Before that works, enable MagicDNS and HTTPS Certificates for the tailnet in
  # the admin console. `serve` terminates TLS with a certificate from that
  # feature; without it the node URL will not come up.
}
