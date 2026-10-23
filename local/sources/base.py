"""Read-only source adapters. No adapter has a source-delete operation."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path


class SourceError(ValueError):
    pass


class SnapshotRecords(list):
    """Parsed records plus the hash of the exact byte prefix that produced them."""
    def __init__(self, path):
        super().__init__()
        self.hasher = hashlib.sha256(str(path).encode('utf-8'))
        self.byte_count = 0
        self.modified_ns = None
        self.dependencies = {}
        self.record_sizes = {}
        self.safe_offset = 0
        self.safe_next_line = 0
        self.safe_digest = self.hasher.digest()
        self.has_unterminated_value = False

    def __getstate__(self):
        state = dict(self.__dict__)
        hasher = state.pop('hasher', None)
        if hasher is not None:
            state['snapshot_digest'] = hasher.digest()
        return state

    @classmethod
    def from_json(cls, path, value):
        records = cls(path)
        records.append((0, value))
        records.hasher = value.snapshot_hasher
        records.byte_count = value.snapshot_byte_count
        records.modified_ns = value.snapshot_modified_ns
        records.record_sizes[0] = value.snapshot_byte_count
        return records


class SnapshotObject(dict):
    def __getstate__(self):
        state = dict(self.__dict__)
        state.pop('snapshot_hasher', None)
        return state


@dataclass(frozen=True)
class SourceFile:
    path: Path
    project: str = ''
    dependencies: tuple = ()

    def fingerprint(self):
        digest = hashlib.sha256()
        for path in (self.path, *self.dependencies):
            digest.update(str(path).encode('utf-8'))
            if not path.exists():
                digest.update(b'<missing>')
                continue
            before = path.stat()
            remaining = before.st_size
            with path.open('rb') as stream:
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise SourceError('File truncated while fingerprinting; retry sync')
                    digest.update(chunk)
                    remaining -= len(chunk)
        return digest.hexdigest()

    def snapshot_fingerprint(self, records):
        """Allow append after a read, but reject rewrites/truncation of that read's prefix."""
        if not isinstance(records, SnapshotRecords):
            raise SourceError('Adapter did not provide snapshot provenance')
        actual = hashlib.sha256(str(self.path).encode('utf-8'))
        remaining = records.byte_count
        with self.path.open('rb') as stream:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise SourceError('Source snapshot was truncated; retry sync')
                actual.update(chunk)
                remaining -= len(chunk)
        expected = getattr(records, 'snapshot_digest', None)
        if expected is None:
            expected = records.hasher.digest()
        if actual.digest() != expected:
            raise SourceError('Source snapshot prefix was rewritten; retry sync')
        digest = actual.copy()
        for path in self.dependencies:
            expected = records.dependencies.get(str(path))
            if expected is None or path.read_bytes() != expected:
                raise SourceError('Source metadata changed during parsing; retry sync')
            digest.update(str(path).encode('utf-8'))
            digest.update(expected)
        return digest.hexdigest()


def read_records(path, *, offset=0, next_line=0, prefix_hasher=None, require_newline=False):
    """Read full JSONL or just its tail, retaining absolute line IDs and provenance."""
    records, warnings = SnapshotRecords(path), []
    if offset:
        if prefix_hasher is None:
            raise SourceError('Tail reads require a verified prefix hash')
        records.hasher = prefix_hasher.copy()
        records.byte_count = offset
        records.safe_offset = offset
        records.safe_next_line = next_line
        records.safe_digest = prefix_hasher.digest()
    with path.open('rb') as stream:
        stream.seek(offset)
        for index, line in enumerate(stream, start=next_line):
            records.hasher.update(line)
            records.byte_count += len(line)
            records.record_sizes[index] = len(line)
            if require_newline and not line.endswith(b'\n'):
                warnings.append(f'Trailing record at line {index + 1} is not newline-terminated; deferred until next sync')
                break
            if not line.strip():
                if line.endswith(b'\n'):
                    records.safe_offset = records.byte_count
                    records.safe_next_line = index + 1
                    records.safe_digest = records.hasher.digest()
                continue
            try:
                value = json.loads(line.decode('utf-8-sig'))
            except (ValueError, UnicodeError) as exc:
                if not line.endswith(b'\n'):
                    warnings.append(f'Incomplete trailing record at line {index + 1}; retry on next sync')
                    break
                raise SourceError(f'Invalid JSON at line {index + 1}') from exc
            if not isinstance(value, dict):
                raise SourceError(f'Expected an object at line {index + 1}')
            records.append((index, value))
            if line.endswith(b'\n'):
                records.safe_offset = records.byte_count
                records.safe_next_line = index + 1
                records.safe_digest = records.hasher.digest()
            else:
                records.has_unterminated_value = True
    records.modified_ns = path.stat().st_mtime_ns
    return records, warnings


def read_json(path, keep_bytes=False):
    payload = path.read_bytes()
    modified = path.stat().st_mtime_ns
    value = json.loads(payload.decode('utf-8-sig'))
    if not isinstance(value, dict):
        raise SourceError('Expected a JSON object')
    value = SnapshotObject(value)
    value.snapshot_hasher = hashlib.sha256(str(path).encode('utf-8') + payload)
    value.snapshot_byte_count = len(payload)
    value.snapshot_modified_ns = modified
    value.snapshot_bytes = payload if keep_bytes else None
    return value


class SourceAdapter:
    source = ''
    label = ''
    parser_version = 2

    def __init__(self, roots):
        self.roots = [Path(os.path.expandvars(str(root))).expanduser().resolve() for root in roots]

    def files(self, patterns):
        seen = set()
        for root in self.roots:
            if not root.exists():
                continue
            for pattern in patterns:
                candidates = [root] if root.is_file() else root.glob(pattern)
                for candidate in candidates:
                    if not candidate.is_file():
                        continue
                    resolved = candidate.resolve()
                    allowed = root.parent if root.is_file() else root
                    if not resolved.is_relative_to(allowed):
                        continue  # Never follow links outside an explicitly configured root.
                    if resolved not in seen:
                        seen.add(resolved)
                        yield resolved

    def discover(self):
        return (SourceFile(path) for path in self.files(['**/*.jsonl']))

    def info(self):
        return {'id': self.source, 'label': self.label,
                'roots': [str(p) for p in self.roots],
                'available': any(p.exists() for p in self.roots),
                'capabilities': {'sourceDelete': False, 'readOnly': True}}
