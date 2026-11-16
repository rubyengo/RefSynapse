"""Chat dock for the RefSynapse assistant (Ctrl+J).

Threading contract:
- Widgets live in the GUI thread only. Agent.run() runs in a worker thread;
  every agent event hops back through the queued _event signal.
- Dangerous-tool confirmation hops the other way: the worker waits on a
  threading.Event while the GUI shows a QMessageBox. Queued BoardBridge
  calls still execute inside that modal loop, so the board never deadlocks.
  The same dialog guards every MCP call unless the server is marked trusted.
- Previews arrive as data: URLs; Transcript.loadResource decodes them.
- MCP child processes are closed on settings change and on app shutdown.
"""

import base64
import html
import json
import threading

from PySide6.QtCore import Qt, QEvent, Signal
from PySide6.QtGui import QImage, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QDockWidget, QHBoxLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QTextBrowser, QVBoxLayout, QWidget)

from .agent import Agent
from .ai_settings import AiSettingsDialog, load_ai_config
from .board_tools import BoardTools
from .mcp_client import NAME_PREFIX, McpHub, ToolSet
from .providers import make_provider


class Transcript(QTextBrowser):
    """QTextBrowser that understands data: URLs for inline preview images."""

    def loadResource(self, rtype, url):
        if url.scheme() == "data":
            payload = url.toString().split(",", 1)[-1]
            try:
                image = QImage.fromData(base64.b64decode(payload))
            except Exception:
                image = QImage()
            if not image.isNull():
                return image
        return super().loadResource(rtype, url)


class ChatPanel(QWidget):
    _event = Signal(object)
    _confirm_req = Signal(object)

    def __init__(self, win, parent=None):
        super().__init__(parent or win)
        self.win = win
        self.board = BoardTools(win)
        self._hub = None
        self._last_hub_errors = {}
        self._agent = None
        self._worker = None
        self._busy = False
        self._stream_open = False
        self._event.connect(self._on_event)
        self._confirm_req.connect(self._on_confirm)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        top = QHBoxLayout()
        self.model_label = QLabel()
        self.settings_btn = QPushButton("Settings…")
        self.settings_btn.clicked.connect(self._open_settings)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._clear)
        top.addWidget(self.model_label, 1)
        top.addWidget(self.settings_btn)
        top.addWidget(self.clear_btn)
        root.addLayout(top)

        self.view = Transcript()
        self.view.setOpenLinks(False)
        self.view.setOpenExternalLinks(False)
        root.addWidget(self.view, 1)

        self.thinking = QPlainTextEdit()
        self.thinking.setReadOnly(True)
        self.thinking.setMaximumHeight(110)
        self.thinking.hide()
        root.addWidget(self.thinking)
        self.show_thinking = QCheckBox("Show thinking")
        self.show_thinking.toggled.connect(self.thinking.setVisible)
        root.addWidget(self.show_thinking)

        self.status = QLabel("Ready")
        root.addWidget(self.status)

        input_row = QHBoxLayout()
        self.input = QPlainTextEdit()
        self.input.setFixedHeight(64)
        self.input.setPlaceholderText("Ask the assistant…  Enter sends, Shift+Enter = newline")
        self.input.installEventFilter(self)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._send)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        buttons = QVBoxLayout()
        buttons.addWidget(self.send_btn)
        buttons.addWidget(self.stop_btn)
        input_row.addWidget(self.input, 1)
        input_row.addLayout(buttons)
        root.addLayout(input_row)

        self._rebuild_agent()
        self._append_html('<p style="color:#888">Assistant ready. Ctrl+J hides / shows this panel.</p>')

    # ---- lifecycle -----------------------------------------------------
    def _rebuild_agent(self):
        cfg = load_ai_config(self.win)
        if self._hub is not None:
            self._hub.close()
        self._hub = McpHub(cfg.get("mcp_servers", []))
        self._last_hub_errors = {}
        self.tools = ToolSet(self.board, self._hub)
        self.model_label.setText("%s · %s · MCP: %d" % (
            cfg.get("provider"), cfg.get("model"), len(self._hub.servers)))
        self._agent = Agent(self.tools, make_provider(cfg),
                            confirm=self._confirm_blocking,
                            on_event=self._event.emit)

    def shutdown(self):
        """Called from MainWindow.closeEvent: stop worker and MCP children."""
        if self._agent is not None:
            self._agent.cancel()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(2.0)
        if self._hub is not None:
            self._hub.close()

    def eventFilter(self, obj, e):
        if obj is self.input and e.type() == QEvent.Type.KeyPress:
            if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
                    not e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._send()
                return True
        return super().eventFilter(obj, e)

    # ---- sending / stopping --------------------------------------------
    def _send(self):
        if self._busy:
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self._stream_open = False
        self._append_html("<p><b>You:</b> %s</p>" % html.escape(text).replace("\n", "<br>"))
        self._busy = True
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Thinking…")
        self._worker = threading.Thread(target=self._work, args=(text,),
                                        name="refsynapse-agent", daemon=True)
        self._worker.start()

    def _work(self, text):
        try:
            self._agent.run(text)
        except Exception as ex:
            self._event.emit({"type": "error", "message": "Agent crashed: %s" % ex})
        finally:
            self._event.emit({"type": "idle"})

    def _stop(self):
        if self._agent is not None:
            self._agent.cancel()
        self.stop_btn.setEnabled(False)
        self.status.setText("Stopping…")

    def _clear(self):
        if self._busy:
            return
        self._agent.reset()
        self.view.clear()
        self.thinking.clear()
        self._stream_open = False
        self._append_html('<p style="color:#888">Conversation cleared.</p>')

    def _open_settings(self):
        if self._busy:
            return
        dialog = AiSettingsDialog(self.win, self)
        if dialog.exec():
            self._rebuild_agent()
            self._append_html('<p style="color:#888">Settings applied; conversation reset.</p>')

    # ---- confirmation (worker -> GUI -> worker) -------------------------
    def _confirm_blocking(self, name, arguments):
        payload = {"name": name, "arguments": arguments,
                   "answer": False, "done": threading.Event()}
        self._confirm_req.emit(payload)
        while not payload["done"].wait(0.2):
            if self._agent is not None and self._agent._cancel.is_set():
                return False
        return bool(payload["answer"])

    def _on_confirm(self, payload):
        name = payload["name"]
        external = name.startswith(NAME_PREFIX)
        box = QMessageBox(self)
        box.setWindowTitle("Confirm external tool" if external
                           else "Confirm destructive action")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("The assistant wants to call an external MCP tool:\n%s" % name
                    if external else "The assistant wants to run %s." % name)
        box.setInformativeText(json.dumps(payload["arguments"], ensure_ascii=False)[:600])
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        payload["answer"] = box.exec() == QMessageBox.StandardButton.Yes
        payload["done"].set()

    # ---- agent events (GUI thread) --------------------------------------
    def _on_event(self, event):
        kind = event.get("type")
        if kind == "status":
            self.status.setText(event.get("text", ""))
        elif kind == "reasoning":
            self.thinking.moveCursor(QTextCursor.MoveOperation.End)
            self.thinking.insertPlainText(event.get("text", ""))
        elif kind == "text":
            self._stream_text(event.get("text", ""))
        elif kind == "tool_call":
            self._stream_open = False
            args = json.dumps(event.get("arguments", {}), ensure_ascii=False)
            self._append_html('<p style="color:#888">tool %s <code>%s</code></p>'
                              % (html.escape(event.get("name", "")),
                                 html.escape(args[:200])))
        elif kind == "tool_result":
            ok = event.get("ok")
            color = "#7cb87c" if ok else "#e07070"
            mark = "ok" if ok else "FAILED"
            detail = "" if ok else html.escape(str(event.get("result", {}).get("error", ""))[:200])
            self._append_html('<p style="color:%s">%s %s %s</p>'
                              % (color, mark, html.escape(event.get("name", "")), detail))
        elif kind == "image":
            self._stream_open = False
            self._append_html('<p><img src="data:%s;base64,%s" width="420"></p>'
                              % (event.get("mime", "image/png"), event.get("data", "")))
        elif kind == "error":
            self._stream_open = False
            self._append_html('<p style="color:#e07070"><b>Error:</b> %s</p>'
                              % html.escape(event.get("message", "")))
            self.status.setText("Error")
        elif kind == "cancelled":
            self._stream_open = False
            self._append_html('<p style="color:#888">Stopped.</p>')
        elif kind == "done":
            self._stream_open = False
            self.status.setText("Ready")
        elif kind == "idle":
            self._busy = False
            self.send_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.status.setText("Ready")
            self._report_hub_errors()
            self.input.setFocus()

    def _report_hub_errors(self):
        if self._hub is None:
            return
        errors = self._hub.errors()
        if errors and errors != self._last_hub_errors:
            for server, message in errors.items():
                self._append_html('<p style="color:#e0a070">MCP server %s: %s</p>'
                                  % (html.escape(server), html.escape(message)))
        self._last_hub_errors = errors

    # ---- transcript helpers ---------------------------------------------
    def _stream_text(self, piece):
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self._stream_open:
            cursor.insertHtml("<p><b>Assistant:</b> ")
            self._stream_open = True
        cursor.insertText(piece)
        self.view.setTextCursor(cursor)
        self.view.ensureCursorVisible()

    def _append_html(self, markup):
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(markup)
        self.view.setTextCursor(cursor)
        self.view.ensureCursorVisible()


def install_chat(win):
    """Attach the chat dock + Ctrl+J. Call once at the end of MainWindow.__init__."""
    panel = ChatPanel(win)
    dock = QDockWidget("AI Assistant", win)
    dock.setObjectName("ai_chat_dock")
    dock.setWidget(panel)
    dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea |
                         Qt.DockWidgetArea.RightDockWidgetArea)
    win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    dock.hide()

    def toggle():
        visible = not dock.isVisible()
        dock.setVisible(visible)
        if visible:
            panel.input.setFocus()
        else:
            win.view.setFocus()

    shortcut = QShortcut(QKeySequence("Ctrl+J"), win)
    shortcut.activated.connect(toggle)
    win.chat_panel = panel
    win.chat_dock = dock
    return dock