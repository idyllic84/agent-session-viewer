# Cloud Viewer / local Syncer design (proposal)

Status: design only; not implemented. This proposal preserves the existing read-only source policy, source-specific incremental parsing and sequential single-file commits.

## 1. Deployment and trust boundaries

```text
Local machine                                   Cloud
Agent history files (read-only)                  HTTPS / authenticated reverse proxy
    ↓                                             ├─ Browser authentication → Viewer / read API
Source adapters + incremental projector            └─ Device authentication → Ingest API
    ↓                                                                  ↓
SyncCoordinator → HttpArchiveRepository          Upload staging → commit worker → ArchiveRepository
    ↓                                                                  ↓
syncer-state.db (receipts / pending operations)                   viewer.db + FTS + checkpoints
```

The Syncer initiates outbound HTTPS connections only. The cloud does not connect to the machine, execute local commands or access local paths. Opening the Viewer never triggers a local source scan. Cloud read service startup performs no source discovery, policy pruning or automatic history reimport; schema/index upgrades are explicit administrative jobs.

V1 deployment: one cloud archive service, one serialized commit writer, SQLite WAL on persistent local/block storage, and private upload staging. Reads continue against the previously committed archive during upload and commit. Do not place SQLite on an object store or unverified shared network filesystem; do not run independent SQLite writers in multiple pods. A future PostgreSQL repository must preserve the same protocol semantics and separately replace SQLite search implementation.

## 2. Code separation

- `sources/*`, `incremental.py`, normalization helpers and source snapshot verification belong to a reusable Syncer library, with no direct SQL or Flask dependency.
- `SyncCoordinator` handles discovery, quiet-period checks, per-file incremental planning, network retries and progress reporting.
- `ArchiveReadService` handles Viewer queries and original-record retrieval.
- `ArchiveRepository` owns archive storage, session/message identity, counts, search and transactionality. Provide `SQLiteArchiveRepository` for local mode and `HttpArchiveRepository` for remote Syncer mode.
- Ingest handlers validate protocol objects and enqueue commit work. They do not expose general SQL, file reads or arbitrary database operations.

Do not simply put HTTP around every current `Database` method. The current service also issues direct SQL, and incremental parsing calls `load_scope_state()` / `load_scope_records()` for dirty turns/requests. Define coarse-grained operations:

1. Resolve a file replica and read one revision-consistent ingest context: session metadata, parser versions, source checkpoint, counts and primary-replica identity.
2. Read the state/raw records of a specific dirty scope at that same revision, with pagination. Never download the whole session to append one turn.
3. Submit a typed atomic change: `initialize`, `append`, `scope_delta`, or guarded `reconcile`.
4. Mark presence / finish a discovery run, independently of content checkpoints.

Separate wire protocol version, archive schema version, parser version, incremental-engine version and search-policy version. Transport only bounded JSON/JSONL and binary chunk bytes, never pickle, Python objects or SQL.

### Fingerprint change required

Current prefix fingerprints include the local path. Separate them into content-only `prefixSha256`, byte/line cursor and dependency hashes; local path belongs to replica identity/provenance. Otherwise a moved file or another device can never compare its content to the cloud checkpoint. Old fingerprints need a one-file bootstrap/reconciliation, not a complete archive rebuild.

## 3. Authentication and authorization

### Browser

Authenticate every page, read API, search API and raw-record route. Prefer OIDC/SSO, with a server-side session cookie (`Secure`, `HttpOnly`, suitable `SameSite`) and CSRF protection for browser mutations. API tokens must not be stored in browser localStorage or embedded in frontend assets.

### Syncer enrollment

1. Administrator/user creates a named device enrollment in the authenticated Viewer.
2. The Syncer completes browser/device-code enrollment. A one-use, short-lived enrollment code is bound to an account and device, not itself a permanent upload credential.
3. The Syncer obtains a short-lived access token (suggested 15 minutes) and rotating refresh credential. Store refresh credentials in the OS credential store. A simpler V1 alternative is a per-device opaque token, displayed once, stored hashed on the server, expiring and independently revocable; never one global shared secret.
4. Access claims bind `accountId`, `deviceId`, `audience=archive-ingest`, expiry and least-privilege scopes. The server derives account/device identity from the credential, never trusts those fields in a request body.
5. Syncer scopes permit writing assigned file replicas and reading only the checkpoint/scope records needed for them. No archive delete, device administration or general browsing permission.
6. Check device revocation on each request and again before a queued operation commits. Token refresh rotation must detect reuse/replay of an old refresh credential; provide re-enrollment after revocation.

The Syncer authenticates the cloud by normal verified HTTPS certificates; the cloud authenticates the Syncer by device credentials. This is not achieved by giving both sides the same API key. mTLS is optional for stronger enterprise device identity. Reject plaintext endpoints, invalid certificates and cross-origin redirects; never forward credentials to a different host.

Existing loopback `Host`/`Origin` checks and `X-Viewer-Request: 1` are not authentication and cannot protect an internet deployment. Cloud mode needs a distinct authenticated configuration with an explicit public origin and trusted-proxy policy. Retain CSRF checks for browser cookies; machine bearer-token routes use a separate authorization path.

## 4. Identities and concurrent devices

- Logical session: `(accountId, source, nativeSessionId)`, with separate lineage IDs when actual divergent histories need separate branches.
- File replica: `(accountId, deviceId, rootId, stableFileId)`; paths are provenance, not filesystem paths the cloud opens.
- Each session revision has an opaque server-generated version/ETag. A replica has a server-controlled active-writer assignment.
- The first authorized replica may initialize an absent session. Attaching a replica to existing native or legacy-imported history requires prefix/raw coverage verification; it must not silently take ownership just because the UUID matches.
- An identical copy on another device may register presence without duplicating messages. Divergent copies must not overwrite each other or be merged by modification time, message count or text similarity. Return `REPLICA_CONFLICT` and require an explicit primary switch or separate lineage.
- Local clocks and `mtime` are hints, not revision ordering or conflict resolution.

On adoption of the current cloud database, migrate its existing records and bind authorized Syncer replicas to matching sessions. Imported legacy-only sessions remain read-only archived sources until a corresponding native file can be safely attached.

## 5. Suggested API surface

All ingest routes below require device authentication and account/device ownership checks. Objects from other accounts return a non-disclosing 404.

| Endpoint | Purpose |
|---|---|
| `GET /sync/v1/capabilities` | Protocol/parser compatibility, server time, compression/chunk limits |
| `POST /sync/v1/runs` | Start a discovery run for specified source roots |
| `POST /sync/v1/files/resolve` | Resolve/adopt a replica and get a revision-consistent ingest context |
| `GET /sync/v1/files/{id}/context?revision=...` | Recover authoritative context/checkpoint |
| `GET /sync/v1/files/{id}/scopes/{scope}/state?revision=...` | Dirty scope state |
| `GET /sync/v1/files/{id}/scopes/{scope}/records?revision=...&cursor=...` | Dirty scope original records, paginated |
| `POST /sync/v1/uploads` | Create/recover a single-file operation with idempotency key |
| `PUT /sync/v1/uploads/{id}/chunks/{n}` | Upload one checked chunk idempotently |
| `GET /sync/v1/uploads/{id}` | Received chunks, seal/job status and durable commit receipt |
| `POST /sync/v1/uploads/{id}/seal` | Freeze chunk count, complete payload digest and schema |
| `POST /sync/v1/uploads/{id}/commit` | Request an atomic compare-and-swap commit |
| `DELETE /sync/v1/uploads/{id}` | Abort only an uncommitted temporary upload; never delete archive history |
| `POST /sync/v1/runs/{run}/files/{file}/seen` | Metadata-only presence / unchanged / deferred-recent status |
| `POST /sync/v1/runs/{run}/finish` | Finish complete root discovery and record availability |

Cloud Viewer keeps its authenticated read API, including 2000-message pages. A cloud “Refresh” refreshes cloud data/status; it cannot call the old local `/api/sync/start` to read files. Remote-triggered local sync, if later needed, is an explicit outbound-polled job feature, not a cloud command-execution channel.

## 6. Normal single-file flow

1. Start a run; advance the source discovery generator to exactly one file.
2. Inspect the file and workspace sidecar timestamps. If any changed within an hour, defer without reading bodies or advancing content checkpoints. A metadata-only presence notification is allowed. Skip subagent paths/headers per existing policy.
3. Resolve the file replica and read its cloud context at revision R. Local cached context is an optimization, not authority. If an earlier upload is unresolved, resolve it before making a new plan for this file.
4. Verify the committed source prefix locally. Parse only the tail for Claude/pi, dirty turns for Codex/Copilot, and dirty requests/mutations for VS Code. Fetch only needed scope state/records from revision R when absent locally. A cloud revision change during these reads invalidates the plan.
5. Construct a typed change containing session identity/provenance, base revision and fingerprint, parser/engine versions, raw records and indices, normalized message changes and references/order keys, changed scopes, replacement scope state, and the next source checkpoint. Do not trust client-provided total/user counts or searchable text.
6. Before the first upload request, durably save the operation ID, idempotency key, snapshot description and base revision to the small local state database. Keep parser version and deterministic serialization fixed for retries.
7. Create the upload; stream the current file’s change in bounded chunks. A suggested starting chunk size is 4 MiB, negotiated by capabilities. Chunks can be retried separately and split large records across byte boundaries; record parsing occurs on the reassembled logical stream. Hash transmitted bytes and the complete decompressed logical payload. Enforce both compressed and decompressed size limits.
8. Verify the source snapshot prefix/dependencies again before seal/commit and recheck the quiet period. A changed/truncated source invalidates an uncommitted plan. Freeze the payload with `seal`; sealed chunks cannot be modified.
9. Commit with `If-Match: R` and the same idempotency key. Short operations may return a receipt immediately; long operations return `202` plus a persistent operation ID. Poll status until a terminal result. `202` is not success and never advances local checkpoints.
10. The commit worker validates authorization, base revision/checkpoint, session/replica binding, references, counts, scope changes, hashes and protocol constraints. Then one archive transaction writes raw/projection changes, scope state, user/total counts, shared search text/FTS, the new checkpoint, revision R+1 and the idempotent success receipt. Commit or roll back all of them together.
11. On durable success, atomically save the receipt/new checkpoint locally. Only then is the file complete; discard its temporary transport state and advance to the next discovered file. A declared permanent per-file failure may also permit advancing, but is not success and advances no content checkpoint.
12. Finish discovery per root. Only roots whose enumeration completed successfully may mark unseen replicas unavailable. A failed, cancelled or partial run, an inaccessible root or a changed root configuration must not mark everything missing. Availability is replica-specific and aggregated across copies; history is never deleted by discovery.

Unchanged files still need a presence notification when appropriate, but no content upload. The cloud independently applies subagent exclusion, empty-session policy and user/assistant-only search policy. Nonempty zero-user sessions remain stored and hidden.

## 7. Operation state, durability and resumption

```text
CREATED → UPLOADING → SEALED → COMMITTING → COMMITTED
              ├──────────────→ ABORTED
              └──────────────→ EXPIRED
                         COMMITTING → FAILED (transaction rolled back)
```

Abort/expiry is legal only before commit ownership is acquired. A committing operation is durably recoverable and must not be garbage-collected as an abandoned upload. Aborting a committed upload cannot undo or delete the archive commit.

- Persist an operation before returning its ID. Persist chunks/lengths/digests before acknowledging receipt.
- Same operation/key + identical intent/chunk/seal returns the previous outcome; the same key/index with different contents returns a conflict. An upload key identifies one immutable source snapshot and base revision, not “the current contents of this filename.”
- The successful receipt is stored in the same transaction as archive changes. Resolve the receipt before CAS checks on a repeated commit: a retry after success still returns success, even though R is no longer current.
- Keep a lightweight committed-operation deduplication record for the archive’s lifetime; temporary-upload expiry must not remove successful operation receipts and permit old replays.
- A crash after DB commit but before HTTP acknowledgement is resolved by status lookup, not by re-appending records. A local crash before saving its receipt is handled the same way.
- Retain only bounded in-memory transport buffers and durable pending metadata on the Syncer, not a cross-file spool or a second full archive. To resume missing chunks, reproduce the exact sealed/planned snapshot from the source and revision-pinned state. If that is no longer reproducible, first resolve cloud commit status; abort only an uncommitted upload and replan. Never reuse the old key with new data.
- Suggested uncommitted upload expiry is 24 hours with renewal during progress; this is a staging policy, not a cap on how long an archive/history file may be. Commit work is persisted and recovered after cloud restarts.

Keep native source checkpoints cloud-authoritative. Losing `syncer-state.db` or reinstalling a device should permit safe context recovery after reauthentication/adoption; it must not require a new full archive upload.

## 8. Error handling

Use structured errors, for example:

```json
{
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "The committed file revision changed",
    "retryable": false,
    "requestId": "req_...",
    "operationId": "op_...",
    "currentRevision": "rev_18"
  }
}
```

`retryable=false` here means “do not blindly repeat this payload”; a new plan may still be possible. Never include raw conversation contents, secrets or internal stack traces in errors.

| Condition | Cloud behavior | Syncer behavior |
|---|---|---|
| Timeout/disconnect/ambiguous response | No assumption about commit outcome | Query operation status; retry the same operation/key or missing chunk, never invent a new append |
| 401 access expiry | Reject without writes | Refresh once, retry same request; refresh failure requires login and stops the run |
| 403 revoked/wrong scope | Reject without writes | Stop; do not repeatedly refresh or continue uploading other files |
| 404 foreign/not found object | Do not disclose another account’s data | Missing session can initialize only through normal resolve; expired upload requires status/checkpoint reconciliation; unknown IDs are not permission to overwrite |
| 409/412 revision conflict | Keep current archive/checkpoint | Resolve previous outcome, fetch fresh context and replan; repeated conflicts require primary-replica resolution |
| 409 idempotency/chunk mismatch | Reject changed payload | Treat as client integrity bug; retain diagnostics, do not retry with a random new key |
| 409 replica/source divergence | Keep existing archive | Require explicit adoption/lineage decision; no automatic last-writer-wins |
| 413 chunk too large | Advertise negotiated bounds | Abort/restart uncommitted transport with smaller chunks; do not discard a large source record or use a new key before resolving the prior operation |
| 422 invalid schema/reference/hash/scope | Reject whole operation | Fail this file, preserve checkpoint/archive, retain path/record location diagnostics; continue other files if error is file-specific |
| 426 unsupported protocol/parser | Reject before commit | Upgrade/migrate or use an explicitly advertised compatible mode; never guess an old schema |
| 429 throttled | Return Retry-After | Pause and obey delay; resume the same operation |
| 500/502/503 or SQLite busy | Roll back or leave a recoverable operation | Status lookup + bounded exponential backoff with jitter; no parallel file flood |
| Disk full/quota exceeded | Commit fails atomically; old archive remains | Pause uploads and surface a capacity alert; do not delete source/cloud history automatically |
| Cloud restart during commit | Recover persisted job/transaction/receipt | Poll after service recovery; checkpoint moves only for COMMITTED |
| Local file unreadable/malformed | No content commit | Fail this file, preserve old archive and continue; do not turn unreadability into source deletion |
| Torn final JSONL record | Accept only source-format-safe complete prefix | Never advance beyond the parser’s safe cursor; retry later |
| Prefix rewrite/truncation/parser change | Require guarded reconcile | Reparse this file only; do not silently truncate cloud history or trigger a full archive rebuild |
| Discovery/root failure | No blanket availability change | Mark root/run incomplete; old history remains browsable |

Network retry recommendation: full jitter over 1/2/4/8/16-second intervals, up to five transient retries within an explicit per-attempt recovery budget; honor Retry-After, and pause a run if the server/network is broadly unavailable. A recovery budget expiring does not cancel a possibly committed operation: persist it for status recovery. Never move to later files merely to bypass a systemic authentication/network failure.

## 9. Reconciliation and browsing consistency

Append and scope delta require both a matching revision and valid source base. Full `reconcile` is not an unrestricted replace API. Record why a rewrite is necessary, validate the source/replica identity and retain the prior revision or recoverable generation before replacing/deleting derived records. Unexpectedly shorter/divergent history defaults to a conflict candidate, not loss of the longer cloud archive. Legitimate VS Code snapshot mutations remain supported as typed scope changes; previous raw generations must remain recoverable according to an explicit retention policy. Retention/purge is an administrator operation, never part of ordinary sync.

Include `sessionRevision` in Viewer message pages. If a later numeric cursor refers to a changed revision, return `SESSION_CHANGED` and let the UI reload the session rather than splice pages from different orderings. Immutable historical page snapshots are a possible later feature; V1 need not retain every normalized projection solely for paging.

A streaming upload or failed transaction is never visible as a half-session. Readers see the old complete revision or the new complete revision.

## 10. Security, operational limits and backups

- Every lookup and foreign key/reference is account/replica scoped. Scope names and supplied IDs must never escape the authorized session.
- Treat all source content as untrusted data, including tool calls and embedded instructions. Never execute tools, fetch supplied URLs, load Python state or evaluate uploaded content. Browser rendering retains escaping, CSP and lazy raw retrieval.
- Validate lengths, nesting, record counts, compressed/uncompressed bytes, chunk counts and JSON number types. Apply per-device rate limits, account storage quotas and maximum concurrent operations. Advertise limits; reject oversized work explicitly without truncation.
- Cloud validation can prove transport integrity and schema consistency, not that a trusted device truthfully described bytes on its private disk. That stronger guarantee would require uploading original byte snapshots/server-side parsing or additional device attestation; it is not claimed by V1.
- Encrypt transport, archive/staging storage and backups. Stage privately with restrictive permissions and expire aborted chunks. Native histories may contain credentials and private paths; do not log payloads, authorization headers, refresh credentials or URL query tokens.
- Log request/run/file/operation IDs, device, phase, bytes, durations, retry category and commit revision. Emit slow-step heartbeats at five-second intervals and preserve the existing per-file start/end/result logs. Administrative audit records cover enrollment, token rotation/revocation, commits, conflicts and retention changes.
- Back up the SQLite archive with the SQLite backup mechanism, not an arbitrary copy of a live `.db` excluding WAL. Back up successful receipts, source checkpoints, device/replica bindings and schema/search versions together. Test restore. Old receipt replay after a restore must never silently append beyond a missing/rolled-back base revision; force context recovery and expose the restore generation.
- Give each restored archive an instance/generation ID. Syncers invalidate cached revisions/operation assumptions on generation change. Do not acknowledge durability stronger than the configured DB/storage/backup recovery guarantees.

## 11. Rollout plan and acceptance criteria

1. Refactor local mode behind repository boundaries without changing current source parsing or sync ordering. Existing tests must keep passing.
2. Add cloud read-only mode and browser authentication; keep internet ingress closed until auth is enforced on every history route.
3. Add per-device enrollment/authorization and revisioned ingest context/scope reads.
4. Add single-file staged uploads, persistent jobs, atomic CAS commits and operation receipts; implement `HttpArchiveRepository` and local pending-state recovery.
5. Adopt an offline copy of the current `viewer.db` in the cloud using backup/validation, preserving all 425 current sessions (including legacy-only archives). Do not bootstrap by rescanning only surviving local Agent files. Bind native replicas after coverage checks.
6. Enable bounded chunk resumption, reconciliation retention, monitoring, backup/restore and fault-injection tests before opening production sync.

Required tests: commit response lost; chunk acknowledgement lost; local/cloud process crash at every state; restart during DB commit; same operation replayed repeatedly; reused key with different data; two Syncers at the same base revision; cross-account IDs; revoked/expired tokens; invalid TLS; giant/split records; compression bombs; corrupted/missing chunks; source rewritten during upload; scope read at stale revision; parser version changes; zero-user and subagent policy; cloud disk full/SQLite busy; failed root discovery; pagination while a revision changes; repeated legacy migration; archive restore with outstanding local operations. None may duplicate records, advance a checkpoint without its data, or delete existing source/history implicitly.
