# Security model

Sister RexenIOT is an agent with tools. Reaching its API means being able to
drive those tools, so the API is treated as a privileged interface rather than
a website.

## The two rules

1. **It binds to loopback by default.** Only this machine can connect.
2. **If it does not bind to loopback, it requires a token.**

The second rule is enforced, not documented-and-hoped-for: the server refuses to
start on a non-loopback address when no token is set, and the NixOS module
refuses to evaluate. A misconfiguration is a startup error, not a silent
exposure.

## Setting a token

```bash
export REXENIOT_API_TOKEN=$(openssl rand -hex 32)
```

Or put it in the config file as `server.auth_token`. The environment variable
wins, so the value can stay out of a file you might commit.

On NixOS, use `environmentFile` rather than `extraEnvironment` -- the latter
ends up in the world-readable Nix store:

```nix
services.rexeniot = {
  enable = true;
  host = "0.0.0.0";                 # only now that a token exists
  environmentFile = "/run/secrets/rexeniot.env";   # REXENIOT_API_TOKEN=...
  openFirewall = true;
};
```

Clients send the token as `X-API-Token`. The bundled web UI asks for it once and
keeps it in `sessionStorage`, which survives a reload but not a browser restart.

## What is protected

| Endpoint | Auth |
|---|---|
| `/api/health` | open, by design |
| `/api/status` | token |
| `/api/chat`, `/api/chat/stream` | token |
| `/api/speak`, `/api/transcribe`, `/api/uploads` | token |
| `/api/admin/*` | token (separate `admin_token`) |

`/api/health` is open so the watchdog and the browser can tell whether the
server is up before they have a token. It returns a version and a
`requires_token` boolean -- never the token itself.

The admin panel keeps its own token (`ADMIN_TOKEN` or `admin_token`). The two
are independent: the admin token guards configuration, the API token guards the
agent. Set both if you expose the server.

## CORS

Browser access is restricted to the loopback origins. Previously
`allow_origins=["*"]`, which meant any website you visited could script the
agent in your browser -- a page does not need to know your token if it can make
requests from a context that already has one.

## Running as an unprivileged user

The `run_shell_command` tool is off by default (`tools.allow_shell`). If you
enable it, the allowlist limits which commands are permitted, but the agent
still runs as whatever user the service runs as. On NixOS, prefer the default
`DynamicUser` over a real user with a home directory.

## Auditing what is exposed

On any machine running the app:

```bash
ss -tlnp | grep 8070
```

- `127.0.0.1:8070` -- loopback only, nothing to do.
- `0.0.0.0:8070` or `[::]:8070` -- reachable from the network. Confirm a token
  is set, or switch back to loopback.

From a phone with Wi-Fi off, try `http://<public-ip>:8070`. That is the only
test that answers "reachable from outside"; the `ss` output answers "reachable
from the LAN".

## Reporting

This is a personal project, not a supported product. If you find a hole, open an
issue rather than assuming it is known.
