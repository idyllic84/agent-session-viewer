"""Claude JSONL projection with verified-prefix, tail-only message parsing."""
from copy import deepcopy
import hashlib
from ..models import SessionDocument, blocks
from .base import SourceAdapter, SourceFile, SourceError, read_records


def _prefix(path, size, count_lines=False):
    digest = hashlib.sha256(str(path).encode('utf-8'))
    remaining, lines, last = size, 0, b''
    with path.open('rb') as stream:
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                return None, lines, last
            digest.update(chunk)
            if count_lines:
                lines += chunk.count(b'\n')
            last = chunk[-1:]
            remaining -= len(chunk)
    return digest, lines, last


class ClaudeAdapter(SourceAdapter):
    source = 'claude'
    label = 'Claude Code'

    def discover(self):
        return (SourceFile(path, path.parent.name) for path in self.files(['**/*.jsonl'])
                if path.name != 'journal.jsonl')

    def _checkpoint(self, document):
        records = document.records
        # A valid but unterminated final row was already projected by full parse;
        # do not replay it via an append checkpoint. Fall back once it changes.
        if records.has_unterminated_value:
            document.metadata.pop('appendCheckpoint', None)
            return
        document.metadata['appendCheckpoint'] = {
            'offset': records.safe_offset, 'nextLine': records.safe_next_line,
            'prefixFingerprint': records.safe_digest.hex(), 'parserVersion': self.parser_version}

    def prime_checkpoint(self, entry, session, fingerprint):
        """Bootstrap existing v2 archives without decoding their old messages."""
        if entry.dependencies or session['metadata'].get('appendCheckpoint'):
            return None
        size = entry.path.stat().st_size
        digest, next_line, last = _prefix(entry.path, size, count_lines=True)
        if digest is None or last != b'\n' or digest.hexdigest() != fingerprint:
            return None
        metadata = deepcopy(session['metadata'])
        metadata['appendCheckpoint'] = {'offset': size, 'nextLine': next_line,
                                       'prefixFingerprint': fingerprint, 'parserVersion': self.parser_version}
        metadata.setdefault('claudeTitle', {'known': False})
        return metadata

    def parse_append(self, entry, session, primary):
        checkpoint = session['metadata'].get('appendCheckpoint')
        if (entry.dependencies or not checkpoint or primary['path'] != str(entry.path)
                or primary['parser_version'] != self.parser_version
                or checkpoint.get('parserVersion') != self.parser_version):
            return None
        offset = checkpoint['offset']
        if entry.path.stat().st_size < offset:
            return None
        digest, _, _ = _prefix(entry.path, offset)
        if digest is None or digest.hexdigest() != checkpoint['prefixFingerprint']:
            return None  # Prefix rewrite/truncation: the caller will do a full parse.
        records, warnings = read_records(entry.path, offset=offset, next_line=checkpoint['nextLine'],
                                         prefix_hasher=digest, require_newline=True)
        # Unknown old title precedence only needs a full parse if a new slug
        # could otherwise overwrite an archived custom title.
        title = session['metadata'].get('claudeTitle', {})
        if not title.get('known') and any(r.get('slug') for _, r in records):
            return None
        expected_sid = session['metadata'].get('rawSessionId')
        if expected_sid is None:
            parent = session['metadata'].get('parentSessionId')
            expected_sid = parent.split(':', 1)[1] if parent else session['externalId']
        if any(r.get('sessionId') and str(r['sessionId']) != expected_sid for _, r in records):
            return None
        document = SessionDocument(self.source, session['externalId'], session['project'], records,
                                   name=session['name'], metadata=deepcopy(session['metadata']), warnings=warnings)
        document.append_base = {'fingerprint': primary['fingerprint'], 'path': primary['path'],
                                'parserVersion': primary['parser_version'], 'messageCount': session['messageCount']}
        self._project(document)
        if not document.metadata.get('projectFromCwd', document.project != entry.project):
            cwd = next((r.get('cwd') for _, r in records if r.get('cwd')), None)
            if cwd:
                document.project = str(cwd); document.metadata['projectFromCwd'] = True
        if session['metadata'].get('metadataOnly') and not document.metadata.get('metadataOnly'):
            if not title.get('custom') and not title.get('slug'):
                document.name = ''  # First real user prompt becomes the title.
        self._titles(document)
        self._checkpoint(document)
        return document.finish()

    def parse(self, entry):
        records, warnings = read_records(entry.path)
        if not records:
            raise SourceError('No complete records yet')
        rows = [r for _, r in records]
        if not any(r.get('type') in ('user', 'assistant', 'summary', 'system', 'file-history-snapshot',
                                     'last-prompt', 'mode', 'permission-mode', 'custom-title') for r in rows):
            raise SourceError('Not a recognized Claude conversation transcript')
        raw_sid = next((r.get('sessionId') for r in rows if r.get('sessionId')), entry.path.stem)
        sid = raw_sid
        if entry.path.stem.startswith('agent-'):
            parent_sid = str(sid) if sid != entry.path.stem else (
                entry.path.parent.parent.name if 'subagents' in entry.path.parts else entry.project)
            sid = f'{parent_sid}:{entry.path.stem}'
        cwd = next((r.get('cwd') for r in rows if r.get('cwd')), None)
        document = SessionDocument(self.source, str(sid), str(cwd or entry.project), records, warnings=warnings)
        document.metadata.update(metadataOnly=True, projectFromCwd=bool(cwd), rawSessionId=str(raw_sid),
                                 claudeTitle={'known': True, 'custom': None, 'slug': None})
        if 'subagents' in entry.path.parts:
            document.metadata.update(parentSessionId=f'claude:{entry.path.parent.parent.name}', relationship='subagent')
        elif entry.path.stem.startswith('agent-'):
            parent = next((r.get('sessionId') for r in rows if r.get('sessionId')), None)
            if parent:
                document.metadata.update(parentSessionId=f'claude:{parent}', relationship='subagent')
        self._project(document)
        self._titles(document)
        self._checkpoint(document)
        return document.finish()

    @staticmethod
    def _project(document):
        for index, row in document.records:
            kind = row.get('type', 'unknown')
            native = row.get('message')
            if kind in ('user', 'assistant', 'summary'):
                document.metadata['metadataOnly'] = False
            if kind in ('user', 'assistant') and isinstance(native, dict):
                document.add(index, kind, blocks(native.get('content')), native_id=row.get('uuid'),
                             parent_id=row.get('parentUuid'), timestamp=row.get('timestamp'),
                             model=native.get('model'), usage=native.get('usage'), nativeType=kind)
            else:
                content = row.get('summary', row.get('content', ''))
                if not content and kind == 'file-history-snapshot':
                    content = list((row.get('snapshot') or {}).get('trackedFileBackups', {}))
                document.add(index, 'system', content, kind=kind, native_id=row.get('uuid'),
                             parent_id=row.get('parentUuid'), timestamp=row.get('timestamp'), nativeType=kind)

    @staticmethod
    def _titles(document):
        title = document.metadata.setdefault('claudeTitle', {'known': True, 'custom': None, 'slug': None})
        for _, row in document.records:
            if row.get('customTitle'):
                title.update(custom=str(row['customTitle']), known=True)
            if row.get('slug'):
                title['slug'] = str(row['slug'])
        if title.get('custom') or title.get('slug'):
            document.name = title.get('custom') or title['slug']
