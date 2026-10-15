# Agent History Viewer (ClaudeViewer)

A local, **read-only** web viewer for coding-agent history. Browse Claude Code,
Codex, GitHub Copilot CLI, pi, and VS Code Chat in the existing three-panel UI.
No model API, agent CLI, cloud account, or frontend build is required.

## Quick start

Python **3.10+**:

```bash
pip install -r requirements.txt
python server.py
```

The server **automatically syncs local history before listening**, as the original
ClaudeViewer did. Open **http://localhost:8000** after the startup sync completes.
Files are handled strictly one at a time: discover a path, look up its archived
session/checkpoint, read new content, commit data and checkpoint, then ask the
discovery iterator for the next path. There is no batching, file-path prefetch,
full-corpus scan/import barrier or temporary projection spool.
Every file prints a start line and a final status/duration, including unchanged,
recently skipped, duplicate and failed files. Expensive steps print a heartbeat
every 5 seconds with current file, step and elapsed time; no transcript content
is logged. This applies to startup sync as well as Reload.

All sources reuse unchanged projections and have incremental update paths:
Claude/pi append entries, Codex/Copilot update affected turns, and VS Code replays
new operations and updates only affected requests. A first import/checkpoint
initialization can still take a while; completed per-session transactions remain
available even if a later file fails.

By default, files modified **less than one hour ago are skipped**, before hashing
or reading their bodies. Existing indexed history remains visible and its sync
checkpoint is not advanced. Once a file has been quiet for an hour, the next sync
resumes from that checkpoint. A recently changed workspace sidecar also defers
the associated transcript. Files changed during processing are likewise deferred;
`skippedRecent` is reported separately from unchanged files and failures.

**Reload** starts a background job and displays live phase, counts, current file
and elapsed time. Refreshing the page reconnects to that job instead of starting
another one. Already committed history remains browsable during a reload.

```bash
python server.py --port 8001  # choose another port if 8000 is occupied
python server.py --no-sync    # explicitly skip startup sync and browse the existing archive
python server.py --debug      # development mode, without the double-import reloader
python server.py --include-recent  # explicit override of the one-hour quiet period
```

The current archive is **`viewer.db` (schema v2)**. Your existing **`claude.db` is
preserved and is not automatically migrated. Use the explicit legacy migration
below to recover sessions that survive only in that database. An older schema-v1
`viewer.db` requires the explicit rebuild below; it is never silently overwritten.
Agent history files are never modified, and
all source deletion endpoints are disabled, including Claude deletion.
The UI reports import errors without discarding the last successful projection.
Active JSONL files may keep appending: the importer validates the exact byte prefix
it read, commits that snapshot, and picks up later appended records on the next
sync. Rewrites or truncation of that prefix are still rejected.

## Migrating the old Claude-only archive

Stop the viewer, then run:

```bash
python migrate_legacy.py
# Optional trusted local paths:
python migrate_legacy.py --source /path/to/claude.db --database /path/to/viewer.db
```

The migration reads the old database in read-only mode and processes one session
at a time. Missing non-subagent sessions are inserted into a staged copy of the
viewer archive. Existing sessions are never overwritten: every legacy raw value
must already be covered by an existing native projection, otherwise validation
fails rather than silently losing an older branch. Truly empty/subagent sessions
are excluded; nonempty zero-user sessions are preserved but remain hidden.

Legacy-only sessions retain all original records, record ordinals, old timestamp
column values and provenance (`metadata.legacyArchive`). They are marked source
unavailable, without inventing a JSONL file watermark or append checkpoint.
They remain searchable/browsable; normal sync retains them and can reconcile
against the original transcript if it reappears. Search still indexes only
user/assistant prose, not system/tool payloads.

Before switching, the command verifies all eligible legacy session IDs/raw
records, byte-for-byte preservation of existing histories/checkpoints, SQLite,
foreign keys, FTS, and the source database SHA-256. It refuses a running viewer or
concurrent destination changes. Failure retains the original archive and the
candidate; success retains `viewer.before-legacy-<timestamp>.db` as rollback
backup and writes `viewer-legacy-migration-result.log`. Neither Agent files nor
`claude.db` are modified. Repeating the migration does not duplicate sessions.

A later rebuild from current source files alone cannot recover these legacy-only
sessions; the normal missing-session safety check will refuse to discard them.
Keep `claude.db` and/or the migration backup.

## Rebuilding an older viewer index

Stop the viewer, then run:

```bash
python rebuild.py
```

The command builds a separate v2 archive, checks SQLite/foreign keys/search-index
integrity and existing session-ID coverage, then switches the database. The old
viewer index is retained as `viewer.backup-<timestamp>.db` for rollback. Agent
files and the old Claude-only database are untouched. Failed builds keep the
original database and the candidate for inspection.

```bash
python rebuild.py --discard-backup  # delete the old viewer index only after successful validation/switch
```

If source files were deliberately removed, inspect missing sessions before using
`--allow-missing`. Rebuilding from current sources cannot recover deleted source
history; do not discard a backup that is your only copy.

## Storage and search

- Raw records and normalized messages use versioned, lossless JSON blobs with
  zlib compression; decoding is transparent to the API.
- Identical search text is stored/indexed once, with separate message/session
  associations. This is **storage deduplication**, not deletion/merging of
  repeated questions, turns, forks or sessions.
- FTS5 trigram uses `detail=none, columnsize=0` to avoid positional-posting bloat.
  Queries select candidates by three-character grams and verify the exact literal
  substring. Short queries/SQLite builds without trigram fall back to literal LIKE.
- Binary attachments, Base64 data, opaque reasoning/signatures and embedded data
  URIs are retained in raw records, but replaced by explicit references/markers in
  normalized content and excluded from search. Structured JSON embedded in tool
  output is inspected for typed binary/opaque fields as well.
- VS Code result metadata is summarized (status, timing, usage, error). Tool output
  is extracted once into tool-result records, paired only by stable call IDs.
  Unpaired outputs remain separate and explicitly marked; the full original
  metadata/rounds/rendered context stays in the raw archive.
- VS Code markdown and inline-reference chips are concatenated in their original
  order into assistant prose, preserving spaces/newlines and symbol labels.
  Reference targets and contributing `chunkIndices` remain in normalized metadata.
  Text is not merged across tools/thinking events or request boundaries; unresolved
  references get an explicit placeholder/warning rather than disappearing.
  Parser v3 automatically refreshes older VS Code projections on the next sync;
  a database/schema rebuild is not needed for this correction.
- Claude Code has a source-specific byte/physical-line checkpoint. Appends parse
  only the tail and insert only new rows/text mappings; existing raw blobs and
  message IDs are not rewritten. Truncation, rewritten history, changed parser
  rules or inconsistent archive sequences fall back to full reconciliation.
  Existing complete Claude archives acquire checkpoints without re-decoding old
  JSON. A torn/newline-incomplete tail is deferred rather than imported twice.
  Old bytes are still hashed to verify the prefix; this is incremental parsing
  and indexing, not a promise of zero reads of existing file bytes.
- pi restores persisted parent/model/provider context and projects only new
  entries, preserving saved branches, labels and session renames. Context edits
  remain raw history events; they do not erase the original conversation.
- Codex and Copilot CLI append raw records and reconcile only dirty native
  turns/interactions. Late tool completion can update its older call/turn without
  decoding/reprojecting unrelated closed turns. Canonical-format transitions may
  legitimately affect additional units and are reconciled accordingly.
- VS Code JSONL resumes at its verified byte/line cursor, restores only changed
  request objects, and applies new Initial/Set/Push/Delete operations. Request
  truncation/deletion and text/inline-reference regrouping update only affected
  request projections. Plain JSON snapshots must be read as a whole because they
  are not append logs, but request digests avoid normalizing/indexing unchanged
  requests. Untouched message IDs and compressed payloads remain unchanged.
- Parser state/request checkpoints and message/raw scope associations are stored
  in additive schema-v2 tables, in the same transaction as projection/search and
  file watermarks. Existing archives do not require rebuilding. A legacy session
  without these checkpoints is initialized once when it next changes; there is
  no forced reimport of every unchanged session just to install the new tables.
  Rewrites, truncation, identity/parser changes or invalid checkpoints trigger
  explicit single-file reconciliation, not an automatic whole-archive rebuild.
- File discovery is lazy and strictly sequential: a file is committed, skipped or
  reported failed before the iterator advances to the next file. Duplicate copies
  are ranked against the current primary (newest nonempty wins), including copies
  discovered later. Failed newer copies do not demote
  a last-good archive to an older copy. Empty copies do not erase saved history.

## Supported sources

| Source | Default location | Notes |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` | Main sessions only; agent/subagent transcripts are excluded |
| Codex | `$CODEX_HOME/{sessions,archived_sessions}/**/*.jsonl` (`~/.codex` by default) | Legacy/paginated rollouts; explicit thread/turn ownership preserves historical subagent work even before migrated context boundaries |
| Copilot CLI | `~/.copilot/session-state/**/events.jsonl` and legacy flat JSONL | Conversation and tool lifecycle events; model snapshots are not duplicated as chat |
| pi | `~/.pi/agent/sessions/**/*.jsonl` | v1–v3, typed content, tree parent IDs and saved-branch view |
| VS Code Chat | Platform `Code/User` and `Code - Insiders/User` | Workspace JSON/JSONL and global empty-window sessions; official mutation-log replay |

On Windows, VS Code defaults to `%APPDATA%/Code/User`; on macOS,
`~/Library/Application Support/Code/User`; on Linux, `~/.config/Code/User`.
`PI_CODING_AGENT_DIR` and `PI_CODING_AGENT_SESSION_DIR` are respected.

### Custom roots

`VIEWER_SOURCE_ROOTS` is a JSON object mapping source IDs to arrays of paths.
Omitted sources keep their defaults. An empty array disables a source. A root
can be a directory or an explicitly selected transcript file. VS Code directory
roots should point to the editor's **User** directory; roots are trusted local
configuration and are not accepted from HTTP requests.

PowerShell:

```powershell
$env:VIEWER_SOURCE_ROOTS = '{"pi":["D:/History/pi"],"codex":["D:/History/codex/sessions"],"vscode_chat":[]}'
$env:VIEWER_DB = 'D:/History/viewer.db'
python server.py
```

Bash:

```bash
export VIEWER_SOURCE_ROOTS='{"pi":["/mnt/history/pi"],"vscode_chat":[]}'
export VIEWER_DB=/mnt/history/viewer.db
python server.py
```

Do **not** point `VIEWER_DB` at any agent-owned database. Recognized legacy
archive schemas are refused instead of migrated.

## Browsing

- Source/project filters, session names, modification times and availability.
- The left list shows **user messages / total messages**, for example `3 / 28`.
  Sessions with **zero user messages are hidden**, even if they have assistant,
  tool or system records. Nonempty zero-user history is retained and can resume
  incrementally/become visible when a real user message arrives. Sync uses full
  raw/message positions and logs total-record changes plus user counts separately.
- Truly empty (0 total records) sessions and subagent sessions are not listed or imported.
  Existing empty/subagent viewer indexes are cleaned at initialization/sync;
  original agent files are never removed. Empty-file fingerprints are retained
  to avoid repeatedly parsing/recreating an empty session. Normal forks are kept.
- User/assistant text is shown by default; enable tool, thinking, summary and
  system filters to inspect other records. A message containing text and tools
  retains both instead of losing one content type.
- Cross-session search covers names, projects, native session IDs and readable
  **user/assistant prose only** (including clear assistant thinking and inline
  reference labels). System events, tool-call names/arguments, tool results and
  raw metadata do not contribute to the session search index. These records are
  still preserved and browsable; only the search scope is narrowed.
  On the first startup with this policy, search text/mappings/FTS are rebuilt in
  one transaction with progress logs; message bodies, raw records and sync
  checkpoints are not reimported or changed. Later startups reuse the policy marker.
  Typing searches after a short debounce; Enter and the Search button also work.
  A changed query clears stale project selection, active source/project scope and
  result count are shown, and one click searches all sources/projects. The middle
  find box is explicitly limited to the current session's loaded messages.
  Position-free FTS5 trigram substring search is used when SQLite supports it;
  short queries and installations without that tokenizer use escaped literal `LIKE`, including
  two-character Chinese queries.
- Messages load **2000 records per page**. Use **Load next 2000 records** to load
  more. The in-conversation search explicitly searches **loaded messages only**.
- pi can switch between all physical records and the current saved branch.
- Details show normalized content/metadata; **Load original record** lazily
  retrieves the unmodified JSON and tool-related records.
- Missing source files do not delete the archive; sessions are marked unavailable.

## Current limits

This is the first multi-source implementation, not a complete replacement for
all agents' native history tools:

- Original records are retained losslessly, but some source-specific rich blocks
  display as structured JSON. Images are not rendered and opaque reasoning is not
  decrypted/indexed. Large VS Code status/error fields are summarized in the UI;
  their complete originals remain in raw records.
- Codex `history_base` inherited prefixes are **not expanded**. Such sessions
  show a warning. Paginated completed items are preferred for visible messages;
  raw response/context records remain accessible in the raw-record API. An
  unfinished paginated turn may only exist in raw records until completion.
- Raw Codex `.jsonl.zst` compression is not supported yet.
- Copilot `session-store.db` and Codex `state_*.sqlite` are not yet used for
  metadata enrichment or missing-transcript recovery.
- VS Code CLI/IDE records are not automatically merged across sources. Duplicate
  storage copies within VS Code are grouped by native session ID; the newest
  nonempty copy wins. Source records for a VS Code message are state operations,
  not necessarily a standalone chat message; `sourceLocator` identifies the
  reconstructed request/chunk. The full operation log is paginated via API.
- Project paths are displayed as recorded; worktree aliases/path-case mapping,
  branch pickers for arbitrary pi leaves, watcher/SSE and export are follow-ups.
- Synchronization runs automatically before startup and on Reload, not through a
  continuous watcher. Per-file content hashes detect rewrites, including
  equal-size/equal-mtime edits. Prefix hashing still reads existing bytes for
  integrity checks; incremental parsing does not mean zero disk reads. Only new
  raw JSONL records and affected projection units are decoded/processed on the
  incremental paths. Restoring a large VS Code request checkpoint can still cost
  time, but unrelated requests and the old operation log are not replayed.
- Schema v2 is independent of the old Claude-only database. V1 viewer archives
  require explicit rebuilding; future schema changes likewise must not silently
  overwrite existing archives.

## API

```text
GET  /api/sources
GET  /api/sessions?source=&project=&q=&cursor=0&limit=100
GET  /api/sessions/<id>/messages?cursor=0&limit=2000&branch=active
GET  /api/messages/<message-id>/raw
GET  /api/sessions/<id>/records?cursor=0&limit=50
POST /api/sync/start   # returns 202 immediately; attaches to an existing job if busy
GET  /api/sync/status  # phase, counts, current file, elapsed time, final result
POST /api/sync        # blocking compatibility endpoint for scripts
```

Pages return `items`, `total`, and `nextCursor` (`null` at the end). Message
pages also include session metadata. IDs include the source namespace.
Timestamps carry `timestampUnit: "ms"`. Session objects expose both
`messageCount` (all normalized records, used for synchronization/paging) and
`userMessageCount` (actual user inputs, used by the left list).

Completed sync results contain `added`, `updated`, `unchanged`, `failed`, `errors`,
`warnings`, `skippedRecent`, `skippedEmpty`, `skippedSubagents`,
`removedEmpty`, `removedSubagents`, `appendedSessions`, `appendedMessages`,
`incrementalSessions`, and `reprojectedScopes`. Incremental sessions also count as `updated`; scope counts
identify dirty turns/requests rather than implying full-history reprocessing.
The asynchronous start endpoint returns a status snapshot; poll `/api/sync/status`
until `running` is false. Progress reports processed files, synced logical sessions,
recent-file skips and the current file. The discovered total is
not a claimed whole-corpus percentage while lazy discovery is still running.
Concurrent async requests share the current job.
Mutation requests require **`X-Viewer-Request: 1`** and, if present, a same-origin
`Origin` header. No CORS is enabled. Host headers are restricted to loopback;
trusted proxy hostnames can be explicitly added with comma-separated
`VIEWER_ALLOWED_HOSTS`. This is not authentication: **do not expose the server
to an untrusted network**.

Legacy `/sessions`, `/sessions/search`, `/sessions/sync`, and `/session/<id>`
read routes remain available, but the old message endpoint is bounded to 500
records. New clients should use the paginated API. Source DELETE returns 405.

## Development and tests

```bash
python -m unittest discover -s tests -v
node --experimental-default-type=module tests/test_utils.mjs
node --experimental-default-type=module tests/test_browser.mjs  # optional: Node 22+ and Edge/Chromium
```

Tests use synthetic transcripts and temporary databases only. They cover
source identity, idempotence, atomic rollback, torn/repaired lines, content
rewrites, raw preservation, pi trees, Codex completed-item/tool deduplication,
VS Code truncation/deep mutations, substring/CJK search, pagination and local
HTTP protections. No agent binaries or user history are used by tests. The optional browser test
launches an isolated browser profile and a synthetic local fixture server, then
checks source filters, mixed blocks, raw records, paging, saved branches,
escaping and Reload. Set `EDGE_BIN` when the browser is not at the default
Windows Edge path.

See [`docs/multi-agent-history-research.md`](docs/multi-agent-history-research.md)
for the research, reference projects and planned follow-up work.
