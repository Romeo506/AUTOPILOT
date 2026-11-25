"""Create the PWA icons (app/static/icon-*.png) using only the Python standard library.
Run automatically by setup/start scripts and CI when the PNGs are missing:  python scripts/make_icons.py"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static"
BG, ACCENT, WHITE = (16, 24, 39), (124, 224, 195), (243, 246, 251)


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def png(n: int, maskable: bool) -> bytes:
    pad = n * (0.22 if maskable else 0.14)
    a, b, c = (n / 2, pad), (n - pad, n - pad * 0.9), (pad, n - pad * 0.9)
    stroke = max(2.0, n * 0.035)
    rows = []
    for y in range(n):
        row = bytearray([0])
        for x in range(n):
            d = min(seg_dist(x, y, *a, *b), seg_dist(x, y, *b, *c), seg_dist(x, y, *c, *a))
            col = BG
            if d <= stroke:
                col = ACCENT
            if n * 0.465 <= x <= n * 0.535 and n * 0.42 <= y <= n * 0.62:
                col = WHITE
            if (x - n / 2) ** 2 + (y - n * 0.735) ** 2 <= (n * 0.037) ** 2:
                col = WHITE
            row += bytes(col)
        rows.append(bytes(row))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    for name, n, maskable in (("icon-192.png", 192, False), ("icon-512.png", 512, False), ("icon-maskable-512.png", 512, True)):
        p = OUT / name
        if not p.exists():
            p.write_bytes(png(n, maskable))
            print("created", p)
