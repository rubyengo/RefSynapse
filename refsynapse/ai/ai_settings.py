"""AI backend settings for the RefSynapse assistant.

Stored in win.settings["ai"]: provider fields plus the MCP server list.
The API key is wrapped with Windows DPAPI (see secrets.py); a legacy
plaintext key is migrated to an encrypted blob on the first load.
Every MCP server asks for per-call confirmation unless its "confirm" flag
is explicitly set to false ("I trust this server").
"""

import json
import shlex
import threading

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QMessageBox, QPlainTextEdit,
                               QPushButton, QVBoxLayout)

from ..config import save_settings
from .mcp_client import split_args
from .providers import PROVIDERS, PROVIDER_LABELS, default_config, make_provider
from .secrets import SecretError, is_encrypted, protect, storage_note, unprotect

EFFORTS = ["off", "low", "medium", "high"]


def load_ai_config(win):
    cfg = default_config()
    cfg["mcp_servers"] = []
    stored = win.settings.get("ai")
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k in cfg})
    key = cfg.get("api_key", "") or ""
    if is_encrypted(key):
        try:
            cfg["api_key"] = unprotect(key)
        except SecretError as ex:
            cfg["api_key"] = ""
            cfg["_key_error"] = str(ex)
    elif key:
        # One-time migration: plaintext on disk -> DPAPI blob.
        merged = dict(stored) if isinstance(stored, dict) else {}
        merged["api_key"] = protect(key)
        win.settings["ai"] = merged
        save_settings(win.settings)
    return cfg


def save_ai_config(win, cfg):
    stored = dict(cfg)
    stored.pop("_key_error", None)
    key = stored.get("api_key", "") or ""
    stored["api_key"] = protect(key) if key else ""
    win.settings["ai"] = stored
    save_settings(win.settings)


class McpServerDialog(QDialog):
    """One MCP server: name, transport, command/args or url/headers, trust."""

    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        cfg = dict(config or {})
        self.setWindowTitle("MCP server")
        self.setMinimumWidth(600)
        form = QFormLayout()
        self.name = QLineEdit(cfg.get("name", ""))
        self.transport = QComboBox()
        self.transport.addItems(["stdio", "http"])
        self.transport.setCurrentText(cfg.get("transport", "stdio"))
        self.command = QLineEdit(cfg.get("command", ""))
        self.command.setPlaceholderText("npx / uvx / python / node …")
        self.args = QLineEdit(shlex.join(cfg.get("args") or []))
        self.args.setPlaceholderText('-y @modelcontextprotocol/server-filesystem "D:\\refs"')
        self.url = QLineEdit(cfg.get("url", ""))
        self.url.setPlaceholderText("http://127.0.0.1:8000/mcp")
        self.headers = QPlainTextEdit(json.dumps(cfg.get("headers") or {}, ensure_ascii=False))
        self.headers.setFixedHeight(60)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(bool(cfg.get("enabled", True)))
        self.confirm = QCheckBox("Ask me before every call of this server (recommended)")
        self.confirm.setChecked(bool(cfg.get("confirm", True)))
        form.addRow("Name", self.name)
        form.addRow("Transport", self.transport)
        form.addRow("Command (stdio)", self.command)
        form.addRow("Arguments (stdio)", self.args)
        form.addRow("URL (http)", self.url)
        form.addRow("Headers (http, JSON)", self.headers)
        form.addRow("", self.enabled)
        form.addRow("", self.confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(buttons)

    def _accept(self):
        name = self.name.text().strip()
        transport = self.transport.currentText()
        if not name:
            QMessageBox.warning(self, "MCP server", "Give the server a name.")
            return
        if transport == "stdio" and not self.command.text().strip():
            QMessageBox.warning(self, "MCP server", "stdio transport needs a command.")
            return
        if transport == "http" and not self.url.text().strip():
            QMessageBox.warning(self, "MCP server", "http transport needs a URL.")
            return
        try:
            headers = json.loads(self.headers.toPlainText() or "{}")
            if not isinstance(headers, dict):
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "MCP server", "Headers must be a JSON object.")
            return
        self.config = {"name": name, "transport": transport,
                       "command": self.command.text().strip(),
                       "args": split_args(self.args.text()),
                       "url": self.url.text().strip(),
                       "headers": headers,
                       "enabled": self.enabled.isChecked(),
                       "confirm": self.confirm.isChecked()}
        self.accept()


class AiSettingsDialog(QDialog):
    """Provider / endpoint / model / key / effort + MCP servers + connection test."""

    probe_done = Signal(object)

    def __init__(self, win, parent=None):
        super().__init__(parent or win)
        self.win = win
        self.cfg = load_ai_config(win)
        self.mcp = [dict(s) for s in self.cfg.get("mcp_servers", [])]
        self._probe_thread = None
        self.setWindowTitle("AI Assistant settings")
        self.setMinimumWidth(640)

        form = QFormLayout()
        self.provider = QComboBox()
        for key, label in PROVIDER_LABELS.items():
            self.provider.addItem(label, key)
        index = self.provider.findData(self.cfg.get("provider", "openai"))
        self.provider.setCurrentIndex(max(0, index))
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.base_url = QLineEdit(str(self.cfg.get("base_url", "")))
        self.model = QLineEdit(str(self.cfg.get("model", "")))
        self.api_key = QLineEdit(str(self.cfg.get("api_key", "")))
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_note = QLabel(self.cfg.get("_key_error") or storage_note())
        self.key_note.setWordWrap(True)
        self.key_note.setStyleSheet("color:#888;")
        self.effort = QComboBox()
        self.effort.addItems(EFFORTS)
        self.effort.setCurrentText(str(self.cfg.get("reasoning_effort", "low")))
        form.addRow("Provider", self.provider)
        form.addRow("Base URL", self.base_url)
        form.addRow("Model", self.model)
        form.addRow("API key", self.api_key)
        form.addRow("", self.key_note)
        form.addRow("Reasoning effort", self.effort)

        self.probe_label = QLabel("")
        self.probe_label.setWordWrap(True)
        self.probe_btn = QPushButton("Test connection")
        self.probe_btn.clicked.connect(self._probe)
        probe_row = QHBoxLayout()
        probe_row.addWidget(self.probe_btn)
        probe_row.addWidget(self.probe_label, 1)

        self.mcp_list = QListWidget()
        self.mcp_list.setMaximumHeight(110)
        self._refresh_mcp()
        add_btn = QPushButton("Add…")
        add_btn.clicked.connect(self._mcp_add)
        edit_btn = QPushButton("Edit…")
        edit_btn.clicked.connect(self._mcp_edit)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._mcp_remove)
        mcp_buttons = QHBoxLayout()
        mcp_buttons.addWidget(add_btn)
        mcp_buttons.addWidget(edit_btn)
        mcp_buttons.addWidget(remove_btn)
        mcp_buttons.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addLayout(probe_row)
        root.addWidget(QLabel("MCP servers (tools become mcp__name__tool):"))
        root.addWidget(self.mcp_list)
        root.addLayout(mcp_buttons)
        root.addWidget(buttons)
        self.probe_done.connect(self._probe_finished)

    # ---- MCP list -------------------------------------------------------
    def _refresh_mcp(self):
        self.mcp_list.clear()
        for server in self.mcp:
            where = server.get("url") if server.get("transport") == "http" else \
                " ".join([server.get("command", "")] + list(server.get("args") or []))
            marks = ""
            if not server.get("enabled", True):
                marks += "  [off]"
            if not server.get("confirm", True):
                marks += "  [no confirm]"
            self.mcp_list.addItem("%s (%s) — %s%s" % (server.get("name", "?"),
                                                      server.get("transport", "stdio"),
                                                      where, marks))

    def _mcp_add(self):
        dialog = McpServerDialog(None, self)
        if dialog.exec():
            self.mcp.append(dialog.config)
            self._refresh_mcp()

    def _mcp_edit(self):
        row = self.mcp_list.currentRow()
        if row < 0:
            return
        dialog = McpServerDialog(self.mcp[row], self)
        if dialog.exec():
            self.mcp[row] = dialog.config
            self._refresh_mcp()

    def _mcp_remove(self):
        row = self.mcp_list.currentRow()
        if row >= 0:
            del self.mcp[row]
            self._refresh_mcp()

    # ---- provider helpers ------------------------------------------------
    def _provider_changed(self, _index):
        cls = PROVIDERS.get(self.provider.currentData())
        if cls is None:
            return
        current = self.base_url.text().strip()
        known = {c.default_base_url() for c in PROVIDERS.values()}
        if not current or current in known:
            self.base_url.setText(cls.default_base_url())

    def _collect(self):
        cfg = dict(self.cfg)
        cfg.update(provider=self.provider.currentData(),
                   base_url=self.base_url.text().strip(),
                   model=self.model.text().strip(),
                   api_key=self.api_key.text(),
                   reasoning_effort=self.effort.currentText(),
                   mcp_servers=self.mcp)
        return cfg

    def _accept(self):
        save_ai_config(self.win, self._collect())
        self.accept()

    # ---- connection test (worker thread, GUI stays alive) --------------
    def _probe(self):
        self.probe_btn.setEnabled(False)
        self.probe_label.setText("Testing…")
        cfg = self._collect()

        def work():
            try:
                result = make_provider(cfg).probe()
            except Exception as ex:
                result = {"ok": False, "message": "%s: %s" % (type(ex).__name__, ex)}
            self.probe_done.emit(result)

        self._probe_thread = threading.Thread(target=work, daemon=True)
        self._probe_thread.start()

    def _probe_finished(self, result):
        self.probe_btn.setEnabled(True)
        self.probe_label.setText(result.get("message", ""))