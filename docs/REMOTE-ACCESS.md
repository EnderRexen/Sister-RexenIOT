# Remote access

You want to reach Sister RexenIOT from your phone or laptop when you are not at
home. There are two ways to do that, and they are not equivalent.

## The short version

Put the machine on a private overlay (Tailscale or WireGuard) and **leave the
server on `127.0.0.1`**. Then:

```bash
tailscale serve --bg 8070
```

The app is now reachable from your own devices anywhere, and from nothing else.
No port forwarding, no firewall hole, nothing visible to a port scanner.

## Why not just bind to 0.0.0.0

Binding to `0.0.0.0` makes the server listen on every interface. Two problems:

1. **It is on your LAN, not just the internet.** Every device on your Wi-Fi --
   including guests, and any device that is compromised -- can reach it.
2. **Reaching it means driving it.** The API is an agent with tools. There is no
   login screen between a network connection and the agent; that is what the
   token in `docs/SECURITY.md` is for.

If you do bind to a non-loopback address, the app now requires a token and will
refuse to start without one.

## Tailscale

Install and authenticate:

```bash
sudo tailscale up
tailscale status          # note this machine's 100.x.y.z address
```

Then proxy to the loopback port:

```bash
tailscale serve --bg 8070
```

`tailscale serve` terminates TLS and forwards to `127.0.0.1:8070`, so the app
never leaves loopback. Access it at `https://<machine>.<tailnet>.ts.net/`.

To check what is being served, and to turn it off:

```bash
tailscale serve status
tailscale serve --https=443 off
```

### Two things to avoid

**Do not advertise a subnet** (`tailscale up --advertise-routes=...`). That
exposes your whole home network to every device in the tailnet, not just this
node. Add individual machines instead.

**Do not use Funnel** (`tailscale funnel`) unless you specifically want the
public internet to reach it. Funnel publishes to the open internet; `serve` does
not. For a personal assistant, `serve` is almost always what you want.

## WireGuard

If you would rather not use a hosted coordination service, WireGuard is the
same idea with more setup. Keep the server on `127.0.0.1` and reach it over the
tunnel address:

```
[Peer]
AllowedIPs = 10.0.0.2/32
```

## NixOS

Tailscale is in nixpkgs; the NixOS wiki's Tailscale page covers the service
options. The short form:

```nix
services.tailscale.enable = true;
```

Leave `services.rexeniot.host` at its default. `tailscale serve` does the rest,
and the module's assertion stays satisfied.

## Mobile clients

The web UI is responsive and works in a phone browser. Over Tailscale, open
`https://<machine>.<tailnet>.ts.net/` and enter the API token once; it is kept in
`sessionStorage` for the tab's lifetime.

## If you cannot use an overlay

Then you need a token **and** TLS. At minimum:

1. Set `REXENIOT_API_TOKEN` (see `docs/SECURITY.md`).
2. Put a TLS-terminating reverse proxy in front (nginx, Caddy). Do not send the
   token over plain HTTP.
3. Restrict the firewall to the proxy.

Port-forwarding the app directly to the internet is the one option worth ruling
out: the token travels in a header, and without TLS that header is readable by
anyone on the path.
