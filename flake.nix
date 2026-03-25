{
  description = "xdr-cli development shell";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.python311
            pkgs.uv
            pkgs.curl
            pkgs.jq
          ];

          shellHook = ''
            export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
            export UV_PYTHON_DOWNLOADS=never
            echo "xdr-cli nix dev shell ready (uv + python, PYTHONPATH=src)"
          '';
        };
      });
}
