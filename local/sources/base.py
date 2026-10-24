"""Read-only source adapters. No adapter has a source-delete operation."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path


class SourceError(ValueError):
    pass


class _PrefixHashes:
    """Process-local RAM cache of verified prefix hashes for large append-only files.

    A big JSONL file is imported in many bounded commits; re-hashing its whole
    prefix for every commit is quadratic. An entry is reused only while the
    file's size AND modification time are exactly unchanged since it was
    computed, so any write (append or rewrite) forces a full re-verification.
    Nothing is persisted.
    """
    def __init__(self, limit=16):
        self.limit = limit; self.entries = {}

    @staticmethod
    def identity(path):
        status = os.stat(os.fspath(path))
        return status.st_size, status.st_mtime_ns

    def hasher(self, path, length):
        key = os.fspath(path); identity = self.identity(key); cached = self.entries.get(key)
        points = cached[1] if cached and cached[0] == identity else {}
        usable = [offset for offset in points if offset <= length]
        if usable:
            position = max(usable); digest = points[position].copy()
        else:
            digest = hashlib.sha256(str(path).encode('utf-8')); position = 0
        remaining = length - position
        with open(key, 'rb') as stream:
            stream.seek(position)
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    return None
                digest.update(chunk); remaining -= len(chunk)
        if self.identity(key) == identity:
            points = dict(points); points[length] = digest.copy()
            for offset in sorted(points)[:-16]:
                points.pop(offset)
            self.entries.pop(key, None); self.entries[key] = (identity, points)
            while len(self.entries) > self.limit:
                self.entries.pop(next(iter(self.entries)))
        return digest

    def fingerprint(self, paths, compute):
        key = tuple(os.fspath(p) for p in paths)
        try:
            identity = tuple(self.identity(p) if Path(p).exists() else None for p in key)
        except OSError:
            return compute()
        cached = self.entries.get(key)
        if cached and cached[0] == identity:
            return cached[1]
        value = compute()
        self.entries.pop(key, None); self.entries[key] = (identity, value)
        while len(self.entries) > self.limit:
            self.entries.pop(next(iter(self.entries)))
        return value


PREFIX_HASHES = _PrefixHashes()


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
        return PREFIX_HASHES.fingerprint((self.path, *self.dependencies), self._fingerprint)

    def _fingerprint(self):
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
        actual = PREFIX_HASHES.hasher(self.path, records.byte_count)
        if actual is None:
            raise SourceError('Source snapshot was truncated; retry sync')
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
