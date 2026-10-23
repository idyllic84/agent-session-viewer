"""Versioned, lossless JSON blobs. Large raw records are encoded incrementally."""
import io
import json
import zlib

_ENCODER = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'))


def pack_json(value, *, streaming=False):
    if not streaming:
        raw = _ENCODER.encode(value).encode('utf-8')
        if len(raw) < 1024:
            return b'J' + raw
        compressed = zlib.compress(raw, level=1)
        return b'Z' + compressed if len(compressed) < len(raw) else b'J' + raw
    compressor = zlib.compressobj(level=1)
    output = io.BytesIO()
    output.write(b'Z')
    pending = bytearray()
    for chunk in _ENCODER.iterencode(value):
        pending.extend(chunk.encode('utf-8'))
        if len(pending) >= 64 * 1024:
            output.write(compressor.compress(pending))
            pending.clear()
    if pending:
        output.write(compressor.compress(pending))
    output.write(compressor.flush())
    return output.getvalue()


def unpack_json(blob):
    if not isinstance(blob, bytes) or not blob:
        raise ValueError('Invalid archive JSON blob')
    if blob[:1] == b'Z':
        payload = zlib.decompress(blob[1:])
    elif blob[:1] == b'J':
        payload = blob[1:]
    else:
        raise ValueError('Unknown archive JSON codec')
    return json.loads(payload.decode('utf-8'))
