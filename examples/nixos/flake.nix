# Standalone flake that imports the RexenIOT module, for a system config that is
# already flake-based. If your configuration.nix is plain (non-flake), use the
# sibling configuration.nix instead.
#
# Adjust:
#   - `rexeniot.url`  -- where this repo lives (a path: input works for a local
#                        checkout, which is convenient while testing)
#   - `desktop`       -- your hostname
#   - `system`        -- your architecture

{
  description = "NixOS desktop running Sister RexenIOT";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # `path:` points at a local checkout. Switch to `github:OWNER/REPO` once
    # it is pushed. `follows` reuses the nixpkgs above rather than resolving a
    # second copy, which keeps the closure small.
    rexeniot = {
      url = "path:/home/ender/sri";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      rexeniot,
      ...
    }:
    {
      nixosConfigurations.desktop = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          # The module that defines services.rexeniot.*
          rexeniot.nixosModules.rexeniot
          # Your own configuration, including the services.rexeniot block from
          # the sibling configuration.nix.
          ./configuration.nix
        ];
      };
    };
}
