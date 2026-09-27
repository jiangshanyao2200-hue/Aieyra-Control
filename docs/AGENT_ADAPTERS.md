# Persistent stations and local host adapters

This is the post-0.6.2 development interface. It is not part of the immutable 0.6.2 downloads. Python 3.11+ and a running local Control service are required. The standard-library adapter makes no model calls and reads no transcripts. It does not start a closed IDE or wake an idle model.

## Three distinct capabilities

| Host | Tools | Presence lifecycle | Execution control |
| --- | --- | --- | --- |
| Codex CLI / IDE | MCP stdio | Project AGENTS.md plus explicit `join` at real work boundaries | Native client owns execution; no automatic wakeup |
| Claude Code | MCP stdio | SessionStart, prompt, tool, compact, stop and session-end hooks | Hooks do not execute queued tasks or start models |
| Cursor local Agent | MCP stdio | sessionStart, prompt, tool, compact, stop and sessionEnd hooks | No followup_message, prompt blocking or model wakeup |
| Aieyra OS | Existing authenticated runtime adapter and explicit registration | Runtime identity and generation remain authoritative | Send/cancel/dispatch require advertised actions plus existing policy and model/tool allowlists |
| Other local agents | HTTP, CLI or MCP | Explicit `join` / `finish`, or a vendor-specific event adapter | Must be implemented and verified separately |

Configured, live communication, runtime activity, completed work and accepted work are different facts. A registered member remains visible while offline. Restoring an external chat cannot be inferred from an old transport identifier. Cloud-hosted agents cannot reach this machine's loopback automatically.

## Private station profile

Use an existing dedicated credential for the same responsibility. Do not share another agent's credential or enroll a duplicate role merely to reconnect. A genuinely new agent uses the existing owner/leader enrollment process described in `agent-access.md`.

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

Runtime `inbox` and coordination chat are separate. Join reports the number of unread messages for this project or coordination in the latest 100-message window; this is not the total historical unread count. Read `agent-client.py --config <credential-file> call center/history` for the latest messages, or `call center/inbox --query '{"after":0,"limit":100}'` and continue with the returned `next_cursor` as `after`. After actually reading relevant messages, use `call center/ack --body-file <receipt.json>` with a stable `request_id` and an `ids` list. Acknowledge only the messages you read and reply with verified outcomes when requested. Hooks neither store chat bodies nor issue read receipts; chat unavailability does not undo a successful join.

Before a final response, the agent saves meaningful memory itself. `Stop`/`SessionEnd` call the existing read-back-and-release helper; this does not invent a summary, complete tasks or mark messages processed. Missing memory, unconfirmed deliveries and release failures remain in the local receipt. The next user turn reconnects the same stable station.

## Installation and recovery

Installation prepends a small managed block to AGENTS.md and merges only adapter-owned entries. It preserves unrelated instructions, MCP servers, hooks and settings. Exact changed bytes are backed up under the private profile state directory, with a before/after digest journal. Repeating install is content-idempotent. Uninstall removes only managed contributions; a manually edited contribution causes a conflict instead of being overwritten. A Windows CRLF/LF conversion alone is accepted for instruction blocks, while unrelated instruction bytes stay intact. Existing empty config containers may remain after removal.

The project must be trusted by the host. Review and reload host configuration where its UI requires it. Local machine paths and private profile references in generated config should not be published; keep project-local configuration private according to the project's policy. Installation configures a host; it does not prove that host actually loaded or ran the hooks.

Claude uses `.claude/settings.local.json`, project `.mcp.json`, and a managed `CLAUDE.md` import of AGENTS.md. Command hooks use official exec-form `command` plus `args` to avoid shell expansion of paths. Claude events carry `session_id` and `cwd`; `additionalContext` is returned only on supported events. Stop hooks never force another model turn.

Cursor uses `.cursor/hooks.json`, `.cursor/mcp.json`, and an `alwaysApply` rule pointing to AGENTS.md. Its hooks use `conversation_id` and `workspace_roots`. The Windows command is a static encoded PowerShell invocation; untrusted input remains on stdin. Cursor sessionStart/sessionEnd are fire-and-forget, so prompt and tool events also reconcile presence. Cloud hook availability differs from local IDE operation and is not represented as local verification.

Codex uses project `.codex/config.toml` and AGENTS.md. No unverified Codex hook event is invented. Project configuration requires trust; inherited instruction files have a default aggregate 32 KiB budget, so the small station rule belongs near the start of a project's effective instruction file. An AGENTS.override.md can supersede AGENTS.md and must be checked locally. A reminder still depends on the agent following instructions.

Some CLI management commands do not load project configuration in the current host build. Use `python scripts/agent-station.py --profile C:/private/demo-station.json codex -- mcp get <managed-server-name> --json` to pass only this station's MCP entry as explicit CLI overrides. The same wrapper can start an explicitly requested Codex command. It changes no global trust or provider settings and does not launch Codex during install. The managed server name appears in `.codex/config.toml` and the private installation manifest. Verify normal project loading separately from this explicit fallback.

MCP's own communication helper now stops renewal after 300 seconds without a successful tool call or four hours total. Ping and a merely open host process do not count as work. It does not reconnect or execute messages. For longer real work use the event adapter or explicit bounded `lease` with genuine activity markers.

## Aieyra OS boundary

`service/os_sessions.py` accepts only explicit registration tied to executable, PID, session, creation time and config digest. `service/product_bridge/` performs authenticated discovery; `service/collaboration.py` intersects advertised runtime actions with local policy. A runtime registration is separate from a development-agent's long-lived office membership. Missing registration must remain unavailable; it is not repaired by scraping user sessions or launching a model.

Read `/api/collaboration` on local Control for the current observation. `available=false`, `stale=true` or no matrices means no currently verified runtime bridge. This does not mean the OS product-maintenance agent is absent. Production send/cancel is tested only when the product owner supplies a current registered runtime and the user's existing execution scope permits it. Generation checks, independent delivery evidence and acceptance remain mandatory.

`python scripts/agent-station.py --profile C:/private/demo-station.json os-doctor` reads that existing projection and emits only runtime IDs, freshness and effective capabilities. It suppresses capabilities on stale observations, returns `no_verified_runtime` for absent registrations, and does not expose task/chat bodies or inspect other sessions.

## Verification and official contract sources

`tests/service/test_agent_station.py` runs a real isolated Control HTTP server with synthetic identities. It covers new/resumed turns, compaction, concurrent events, lost responses, explicit handoff, foreign identity rejection, stop receipts, vendor payloads and config preservation. It is not a paid model test or proof of a Cursor/Claude GUI session. Existing OS suites cover registration, generation guards, read-only behavior and policy gating.

Contract snapshots were checked on 2026-09-27 against the official sources below:

- Claude Code: `https://code.claude.com/docs/en/hooks` and `https://code.claude.com/docs/en/mcp`.
- Cursor: `https://cursor.com/docs/hooks` and `https://cursor.com/docs/mcp`.
- Codex: `https://developers.openai.com/codex/guides/agents-md` and `https://developers.openai.com/codex/mcp` (official documentation redirects to learn.chatgpt.com).

Use each host's installed-version behavior when it differs from the current documentation. Adapter contract tests, native host config recognition and actual model-driven use must be reported separately.
