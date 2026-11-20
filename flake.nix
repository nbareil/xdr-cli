{
  description = "xdr-cli — pilot Cortex XDR from the command-line";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        inherit (pkgs) lib;
        python = pkgs.python311;

        # Only ship the files needed to build the package, so editor/build
        # clutter (.venv, build/, *.egg-info, caches) never lands in the store.
        src = lib.fileset.toSource {
          root = ./.;
          fileset = lib.fileset.unions [
            ./pyproject.toml
            ./README.md
            ./src
          ];
        };

        xdr-cli = python.pkgs.buildPythonApplication {
          pname = "xdr-cli";
          version = "0.1.0";
          pyproject = true;
          inherit src;

          build-system = [ python.pkgs.hatchling ];

          dependencies = with python.pkgs; [
            click
            httpx
            xlsxwriter
            # The sftp extra, shipped by default so `get-large-file --via sftp`
            # works from the nix package.
            paramiko
          ];

          optional-dependencies = {
            slack = with python.pkgs; [ slack-bolt aiohttp ];
            mcp = with python.pkgs; [ mcp ];
          };

          # No test suite is wired into the build yet.
          doCheck = false;

          pythonImportsCheck = [ "xdr_cli" ];

          meta = {
            description = "Pilot Cortex XDR from the command-line";
            mainProgram = "xdr-cli";
            platforms = lib.platforms.unix;
          };
        };
      in
      {
        packages = {
          default = xdr-cli;
          xdr-cli = xdr-cli;
        };

        apps.default = flake-utils.lib.mkApp { drv = xdr-cli; };

        devShells.default = pkgs.mkShell {
          packages = [
            python
            pkgs.uv
            pkgs.curl
            pkgs.jq
          ];

          shellHook = ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
            export UV_PYTHON_DOWNLOADS=never
            echo "xdr-cli nix dev shell ready (uv + python, PYTHONPATH=src)"
          '';
        };

        formatter = pkgs.nixpkgs-fmt;
      });
}
