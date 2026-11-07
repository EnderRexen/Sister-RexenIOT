{
  description = "Sister RexenIOT -- local-first voice assistant and home-keeper";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      # The gpu and directml extras both install the `onnxruntime` module, so
      # they are mutually exclusive by construction -- that is why pyproject.toml
      # declares them as a uv `conflicts` pair. mkPyprojectOverlay defaults to
      # deps.all, which would try to resolve both at once and fail. The overlay
      # therefore covers exactly the set Nix builds: base + stt + full.
      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
        dependencies = {
          rexeniot = [
            "stt"
            "full"
          ];
        };
      };

      editableOverlay = workspace.mkEditablePyprojectOverlay {
        root = "$REPO_ROOT";
      };

      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        (pkgs.callPackage pyproject-nix.build.packages {
          python = pkgs.python3;
        }).overrideScope
          (
            lib.composeManyExtensions [
              pyproject-build-systems.overlays.wheel
              overlay
            ]
          )
      );

      # Build one wrapped application per dependency tier. `deps.default` is the
      # light tier: no optional dependencies at all.
      mkApp = system: pname: deps: (
        nixpkgs.legacyPackages.${system}.callPackage ./nix/package.nix {
          inherit pname;
          pythonEnv = pythonSets.${system}.mkVirtualEnv "${pname}-env" deps;
        }
      );
    in
    {
      packages = forAllSystems (system: {
        default = self.packages.${system}.rexeniot;
        rexeniot = mkApp system "rexeniot" workspace.deps.default;
        rexeniot-stt = mkApp system "rexeniot-stt" {
          rexeniot = [ "stt" ];
        };
        rexeniot-full = mkApp system "rexeniot-full" {
          rexeniot = [
            "stt"
            "full"
          ];
        };
      });

      overlays.default = final: prev: {
        rexeniot = self.packages.${final.stdenv.hostPlatform.system}.rexeniot;
      };

      nixosModules = {
        rexeniot = import ./nix/module.nix { inherit self; };
        default = self.nixosModules.rexeniot;
      };

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system}.overrideScope editableOverlay;
          virtualenv = pythonSet.mkVirtualEnv "rexeniot-dev-env" workspace.deps.default;
        in
        {
          default = pkgs.mkShell {
            packages = [
              virtualenv
              pkgs.uv
            ];
            env = {
              UV_NO_SYNC = "1";
              UV_PYTHON = pythonSet.python.interpreter;
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT=$(git rev-parse --show-toplevel)
            '';
          };
        }
      );

      checks = forAllSystems (system: {
        inherit (self.packages.${system}) rexeniot;
      });

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixpkgs-fmt);
    };
}
