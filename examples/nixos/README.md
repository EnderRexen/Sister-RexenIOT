# Running Sister RexenIOT on a NixOS desktop

Two files here:

- `configuration.nix` -- a fragment to merge into an existing NixOS config.
- `flake.nix` -- a standalone flake that imports the RexenIOT module, if your
  system config is already a flake.

Both leave the service on loopback and use Tailscale to publish it. That is the
whole design: nothing binds to a network interface, so there is no port to scan.

## Why loopback plus `tailscale serve`

`tailscale serve` proxies to `http://127.0.0.1` only -- the Tailscale docs are
explicit that "only `http://127.0.0.1` is supported for proxies". So the
loopback binding is not just the safe choice here, it is the *only* choice that
works with `serve`. The two decisions reinforce each other.

The result: the app is reachable from your phone and laptop anywhere, over a
WireGuard mesh, and from nothing else. No port forward, no firewall hole.

## Steps

1. **Merge the config and rebuild.**

   ```bash
   sudo nixos-rebuild switch --flake .#desktop    # flake variant
   # or, for the plain configuration.nix:
   sudo nixos-rebuild switch
   ```

2. **Bring up Tailscale.**

   ```bash
   sudo tailscale up
   tailscale status
   ```

3. **Enable HTTPS certificates for your tailnet.** In the Tailscale admin
   console, turn on MagicDNS and HTTPS Certificates. `serve` terminates TLS with
   a certificate from that feature; without it the node URL will not come up.

4. **Publish the loopback port.**

   ```bash
   tailscale serve --bg 8070
   tailscale serve status
   ```

   `--bg` makes it persist across reboots and `tailscale up/down`, so this is a
   one-time command. Undo with `tailscale serve reset`.

5. **Open it.** From any device in your tailnet:

   ```
   https://<machine>.<tailnet>.ts.net/
   ```

## Verifying

```bash
ss -tlnp | grep 8070
```

You want `127.0.0.1:8070`. If you see `0.0.0.0:8070` or `[::]:8070`, something
is binding to the network and this setup is not in effect.

```bash
tailscale serve status
```

You want the node URL listed with `--> http://127.0.0.1:8070`.

## Two things not to do

- **Do not set `openFirewall = true`.** It opens the port on every interface.
  It is only meaningful with a non-loopback `host`, and that combination
  requires a token.
- **Do not use `tailscale funnel`.** Funnel publishes to the open internet;
  `serve` does not. For a personal assistant, `serve` is what you want. Also do
  not `--advertise-routes` a subnet: that exposes your whole home network to the
  tailnet, not just this machine.

## About the token

On loopback the API token is optional -- nothing off-machine can reach it. Set
`REXENIOT_API_TOKEN` in `environmentFile` anyway if you want the second layer;
it costs nothing and means a future change of `host` is not a silent exposure.

If you ever set `host` to something non-loopback, the service refuses to start
without a token, and the module refuses to evaluate. That is deliberate: a
misconfiguration should be a startup error, not a quiet hole. See
`docs/SECURITY.md`.

## Checking the module

```bash
nix flake check
```

This evaluates the NixOS module, which is the only thing that catches a mistyped
option path or a broken assertion -- a syntax check accepts both. The check
lives in `nix/module-check.nix`.

## A note on these files

They were written against the module's option names (`services.rexeniot.*`,
`services.tailscale.enable`) and are not machine-verified: the development
sandbox has no Nix daemon, so `nix flake check` and `nixos-rebuild` could not be
run here. Read them once before you switch, and let `nixos-rebuild` be the
verifier -- it fails loudly on an unknown option rather than silently ignoring
it.
