"""Small dependency-free fallback for static FBX mesh geometry.

It reads binary FBX 7.x and common ASCII FBX files. Scene animation,
materials and node transforms are intentionally left to the primary Assimp
importer; this fallback only recovers vertices and polygon indices.
"""
import re
import struct
import zlib

import numpy as np


class _BinaryFBX:
    def __init__(self, data):
        self.data = data
        self.version = struct.unpack_from("<I", data, 23)[0]
        self.wide = self.version >= 7500

    def node(self, offset):
        if self.wide:
            end, count, byte_count = struct.unpack_from("<QQQ", self.data, offset)
            header, null_size = 25, 25
            name_size = self.data[offset + 24]
        else:
            end, count, byte_count = struct.unpack_from("<III", self.data, offset)
            header, null_size = 13, 13
            name_size = self.data[offset + 12]
        if end <= offset or end > len(self.data):
            raise ValueError("Invalid FBX node record")
        start = offset + header
        name = self.data[start:start + name_size].decode("ascii", "replace")
        cursor = start + name_size
        properties = []
        for _ in range(count):
            value, cursor = self.property(cursor)
            properties.append(value)
        children = []
        while cursor < end - null_size:
            child, cursor = self.node(cursor)
            if child is not None:
                children.append(child)
        return (name, properties, children), end

    def property(self, offset):
        kind = chr(self.data[offset])
        offset += 1
        if kind in "YCI":
            formats = {"Y": "<h", "C": "<?", "I": "<i"}
            fmt = formats[kind]
            size = struct.calcsize(fmt)
            return struct.unpack_from(fmt, self.data, offset)[0], offset + size
        if kind == "F":
            return struct.unpack_from("<f", self.data, offset)[0], offset + 4
        if kind in "DL":
            fmt = "<d" if kind == "D" else "<q"
            return struct.unpack_from(fmt, self.data, offset)[0], offset + 8
        if kind in "SR":
            size = struct.unpack_from("<I", self.data, offset)[0]
            offset += 4
            raw = self.data[offset:offset + size]
            return (raw.decode("utf-8", "replace") if kind == "S" else raw), offset + size
        if kind in "fdlibc":
            length, encoding, byte_count = struct.unpack_from("<III", self.data, offset)
            offset += 12
            raw = self.data[offset:offset + byte_count]
            if encoding == 1:
                raw = zlib.decompress(raw)
            elif encoding != 0:
                raise ValueError("Unknown FBX array compression")
            dtypes = {"f": "<f4", "d": "<f8", "l": "<i8",
                      "i": "<i4", "b": "|b1", "c": "|b1"}
            values = np.frombuffer(raw, dtype=dtypes[kind], count=length)
            return values, offset + byte_count
        raise ValueError("Unsupported FBX property type: " + kind)

    def roots(self):
        offset = 27
        roots = []
        while offset < len(self.data):
            null_size = 25 if self.wide else 13
            if not any(self.data[offset:offset + null_size]):
                break
            node, offset = self.node(offset)
            roots.append(node)
        return roots


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node[2])


def _child(node, name):
    return next((child for child in node[2] if child[0] == name), None)


def _triangles(vertices, indices):
    vertices = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    polygons, current = [], []
    for raw in np.asarray(indices, dtype=np.int64).reshape(-1):
        end = raw < 0
        current.append(int(~raw if end else raw))
        if end:
            if len(current) >= 3:
                polygons.append(current)
            current = []
    faces = []
    for polygon in polygons:
        for index in range(1, len(polygon) - 1):
            faces.append((polygon[0], polygon[index], polygon[index + 1]))
    if not len(vertices) or not faces:
        raise ValueError("FBX geometry has no polygons")
    return vertices, np.asarray(faces, dtype=np.int64)


def _binary_mesh(path):
    with open(path, "rb") as handle:
        data = handle.read()
    if not data.startswith(b"Kaydara FBX Binary"):
        raise ValueError("Not a binary FBX file")
    roots = _BinaryFBX(data).roots()
    all_vertices, all_faces = [], []
    for node in _walk(roots):
        if node[0] != "Geometry":
            continue
        vertices_node, indices_node = _child(node, "Vertices"), _child(node, "PolygonVertexIndex")
        if not vertices_node or not indices_node or not vertices_node[1] or not indices_node[1]:
            continue
        vertices, faces = _triangles(vertices_node[1][0], indices_node[1][0])
        offset = sum(len(part) for part in all_vertices)
        all_vertices.append(vertices)
        all_faces.append(faces + offset)
    if not all_vertices:
        raise ValueError("No static mesh geometry found in binary FBX")
    return np.vstack(all_vertices), np.vstack(all_faces)


_NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")


def _array_after(text, match, name):
    pattern = re.compile(name + r"\s*:\s*(?:\*\s*\d+\s*)?\{?\s*(?:a\s*:)?(.*?)\}", re.S)
    found = pattern.search(text, match.end())
    if not found:
        return None
    return [_NUMBER.findall(found.group(1)), found]


def _ascii_mesh(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        text = handle.read()
    vertices_pattern = re.compile(r"Vertices\s*:\s*(?:\*\s*\d+\s*)?\{?\s*(?:a\s*:)?(.*?)\}", re.S)
    all_vertices, all_faces = [], []
    for match in vertices_pattern.finditer(text):
        result = _array_after(text, match, "PolygonVertexIndex")
        if result is None:
            continue
        indices, _ = result
        vertices = [float(value) for value in _NUMBER.findall(match.group(1))]
        indices = [int(float(value)) for value in indices]
        if len(vertices) < 9 or len(indices) < 3:
            continue
        points, faces = _triangles(vertices, indices)
        offset = sum(len(part) for part in all_vertices)
        all_vertices.append(points)
        all_faces.append(faces + offset)
    if not all_vertices:
        raise ValueError("No static mesh geometry found in ASCII FBX")
    return np.vstack(all_vertices), np.vstack(all_faces)


def read_fbx(path):
    with open(path, "rb") as handle:
        signature = handle.read(23)
    if signature.startswith(b"Kaydara FBX Binary"):
        return _binary_mesh(path)
    return _ascii_mesh(path)
