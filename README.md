# Shout — Linux voice dictation

A SuperWhisper-style voice dictation engine for Linux desktops. Press a global
hotkey, speak in any of your configured languages, and the transcribed text is
typed into whatever field has focus. Works on **X11 and Wayland**, with KDE
Plasma, GNOME and any other desktop / WM. Transcription is local using
[whisper.cpp](https://github.com/ggerganov/whisper.cpp) — no network required.

> Status: **alpha (v0.1)**. See [`plan.md`](plan.md) and [`TODO.md`](TODO.md).

## Features (v1)

- Global hotkey (toggle or push-to-talk) to start/stop dictation.
- Local transcription via whisper.cpp (`pywhispercpp`), models downloadable from the UI.
- Voice Activity Detection auto-stops on silence.
- Text injection on Wayland (`wtype`) and X11 (`xdotool`), with clipboard+paste fallback.
- Multilingual: configure several languages, switch on the fly from the badge or tray.
- System tray icon with state (idle / recording / transcribing) and language switcher.
- Floating microphone badge with a hover language selector. Tries to position itself
  next to the focused text field via AT-SPI; falls back to mouse position (X11) or
  to a configurable screen corner.
- Optional post-processing through a local OpenAI-compatible LLM endpoint
  (Ollama / `llama-server`) to clean up filler words and add punctuation.
- Audio feedback on start/stop (configurable).
- Autostart at session login.

## System dependencies

You need a few command-line helpers for the platform layer to work.

### Arch / Manjaro

```bash
sudo pacman -S \
    wtype wl-clipboard \
    xdotool xclip \
    xdg-desktop-portal xdg-desktop-portal-kde \
    portaudio pipewire-pulse
# Optional: AT-SPI for caret-aware badge positioning
sudo pacman -S python-gobject at-spi2-core
```

On GNOME use `xdg-desktop-portal-gnome` instead of `-kde`.

### Debian / Ubuntu

```bash
sudo apt install wtype wl-clipboard xdotool xclip \
    xdg-desktop-portal xdg-desktop-portal-kde \
    libportaudio2 pipewire-pulse
# Optional
sudo apt install python3-gi gir1.2-atspi-2.0
```

### Fedora

```bash
sudo dnf install wtype wl-clipboard xdotool xclip \
    xdg-desktop-portal xdg-desktop-portal-kde \
    portaudio pipewire-pulseaudio
```

## Install

```bash
pip install -e .
# or
pipx install .
```

Optional extras:

```bash
pip install -e '.[silero,atspi,dev]'
```

## Run

```bash
shout-stt          # main command (avoids name clash with libshout/Icecast)
# or, equivalently:
python -m shout
```

> The CLI binary is named `shout-stt` because Arch / Debian / Fedora ship a
> `/usr/bin/shout` from `libshout` (the Icecast streamer). The Python package
> is still called `shout`.

The first run creates `~/.config/shout/config.toml`. Open the configuration
window from the tray icon (left click) or by running `shout` again.

Download a Whisper model from the **Modelos** tab — `small-q5_1` (~190 MB) is a
good default for everyday CPU use.

## Notes & known limitations

- **Wayland direct text injection (`wtype`)**: most Wayland compositors —
  including KDE Plasma — do *not* expose the `zwp_virtual_keyboard_v1` protocol
  to regular clients (only to on-screen keyboards), so `wtype` will fail with
  *"Compositor does not support the virtual keyboard protocol"*. Shout detects
  this and falls back automatically through this cascade:

  1. **`ydotool`** — kernel-level injection via `/dev/uinput`. Works on
     **every** compositor and every app. **Recommended.** Setup:

     ```bash
     # Arch
     sudo pacman -S ydotool
     # Debian / Ubuntu / Fedora — package is also called `ydotool`

     # Make sure /dev/uinput is accessible (most distros set this up via uaccess
     # ACLs; if not, add yourself to the `input` group and re-login)
     ls -l /dev/uinput
     groups | tr ' ' '\n' | grep -E 'input'

     # Start the daemon as a user service
     systemctl --user enable --now ydotool.service
     ls -l "$XDG_RUNTIME_DIR/.ydotool_socket"   # should exist
     ```

     Once `ydotool` + the socket are present, Shout picks it up automatically.

  2. **`kdotool`** (KDE only) — uses KWin's scripting API to send Ctrl+V after
     copying to the clipboard. No daemon, no root needed. `paru -S kdotool` /
     build from <https://github.com/jinliu/kdotool>.

  3. **`wtype` direct typing** — works on Sway, Hyprland, Mir, river. Skipped
     after the first failure on KDE/GNOME.

  4. **Clipboard + synthetic Ctrl+V (`wtype -M ctrl -k v`)** — same protocol
     issue on KDE/GNOME, works on sway/hypr.

  5. **Copy-only fallback** — text is left on the clipboard and a notification
     asks you to press Ctrl+V manually.

- **Wayland & global hotkeys**: Wayland forbids apps from grabbing keys directly.
  Shout uses the `xdg-desktop-portal` GlobalShortcuts protocol; the first time
  your compositor will prompt you to confirm the binding. Push-to-talk (key
  release) is not reliably reported by the portal yet — Shout degrades to toggle
  in that case (see TODO.md).

  **Reliable fallback**: Shout exposes a D-Bus service. You can bind any
  shortcut you like in your compositor and have it call:

  - **KDE Plasma**: System Settings → Shortcuts → Add Custom Shortcut →
    "Run command" → `shout-stt --toggle`
  - **GNOME**: Settings → Keyboard → View and Customize Shortcuts →
    Custom Shortcuts → Add → command `shout-stt --toggle`

  Other useful client commands: `shout-stt --start`, `--stop`, `--cancel`,
  `--quit`, `--settings`.

- **Wayland & badge placement**: a Wayland app cannot query where another app's
  text caret is. Shout tries AT-SPI first; if that's unavailable, it places the
  badge in the screen corner you configured. On X11 it can also follow the mouse
  cursor.
- **Plasma**: tray icon uses StatusNotifier, supported natively. The portal
  GlobalShortcuts implementation in KWin requires Plasma 5.27+ / 6.x.

## License

MIT — see source headers.
