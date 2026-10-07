"""
Shared, cached DXF parsing.
===========================

Dropping a DXF with "Build clean plan on import" on (the default) parses the
same bytes twice, moments apart: once in `dxf_bridge` for the raw import,
once in `fs_clean` for the clean-plan build. Both call `ezdxf.read`
independently. ezdxf's own tag parse is the single most expensive step in
either path -- minutes, on a large real file -- so paying for it twice on
one drop was pure waste, doubling the wait before the clean build had even
started its own (also expensive) work.

`read_document` caches its last result by the bytes' own content, not by
who is asking, so either caller can ask first without knowing or caring
whether the other one already parsed this exact file. It holds one entry:
nothing here is asked to remember more than the file just dropped, and a
second drop with different bytes is a plain cache miss, not a stale result.
"""

from __future__ import annotations

import io

import ezdxf

# A DWG's own six-byte signature: AC1015 is AutoCAD 2000, through AC1032 for
# 2018. A DXF opens with a group code, so the two are never confusable.
_DWG_MAGIC = b"AC10"

_cache_raw: bytes | None = None
_cache_doc = None


def check_not_dwg(raw: bytes) -> None:
    """Refuse a DWG before ezdxf tries to read it as text.

    ezdxf reads DXF only. Handed a DWG it fails deep inside the tagger with
    `Invalid group code "AC1032<binary>" at line 1`, and the message carries
    a screenful of the binary with it. Nobody can act on that. Checking the
    signature here catches the renamed case too, which an extension check in
    the page cannot.
    """
    head = bytes(raw[:6])
    if head.startswith(_DWG_MAGIC) and head[4:6].isalnum():
        raise ValueError(
            "This is a DWG file, not a DXF. The offline engine reads DXF "
            "only, because it has no converter in the browser. Open the "
            "drawing in CAD and save a copy as DXF, then drop that in.")


def read_document(dxf_bytes):
    """Parse DXF bytes into an ezdxf Document.

    Reads through a text stream with newlines normalised. A DWG-to-DXF
    conversion on Windows leaves CRLF endings, and feeding those to a strict
    binary-tag parser fails on group code 310 with "Invalid binary data".

    A second call with the same bytes returns the cached Document instead of
    parsing again. `dxf_bytes` may also be a plain `str` (some callers build
    a DXF body directly, e.g. in tests); that path is never cached, since it
    is not the large-file, parsed-twice-per-drop case this exists for.

    The returned Document is shared: two callers asking for the same bytes
    get the same object, not a copy each. Every current caller only reads
    it (walks entities, measures, converts to another shape) -- neither
    writes a layer, an attribute, or anything else into it. Keep it that
    way; a future caller that mutates this Document would leak that edit
    into whatever else asked for the same bytes.
    """
    global _cache_raw, _cache_doc

    if isinstance(dxf_bytes, (bytes, bytearray, memoryview)):
        raw = bytes(dxf_bytes)
        if _cache_raw is not None and raw == _cache_raw:
            return _cache_doc
        check_not_dwg(raw)
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
    else:
        raw = None
        text = str(dxf_bytes)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    doc = ezdxf.read(io.StringIO(text))

    if raw is not None:
        _cache_raw, _cache_doc = raw, doc
    return doc
