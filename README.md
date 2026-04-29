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

## GPU acceleration (NVIDIA / CUDA)

Shout's transcriber calls the `whisper-cli` binary from
[whisper.cpp](https://github.com/ggerganov/whisper.cpp) via subprocess. If that
binary is built with CUDA support, transcription runs on the GPU and is
roughly **10× faster** than the default CPU build (≈0.4 s vs ≈5 s for a
3-second chunk on an RTX 3070 Ti with `small-q5_1`).

> Tested with: Arch Linux, NVIDIA driver 565+, CUDA Toolkit 13.x, GCC 15,
> RTX 3070 Ti (compute capability 8.6). Adapt the architecture flag for your
> card (Pascal `61`, Turing `75`, Ampere `86`, Ada `89`, Hopper `90`).

### 1. Install the CUDA Toolkit and a compatible compiler

```bash
# Arch / Manjaro
sudo pacman -S cuda cudnn cmake gcc git
```

```bash
# Debian / Ubuntu — see https://developer.nvidia.com/cuda-downloads
sudo apt install nvidia-cuda-toolkit cmake build-essential git
```

```bash
# Fedora
sudo dnf install cuda-toolkit cmake gcc-c++ git
```

Verify the toolchain:

```bash
nvcc --version       # should print a CUDA release
nvidia-smi           # should list your GPU and driver
```

If `nvcc` is not on `PATH`, add it (Arch installs it under `/opt/cuda`):

```bash
echo 'export PATH=/opt/cuda/bin:$PATH' >> ~/.zshrc
echo 'export LD_LIBRARY_PATH=/opt/cuda/lib64:$LD_LIBRARY_PATH' >> ~/.zshrc
exec $SHELL
```

### 2. Build whisper.cpp with CUDA

Pick the right compute capability for your card and replace `86` below:

| GPU family            | `CMAKE_CUDA_ARCHITECTURES` |
|-----------------------|----------------------------|
| GTX 10xx (Pascal)     | `61`                       |
| RTX 20xx (Turing)     | `75`                       |
| RTX 30xx (Ampere)     | `86`                       |
| RTX 40xx (Ada)        | `89`                       |
| H100 (Hopper)         | `90`                       |

```bash
git clone --depth 1 https://github.com/ggerganov/whisper.cpp.git /tmp/whisper.cpp
cd /tmp/whisper.cpp
cmake -B build \
    -DGGML_CUDA=ON \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CUDA_ARCHITECTURES=86
cmake --build build -j"$(nproc)" --target whisper-cli
```

Compilation of the CUDA template instances takes 5–15 min depending on the
machine. When it finishes you'll have `build/bin/whisper-cli` (~1 MB) plus
`build/src/libwhisper.so` and `build/ggml/src/libggml*.so`.

### 3. Install the binary system-wide

```bash
sudo cp build/bin/whisper-cli       /usr/local/bin/
sudo cp build/src/libwhisper.so     /usr/local/lib/
sudo cp build/ggml/src/libggml*.so  /usr/local/lib/
sudo ldconfig
```

### 4. Verify GPU is detected

```bash
whisper-cli --help | head -2
# usage: whisper-cli [options] file0 file1 ...
# supported audio formats: flac, mp3, ogg, wav

# Smoke-test against a model. The boot output should mention CUDA0:
whisper-cli -m ~/.local/share/shout/models/ggml-small-q5_1.bin -f /dev/null 2>&1 \
    | grep -E 'cuda|CUDA|device'
# ggml_cuda_init: found 1 CUDA devices (Total VRAM: 7833 MiB):
#   Device 0: NVIDIA GeForce RTX 3070 Ti, compute capability 8.6, ...
# whisper_backend_init_gpu: using CUDA0 backend
```

That's it — the next time you launch Shout the transcriber will pick up
`whisper-cli` from `PATH` automatically. To disable GPU temporarily, pass
`-ng` in the command list (or rebuild without `GGML_CUDA`).

### Troubleshooting

- **`whisper-cli: error while loading shared libraries: libwhisper.so`** —
  you forgot `sudo ldconfig`, or `/usr/local/lib` is not in your loader path.
  Fix: `echo /usr/local/lib | sudo tee /etc/ld.so.conf.d/local.conf && sudo ldconfig`.
- **`nvcc fatal: Unsupported gpu architecture 'compute_XX'`** — your
  CMake architecture value doesn't match the toolkit. Look up the
  capability for your card and adjust `-DCMAKE_CUDA_ARCHITECTURES`.
- **CUDA Toolkit ↔ GCC version mismatch** (e.g. CUDA 12 + GCC 14): install
  an older GCC and pass `-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/gcc-13` to
  `cmake`.
- **Driver too old** (`CUDA driver version is insufficient for CUDA runtime
  version`): update your NVIDIA driver to a version ≥ the toolkit's minimum
  (CUDA 13 needs driver 560+, CUDA 12 needs 525+).
- **`out of memory`**: the model needs ~200 MB VRAM for `small`, ~600 MB for
  `medium`, ~3 GB for `large`. Free VRAM with `nvidia-smi` and close other
  GPU apps.

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

## Real-time streaming backend (experimental)

The default transcription backend (`whisper_cpp`) waits for a silence pause
(or a max-chunk timeout) before producing text. Shout also ships with an
**experimental streaming backend** that decodes audio continuously and types
words as they are confirmed, with ~0.5–1.5 s end-to-end latency on a modern
NVIDIA GPU.

It uses [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper)
(CTranslate2 backend) plus a **LocalAgreement-2** committer: every
`streaming_step_ms` the rolling audio window is re-decoded, and a token is
emitted only when two consecutive hypotheses agree on it. This produces a
"writes as you speak" experience while keeping Whisper's accuracy.

Enable it:

```bash
.venv/bin/pip install faster-whisper
# Optionally, for CUDA: pip install ctranslate2 nvidia-cublas-cu12 nvidia-cudnn-cu12
```

Then in **Ajustes → Avanzado**:

- **Backend de transcripción**: *Streaming en tiempo real (faster-whisper)*
- **Modelo streaming**: `small`, `medium`, `large-v3` (downloaded on demand
  to `~/.cache/huggingface/`) or a path to a local CT2 directory.

Tunables in `~/.config/shout/config.toml` under `[models]`:

```toml
backend = "streaming"
streaming_model = "small"
streaming_device = "auto"            # "cuda" | "cpu" | "auto"
streaming_compute_type = "int8_float16"
streaming_step_ms = 500              # how often to re-decode
streaming_min_chunk_ms = 1000        # min audio before first decode
```

Trade-offs vs the default `whisper_cpp` backend:

- **Pros**: text appears while you speak, no chunk boundary issues, no wasted
  re-transcriptions on silence.
- **Cons**: requires Python wheels for `faster-whisper` + `ctranslate2`
  (~600 MB with CUDA libs); committed text cannot be revised, so a
  late-arriving word may slightly change punctuation choices that came before
  it.

This backend lives on the `feature/streaming-faster-whisper` branch until
benchmarks show a clear win across our es-ES corpus. See
[TODO.md](TODO.md) for the comparison plan.

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
