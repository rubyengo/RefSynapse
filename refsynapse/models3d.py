"""Portable mesh/CAD import and a lightweight canvas-native 3D preview."""
import math
import os
import tempfile

import numpy as np
from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QImage
from PySide6.QtWidgets import QGraphicsItem

from .config import APP_DIR, FLAGS, new_id
from .items import draw_selection
from .software_render import rasterize

MODEL3D_EXT = {".stl", ".3mf", ".obj", ".fbx", ".gltf", ".glb",
               ".step", ".stp", ".ply"}


def _log_gpu_error(message):
    try:
        path = os.path.join(APP_DIR, "gpu_diagnostics.log")
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(str(message).strip() + "\n")
    except Exception:
        pass


def _visual_texture(visual):
    uv = getattr(visual, "uv", None)
    material = getattr(visual, "material", None)
    image = getattr(material, "baseColorTexture", None)
    if image is None:
        image = getattr(material, "image", None)
    return uv, image


def _trimesh_mesh(path):
    import trimesh
    loaded = trimesh.load(path, process=False)
    if isinstance(loaded, trimesh.Scene):
        parts = loaded.dump()  # applies each node's transform to its geometry
        if not parts:
            raise ValueError("The 3D scene contains no mesh geometry")
    else:
        parts = [loaded]
    vertices, faces, uv_parts = [], [], []
    texture = None
    for part in parts:
        if not hasattr(part, "vertices") or not hasattr(part, "faces"):
            continue
        points = np.asarray(part.vertices, dtype=np.float64).reshape(-1, 3)
        triangles = np.asarray(part.faces, dtype=np.int64).reshape(-1, 3)
        uv, image = _visual_texture(getattr(part, "visual", None))
        if texture is None and image is not None and hasattr(image, "convert"):
            texture = np.asarray(image.convert("RGB"), dtype=np.uint8).copy()
        valid_uv = uv is not None and len(uv) == len(points)
        # A single software material slot is used per object. Other materials
        # remain neutral rather than receiving a texture that does not belong.
        use_uv = valid_uv and image is not None and np.array_equal(
            np.asarray(image.convert("RGB")) if hasattr(image, "convert") else [], texture)
        uv_parts.append(np.asarray(uv, dtype=np.float64) if use_uv
                        else np.full((len(points), 2), np.nan))
        offset = sum(len(part_vertices) for part_vertices in vertices)
        vertices.append(points)
        faces.append(triangles + offset)
    if not vertices or not faces:
        raise ValueError("The file contains no triangular mesh")
    uvs = np.vstack(uv_parts) if texture is not None else None
    return np.vstack(vertices), np.vstack(faces), uvs, texture


def _step_mesh(path):
    # STEP is B-rep rather than a mesh: OpenCASCADE performs the required
    # tessellation, after which the same renderer handles it as every other model.
    import cadquery as cq
    with tempfile.TemporaryDirectory(prefix="RefSynapse-step-") as folder:
        target = os.path.join(folder, "mesh.stl")
        shape = cq.importers.importStep(path)
        cq.exporters.export(shape, target, exportType=cq.exporters.ExportTypes.STL,
                            tolerance=0.12, angularTolerance=0.22)
        return _trimesh_mesh(target)


def _fbx_assimp(path):
    import assimp_py
    flags = assimp_py.Process_Triangulate | assimp_py.Process_CalcTangentSpace
    flags |= getattr(assimp_py, "Process_PreTransformVertices", 0)
    # Current assimp_py uses import_file; accept the old camelCase binding too.
    importer = getattr(assimp_py, "import_file", None)
    importer = importer or getattr(assimp_py, "ImportFile", None)
    if importer is None:
        raise RuntimeError("assimp_py has no FBX import function")
    scene = importer(path, flags)
    if scene is None:
        raise ValueError("Assimp could not read this FBX file")
    vertices, faces = [], []
    for mesh in scene.meshes:
        points = np.asarray(mesh.vertices, dtype=np.float64).reshape(-1, 3)
        indices = np.asarray(mesh.indices, dtype=np.int64)
        if indices.ndim == 1:
            triangles = indices.reshape(-1, 3)
        else:
            triangles = []
            for polygon in indices:
                for i in range(1, len(polygon) - 1):
                    triangles.append((polygon[0], polygon[i], polygon[i + 1]))
            triangles = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
        offset = sum(len(part) for part in vertices)
        vertices.append(points)
        faces.append(triangles + offset)
    if not vertices or not faces:
        raise ValueError("The FBX file contains no mesh geometry")
    return np.vstack(vertices), np.vstack(faces).reshape(-1, 3)


def _fbx_mesh(path):
    try:
        return _fbx_assimp(path)
    except Exception as assimp_error:
        # Some FBX versions/exporters are rejected by Assimp. Recover the
        # static mesh directly instead of refusing the reference altogether.
        try:
            from .fbx_reader import read_fbx
            return read_fbx(path)
        except Exception as fallback_error:
            raise ValueError(
                "FBX import failed with both readers.\n"
                f"Assimp: {assimp_error}\n"
                f"Fallback: {fallback_error}") from fallback_error


def load_model(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".step", ".stp"):
        result = _step_mesh(path)
    elif ext == ".fbx":
        result = _fbx_mesh(path)
    else:
        result = _trimesh_mesh(path)
    # FBX currently has geometry only; mesh loaders may also supply UVs + RGB.
    return result if len(result) == 4 else (*result, None, None)


class Model3DItem(QGraphicsItem):
    """Software-rendered, orbitable 3D object that behaves like other references."""
    kind = "model3d"

    def __init__(self, path):
        super().__init__()
        self.uid, self.path, self.note = new_id(), path, None
        self.error = ""
        self._valid = False
        self._width, self._height = 640.0, 500.0
        self.yaw, self.pitch, self.model_zoom = -35.0, 24.0, 1.0
        self.pan_x, self.pan_y = 0.0, 0.0
        self.navigation_hint = "Alt+LMB orbit  •  Alt+Shift+LMB pan  •  Wheel zoom"
        self.vertices = np.empty((0, 3), dtype=np.float64)
        self.faces = np.empty((0, 3), dtype=np.int64)
        self.vertex_normals = np.empty((0, 3), dtype=np.float64)
        self.uvs, self.texture = None, None
        self.smooth = False
        self.triangle_count = 0
        self._pixmap = QPixmap()
        self._gl_resources = None
        self._gl_failed = False
        self._gl_error = ""
        self.render_backend = "CPU"
        # Coalesce mouse-move events into interactive frames, then restore the
        # full-resolution image shortly after the pointer stops moving.
        self._frame_timer = QTimer()
        self._frame_timer.setSingleShot(True)
        self._frame_timer.setInterval(25)
        self._frame_timer.timeout.connect(self._render_interactive)
        self._full_timer = QTimer()
        self._full_timer.setSingleShot(True)
        self._full_timer.setInterval(160)
        self._full_timer.timeout.connect(self._render_full)
        self.setFlags(FLAGS)
        try:
            vertices, faces, uvs, texture = load_model(path)
            self._set_mesh(vertices, faces, uvs, texture)
            self._render()
            self._valid = True
        except Exception as exc:
            self.error = str(exc)

    def _set_mesh(self, vertices, faces, uvs=None, texture=None):
        vertices = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
        faces = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
        valid = np.isfinite(vertices).all(axis=1)
        if not valid.all():
            vertices = np.where(valid[:, None], vertices, 0.0)
        faces = faces[(faces >= 0).all(axis=1) & (faces < len(vertices)).all(axis=1)]
        if not len(vertices) or not len(faces):
            raise ValueError("The model has no valid triangles")
        lower, upper = vertices.min(axis=0), vertices.max(axis=0)
        center = (lower + upper) / 2
        radius = float(np.linalg.norm(vertices - center, axis=1).max())
        if not math.isfinite(radius) or radius < 1e-12:
            raise ValueError("The model has invalid or zero-size geometry")
        # Always render the complete source mesh. Interaction is accelerated by
        # temporary output scaling, never by removing or simplifying triangles.
        self.vertices = (vertices - center) / radius
        self.triangle_count = len(faces)
        self.faces = faces
        self.uvs = (np.asarray(uvs, dtype=np.float64).reshape(-1, 2)
                    if uvs is not None and len(uvs) == len(vertices) else None)
        self.texture = (np.asarray(texture, dtype=np.uint8)
                        if texture is not None and self.uvs is not None else None)
        # Area-weighted vertex normals, welded by position so Assimp's split
        # vertices still produce a continuous Smooth surface.
        triangles = self.vertices[self.faces]
        face_normals = np.cross(triangles[:, 1] - triangles[:, 0],
                                triangles[:, 2] - triangles[:, 0])
        _, inverse = np.unique(np.round(self.vertices, 6), axis=0, return_inverse=True)
        inverse = inverse.reshape(-1)
        welded = np.zeros((int(inverse.max()) + 1, 3), dtype=np.float64)
        for corner in range(3):
            np.add.at(welded, inverse[self.faces[:, corner]], face_normals)
        self.vertex_normals = welded[inverse]
        lengths = np.linalg.norm(self.vertex_normals, axis=1)
        lengths[lengths < 1e-12] = 1.0
        self.vertex_normals /= lengths[:, None]

    def is_valid(self):
        return self._valid

    def boundingRect(self):
        return QRectF(0, 0, self._width, self._height)

    def smooth_rect(self):
        return QRectF(self._width - 154, 7, 142, 25)

    def smooth_hit(self, point):
        return self.smooth_rect().contains(point)

    def _request_interactive_render(self):
        self._full_timer.start()
        if not self._frame_timer.isActive():
            self._frame_timer.start()

    def orbit(self, dx, dy):
        self.yaw = (self.yaw + dx * 0.55) % 360
        self.pitch = max(-89.0, min(89.0, self.pitch + dy * 0.55))
        self._request_interactive_render()

    def pan(self, dx, dy):
        # Store the offset in viewport units, independent of model zoom.
        self.pan_x = max(-4.0, min(4.0, self.pan_x + dx / 480.0))
        self.pan_y = max(-4.0, min(4.0, self.pan_y + dy / 480.0))
        self._request_interactive_render()

    def zoom(self, factor):
        old_zoom = self.model_zoom
        self.model_zoom = max(0.15, min(8.0, self.model_zoom * factor))
        if abs(self.model_zoom - old_zoom) > 1e-9:
            self._request_interactive_render()
            return True
        return False

    def set_smooth(self, enabled):
        enabled = bool(enabled)
        if self.smooth != enabled:
            self.smooth = enabled
            self._render()
            self.update()

    def reset_view(self):
        self.yaw, self.pitch, self.model_zoom = -35.0, 24.0, 1.0
        self.pan_x, self.pan_y = 0.0, 0.0
        self._render()
        self.update()

    def restore_state(self, data):
        self.yaw = float(data.get("yaw", -35.0))
        self.pitch = max(-89.0, min(89.0, float(data.get("pitch", 24.0))))
        self.model_zoom = max(0.15, min(8.0, float(data.get("model_zoom", 1.0))))
        self.pan_x = max(-4.0, min(4.0, float(data.get("pan_x", 0.0))))
        self.pan_y = max(-4.0, min(4.0, float(data.get("pan_y", 0.0))))
        self.smooth = bool(data.get("smooth", False))
        self._render()
        self.update()

    def _render_interactive(self):
        self._render(interactive=True)

    def _render_full(self):
        self._frame_timer.stop()
        self._render(interactive=False)

    def _render(self, interactive=False):
        if not len(self.faces):
            return
        # The full mesh is used at both quality levels. Only the temporary
        # framebuffer is smaller while orbiting, panning or zooming.
        width, height = (480, 360) if interactive else (960, 720)
        try:
            from .gpu_render import render_model_gpu
            gpu_image, gpu_error = render_model_gpu(self, width, height)
        except Exception as exc:
            self._gl_failed = True
            gpu_image = None
            gpu_error = f"{type(exc).__name__}: {exc}"
            _log_gpu_error("OpenGL module loading: " + gpu_error)
        if gpu_image is not None and not gpu_image.isNull():
            self.render_backend = "GPU"
            self.setToolTip("OpenGL hardware renderer")
            # The framebuffer already contains the finished model. Never draw
            # the viewport grid over it as a post-processing layer.
            self._pixmap = QPixmap.fromImage(gpu_image)
            self.update()
            return
        self.render_backend = "CPU"
        self.setToolTip("Software renderer" + (f"\nOpenGL: {gpu_error}" if gpu_error else ""))
        yaw, pitch = math.radians(self.yaw), math.radians(self.pitch)
        cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
        rotate_y = np.array(((cy, 0, sy), (0, 1, 0), (-sy, 0, cy)))
        rotate_x = np.array(((1, 0, 0), (0, cp, -sp), (0, sp, cp)))
        rotation = rotate_x @ rotate_y
        rotated = self.vertices @ rotation.T
        # Flat shading never reads vertex normals, so avoid rotating a large
        # normal array on every interactive frame in that mode.
        rotated_normals = (self.vertex_normals @ rotation.T if self.smooth
                           else self.vertex_normals)
        rgb = rasterize(rotated, self.faces, rotated_normals,
                        width=width, height=height, zoom=self.model_zoom,
                        pan_x=self.pan_x, pan_y=self.pan_y, smooth=self.smooth,
                        uvs=self.uvs, texture=self.texture)
        # Copy because QImage otherwise only borrows NumPy's temporary buffer.
        image = QImage(rgb.data, width, height, width * 3,
                       QImage.Format_RGB888).copy()
        self._pixmap = QPixmap.fromImage(image)
        self.update()

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.fillRect(self.boundingRect(), QColor("#343434"))
        painter.setPen(QPen(QColor("#555555"), 1))
        painter.drawRect(self.boundingRect())
        painter.setPen(QColor("#e1e6eb"))
        painter.setFont(QFont("Segoe UI", 10))
        painter.drawText(QRectF(14, 7, self._width - 182, 25),
                         Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine,
                         os.path.basename(self.path))
        # A real checkbox inside every 3D field, independent of context menus.
        box = QRectF(self._width - 150, 13, 13, 13)
        painter.setPen(QPen(QColor("#9aa7b2"), 1))
        painter.setBrush(QColor("#262626"))
        painter.drawRect(box)
        if self.smooth:
            painter.setPen(QPen(QColor("#8fd18f"), 2))
            painter.drawLine(int(box.left() + 2), int(box.top() + 6),
                             int(box.left() + 5), int(box.top() + 10))
            painter.drawLine(int(box.left() + 5), int(box.top() + 10),
                             int(box.left() + 11), int(box.top() + 2))
        painter.setPen(QColor("#c7d0d8"))
        painter.setFont(QFont("Segoe UI", 9))
        painter.drawText(QRectF(self._width - 131, 7, 119, 25),
                         Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine,
                         "Smooth Shading")
        content = QRectF(12, 38, self._width - 24, self._height - 76)
        painter.drawPixmap(content, self._pixmap, QRectF(self._pixmap.rect()))
        painter.setPen(QColor("#8e9aa6"))
        painter.setFont(QFont("Segoe UI", 9))
        painter.drawText(QRectF(14, self._height - 32, self._width - 28, 22),
                         Qt.AlignCenter | Qt.TextSingleLine,
                         f"{self.render_backend}  •  {self.triangle_count:,} triangles  •  {self.navigation_hint}  •  double-click: reset")
        if self.isSelected():
            draw_selection(self, painter)
