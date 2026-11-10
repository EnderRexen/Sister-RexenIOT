# Administering this machine remotely

The problem this directory solves: you are not at the machine, you cannot run
`sudo`, and the remote access you want is the thing that needs root to set up.
That is a bootstrapping trap, and no amount of config on my side breaks it. What
this setup does is make the *one* moment of access you get count.

## The trap, precisely

| Step | Needs root? |
|---|---|
| `nixos-rebuild switch` | yes -- writes the system configuration |
| `tailscale up` | yes -- creates a TUN device, starts `tailscaled` |
| `tailscale serve` (port forward) | **no** |
| `tailscale serve` (file or directory) | yes |

So the only part that is root-free is `serve`, and it is useless until
`tailscaled` is running and you are its operator. Setting *that* up is the first
root step. Hence the trap.

## Breaking it, once

You need exactly one of these, one time:

- physical access to the machine, or
- an existing remote path you have not mentioned (SSH, a web console, IPMI/KVM,
  someone who can type), or
- a rescue boot / live USB.

With that single access, apply `remote-admin.nix` and the trap is gone
permanently: after the rebuild, `operator=` means every future `tailscale`
command, including `serve`, works **without sudo**.

```bash
# 1. put the auth key where the module expects it (one time, as root)
sudo install -m 0400 /dev/null /run/secrets/tailscale_key
sudo sh -c "printf '%s' 'tskey-auth-XXXX' > /run/secrets/tailscale_key"

# 2. merge remote-admin.nix into your flake's host config, then commit -- flakes
#    only see git-tracked files
git add -A && git commit -m "tailscale + rexeniot"

# 3. build first (changes nothing running), then switch
sudo nixos-rebuild build --flake .#REXEN-NIXOS-DT
sudo nixos-rebuild switch --flake .#REXEN-NIXOS-DT
```

Then, with no root at all:

```bash
tailscale serve --bg 8070
tailscale serve status
```

## Why each piece is there

**`authKeyFile`** -- connects on boot with no browser login. Without it a reboot
logs the node out and you lose access again, which is the failure mode that
turns a working setup into a stranded machine.

**`extraSetFlags = [ "operator=you" ]`** -- the whole point. It lets your account
drive `tailscaled` without sudo.

**MagicDNS + HTTPS Certificates** -- must be enabled in the Tailscale admin
console *before* `serve` will work. `serve` terminates TLS with a certificate
from that feature. This is the single most common silent failure: `serve` looks
like it worked but the node URL never comes up.

**Not advertising routes or an exit node** -- a subnet router exposes your whole
LAN to every device on the tailnet, not just this machine. Add nodes
individually.

## If there is no access at all

Then this is not a configuration problem, and no command fixes it. There is no
remote path from this development sandbox to your machine -- no SSH key, no
route, no credential. If you have no physical access, no existing remote path,
and no one who can run a command for you, the machine needs to be reached
another way before any of this applies.

## What is verified here and what is not

Verified against upstream sources: the `authKeyFile` and `extraSetFlags` options
exist and behave as described; `tailscale serve` needs no root for port
forwarding; only `http://127.0.0.1` is supported as a `serve` proxy target;
`REXEN-NIXOS-DT` is a valid hostname (nixpkgs allows uppercase, systemd's LDH
check uses `ascii_isalpha`).

Not verified: anything actually running. There is no Nix daemon in the
development sandbox, so `nixos-rebuild` on your machine is the first real test.
It fails loudly on an unknown option, which is the behaviour you want.
