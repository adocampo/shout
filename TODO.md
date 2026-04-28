# TODO — features deferred from v1

## Core

- [ ] **Auto-detección de idioma** como modo principal de operación (hoy es solo
      una opción en `models.auto_detect_language` que pasa `language="auto"` al modelo).
- [ ] **Diccionario personalizado / palabras clave** (sustituciones, acrónimos
      preservados, prompt de inicio de Whisper).
- [ ] **Historial de los últimos N dictados** con re-inyección rápida.
- [ ] **Aceleración GPU** detectada en runtime (Vulkan / CUDA en pywhispercpp).
- [ ] Soporte para **silero-vad** además de webrtcvad (mejor calidad).

## Plataforma

- [ ] **Push-to-talk en Wayland** mediante atajo manual del compositor que
      invoque `shout --start` / `shout --stop` vía D-Bus IPC (los flags de CLI
      ya existen, falta el servicio D-Bus).
- [ ] BindShortcuts completo del portal con label localizable y `preferred_trigger`.
- [ ] Backend `ydotool` para Wayland cuando `wtype` no esté disponible.

## UI

- [ ] **Animación** del icono de micrófono mientras graba (latido).
- [ ] Drag & drop para reordenar idiomas en la pestaña Idiomas.
- [ ] Capturador de hotkey con preview de conflictos.
- [ ] Notificación al usuario tras la primera ejecución explicando el portal.

## Empaquetado

- [ ] Manifest **Flatpak** (org.shout.Shout) con permisos mínimos.
- [ ] **AppImage** generado por CI.
- [ ] Paquetes **.deb** y **.rpm** nativos.
- [ ] Tests automatizados con WAVs de muestra (es / en).

## Migración

- [ ] Reescribir `shout/core/{audio,transcriber,vad}.py` en C++ con bindings
      pybind11 una vez la app esté estable y haya datos reales de cuellos de botella.
