# Code structure

Control is a desktop application with a loopback Python service. The browser observes local workstations, tasks and chat; authorized agents perform mutations. The optional cloud handles account login, native Matrix participation, private feedback and signed distribution. Private project memory stays local.

| Directory | Responsibility |
| --- | --- |
| `web/` | Current single-page interface, models and account panel |
| `service/main.py` | Application composition, HTTP routing and process lifecycle |
| `service/service_common.py` | Shared local protocol primitives and errors |
| `service/delivery_store.py` | Delivery, event and snapshot persistence |
| `service/runtime_observer.py` | Read-only native execution evidence |
| `service/hub_client.py` | Explicit legacy remote-center adapter |
| `service/product_bridge/` | Bundled OS protocol adapter, separate from private historical evidence |
| `service/local_hub.py` | Default local transport over the pinned office engine |
| `service/coordination_core/` | Pinned adapted engine; provenance and hashes in `UPSTREAM.json` |
| `service/agent_access.py` | Agent identities, leases, ACLs and station lifecycle |
| `service/project_memory.py` | Versioned project documents and compare-and-swap saves |
| `service/cloud_*`, `feedback*` | Explicit account boundary and private diagnostic queue |
| `desktop/` | Electron supervision, window lifecycle and OS integration |
| `cloud/` | Website, account-bound APIs and operator-only maintenance CLI |
| `cloud/site/transitions.js` | Shared optional page-state transitions with reduced-motion and interruption handling |
| `scripts/` | Enrollment, updates, package construction and validation entrypoints |
| `scripts/agent-station.py` | Private stable profiles, bounded lifecycle adapters, project config merge and read-only OS diagnostics |
| `tests/` | Isolated service, unit, desktop and browser checks |

The local `main` module retains aliases for extracted classes so existing internal integrations remain compatible. Domain modules do not import the application entrypoint. `service/feedback_contract.py` is the single contract source; export copies it into the standalone cloud package.

The public website is read-only. Matrix topic and reply writes require the authenticated native proof described in `MATRIX_GROWTH.md`; retired community writes return 410 while preserving historical records. Public static files use a finite allowlisted byte cache with content ETags and mandatory revalidation. Account/session data and authenticated downloads remain private and non-cacheable. Downloads support one byte range, 416 size hints, HEAD, account concurrency limits and periodic session revocation checks; shared edge limits belong to the deployment configuration.

The vendored office engine is excluded from mechanical reformatting so its recorded adapted hashes remain meaningful. Changes there need explicit upstream review and regression evidence. The Android source tree in the private development workspace is a separately validated historical prototype, outside the current Windows/macOS release matrix; it is not silently represented as a shipped platform.

Runtime packages contain only the allowlisted application. Public development exports additionally include pinned tooling, isolated tests, current developer guides and CI. Local configuration, databases, credentials, historical correspondence and operations evidence never enter either export. Historical collaboration, correspondence and recovery evidence are retained in a private library outside the software tree.

Do not move state into source folders or hide access errors behind successful-looking UI. A communication lease, latest execution report, task claim and acceptance receipt are separate facts. Saved memory uses explicit versions; uncertain writes are independently read back before another mutation.

Station profiles refer to existing private credential files; host hooks consume only identity/project/event metadata. They never read transcripts, invoke models or replay deliveries. New native identities require explicit handoff. The host adapters and installation boundaries are described in `AGENT_ADAPTERS.md`; source availability does not imply an immutable released package contains them.
