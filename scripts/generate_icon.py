from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "src" / "cueforge" / "assets"
ICO_PATH = ASSET_DIR / "cueforge.ico"
PNG_PATH = ASSET_DIR / "cueforge.png"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    images = []
    for size in SIZES:
        source = draw_icon(size * 4)
        image = downsample(source, size, 4)
        images.append((size, write_png_bytes(image)))
    ICO_PATH.write_bytes(write_ico(images))
    PNG_PATH.write_bytes(images[-1][1])
    print(f"Wrote {ICO_PATH}")
    print(f"Wrote {PNG_PATH}")


def draw_icon(size: int) -> list[list[tuple[int, int, int, int]]]:
    image = [[(0, 0, 0, 0) for _x in range(size)] for _y in range(size)]
    rounded_gradient(image)
    border(image)
    record(image)
    tag_plate(image)
    neon_wave(image)
    cue_spark(image)
    return image


def rounded_gradient(image: list[list[tuple[int, int, int, int]]]) -> None:
    size = len(image)
    radius = size * 0.2
    margin = size * 0.035
    for y in range(size):
        for x in range(size):
            coverage = rounded_rect_coverage(x + 0.5, y + 0.5, margin, margin, size - 2 * margin, size - 2 * margin, radius)
            if coverage <= 0:
                continue
            t = (0.55 * x + 0.45 * y) / max(1, size - 1)
            pulse = 0.5 + 0.5 * math.sin((x * 1.4 + y * 0.8) / size * math.tau)
            base = mix((12, 14, 20), (30, 34, 44), t)
            glow = mix((0, 0, 0), (18, 40, 38), pulse * 0.35)
            color = clamp_rgba((base[0] + glow[0], base[1] + glow[1], base[2] + glow[2], int(255 * coverage)))
            put_pixel(image, x, y, color)


def border(image: list[list[tuple[int, int, int, int]]]) -> None:
    size = len(image)
    margin = size * 0.035
    radius = size * 0.2
    for inset, alpha in ((0.0, 150), (size * 0.016, 70)):
        x0 = margin + inset
        y0 = margin + inset
        width = size - 2 * (margin + inset)
        height = width
        thickness = max(1.0, size * 0.01)
        for y in range(size):
            for x in range(size):
                outer = rounded_rect_coverage(x + 0.5, y + 0.5, x0, y0, width, height, radius)
                inner = rounded_rect_coverage(
                    x + 0.5,
                    y + 0.5,
                    x0 + thickness,
                    y0 + thickness,
                    width - 2 * thickness,
                    height - 2 * thickness,
                    radius - thickness,
                )
                coverage = max(0.0, outer - inner)
                if coverage:
                    put_pixel(image, x, y, (72, 246, 209, int(alpha * coverage)))


def record(image: list[list[tuple[int, int, int, int]]]) -> None:
    size = len(image)
    cx = size * 0.44
    cy = size * 0.57
    radius = size * 0.32
    draw_circle(image, cx + size * 0.018, cy + size * 0.025, radius, (0, 0, 0, 90))
    draw_circle(image, cx, cy, radius, (13, 15, 22, 245))
    draw_circle(image, cx, cy, radius * 0.88, (24, 27, 35, 160))
    for scale, alpha in ((0.76, 130), (0.61, 90), (0.47, 65)):
        draw_ring(image, cx, cy, radius * scale, max(1.0, size * 0.005), (154, 178, 184, alpha))
    draw_arc(image, cx, cy, radius * 0.93, math.radians(208), math.radians(335), max(1.0, size * 0.026), (72, 246, 209, 220))
    draw_arc(image, cx, cy, radius * 0.93, math.radians(35), math.radians(106), max(1.0, size * 0.022), (255, 214, 83, 210))
    draw_circle(image, cx, cy, radius * 0.22, (46, 52, 64, 255))
    draw_circle(image, cx, cy, radius * 0.105, (9, 11, 15, 255))
    draw_circle(image, cx - radius * 0.34, cy - radius * 0.39, radius * 0.08, (255, 255, 255, 24))


def tag_plate(image: list[list[tuple[int, int, int, int]]]) -> None:
    size = len(image)
    points = [
        (size * 0.55, size * 0.19),
        (size * 0.78, size * 0.15),
        (size * 0.91, size * 0.28),
        (size * 0.86, size * 0.50),
        (size * 0.61, size * 0.54),
        (size * 0.50, size * 0.39),
    ]
    draw_polygon(image, points, (254, 176, 59, 230))
    inner = [
        (size * 0.58, size * 0.24),
        (size * 0.76, size * 0.21),
        (size * 0.85, size * 0.30),
        (size * 0.82, size * 0.45),
        (size * 0.63, size * 0.49),
        (size * 0.56, size * 0.38),
    ]
    draw_polygon(image, inner, (30, 34, 42, 255))
    draw_circle(image, size * 0.64, size * 0.31, size * 0.036, (255, 214, 83, 255))
    draw_circle(image, size * 0.64, size * 0.31, size * 0.018, (21, 24, 31, 255))
    draw_line(image, size * 0.70, size * 0.34, size * 0.81, size * 0.32, size * 0.018, (255, 214, 83, 225))
    draw_line(image, size * 0.67, size * 0.42, size * 0.78, size * 0.40, size * 0.016, (72, 246, 209, 220))


def neon_wave(image: list[list[tuple[int, int, int, int]]]) -> None:
    size = len(image)
    points = []
    for index in range(92):
        t = index / 91
        x = size * (0.16 + 0.68 * t)
        y = size * (0.69 - 0.30 * t + 0.055 * math.sin(t * math.tau * 2.5))
        points.append((x, y))
    for width, color in (
        (size * 0.068, (29, 255, 214, 45)),
        (size * 0.042, (64, 246, 209, 90)),
        (size * 0.020, (196, 255, 245, 245)),
    ):
        draw_polyline(image, points, width, color)
    for t in (0.18, 0.39, 0.61, 0.81):
        index = int(t * (len(points) - 1))
        x, y = points[index]
        draw_circle(image, x, y, size * 0.033, (255, 87, 135, 210))
        draw_circle(image, x, y, size * 0.014, (255, 245, 250, 255))


def cue_spark(image: list[list[tuple[int, int, int, int]]]) -> None:
    size = len(image)
    cx = size * 0.79
    cy = size * 0.69
    points = [
        (cx, cy - size * 0.085),
        (cx + size * 0.024, cy - size * 0.020),
        (cx + size * 0.095, cy),
        (cx + size * 0.024, cy + size * 0.020),
        (cx, cy + size * 0.085),
        (cx - size * 0.024, cy + size * 0.020),
        (cx - size * 0.095, cy),
        (cx - size * 0.024, cy - size * 0.020),
    ]
    draw_polygon(image, points, (255, 214, 83, 230))
    draw_circle(image, cx, cy, size * 0.026, (255, 255, 241, 245))


def downsample(source: list[list[tuple[int, int, int, int]]], target_size: int, factor: int) -> list[list[tuple[int, int, int, int]]]:
    target = []
    for y in range(target_size):
        row = []
        for x in range(target_size):
            accum = [0, 0, 0, 0]
            for yy in range(factor):
                for xx in range(factor):
                    pixel = source[y * factor + yy][x * factor + xx]
                    for index in range(4):
                        accum[index] += pixel[index]
            count = factor * factor
            row.append(tuple(int(round(value / count)) for value in accum))
        target.append(row)
    return target


def draw_circle(image: list[list[tuple[int, int, int, int]]], cx: float, cy: float, radius: float, color: tuple[int, int, int, int]) -> None:
    min_x = max(0, int(math.floor(cx - radius - 1)))
    max_x = min(len(image) - 1, int(math.ceil(cx + radius + 1)))
    min_y = max(0, int(math.floor(cy - radius - 1)))
    max_y = min(len(image) - 1, int(math.ceil(cy + radius + 1)))
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            distance = math.hypot(x + 0.5 - cx, y + 0.5 - cy)
            coverage = clamp(radius + 0.5 - distance)
            if coverage:
                put_pixel(image, x, y, with_alpha(color, color[3] * coverage))


def draw_ring(image: list[list[tuple[int, int, int, int]]], cx: float, cy: float, radius: float, width: float, color: tuple[int, int, int, int]) -> None:
    outer = radius + width / 2
    inner = radius - width / 2
    min_x = max(0, int(math.floor(cx - outer - 1)))
    max_x = min(len(image) - 1, int(math.ceil(cx + outer + 1)))
    min_y = max(0, int(math.floor(cy - outer - 1)))
    max_y = min(len(image) - 1, int(math.ceil(cy + outer + 1)))
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            distance = math.hypot(x + 0.5 - cx, y + 0.5 - cy)
            coverage = clamp(outer + 0.5 - distance) * clamp(distance - inner + 0.5)
            if coverage:
                put_pixel(image, x, y, with_alpha(color, color[3] * coverage))


def draw_arc(
    image: list[list[tuple[int, int, int, int]]],
    cx: float,
    cy: float,
    radius: float,
    start: float,
    end: float,
    width: float,
    color: tuple[int, int, int, int],
) -> None:
    points = []
    steps = max(8, int(abs(end - start) * radius / 5))
    for step in range(steps + 1):
        angle = start + (end - start) * step / steps
        points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    draw_polyline(image, points, width, color)


def draw_polyline(image: list[list[tuple[int, int, int, int]]], points: list[tuple[float, float]], width: float, color: tuple[int, int, int, int]) -> None:
    for start, end in zip(points, points[1:]):
        draw_line(image, start[0], start[1], end[0], end[1], width, color)


def draw_line(
    image: list[list[tuple[int, int, int, int]]],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width: float,
    color: tuple[int, int, int, int],
) -> None:
    radius = width / 2
    min_x = max(0, int(math.floor(min(x1, x2) - radius - 1)))
    max_x = min(len(image) - 1, int(math.ceil(max(x1, x2) + radius + 1)))
    min_y = max(0, int(math.floor(min(y1, y2) - radius - 1)))
    max_y = min(len(image) - 1, int(math.ceil(max(y1, y2) + radius + 1)))
    dx = x2 - x1
    dy = y2 - y1
    length_sq = dx * dx + dy * dy
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            px = x + 0.5
            py = y + 0.5
            if length_sq:
                t = clamp(((px - x1) * dx + (py - y1) * dy) / length_sq)
                nearest_x = x1 + t * dx
                nearest_y = y1 + t * dy
            else:
                nearest_x = x1
                nearest_y = y1
            distance = math.hypot(px - nearest_x, py - nearest_y)
            coverage = clamp(radius + 0.5 - distance)
            if coverage:
                put_pixel(image, x, y, with_alpha(color, color[3] * coverage))


def draw_polygon(image: list[list[tuple[int, int, int, int]]], points: list[tuple[float, float]], color: tuple[int, int, int, int]) -> None:
    min_x = max(0, int(math.floor(min(point[0] for point in points) - 1)))
    max_x = min(len(image) - 1, int(math.ceil(max(point[0] for point in points) + 1)))
    min_y = max(0, int(math.floor(min(point[1] for point in points) - 1)))
    max_y = min(len(image) - 1, int(math.ceil(max(point[1] for point in points) + 1)))
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            if point_in_polygon(x + 0.5, y + 0.5, points):
                put_pixel(image, x, y, color)


def point_in_polygon(x: float, y: float, points: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(points) - 1
    for i, point in enumerate(points):
        xi, yi = point
        xj, yj = points[j]
        if ((yi > y) != (yj > y)) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def rounded_rect_coverage(x: float, y: float, left: float, top: float, width: float, height: float, radius: float) -> float:
    right = left + width
    bottom = top + height
    if left + radius <= x <= right - radius and top <= y <= bottom:
        return 1.0
    if left <= x <= right and top + radius <= y <= bottom - radius:
        return 1.0
    corner_x = left + radius if x < left + radius else right - radius
    corner_y = top + radius if y < top + radius else bottom - radius
    if left <= x <= right and top <= y <= bottom:
        return clamp(radius + 0.5 - math.hypot(x - corner_x, y - corner_y))
    return 0.0


def put_pixel(image: list[list[tuple[int, int, int, int]]], x: int, y: int, source: tuple[int, int, int, int]) -> None:
    if source[3] <= 0:
        return
    destination = image[y][x]
    sa = source[3] / 255
    da = destination[3] / 255
    out_a = sa + da * (1 - sa)
    if out_a <= 0:
        image[y][x] = (0, 0, 0, 0)
        return
    out = []
    for index in range(3):
        value = (source[index] * sa + destination[index] * da * (1 - sa)) / out_a
        out.append(int(round(value)))
    out.append(int(round(out_a * 255)))
    image[y][x] = tuple(out)


def write_png_bytes(image: list[list[tuple[int, int, int, int]]]) -> bytes:
    height = len(image)
    width = len(image[0])
    raw = bytearray()
    for row in image:
        raw.append(0)
        for r, g, b, a in row:
            raw.extend((r, g, b, a))
    chunks = [png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))]
    chunks.append(png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
    chunks.append(png_chunk(b"IEND", b""))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def write_ico(images: list[tuple[int, bytes]]) -> bytes:
    header = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    payload = bytearray()
    for size, png in images:
        dimension = 0 if size >= 256 else size
        header.extend(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(png), offset))
        payload.extend(png)
        offset += len(png)
    return bytes(header + payload)


def mix(left: tuple[int, int, int], right: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    amount = clamp(amount)
    return tuple(int(round(left[index] + (right[index] - left[index]) * amount)) for index in range(3))


def with_alpha(color: tuple[int, int, int, int], alpha: float) -> tuple[int, int, int, int]:
    return (color[0], color[1], color[2], int(round(clamp(alpha / 255) * 255)))


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def clamp_rgba(color: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    return tuple(max(0, min(255, int(round(value)))) for value in color)


if __name__ == "__main__":
    main()
