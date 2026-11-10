# Administering this machine remotely

The problem this directory solves: you are not at the machine, you cannot run
`sudo`, and the remote access you want is the thing that needs root to set up.
That is a bootstrapping trap, and no amount of config on my side breaks it. What
this setup does is make the *one* moment of access you get count.

**If you have exactly one trip to the keyboard, use `one-shot.nix`.** It is the
whole bootstrap in a single rebuild -- sshd for a way in, Tailscale for
`operator=` so you never need root again, and RexenIOT on loopback. It
supersedes `ssh.nix` and `remote-admin.nix`; use one or the other, not both.

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

## SSH: two paths, and the one that needs no root

`ssh.nix` sets up both, deliberately:

**Tailscale SSH** (`extraUpFlags = [ "--ssh" ]`) runs sshd inside `tailscaled`.
No port 22, no firewall rule, no keys to distribute -- authorisation comes from
the `ssh` section of your tailnet ACL policy. For a machine you are not sitting
at, this is the path that saves you, because there is nothing to expose and no
key to get wrong. It needs the tailnet up, which needs the one root step.

**Plain `services.openssh`** on port 22 is the fallback for when the tailnet is
down. It needs a firewall hole and key management.

### The part that needs no root

If you can already reach the machine over SSH *at all* -- even with a password --
then installing a key needs no root. `~/.ssh/authorized_keys` is a file in your
own home directory:

```bash
# on your client, not the server
ssh-keygen -t ed25519 -C "you@client"          # if you have no key yet
ssh-copy-id you@REXEN-NIXOS-DT                 # appends your .pub to the server
```

That is a user-level change. What *does* need root is turning password
authentication off, and that is deliberate: it is the setting that locks you out
if the key is wrong. Do it in a later rebuild, after you have confirmed the key
works.

### The lockout order, which matters more than the config

Applying `ssh.nix` as written turns password auth off in the same rebuild that
first enables sshd. If your key is not already installed and working, that
rebuild leaves you with a machine you cannot reach at all. So:

1. Enable sshd with `PasswordAuthentication = true`. Rebuild. Confirm you can
   log in from your client.
2. Install your key (`ssh-copy-id`). Confirm `ssh -i ~/.ssh/id_ed25519 you@host`
   works with the key alone.
3. *Then* flip `PasswordAuthentication = false` and rebuild.

The comment in `ssh.nix` says this too, next to the option itself, because that
is where someone will read it.

### `openFirewall = true` is the same class of decision

It exposes port 22 on every interface. If the machine is only ever reached over
the tailnet, set it to `false` and let Tailscale carry the traffic -- Tailscale
SSH needs no port open at all.

## What is verified here and what is not

Verified against upstream sources: the `authKeyFile`, `extraSetFlags` and
`extraUpFlags` options exist and behave as described; `services.openssh` has
`enable`, `ports`, `openFirewall` and `settings` (the modern names -- the old
top-level `passwordAuthentication` was removed); `tailscale serve` needs no root
for port forwarding; only `http://127.0.0.1` is supported as a `serve` proxy
target; `REXEN-NIXOS-DT` is a valid hostname (nixpkgs allows uppercase,
systemd's LDH check uses `ascii_isalpha`).

Verified by execution, in the development sandbox: the sshd settings above were
checked with a real `sshd -t` (OpenSSH 10.0p2), which accepts the intended
config and rejects a deliberately bogus option -- so the check has teeth, and
`sshd -T` confirms the effective values are `passwordauthentication no`,
`permitrootlogin no`, `pubkeyauthentication yes`.

Not verified: anything running on your machine. There is no Nix daemon in the
sandbox, so `nixos-rebuild` there is the first real test. It fails loudly on an
unknown option, which is the behaviour you want.
