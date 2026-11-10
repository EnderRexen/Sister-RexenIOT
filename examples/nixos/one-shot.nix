# One rebuild, one keyboard, done.
#
# You are at the machine once. This file is written so that single rebuild
# leaves you with remote access that never needs root again. Everything that
# would otherwise need a second trip is in here.
#
# It supersedes ssh.nix and remote-admin.nix for the bootstrap case -- use one
# or the other, not both, because they define overlapping options and the merge
# is easy to get wrong when you are standing at a keyboard with a deadline.
#
# Before you run this, do the two things below. Both are outside Nix and neither
# can be done from inside a rebuild.
#
#   1. Tailscale auth key, in a root-only file:
#        sudo install -m 0400 /dev/null /run/secrets/tailscale_key
#        sudo sh -c "printf '%s' 'tskey-auth-XXXX' > /run/secrets/tailscale_key"
#
#      /run is tmpfs, so this is gone after a reboot. That is fine: the service
#      reads it at start. If you want it to survive, use sops-nix or agenix.
#
#   2. A password you actually know, for the account below. You will log in with
#      it first and switch to a key afterwards:
#        sudo passwd ender
#
# Then: sudo nixos-rebuild switch --flake .#REXEN-NIXOS-DT

{ config, pkgs, lib, ... }:

let
  operator = "ender";
  # Your public key (.pub, not a secret). Optional -- password auth is on, so
  # you can add this later with ssh-copy-id and it will still work.
  operatorKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... you@client";
in
{
  # ------------------------------------------------------------------ sshd
  # The only way in that does not depend on the tailnet being up. Deliberately
  # permissive: password auth is ON, because turning it off in the same rebuild
  # that first enables sshd is how people lose a remote machine.
  services.openssh = {
    enable = true;
    ports = [ 22 ];
    openFirewall = true;
    settings = {
      PasswordAuthentication = true;
      PubkeyAuthentication = true;
      PermitRootLogin = "no";
    };
  };

  users.users.${operator}.openssh.authorizedKeys.keys = [ operatorKey ];

  # ------------------------------------------------------------------ Tailscale
  services.tailscale = {
    enable = true;

    # Connects on boot with no browser login, so a reboot does not log the node
    # out and strand you a second time.
    authKeyFile = "/run/secrets/tailscale_key";

    # Your account may drive tailscaled without sudo. This is what makes
    # `tailscale serve` work later without another root step.
    extraSetFlags = [ "operator=${operator}" ];

    # Tailscale SSH: sshd inside tailscaled. No port, no firewall rule, no keys
    # -- who may log in is set in the tailnet ACL policy, not on this machine.
    extraUpFlags = [ "--ssh" ];
  };

  # ------------------------------------------------------------------ TeamViewer
  # Optional. Included because you are already at the keyboard for one rebuild,
  # so it costs nothing extra -- but be clear about what it does and does not
  # buy you.
  #
  # It gives you a *picture* of the screen: look, click, type. It does not give
  # you a shell, so it cannot run `nixos-rebuild`, `journalctl`, or anything
  # scriptable. For managing the machine, the SSH path above is the one that
  # matters. TeamViewer is a fallback for when you need to see the desktop.
  #
  # X11 confirmed, so screen capture works. On Wayland this would silently
  # fail to share the screen.
  nixpkgs.config.allowUnfreePackages = [ "teamviewer" ];
  services.teamviewer.enable = true;

  # The daemon starts on boot, but the account login is interactive and cannot
  # be scripted -- it needs your email, password, and usually a second factor.
  # So it happens in the same keyboard session as this rebuild:
  #
  #   1. Launch TeamViewer from the app menu.
  #   2. Sign in to your account.
  #   3. Set a personal password for unattended access, and tick
  #      "Grant easy access" / start-with-system.
  #
  # Until step 2, the machine shows up nowhere and nobody can connect.
  # `teamviewer passwd <pw>` sets the unattended password from the CLI, but the
  # account assignment itself is GUI-only. There is no supported way to do the
  # login from a script, which is why this cannot be set up for you remotely.

  # ------------------------------------------------------------------ RexenIOT
  services.rexeniot = {
    enable = true;
    tier = "stt";
    # Left on loopback: `tailscale serve` only proxies to http://127.0.0.1, so
    # this is both the safe choice and the only one that works with serve.
    llm = {
      provider = "openai";
      baseUrl = "http://127.0.0.1:8080/v1";
      model = "qwen3-8b";
    };
  };

  # After the rebuild, still with no root:
  #
  #   tailscale serve --bg 8070
  #   tailscale serve status
  #
  # MagicDNS and HTTPS Certificates must be on for the tailnet first, or serve
  # has no certificate and the node URL never comes up.

  # And later, when you have confirmed key login works, tighten sshd in a
  # second rebuild -- this one can be done over SSH, no keyboard needed:
  #
  #   PasswordAuthentication = false;
  #   KbdInteractiveAuthentication = false;
}
