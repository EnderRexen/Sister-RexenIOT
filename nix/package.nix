# Standalone build of Sister RexenIOT.
#
# The Python environment is produced by uv2nix (see flake.nix) and passed in as
# pythonEnv, so this derivation only has to place the non-Python assets and wrap
# the entry point.
{
  lib,
  stdenv,
  makeWrapper,
  writeShellScript,
  coreutils,
  # Named explicitly rather than relying on implicit scoping, so a caller that
  # forgets it fails loudly instead of silently picking up nixpkgs' python3.
  pythonEnv,
  pname ? "rexeniot",
  version ? "0.1.0",
  # Last on purpose: a path literal directly before a comma trips the
  # tree-sitter Nix grammar, and `src` is the only path default here.
  src ? ./..
}:

let
  # app/config.py resolves ROOT from $REXENIOT_HOME and then expects `web/` and
  # a writable `data/` beneath it. Inside the store ROOT would be read-only, so
  # the launcher points REXENIOT_HOME at a writable state directory and links
  # the packaged web assets into it. The link is rewritten on every start, so a
  # rebuild cannot leave it dangling at a collected store path.
  #
  # REXENIOT_WEB_DIR is injected by makeWrapper below rather than interpolated
  # here, which keeps this string free of nested shell heredocs.
  launcher = writeShellScript "rexeniot" ''
    set -eu

    export REXENIOT_HOME="''${REXENIOT_HOME:-''${XDG_DATA_HOME:-$HOME/.local/share}/rexeniot}"
    mkdir -p "$REXENIOT_HOME"

    if [ -L "$REXENIOT_HOME/web" ] || [ ! -e "$REXENIOT_HOME/web" ]; then
      ln -sfn "$REXENIOT_WEB_DIR" "$REXENIOT_HOME/web"
    fi

    exec ${pythonEnv}/bin/rexeniot "$@"
  '';
in
stdenv.mkDerivation {
  inherit pname version src;

  nativeBuildInputs = [ makeWrapper ];

  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall

    mkdir -p $out/share/rexeniot $out/bin
    cp -r web $out/share/rexeniot/web
    cp README.md $out/share/rexeniot/README.md 2>/dev/null || true

    makeWrapper ${launcher} $out/bin/rexeniot \
      --prefix PATH : ${lib.makeBinPath [ coreutils ]} \
      --set REXENIOT_WEB_DIR $out/share/rexeniot/web

    runHook postInstall
  '';

  passthru.pythonEnv = pythonEnv;

  meta = with lib; {
    description = "Sister RexenIOT -- local-first voice assistant and home-keeper";
    longDescription = ''
      A local-first voice assistant: a FastAPI web app, ONNX speech synthesis,
      optional local dictation, and optional MQTT home-automation integration.
      Every install is self-contained -- models, caches and configuration all
      live under a single state directory.
    '';
    homepage = "https://github.com/rexen/rexeniot";
    license = licenses.mit;
    platforms = platforms.linux;
    mainProgram = "rexeniot";
  };
}
