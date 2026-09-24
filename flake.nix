{
  description = "Personal learning development setup for the VenTapes fork of Mixtapes";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, utils }:
    utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        version = pkgs.lib.removeSuffix "\n" (builtins.readFile (self + "/VERSION"));

        lines = pkgs.lib.pipe (self + "/requirements.txt") [
          builtins.readFile
          (builtins.split "\n")
          (builtins.filter (x: builtins.isString x && x != ""))
        ];

        pipName = line: let
          m = builtins.match "([a-zA-Z][a-zA-Z0-9._-]*).*" line;
        in
          if m == null then null else builtins.head m;

        pipToNix = {
          PyGObject = "pygobject3";
          Pillow = "pillow";
          StrEnum = "strenum";
        };

        nixAttr = name: pipToNix.${name} or (pkgs.lib.strings.toLower name);

        pythonDeps = pkgs.lib.pipe lines [
          (builtins.map pipName)
          (builtins.filter (n: n != null))
          (builtins.map (name: pkgs.python314Packages.${nixAttr name} or null))
          (builtins.filter (d: d != null))
        ];

        pythonEnv = pkgs.python314.withPackages (ps: pythonDeps);
      in {
        packages.default = pkgs.stdenv.mkDerivation {
          pname = "ventapes";
          inherit version;
          src = self;

          nativeBuildInputs = [
            pkgs.makeWrapper
            pkgs.wrapGAppsHook3
            pkgs.glib
          ];

          buildInputs = [
            pkgs.gtk4
            pkgs.libadwaita
            pkgs.webkitgtk_6_0
            pkgs.gobject-introspection
            pkgs.gst_all_1.gstreamer
            pkgs.gst_all_1.gst-plugins-base
            pkgs.gst_all_1.gst-plugins-good
            pkgs.gst_all_1.gst-plugins-bad
            pkgs.gst_all_1.gst-plugins-ugly
            pythonEnv
          ];

          installPhase = ''
            glib-compile-resources \
              --sourcedir=. \
              src/ventapes.gresource.xml \
              --target=src/ventapes.gresource

            mkdir -p \
              $out/bin \
              $out/share/ventapes \
              $out/share/applications \
              $out/share/metainfo \
              $out/share/icons/hicolor/scalable/apps \
              $out/share/icons/hicolor/symbolic/apps \
              $out/share/licenses/ventapes
            cp -r src/* $out/share/ventapes/
            cp -r assets $out/share/ventapes/
            install -Dm644 io.github.realvenerable.VenTapes.desktop \
              $out/share/applications/io.github.realvenerable.VenTapes.desktop
            install -Dm644 io.github.realvenerable.VenTapes.metainfo.xml \
              $out/share/metainfo/io.github.realvenerable.VenTapes.metainfo.xml
            install -Dm644 assets/icons/hicolor/scalable/apps/io.github.realvenerable.VenTapes.svg \
              $out/share/icons/hicolor/scalable/apps/io.github.realvenerable.VenTapes.svg
            install -Dm644 assets/icons/hicolor/symbolic/apps/io.github.realvenerable.VenTapes-symbolic.svg \
              $out/share/icons/hicolor/symbolic/apps/io.github.realvenerable.VenTapes-symbolic.svg
            install -Dm644 LICENSE $out/share/licenses/ventapes/LICENSE
            install -Dm644 NOTICE.md $out/share/licenses/ventapes/NOTICE.md
            install -Dm644 CREDITS.md $out/share/licenses/ventapes/CREDITS.md
            install -Dm644 THIRD_PARTY_NOTICES.md $out/share/licenses/ventapes/THIRD_PARTY_NOTICES.md
            makeWrapper ${pythonEnv}/bin/python $out/bin/ventapes \
              --add-flags "$out/share/ventapes/main.py"
          '';
        };

        devShells.default = pkgs.mkShell {
          inputsFrom = [ self.packages.${system}.default ];
          packages = [ pkgs.nodejs ];
          shellHook = ''
            echo "VenTapes: private learning fork; not a supported public release."
            python --version
          '';
        };
      }
    );
}
