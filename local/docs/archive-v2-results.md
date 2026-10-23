# Archive v2 rebuild verification

Date: 2026-10-05. Local read-only agent-history sources; no transcript contents are included in this report.

## Result

- Rebuilt and validated all **354** previously indexed session IDs; **0 missing**, **0 failed**.
- Schema v1: **8,696,393,728 bytes** (8.10 GiB).
- Schema v2: **2,259,632,128 bytes** (2.10 GiB), a **74.0% reduction**.
- Offline full rebuild, validation and switch: **981.49 seconds** (16 min 21 sec).
  This is a first full rebuild, not an unchanged incremental reload benchmark.
- HTTP server remained stopped during rebuilding.
- The temporary schema-v1 viewer backup was removed only after independent
  session coverage, real raw-record/API/search checks and successful validation.
  The old Claude-only `claude.db` remains untouched.

## Improvements verified

1. Compressed, versioned JSON blobs preserve original record values losslessly.
2. Normalized binary/opaque values have explicit original-record markers; they are not duplicated into search text.
3. Search text is shared by exact content digest. Distinct message/session associations remain distinct.
4. Trigram FTS has no positional postings/docsize table. Three-character candidate matching plus literal substring verification preserves search semantics.
5. VS Code status records contain lightweight summaries, not entire request metadata. Tool outputs are extracted separately; stable-ID pairing or explicit unpaired status avoids guessing.
6. Changed sources are parsed once, privately spooled with authenticated compressed cache bytes, and validated against their captured byte prefixes before committing.

## Storage measurements

Logical payload byte counts (not table/page sizes):

| Payload | Bytes |
| --- | ---: |
| Compressed original JSON | 680,324,229 |
| Packed normalized messages | 485,399,684 |
| Unique searchable text, UTF-8 | 644,031,659 |
| FTS data blocks | 118,338,416 |

The remaining physical space comprises indexes, row metadata and SQLite page overhead.

**379,917** normalized messages remain distinct; **330,726** nonempty searchable message associations point to **191,039** unique search-text records. This is storage deduplication, not conversation deduplication.

## Codex correctness

The 13 historical child sessions previously projected as empty were recovered using explicit thread and turn ownership. The recovered records comprise:

- 14 user messages
- 85 assistant messages
- 820 tool calls
- 820 tool results
- 28 lifecycle/system records
- 3 summaries

Total: **1,770** records. Ordinal boundaries are still respected when no ownership proof is available; parent context is not blindly included.

## Verification

- 52 synthetic Python tests passed, including compression/raw round trips, recursive binary filtering, JSON-encoded tool output, shared-text reference cleanup, exact substring/CJK search, archive replacement safety and spool authentication.
- Frontend utility assertions and isolated real Edge smoke tests passed.
- SQLite quick check, foreign-key check and external-content FTS integrity check passed.
- Session-ID coverage matched the old archive.
- Real original records were compared to decoded compressed archive records.
- Real API message/raw retrieval and search queries were exercised without starting a persistent server.

Search probe timings on this machine (one run, cache-sensitive, not a benchmark guarantee):

| Query | Matching sessions | Seconds |
| --- | ---: | ---: |
| `authentication` | 107 | 0.839 |
| `工具` | 66 | 4.490 |
| `数据库` | 28 | 0.010 |
| `100%_done` | 1 | 0.007 |
| `quoted"text` | 1 | 0.010 |

Two-character queries intentionally fall back to literal LIKE because trigram requires three characters; they remain noticeably slower on a large corpus. Automatic watching, inherited history-base expansion, compressed Codex rollout input and arbitrary rich-block rendering remain separate follow-ups.
