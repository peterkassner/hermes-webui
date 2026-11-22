# Active-chat sidebar cache availability

November 20, 2026. The comparison uses local fork checkpoint `3b9f7be39`,
the working-tree correction, and disposable authenticated localhost servers.

## Incident and contract change

The earlier live observation recorded 11 sidebar 503s in roughly 322 seconds,
including failures while a real chat was active. Initial warming had succeeded.
The old watcher removed the shared list on a file change. Its builder then had
three opportunities to find all files unchanged across the build. A missing
list had a 250 ms request budget. That combination made ordinary writes capable
of producing recurring cold responses after successful startup.

Contract routing follows the [warm inventory contract](../docs/architecture/warm-session-inventory.md),
[contract index](../docs/CONTRACTS.md) and [change guidelines](../docs/GUIDELINES.md).
An ordinary append now leaves the published list eligible. Reconciliation can
publish the consistent data it read, even if another message has arrived.
SQLite and session files own the durable data. An older candidate loses the
right to publish when the admission instance or safety generation changes.

## Mutation and reader audit

| Change | Effect on an admitted interactive list |
| --- | --- |
| Ordinary database or sidecar content, new/duplicated/branched conversations, attention and cron completion | Keep eligible rows available. The next watcher pass handles outstanding content. |
| Committed rename, archive, pin or project move | Use the owning session's current field when available, patch the affected row and fence older work. A missing trusted value falls back to conservative invalidation. |
| Committed delete | Remove the identified row from affected scopes. An unresolved owner uses conservative invalidation. |
| Share/access, settings and unclassified legacy clears | Withdraw affected scopes. |
| External change in a row's existence, ownership, archive state or source | Compare sidecar/index metadata and indexed SQL reads for published and candidate IDs. Withhold affected rows or the affected scope until a verified replacement exists. |
| Home or database replacement, inability to verify ownership | Refuse the old identity. Ordinary WAL rotation and unrelated home heartbeats preserve it. |
| Eviction, stop, repeated dirty signals, slow or failed rebuild | Per-instance fencing and the existing rebuild registries bound concurrent work and release followers. One scope's slow build runs independently of the watcher. |

The builder borrows related SQL readers from a read-only transaction per
participating database. Its context owns their cleanup on every exit. It checks
the candidate's IDs again after closing those views. The strict warm path uses
the inner CLI owner's slot and publishes through the outer cache. It never
promotes an independently cached fallback into a fresh outer result. Fingerprints
capture the inputs at the start of work so later ordinary writes remain due.
An external change after the final safety check is observable on the subsequent
reconciliation pass. Local committed events fence publication immediately.

## Deterministic and neighboring tests

Before editing the implementation, both activity regressions failed. A changed
title triggered a 503 during a blocked build. Three atomic heartbeat replacements
triggered three extra projections. The corrected tests check the prior title
during the blocked read, the intermediate title after publication, and the later
title on the following refresh. The heartbeat-only case performs zero extra
projections. Additional tests cover candidate-only deletion during a real WAL
read, connection cleanup, same-ID ownership, archive and store replacement,
admission churn, cross-scope safety checks and stale resident/index metadata.

Run the affected tests and the browser commands in [TESTING.md](../TESTING.md#active-chat-sidebar-availability).
The 15 suites listed in TEST-003 of the [implementation plan](cache-availability-webui-v1.md)
passed all **249 tests**. The expanded 26-suite run passed **412 of 417**.
All five failures reproduced in an archive of the unmodified checkpoint using
the same Python environment. Two are the creation/reopen cases for recompressed
reset children. Three concern messaging-session grouping by user, group chat or
thread in `tests/test_gateway_sync.py`. The standalone checkpoint Gateway run
also failed a sixth gateway-metadata chat-identity case that passed in the
expanded corrected run.

The idle browser probe also verified old-session opening, no-client external
rename and insertion, zero background history passes before explicit selection,
and no new CLI builds during the unchanged 11-second interval.

## Browser measurements

The fixture appends a synthetic message, changes a sidebar title and commits
roughly every 100 ms for 120 seconds per mode. It atomically replaces a
heartbeat file during the run. Four authenticated Chromium contexts poll
independently every 250 ms. Each refresh delay and injected failure occurs
inside the disposable server. The clients also exercise the normal sidebar
render path. Each activity evidence file records request status and duration,
rendered titles, skipped polls, write times, peak WAL size and server counters.

| Revision and activity | HTTP 200 | HTTP 503 | Skipped polls | Committed writes | p95 / maximum response |
| --- | ---: | ---: | ---: | ---: | ---: |
| Checkpoint, external | 139 | 1,380 | 397 | 1,138 | 27 / 270 ms |
| Checkpoint, stream | 1,872 | 20 | 24 | 1,134 | 22 / 266 ms |
| Corrected, external | 1,916 | 0 | 0 | 1,135 | 22 / 44 ms |
| Corrected, stream | 1,916 | 0 | 0 | 1,135 | 22 / 58 ms |

- **External writes.** 23 replacements published during activity. The four clients rendered 23, 23, 23, 23 successive title values. The final title reached the API 0.62 seconds after the last commit. The largest per-client delay from a sampled title's commit to its first rendered observation was 1.04 seconds. Peak WAL was 3.94 MiB, peak server RSS 166.0 MiB, and the longest measured read-view scope 2.08 seconds (an upper bound on transaction lifetime, including the injected delay).
- **Stream writes.** 23 replacements published during activity. The four clients rendered 23, 23, 23, 23 successive title values. The final title reached the API 0.80 seconds after the last commit. The largest per-client delay from a sampled title's commit to its first rendered observation was 0.80 seconds. Peak WAL was 3.94 MiB, peak server RSS 86.0 MiB, and the longest measured read-view scope 2.03 seconds (an upper bound on transaction lifetime, including the injected delay).

Each corrected run recorded one build owner at peak, one injected content
failure, successful reads from all clients during the slow refresh, and no
fabricated empty responses. After the activity interval, an authenticated
browser switched to a cold second profile, received the loading error without
displaying the previous profile's row, and recovered using the Retry button.

Raw `activity.json` files are generated under the chosen output directory for
both modes. Timings include the local browser and HTTP client. The fixture
measures a synthetic store, so its timings describe local behavior.

The final idle interval used 0.012 seconds of process CPU, with zero reported
block inputs and zero new publications or CLI builds over 11 seconds. The
old-conversation read succeeded and the cron and webhook views each retained
their 200-row limit. Python parsing and whitespace checks passed. Ruff reported
the same 16 existing diagnostics across the changed Python files as the
checkpoint, with no additional rule/message/file findings.

## Later live verification

1. Obtain operator authorization for the restart. Check current active runs and streams with the existing service controls, and preserve ongoing chats.
2. Record the restart time and the first healthy process sample. Sample authenticated system health until the first eligible publication without using list reads to warm the service.
3. Observe at least 322 seconds and include sustained real writes. Record per-client sidebar attempts, errors, response times and visible updates alongside publication, refusal, cold-wait and refresh-failure counters.
4. Require zero failures for an already-warm eligible sidebar and evidence of publication during writes. Record cold starts, profile changes and unrelated upstream errors separately. If no active work occurs, the live activity check remains pending.

Deployment, public ingress and physical-device behavior require separate verification.
