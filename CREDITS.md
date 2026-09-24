# Credits and provenance

## Upstream project

VenTapes is a modified fork of **Mixtapes**:

- Project: <https://github.com/m-obeid/Mixtapes>
- Original author and maintainer: [Mohamad Obeid (`m-obeid`)](https://github.com/m-obeid)
- Contributors: [m-obeid/Mixtapes contributors](https://github.com/m-obeid/Mixtapes/graphs/contributors)
- Forked snapshot used as the starting point: upstream commit `00f47077627ba11b34f7cd62eed1c90f519467c1` (2026-09-12)
- License: [GNU GPL v3.0 or later](LICENSE)

Mixtapes was formerly known as Muse. The original application ID and many internal paths still reflect that older name in the upstream history. This fork uses a separate application ID and data directory, but that does not erase the origin of the code.

All original copyright and authorship remain with the relevant Mixtapes authors and contributors. This fork is distributed under the same GPL terms. Please consult the upstream repository and git history for the authoritative record of contributions.

## Visual and third-party material

- The Mixtapes app icon was sketched by [Jakub Steiner](https://gitlab.gnome.org/jimmac) and rendered by [gnoman](https://gitlab.gnome.org/gnoman). The checked-in VenTapes cassette is a fork-specific mark and does not imply that either creator endorses this fork.
- The bundled Adwaita Sans and Adwaita Mono fonts remain under the SIL Open Font License 1.1. See [`fonts/README.md`](fonts/README.md) and [`fonts/LICENSE.adwaita-fonts`](fonts/LICENSE.adwaita-fonts).
- The optional `rustypipe-botguard` project is copyright ThetaDev and MIT licensed. See [`vendor/rustypipe-botguard/LICENSE`](vendor/rustypipe-botguard/LICENSE).
- The theme-selector CSS is adapted from [GNOME Text Editor](https://github.com/GNOME/gnome-text-editor), which is GPL-3.0 licensed. The adaptation is noted in `src/ui/style.css`.
- The lyrics widget architecture explicitly acknowledges [Nocturne by Jeffser](https://github.com/Jeffser/Nocturne), a GPL-3.0 project. This fork credits that design influence rather than claiming it as new work.
- The action SVGs under `assets/icons/hicolor/scalable/actions/` are simple fork-specific marks created for VenTapes. The original Mixtapes icon work remains credited above.

Other dependencies retain their own licenses. Refer to their upstream projects and installed package metadata for details. A consolidated notice is in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## This fork

VenTapes is maintained as a personal learning exercise by [realvenerable](https://github.com/realvenerable). It is not an official Mixtapes release and is not affiliated with or endorsed by the upstream maintainers.
