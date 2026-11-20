# Warm session inventory: local implementation evidence

Date: November 19, 2026 (operator time zone).

## Goal

Implement version 1.1 of the warm-session inventory plan after committing the
existing local fork as checkpoint `e5bb5804a`. Improve shared session loading,
exact conversation access and on-demand background histories. Deployment and
live service restart were not part of this implementation.

## Key decisions and reasoning

- Keep metadata in the existing bounded route cache. Admission records contain
  scope/options only, so there is no competing session store or new service.
- Extend the existing watcher owner to reconcile without browser subscribers.
  Stat-based checks include sidecars/settings and replaced identities because
  the original watcher fingerprint did not cover those inputs.
- Separate common metadata preparation from response filtering. Prepare the
  union of the existing top-N lineage selections so newer hidden/archived rows
  cannot consume a visible tab's metadata allowance.
- Use complete inventory reads for shared snapshots; preserve legacy uncached
  partial results for other additive model callers. Include that distinction in
  cache identity to prevent an incomplete result being reused as a complete one.
- A detected external input change forces a fresh derived CLI projection,
  including during stream hold-down. A failed/slow read cannot publish an empty
  success or start a second simultaneous owner. Existing valid common metadata
  remains available when its identity/source still permits it.
- Resolve known IDs directly and disable unrelated history passes, including
  inside the existing exact-ID reader. Preserve authorized profile-context order
  for duplicate IDs and preserve the existing direct-link ownership checks.
- Default chat and SSE use interactive discovery. Background project views opt
  into their own history and keep the existing single/all-profile limits.
  Hidden panels reject late responses and do not restart inventory polling.
  Lightweight completion delivery and job execution retain their existing paths.
- The operator declined the additional recommendation requiring zero persistent
  writes during warming. That requirement was not added. Existing metadata
  reconciliation/cleanup remains in use; no new schema maintenance was added.

## What changed

- `api/models.py`, `api/agent_sessions.py` (existing reader reused, unchanged):
  exact-ID resolution, scoped rebuild ownership, complete-read policy and explicit
  source intent. Claude Code identifies the matching file before parsing it.
- `api/routes.py`, `api/route_session_list_cache.py`: shared bounded metadata,
  filter reuse, scope admission, generation checks and retryable cold responses.
- `api/gateway_watcher.py`, `server.py`: nonblocking startup, no-client refresh,
  reuse of shared metadata for watcher notifications and bounded teardown.
- `static/sessions.js`, `static/panels.js`: background-project intent and
  profile/view-safe lazy loading. Existing error/Retry UI is reused.
- New regression/browser fixtures, related contract tests, README, TESTING and
  the [session inventory contract](../docs/architecture/warm-session-inventory.md).
  No changelog, model-catalog, deployment or virtual-keyboard changes.

## Contract routing and changes

State layer: derived session metadata in existing server caches, plus browser
presentation of those lists. SQLite/sidecars and existing run ownership remain
authoritative. Relevant guidance: AGENTS.md, docs/CONTRACTS.md,
docs/GUIDELINES.md, the run-state consistency and canonical-session-resolution
RFCs, and the new session inventory contract.

Previous contracts intentionally changed:

1. A cold follower deadline could authorize an independent rebuild. It now
   produces eligible prior data or a retryable failure while the owner continues.
2. A metadata miss could enumerate the recent sidebar list, and an older row
   outside that list could appear nonexistent. Exact-ID reads now keep it reachable;
   tests/plugins should override the exact reader rather than the enumeration.
3. Ordinary sidebar/SSE loading could discover hidden background histories.
   Histories now require the corresponding view's explicit request.
4. Hidden Tasks could start another list refresh after navigation. It now waits
   for activation, and old in-flight results cannot repaint the incoming view.

Corresponding regression expectations and public documentation were updated.

## Validation

The repository runner was used with fresh temporary HERMES_HOME,
HERMES_BASE_HOME, HERMES_CONFIG_PATH, HERMES_WEBUI_STATE_DIR and
HERMES_WEBUI_TEST_STATE_DIR values. Fixtures do not use live credentials or
backing services. The initial seven targeted regressions failed before the
implementation. The final combined affected/neighboring run passed **416 tests**,
covering the new contracts, cache concurrency/invalidation, real SQLite exact-ID
reads, source limits, profile ownership, watcher lifecycle, client retry,
cron refresh races/completion scopes and Kanban neighbors.

Commands are reproducible from [TESTING.md](../TESTING.md#shared-warm-session-inventory).
Additional checks: Python compilation, `node --check` on sessions.js/panels.js/
boot.js, and `git diff --check` passed. Ruff passed on the new/standalone changed
files. Comparing Ruff findings in the large existing modules with checkpoint
`e5bb5804a` found no new lint findings; pre-existing findings remain.

`tests/test_metadata_cached_authority.py` has five pre-existing failures involving
`msgContent` missing from its Node regeneration harness. The same five failures
were reproduced against the checkpoint's unmodified sessions.js/ui.js/messages.js
in a disposable fixture (**57 passed, 5 failed**). They are separate from the 416
passing checks and were not changed in this performance task.

The browser probe uses real SQLite/sidecars, real server handlers and four
independent authenticated Chromium contexts at desktop/narrow viewport sizes.
Instrumentation wraps actual readers; it does not replace their results. Both
server/browser outbound network access is blocked. Data consists of 80 WebUI,
120 CLI and 300 sessions from each background source, with 30 messages each.
Each measured endpoint receives 20 reads. The temporary metrics endpoint exists
only in the fixture launcher, not the application.

## Browser measurements

The checkpoint and implementation were run sequentially on the same host with
identical synthetic data and four authenticated contexts. All measured endpoint
responses returned 200. Times include local browser HTTP overhead; this is not a
remote-device benchmark.

| Endpoint | Checkpoint median / p95 | Implementation median / p95 |
|---|---:|---:|
| sidebar | 8.3 / 12.2 ms | 10.7 / 16.2 ms |
| metadata | 36.8 / 44.4 ms | 27.3 / 31.3 ms |
| transcript | 37.2 / 42.9 ms | 28.7 / 35.3 ms |

Deterministic observations:

- CLI inventory builds observed by the end of boot: two in each run. The
  implementation uses one owner and permits bounded follow-up work when inputs
  change during preparation; an unchanged fixture is separately tested to build
  once. The four clients did not create independent simultaneous builders.
- Ordinary boot plus 60 warm endpoint reads: zero additional inventory builds
  after implementation. Known cached metadata reads also avoided exact SQL
  projections; the old out-of-window direct link still returned 200.
- Background history passes before activation: two per source at the checkpoint,
  zero per source after implementation. Opening Cron Jobs then Webhooks invoked
  one pass for each respective source. Both retained their 200-row fixture history
  limit. Kanban activation requested its own boards/config endpoints; the optional
  Kanban backend was unavailable in this fixture and did not cause other histories
  to load.
- With browsers closed and the store unchanged for 11 seconds: zero expensive
  inventory builds or history reads. Process CPU time increased by approximately
  0.066 seconds; reported block-input operations stayed at zero.
  File-stat checks still run; cached block I/O counters do not measure every
  filesystem access or whole-machine power use.
- An external SQL rename and insertion were visible on reconnect after the
  six-second no-browser reconciliation wait. This checks the five-second cadence
  plus execution allowance, not a hard end-to-end remote deadline.
- Desktop and narrow screenshots, request paths, counters and server logs were
  captured by the disposable browser probe. Its output locations are runtime
  artifacts, not application state.

## Current state and remaining validation

Local code is implemented. The browser probe verifies no history passes before
activation, shared startup preparation, no further inventory builds from warm
reads, direct access to an old CLI session, source-specific history on first use,
no expensive rebuilds during an unchanged 11-second no-browser interval, and an
external database update visible on reconnect. Its measured CPU/block-input
counters describe this synthetic process, not whole-machine energy use.

These fixtures do not reproduce the historical 15–57-second production stalls.
Already-warm sidebar requests can be slightly slower because response filters
are applied to the shared metadata on each read. Warm local latency and scan
counts are reported separately; no remote speedup is claimed from fixture timing.

Public-ingress timing, an actual live restart and physical iPad/iPhone behavior
remain unverified. There was no deployment, live cache flush, database maintenance
or service restart as part of validation. Existing cleanup behavior was exercised
only against disposable state.

## Next steps

1. Review the implementation and local evidence.
2. If separately authorized, restart/deploy through the existing operator path.
3. Verify origin/public-ingress behavior and physical devices after deployment,
   recording cold preparation separately from warm reads without flushing live
   state or weakening the correctness checks.
