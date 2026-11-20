# Shared warm session inventory

The server prepares interactive session metadata once for matching clients. The
startup profile is prepared asynchronously, without waiting before opening the
listening socket. The existing Gateway watcher checks admitted scopes every five
seconds, even with no browsers connected. Additional scopes are admitted when
requested; startup does not scan every profile.

SQLite and session sidecars remain authoritative. The existing route cache holds
bounded, copy-safe metadata rather than transcripts. Its existing 64-entry bound
remains in place. Up to eight warm scope records retain options and identity only;
they do not hold another copy of session rows. The startup scope stays admitted.
Other scopes can become cold after eviction.

## Identity, filtering and freshness

Identity includes the active profile and resolved home, all-profiles mode,
visibility/source settings, Claude Code inclusion and discovery intent. Archive,
hidden-row, pagination and sidebar-tab filters reuse the common prepared metadata.
Lineage metadata for the existing top-N selections is prepared with it. Runtime
stream/pending/unread-related flags retain their existing response-time overlay.

Age alone does not discard an admitted snapshot: the background checker owns its
refresh. Unchanged checks inspect file identities and timestamps without projecting
histories. They include the database/WAL, sidecars, sidebar index, project/settings
files, profile configuration and applicable profile homes. Inode and ctime checks
cover replacements and same-size edits. A detected change can remove or hide a
row, so the old snapshot is withheld while the replacement is prepared. Explicit
WebUI invalidation fences publication by older work immediately. Refreshing metadata
continues to use the existing reconciliation/cleanup behavior.

Both cache layers allow one rebuild owner per scope. A follower's request deadline
never authorizes an independent rebuild. Expired eligible metadata can remain
readable during refresh; a genuinely cold or invalidated scope can return HTTP 503
with `retryable: true`. The existing client loading-error message and Retry button
handle that response while retaining previously loaded rows only for a matching
client scope. Read failure is never cached as a successful empty inventory.

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
[local implementation evidence](../../plan/warm-session-inventory-evidence.md).
Local fixture results do not establish public-ingress latency or physical-device
behavior. Deployment/restart remains a separate operator action.
