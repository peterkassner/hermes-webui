---
goal: 'Keep eligible session metadata available during active chats while safely refreshing it in the background'
version: '1.1'
date_created: '2026-11-20'
last_updated: '2026-11-20'
owner: 'Hermes WebUI fork'
status: 'Implemented locally; live verification pending'
tags: [performance, sessions, caching, availability, regression]
---

# Introduction

![Status: Implemented locally](https://img.shields.io/badge/status-Implemented_locally-green) (`last_linked: 2026-11-20`)

Correct the live cache-availability defect in the first warm-session implementation. A usable session list must remain available while messages arrive and a replacement is prepared. Separate ordinary updates from changes that remove a session or permission; publish complete replacements without waiting for all writers to stop; and ignore unrelated heartbeat updates. Preserve exact-ID access, lazy background histories, profile separation, existing cleanup and bounded cache ownership. Local implementation and verification are recorded in the [correction evidence](cache-availability-webui-evidence.md). Live deployment and observation remain pending.

### Review against the reported incident

The proposed correction is likely to resolve the active-chat sidebar failures. The [incident report in the review chat](codex://threads/01a1f6e2-deb5-7202-b52d-4bbb18bea262) recorded 11 sidebar 503s in about 322 seconds despite fast initial warm reads. Current code explains a way to produce that pattern. `warm_scope_needs_refresh` removes the published list on a fingerprint change. The interactive request can wait only 250 ms for a missing list, and `_refresh_shared_inventory` can exhaust all three attempts while the watched files keep changing. Serving an eligible list immediately and allowing a replacement to publish between ordinary writes addresses both failure points. The observation did not establish the cause of each individual 503. Local confidence depends on reproducing the failure and showing both availability and visible progress under the same workload. Production recovery requires a later observation during activity.

## 1. Requirements & Constraints

### Assumptions first

- **ASSUMPTION-001**: One server process owns the existing derived caches. Its matching clients share those caches; no distributed cache is needed.
- **ASSUMPTION-002**: During ordinary message/title/count updates, a previously accepted list may be briefly behind the latest content while a replacement is prepared. Existing response-time run/stream overlays continue to supply live activity. Cached metadata never grants permission to open or change a conversation.
- **ASSUMPTION-003**: WebUI-owned removals and visibility/access changes have a committed mutation boundary that can update cache eligibility before the successful mutation response. External changes retain the existing five-second detection cadence plus the time needed for the scoped check; they are not instant notifications.
- **ASSUMPTION-004**: SQLite can supply a consistent read-only view for one profile while writers continue. Separate profile databases and JSON sidecars do not form one global transaction; explicit identity/safety checks and mutation overlays must bridge those boundaries.

### Requirements

- **REQ-001**: After a scope has an eligible successful snapshot, ordinary activity, expiry, a slow content refresh or a failed content refresh must produce neither HTTP 503 nor a fabricated empty list. Serve that snapshot until an eligible complete replacement can be swapped in. Truly cold scopes and unverifiable access/identity remain explicit retryable failures.
- **REQ-002**: Track ordinary content changes separately from safety changes in the existing cache records. A content change marks work pending; it does not remove the published snapshot or revoke its eligibility. Define a safety counter that changes when old work could restore removed/restricted data, and a separate content-change counter used only to schedule refreshes.
- **REQ-003**: Apply committed delete/archive/hide/access changes at the affected row or scope boundary. Older refresh work must not reintroduce a removed or newly ineligible row, undo a newer metadata mutation, or cross profiles/homes/settings identities. Unknown legacy invalidation reasons retain conservative behavior until audited; do not globally convert every cache clear into a soft refresh.
- **REQ-004**: A refresh can publish a consistent point-in-time result while ordinary writes continue. Remove the requirement that all database/file timestamps stay unchanged throughout the build. Validate profile/home/database identity and safety counters at publication, record the revision actually read, and leave a later content change pending for bounded follow-up work. Never label an older read with a later, unread revision.
- **REQ-005**: Keep one rebuild owner at both route and CLI layers. Many clients, repeated dirty signals and request deadlines must not create independent builders. Maintain at most one pending follow-up per identity, with no overlap or busy retry loop. All exits release waiters; shutdown, retirement and replacement fence late publication.
- **REQ-006**: External eligibility checks use scoped, indexed checks of published IDs, IDs introduced by a replacement, and relevant sidecar metadata. Distinguish append/title/count changes from removal, archive, source/owner or access changes. Apply observed safety changes before accepting another snapshot. A permissive existence helper that assumes a row exists on read failure is not authority for continuing to show a restricted row. A content-read failure with known valid eligibility is distinct from an unverifiable safety check.
- **REQ-007**: Track profile/home/database identity separately from content freshness. Home-directory mtime/ctime changes caused by an unrelated atomic heartbeat replacement must not schedule a session projection. Relevant database/WAL, sidecar, index and settings changes must still be detected, including deletions and same-size edits. Ordinary WAL rotation/checkpoints are not profile or primary-database replacement.
- **REQ-008**: Preserve existing session order, compression lineage, top-N metadata coverage, archived/hidden filtering, project ownership, exact-ID access outside recent windows, all-profile context priority, completion/unread delivery and response-time runtime overlays. Ordinary boot, activity polling, warming and SSE reconnect must retain zero cron/webhook/kanban history passes.
- **REQ-009**: Expose or record bounded scalar evidence that distinguishes an eligible snapshot being served during refresh, a truly cold scope, a safety refusal, refresh failure and completed publication. Read-only health sampling must not build caches. Do not expose session contents, credentials or per-session identifiers in these metrics.

### Constraints and guidance

- **CON-001**: SQLite and sidecars stay authoritative persistent state; server cache entries are derived metadata and browser state is presentation. Add no persistent session store, runtime package, distributed service or execution redesign. Reuse the current watcher, cache registries, copy-safe row projection, bounds and generation mechanisms.
- **CON-002**: Keep the existing 64 route-entry and eight warm-scope bounds, startup-scope retention and bounded CLI caches. Count the temporary replacement under construction when checking peak memory. Do not solve availability by merely raising TTLs, freezing freshness indefinitely, or suppressing deletions/access checks.
- **CON-003**: Hold no cache/store/stream registry lock across SQLite/file reads, waits or HTTP writes. Borrowed read connections must not be closed by a callee. Release transactions/connections on success, error, cancellation and shutdown. Do not add unbounded transactions, WAL-retention pressure, retries or queued refresh work.
- **CON-004**: Preserve the existing reconciliation/cleanup behavior. Do not add the previously declined blanket requirement that warming perform zero persistent writes. This correction still must not rewrite transcripts, alter run ownership, create database indexes/schema, perform production maintenance or flush live caches to manufacture tests.
- **CON-005**: Preserve all concurrent working-tree edits, including the existing login/favicon/UI changes in FILE-002 and other unrelated files. Use isolated state with no live credentials/providers for execution tests. No active-chat interruption, deployment, restart or exposure beyond localhost belongs to the implementation gate; any later live verification requires a separately authorized operator action.
- **CON-006**: Model catalogs, OAuth/provider repair, CourtListener authentication, Tailscale, Docker and virtual-keyboard policy are out of scope. A healthy process or passing idle benchmark cannot substitute for active-write sidebar availability.
- **GUD-001**: Follow [contract routing](../docs/CONTRACTS.md) (`last_linked: 2026-11-20`), [change guidelines](../docs/GUIDELINES.md) (`last_linked: 2026-11-20`) and the [current inventory contract](../docs/architecture/warm-session-inventory.md) (`last_linked: 2026-11-20`). Explicitly document the change from withholding a snapshot on every source change to retaining eligible data during ordinary refresh.
- **GUD-002**: `MISSING_GUIDANCE`: the invoked skill has no supporting references directory. In the next skill-maintenance iteration, create [planning guidance at the skill's references location](../../../Documents/SSI/Agents/skills/create-implementation-plan/references/planning-guidance.md) (`last_linked: 2026-11-20`; planned, currently absent). That maintenance is separate from this correction and is not a repository blocker.

### Required cache behavior

| Condition | Published list | Refresh/publication behavior |
|---|---|---|
| Ordinary message, title, count or ordering update | Keep eligible data available | Mark content pending; build once; publish a complete point-in-time replacement |
| Content refresh is slow or fails | Keep eligible data available | Record failure/age; retry at bounded cadence; never publish failure as empty success |
| Committed delete/archive/hide or observed external removal | Remove or re-filter affected rows | Advance safety counter; prevent older candidates from restoring them |
| Profile/access/home/primary-database identity becomes invalid or unverifiable | Do not serve incompatible or unverified data | Retire/refuse the affected scope and prepare the correct identity separately |
| First load or permitted eviction leaves no accepted snapshot | No invented list | Share the owner/result within the bounded request budget; explicit retry if unavailable |
| Irrelevant heartbeat replacement in the home directory | Keep data available | No session projection solely because the parent directory timestamp changed |

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Reproduce the live failure and define explicit state ownership before changing availability behavior.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Trace [cache records and source checks](../api/route_session_list_cache.py) (`last_linked: 2026-11-20`) and [route refresh/request paths](../api/routes.py) (`last_linked: 2026-11-20`). Audit the shared session-list change listener, direct clears, settings/profile paths, inner CLI refresh, watcher notifications and cleanup writes through FILE-001 through FILE-005. Record each mutation's committed owner, affected rows/scope and ordinary-versus-safety classification. Identify every place that deletes/refuses a published snapshot or requires source quiescence. Output: the implementation uses one classified invalidation path; unclassified callers remain conservative. | Yes | 2026-11-20 |
| TASK-002 | Add failing cases in [the planned activity suite](../tests/test_warm_cache_activity.py) (`last_linked: 2026-11-20`; new file) and extend FILE-007. Start with an accepted snapshot, keep writes happening across a blocked build, then issue several matching reads. Reproduce the existing removal on fingerprint change, refusal to publish until files stop changing, and unrelated heartbeat invalidation. Use events/barriers and an injected clock for correctness tests. Output: recorded red results fail for availability/publication reasons, not only a missing new argument. | Yes | 2026-11-20 |
| TASK-003 | Define the exact entry/state transitions, eligibility proof, safety counter, content counter, candidate revision and ownership cleanup in [the inventory contract](../docs/architecture/warm-session-inventory.md) (`last_linked: 2026-11-20`). Identify fields added to existing bounded records, default behavior for legacy callers, and whether each read connection is borrowed or owned. Output: no undefined category of source change silently permits stale access or discards all data. | Yes | 2026-11-20 |

### Implementation Phase 2

- **GOAL-002**: Preserve eligible published data and make mutation safety independent of ordinary freshness.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | Extend [the existing cache lifecycle](../api/route_session_list_cache.py) (`last_linked: 2026-11-20`) and FILE-002/FILE-003 mutation notifications to distinguish ordinary dirty signals from safety invalidation. Keep the published snapshot on ordinary writes. Apply copy-safe row updates/removals and advance the relevant safety counter after committed mutations and before their successful response. Ensure failed mutations do not leave false removals; preserve deletion markers/overlays until older work can no longer restore the row. Output: ordinary saves retain availability; delete/archive/hide/access changes win both race orderings. | Yes | 2026-11-20 |
| TASK-005 | Update [the shared request path](../api/routes.py) (`last_linked: 2026-11-20`) and warm exact-ID/SSE consumers in FILE-001/FILE-005 to use eligibility rather than full source-timestamp equality when deciding whether old data may be served. Keep current profile/home/discovery/visibility identity checks and existing runtime overlays. A failed content refresh cannot force a warm request into the cold 503 branch. Output: all same-scope warm consumers agree on the accepted snapshot; genuine cold/safety refusals remain explicit. | Yes | 2026-11-20 |
| TASK-006 | Implement scoped external eligibility reconciliation through [strict existing-row readers](../api/models.py) (`last_linked: 2026-11-20`) and [agent readers](../api/agent_sessions.py) (`last_linked: 2026-11-20`). Use bounded indexed ID batches plus current sidecar/deletion/access metadata, not a full history enumeration per request. Observe removal/source/owner/archive changes, apply eligibility updates before publication, and fence earlier candidates. Output: external appends keep the prior list usable; observed deletion/restriction cannot be resurrected; unverifiable safety does not inherit a permissive existence fallback. | Yes | 2026-11-20 |

### Implementation Phase 3

- **GOAL-003**: Publish refreshed metadata while writers continue, with bounded work and narrower change detection.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-007 | Extend [read-only reader ownership](../api/agent_sessions.py) (`last_linked: 2026-11-20`) and FILE-003 warm projection/lineage callers so the related warm queries for one profile share a builder-owned read transaction where needed. Reuse the existing read-only connector; add optional borrowed-connection support without changing default caller ownership. Pin the read view before collecting its related rows, close it promptly, and preserve existing SQL filtering/source/lineage rules. Treat sidecars as individually read versions with mutation overlays; do not claim cross-store atomicity. Output: append writers commit while a build reads a coherent profile view, and every connection/transaction is released. | Yes | 2026-11-20 |
| TASK-008 | Replace the full before/after file-equality admission rule in [the publisher](../api/routes.py) (`last_linked: 2026-11-20`) and audit inner cache publication/fallback in FILE-003. Accept a completed candidate when its owning scope, database identity and safety counter remain valid. Swap the bounded replacement under the existing cache lock; record what was actually read. Later ordinary changes retain one pending follow-up instead of rejecting useful work or restarting until quiet. Output: continuous writes cannot indefinitely prevent successful publication, and a newer deletion/access change still defeats an older candidate. | Yes | 2026-11-20 |
| TASK-009 | Narrow [warm fingerprints](../api/route_session_list_cache.py) (`last_linked: 2026-11-20`): resolved home/target plus device/inode establish directory identity; ordinary parent-directory mtime/ctime does not. Keep content signatures for relevant individual files, settings semantics, sidecar discovery and primary-database replacement. Treat WAL rotation as freshness, not ownership loss. Use the same classification during and outside streaming; do not introduce a second contradictory full-timestamp publication gate. Output: heartbeat-only replacements cause zero projections, while relevant edits/deletes/replacements remain detectable. | Yes | 2026-11-20 |
| TASK-010 | Integrate dirty/follow-up scheduling with [the existing watcher](../api/gateway_watcher.py) (`last_linked: 2026-11-20`) and existing route/CLI single-owner registries. Retain five-second reconciliation, nonblocking startup, no-client operation, bounded admission, no overlapping builders and bounded retry/shutdown. Store only lifecycle state in admission records; the candidate is temporary, not another published store. Output: many clients and many writes create one live owner plus at most one pending follow-up; failures, invalidation and teardown release all waiters without late publication. | Yes | 2026-11-20 |

### Implementation Phase 4

- **GOAL-004**: Prove availability during real concurrent writes and preserve the neighboring contracts.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-011 | Extend [the existing browser probe](../tests/browser_warm_session_inventory.py) (`last_linked: 2026-11-20`) with the activity mode specified in TEST-004, and add real SQLite/WAL concurrency cases in FILE-008. Warm first, then keep synthetic messages arriving while four authenticated contexts request the sidebar. Include external-only writes and fixture-owned active stream/run flags, a deliberately slow refresh, one failed content refresh and unrelated heartbeat replacement. Use no real provider or live chat. Output: red at the failing baseline, green after correction, with writes confirmed throughout the read interval and replacements published before writes stop. | Yes | 2026-11-20 |
| TASK-012 | Add bounded scalar readiness/freshness/refusal evidence to [the existing cache health snapshot](../api/route_session_list_cache.py) (`last_linked: 2026-11-20`) and [system health](../api/system_health.py) (`last_linked: 2026-11-20`) only as needed for the tests/report. Preserve nonblocking lock collection and redact payloads. Update FILE-012 coverage. Output: an eligible list being served during refresh is distinguishable from an empty cold/safety-blocked scope, and sampling adds no discovery work. | Yes | 2026-11-20 |
| TASK-013 | Execute TEST-001 through TEST-007 using isolated state. Cover mutations and external safety checks during both publication orderings, all-profile duplicate IDs, old direct links, archive/lineage coverage, source limits, hidden panels and cleanup/lifecycle failures. Output: actual red/green evidence, current affected/neighboring results, measured activity latency and any pre-existing failures clearly separated; idle success alone cannot pass. | Yes | 2026-11-20 |

### Implementation Phase 5

- **GOAL-005**: Publish reviewable evidence and keep live deployment separately controlled.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-014 | Update [the inventory contract](../docs/architecture/warm-session-inventory.md) (`last_linked: 2026-11-20`), FILE-014 and FILE-015 to describe content-versus-safety invalidation, serving during refresh, point-in-time publication and genuine cold/refusal behavior. Add a dated correction to FILE-016's historical validation context without rewriting its old results as new proof. Create FILE-017 with commands, outcomes and Contract Routing/Contract Change evidence. Output: the defect, correction, safety reasoning and measured limits are independently reviewable. | Yes | 2026-11-20 |
| TASK-015 | Mark implementation readiness only after local gates pass; report deployment pending. Prepare a later operator checklist using FILE-018: confirm authorization and active-run handling, observe startup without warming via probe side effects, then exercise safe real activity and correlate API outcomes with cache eligibility/refresh metrics. Do not create a live test conversation, interrupt a chat, restart or modify credentials as part of this local implementation. Output: a concrete handoff distinguishes completed isolated proof from later authorized live proof. | Yes | 2026-11-20 |

### Implementation details that affect acceptance

- **TASK-004 / TASK-006 — Safety across copies.** Key each decision by owning profile/home and session ID. Apply it to every affected single-profile and all-profile view, including `sidebar_reference_sessions`, exact-ID metadata and Gateway notifications. Preserve archived rows for authorized archive views. Include IDs present only in a candidate when reconciling external changes. Check external eligibility against a fresh authoritative view after the candidate read transaction closes. A deletion committed after that check is subject to ASSUMPTION-003. Once observed, a safety change must fence publication even if it lands between the check and the swap. Test a previously unseen row deleted during a blocked build, with a same-ID row in a second profile.
- **TASK-005 / TASK-008 — One publication decision.** Take a short cache lock to verify the current admission instance, owner and safety generation and install rows, their read revision and pending-refresh state together. At the checkpoint `_session_list_cache_set` sampled a new stamp and `mark_warm_scope_refreshed` recorded the fingerprint separately. Pass the builder’s captured revision through both cache layers. A change observed after its read began must leave follow-up work pending even if publication succeeds. A nonempty or legitimately empty eligible snapshot returns immediately during refresh. Reserve the bounded wait for a genuinely missing eligible result.
- **TASK-009 / TASK-010 — Isolation under load.** At the checkpoint the watcher rebuilt admitted scopes serially. Keep eligibility checks and Gateway notifications for a second scope responsive while the first scope has a blocked content build. Reuse bounded worker admission and prioritize due safety checks. Eviction or retirement of one options key previously cleared its whole profile and all-profile entries. Retire only the displaced instance for capacity eviction. Verify the startup list stays available under admission churn and a worker from an evicted instance cannot publish after the identical key is readmitted.

## 3. Alternatives

- **ALT-001**: Increase TTLs or stop checking during streaming. Rejected: hides updates/deletions and does not fix the empty interval or requirement that writers stop before publication.
- **ALT-002**: Keep every old row on every kind of change. Rejected: can restore deleted/hidden sessions, use replaced-profile data or bypass current eligibility checks. Retention must be conditional on identity and safety.
- **ALT-003**: Make each client wait for or perform a fresh inventory build. Rejected: recreates request stalls and duplicate work; it prevents the cache from providing availability during activity.
- **ALT-004**: Introduce a second database/service or redesign transcript execution. Rejected: the defect is in the existing derived-cache validity and refresh rules; broader infrastructure adds unnecessary ownership and migration obligations.

## 4. Dependencies

- **DEP-001**: Current FILE-001 through FILE-005 mechanisms: bounded caches, per-scope ownership, mutation notifications, exact-ID readers, strict existence checks, sidecar metadata, watcher lifecycle and runtime overlays. Extend them rather than creating parallel authorities.
- **DEP-002**: Existing SQLite WAL/read-only support through FILE-004. Verify reader borrowing, transaction lifetime and checkpoint implications in isolated fixtures before depending on a shared read view.
- **DEP-003**: Repository test runner, Node for existing client tests, and the already documented disposable Playwright browser probe. No application dependency is added; browser tooling remains optional verification tooling with an installed Chromium.
- **DEP-004**: Successful local gates are prerequisites for proposing deployment. Any live restart/activity trial needs current operator authorization and must respect real active runs; prior restart authorization has already been used.

## 5. Files

Paths below exist unless explicitly marked planned. Resolve symbols again before editing; concurrent changes are expected.

- **FILE-001**: [Route cache](../api/route_session_list_cache.py) (`last_linked: 2026-11-20`): `warm_scope_needs_refresh`, `warm_scope_fingerprint`, eligibility, cache get/set/clear, mutation generations, bounded admission and health snapshot.
- **FILE-002**: [Routes](../api/routes.py) (`last_linked: 2026-11-20`): `_on_session_list_changed`, `_get_shared_session_list_payload`, `_refresh_shared_inventory`, `_reconcile_warm_session_inventories`, mutation boundaries and exact-ID/SSE consumers. Preserve the pre-existing login favicon edits.
- **FILE-003**: [Models](../api/models.py) (`last_linked: 2026-11-20`): inner CLI ownership/complete-read policy, invalidation listeners, `agent_session_rows_existing_strict`, tombstones, sidecar projections and lineage enrichment.
- **FILE-004**: [Agent readers](../api/agent_sessions.py) (`last_linked: 2026-11-20`): `open_state_db_readonly`, `read_importable_agent_session_rows`, `read_session_lineage_metadata` and connection/transaction ownership.
- **FILE-005**: [Gateway watcher](../api/gateway_watcher.py) (`last_linked: 2026-11-20`): five-second reconciliation, shared-row notifications, coalescing and stop/retire behavior. [Server lifecycle](../server.py) (`last_linked: 2026-11-20`) is an audit neighbor; change it only if lifecycle integration actually requires it.
- **FILE-006**: [Profile resolution](../api/profiles.py) (`last_linked: 2026-11-20`): authoritative scope/home identity and read-only worker context. Audit before widening eligibility; preserve current authorization boundaries.
- **FILE-007**: [Existing warm regressions](../tests/test_warm_session_inventory.py) (`last_linked: 2026-11-20`): extend age/failure/retirement/deletion/stream-freeze coverage and replace changed-source expectations intentionally.
- **FILE-008**: [Activity regressions](../tests/test_warm_cache_activity.py) (`last_linked: 2026-11-20`): deterministic continuous-write publication, heartbeat, eligibility and concurrency cases with real SQLite where required.
- **FILE-009**: [Browser probe](../tests/browser_warm_session_inventory.py) (`last_linked: 2026-11-20`): add continuous activity, slow/failure injection, four-context read results and safe evidence collection.
- **FILE-010**: [Sidebar cache tests](../tests/test_session_sidebar_cache.py), [CLI streaming cache tests](../tests/test_issue4842_cli_cache_streaming.py), [fingerprint tests](../tests/test_cli_sessions_cache_fingerprint.py) and [bounded cache tests](../tests/test_session_list_cache_bounded.py) (each `last_linked: 2026-11-20`): ownership, freshness, mutation and memory neighbors.
- **FILE-011**: [Read-only reader tests](../tests/test_state_db_readonly_readers.py), [model read-only tests](../tests/test_state_db_readonly_reads_models.py), [cross-profile materialization tests](../tests/test_7731_cross_profile_materialize_isolation.py) and [archive owner tests](../tests/test_7731_archive_cross_profile_owner_gate.py) (each `last_linked: 2026-11-20`): borrowed handles, schema safety and profile/deletion boundaries.
- **FILE-012**: [System health](../api/system_health.py) and [health tests](../tests/test_issue693_system_health_panel.py) (each `last_linked: 2026-11-20`): existing bounded, nonblocking scalar diagnostics; no new production diagnostics endpoint.
- **FILE-013**: [CLI metadata integration tests](../tests/test_cli_session_metadata_fastpath_integration.py), [cron project histories](../tests/test_issue3019_cron_project_sessions.py), [webhook project histories](../tests/test_webhook_project_sessions.py) and [assigned-project limits](../tests/test_project_assigned_cli_session_limit.py) (each `last_linked: 2026-11-20`): preserve direct reachability, collisions, source intent and limits.
- **FILE-014**: [README](../README.md) (`last_linked: 2026-11-20`): short plain-language availability description.
- **FILE-015**: [Testing instructions](../TESTING.md) (`last_linked: 2026-11-20`): exact reproducible active-write commands and distinction between local/remote proof.
- **FILE-016**: [Original plan](warm-session-inventory-webui-v1.md) and [earlier local evidence](warm-session-inventory-evidence.md) (each `last_linked: 2026-11-20`): historical scope and proof; their earlier idle success does not establish this correction.
- **FILE-017**: [Correction evidence](cache-availability-webui-evidence.md) (`last_linked: 2026-11-20`): dated decision chain, contract change, red/green results, measurements and remaining deployment gaps.
- **FILE-018**: [Existing service control](../scripts/macos_launchd_control.sh) and [restart safety implementation](../api/server_restart.py) (each `last_linked: 2026-11-20`): read-only planning references for a later separately authorized restart; no modifications required by this correction.

## 6. Testing

- **TEST-001**: Deterministic red/green tests in FILE-007/FILE-008 must cover: a populated cache read while a refresh is blocked beyond the request budget; content writes at every attempted publication; one failed content refresh; many followers/dirty signals; and heartbeat-only directory timestamp changes. Assert usable row identities, successful publication before writer shutdown, exactly one live builder and bounded pending work. Barriers/events control ordering; timeouts only bound test failure, not prove correctness. Record failure at the uncorrected baseline before applying the fix.
- **TEST-002**: Safety matrix: race delete/archive/hide/profile removal/access change against publication in both orders; check committed updates and observed external removals cannot be undone. Replace the home/symlink target or primary database, collide IDs across profiles, and make eligibility unverifiable. Old incompatible data must not be served. Separately fail only the content projection while safety remains valid: the last good list must still be served. Verify shutdown, retirement, cold start and eviction do not strand waiters or admit late publication.
- **TEST-003**: Use a fresh writable scratch root, verify no fixture can resolve live state/credentials, and run the affected/neighboring suites through the repository runner. Add any extra mutation/reader suites discovered by TASK-001 before claiming completion.

```sh
trial_root=$(mktemp -d "${TMPDIR:-/tmp}/webui-cache-availability.XXXXXX")
export HERMES_HOME="$trial_root/home"
export HERMES_BASE_HOME="$trial_root/base"
export HERMES_CONFIG_PATH="$trial_root/home/config.yaml"
export HERMES_WEBUI_STATE_DIR="$trial_root/webui"
export HERMES_WEBUI_TEST_STATE_DIR="$trial_root/tests"
mkdir -p "$HERMES_HOME" "$HERMES_BASE_HOME" "$HERMES_WEBUI_STATE_DIR"
./scripts/test.sh -q tests/test_warm_cache_activity.py tests/test_warm_session_inventory.py tests/test_session_sidebar_cache.py tests/test_issue4842_cli_cache_streaming.py tests/test_cli_sessions_cache_fingerprint.py tests/test_session_list_cache_bounded.py tests/test_state_db_readonly_readers.py tests/test_state_db_readonly_reads_models.py tests/test_7731_cross_profile_materialize_isolation.py tests/test_7731_archive_cross_profile_owner_gate.py tests/test_issue693_system_health_panel.py tests/test_cli_session_metadata_fastpath_integration.py tests/test_issue3019_cron_project_sessions.py tests/test_webhook_project_sessions.py tests/test_project_assigned_cli_session_limit.py
```

- **TEST-004**: Extend FILE-009 to accept the following planned activity arguments. TASK-011 implemented these commands. Run the checkpoint and corrected code sequentially against identical representative synthetic data. Use four independent authenticated Chromium contexts at desktop/narrow sizes. Warm the scope before starting the activity interval, then read every 250 ms per client while a synthetic WAL writer commits every 100 ms for 120 seconds. Exercise both external-only writes and a provider-free fixture-owned active WebUI stream. Keep provider/network access blocked and record actual write progress, not just a running timer.

```sh
uv run --no-project --with playwright python tests/browser_warm_session_inventory.py --revision 3b9f7be39 --activity --duration 120 --writer-interval 0.1 --clients 4 --read-period 0.25 --slow-refresh 2 --fail-one-refresh --output /tmp/webui-cache-availability-before
uv run --no-project --with playwright python tests/browser_warm_session_inventory.py --activity --duration 120 --writer-interval 0.1 --clients 4 --read-period 0.25 --slow-refresh 2 --fail-one-refresh --output /tmp/webui-cache-availability-after
```

- **TEST-005**: Hard activity gates: zero sidebar HTTP 503/errors and zero fabricated empty lists for the already-warm eligible scope; all clients retain access while the two-second refresh and injected content failure occur; replacement snapshots publish while writes are still continuing; latest changes appear within one five-second reconciliation interval plus measured check/build time after the writer stops. Record status counts, eligible-snapshot occupancy, publication age/progress, concurrent-builder maximum, pending-work bound and memory. Do not require zero rebuilds when relevant input actually changes. A safety refusal or truly cold failure belongs to the separate TEST-002 matrix, not this eligible-activity success count.
- **TEST-006**: Preserve neighboring behavior: exact-ID reads still cause zero unrelated inventory passes; no-client updates still reconcile; heartbeat-only replacements cause zero inventory rebuilds; hidden backgrounds still cause zero history passes; all-profile/project/archive/compression and runtime overlay behavior remains correct. Measure warm sidebar p95 during activity, targeting below 100 ms on a comparable local host; report contention and distributions without relaxing the zero-error correctness gate. Include transaction lifetime/WAL growth and peak memory so a long read transaction cannot hide an operational regression. No production cache flush or forced quiet interval is permitted as proof.
- **TEST-007**: Run `git diff --check`, Python syntax validation for changed modules, the repository's established JS syntax/runtime checks if any JS is touched, and actual affected/neighboring lint/test commands. Document pre-existing failures separately. After local gates, provide an operator checklist for later authorized live verification: record restart/first-eligible-snapshot times, distinguish core health from sidebar success, sample cache refresh/eligibility, and count sidebar outcomes during safe real activity. Do not infer success from one initial warm sample or a process health check; do not interrupt a real active chat to obtain evidence.

### Evidence required from the activity probe

1. For TEST-004 run each activity mode against both revisions using the same harness, seed, settings and fault timing. The current probe rotates synchronously through four contexts. Start four independent polling loops behind one barrier, bound outstanding requests per client, and report attempts, completions, timeouts, skipped deadlines and longest response by client. Confirm overlapping reads during the blocked refresh and committed writes on both sides of publication. Fail the run if an injected slow or failed refresh never executed, or a client stopped sampling.
2. For TEST-005 write a sequence of distinct sidebar-visible metadata changes to a seeded row. During sustained writes every client must see successively newer values in order, with at least two distinct replacements published and seen before the writer stops. Include a final marker for the existing convergence bound. Cache counters alone cannot prove the UI updated. Exercise the normal sidebar refresh at desktop and narrow widths, checking the expected row stays usable throughout the fault and the updated title renders. Record per-client time to visible change. In a separate cold-failure case verify the loading error, successful Retry, and absence of previous-profile rows after a profile switch.
3. For TEST-002 / TEST-006 block one scope’s content build across a reconciliation tick. Delete or restrict a row in another warm scope and verify removal there within the specified detection interval plus measured safety-check time. Also exercise same-key retirement/readmission, and the candidate-only deletion described above. Check both visible and reference row collections and the appropriate exact-ID and Gateway results.
4. For the later authorized live check, cover at least the original 322-second observation window and include sustained real activity. Record sidebar attempts, status counts and publication progress during the active portion alongside core health. If no active chat or relevant writes occur, mark activity verification pending. Initial warm-up timing and idle successes remain separate measurements.

### Live baseline and acceptance meaning

The prior live restart found the first cache sample already populated by 12.8 seconds after the restart request, about 2.1 seconds after server initialization; that was an upper bound, not an exact completion timestamp. Ten controlled warm reads succeeded at approximately 11.6 ms median and 20.9 ms maximum. Longer observation found 11 sidebar HTTP 503 responses in about 322 seconds, including repeated failures after a real chat became active. A later core-health response was still healthy while the session cache was empty/rebuilding. Those observations establish the defect to reproduce; they are not verification of this proposed correction. The separate pre-existing CourtListener OAuth failure is outside this plan.

## 7. Risks & Assumptions

Assumptions ASSUMPTION-001 through ASSUMPTION-004 are declared before the requirements. There are no additional assumptions.

- **RISK-001**: Retaining old metadata can expose removed/restricted rows or restore older mutation values. Mitigation: explicit eligibility, scoped safety counters, strict external-ID checks, committed row overlays/removal markers, profile/home/database identity guards and deterministic publication races. Preserve current authorization at the actual open/mutate boundary.
- **RISK-002**: Continuous writes can starve publication, or a long read transaction/queued follow-ups can increase WAL growth, memory or CPU. Mitigation: consistent bounded reads, prompt connection release, useful point-in-time publication, one owner/one pending follow-up, existing bounds, and measured transaction/WAL/memory behavior under sustained activity.
- **RISK-003**: An idle fixture or process-health result can conceal activity failures again, and a live trial can disturb real work. Mitigation: red-before/green-after continuous-write HTTP gates with independent clients, explicit eligibility/error counters, unchanged source/ownership neighbors, and separately authorized live validation without forced quiet or active-chat interruption.

## 8. Related Specifications / Further Reading

- [Repository agent instructions](../AGENTS.md) (`last_linked: 2026-11-20`).
- [Contract routing index](../docs/CONTRACTS.md) (`last_linked: 2026-11-20`).
- [Change/evidence guidelines](../docs/GUIDELINES.md) (`last_linked: 2026-11-20`).
- [Current warm inventory contract](../docs/architecture/warm-session-inventory.md) (`last_linked: 2026-11-20`): its withholding-on-any-source-change rule is intentionally revised by this correction.
- [Run-state consistency contract](../docs/rfcs/webui-run-state-consistency-contract.md) (`last_linked: 2026-11-20`): derived metadata, read-only ownership preflight, authoritative runtime state and mutation boundaries.
- [Canonical session resolution contract](../docs/rfcs/canonical-session-resolution.md) (`last_linked: 2026-11-20`): direct links, recent-window independence and compression targets.
- [Testing instructions](../TESTING.md) (`last_linked: 2026-11-20`).
- [Original warm inventory plan](warm-session-inventory-webui-v1.md) (`last_linked: 2026-11-20`).
- [Earlier implementation evidence](warm-session-inventory-evidence.md) (`last_linked: 2026-11-20`): historical local fixture results, not a passing active-write gate.
