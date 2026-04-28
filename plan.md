# Plan: Shout — Dictado por voz para Linux (estilo SuperWhisper)

App de escritorio Linux que, al pulsar un atajo global, graba audio del micrófono, lo transcribe localmente con whisper.cpp en el idioma elegido, e inyecta el texto en el campo de texto activo. Funciona en X11 y Wayland, en cualquier escritorio/WM. Stack: **Python 3.11+ + PySide6 (Qt6) + pywhispercpp + sounddevice + xdg-desktop-portal**, distribuido vía `pip` en la v1. Diseño modular pensado para portar el núcleo a C++ más adelante.

---

## Arquitectura

Tres capas dentro de un solo binario (`shout`), separadas por módulos para facilitar la futura migración a C++:

- **Core** (sin Qt): captura de audio, VAD, motor STT, post-procesado LLM, gestión de modelos, configuración. Abstracciones (`AudioCapture`, `Transcriber`, `TextInjector`, `HotkeyProvider`) con implementaciones intercambiables.
- **Platform** (sin Qt): inyección de texto, hotkey global, AT-SPI, detección de sesión X11/Wayland. Cada backend tras una interfaz común.
- **UI** (Qt6 / PySide6): tray, ventana de configuración, badge flotante. Comunica con Core vía señales Qt.

---

## Fases

### Fase 1 — Esqueleto y configuración

1. Estructura del paquete Python: `shout/{core,platform,ui,resources}`, `pyproject.toml`, entrypoint `shout = shout.app:main`.
2. Configuración: dataclasses serializadas a TOML en `~/.config/shout/config.toml` (idiomas activos, idioma por defecto, hotkey, modo push-to-talk vs toggle, autostart, tray, posición badge fallback, modelo seleccionado, opciones LLM).
3. Logging rotativo en `~/.local/state/shout/shout.log`.
4. Detección de sesión: `XDG_SESSION_TYPE` + `WAYLAND_DISPLAY` / `DISPLAY`.

### Fase 2 — Núcleo de audio y STT

5. `AudioCapture` con `sounddevice` (PipeWire/PulseAudio): 16 kHz mono PCM en buffer, hilo dedicado, control start/stop.
2. **VAD** con `silero-vad` (fallback `webrtcvad`): cierre automático tras N ms de silencio.
3. `Transcriber` envolviendo `pywhispercpp`. API `transcribe(pcm, language) -> str`. Modelo cargado en memoria y reutilizado.
4. Gestor de modelos: descarga desde Hugging Face (`ggerganov/whisper.cpp`) con progreso, almacenado en `~/.local/share/shout/models/`, verificación SHA256.

### Fase 3 — Inyección de texto y hotkey global

9. `TextInjector` con backends por sesión:
   - **Wayland**: `wtype`; fallback `wl-copy` + paste simulado.
   - **X11**: `xdotool type --clearmodifiers --delay 1`; fallback `xclip` + `xdotool key ctrl+v`.
   - Si el texto contiene caracteres problemáticos o supera N chars, usar siempre portapapeles + paste y restaurar el portapapeles previo.
2. `HotkeyProvider`:
    - **Wayland/portal**: `xdg-desktop-portal` GlobalShortcuts vía D-Bus.
    - **X11**: `python-xlib` con grab de tecla.
    - Modos **toggle** y **push-to-talk** (en Wayland degradación documentada).

### Fase 4 — UI Qt6

11. **System tray** con `QSystemTrayIcon`: icono dinámico (idle/recording/transcribing), menú con idioma activo, abrir configuración, pausa, salir.
2. **Ventana de configuración** (`QMainWindow` con pestañas):
    - General: autoarranque, mostrar tray, modo de activación, hotkey.
    - Idiomas: lista activa con orden, idioma por defecto.
    - Modelos: descargar/eliminar/seleccionar.
    - Audio: dispositivo, sensibilidad VAD, sonidos.
    - Avanzado: backend de inyección override, post-procesado LLM.
3. **Badge flotante** frameless, `Qt.WindowStaysOnTopHint | Qt.Tool | Qt.FramelessWindowHint`, translúcido:
    - Icono micrófono con animación según estado.
    - Flecha desplegable que muestra los idiomas configurados al hacer hover.
    - **Posicionamiento en cascada**: AT-SPI → posición ratón (X11) → esquina configurable (fallback).

### Fase 5 — Pegamento e integración

14. Máquina de estados: `IDLE → RECORDING → TRANSCRIBING → INJECTING → IDLE`, cancelación con Esc, errores manejados.
2. Sonidos de feedback start/stop con `QSoundEffect`.
3. Post-procesado LLM opcional vía endpoint OpenAI-compatible local (Ollama / llama-server), prompt configurable.
4. Autoarranque vía `~/.config/autostart/shout.desktop`.

### Fase 6 — Empaquetado

18. `pyproject.toml` instalable con `pip install -e .` y `pipx install .`.
2. README con dependencias del sistema (`wtype`, `xdotool`, `wl-clipboard`, `xclip`, `pipewire`).
3. Smoke tests manuales documentados.

---

## Ficheros relevantes

- `pyproject.toml`
- `shout/app.py`, `shout/config.py`, `shout/logging_setup.py`
- `shout/core/{audio,vad,transcriber,models,postprocess,session}.py`
- `shout/platform/{session,injector,hotkey,atspi,autostart}.py` + backends `_wtype`, `_xdotool`, `_clipboard`, `_portal`, `_x11`
- `shout/ui/{tray,settings,badge,hotkey_capture}.py`
- `shout/resources/` — iconos SVG, sonidos WAV
- `README.md`, `TODO.md`

---

## Verificación

1. Detección de sesión correcta en X11 y Wayland.
2. Audio: grabación 2 s con RMS > umbral.
3. Transcripción: WAV de muestra ES/EN.
4. Inyección: matriz manual KWrite, Firefox, Chromium, Konsole, GNOME Text Editor.
5. Hotkey en X11 y Wayland (portal pide confirmación).
6. Badge: AT-SPI / ratón / esquina según fallback.
7. Tray funcional en Plasma.
8. End-to-end < 3 s con `small-q5_0` en CPU.
9. Cancelación con Esc limpia.
10. Persistencia de config tras reinicio.

---

## Decisiones tomadas

- **Stack**: Python + PySide6 + pywhispercpp + sounddevice. Núcleo modular para porte futuro a C++.
- **STT**: whisper.cpp vía pywhispercpp; modelos GGUF descargables desde la UI.
- **Inyección**: wtype / xdotool + fallback portapapeles.
- **Hotkey**: portal GlobalShortcuts en Wayland, Xlib en X11.
- **Audio**: PipeWire/PulseAudio vía sounddevice.
- **Distribución v1**: `pip` / `pipx`.
- **Modelos**: en `~/.local/share/shout/models/`.
- **Badge**: AT-SPI → ratón (X11) → esquina configurable.
- **Features v1**: push-to-talk + toggle, VAD, sonidos, post-proceso LLM opcional, idiomas, tray, badge, autostart.
- **Diferido a TODO.md**: historial de dictados, auto-detección de idioma como modo principal, diccionario personalizado.

---

## Consideraciones futuras

1. **Push-to-talk en Wayland**: si el portal no entrega keyup fiable, ofrecer atajo manual en KWin/Mutter que invoque `shout --start`/`--stop`.
2. **Porte del Core a C++** con pybind11 cuando haya datos reales de cuellos de botella.
3. **Aceleración GPU** (Vulkan/CUDA) detectada en runtime y activada por defecto.
