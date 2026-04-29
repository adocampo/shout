# TODO — features deferred from v1

## Core

- [ ] **Auto-detección de idioma** como modo principal de operación (hoy es solo
      una opción en `models.auto_detect_language` que pasa `language="auto"` al modelo).
- [ ] **Comandos de voz dictados** para puntuación y formato que Whisper no
      maneja bien: "abre comillas / cierra comillas", "abre paréntesis",
      "cierra admiración", "nueva línea", "nuevo párrafo", "bloque de código",
      "bloque markdown", etc. Implementarlo como una capa de post-proceso del
      texto transcrito (regex + tabla configurable por idioma) o como una
      interfaz separada de "dictado estructurado". Considerar atajo dedicado.
- [ ] **Multiidioma mezclado en una misma frase**: cuando el idioma principal
      es ES y se mete una palabra/frase en EN (o viceversa), Whisper la traduce
      o destroza. Opciones a explorar: (a) detectar code-switching por
      segmentos y re-transcribir el segmento con el otro idioma, (b) prompt
      multilingüe a Whisper con `--prompt`, (c) post-proceso LLM que corrija
      los términos en idioma secundario, (d) configurar lista de "idiomas
      secundarios permitidos" además del principal.
- [ ] **Diccionario personalizado / palabras clave** (sustituciones, acrónimos
      preservados, prompt de inicio de Whisper).
- [ ] **Historial de los últimos N dictados** con re-inyección rápida.
- [x] **Aceleración GPU** detectada en runtime (Vulkan / CUDA en pywhispercpp).
      Hecho 2026-04-29: whisper.cpp compilado con `GGML_CUDA=ON` (sm_86,
      RTX 3070 Ti), instalado como `whisper-cli` en `/usr/local/bin/`,
      `transcriber.py` reescrito para invocarlo por subprocess.
- [ ] Soporte para **silero-vad** además de webrtcvad (mejor calidad).
- [ ] **Probar y comparar motores de transcripción en streaming** (escritura
      "en vivo" mientras hablas, sin esperar a flush por silencio):
  - [x] **faster-whisper + LocalAgreement-2** (rama `feature/streaming-faster-whisper`).
        Implementado en `shout/core/streaming.py`. Latencia objetivo
        ~0.5–1.5 s, mantiene la calidad de Whisper. Requiere
        `pip install faster-whisper`.
  - [ ] **whisper.cpp `stream` example** reusando el binario CUDA ya compilado.
        Ventaja: cero deps Python nuevas. Desventaja: experimental.
  - [ ] **NVIDIA Parakeet / Canary (NeMo)**: streaming RNN-T verdadero,
        latencia <300 ms. Multilingüe limitado vs Whisper en es-ES coloquial.
  - [ ] **Vosk**: streaming nativo por bytes, latencia <100 ms, CPU.
        Calidad muy inferior; útil como fallback ligero.
  Métricas a recolectar para cada motor: latencia P50/P95 hasta primer token
  confirmado, WER en muestras es-ES propias, uso de VRAM, estabilidad de
  commit (cuántas veces re-escribe palabras ya emitidas).

## Plataforma

- [ ] **Push-to-talk en Wayland** mediante atajo manual del compositor que
      invoque `shout --start` / `shout --stop` vía D-Bus IPC (los flags de CLI
      ya existen, falta el servicio D-Bus).
- [ ] BindShortcuts completo del portal con label localizable y `preferred_trigger`.
- [x] Backend `ydotool` para Wayland cuando `wtype` no esté disponible.
      Hecho: `YdotoolInjector` en `shout/platform/injector.py` con detección
      de socket, liberación de modificadores y modo copy+paste por scancodes.

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
