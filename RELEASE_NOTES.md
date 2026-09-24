# VenTapes v0.0.0-pre1

> **Private learning prerelease.** This is not a supported public release and
> is not intended for public use, distribution, or dependable everyday
> operation.

This prerelease packages the current VenTapes fork for Arch-based Linux
systems. It includes the VenTapes rebrand, separate application/data paths,
explicit upstream GPL and third-party notices, safer authentication handling,
and a local Arch `PKGBUILD`.

## Build

```bash
cd packaging/arch
makepkg -sr
sudo pacman -U ./ventapes-*.pkg.tar.zst
```

The package is source-based and uses the system Python/GTK stack. It bundles
the small Python integrations that are not available as stable Arch packages
and the optional `rustypipe-botguard` helper.

## Known limitations

- This is a learning experiment, not a polished release.
- The public repository's older git history still contains the original
  upstream snapshot; this prerelease does not make that history private.
- Last.fm requires the user's own API credentials.
- Discord Rich Presence is disabled unless a separate VenTapes application ID
  is supplied.
- Full Flatpak, Nix, Windows, and GUI validation were not part of this Arch
  package build.

## Upstream

VenTapes is based on [Mixtapes](https://github.com/m-obeid/Mixtapes) by
Mohamad Obeid and the Mixtapes contributors. See `CREDITS.md`,
`THIRD_PARTY_NOTICES.md`, and `NOTICE.md` for provenance and licensing.
