# Corpus Intel — Multi-User Architecture (Proposed)

Status: **draft for discussion**. Not scheduled. Written 2026-04-20 after Opus 4.7 audit + architecture conversation.

## 1. Context

Corpus Intel today is a single-user desktop-style app:

- One global `AppState` singleton per process
- State persisted to `sessions/latest.ci` (JSON) + parquet files in `sessions/corpora/<hash>.parquet`
- `api_key` in-memory only (never hits disk)
- Long-running jobs (`/api/coding/ai/run`, `/api/topics/induce`, CSV export) run inside the uvicorn request cycle via SSE
- ~50 endpoints call `get_state()` directly

Decision: **Corpus Intel stays a separate app** from AllAtOnce. It does not read AllAtOnce's tables and is not a module inside it. Only the identity layer is shared (see §3).

## 2. The Core Shift

Move from "one process = one user's state" to **workspace-scoped multi-tenant server**.

- Every domain object gets a `workspace_id`: `datasets`, `corpora`, `snapshots`, `codebooks`, `slices`, `tags`, `topic_sets`, `provenance`, `memos`, `jobs`.
- A workspace has members with roles: `owner` / `editor` / `coder` / `viewer`.
- Two analysts on the same project share one workspace. Two projects for the same person = two workspaces.
- Do **not** spin up one uvicorn process per user. That shortcut blocks sharing codebooks/datasets later and multiplies ops cost.

## 3. Identity

Piggyback on **ISD's existing Supabase** (same one AllAtOnce uses) as an auth provider only.

- Corpus Intel validates Supabase JWTs on every request.
- Corpus Intel does **not** read AllAtOnce's schema. Zero coupling.
- Benefit: researchers use one login across ISD tools.

## 4. Storage

- **Postgres** — replaces `sessions/latest.ci`. Owns all structured state (workspaces, datasets metadata, codebooks, slices, tags, provenance, jobs).
- **Blob storage** (Azure Blob or S3) — replaces `sessions/corpora/*.parquet` and raw uploads. Keyed by `workspace_id/<content_hash>.parquet`. Keep the content-addressed hash — it's one of the better pieces of the current design.
- **Cache** — the current answer cache (`sessions/cache/ai_coding.json`) moves into Postgres keyed on `(workspace_id, row_hash, codebook_id, codebook_version, prompt_version, cat_ids)`.

## 5. Jobs / Long-Running Work

The three flows that currently stream inside the HTTP request cycle become background jobs with worker processes:

- `/api/coding/ai/run` → enqueue job, poll or subscribe to status.
- `/api/topics/induce` → same.
- CSV / XLSX export on huge corpora → same (small exports stay inline).

A `jobs` table tracks `(id, workspace_id, kind, params, status, progress, result, created_by)`. UI subscribes via Supabase Realtime or polls. SSE stays for *progress events within* a running job; the HTTP call that *kicks off* the job returns immediately.

## 6. Concurrency — the Corpus-Intel-specific gotchas

Most of the multi-user difficulty is here, because the app's primitives are designed for single-editor flow.

### Codebook edits

Optimistic locking: every codebook row carries a `version` column, bumped on each edit. Writes include the expected prior version; stale writes return 409. Prevents two coders silently clobbering each other's category titles/descriptions.

### Manual coding (tagging)

Tags stay append-only per `(row_hash, coder_id, codebook_version)`. **Two coders tagging the same row is expected, not a conflict** — that's how inter-coder reliability works. Don't deduplicate.

### AI runs

`jobs` table has a unique constraint on `(workspace_id, slice_id, codebook_version, status='running')`. Stops the same scope from being coded twice concurrently, wasting API budget.

### Snapshot activation

Keep the existing "re-activating current snapshot is a no-op" behaviour (audit Fix #5). Extend: invalidating slices on snapshot change must be workspace-scoped — one workspace's snapshot swap must not touch another workspace's slices.

### Budget

The `spent_usd` / `budget_usd` accounting moves to per-workspace. A workspace owner sets the monthly cap; editors can override with explicit confirm (same UX as today).

## 7. What the Rewrite Actually Touches

Rough scope — this is **not a weekend migration**:

- `get_state()` → `get_state(workspace_id)`. Every endpoint (~50) gains an auth dependency that resolves workspace from JWT + path/query.
- `AppState` becomes a per-workspace repository backed by Postgres transactions. The `get_state_lock()` global RLock dissolves into row-level locks / optimistic concurrency.
- `save_state()` / `load_state()` → per-table ORM writes.
- Parquet paths become blob URIs; `load_corpus()` / `load_dataset()` stream from blob.
- `api_key` moves off the process. Becomes either a per-user secret encrypted at rest, or a per-workspace secret owned by the workspace owner. Open question — see §9.
- SSE endpoints split: kick-off returns a `job_id`, progress endpoint streams events for that job.
- Provenance events gain `actor_user_id` (they currently only record action+params; the person is implicit).

**Estimate:** 2–4 weeks of focused work. No clean partial-migration path — the global `AppState` pattern is everywhere, so it's a single cut-over.

## 8. Non-goals (for v1 multi-user)

- **No real-time collaborative editing** on codebooks (no Figma-style cursors). Optimistic locking + "someone else edited this, refresh" is enough.
- **No live presence indicators** ("X is coding this slice now") unless inter-coder reliability is the primary use case — see §9 Q2.
- **No organization/team hierarchy beyond workspaces** in v1. Flat workspace list with member roles. Add orgs later if needed.
- **No public sharing / guest links.** All access goes through ISD Supabase accounts.

## 9. Open Questions (revisit before starting)

1. **Infra:** Same Azure VM as AllAtOnce, or separate?
   - Shared → lower ops overhead, reuses existing monitoring/backups.
   - Separate → clean blast-radius, independent scaling, easier to hand off.

2. **Primary multi-user use case:** is it **inter-coder reliability** (multiple coders independently tagging the same rows, then comparing) or just **parallel work on different slices** (two analysts, same corpus, different questions)?
   - ICR-primary → we need presence indicators, coder-identity on every tag, ICR dashboards as first-class UI.
   - Parallel-work-primary → simpler; no presence needed, but workspace-level slice ownership matters more.

3. **Offline / single-user mode:** do we keep a local-desktop install path, or is multi-user the only future?
   - Keeping both doubles the test surface and constrains the rewrite (can't assume Postgres is present).
   - Cutting it means every user is online and authenticated — fine if that matches ISD research workflow.

4. **API key ownership:** per-user or per-workspace?
   - Per-user → each researcher brings their own Anthropic account; billing is personal; privacy is clean. But hurts shared budget visibility and means every new workspace member has to paste a key.
   - Per-workspace → one key per project, admin-set; simpler onboarding; matches AllAtOnce's model where the tool owns the credential. But the key sits in DB (encrypted) rather than in-process-only.

5. **Data retention:** AllAtOnce's DPIA mentions policies to delete data from OneDrive once research is finished. Does Corpus Intel inherit the same retention rules? Need workspace-level "archive" / "purge" actions and a scheduled cleanup.

6. **Server region:** AllAtOnce's Azure VM is currently US-based with migration to EU possible. Corpus Intel will hold raw social media content under ISD research context — presumably EU-only from day one?

## 10. What We Already Got Right (keep)

- Content-addressed snapshots (`<hash>.parquet`) — survives cleanly into multi-tenant.
- Answer-cache keyed on `(row_hash, codebook_id, codebook_version, prompt_version, cat_ids)` — just add `workspace_id` to the key.
- Provenance event log — just add `actor_user_id`.
- Claude client centralization in `ai/claude_client.py` — stays; API-key lookup gains a workspace dimension.
- Prompt constants in `ai/prompts.py` — stays.
- Preflight / budget gates — stays; scope moves to workspace.

## 11. Recommended Next Step (if green-lit)

1. Answer §9 Qs 1–4 in a short meeting.
2. Write a migration plan: new schema, data-model diagram, endpoint-by-endpoint auth dependency.
3. Stand up a parallel branch with Postgres + Supabase JWT middleware, port one vertical slice end-to-end (Upload → Mapping → Corpus → one codebook tag) before touching the rest. Proves the pattern before the full 50-endpoint sweep.
