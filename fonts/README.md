# Bundled Fonts

These files are inherited build/runtime support from Mixtapes. VenTapes does
not claim the font projects or redesigns as its own; their original licensing
and notices remain in this directory.

This directory ships **Adwaita Sans** and **Adwaita Mono** — the default
typefaces used by libadwaita / GNOME. Both are derived from upstream
projects (Inter and Iosevka respectively) and are licensed under the
**SIL Open Font License 1.1** (see `LICENSE.adwaita-fonts`).

## Why bundle them?

On Linux the user's system typically ships Adwaita Sans already (it's a
default GNOME font on most modern distros, and the Flatpak runtime
includes it). On Windows there is no such guarantee, so we bundle the
fonts directly. At first launch, `src/main.py` copies any `.ttf`/`.otf`
in this directory into `%LOCALAPPDATA%\Microsoft\Windows\Fonts` (the
per-user fonts directory, no admin rights needed). FontConfig — which
the Windows build forces on via `PANGOCAIRO_BACKEND=fc` — then picks
them up via `windows/fonts.conf`.

Without these files, FontConfig falls back to Segoe UI which renders at
slightly different metrics, making the Windows build feel ~80% the size
of the Linux build at 100% scaling.

## What's here

| File                          | Source                  | Notes                       |
| ----------------------------- | ----------------------- | --------------------------- |
| `AdwaitaSans-Regular.ttf`     | adwaita-fonts/sans      | Variable weight             |
| `AdwaitaSans-Italic.ttf`      | adwaita-fonts/sans      | Variable weight             |
| `AdwaitaMono-Regular.ttf`     | adwaita-fonts/mono      |                             |
| `AdwaitaMono-Italic.ttf`      | adwaita-fonts/mono      |                             |
| `AdwaitaMono-Bold.ttf`        | adwaita-fonts/mono      |                             |
| `AdwaitaMono-BoldItalic.ttf`  | adwaita-fonts/mono      |                             |
| `LICENSE.adwaita-fonts`       | adwaita-fonts/LICENSE   | OFL 1.1 — keep with fonts   |

## Refreshing

Re-fetch the latest release from
<https://gitlab.gnome.org/GNOME/adwaita-fonts/>:

```sh
./update-fonts.sh
```

(Script lives in this directory.)
