# SSH for a machine you administer remotely.
#
# Two independent paths, and it is worth having both:
#
#   1. services.openssh  -- ordinary sshd on port 22. Works on the LAN and,
#      once Tailscale is up, over the tailnet too. Needs a firewall hole and
#      key management.
#
#   2. Tailscale SSH    -- sshd built into tailscaled. No port 22, no firewall
#      rule, no keys to distribute; authorisation comes from your tailnet ACL
#      policy. Reachable from anywhere your tailnet reaches, and from nowhere
#      else.
#
# For a machine you are not sitting at, path 2 is the one that saves you: there
# is no listening port to expose and no key to get wrong. Path 1 is worth
# keeping as a fallback for when the tailnet is down.
#
# Read REMOTE-ADMIN.md first. This file assumes the same `operator` idea.

{ config, pkgs, lib, ... }:

let
  operator = "ender";
  # Your public key, as a string. This is not a secret -- it is the .pub half.
  # If you would rather keep it in a file, use
  # `users.users.${operator}.openssh.authorizedKeys.keyFiles = [ ... ];`
  # instead of the `keys` list below.
  operatorKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... you@host";
in
{
  # ------------------------------------------------------------------ sshd
  services.openssh = {
    enable = true;

    # Port 22. Keep it: Tailscale SSH does not use it, so this is purely the
    # fallback path.
    ports = [ 22 ];

    # Opening this makes sshd reachable on every interface. That is the point
    # of a fallback, but it is also the step that can lock you out if something
    # else in the same rebuild goes wrong -- see the ordering note in
    # REMOTE-ADMIN.md. If you only ever reach this machine over the tailnet,
    # set this to false and let Tailscale carry the traffic.
    openFirewall = true;

    # The modern way to set sshd_config values. The old top-level options
    # (services.openssh.passwordAuthentication and friends) were removed; using
    # them is a "option does not exist" error, which is a safe failure but a
    # confusing one.
    settings = {
      # Bootstrap values: the ones that cannot lock you out.
      #
      # PasswordAuthentication stays ON for now. Turning it off in the same
      # rebuild that first enables sshd strands the box if the key is not
      # already working -- the classic way to lose a remote machine. Confirm
      # key login first, then tighten.
      PasswordAuthentication = true;
      PubkeyAuthentication = true;
      PermitRootLogin = "no";

      # Tighten to these in a later rebuild, once `ssh -i <key>` works:
      #   PasswordAuthentication = false;
      #   KbdInteractiveAuthentication = false;
    };
  };

  users.users.${operator}.openssh.authorizedKeys.keys = [ operatorKey ];

  # ------------------------------------------------------------------ Tailscale SSH
  services.tailscale = {
    enable = true;

    # Connect on boot with no browser login. Without this a reboot logs the
    # node out and takes the fallback path with it.
    authKeyFile = "/run/secrets/tailscale_key";

    # `operator=` lets your own account run `tailscale` without sudo.
    extraSetFlags = [ "operator=${operator}" ];

    # `--ssh` turns on Tailscale SSH. Only applied because authKeyFile is set.
    #
    # Authorisation for this comes from the `ssh` section of your tailnet ACL
    # policy, not from files on this machine -- so who may log in is changed in
    # the admin console, with no rebuild and no key to rotate. The default
    # policy allows the owner to reach their own devices.
    #
    # Tailscale SSH is only reachable over the tailnet. Nothing is exposed.
    extraUpFlags = [ "--ssh" ];
  };

  # With both paths up, `ssh operator@REXEN-NIXOS-DT` works from any tailnet
  # device with no key setup, and plain `ssh operator@<lan-ip>` still works on
  # the LAN. Neither needs a port forward on your router.
}
