<div align="center">
  <h1>VenTapes</h1>
  <p><strong>A private learning experiment, not a product.</strong></p>
  <img height="150" src="assets/icons/hicolor/scalable/apps/io.github.realvenerable.VenTapes.svg" alt="VenTapes icon" />
</div>

> [!IMPORTANT]
> **VenTapes is me making dumb little changes to an interesting codebase, breaking things, fixing things, and learning how a GTK music player works.** It is not an official release, it is not supported, and it is not intended for public use, distribution, or everyday reliance. Please use the upstream project if you need a dependable application.

VenTapes is an unofficial, modified fork of [Mixtapes](https://github.com/m-obeid/Mixtapes) by [Mohamad Obeid (`m-obeid`)](https://github.com/m-obeid) and the Mixtapes contributors. Its name, application identity, data paths, icon, and presentation are changed to make this a clearly separate learning playground. The underlying work remains the work of its original authors and contributors.

## Why this fork exists

This checkout is here so I can learn by doing:

- reading unfamiliar Python, GTK4, Libadwaita, GStreamer, and packaging code;
- tracing a bug through the whole application instead of only changing its symptom;
- trying branding, desktop metadata, persistence paths, and build plumbing;
- breaking the project on purpose and learning how to put it back together.

There is no roadmap, release schedule, support promise, or public contribution program here. Features may disappear without notice. If you are here to use a YouTube Music player, use [Mixtapes](https://github.com/m-obeid/Mixtapes), not this fork.

## What is different

- The visible project name is **VenTapes**.
- The application ID and local data directories are separate from Mixtapes; existing Mixtapes/Muse data is not imported automatically.
- Upstream badges, screenshots, funding links, release notes, issue calls, and contributor promotion were removed.
- The in-app About dialog identifies this as an unofficial learning fork and credits the original project.
- The checked-in cassette icon is specific to this fork; Mixtapes' original icon creators remain credited.

Most of the inherited source code is still upstream Mixtapes code. Renaming a fork does not turn inherited code or ideas into a new codebase.

## Credits and provenance

- **Original project:** [Mixtapes](https://github.com/m-obeid/Mixtapes) (formerly Muse)
- **Original author:** [Mohamad Obeid / POCOGuy](https://github.com/m-obeid)
- **Upstream contributors:** [m-obeid/Mixtapes contributors](https://github.com/m-obeid/Mixtapes/graphs/contributors)
- **Original icon concept:** sketched by [Jakub Steiner](https://gitlab.gnome.org/jimmac) and rendered by [gnoman](https://gitlab.gnome.org/gnoman)
- **Bundled fonts:** Adwaita Sans and Adwaita Mono; see [`fonts/README.md`](fonts/README.md) and [`fonts/LICENSE.adwaita-fonts`](fonts/LICENSE.adwaita-fonts)
- **Optional PO-Token helper:** [`ThetaDev/rustypipe-botguard`](https://codeberg.org/ThetaDev/rustypipe-botguard), MIT licensed; see [`vendor/rustypipe-botguard/LICENSE`](vendor/rustypipe-botguard/LICENSE)

Full provenance is recorded in [`CREDITS.md`](CREDITS.md), third-party material in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md), and the GPL modification notice in [`NOTICE.md`](NOTICE.md).

VenTapes is not affiliated with, endorsed by, or authorized by the Mixtapes maintainers. It is also not affiliated with YouTube or Google.

## Running it locally

This is intentionally source-first. There are no supported VenTapes packages, published installers, downloads, or release builds here.

### Linux

1. Install the GTK4, Libadwaita, WebKitGTK 6, GStreamer, Python, and Node.js packages for your distribution.
2. Create a virtual environment:

   ```bash
   python3 -m venv .venv --system-site-packages
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

3. Start the app:

   ```bash
   ./start.sh
   ```

You can also invoke it directly:

```bash
glib-compile-resources --sourcedir=. \
  src/ventapes.gresource.xml \
  --target=src/ventapes.gresource
PYTHONPATH=src python3 src/main.py
```

Some playback formats need a separate `rustypipe-botguard` binary. Without it, ordinary playback can still work, but PO-Token-gated formats may be unavailable.

Last.fm scrobbling needs your own registered API key and secret; set `VENTAPES_LASTFM_API_KEY` and `VENTAPES_LASTFM_API_SECRET` at runtime. This fork does not reuse Mixtapes' credentials. Discord Rich Presence is disabled unless you set a VenTapes-specific `VENTAPES_DISCORD_APP_ID`.

### Nix development shell

```bash
nix develop
./start.sh
```

The inherited Flatpak and Windows build recipes remain for study, but they are not VenTapes release infrastructure and may not have been validated after this rebrand.

## Legal and safety notes

- VenTapes remains licensed under the [GNU General Public License v3.0 or later](LICENSE).
- This repository is publicly visible only because the code is a fork; the disclaimers do **not** revoke the GPL rights of anyone who receives it. The GPL still permits use, copying, modification, and redistribution under its terms.
- The existing public fork history can still contain the upstream snapshot even after this working tree is changed. If that history must not be public, use a new private repository rather than relying on wording in this file.
- There is no warranty. Do not put important credentials or irreplaceable data at risk while experimenting.
- YouTube authentication stores sensitive cookies locally. The app creates its auth file with owner-only permissions on POSIX and requires an explicit file selection for imports. Use a separate account or an isolated test environment if that matters to you.
- VenTapes is not a YouTube or Google client and does not imply any endorsement.

## License

This modified work is distributed under the [GNU General Public License v3.0 or later](LICENSE). Mixtapes was created by Mohamad Obeid and its contributors; see the upstream project for the authoritative project history and contributor list.
