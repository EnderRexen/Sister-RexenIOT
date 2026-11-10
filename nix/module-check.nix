# Contract checks for nix/module.nix, evaluated inside the real NixOS module
# system against real nixpkgs. Evaluation only -- nothing is built, so no nix
# daemon, build users or network access are required beyond nixpkgs itself.
#
# This exists because a syntax check is not enough. tree-sitter happily accepts a
# duplicate attribute and a mistyped option path, both of which are hard errors
# that would only surface at `nixos-rebuild switch`. Evaluating the module
# catches them, and pins the behaviour the module is supposed to have.
#
# Usage (nixpkgsPath is required -- there is deliberately no `<nixpkgs>`
# default, because tree-sitter cannot parse the angle-bracket search path and
# the flake always supplies this argument anyway):
#   nix-instantiate --eval --strict nix/module-check.nix \
#     --arg nixpkgsPath /path/to/nixpkgs
# or, on a NixOS host with a channel:
#   nix-instantiate --eval --strict nix/module-check.nix --arg nixpkgsPath '<nixpkgs>'
#
# Prints "ok" on success and throws with the failing assertions otherwise.
#
# `self` is stubbed: the module takes the flake's own `self` so it can pick the
# package matching `tier`, and stubbing it keeps this independent of the flake
# and of uv2nix, which would otherwise have to resolve a Python environment.

{
  nixpkgsPath,
  # Last formal and no trailing comma: the tree-sitter Nix grammar rejects a
  # trailing comma in a formals list, even though real Nix accepts it.
  modulePath ? ./module.nix
}:

let
  # Accept either a nixpkgs source path (what <nixpkgs> is) or a flake input,
  # which arrives as an attrset of outputs whose outPath is the source.
  nixpkgsRoot =
    if builtins.isPath nixpkgsPath || builtins.isString nixpkgsPath then
      nixpkgsPath
    else
      nixpkgsPath.outPath;

  lib = import (nixpkgsRoot + "/lib");
  mod = import modulePath;

  stub = "/nix/store/00000000000000000000000000000000-rexeniot-stub";
  stubSelf = {
    packages.x86_64-linux = {
      rexeniot = {
        outPath = stub;
      };
      rexeniot-stt = {
        outPath = stub;
      };
      rexeniot-full = {
        outPath = stub;
      };
    };
  };

  module = mod { self = stubSelf; };

  evalWith =
    extra:
    import (nixpkgsRoot + "/nixos/lib/eval-config.nix") {
      system = "x86_64-linux";
      modules = [
        module
        { system.stateVersion = "25.05"; }
        extra
      ];
    };

  default = evalWith { services.rexeniot.enable = true; };
  ordered = evalWith {
    services.rexeniot.enable = true;
    services.rexeniot.llm.afterLlamaSwap = true;
  };
  declared = evalWith {
    services.rexeniot.enable = true;
    services.rexeniot.llm.baseUrl = "http://127.0.0.1:8080/v1";
    services.rexeniot.llm.model = "qwen3-8b";
  };

  unitOf = e: e.config.systemd.services.rexeniot;

  failures = builtins.filter (f: f != null) [
    (
      if (unitOf default).after == [ "network.target" ] then
        null
      else
        "the default must not order against llama-swap.service (got ${builtins.toJSON (unitOf default).after})"
    )
    (
      if lib.elem "llama-swap.service" (unitOf ordered).after then
        null
      else
        "llm.afterLlamaSwap = true must add llama-swap.service to After"
    )
    (
      if (unitOf default).wantedBy == [ "multi-user.target" ] then
        null
      else
        "the service must be wanted by multi-user.target"
    )
    (
      if lib.all (
        t: (evalWith { services.rexeniot.tier = t; }).config.services.rexeniot.tier == t
      ) [ "light" "stt" "full" ] then
        null
      else
        "tier must accept light, stt and full"
    )
    # The seeding gate is baseUrl/model, not provider (which defaults to
    # "openai"). Unset must write no llm section, so the admin panel keeps
    # owning the model choice; set must seed it.
    (
      let
        seed = u: builtins.filter (p: lib.hasInfix "llm" (toString p)) (u.serviceConfig.ExecStartPre or [ ]);
      in
      if seed (unitOf default) == [ ] then
        null
      else
        "an unset llm.baseUrl/model must not seed a config"
    )
    (
      let
        seed = u: builtins.filter (p: lib.hasInfix "llm" (toString p)) (u.serviceConfig.ExecStartPre or [ ]);
      in
      if seed (unitOf declared) != [ ] then
        null
      else
        "a declared llm.baseUrl/model must seed a config"
    )
    (
      if lib.hasInfix "--port 8070" (unitOf default).serviceConfig.ExecStart then
        null
      else
        "ExecStart must carry the configured port"
    )
    # The binding guard. NixOS assertions are only thrown when the system is
    # built, so forcing a service does not exercise them; this inspects their
    # values directly, which is the same condition NixOS checks, run earlier.
    (
      let
        bad = evalWith {
          services.rexeniot.enable = true;
          services.rexeniot.host = "0.0.0.0";
        };
        guard = builtins.filter (a: lib.hasInfix "reachable from" a.message) bad.config.assertions;
      in
      if lib.any (a: !a.assertion) guard then
        null
      else
        "a non-loopback host with no token must trip the binding assertion"
    )
    (
      let
        good = evalWith { services.rexeniot.enable = true; };
        guard = builtins.filter (a: lib.hasInfix "reachable from" a.message) good.config.assertions;
      in
      if guard != [ ] && lib.all (a: a.assertion) guard then
        null
      else
        "the default loopback config must satisfy the binding assertion"
    )
  ];
in
if failures == [ ] then
  "ok"
else
  throw "module-check failed:\n  - ${lib.concatStringsSep "\n  - " failures}"
