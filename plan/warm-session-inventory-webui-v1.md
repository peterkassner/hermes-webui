---
goal: Keep interactive session metadata warm and shared, resolve known sessions directly, and load background inventories only on demand
version: '1.1'
date_created: '2026-11-19'
last_updated: '2026-11-19'
owner: Hermes WebUI fork
status: 'Implemented locally; deployment pending'
tags: [performance, sessions, caching, lazy-loading]
---

# Introduction

![Status: Implemented locally](https://img.shields.io/badge/status-Implemented_locally-blue)

Implement only the three agreed changes: maintain shared warm interactive-session metadata independently of client polling; prevent known-session lookups and concurrent readers from triggering redundant discovery; and defer cron, webhook and kanban inventories until the corresponding view is opened. Extend the current cache, invalidation and watcher mechanisms; do not create a second session store or redesign execution. This document authorizes no deployment, restart, production-state migration or database maintenance. Local implementation and isolated validation are complete; deployment and public-ingress verification remain pending. See [implementation evidence](warm-session-inventory-evidence.md).

## 1. Requirements & Constraints

### Assumptions first

- **ASM-001**: One server process owns its in-memory metadata snapshots. Multiple devices share that process; cross-process distributed caching is not required.
- **ASM-002**: “Always warm” means retaining the last valid interactive snapshot while refreshing it, including when clients disconnect or suspend. A process restart necessarily has an initial cold interval.
- **ASM-003**: Background inventories are not part of normal chat boot or periodic chat refresh. Opening a specific background session by URL remains supported without enumerating its inventory.

### Requirements

- **REQ-001**: Warm interactive metadata for the startup active profile asynchronously, without delaying the health endpoint or listening socket. Keep that metadata refreshed without depending on browser requests. Additional authorized profiles are initialized on demand, not all eagerly at startup.
- **REQ-002**: Serve all matching clients from the same immutable/copy-safe snapshot. Filtered response variants must reuse the underlying warmed metadata rather than each enumerate backing stores. Preserve complete profile/home, visibility, source and settings identity; never share across incompatible scopes.
- **REQ-003**: Maintain one rebuild owner per existing cache identity at BOTH route and CLI-discovery layers. A slow live owner does not permit independent follower rebuilds. Coalesce further changes into bounded follow-up work.
- **REQ-004**: Expiry requests background refresh, not snapshot destruction. An eligible previous snapshot remains available during refresh/failure. Authorization changes and destructive mutations must not resurrect stale rows. Truly cold followers share one result within the existing request budget; a failed or timed-out read must be distinguishable from a successful read of an empty store. Never publish or cache a read failure as an authoritative empty list. If no eligible previous snapshot exists, return a bounded, explicit retryable failure and let the browser show a temporary loading failure with a retry action. A slow live owner remains the only builder even after a follower reaches its request deadline.
- **REQ-005**: Known-session metadata lookup uses a valid cached row or a bounded, authorized point lookup by exact session ID. No fallback to global `get_cli_sessions()` enumeration, including sibling helper callers. Missing-from-visible-window does not mean nonexistent. Opening one conversation must not enumerate unrelated histories inside the exact-ID reader either: disable cron/webhook/kanban inventory passes and other unrelated discovery for this path, while preserving the requested conversation and its required ownership/lineage checks.
- **REQ-006**: WebUI mutations update or invalidate affected metadata at the existing mutation boundary. External CLI changes are detected by a shared background reconciliation cycle; baseline cadence is five seconds, with no overlapping scans. Cheap unchanged checks skip expensive projection. Detect edits, deletions and DB/profile identity replacement as well as additions. Coverage must include WebUI-origin sessions, saved WebUI metadata files (sidecars), project assignments and visibility/settings changes; keeping the existing checker awake is insufficient because its current fingerprint excludes WebUI sessions. Verify correct results on reconnect after these changes occur with all clients closed.
- **REQ-007**: Ordinary interactive boot, polling, inventory warming and all-profiles chat refresh execute zero cron/webhook/kanban inventory passes. First activation of the matching view initiates its scoped load; subsequent loads reuse that view's existing cache. Hidden views do not initiate or continue expensive inventory polling.
- **REQ-008**: Preserve session ordering, compression lineage, archived/hidden handling, project ownership, unread/runtime overlays and direct links. A background-only project chip may be deferred with its inventory; ordinary interactive project chips must remain present. Background executions and completion delivery continue normally.

### Constraints

- **CON-001**: State ownership: SQLite and WebUI sidecars remain authoritative persistent state; server caches are derived metadata; browser state is presentation only. Cache refresh must not hydrate every transcript, rewrite session histories or alter run ownership.
- **CON-002**: Reuse current cache registries, generation guards, bounds and watcher lifecycle. Add no dependencies, persistent service, distributed cache or independent inventory framework. Do not modify model catalogs, profile/skill enumeration, Tailscale, Docker or virtual-keyboard policy.
- **CON-003**: Never hold cache/store/stream locks across database scans, network response writes or waits. Each owner releases followers on success, error and shutdown. Old-generation work cannot publish after invalidation or profile removal.
- **CON-004**: Do not simply raise TTLs or remove cache bounds. Retain bounded metadata for the startup profile and a bounded set of admitted additional scopes; evicted scopes may become cold, but client count alone must not cause eviction/rebuilds.
- **CON-005**: Read paths stay read-only with respect to database schema. No index creation, production-state deletion or live cache flushing. No live service restart without explicit operator approval. Preserve pre-existing working-tree edits.
- **CON-006**: MISSING_GUIDANCE: the loaded planning skill exposes no supporting references directory. This is not a repository blocker; define supporting planning guidance in a separate skill-maintenance iteration, not in this performance change.
- **GUD-001**: Follow the [contract routing index](../docs/CONTRACTS.md) (`last_linked: 2026-11-19`) and [change guidelines](../docs/GUIDELINES.md) (`last_linked: 2026-11-19`). Declare intentional changes to independent-fallback and background-discoverability behavior in Contract Change evidence.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Lock down the current call graph and prove the regressions before changing behavior.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Trace current warm/cache identity, mutations, lookup callers, watcher startup/stop and panel activation using FILE-001 through FILE-009. Record exactly which scopes the startup profile needs. Reuse one watcher owner; do not bolt on a second polling loop. Trace background discovery in both single-profile and all-profiles modes and distinguish expensive history from lightweight runtime signals. | Yes | 2026-11-19 |
| TASK-002 | Add failing tests in FILE-010 and FILE-011: a known ID missing from the cached list resolves without enumeration; a slow owner with several followers performs exactly one build at each cache layer; an expired valid snapshot stays readable during refresh. Use barriers/events, not timing sleeps. Replace the existing test that explicitly expects an independent follower rebuild, documenting the intentional contract change. | Yes | 2026-11-19 |
| TASK-003 | Add failing tests for ordinary boot/warm/poll paths invoking zero background inventory passes, and each view's first activation invoking only its own loader. Include all-profiles mode, hidden-panel polling, direct background-session links, empty stores and missing optional stores. Use FILE-012 and existing frontend test conventions. | Yes | 2026-11-19 |

### Implementation Phase 2

- **GOAL-002**: Make known-session metadata resolution bounded and authoritative.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | Update the shared metadata lookup in FILE-001 and point-lookup helpers in FILE-002/FILE-004. Trace the existing `lookup_cli_session_row` and `get_cli_session_by_id` implementations before extending them. Resolve source/owner without enumerating all candidates; preserve normalization and eligibility rules, all-profiles collision handling, excluded-source reachability and foreign-profile denial. Do not trust an ID prefix as ownership. The current `get_cli_session_by_id` passes the exact ID to its primary query but leaves optional background-history passes enabled; explicitly suppress those passes and any other unrelated discovery. Count underlying reader calls in tests so replacing `get_cli_sessions()` with another broad reader cannot satisfy this requirement accidentally. | Yes | 2026-11-19 |
| TASK-005 | Audit every caller of `_lookup_cli_session_metadata`, including session open, import/recovery, Git information and related session-specific operations. Preserve missing/deleted WebUI-sidecar semantics and read-only authorization preflight. Test cached, uncached, archived, compressed and outside-window IDs plus unknown IDs and ambiguous cross-profile IDs. | Yes | 2026-11-19 |

### Implementation Phase 3

- **GOAL-003**: Keep shared interactive metadata warm with exactly one owner of refresh work.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-006 | Repair follower behavior at both existing cache layers in FILE-001/FILE-002/FILE-003. Followers reuse an eligible snapshot or await the live owner within existing request limits. Retry acquisition only after confirmed owner completion/failure. Remove timeout-triggered independent builds. Always clear ownership and signal waiters in `finally`; guard publication with existing generations. Trace CLI read failures through the route response and browser presentation: carry an explicit failure result instead of converting it to a successful empty inventory. At a follower request deadline, use an eligible previous snapshot or the explicit retryable response, without starting a second build or cancelling the live owner. | Yes | 2026-11-19 |
| TASK-007 | Extend the existing watcher lifecycle in FILE-005 and server integration in FILE-006 to warm the startup active profile after state setup and reconcile admitted interactive scopes every five seconds independently of clients. Keep initialization nonblocking, skip unchanged projections, serialize expensive refreshes, coalesce dirty signals and stop/join workers with bounded shutdown. Reuse FILE-003's existing snapshot/cache mechanisms rather than retaining a competing metadata store. Audit and adapt the existing no-subscriber pause, subscriber-dependent publication checks and idle-watcher eviction so admitted warm scopes survive client disconnects. Extend change detection beyond the current WebUI-excluding database fingerprint to cover the saved metadata, settings and profile identities required by REQ-006; do not rely solely on keeping the poll loop awake. | Yes | 2026-11-19 |
| TASK-008 | Route normal source/filter response variants through warmed interactive metadata. Preserve fresh per-response runtime overlays. Audit create, title, archive, delete, project assignment, profile switch/removal and visibility/settings mutations so existing invalidation immediately prevents prohibited stale rows and schedules one refresh. Maintain bounded memory and no eager all-profile scan. | Yes | 2026-11-19 |
| TASK-009 | Test no-client refresh, reconnects, four-client bursts, expiry, invalidation during a blocked build, repeated mutations, failed store reads, startup failure/retry, profile replacement and shutdown. Prove that refresh failure retains only eligible last-good data, never stale authorization or deleted rows, and never leaves waiters/owners stranded. Distinguish genuine empty stores from failed reads; prove failures are not stored as empty successes at either cache layer and the browser offers retry without replacing a valid list with an empty one. Use deterministic barriers for slow-owner deadline tests. | Yes | 2026-11-19 |

### Implementation Phase 4

- **GOAL-004**: Defer background inventory work until requested, without changing execution.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-010 | Thread explicit discovery intent through FILE-001/FILE-002 into FILE-004. Normal interactive inventory must disable the existing cron/webhook/kanban passes, not load then filter them. Use the existing disabled-pass mechanism (`False` where supported); `None` currently means unbounded for those history limits. Include intent in relevant cache identities and apply the same rules to all-profiles requests. | Yes | 2026-11-19 |
| TASK-011 | In FILE-007/FILE-008/FILE-009, reuse existing panel activation hooks for first-load demand. Audit boot/resume/profile-switch/SSE/poll handlers for hidden inventory fetches. Cancel hidden-view timers or suppress their inventory requests; discard late responses after scope/view changes. An already in-flight read need not be forcibly aborted if its result cannot repaint an unrelated view. Preserve lightweight liveness needed for existing completion/unread behavior without scanning histories. | Yes | 2026-11-19 |
| TASK-012 | Verify each view still exposes its intended history, project assignment and existing pagination/limits, including all-profiles mode. Do not silently truncate history to optimize it. Background session direct links use TASK-004's point lookup without triggering other inventories. Do not stop jobs, webhooks or kanban workers when a view closes. | Yes | 2026-11-19 |

### Implementation Phase 5

- **GOAL-005**: Verify observable latency and correctness, document behavior and leave deployment separately gated.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-013 | Run TEST-001 through TEST-007 in isolated state and capture actual red-before/green-after results, neighboring coverage and browser request traces. Re-run exact commands after determining additional affected tests in TASK-001. Do not claim external client performance from mocks. | Partial: local checks complete; public ingress pending | 2026-11-19 |
| TASK-014 | Update FILE-013 and FILE-014 with the warm metadata lifecycle, startup cold interval, per-view background discovery and manual verification. Include Contract Routing and intentional Contract Change notes in the handoff. Do not edit the changelog. Report deployment pending; any later authorized restart must separately verify origin and remote behavior. | Yes | 2026-11-19 |

## 3. Alternatives

- **ALT-001**: Increase TTLs only. Rejected: does not prevent duplicate cold builds or catalog enumeration during known-session lookup, and can delay mutation visibility.
- **ALT-002**: Warm every profile and all background histories on startup. Rejected: expands startup I/O and scope; violates on-demand background loading.
- **ALT-003**: Replace caches with a new database/materialized persistent index or distributed service. Rejected: unnecessary for a single-process metadata problem and adds migration/consistency obligations.

## 4. Dependencies

- **DEP-001**: Existing Python/vanilla-JavaScript architecture, cache registries, invalidation generations, point readers and watcher startup/stop. No new package is required.
- **DEP-002**: Isolated fixture state and the repository test runner. Use [testing guidance](../TESTING.md) (`last_linked: 2026-11-19`); no production credentials or backing services in automated tests.
- **DEP-003**: Actual panel/source semantics must be traced before implementing TASK-010/011. Some panels already lazy-load; preserve that machinery and fix only remaining eager history work.

## 5. Files

Every path below exists at plan creation. Line numbers are orientation only; re-resolve symbols before editing.

- **FILE-001**: [API routes](../api/routes.py) (`last_linked: 2026-11-19`): sidebar builder near 3008, route cache coordination near 3537, `_lookup_cli_session_metadata` near 10217 and session GET near 16006.
- **FILE-002**: [Session models/discovery](../api/models.py) (`last_linked: 2026-11-19`): inner cache coordination near 10230, point/cache lookups near 10413/11848, optional source passes near 11641/11712/11780 and all-profiles limits near 11939.
- **FILE-003**: [Route session-list cache](../api/route_session_list_cache.py) (`last_linked: 2026-11-19`): shared snapshots, identities, invalidation, bounds and runtime overlays.
- **FILE-004**: [Agent session readers](../api/agent_sessions.py) (`last_linked: 2026-11-19`): read-only queries and source/profile-scoped point resolution.
- **FILE-005**: [Gateway watcher](../api/gateway_watcher.py) (`last_linked: 2026-11-19`): existing fingerprint, background lifecycle and notifications; extend without duplicate scans or background history expansion.
- **FILE-006**: [Server lifecycle](../server.py) (`last_linked: 2026-11-19`): startup watcher integration near 647 and shutdown near 724.
- **FILE-007**: [Session browser logic](../static/sessions.js) (`last_linked: 2026-11-19`): list query, refresh and known-session open flow.
- **FILE-008**: [Panel browser logic](../static/panels.js) (`last_linked: 2026-11-19`): existing activation loaders and cron/kanban lifecycle.
- **FILE-009**: [Browser boot](../static/boot.js) (`last_linked: 2026-11-19`): audit eager/resume work; already locally modified, so preserve unrelated edits.
- **FILE-010**: [Sidebar cache tests](../tests/test_session_sidebar_cache.py) (`last_linked: 2026-11-19`) and [CLI streaming cache tests](../tests/test_issue4842_cli_cache_streaming.py) (`last_linked: 2026-11-19`): regression ownership/expiry tests and intentional independent-fallback expectation change.
- **FILE-011**: [Cached metadata authority tests](../tests/test_metadata_cached_authority.py) (`last_linked: 2026-11-19`) and [cache fingerprint tests](../tests/test_cli_sessions_cache_fingerprint.py) (`last_linked: 2026-11-19`): lookup consistency and identity/invalidation neighbors.
- **FILE-012**: [Cron project-session tests](../tests/test_issue3019_cron_project_sessions.py) (`last_linked: 2026-11-19`), [webhook project-session tests](../tests/test_webhook_project_sessions.py) (`last_linked: 2026-11-19`) and [project-assigned CLI limits tests](../tests/test_project_assigned_cli_session_limit.py) (`last_linked: 2026-11-19`): source inclusion, history reachability and project-chip neighbors. Add narrowly named lifecycle/frontend tests if these existing suites do not cover the required boundaries.
- **FILE-013**: [README](../README.md) (`last_linked: 2026-11-19`): concise user-facing loading behavior; preserve pre-existing edits.
- **FILE-014**: [Testing manual](../TESTING.md) (`last_linked: 2026-11-19`): reproducible warm/cold and multi-client validation.

## 6. Testing

- **TEST-001**: RED/GREEN deterministic regressions: known-ID misses must perform zero global enumeration calls; blocked cold builds with four matching readers must invoke one builder at each cache layer; warm readers complete before a blocked refresh is released. Prove current code fails the relevant tests before implementation.
- **TEST-002**: Run the affected/neighboring Python suite through the repository runner with isolated state. From the repository root, use the following shell setup and command; the scratch root must exist and be writable. The repository runner owns the test interpreter/dependency setup. Verify fixtures do not reach real state, live credentials or external services before execution.

```sh
trial_root=$(mktemp -d "${TMPDIR%/}/webui-inventory-tests.XXXXXX")
export HERMES_HOME="$trial_root/home"
export HERMES_BASE_HOME="$trial_root/base"
export HERMES_CONFIG_PATH="$trial_root/home/config.yaml"
export HERMES_WEBUI_STATE_DIR="$trial_root/webui"
mkdir -p "$HERMES_HOME" "$HERMES_BASE_HOME" "$HERMES_WEBUI_STATE_DIR"
./scripts/test.sh -q tests/test_session_sidebar_cache.py tests/test_issue4842_cli_cache_streaming.py tests/test_cli_sessions_cache_fingerprint.py tests/test_metadata_cached_authority.py tests/test_session_list_cache_bounded.py tests/test_streaming_cache_ttl_vs_poll.py tests/test_issue3019_cron_project_sessions.py tests/test_webhook_project_sessions.py tests/test_project_assigned_cli_session_limit.py
```

- **TEST-003**: Add/run watcher start/stop, invalidation and point-lookup neighboring suites identified in TASK-001. Exercise success/error/shutdown/replacement, empty/missing stores, cache bounds, cross-profile identical IDs, visibility denial and generation races. Count underlying exact-ID reader and optional history calls: opening cached, uncached and outside-window sessions must perform zero unrelated cron/webhook/kanban inventory reads, including through `get_cli_session_by_id`. Test an actual empty store separately from read failure and owner timeout; only the successful empty read may populate an authoritative empty snapshot. Verify the retryable response and browser retry action, and prove a timed-out follower starts no duplicate build. No wall-clock performance assertions as correctness substitutes.
- **TEST-004**: Run `node --check static/sessions.js`, `node --check static/panels.js`, `node --check static/boot.js` and `git diff --check`. Run any added frontend harness through its repository-established runner.
- **TEST-005**: In an isolated server with representative synthetic histories, open four authenticated browser contexts against the same scope. Record request waterfalls at desktop and narrow/mobile widths. Normal boot and chat refresh must make zero background inventory queries. Each background view activates only its own loader, profile switches cannot repaint foreign data, and direct links render the requested conversation. Capture before/after evidence; do not introduce keyboard compensation.
- **TEST-006**: Instrument builder/read invocation counts and verify: interactive startup warm occurs once; ten warm reads and four concurrent clients cause no new discovery solely from client count; a mutation coalesces refresh work; an externally added CLI session appears within one five-second detection interval plus refresh execution time. With all clients closed, the startup profile still reconciles. With all clients closed, also rename and delete sessions, edit WebUI sidecar metadata, change project/visibility settings and replace a profile database; reconnect and verify the expected list without foreign or deleted rows. Assert the applicable five-second detection interval plus refresh execution time. Measure idle CPU usage, disk-read activity and expensive scan counts on unchanged representative stores before/after, reporting the observation duration and number of admitted scopes so the cost of continuous warming is visible. Memory remains within existing scope/entry bounds.
- **TEST-007**: Benchmark authenticated identical sidebar, session-metadata and 30-visible-row transcript requests, separating initial cold build from warm reads and background refresh. Alternate local and forced-public-ingress order; keep credentials out of logs/artifacts. Target warm loopback sidebar/metadata p95 below 100 ms over 20 reads on an otherwise comparable host; report host contention instead of weakening correctness gates. The primary deterministic gate is zero synchronous discovery for warm reads/known-ID resolution, not a promised remote latency. Do not flush live caches or restart production to manufacture cold results.

### Evidence baseline (diagnosis, not implementation verification)

- November 18–19 slow-request logs: 35 slow sidebar requests, median 15.13 s and maximum 57.60 s; CLI discovery accounted for 80.4% of their combined elapsed time. These are threshold-selected slow requests, not all traffic.
- The 35.1 s session-open watchdog showed CLI metadata fallback enumerating sessions and blocking at SQLite candidate selection. The diagnostic stage label spans multiple operations; do not attribute the entire delay to transcript hydration.
- Three alternating warm rounds: loopback medians 9 ms sidebar / 18 ms metadata / 45 ms transcript; forced public Funnel medians 844 / 466 / 742 ms. All returned 200. Browser session selection was exercised on both origins; local transcript rendering was verified. This is not an iPad network/rendering benchmark and did not reproduce the historical cold stalls.

## 7. Risks & Assumptions

Assumptions ASM-001 through ASM-003 are stated before the requirements; there are no additional assumptions.

- **RISK-001**: Serving old metadata can resurrect deleted or unauthorized rows. Mitigation: preserve complete cache identity, generation checks, mutation overlays/tombstones and immediate scope invalidation. Never treat a stale snapshot as authorization.
- **RISK-002**: Moving discovery into the background can merely relocate a heavy scan or block shutdown. Mitigation: one existing lifecycle owner, cheap change checks, no overlapping work, bounded admission/retries, stop event and no I/O under registry locks; measure invocation counts with no clients attached.
- **RISK-003**: Removing hidden background inventories can break project chips, direct links or completion indicators. Mitigation: explicit source intent, direct point resolution, separate lightweight runtime signals, and single/all-profile view tests. Do not disable execution to suppress UI loading.

## 8. Related Specifications / Further Reading

- [Repository agent instructions](../AGENTS.md) (`last_linked: 2026-11-19`): local state safety, test runner and no-compensation deployment policy.
- [Contribution requirements](../CONTRIBUTING.md) (`last_linked: 2026-11-19`): scoped changes and review evidence.
- [Run-state consistency contract](../docs/rfcs/webui-run-state-consistency-contract.md) (`last_linked: 2026-11-19`): derived sidebar state, read-only ownership preflight and persistent/runtime authority. Proposed portions are guardrails, not extra implementation scope.
- [Architecture](../ARCHITECTURE.md) (`last_linked: 2026-11-19`): current module boundaries; preserve Python plus vanilla JavaScript architecture.
