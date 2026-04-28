"""Settings window — tabbed configuration UI."""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import config as cfg_mod
from ..config import Config
from ..core import audio as audio_mod
from ..core import models as models_mod
from ..platform import autostart as autostart_mod
from .hotkey_capture import HotkeyCaptureButton
from .tray import LANG_LABELS, lang_label

log = logging.getLogger(__name__)

# Languages we offer in the picker. Whisper supports many more; this is a curated default.
_OFFERED_LANGUAGES = [
    "en", "es", "fr", "de", "it", "pt", "ca", "gl", "eu",
    "nl", "ru", "pl", "tr", "ar", "ja", "zh", "ko",
]


class _ModelDownloadSignals(QObject):
    progress = Signal(str, int, int)   # key, downloaded, total
    finished = Signal(str, bool, str)  # key, success, error_message


class SettingsWindow(QMainWindow):
    """Edits a `Config` in place; emits `applied` when the user clicks Apply/OK."""

    applied = Signal()

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("Shout — Configuración")
        self.resize(720, 540)

        self._tabs = QTabWidget()
        self.setCentralWidget(self._build_root())
        self._reload_from_cfg()

        self._dl_signals = _ModelDownloadSignals()
        self._dl_signals.progress.connect(self._on_dl_progress)
        self._dl_signals.finished.connect(self._on_dl_finished)

    # --- Layout --------------------------------------------------------------

    def _build_root(self) -> QWidget:
        root = QWidget()
        v = QVBoxLayout(root)
        v.addWidget(self._tabs)

        self._tabs.addTab(self._build_general_tab(), "General")
        self._tabs.addTab(self._build_languages_tab(), "Idiomas")
        self._tabs.addTab(self._build_models_tab(), "Modelos")
        self._tabs.addTab(self._build_audio_tab(), "Audio")
        self._tabs.addTab(self._build_advanced_tab(), "Avanzado")

        # Buttons row
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.close)
        ok = QPushButton("Guardar")
        ok.setDefault(True)
        ok.clicked.connect(self._on_save)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        v.addLayout(btns)
        return root

    def _build_general_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)

        self.cb_autostart = QCheckBox("Arrancar al iniciar sesión")
        self.cb_tray = QCheckBox("Mostrar icono en la bandeja del sistema")

        self.cb_mode = QComboBox()
        self.cb_mode.addItem("Toggle (pulsar para iniciar/parar)", "toggle")
        self.cb_mode.addItem("Push-to-talk (mantener pulsado)", "push_to_talk")

        self.btn_hotkey = HotkeyCaptureButton()

        f.addRow(self.cb_autostart)
        f.addRow(self.cb_tray)
        f.addRow("Modo de activación:", self.cb_mode)
        f.addRow("Atajo global:", self.btn_hotkey)

        note = QLabel(
            "En Wayland, el atajo se registra a través del portal del escritorio. "
            "Es posible que tu compositor pida confirmación la primera vez."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888;")
        f.addRow(note)
        return w

    def _build_languages_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(
            "Marca los idiomas que quieres tener disponibles. El idioma activo "
            "se usa por defecto al dictar."
        ))

        self.lst_languages = QListWidget()
        for code in _OFFERED_LANGUAGES:
            item = QListWidgetItem(f"{lang_label(code)}  ({code})")
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.lst_languages.addItem(item)
        v.addWidget(self.lst_languages, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("Idioma activo:"))
        self.cb_active_lang = QComboBox()
        row.addWidget(self.cb_active_lang, 1)
        v.addLayout(row)

        self.lst_languages.itemChanged.connect(self._refresh_active_lang_combo)
        return w

    def _build_models_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("Modelos Whisper disponibles. Descarga los que necesites."))

        self.lst_models = QListWidget()
        v.addWidget(self.lst_models, 1)

        self.lbl_dl = QLabel("")
        self.bar_dl = QProgressBar()
        self.bar_dl.setRange(0, 100)
        self.bar_dl.setValue(0)
        self.bar_dl.setVisible(False)
        v.addWidget(self.lbl_dl)
        v.addWidget(self.bar_dl)

        row = QHBoxLayout()
        self.btn_dl = QPushButton("Descargar")
        self.btn_rm = QPushButton("Eliminar")
        self.btn_use = QPushButton("Usar este")
        row.addWidget(self.btn_dl)
        row.addWidget(self.btn_rm)
        row.addWidget(self.btn_use)
        row.addStretch(1)
        v.addLayout(row)

        self.btn_dl.clicked.connect(self._on_download_model)
        self.btn_rm.clicked.connect(self._on_remove_model)
        self.btn_use.clicked.connect(self._on_use_model)
        return w

    def _build_audio_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)

        self.cb_device = QComboBox()
        self.cb_device.addItem("Sistema (predeterminado)", None)
        try:
            for dev in audio_mod.list_input_devices():
                self.cb_device.addItem(dev["name"], dev["name"])
        except Exception as exc:  # noqa: BLE001
            log.warning("Cannot list audio devices: %s", exc)

        self.cb_vad = QCheckBox("Detección automática de fin de habla (VAD)")
        self.sp_vad_aggr = QSpinBox()
        self.sp_vad_aggr.setRange(0, 3)

        self.sp_silence = QSpinBox()
        self.sp_silence.setRange(200, 5000)
        self.sp_silence.setSuffix(" ms")
        self.sp_silence.setSingleStep(100)

        self.sp_max = QSpinBox()
        self.sp_max.setRange(5, 600)
        self.sp_max.setSuffix(" s")

        self.cb_sounds = QCheckBox("Reproducir sonido de inicio/fin")

        f.addRow("Dispositivo de entrada:", self.cb_device)
        f.addRow(self.cb_vad)
        f.addRow("Sensibilidad VAD (0–3):", self.sp_vad_aggr)
        f.addRow("Silencio para autocerrar:", self.sp_silence)
        f.addRow("Duración máxima:", self.sp_max)
        f.addRow(self.cb_sounds)
        return w

    def _build_advanced_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)

        self.cb_inj = QComboBox()
        for label, key in (
            ("Auto", "auto"),
            ("wtype (Wayland)", "wtype"),
            ("xdotool (X11)", "xdotool"),
            ("Portapapeles + pegar", "clipboard"),
        ):
            self.cb_inj.addItem(label, key)

        self.sp_clip_thr = QSpinBox()
        self.sp_clip_thr.setRange(0, 100000)

        self.cb_llm = QCheckBox("Post-procesar con LLM local (OpenAI-compatible)")
        self.le_llm_url = QLineEdit()
        self.le_llm_model = QLineEdit()
        self.le_llm_key = QLineEdit()
        self.le_llm_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.te_llm_prompt = QTextEdit()
        self.te_llm_prompt.setAcceptRichText(False)
        self.te_llm_prompt.setFixedHeight(90)
        self.sp_llm_timeout = QDoubleSpinBox()
        self.sp_llm_timeout.setRange(1, 300)
        self.sp_llm_timeout.setDecimals(0)
        self.sp_llm_timeout.setSuffix(" s")

        f.addRow("Backend de inyección:", self.cb_inj)
        f.addRow("Umbral para portapapeles:", self.sp_clip_thr)
        f.addRow(self.cb_llm)
        f.addRow("URL base:", self.le_llm_url)
        f.addRow("Modelo:", self.le_llm_model)
        f.addRow("API key:", self.le_llm_key)
        f.addRow("Prompt:", self.te_llm_prompt)
        f.addRow("Timeout:", self.sp_llm_timeout)
        return w

    # --- Bind <-> cfg --------------------------------------------------------

    def _reload_from_cfg(self) -> None:
        c = self.cfg

        self.cb_autostart.setChecked(c.general.autostart)
        self.cb_tray.setChecked(c.tray.enabled)
        self.cb_mode.setCurrentIndex(0 if c.hotkey.mode == "toggle" else 1)
        self.btn_hotkey.set_accelerator(c.hotkey.accelerator)

        for i in range(self.lst_languages.count()):
            item = self.lst_languages.item(i)
            code = item.data(Qt.ItemDataRole.UserRole)
            item.setCheckState(
                Qt.CheckState.Checked if code in c.languages.enabled else Qt.CheckState.Unchecked
            )
        self._refresh_active_lang_combo()
        idx = self.cb_active_lang.findData(c.languages.active)
        if idx >= 0:
            self.cb_active_lang.setCurrentIndex(idx)

        self._refresh_models_list()

        # Audio
        idx = self.cb_device.findData(c.audio.input_device)
        if idx >= 0:
            self.cb_device.setCurrentIndex(idx)
        self.cb_vad.setChecked(c.audio.vad_enabled)
        self.sp_vad_aggr.setValue(c.audio.vad_aggressiveness)
        self.sp_silence.setValue(c.audio.silence_timeout_ms)
        self.sp_max.setValue(c.audio.max_recording_seconds)
        self.cb_sounds.setChecked(c.audio.play_feedback_sounds)

        # Advanced
        idx = self.cb_inj.findData(c.injection.backend)
        if idx >= 0:
            self.cb_inj.setCurrentIndex(idx)
        self.sp_clip_thr.setValue(c.injection.clipboard_threshold)
        self.cb_llm.setChecked(c.llm.enabled)
        self.le_llm_url.setText(c.llm.base_url)
        self.le_llm_model.setText(c.llm.model)
        self.le_llm_key.setText(c.llm.api_key)
        self.te_llm_prompt.setPlainText(c.llm.prompt)
        self.sp_llm_timeout.setValue(c.llm.timeout_seconds)

    def _commit_to_cfg(self) -> None:
        c = self.cfg

        c.general.autostart = self.cb_autostart.isChecked()
        c.tray.enabled = self.cb_tray.isChecked()
        c.hotkey.mode = self.cb_mode.currentData()
        c.hotkey.accelerator = self.btn_hotkey.accelerator() or c.hotkey.accelerator

        enabled: list[str] = []
        for i in range(self.lst_languages.count()):
            item = self.lst_languages.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                enabled.append(item.data(Qt.ItemDataRole.UserRole))
        if not enabled:
            enabled = ["en"]
        c.languages.enabled = enabled
        active = self.cb_active_lang.currentData()
        c.languages.active = active if active in enabled else enabled[0]

        c.audio.input_device = self.cb_device.currentData()
        c.audio.vad_enabled = self.cb_vad.isChecked()
        c.audio.vad_aggressiveness = self.sp_vad_aggr.value()
        c.audio.silence_timeout_ms = self.sp_silence.value()
        c.audio.max_recording_seconds = self.sp_max.value()
        c.audio.play_feedback_sounds = self.cb_sounds.isChecked()

        c.injection.backend = self.cb_inj.currentData()
        c.injection.clipboard_threshold = self.sp_clip_thr.value()
        c.llm.enabled = self.cb_llm.isChecked()
        c.llm.base_url = self.le_llm_url.text().strip() or c.llm.base_url
        c.llm.model = self.le_llm_model.text().strip() or c.llm.model
        c.llm.api_key = self.le_llm_key.text()
        c.llm.prompt = self.te_llm_prompt.toPlainText()
        c.llm.timeout_seconds = int(self.sp_llm_timeout.value())

    # --- Actions -------------------------------------------------------------

    def _on_save(self) -> None:
        self._commit_to_cfg()
        try:
            cfg_mod.save(self.cfg)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"No se pudo guardar la configuración:\n{exc}")
            return
        # Apply autostart side-effect
        try:
            autostart_mod.set_enabled(self.cfg.general.autostart)
        except Exception as exc:  # noqa: BLE001
            log.warning("autostart toggle failed: %s", exc)
        log.info("Settings saved: %s", asdict(self.cfg))
        self.applied.emit()
        self.close()

    def _refresh_active_lang_combo(self) -> None:
        prev = self.cb_active_lang.currentData()
        self.cb_active_lang.clear()
        for i in range(self.lst_languages.count()):
            item = self.lst_languages.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                code = item.data(Qt.ItemDataRole.UserRole)
                self.cb_active_lang.addItem(lang_label(code), code)
        idx = self.cb_active_lang.findData(prev) if prev else -1
        if idx >= 0:
            self.cb_active_lang.setCurrentIndex(idx)

    # --- Models --------------------------------------------------------------

    def _refresh_models_list(self) -> None:
        self.lst_models.clear()
        for m in models_mod.AVAILABLE:
            installed = models_mod.is_downloaded(m.key)
            star = "★" if m.key == self.cfg.models.selected else " "
            mark = "✓" if installed else " "
            text = f"{star} [{mark}] {m.key}  —  {m.size_mb} MB  —  {m.description}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, m.key)
            self.lst_models.addItem(item)

    def _selected_model_key(self) -> str | None:
        item = self.lst_models.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_download_model(self) -> None:
        key = self._selected_model_key()
        if key is None:
            return
        if models_mod.is_downloaded(key):
            QMessageBox.information(self, "Modelos", "Este modelo ya está descargado.")
            return
        self.bar_dl.setVisible(True)
        self.bar_dl.setValue(0)
        self.lbl_dl.setText(f"Descargando {key}…")
        self.btn_dl.setEnabled(False)

        sigs = self._dl_signals

        def _worker():
            def progress(d, t):
                sigs.progress.emit(key, d, t)
            try:
                models_mod.download(key, on_progress=progress)
                sigs.finished.emit(key, True, "")
            except Exception as exc:  # noqa: BLE001
                sigs.finished.emit(key, False, str(exc))

        threading.Thread(target=_worker, name="shout-model-dl", daemon=True).start()

    def _on_remove_model(self) -> None:
        key = self._selected_model_key()
        if key is None or not models_mod.is_downloaded(key):
            return
        if QMessageBox.question(self, "Eliminar modelo", f"¿Eliminar {key}?") != QMessageBox.StandardButton.Yes:
            return
        models_mod.remove(key)
        self._refresh_models_list()

    def _on_use_model(self) -> None:
        key = self._selected_model_key()
        if key is None:
            return
        if not models_mod.is_downloaded(key):
            QMessageBox.warning(self, "Modelos", "Descarga este modelo antes de usarlo.")
            return
        self.cfg.models.selected = key
        self._refresh_models_list()

    def _on_dl_progress(self, key: str, downloaded: int, total: int) -> None:
        if total > 0:
            self.bar_dl.setValue(int(downloaded * 100 / total))
        self.lbl_dl.setText(f"Descargando {key}: {downloaded // 1024} / {total // 1024 if total else '?'} KB")

    def _on_dl_finished(self, key: str, ok: bool, err: str) -> None:
        self.btn_dl.setEnabled(True)
        self.bar_dl.setVisible(False)
        if ok:
            self.lbl_dl.setText(f"Descargado: {key}")
            self._refresh_models_list()
        else:
            self.lbl_dl.setText(f"Error: {err}")
            QMessageBox.critical(self, "Descarga", f"No se pudo descargar {key}:\n{err}")
