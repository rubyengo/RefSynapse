"""Software rasterizer with an optional compiled Numba backend."""
import numpy as np

try:
    from numba import njit
except ImportError:  # The pure NumPy fallback remains fully functional.
    njit = None


def backend_name():
    return "Numba" if njit is not None else "NumPy"


def _sample_texture(texture, u, v):
    """Bilinear sampling using trimesh's bottom-left UV convention."""
    height, width = texture.shape[:2]
    # This is also how trimesh.visual.color.uv_to_color samples an image.
    # Keeping integer UV=1 on the last texel avoids atlas edge/seam errors.
    x = np.mod(u * (width - 1), width)
    y = np.mod((1.0 - v) * (height - 1), height)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = (x0 + 1) % width
    y1 = (y0 + 1) % height
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    top = texture[y0, x0] * (1 - fx) + texture[y0, x1] * fx
    bottom = texture[y1, x0] * (1 - fx) + texture[y1, x1] * fx
    return top * (1 - fy) + bottom * fy


def _prepare(vertices, faces, vertex_normals, width, height, zoom, pan_x, pan_y,
             smooth, uvs, texture):
    rotated = np.ascontiguousarray(vertices, dtype=np.float64)
    faces = np.ascontiguousarray(faces, dtype=np.int64)
    triangles = np.ascontiguousarray(rotated[faces])

    key = np.array((-0.45, 0.65, 1.0))
    key /= np.linalg.norm(key)
    fill = np.array((0.75, -0.25, 0.55))
    fill /= np.linalg.norm(fill)
    half = key + np.array((0.0, 0.0, 1.0))
    half /= np.linalg.norm(half)

    def brightness(normal):
        diffuse = (0.24 + 0.58 * np.abs(normal @ key)
                   + 0.16 * np.abs(normal @ fill))
        specular = np.abs(normal @ half) ** 18
        return np.clip(diffuse + 0.22 * specular, 0.18, 1.0)

    # Compute only the lighting data used by the active shading mode. Dense
    # meshes avoid a full face-normal pass in Smooth mode and a full vertex-
    # normal copy/lighting pass in Flat mode.
    if smooth:
        normals = np.ascontiguousarray(vertex_normals, dtype=np.float64)
        smooth_values = np.ascontiguousarray(brightness(normals))
        flat_values = np.empty(0, dtype=np.float64)
    else:
        face_normals = np.cross(triangles[:, 1] - triangles[:, 0],
                                triangles[:, 2] - triangles[:, 0])
        lengths = np.linalg.norm(face_normals, axis=1)
        lengths[lengths < 1e-12] = 1.0
        face_normals /= lengths[:, None]
        flat_values = brightness(face_normals)
        smooth_values = np.empty(0, dtype=np.float64)
    unit = min(width, height)
    scale = unit * 0.41 * zoom
    screen_x = np.ascontiguousarray(
        width / 2 + rotated[:, 0] * scale + pan_x * unit)
    screen_y = np.ascontiguousarray(
        height / 2 - rotated[:, 1] * scale + pan_y * unit)
    has_texture = texture is not None and uvs is not None and len(uvs) == len(vertices)
    if has_texture:
        texture = np.asarray(texture)
        if texture.ndim == 2:
            texture = np.repeat(texture[:, :, None], 3, axis=2)
        texture = np.ascontiguousarray(texture[:, :, :3], dtype=np.uint8)
        uvs = np.ascontiguousarray(uvs, dtype=np.float64)
    else:
        texture = np.empty((1, 1, 3), dtype=np.uint8)
        uvs = np.empty((1, 2), dtype=np.float64)
    return (triangles, faces, screen_x, screen_y, flat_values, smooth_values,
            uvs, texture, has_texture)


def _rasterize_numpy(triangles, faces, screen_x, screen_y, flat_values,
                     smooth_values, uvs, texture, rgb, z_buffer, smooth, has_texture):
    height, width = z_buffer.shape
    texture_float = texture.astype(np.float64) if has_texture else texture
    for index, face in enumerate(faces):
        x = screen_x[face]
        y = screen_y[face]
        denominator = ((y[1] - y[2]) * (x[0] - x[2])
                       + (x[2] - x[1]) * (y[0] - y[2]))
        if abs(denominator) < 1e-9:
            continue
        xmin = max(0, int(np.floor(x.min())))
        xmax = min(width - 1, int(np.ceil(x.max())))
        ymin = max(0, int(np.floor(y.min())))
        ymax = min(height - 1, int(np.ceil(y.max())))
        if xmin > xmax or ymin > ymax:
            continue
        px = np.arange(xmin, xmax + 1, dtype=np.float64)[None, :] + 0.5
        py = np.arange(ymin, ymax + 1, dtype=np.float64)[:, None] + 0.5
        w0 = ((y[1] - y[2]) * (px - x[2]) + (x[2] - x[1]) * (py - y[2])) / denominator
        w1 = ((y[2] - y[0]) * (px - x[2]) + (x[0] - x[2]) * (py - y[2])) / denominator
        w2 = 1.0 - w0 - w1
        mask = (w0 >= -1e-7) & (w1 >= -1e-7) & (w2 >= -1e-7)
        if not mask.any():
            continue
        depths = triangles[index, :, 2]
        z = w0 * depths[0] + w1 * depths[1] + w2 * depths[2]
        depth_region = z_buffer[ymin:ymax + 1, xmin:xmax + 1]
        mask &= z > depth_region
        if not mask.any():
            continue

        if smooth:
            values = (w0 * smooth_values[face[0]] + w1 * smooth_values[face[1]]
                      + w2 * smooth_values[face[2]])
        else:
            values = np.full(z.shape, flat_values[index])
        textured = has_texture and np.isfinite(uvs[face]).all()
        if textured:
            uv = uvs[face]
            u = w0 * uv[0, 0] + w1 * uv[1, 0] + w2 * uv[2, 0]
            v = w0 * uv[0, 1] + w1 * uv[1, 1] + w2 * uv[2, 1]
            sampled = _sample_texture(texture_float, u, v)
            colors = sampled * (0.42 + 0.58 * values[..., None])
        else:
            gray = 48 + 174 * values
            colors = np.stack((np.minimum(232, gray + 3),
                               np.minimum(230, gray + 1),
                               np.maximum(0, gray - 2)), axis=-1)
        region = rgb[ymin:ymax + 1, xmin:xmax + 1]
        region[mask] = np.clip(colors[mask], 0, 255).astype(np.uint8)
        depth_region[mask] = z[mask]
    return rgb


if njit is not None:
    @njit(cache=True, fastmath=True, nogil=True)
    def _rasterize_fast(triangles, faces, screen_x, screen_y, flat_values,
                        smooth_values, uvs, texture, rgb, z_buffer, smooth, has_texture):
        height, width = z_buffer.shape
        tex_h, tex_w = texture.shape[:2]
        for index in range(faces.shape[0]):
            i0, i1, i2 = faces[index]
            x0, x1, x2 = screen_x[i0], screen_x[i1], screen_x[i2]
            y0, y1, y2 = screen_y[i0], screen_y[i1], screen_y[i2]
            denominator = ((y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2))
            if abs(denominator) < 1e-9:
                continue
            xmin = max(0, int(np.floor(min(x0, x1, x2))))
            xmax = min(width - 1, int(np.ceil(max(x0, x1, x2))))
            ymin = max(0, int(np.floor(min(y0, y1, y2))))
            ymax = min(height - 1, int(np.ceil(max(y0, y1, y2))))
            if xmin > xmax or ymin > ymax:
                continue
            textured = has_texture
            if textured:
                for vertex in (i0, i1, i2):
                    if not np.isfinite(uvs[vertex, 0]) or not np.isfinite(uvs[vertex, 1]):
                        textured = False
                        break
            for py in range(ymin, ymax + 1):
                sy = py + 0.5
                for px in range(xmin, xmax + 1):
                    sx = px + 0.5
                    w0 = ((y1 - y2) * (sx - x2) + (x2 - x1) * (sy - y2)) / denominator
                    w1 = ((y2 - y0) * (sx - x2) + (x0 - x2) * (sy - y2)) / denominator
                    w2 = 1.0 - w0 - w1
                    if w0 < -1e-7 or w1 < -1e-7 or w2 < -1e-7:
                        continue
                    z = (w0 * triangles[index, 0, 2] + w1 * triangles[index, 1, 2]
                         + w2 * triangles[index, 2, 2])
                    if z <= z_buffer[py, px]:
                        continue
                    if smooth:
                        value = (w0 * smooth_values[i0] + w1 * smooth_values[i1]
                                 + w2 * smooth_values[i2])
                    else:
                        value = flat_values[index]
                    if textured:
                        u = (w0 * uvs[i0, 0] + w1 * uvs[i1, 0] + w2 * uvs[i2, 0])
                        v = (w0 * uvs[i0, 1] + w1 * uvs[i1, 1] + w2 * uvs[i2, 1])
                        tx = (u * (tex_w - 1)) % tex_w
                        ty = ((1.0 - v) * (tex_h - 1)) % tex_h
                        ix0, iy0 = int(np.floor(tx)), int(np.floor(ty))
                        ix1, iy1 = (ix0 + 1) % tex_w, (iy0 + 1) % tex_h
                        fx, fy = tx - ix0, ty - iy0
                        light = 0.42 + 0.58 * value
                        for channel in range(3):
                            top = (texture[iy0, ix0, channel] * (1.0 - fx)
                                   + texture[iy0, ix1, channel] * fx)
                            bottom = (texture[iy1, ix0, channel] * (1.0 - fx)
                                      + texture[iy1, ix1, channel] * fx)
                            sampled = top * (1.0 - fy) + bottom * fy
                            rgb[py, px, channel] = min(255, max(0, int(sampled * light)))
                    else:
                        gray = 48 + 174 * value
                        rgb[py, px, 0] = min(232, max(0, int(gray + 3)))
                        rgb[py, px, 1] = min(230, max(0, int(gray + 1)))
                        rgb[py, px, 2] = min(255, max(0, int(gray - 2)))
                    z_buffer[py, px] = z
        return rgb
else:
    _rasterize_fast = None


def rasterize(vertices, faces, vertex_normals, width=960, height=720,
              zoom=1.0, pan_x=0.0, pan_y=0.0, smooth=False,
              uvs=None, texture=None):
    """Return an RGB uint8 image with a z-buffer and orthographic projection."""
    rgb = np.full((height, width, 3), 52, dtype=np.uint8)
    rgb[::60, :, :] = 62
    rgb[:, ::60, :] = 62
    z_buffer = np.full((height, width), -np.inf, dtype=np.float64)
    if not len(faces):
        return rgb
    prepared = _prepare(vertices, faces, vertex_normals, width, height, zoom,
                        pan_x, pan_y, smooth, uvs, texture)
    if _rasterize_fast is not None:
        return _rasterize_fast(*prepared[:-1], rgb, z_buffer, smooth, prepared[-1])
    return _rasterize_numpy(*prepared[:-1], rgb, z_buffer, smooth, prepared[-1])
