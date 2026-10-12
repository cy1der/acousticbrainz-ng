import hashlib
import struct
import zlib
from typing import List


def fingerprint_png(values: List[float]) -> bytes:
    # 95 values plus a 5 value checksum, rendered as an inverted 10x10 grayscale image
    clipped = [min(max(value, 0.0), 1.0) for value in values]

    buf = hashlib.sha256(struct.pack(f"<{len(clipped)}H", *(int(value * 65535) for value in clipped))).digest()
    while len(buf) < 5 * 4:
        buf += hashlib.sha256(buf).digest()
    checksum = [struct.unpack(">I", buf[i * 4:i * 4 + 4])[0] / 0xFFFFFFFF for i in range(5)]

    pixels = bytes(255 - round(value * 255) for value in clipped + checksum)
    width = height = 10

    def _chunk(c_type, data):
        chunk = struct.pack(">I", len(data)) + c_type + data
        crc = zlib.crc32(c_type + data) & 0xFFFFFFFF
        return chunk + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB",
                       width, height,
                       8,    # bit depth
                       0,    # color type = 0 (grayscale)
                       0,    # compression
                       0,    # filter
                       0)    # interlace
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(pixels[y * width:(y + 1) * width])

    return (
        b'\x89PNG\r\n\x1a\n'
        + _chunk(b'IHDR', ihdr)
        + _chunk(b'IDAT', zlib.compress(bytes(raw), level=9))
        + _chunk(b'IEND', b'')
    )
