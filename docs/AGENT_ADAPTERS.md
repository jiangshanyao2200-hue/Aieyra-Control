# Persistent stations and local host adapters

Python 3.11+ and a running local Control service are required. The station lifecycle adapter reads no transcripts and makes no model calls. The separately configured leader notification service can activate an authorized leader's existing Codex thread as described below. It requires the local daemon to be running. Both `create` and leader notifications are included from 0.6.5. Check the installed script's help and the signed release manifest for availability.

## Three distinct capabilities

| Host | Tools | Presence lifecycle | Execution control |
| --- | --- | --- | --- |
| Codex CLI / IDE | MCP stdio | Project AGENTS.md plus explicit `join` at real work boundaries | Native client owns execution; authorized leaders can opt into the notification wake adapter below |
| Claude Code | MCP stdio | SessionStart, prompt, tool, compact, stop and session-end hooks | Hooks do not execute queued tasks or start models |
| Cursor local Agent | MCP stdio | sessionStart, prompt, tool, compact, stop and sessionEnd hooks | No followup_message, prompt blocking or model wakeup |
| Aieyra OS | Existing authenticated runtime adapter and explicit registration | Runtime identity and generation remain authoritative | Send/cancel/dispatch require advertised actions plus existing policy and model/tool allowlists |
| Other local agents | HTTP, CLI or MCP | Explicit `join` / `finish`, or a vendor-specific event adapter | Must be implemented and verified separately |

Configured, live communication, runtime activity, completed work and accepted work are different facts. A registered member remains visible while offline. Restoring an external chat cannot be inferred from an old transport identifier. Cloud-hosted agents cannot reach this machine's loopback automatically.

## Notify and wake an offline leader

An enrolled station can send a durable message without a live station lease:

```text
python scripts/agent-station.py --profile C:/private/worker.json notify-leader --request-id UNIQUE_STABLE_ID --body-file C:/private/leader-message.txt
python scripts/agent-station.py --profile C:/private/worker.json notification --id RETURNED_NOTIFICATION_ID
```

The UTF-8 body is limited to 2000 characters. Retry the same request ID and exact body after a lost response. The service chooses the active appointed leader for the sender's project. If several leaders apply, supply `--leader-actor-id`; callers cannot choose an arbitrary native thread. A changed-native `join` also submits this notification automatically after its explicit handoff notice. It never performs the handoff itself.

Native wakeup is an explicit local installation setting in `data/config/control.json`:

```json
{"leader_wakeup":{"enabled":true,"actors":["ENROLLED_LEADER_ACTOR"],"executable":"C:/absolute/path/to/codex.exe"}}
```

Enable only for leaders the user authorized to receive activation. The configured Codex executable must support `app-server proxy` and connect to its already running local daemon. No second daemon or new thread is created. Current native binding and governance are rechecked at send time; a handoff invalidates the old queued target. Model, provider, working directory, permissions and approval policy are inherited from the existing thread. A failed/interrupted/archived thread or one waiting for input needs explicit recovery; a closed daemon leaves `waiting_adapter`. Other host kinds can still send messages; their own wake adapters are not claimed implemented.

Messages persist in the station outbox and original center chat. The native prompt contains only notification identifiers and a reminder to read `station/notifications`; arbitrary sender prose is not passed as a user instruction. `notified` requires a native turn receipt, and remains separate from `read_at` and `handled_at`. The leader joins with its own profile, reads the message, then calls `station/notification-ack` with its live `session_id`, notification `id`, and `state` of `read` or `handled`. Runtime inbox advertises pending leader notifications. MCP exposes `aieyra_notify_leader`, `aieyra_leader_notifications`, and `aieyra_notification_ack`.

Notification lists use stable cursor pagination. `direction` is `received` (default) or `sent`; `status` is `unhandled` (default) or `all`; `limit` is 1–100 (default 50). Use the returned `next_cursor` as `after` while `has_more` is true. `total` counts all records matching the direction/status before the cursor, from the same read snapshot. It can change when notices arrive or are handled. A cursor remains usable after its notice is handled and across service restarts. It cannot reference another actor's records. Start a new scan for later arrivals; pages are ordered by creation time and ID. Listing never marks a notice read. Runtime inbox `pending` and `unread` count the full outstanding set, not one page.

```text
python scripts/agent-station.py --profile C:/private/leader.json notifications --limit 50
python scripts/agent-station.py --profile C:/private/leader.json notifications --after RETURNED_NEXT_CURSOR
python scripts/agent-station.py --profile C:/private/worker.json notifications --direction sent --status all
python scripts/agent-station.py --profile C:/private/leader.json notification-ack --session-id CURRENT_JOIN_SESSION --id NOTICE_ID --state read
```

After an explicit native handoff, the new session for the same appointed leader may review old notifications. Read the original notice and verify its outcome first. Then use `notification-ack` with `--review-previous-binding --expected-binding-version CURRENT_VERSION --reason "What was verified"` and the current live session. The HTTP/MCP equivalents are `review_previous_binding: true`, `expected_binding_version`, and `reason`. Both `read` and `handled` need this explicit review if the original binding differs. An outdated expected version returns 409; current project leadership and the current native/seat binding are rechecked. This operation neither transfers leadership nor sends the old native input again.

Original target, binding version, delivery state and native receipt stay intact. `read_receipt` and `handled_receipt` record the acting leader, transport, native binding, review reason and time separately; retries retain the first receipt. Historical timestamps from older versions keep null receipts rather than invented attribution. Query a sent notice by ID or via `direction=sent&status=all` to see these outcomes. Review reasons are visible to that notice's sender and target; include only necessary coordination facts.

Wake requests are coalesced in batches of at most 20, separated by at least 60 seconds and limited to 12 attempts per leader per hour; each sender can create at most 20 notices per hour. Unsent wake requests expire after 24 hours, with their messages retained for review. Before activation, CPU and available RAM must show at least 20% headroom; unavailable samples defer the request. This is a sampled gate, not a machine-wide CPU/GPU limit. Native response loss or a crash during submission leaves `unknown` and never retries the model input automatically. Read or handled notices do not wake again. Owner recovery, product execution and production permissions remain separate.


## Create and join a new station

A new local agent can create its own dedicated identity, save its station profile and join in one explicit command. The project must already be registered in Control and the agent must have permission to work on it. Use a new private profile path and the real current host session ID:

```text
python scripts/agent-station.py --profile C:/private/demo-station.json create --name "Demo maintenance" --project demo --root C:/Projects/Demo --host codex --native-session-id REAL_CURRENT_HOST_SESSION_ID
```

The command reuses `enroll-agent.py` and the existing loopback-only, same-origin CSRF enrollment endpoint. It does not require another agent's credential or add a remote self-registration endpoint. The dedicated credential is saved privately in `demo-station.state/credential.json`; the profile contains only its absolute reference. Output includes the profile, actor, seat, real native ID and transport ID, never the token. Standard `join` identity checks, bounded activity lease, memory readiness and inbox reporting apply. `--no-lease` disables the child lease helper; `--port` selects an alternate local Control port. Creation does not install hooks, edit AGENTS.md, register projects, accept tasks or grant leadership. Run `install` separately when host integration is wanted.

`--root` is the explicitly selected local working directory used by this host's hooks. It may be a separate worktree of the registered project and does not change the registry's canonical project root. An existing directory alone does not establish authorization for that project. Unknown projects and other confirmed HTTP refusals retain their server error code; correct the registration or permission issue before retrying the saved creation.

If creation is interrupted, retain the profile and its sibling state directory. Repeat the **identical creation command with `--resume`**. The saved creation request and credential are reused; completed enrollment, profile publication and join are reconciled. Concurrent retries serialize on the same profile. Private JSON is published complete and without overwriting an existing file, using a same-filesystem hard link; use a local filesystem that supports hard links. An unresolved connect intent remains query-only under the existing `join` rules. A retry with changed name, project, root, host, native ID, port or lease option is rejected. Do not delete state, invent another request ID or choose another profile merely to retry an unknown result.

After successful creation, use ordinary `join` and `finish` with this profile. An existing responsibility must reuse its original profile; a changed native ID requires explicit CAS handoff. `create` refuses an existing profile and does not adopt another identity. Its `--resume` option continues creation; the separate `join --resume` option returns bounded private recovery content.

## Private station profile

Use an existing dedicated credential for the same responsibility. Do not share another agent's credential or enroll a duplicate role merely to reconnect. A new agent can use the explicit `create` command above, or the lower-level owner/leader enrollment process described in `agent-access.md`.

Store this profile outside public source exports. All paths must be absolute. Identity values come from `agent-client.py info` and `seats`, not guesses. The profile contains a reference to the credential file, never its token:

```json
{
  "schema": 1,
  "host": "claude",
  "project": "demo",
  "root": "C:/Projects/Demo",
  "config_file": "C:/private/demo-agent.json",
  "actor_id": "agent-from-info",
  "seat_id": "seat-from-seats",
  "max_seconds": 3600,
  "idle_seconds": 300
}
```

Host values: `codex`, `claude`, `cursor`, `os`, `generic`. The configurable bounds are 30–14400 seconds total and 30–600 seconds idle. Work events refresh activity; the helper never refreshes its own activity marker. It releases the communication lease at the idle or total limit. The server lease also expires if the process dies. Long waits beyond the limit therefore need a new real work event.

```text
python scripts/agent-station.py --profile C:/private/demo-station.json doctor
python scripts/agent-station.py --profile C:/private/demo-station.json install
python scripts/agent-station.py --profile C:/private/demo-station.json join --native-session-id REAL_CURRENT_HOST_SESSION_ID
python scripts/agent-station.py --profile C:/private/demo-station.json finish --native-session-id REAL_CURRENT_HOST_SESSION_ID
python scripts/agent-station.py --profile C:/private/demo-station.json uninstall
```

`doctor` is read-only. `join` verifies the credential actor, project, seat and current native binding, then either reuses a matching live transport or connects a fresh transport. It persists the connect intent before writing. A lost response is resolved by lookup; unresolved intent is retained and never replayed. After investigating an unresolved intent using its original request/session IDs, an operator may reconcile the private state; do not delete it merely to force a retry.

A changed native ID returns `handoff_required` and sends a deduplicated coordination notice requesting explicit CAS handoff. The leader verifies ownership and releases any old connection before using the existing `station/handoff` contract. The adapter does not change the binding itself. Another credential's active transport cannot be adopted. Subagent events are ignored to prevent children from taking the parent station.

The profile's sibling `<profile-stem>.state/connection.json` contains the actual current transport ID. Agents use that ID with the original `agent-client.py` tools to read/acknowledge inbox and save all five memory sections with the current CAS version. Hooks show only compact status and memory version, not private memory bodies. Prompts, tool arguments, transcripts, emails and provider credentials are not saved by the adapter.

Runtime `inbox` and coordination chat are separate. Join reports recent unread messages for this project or coordination; this is not the total historical unread count. Read `agent-client.py --config <credential-file> call center/history --query '{"limit":3}'` for a small newest-first page. History defaults to 20, allows 1–100, and returns `has_more` plus `next_before`; pass that value as `before` for older pages. Unknown fields such as `after` are rejected. For ascending incremental reads use `call center/inbox --query '{"after":0,"limit":100}'` and continue with `next_cursor` as `after`. After actually reading relevant messages, use `call center/ack --body-file <receipt.json>` with a stable `request_id` and an `ids` list. Acknowledge only the messages you read and reply with verified outcomes when requested. Hooks neither store chat bodies nor issue read receipts; chat unavailability does not undo a successful join.

`--query` requires a JSON object, not `limit=3`. Malformed JSON or a non-object returns `invalid_agent_query_json` without sending a request. A malformed/non-object body file returns `invalid_agent_body_json`; an unreadable body file returns `agent_body_file_unreadable`. These errors do not mean that the private credential is invalid, and never echo the body or credential.

Before a final response, the agent saves meaningful memory itself. `Stop`/`SessionEnd` call the existing read-back-and-release helper; this does not invent a summary, complete tasks or mark messages processed. Missing memory, unconfirmed deliveries and release failures remain in the local receipt. The next user turn reconnects the same stable station.

## Installation and recovery

For an explicit private handoff, use `join --native-session-id REAL_CURRENT_HOST_SESSION_ID --resume --after 0 --limit 20`. The result reuses the memory, runtime inbox and coordination reads already performed by join. It includes `project`, `transport_session_id` (also available as `session_id`), the distinct native ID, ordered bodies and `next_cursor`. Use `center/inbox` for ascending `after` pagination; `center/history` is a recent-history interface and does not implement that cursor.

`--limit` is 1–100 (default 20); `--after` is a nonnegative 64-bit cursor. The coordination bundle includes only this project and coordination messages, but its cursor covers all scanned rows. Each body is limited to 128 KiB; an oversized body returns `truncated` and `read_separately`, never a false empty success. Default join and hooks remain compact. The bundle does not ACK, persist a cursor, save memory or execute tasks. After actually reading messages, ACK their IDs yourself and retain a cursor explicitly if needed. Renew presence at real work boundaries; an expired idle lease remains expired until a real join event.

A project's source ownership and a credential's task-writing scope are separate. Read the returned project and use the actual transport ID. A `project_scope_denied` response is not repaired by substituting another agent's credential or silently changing its binding.

Installation prepends a small managed block to AGENTS.md and merges only adapter-owned entries. It preserves unrelated instructions, MCP servers, hooks and settings. Exact changed bytes are backed up under the private profile state directory, with a before/after digest journal. Repeating install is content-idempotent. Uninstall removes only managed contributions; a manually edited contribution causes a conflict instead of being overwritten. A Windows CRLF/LF conversion alone is accepted for instruction blocks, while unrelated instruction bytes stay intact. Existing empty config containers may remain after removal.

The project must be trusted by the host. Review and reload host configuration where its UI requires it. Local machine paths and private profile references in generated config should not be published; keep project-local configuration private according to the project's policy. Installation configures a host; it does not prove that host actually loaded or ran the hooks.

Claude uses `.claude/settings.local.json`, project `.mcp.json`, and a managed `CLAUDE.md` import of AGENTS.md. Command hooks use official exec-form `command` plus `args` to avoid shell expansion of paths. Claude events carry `session_id` and `cwd`; `additionalContext` is returned only on supported events. Stop hooks never force another model turn.

Cursor uses `.cursor/hooks.json`, `.cursor/mcp.json`, and an `alwaysApply` rule pointing to AGENTS.md. Its hooks use `conversation_id` and `workspace_roots`. The Windows command is a static encoded PowerShell invocation; untrusted input remains on stdin. Cursor sessionStart/sessionEnd are fire-and-forget, so prompt and tool events also reconcile presence. Cloud hook availability differs from local IDE operation and is not represented as local verification.

Codex uses project `.codex/config.toml` and AGENTS.md. No unverified Codex hook event is invented. Project configuration requires trust; inherited instruction files have a default aggregate 32 KiB budget, so the small station rule belongs near the start of a project's effective instruction file. An AGENTS.override.md can supersede AGENTS.md and must be checked locally. A reminder still depends on the agent following instructions.

Some CLI management commands do not load project configuration in the current host build. Use `python scripts/agent-station.py --profile C:/private/demo-station.json codex -- mcp get <managed-server-name> --json` to pass only this station's MCP entry as explicit CLI overrides. The same wrapper can start an explicitly requested Codex command. It changes no global trust or provider settings and does not launch Codex during install. The managed server name appears in `.codex/config.toml` and the private installation manifest. Verify normal project loading separately from this explicit fallback.

MCP's own communication helper now stops renewal after 300 seconds without a successful tool call or four hours total. Ping and a merely open host process do not count as work. It does not reconnect or execute messages. For longer real work use the event adapter or explicit bounded `lease` with genuine activity markers.

## Aieyra OS boundary

An OS agent with an authorized local `exec_command` tool can use `create --host os` from the Control installation. Use that agent's real runtime session ID and a private profile unique to its responsibility; do not use the OS developer's profile, another agent's native ID or a made-up transport ID:

```text
python CONTROL_INSTALL/scripts/agent-station.py --profile PRIVATE_DIR/os-agent.json create --name "OS task agent" --project REGISTERED_PROJECT --root AGENT_WORKDIR --host os --native-session-id REAL_AGENT_RUNTIME_SESSION_ID
```

Replace the uppercase placeholders with the actual local installation, assigned project, private directory, work directory and runtime session. The same Python runtime shipped with Control can run the script if Python is not on PATH. Read memory and inbox through the returned credential-file reference and actual transport ID; use `join` at real work boundaries and save memory before `finish`. Each new agent creates a distinct identity and station; resuming the same agent uses its saved profile. The host is responsible for exposing the real runtime identity and authorized local execution tool. Control does not run a model to make it enroll.

This grants office communication membership. Runtime observation and production dispatch still use the separately authenticated registration below; a connected office station is not proof that the runtime bridge can send or cancel work. Isolated `host=os` acceptance verifies two new agents can each create/join/read/finish without existing credentials. It is separate from a live OS model-session receipt.

`service/os_sessions.py` accepts only explicit registration tied to executable, PID, session, creation time and config digest. `service/product_bridge/` performs authenticated discovery; `service/collaboration.py` intersects advertised runtime actions with local policy. A runtime registration is separate from a development-agent's long-lived office membership. Missing registration must remain unavailable; it is not repaired by scraping user sessions or launching a model.

The standalone installation keeps data under its installation directory; an OS registration written to a different local data directory is not automatically discovered there. The local operator must first verify the running service's `--data-dir`, the intended registration and policy, and the live runtime identity. An existing `collaboration_sources` entry can reference that exact `source.json` and `bridge.json` using absolute paths plus `registration_sha256` and `config_sha256`, with the installation's own `service/product_bridge` adapter. This does not require enabling automatic directory discovery. Preserve unrelated configuration and reload the owned service after backup, then verify `os-doctor` and `/api/collaboration`. A new runtime identity or changed policy requires fresh validation; never repoint a pinned entry merely to revive old work.

Read `/api/collaboration` on local Control for the current observation. `available=false`, `stale=true` or no matrices means no currently verified runtime bridge. This does not mean the OS product-maintenance agent is absent. Production send/cancel is tested only when the product owner supplies a current registered runtime and the user's existing execution scope permits it. Generation checks, independent delivery evidence and acceptance remain mandatory.

`python scripts/agent-station.py --profile C:/private/demo-station.json os-doctor` reads that existing projection and emits only runtime IDs, freshness and effective capabilities. It suppresses capabilities on stale observations, returns `no_verified_runtime` for absent registrations, and does not expose task/chat bodies or inspect other sessions.

## Verification and official contract sources

`tests/service/test_agent_station.py` runs a real isolated Control HTTP server with synthetic identities. It covers new/resumed turns, compaction, concurrent events, lost responses, explicit handoff, foreign identity rejection, stop receipts, vendor payloads and config preservation. It is not a paid model test or proof of a Cursor/Claude GUI session. Existing OS suites cover registration, generation guards, read-only behavior and policy gating.

Contract snapshots were checked on 2026-09-27 against the official sources below:

- Claude Code: `https://code.claude.com/docs/en/hooks` and `https://code.claude.com/docs/en/mcp`.
- Cursor: `https://cursor.com/docs/hooks` and `https://cursor.com/docs/mcp`.
- Codex: `https://developers.openai.com/codex/guides/agents-md` and `https://developers.openai.com/codex/mcp` (official documentation redirects to learn.chatgpt.com).

Use each host's installed-version behavior when it differs from the current documentation. Adapter contract tests, native host config recognition and actual model-driven use must be reported separately.
