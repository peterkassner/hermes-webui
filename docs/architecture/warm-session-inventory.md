# Shared warm session inventory

The server prepares interactive session metadata once for matching clients. The
startup profile is prepared asynchronously, without waiting before opening the
listening socket. The existing Gateway watcher checks admitted scopes every five
seconds, even with no browsers connected. Additional scopes are admitted when
requested; startup does not scan every profile.

SQLite and session sidecars remain authoritative. The existing route cache holds
bounded, copy-safe metadata rather than transcripts. Its existing 64-entry bound
remains in place. Up to eight admitted scopes retain their options, source revisions and compact
eligibility evidence. The route cache owns the only published list. The startup scope stays admitted.
Other scopes can become cold after eviction.

## Identity, filtering and freshness

Identity includes the active profile and resolved home, all-profiles mode,
visibility/source settings, Claude Code inclusion and discovery intent. Archive,
hidden-row, pagination and sidebar-tab filters reuse the common prepared metadata.
Lineage metadata for the existing top-N selections is prepared with it. Runtime
stream/pending/unread-related flags retain their existing response-time overlay.

A warm request immediately uses the last eligible list, including a valid
empty list. The background checker detects ordinary database/WAL, sidebar
index and session-file edits while browsers are connected or disconnected.
Unrelated files atomically replaced in the profile home do not trigger a build.
Matching clients share one build. A content failure leaves eligible data
available. A slow build in one profile leaves the watcher free to check another.

Each builder captures its input revision and reads the related SQLite projection
and lineage queries in a read-only transaction per participating database.
A 30-second read budget limits long-running SQLite work. The file revision is
a conservative lower bound on the inputs read. SQLite and
the JSON files have independent versions. After releasing the read transactions,
the builder checks current ownership and eligibility for the rows it actually
read, including rows discovered for the first time. Ordinary changes after
capture leave another refresh due at the next watcher tick.

Known committed sidebar mutations update or remove the affected row in admitted
inventories and fence older work before notifying subscribers. External safety
changes are checked on the watcher's five-second cadence. Changes in row
ownership, visibility or existence retire the affected rows until the next
complete projection. Unverifiable eligibility, changed access settings and
replaced stores can withdraw the whole affected list. A missing eligible
snapshot shares the owner's bounded wait and can return HTTP 503 with
`retryable: true`. The existing client displays a loading error with Retry.
Direct opening and mutation continue to use their own authorization checks.

The cache lock protects the final swap, admission identity and safety generation.
The installed entry records the builder's captured revision. Evicting one
options key leaves other scopes in the same profile available. Health reads
report scalar counts of publications, failed builds, safety refusals, cold waits
and warm reads completed during a rebuild. Sampling these counts performs no
session discovery.

Older additive model callers retain their uncached-partial-read behavior. Shared
inventories explicitly request complete results; this policy is part of the cache
identity so a permissive caller cannot populate the strict inventory's snapshot.

## Opening conversations and background views

A known session ID uses an eligible cached row or reads that exact ID in the
admitted profile databases. It never falls back to `get_cli_sessions()` enumeration.
An older session outside the sidebar's recent window remains directly reachable.
The exact reader disables unrelated project/history passes. Authorized all-profile
lookups preserve the existing context priority, with the active profile first;
ID prefixes do not establish ownership. Claude Code exact reads identify the
matching file before parsing its conversation.

`GET /api/sessions` defaults to `inventory=interactive`. Chat boot, refresh, SSE
boot/reconnect and no-client warming perform no cron/webhook/kanban history passes.
The corresponding history view explicitly requests `inventory=cron`, `webhook` or
`kanban`. Reserved Cron Jobs and Webhooks project chips send that intent when
selected. Existing per-source limits, all-profile history behavior and project
assignment remain in the model readers. Background-view cache entries use the
existing route cache but are not admitted to continuous warming.

Tasks and Kanban retain their existing first-activation loaders. Hidden views do
not initiate another expensive load, and late loads cannot repaint a different
profile/view or restart its polling. Leaving Tasks stops its detail-view timer;
leaving Kanban closes its existing view stream/timer. Lightweight completion
notification polling continues, and background jobs/workers are not stopped.

## Contract changes and validation

The previous deadline fallback could start a second simultaneous scan. It now
returns eligible prior data or a retryable failure. The previous metadata-miss
fallback enumerated the sidebar window; it now resolves the exact requested ID,
including older rows. Plugins/tests overriding that fallback should replace
`get_cli_session_by_id`, rather than `get_cli_sessions`.

Background histories are now discovered when their view requests them. Existing
execution, direct conversation links, profile boundaries, session ordering and
archive/compression ownership remain in scope for regression coverage. No database
schema, model catalog, deployment configuration or virtual-keyboard behavior is
changed.

See [verification instructions](../../TESTING.md#shared-warm-session-inventory) and
[original implementation evidence](../../plan/warm-session-inventory-evidence.md)
and [active-write results](../../plan/cache-availability-webui-evidence.md).
Local fixture results do not establish public-ingress latency or physical-device
behavior. Deployment/restart remains a separate operator action.
