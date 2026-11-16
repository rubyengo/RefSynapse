"""Cursor direction helpers shared by the canvas and its mixins."""
from PySide6.QtCore import Qt


def edge_cursor(e):
    if e is None:
        return None
    E = Qt.Edge
    if e in (E.LeftEdge | E.TopEdge, E.RightEdge | E.BottomEdge):
        return Qt.SizeFDiagCursor
    if e in (E.RightEdge | E.TopEdge, E.LeftEdge | E.BottomEdge):
        return Qt.SizeBDiagCursor
    if e in (E.LeftEdge, E.RightEdge):
        return Qt.SizeHorCursor
    return Qt.SizeVerCursor


def handle_cursor(index):
    """Cursor direction matches the actual corner or side being dragged."""
    return (Qt.SizeFDiagCursor, Qt.SizeBDiagCursor, Qt.SizeBDiagCursor,
            Qt.SizeFDiagCursor, Qt.SizeHorCursor, Qt.SizeHorCursor,
            Qt.SizeVerCursor, Qt.SizeVerCursor)[index]