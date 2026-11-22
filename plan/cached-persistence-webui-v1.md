---
goal: Preserve live Agent persistence across repeated WebUI turns and verify it alongside the pending sidebar update.
version: "1.0"
date_created: 2026-11-22
last_updated: 2026-11-22
last_linked: 2026-11-22
status: Completed
---

# Introduction

A second message in the same chat reuses the in-process Agent. The fork currently treats a missing SQLite connection attribute as evidence that the Agent’s storage is closed. With the installed Agent, that can close storage still in use and leave later turns pointing to it. Backport the reviewed four-file change from upstream, prove it against the installed Agent, then include it in the same WebUI deployment as the active-chat sidebar correction.

## 1. Requirements & Constraints

- **ASSUMPTION-001** — Planning baseline is WebUI HEAD `3b9f7be39` plus pending local edits and Agent HEAD `f0bce1c77c`. A read-only `git apply --check` of the full upstream patch succeeded against this working tree on November 22. Repeat the check at implementation time.
- **REQ-001** — Reuse live storage across turns, release the unused per-request handle exactly once and preserve borrowers such as the compressor and active background consumers.
- **REQ-002** — Support both legacy SQLite handles and the installed Agent’s newer interfaces. Checking liveness must leave a lazy backend unopened.
- **CON-001** — Scope implementation to WebUI. Preserve local edits, use disposable homes and databases for tests, and reserve the production restart for the combined deployment.

## 2. Implementation Steps

### Implementation Phase 1

**GOAL-001** — Reproduce the failure and backport the exact reviewed change.

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-001 | Record both repository revisions and WebUI staged, unstaged and untracked files. Download the patch from REF-001 at the pinned commit. Run `git apply --check <patch-file>` from the fork root. Inspect any drift before applying. | Yes | 2026-11-22 |
| TASK-002 | Apply only the new regression file with `git apply --include=tests/test_issue9149_cached_persistence.py <patch-file>`. Run TEST-001 on the original implementation. Record the failing live-handle and stale-binding assertions, plus any skipped installed-Agent cases. | Yes | 2026-11-22 |
| TASK-003 | Apply the remaining hunks with `git apply --exclude=tests/test_issue9149_cached_persistence.py <patch-file>`. In FILE-001 verify the liveness check handles legacy `_conn`, shim `_closed` and `_store`, and direct lazy persistence with `_release`. Unknown shapes are unusable. Preserve deferred release when a shared call is still in flight. | Yes | 2026-11-22 |
| TASK-004 | Trace normal cached reuse and the credential-recovery paths. Retained constructor kwargs must reference the adopted handle. On replacement, update the Agent’s session DB and persistence reference and rebind compressor state, forwarding the persistence keyword only to a compatible hook. Confirm that failed creation preserves an existing live handle. | Yes | 2026-11-22 |
| TASK-005 | Review the contract and existing close-test changes against REF-001. Run TEST-001 through TEST-003 on the combined working tree. Commit the four upstream files together with source attribution. Review and commit the intended pending WebUI edits separately so the release contents and rollback point are identifiable. | Yes | 2026-11-22 |

### Implementation Phase 2

**GOAL-002** — Deploy the verified WebUI changes together and check both symptoms through the running service.

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-006 | Before deployment, record the release manifest, current listener PID, active streams and a restorable copy or revision of the previously running WebUI source and configuration. Account for previously uncommitted runtime edits. Let active turns finish. Use the existing WebUI service control for one combined restart. Verify a new listener, successful health response and authenticated access. | Yes | 2026-11-22 |
| TASK-007 | Run TEST-004, recording the session ID, completed turns, persisted history and sidebar observations. If a release regression appears, preserve the error and timestamps, drain active turns, restore the recorded WebUI release and verify authenticated chat after restart. | Yes | 2026-11-22 |

## 3. Alternatives

**ALT-001** — Changing only the liveness predicate would leave stale Agent, compressor and credential-recovery references on genuine replacement. The upstream backport covers all three.

**ALT-002** — An upstream release upgrade would bring additional changes into a fork with pending local work. The reviewed fix already applies on its own.

## 4. Dependencies

**DEP-001** — Use the repository test runner and make the installed Agent checkout importable for its real-storage cases. Verify the module paths and inspect skip reasons. The upstream pass counts describe a different checkout.

## 5. Files

| ID | Patch contents |
|---|---|
| FILE-001 | [Streaming and cached-agent adoption](../api/streaming.py) |
| FILE-002 | [Agent compatibility contract](../docs/architecture/agent-api-contract.md) |
| FILE-003 | New regression suite at `tests/test_issue9149_cached_persistence.py`, supplied by [the pinned patch](https://github.com/nesquena/hermes-webui/commit/45c09d987628b1753469874ff6b7b08999f2786d.patch) |
| FILE-004 | [Existing descriptor-leak and close regressions](../tests/test_v050259_sessiondb_fd_leak.py) |

## 6. Testing

- **TEST-001** — From the WebUI root run `./scripts/test.sh tests/test_issue9149_cached_persistence.py -q -rs` before and after the production hunks. Require the new suite to pass after the patch, including the real lazy-handle, deferred-close and Agent storage cases. A dependency skip leaves that case unverified.
- **TEST-002** — Run `./scripts/test.sh tests/test_v050259_sessiondb_fd_leak.py tests/test_issue3548_sessiondb_self_heal.py tests/test_issue2965_streaming_sessiondb_profile_home.py tests/test_streaming_agent_cache_lifecycle.py tests/test_issue1897_profile_switch_agent_cache.py -q -rs` and `git diff --check`. Also rerun the sidebar tests and four-client activity fixture documented in REF-003 on the combined candidate. Require zero warm sidebar failures and visible updates during writes.
- **TEST-003** — With disposable storage, the installed Agent and a loopback provider fixture, exercise the WebUI adoption helper between three sequential turns on the same cached Agent. Capture baseline turn-two failure and candidate success. Check the provider received each candidate turn, exactly three user and three assistant messages persisted in order, and no turn lease remains. The upstream PR describes this harness separately from its committed pytest suite. Recreate it locally if needed.
- **TEST-004** — After the combined restart, send three successive messages in one authenticated WebUI chat. Confirm each completes, refresh the page, verify all six messages in order and inspect the matching logs for persistence errors. Also follow REF-003’s live cache check for at least 322 seconds with sustained writes. Record eligible warm sidebar request failures and visible updates. A quiet interval leaves the active-write check pending.

## 7. Risks & Assumptions

ASSUMPTION-001 identifies the checked baseline. The remaining risks affect verification and release.

- **RISK-001** — Agent versions expose different lifecycle fields. Exercise the legacy fixtures and the actual installed runtime.
- **RISK-002** — A fresh Agent per turn can mask this defect. Check object reuse in the isolated test and reuse the same chat ID in the live test.
- **RISK-003** — The combined release includes local edits made at different times. Capture the intended files and prior running state before restart so rollback preserves operator changes.

## 8. Related Specifications / Further Reading

- **REF-001** — [Upstream commit and four-file diff](https://github.com/nesquena/hermes-webui/commit/45c09d987628b1753469874ff6b7b08999f2786d) and [PR #9150 including acceptance evidence](https://github.com/nesquena/hermes-webui/pull/9150) (last_linked: 2026-11-22).
- **REF-002** — [Original later-turn failure report](https://github.com/nesquena/hermes-webui/issues/9149) (last_linked: 2026-11-22).
- **REF-003** — [Sidebar test commands](../TESTING.md#active-chat-sidebar-availability) and [prior cache evidence and live acceptance criteria](cache-availability-webui-evidence.md) (last_linked: 2026-11-22).

## Local implementation and test results, November 22

Persistence backport `3104d630f`. Sidebar correction `2dbbc9b67`. The original 16-case regression suite produced 13 failures and 3 passes against the unpatched implementation. The combined tree passed all 293 focused persistence and sidebar tests and all 83 additional neighboring tests. The installed Agent source was at `f0bce1c77c`. The Python 3.12 WebUI test environment required the Agent YAML dependency, ruamel.yaml 0.18.16, to exercise real storage.

The loopback-provider test reused one real Agent. The original code failed on turn two before the provider received a second request. The patched code completed all three requests, persisted the six expected messages in order, and released every turn lease.

The four-client browser runs each lasted 120 seconds with slow and failed refreshes injected. Both also passed the cold-profile retry check.

- external: 1,916 successful reads, 1,133 writes, observed title counts per client [22, 22, 22, 22].
- stream: 1,916 successful reads, 1,131 writes, observed title counts per client [23, 23, 23, 23].

## Combined deployment and live verification, November 22

The launchd restart replaced PID 74901 with PID 75935 after zero active runs and streams. Loopback health returned in 158 seconds and the cache published its first session list in 161 seconds. The live in-process chat completed three consecutive turns. Chromium reloaded the conversation and displayed each expected reply. The core database contained six alternating messages and zero outstanding turn leases. The verification chat was archived.

Two authenticated Chromium clients observed 39 session-title writes across 326.57 seconds, including 55 samples with an active stream. Each received 406 successful sidebar responses and displayed 38 successive titles. Cache publications advanced from 1 to 44. The service served 240 reads during refresh with zero refresh failures. The measured 95th-percentile sidebar request took 43 ms.

All 376 tests selected for the fixes passed. The separate 73-test gateway-sync suite had five failures. All five also failed on the pre-fix baseline, where a sixth case failed. Two current failures involve the removed Agent batch-append method during reset-child checks. Three involve distinct Telegram conversations being grouped together. The initial verification script used an incorrect history-query parameter and returned HTTP 400. The independent reload check used the browser’s canonical history endpoint and passed.
