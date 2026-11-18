# Changelog

## 2.0.2

Clone timeouts on large repositories, seen on a 173 repository organisation:

- The clone timeout now scales with repository size (2 s per MB from the platform's size figure, at least 600 s, was a flat 300 s).
- A clone that times out is no longer retried with the same timeout, which only burned another full timeout since git cannot resume a clone. It is retried once with `--clone-extended-timeout` (now 3600 s, was 1200 s) and then marked failed; network errors are still retried as before.
- Clone log lines name the repository, its size and the timeout in use, so a slow repository can be found in the log.
- Four new offline checks cover the clone retry rules (47 in total).
- `--skip-prs` was accepted but ignored; it now skips the merged pull request fetch and the PR columns fall back to merge commit markers in the history.

## 2.0.1

First public release of the profiler.

Fixes since the internal 2.0.0 build:

- scc is called with the correct flags for the generated-code run; with 2.0.0 every repository failed when scc was installed.
- Non-Authored LOC and Data/Markup LOC follow the same file scope as Logical LOC, so neither can exceed it.
- Platform totals come from one GraphQL query instead of three search API calls per repository, which hit GitHub's search rate limit with several workers.
- Merged pull request fetching logs progress, uses smaller pages, checkpoints every page so `--resume` continues where it stopped, and keeps the merged total correct when `--max-prs` is used.
- `--resume` re-extracts repositories whose work files are missing instead of writing empty rows.
- Failures log a traceback with `-v`; network retries are logged.
- The Docker image builds on amd64 and arm64 and pins scc 4.1.0, gitleaks 8.30.1 and jscpd 4.0.5.

Packaging:

- `.env` driven runners for macOS and Linux (`run.sh`), Windows (`run.bat`) and Docker Compose.
- Offline test suite (43 checks) and a CI workflow.
