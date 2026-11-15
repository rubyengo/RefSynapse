"""Full-geometry OpenGL preview with an automatic software-renderer fallback."""
import math
import os

import numpy as np
from PySide6.QtGui import (QMatrix4x4, QOffscreenSurface, QOpenGLContext,
                           QSurfaceFormat)
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject,
                              QOpenGLFramebufferObjectFormat, QOpenGLShader,
                              QOpenGLShaderProgram, QOpenGLTexture,
                              QOpenGLVertexArrayObject)
from shiboken6 import VoidPtr

_GL_TRIANGLES = 0x0004
_GL_DEPTH_TEST = 0x0B71
_GL_CULL_FACE = 0x0B44
_GL_COLOR_BUFFER_BIT = 0x00004000
_GL_DEPTH_BUFFER_BIT = 0x00000100
_GL_FLOAT = 0x1406
_GL_UNSIGNED_INT = 0x1405
_GL_TEXTURE_2D = 0x0DE1
_GL_TEXTURE0 = 0x84C0
_GL_ERROR_NAMES = {
    0x0500: "GL_INVALID_ENUM",
    0x0501: "GL_INVALID_VALUE",
    0x0502: "GL_INVALID_OPERATION",
    0x0503: "GL_STACK_OVERFLOW",
    0x0504: "GL_STACK_UNDERFLOW",
    0x0505: "GL_OUT_OF_MEMORY",
    0x0506: "GL_INVALID_FRAMEBUFFER_OPERATION",
    0x0507: "GL_CONTEXT_LOST",
}


def _log_error(message):
    """Keep driver/shader failures visible instead of silently hiding them."""
    try:
        from .config import APP_DIR
        path = os.path.join(APP_DIR, "gpu_diagnostics.log")
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(str(message).strip() + "\n")
    except Exception:
        pass


def _shader_sources(gles):
    if gles:
        vertex_header = "#version 300 es\nprecision highp float;\n"
        fragment_header = ("#version 300 es\nprecision highp float;\n"
                           "precision highp int;\n")
    else:
        vertex_header = "#version 330 core\n"
        fragment_header = "#version 330 core\n"
    vertex = vertex_header + """
in vec3 aPosition;
in vec3 aNormal;
in vec2 aUv;
in float aTextured;
uniform mat4 uMvp;
uniform mat4 uRotation;
out vec3 vNormal;
out vec3 vPosition;
out vec2 vUv;
out float vTextured;
void main() {
    vPosition = mat3(uRotation) * aPosition;
    vNormal = mat3(uRotation) * aNormal;
    vUv = aUv;
    vTextured = aTextured;
    gl_Position = uMvp * vec4(aPosition, 1.0);
}
"""
    common = fragment_header + """
in vec3 vNormal;
in vec3 vPosition;
in vec2 vUv;
in float vTextured;
uniform sampler2D uTexture;
uniform int uHasTexture;
out vec4 fragColor;

float brightness(vec3 normal) {
    vec3 n = normalize(normal);
    vec3 key = normalize(vec3(-0.45, 0.65, 1.0));
    vec3 fill = normalize(vec3(0.75, -0.25, 0.55));
    vec3 halfVector = normalize(key + vec3(0.0, 0.0, 1.0));
    float diffuse = 0.24 + 0.58 * abs(dot(n, key)) + 0.16 * abs(dot(n, fill));
    float specular = pow(abs(dot(n, halfVector)), 18.0);
    return clamp(diffuse + 0.22 * specular, 0.18, 1.0);
}

void writeColor(vec3 normal) {
    float value = brightness(normal);
    bool textured = uHasTexture != 0 && vTextured > 0.999;
    vec3 color;
    if (textured) {
        vec3 base = texture(uTexture, vec2(vUv.x, 1.0 - vUv.y)).rgb;
        color = base * (0.42 + 0.58 * value);
    } else {
        float gray = 48.0 + 174.0 * value;
        color = vec3(min(232.0, gray + 3.0),
                     min(230.0, gray + 1.0),
                     max(0.0, gray - 2.0)) / 255.0;
    }
    fragColor = vec4(clamp(color, 0.0, 1.0), 1.0);
}
"""
    flat = common + """
void main() {
    vec3 normal = cross(dFdx(vPosition), dFdy(vPosition));
    if (dot(normal, normal) < 1e-16)
        normal = vec3(0.0, 0.0, 1.0);
    writeColor(normal);
}
"""
    smooth = common + """
void main() {
    writeColor(vNormal);
}
"""
    return vertex, flat, smooth


def _matrix_values(model, width, height):
    """Match the software renderer's orthographic projection exactly."""
    yaw, pitch = math.radians(model.yaw), math.radians(model.pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    rotate_y = np.array(((cy, 0, sy), (0, 1, 0), (-sy, 0, cy)), dtype=np.float64)
    rotate_x = np.array(((1, 0, 0), (0, cp, -sp), (0, sp, cp)), dtype=np.float64)
    rotation = rotate_x @ rotate_y
    unit = min(width, height)
    scale = unit * 0.41 * model.model_zoom
    sx, sy = 2 * scale / width, 2 * scale / height
    mvp = np.eye(4, dtype=np.float64)
    mvp[0, :3] = sx * rotation[0]
    mvp[1, :3] = sy * rotation[1]
    # Larger rotated Z is closer, just as in the software z-buffer.
    mvp[2, :3] = -0.1 * rotation[2]
    mvp[0, 3] = 2 * model.pan_x * unit / width
    mvp[1, 3] = -2 * model.pan_y * unit / height
    rotation_4x4 = np.eye(4, dtype=np.float64)
    rotation_4x4[:3, :3] = rotation
    return mvp, rotation_4x4


class GpuRenderer:
    def __init__(self):
        self.failed = False
        self.error = ""
        self.context = None
        self.surface = None
        self.functions = None
        self.flat_program = None
        self.smooth_program = None
        self._fbos = {}
        try:
            self._initialize()
        except Exception as exc:
            self.failed = True
            self.error = f"{type(exc).__name__}: {exc}"
            _log_error("OpenGL initialization: " + self.error)
            if self.context is not None:
                self.context.doneCurrent()

    def _initialize(self):
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)
        self.context = QOpenGLContext()
        self.context.setFormat(fmt)
        if not self.context.create():
            self.failed = True
            self.error = "QOpenGLContext.create() failed; OpenGL 3.3 is unavailable"
            _log_error(self.error)
            return
        self.surface = QOffscreenSurface()
        self.surface.setFormat(self.context.format())
        self.surface.create()
        if not self.context.makeCurrent(self.surface):
            self.failed = True
            self.error = "QOpenGLContext.makeCurrent() failed on the offscreen surface"
            _log_error(self.error)
            return
        self.functions = self.context.functions()
        self.functions.initializeOpenGLFunctions()
        vertex, flat, smooth = _shader_sources(self.context.isOpenGLES())
        self.flat_program = self._make_program(vertex, flat)
        self.smooth_program = self._make_program(vertex, smooth)
        self.context.doneCurrent()

    @staticmethod
    def _make_program(vertex_source, fragment_source):
        program = QOpenGLShaderProgram()
        if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex,
                                               vertex_source):
            raise RuntimeError(program.log())
        if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment,
                                               fragment_source):
            raise RuntimeError(program.log())
        for location, name in enumerate(("aPosition", "aNormal", "aUv", "aTextured")):
            program.bindAttributeLocation(name, location)
        if not program.link():
            raise RuntimeError(program.log())
        return program

    @staticmethod
    def _buffer(buffer_type, array):
        buffer = QOpenGLBuffer(buffer_type)
        if not buffer.create():
            raise RuntimeError("Could not create an OpenGL buffer")
        buffer.setUsagePattern(QOpenGLBuffer.UsagePattern.StaticDraw)
        buffer.bind()
        data = np.ascontiguousarray(array)
        raw = data.tobytes()
        buffer.allocate(raw, len(raw))
        buffer.release()
        return buffer

    @staticmethod
    def _texture_constant(group, name):
        # PySide6 exposes these enums either as QOpenGLTexture.Group.Value
        # or, in older builds, directly as QOpenGLTexture.Value.
        scope = getattr(QOpenGLTexture, group, QOpenGLTexture)
        return getattr(scope, name)

    def _make_texture(self, texture):
        pixels = np.asarray(texture, dtype=np.uint8)
        if pixels.ndim == 2:
            pixels = np.repeat(pixels[:, :, None], 3, axis=2)
        pixels = pixels[:, :, :3]
        height, width = pixels.shape[:2]
        rgba = np.empty((height, width, 4), dtype=np.uint8)
        rgba[:, :, :3] = pixels
        rgba[:, :, 3] = 255
        texture_object = QOpenGLTexture(self._texture_constant("Target", "Target2D"))
        texture_object.setFormat(self._texture_constant("TextureFormat", "RGBA8_UNorm"))
        texture_object.setSize(width, height)
        texture_object.setAutoMipMapGenerationEnabled(False)
        texture_object.allocateStorage()
        texture_object.setData(self._texture_constant("PixelFormat", "RGBA"),
                               self._texture_constant("PixelType", "UInt8"),
                               np.ascontiguousarray(rgba).tobytes())
        texture_object.setMinificationFilter(self._texture_constant("TextureFilter", "Linear"))
        texture_object.setMagnificationFilter(self._texture_constant("TextureFilter", "Linear"))
        texture_object.setWrapMode(self._texture_constant("CoordinateDirection", "DirectionS"),
                                   self._texture_constant("WrapMode", "Repeat"))
        texture_object.setWrapMode(self._texture_constant("CoordinateDirection", "DirectionT"),
                                   self._texture_constant("WrapMode", "Repeat"))
        return texture_object

    def _upload(self, model):
        positions = np.ascontiguousarray(model.vertices, dtype=np.float32)
        normals = np.ascontiguousarray(model.vertex_normals, dtype=np.float32)
        indices = np.ascontiguousarray(model.faces.reshape(-1), dtype=np.uint32)
        if model.uvs is not None and len(model.uvs) == len(positions):
            uv = np.nan_to_num(np.asarray(model.uvs, dtype=np.float32),
                               nan=0.0, posinf=0.0, neginf=0.0)
            textured = np.isfinite(model.uvs).all(axis=1).astype(np.float32)
        else:
            uv = np.zeros((len(positions), 2), dtype=np.float32)
            textured = np.zeros(len(positions), dtype=np.float32)
        resources = {
            "position": self._buffer(QOpenGLBuffer.Type.VertexBuffer, positions),
            "normal": self._buffer(QOpenGLBuffer.Type.VertexBuffer, normals),
            "uv": self._buffer(QOpenGLBuffer.Type.VertexBuffer, uv),
            "textured": self._buffer(QOpenGLBuffer.Type.VertexBuffer, textured),
            "index": self._buffer(QOpenGLBuffer.Type.IndexBuffer, indices),
            "texture": (self._make_texture(model.texture)
                        if model.texture is not None and model.uvs is not None else None),
        }
        vao = QOpenGLVertexArrayObject()
        if not vao.create():
            raise RuntimeError("Could not create an OpenGL vertex array")
        resources["vao"] = vao
        vao.bind()
        self.flat_program.bind()
        for location, (key, size) in enumerate((("position", 3), ("normal", 3),
                                                ("uv", 2), ("textured", 1))):
            buffer = resources[key]
            buffer.bind()
            self.flat_program.enableAttributeArray(location)
            self.flat_program.setAttributeBuffer(location, _GL_FLOAT, 0, size)
            buffer.release()
        resources["index"].bind()
        vao.release()
        resources["index"].release()
        self.flat_program.release()
        model._gl_resources = resources

    def _fbo(self, width, height):
        key = (width, height)
        if key not in self._fbos:
            fmt = QOpenGLFramebufferObjectFormat()
            fmt.setAttachment(QOpenGLFramebufferObject.Attachment.CombinedDepthStencil)
            fbo = QOpenGLFramebufferObject(width, height, fmt)
            if not fbo.isValid():
                raise RuntimeError("Could not create an OpenGL framebuffer")
            self._fbos[key] = fbo
        return self._fbos[key]

    def _clear_gl_errors(self):
        # Qt texture/FBO helpers can leave an old error in the shared context.
        # Do not mistake such an error for a failure of the model draw call.
        for _ in range(32):
            if self.functions.glGetError() == 0:
                break

    def _check_gl_error(self, stage):
        code = self.functions.glGetError()
        if code:
            name = _GL_ERROR_NAMES.get(code, "unknown OpenGL error")
            raise RuntimeError(f"{stage}: {name} (0x{code:04X})")

    def render(self, model, width, height):
        if self.failed or getattr(model, "_gl_failed", False):
            return None
        if not self.context.makeCurrent(self.surface):
            model._gl_failed = True
            model._gl_error = "Could not activate the OpenGL context for rendering"
            _log_error(model._gl_error)
            return None
        fbo = None
        try:
            self._clear_gl_errors()
            if getattr(model, "_gl_resources", None) is None:
                self._upload(model)
            self._check_gl_error("resource upload")
            resources = model._gl_resources
            program = self.smooth_program if model.smooth else self.flat_program
            mvp, rotation = _matrix_values(model, width, height)
            fbo = self._fbo(width, height)
            if not fbo.bind():
                raise RuntimeError("framebuffer bind failed")
            self._check_gl_error("framebuffer bind")
            self.functions.glViewport(0, 0, width, height)
            self.functions.glEnable(_GL_DEPTH_TEST)
            # The software renderer is double-sided; retain that behavior.
            self.functions.glDisable(_GL_CULL_FACE)
            self.functions.glClearColor(52 / 255, 52 / 255, 52 / 255, 1.0)
            self.functions.glClear(_GL_COLOR_BUFFER_BIT | _GL_DEPTH_BUFFER_BIT)
            self._check_gl_error("framebuffer clear")
            if not program.bind():
                raise RuntimeError("shader bind: " + program.log())
            self._check_gl_error("shader bind")
            program.setUniformValue("uMvp", QMatrix4x4(*mvp.reshape(-1).tolist()))
            self._check_gl_error("MVP uniform")
            program.setUniformValue("uRotation", QMatrix4x4(*rotation.reshape(-1).tolist()))
            self._check_gl_error("rotation uniform")
            has_texture = resources["texture"] is not None
            # Use explicit integer uniforms. Some PySide6 builds resolve the
            # overloaded setUniformValue(int) call incorrectly for samplers.
            self.functions.glUniform1i(program.uniformLocation("uHasTexture"),
                                       int(has_texture))
            self.functions.glUniform1i(program.uniformLocation("uTexture"), 0)
            self._check_gl_error("integer uniforms")
            if has_texture:
                if not resources["texture"].isCreated():
                    raise RuntimeError("texture bind: texture was not created")
                self.functions.glActiveTexture(_GL_TEXTURE0)
                self.functions.glBindTexture(_GL_TEXTURE_2D,
                                             resources["texture"].textureId())
            self._check_gl_error("texture bind")
            resources["vao"].bind()
            self._check_gl_error("VAO bind")
            self.functions.glDrawElements(_GL_TRIANGLES, len(model.faces) * 3,
                                          _GL_UNSIGNED_INT, VoidPtr(0))
            self._check_gl_error("glDrawElements")
            resources["vao"].release()
            if has_texture:
                self.functions.glBindTexture(_GL_TEXTURE_2D, 0)
            program.release()
            image = fbo.toImage()
            self._check_gl_error("framebuffer readback")
            fbo.release()
            fbo = None
            return image
        except Exception as exc:
            model._gl_failed = True
            model._gl_error = f"{type(exc).__name__}: {exc}"
            _log_error("OpenGL rendering: " + model._gl_error)
            return None
        finally:
            if fbo is not None:
                fbo.release()
            self.context.doneCurrent()


_renderer = None


def render_model_gpu(model, width, height):
    """Return (QImage | None, diagnostic error) for the active render path."""
    global _renderer
    if getattr(model, "_gl_failed", False):
        return None, getattr(model, "_gl_error", "OpenGL rendering failed")
    if _renderer is None:
        _renderer = GpuRenderer()
    if _renderer.failed:
        return None, _renderer.error or "OpenGL 3.3 is unavailable"
    image = _renderer.render(model, width, height)
    error = getattr(model, "_gl_error", "") if image is None else ""
    return image, error
