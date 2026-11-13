"""Application theme, single-instance entry point and error logging."""
import sys, os, json, time, traceback
from PySide6.QtCore import QTimer
from PySide6.QtGui import QPalette, QColor, QIcon
from PySide6.QtWidgets import QApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from .config import APP_NAME, APP_DIR, PROJECT_EXT, ICON_PATH
from .main_window import MainWindow

# =====================================================================
#  Dark theme
# =====================================================================
STYLE = """
QFrame#titleBar { background: #20242a; border-bottom: 1px solid #3c3c3c; }
QLabel#appTitle { color: #dddddd; font-size: 13px; font-weight: 600; padding: 0 12px; }
QLabel#canvasProjectTitle { color: #ffffff; background: rgba(24, 28, 34, 220);
                            border: 1px solid #4fc3f7; border-radius: 7px;
                            font-size: 16px; font-weight: 700; padding: 6px 18px; }
QFrame#titleBar QToolButton { background: #30363f; color: #eeeeee; border: 1px solid #454c56;
                              border-radius: 4px; padding: 4px 9px; }
QFrame#titleBar QToolButton:hover { background: #3d6fa5; }
QFrame#titleBar QToolButton:checked { background: #3d6fa5; color: white; border-color: #4fc3f7; }
QFrame#titleBar QToolButton#windowButton, QFrame#titleBar QToolButton#closeButton {
    background: transparent; border: none; font-size: 17px; padding: 0; }
QFrame#titleBar QToolButton#closeButton:hover { background: #c42b1c; }
QMenu { background: #262626; color: #dddddd; border: 1px solid #3c3c3c; padding: 4px 0; }
QMenu::item { padding: 5px 28px 5px 26px; }
QMenu::item:selected { background: #3d6fa5; color: white; }
QMenu::item:disabled { color: #666666; }
QMenu::separator { height: 1px; background: #3c3c3c; margin: 4px 10px; }
QGroupBox { border: 1px solid #444; border-radius: 4px; margin-top: 10px; padding-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
"""


def apply_dark_theme(app):
    app.setStyle("Fusion")
    R, G = QPalette.ColorRole, QPalette.ColorGroup
    pal = QPalette()
    for role, col in ((R.Window, "#2b2b2b"), (R.WindowText, "#dddddd"), (R.Base, "#1e1e1e"),
                      (R.AlternateBase, "#262626"), (R.Text, "#eeeeee"), (R.Button, "#3a3a3a"),
                      (R.ButtonText, "#dddddd"), (R.Highlight, "#3d6fa5"),
                      (R.HighlightedText, "#ffffff"), (R.ToolTipBase, "#333333"),
                      (R.ToolTipText, "#eeeeee"), (R.PlaceholderText, "#888888")):
        pal.setColor(role, QColor(col))
    for role in (R.Text, R.ButtonText, R.WindowText):
        pal.setColor(G.Disabled, role, QColor("#666666"))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)


ERROR_LOG = os.path.join(APP_DIR, "refsynapse-error.log")
INSTANCE_NAME = "RefSynapse.0.5.single-instance"


def log_exception(exc_type, exc_value, exc_traceback):
    """Keep pythonw failures visible instead of silently losing them."""
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write("\n[" + time.strftime("%Y-%m-%d %H:%M:%S") + "]\n")
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
    except OSError:
        pass


def send_to_running_instance(project):
    """Forward an Explorer double-click to the existing application window."""
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_NAME)
    if not socket.waitForConnected(350):
        return False
    message = json.dumps({"project": os.path.abspath(project) if project else ""},
                         ensure_ascii=False).encode("utf-8") + b"\n"
    socket.write(message)
    if not socket.waitForBytesWritten(1200):
        return False
    socket.flush()
    socket.disconnectFromServer()
    return True


def main():
    app = QApplication(sys.argv)
    sys.excepthook = log_exception
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(ICON_PATH))
    apply_dark_theme(app)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RefSynapse.0.5")
        except Exception:
            pass

    project = next((arg for arg in sys.argv[1:]
                    if arg.lower().endswith(PROJECT_EXT) and os.path.isfile(arg)), None)
    if send_to_running_instance(project):
        return

    server = QLocalServer()
    if not server.listen(INSTANCE_NAME):
        # A crashed process can leave a stale local-server endpoint behind.
        QLocalServer.removeServer(INSTANCE_NAME)
        server.listen(INSTANCE_NAME)

    w = MainWindow()
    w.setWindowIcon(QIcon(ICON_PATH))
    w.show()

    def open_external(path):
        if path and os.path.isfile(path):
            w.open_project(path)
        if w.isMinimized():
            w.showNormal()
        w.show()
        w.raise_()
        w.activateWindow()

    def read_client(client):
        raw = bytes(client.readAll())
        client.disconnectFromServer()
        try:
            message = json.loads(raw.decode("utf-8").strip())
            path = message.get("project", "") if isinstance(message, dict) else ""
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        QTimer.singleShot(0, lambda: open_external(path))

    def accept_clients():
        while server.hasPendingConnections():
            client = server.nextPendingConnection()
            client.readyRead.connect(lambda c=client: read_client(c))

    server.newConnection.connect(accept_clients)
    if project:
        QTimer.singleShot(0, lambda: w.open_project(project))
    else:
        QTimer.singleShot(0, w.show_startup)
    sys.exit(app.exec())


def main_entry():
    try:
        main()
    except Exception:
        log_exception(*sys.exc_info())
        raise
