# Arch Linux prerelease packaging

This is a local Arch `PKGBUILD` for the private VenTapes learning build. It is
not an AUR package and is not an official or supported Mixtapes release.

Build from this directory on an Arch-based system:

```bash
makepkg -sr
```

Install the resulting package with:

```bash
sudo pacman -U ./ventapes-*.pkg.tar.zst
```

The package installs a `/usr/bin/ventapes` wrapper and keeps its Python
integration modules under `/usr/lib/ventapes`. It uses the system Python,
GTK4, Libadwaita, WebKitGTK, GStreamer, yt-dlp, and the declared Arch Python
dependencies. `rustypipe-botguard` is bundled for the optional PO-Token
workflow.

The package version follows the exact Git tag when one exists. A local build
from an untagged commit uses the short commit hash as its version suffix.
