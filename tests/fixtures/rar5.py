"""Hand-written RAR5 *store* (uncompressed) archives for tests.

macOS ships no ``rar`` binary and the Debian one is non-free, so instead of
committing an opaque fixture we write the RAR5 container by hand. Only what
rarfile / 7zz / bsdtar need to list and extract a stored file is emitted:
signature, main header, one file header + data per entry, end header.
Format reference: https://www.rarlab.com/technote.htm
"""

import struct
import zlib
from pathlib import Path


def _vint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _header(body: bytes) -> bytes:
    size = _vint(len(body))
    crc = zlib.crc32(size + body) & 0xFFFFFFFF
    return struct.pack("<I", crc) + size + body


def make_rar5_store(path: Path, entries: list[tuple[str, bytes]]) -> None:
    """Write *entries* (``(inner_name, data)``) as a non-solid RAR5 store archive."""
    out = bytearray(b"Rar!\x1a\x07\x01\x00")
    # Main archive header: type=1, header flags=0, archive flags=0.
    out += _header(_vint(1) + _vint(0) + _vint(0))
    for name, data in entries:
        nb = name.encode("utf-8")
        body = (
            _vint(2)  # header type: file
            + _vint(0x0002)  # header flags: data area present
            + _vint(len(data))  # data size
            + _vint(0x0004)  # file flags: CRC32 present
            + _vint(len(data))  # unpacked size
            + _vint(0x20)  # attributes (archive bit)
            + struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)
            + _vint(0)  # compression info: version 0, method store
            + _vint(1)  # host OS: unix
            + _vint(len(nb))
            + nb
        )
        out += _header(body) + data
    # End of archive header: type=5, header flags=0, end flags=0.
    out += _header(_vint(5) + _vint(0) + _vint(0))
    path.write_bytes(bytes(out))
