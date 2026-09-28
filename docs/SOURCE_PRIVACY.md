# Public source privacy maintenance

On September 28, 2026, the public source history and six release tags were rewritten to remove private operational metadata and historical local test artifacts. Commit identifiers changed. Start new release work from a fresh clone of the cleaned repository; do not merge or push an older private checkout's history back into it.

The source export uses an explicit file allowlist, rejects private output directories and common credential and workstation metadata, and permits binary assets only at their reviewed hashes. New or changed images require a separate content and metadata review before their hashes are approved. Automated scanning supplements human review; it does not prove that every possible private value has been identified.

Run `python scripts/check-public-history.py .` with complete local history before publishing. CI fetches the complete history and runs this check, so deleting a sensitive artifact from the latest tree alone cannot satisfy the gate. Public source checks and source archive verification must pass before a new release is published.

Original signed baselines were retained unchanged. The sanitized source has these differences from the original runtime baselines:

| Tag | Difference |
| --- | --- |
| v0.6.0 | Four historical local test output files were removed. |
| v0.6.1 | Runtime file sets and bytes remain identical. |
| v0.6.2–v0.6.5 | An installation path example in `docs/agent-access.md` was generalized. |

For affected tags, the original baseline check is expected to reject an exact reconstruction. Do not disable signature or hash verification, re-sign an old version, or claim that a modified source archive recreates the original binary. Future releases use a new version and normal signed release sequence.

This maintenance changes reachable Git history and tag source archives. It does not erase existing clones, earlier downloads, issued binaries, or GitHub's cached and unreferenced objects. Those surfaces require separate verification and, where applicable, host-side removal. Historical local evidence stays private.
