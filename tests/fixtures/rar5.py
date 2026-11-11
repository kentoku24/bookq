"""Hand-written RAR5 *store* (uncompressed) archives for tests.

macOS ships no ``rar`` binary and the Debian one is non-free, so instead of
committing an opaque fixture we write the RAR5 container by hand. Only what
rarfile / 7zz / bsdtar need to list and extract a stored file is emitted:
signature, main header, one file header + data per entry, end header.
Format reference: https://www.rarlab.com/technote.htm

Flag support (#246): ``solid=True`` sets the archive-level SOLID flag in the
main header (``RAR5_MAIN_FLAG_SOLID``) and the per-entry solid bit
(``RAR5_COMPR_SOLID``, bit 6 of the compression-info vint) on every entry,
so ``RarFile.is_solid()`` / ``RarInfo.flags & RAR_FILE_SOLID`` report true.
``method=N`` sets the per-entry method bits (bits 7..9 of the compression
info), making ``RarInfo.compress_type`` report ``RAR_M0+N`` — the stored
data payload is still written verbatim (real encoders are proprietary), so
method-marked entries only work with a stubbed extractor.
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


def make_rar5_store(
    path: Path,
    entries: list[tuple[str, bytes]],
    *,
    solid: bool = False,
    method: int = 0,
) -> None:
    """Write *entries* (``(inner_name, data)``) as a RAR5 store archive.

    ``solid=True`` marks the archive solid (main-header flag + per-entry
    solid bit); ``method=N`` marks every entry with compression method N.
    Data is always written verbatim — flag manipulation only.
    """
    out = bytearray(b"Rar!\x1a\x07\x01\x00")
    # Main archive header: type=1, header flags=0, archive flags
    # (0x0004 = RAR5_MAIN_FLAG_SOLID).
    out += _header(_vint(1) + _vint(0) + _vint(0x0004 if solid else 0))
    for name, data in entries:
        nb = name.encode("utf-8")
        # Compression info vint: method in bits 7..9, solid bit 6 (0x40),
        # version in bits 0..5 (0 here).
        compress_info = (method << 7) | (0x40 if solid else 0)
        body = (
            _vint(2)  # header type: file
            + _vint(0x0002)  # header flags: data area present
            + _vint(len(data))  # data size
            + _vint(0x0004)  # file flags: CRC32 present
            + _vint(len(data))  # unpacked size
            + _vint(0x20)  # attributes (archive bit)
            + struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)
            + _vint(compress_info)  # compression info
            + _vint(1)  # host OS: unix
            + _vint(len(nb))
            + nb
        )
        out += _header(body) + data
    # End of archive header: type=5, header flags=0, end flags=0.
    out += _header(_vint(5) + _vint(0) + _vint(0))
    path.write_bytes(bytes(out))
