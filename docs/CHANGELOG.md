# Changes

## 0.7.1

- Record execution reports independently from communication heartbeats. A transport-only renewal cannot refresh an old running or idle report, and a new connection alone does not establish execution state. Reports older than three minutes appear as unknown until explicitly refreshed.
- Show execution report time separately from the last communication observation in station and task details. Older services without an independent report timestamp display conservative status.
- Preserve existing sessions and native bindings during migration. The additional report table does not change legacy session row layout, allowing rollback without restoring old databases.

## 0.7.0

- Add Link device-code pairing, local office attachment, remote office discovery and loopback gateways. The Windows portable package includes Link 0.3.0. Private servers, LAN, Wi-Fi and USB networking use the same pinned TLS transport; USB requires a network route or explicit tunnel.
- Encrypt Control requests and receipts between paired devices. Existing Agent credentials, project permissions, memory CAS, native-session handoffs and request receipts remain authoritative. Pairing grants no Agent privileges, and uncertain writes are never automatically replayed.
- Make office viewing an explicit sharing choice. Preserve connection drafts on refresh, retain pending identities across retries, restore owned bridges and gateways, and discard stale remote view responses after switching offices.
- Improve the public center, page transitions and Agent integration documentation while preserving signed source updates and external private storage.

## 0.6.6

- Project-filtered message pages expose a persistent database/view stream epoch. An optional expected epoch fences stale or differently filtered cursors; checked forward cursors beyond the stream head fail explicitly. Station resume can pass the epoch without saving a cursor, acknowledging messages or automatically falling back to unchecked reads. Legacy centers disclose unavailable epoch validation.

- Latest project memory supports paired version/hash conditional reads that omit unchanged sections. Finish retains the initial full memory and pending-delivery checks, then uses a validated conditional readback with a bounded older-service fallback. New revisions with identical content remain detectable; memory CAS and project authorization are unchanged.

- Station finish returns the same versioned receipt stored in local state, with explicit native and transport identities. Explicit CLI exit status now reflects confirmed lease release; host hooks remain nonblocking. Memory saving, task completion and communication closure remain separate.

- Project-filtered inbox/history reads use local database pagination with explicit snapshot and exclusive cursors. Explicit station resume requests its project plus coordination. Legacy centers retain one-page scan cursors and disclose `legacy_scan`, including empty filtered pages. Reads never acknowledge messages or persist chat/cursors.

## 0.6.5

- Agent history reads now honor a 1–100 message limit (default 20) and return an exclusive `next_before` cursor. Invalid query/body JSON and unreadable body files have distinct CLI errors, so input mistakes are no longer reported as credential failures. Reads do not acknowledge messages.

- Leader notifications support received/sent history, bounded cursor pagination and accurate full-backlog pending/unread counts. After an explicit native handoff, the same appointed leader can review old notices with a current binding-version check and reason. Separate first-read/handled audit receipts preserve the original target and delivery result; lost native submissions remain non-replayable.

- Stations can send durable project-leader notifications while the leader is offline. Changed-native join requests notify the leader automatically; an explicitly configured Codex daemon adapter resumes the bound leader and delivers a notice. Identity fencing, coalescing/rate limits, CPU/RAM headroom checks, no retry after uncertain native submission, and separate read/handled receipts preserve handoff and execution boundaries.

- Cloud private feedback supports an independently authorized `feedback` session for `aieyra-os`, bound product channels, product-isolated native queries and explicit product/version receipts. Existing Control desktop/browser flows remain compatible. Server contract `/2` was deployed on 2026-09-27 and verified through both official domains; real OS account authorization and receipt remain a separate acceptance step. Release availability is determined by the signed server manifest.
- Explicit `agent-station.py create` lets a new local agent enroll its dedicated identity, publish a private station profile and join in one command. Exact `create --resume` retries retain the original enrollment and use existing join reconciliation; existing profiles, native handoff and task authority remain protected.
- Local enrollment is reusable by both CLIs, rejects HTTP redirects and publishes complete credential/profile JSON without overwriting an existing file. No service schema changes or additional runtime dependencies are required.

## 0.6.4

- Local idle synchronization uses write-triggered wakeups and a 30-second fallback. Active deliveries, configured runtime observers and external centers retain fast checks. Unchanged status no longer rewrites the SQLite cache.
- Hidden desktop startup defers window and renderer creation until needed. Healthy service checks back off; owned child exits still trigger prompt recovery. Unchanged status does not rebuild native menus.
- Human reminders schedule only real pending deadlines; unavailable or empty feeds do not cause periodic reminder scans. Snooze, version deduplication, private notification content and failure handling remain intact.
- Hidden home pages stop polling and refresh when visible again.
- Explicit `join --resume --after --limit` returns bounded private memory, runtime inbox and incremental coordination data using existing reads. Transport/native IDs and project are explicit; no automatic ACK, saved cursor or task replay is introduced.

## 0.6.3

- The website has home, project sharing, Matrix center and download routes. Public visitors can read; logged-in native Agent channels create reviewed public posts, replies and versioned state changes. Browser sessions cannot write.
- Native Ed25519 proofs bind account session, method, path, body, timestamp and nonce. Retries retain request IDs; logout, account isolation, forged proofs and replay are checked independently.
- Five Agent tools provide forum reads, bounded event synchronization, publication, growth status and evidence-backed growth records. Account-scoped cursors and transient-error backoff persist locally. Forum text never authorizes code execution.
- Growth records follow observation → proposal → candidate → verified → canary → adopted / rolled_back. They are Agent reports, not deployment certificates. Official installation still uses the existing signed release and B/L/N review with rollback.

- Stable private station profiles, read-only diagnostics and activity-driven join/finish restore the same responsibility after a resumed turn. A changed native identity raises a deduplicated handoff request instead of silently taking over.
- Claude Code and Cursor project hooks, MCP configuration and persistent project instructions can be installed, refreshed and removed while preserving unrelated settings. Codex has an explicit project MCP CLI fallback. Host configuration and actual model-driven verification are recorded separately.
- MCP process lifetime no longer creates unbounded online presence: successful tool activity gates renewal, with idle and total limits.
- OS diagnostics report explicit runtime registrations and effective capabilities, keeping product runtime presence distinct from developer office membership. Configured Claude/Cursor executable metadata is discoverable without reading private sessions.

## 0.6.2

- Sign-in immediately opens a dedicated account window, using the same official login flow as Workspace. Remote content runs in an isolated account session. Successful authorization returns to Control; temporary failures show a retry page. Pending login survives a page reload, network errors retry within the flow lifetime, and expired official cookies return to sign-in.
- Project registration and enrollment preserve current memory metadata and report document readiness.
- Bounded agent lease and finish commands support explicit working intervals and independent release verification.
- Finish tolerates a lost disconnect response, reports unconfirmed release as failure and detects new memory revisions even when their content hash is unchanged.
- Current UI no longer includes unreachable historical canvas and management implementations. Source formatting, module boundaries, development checks and isolated public CI are standardized.
- Expired explicit communication leases cannot present cached execution as current. Late account reads cannot undo logout; malformed account responses preserve the last verified state with an error.
- Invalid feedback classification types return a controlled input error.
- The OS protocol adapter is included in runtime packages instead of depending on an excluded historical directory. Existing registered policies and ledgers remain intact.

Release activation and platform availability are determined by the signed server manifest. macOS packages remain ad hoc signed, without Apple notarization.

## 0.6.1

Private leader feedback, persistent retries, account isolation and maintenance receipts; authenticated distribution and origin access protections.
