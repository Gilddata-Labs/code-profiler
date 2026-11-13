# GildData Code Profiler

Profiles your code repositories into one spreadsheet, without the source code ever leaving your machine.

You run one command against your GitHub organisation (or GitLab group, Bitbucket workspace, or a folder of clones). It produces a zip with one row per repository: size, languages, duplication, commit and pull request history, tests, secrets and personal data scans, plus the raw evidence behind every number. You send that zip to GildData. Nobody at GildData, and no buyer, sees a line of your code.

## What it measures

For every repository, on the default branch at the time of the run:

| Area | Columns |
|---|---|
| Volume | Physical LOC, Logical LOC (no blanks or comments), Data/Markup LOC, Non-Authored LOC (generated, vendored, dependencies), source and test files and LOC, repository size |
| Languages | Primary language, full language breakdown in percent, rare-language category |
| Duplication | Copy-paste ratio inside the repository, duplication across your repositories, repositories that share history |
| Structure | Functions, classes and types, parser coverage, comment and docstring ratio |
| Tests | Test-to-code ratio, share of files without tests, test runner and CI detected, suggested build and test commands |
| History | Default branch, branches, tags, total and non-merge commits, first and last commit, contributors (one person's several emails merged), bot authors, snapshot commit |
| Pull requests | Total, merged and reviewed pull requests, average and median lines per PR, share of simple, standard and rich PRs |
| Hygiene | Secrets found (count, rule and file path only), personal data found (count and type only), licence and open-source hints, fork and upstream hints |
| Signals | Evidence of AI-generated code (tool signatures in commits and PRs, bulk uploads), code vintage |

The tools behind the numbers are the standard ones buyers use to re-check them: `git`, `scc` for lines of code, `jscpd` for duplication, `gitleaks` for secrets, `tree-sitter` for structure. Every number in the output records which command produced it.

## What never leaves your machine

* No source code, no file contents, no secret values, no personal data values. Scans report a count, a rule name and a file path.
* Commit messages and pull request text are dropped. Author names and emails are hashed.
* The run is read-only on your platform. In API mode the zip records repository counts and branch heads before and after the run as proof that nothing changed.
* The optional AI labelling (`--llm`) is off by default and asks before sending anything.

You can open the zip and check all of this before sending it.

## Requirements

Either **Docker** (recommended, all tools included), or **Python 3.9+ and git**. With plain Python the profiler still runs end to end; for numbers that match the buyers' own tooling exactly, also install `scc`, `jscpd` and `gitleaks` (see "Without Docker" below).

Disk: about the size of your repositories, used temporarily. Memory: 4 GB is enough for most organisations with `WORKERS=4`; an organisation with a few repositories above 500 MB or above 5,000 pull requests may want 8 GB.

Access: a **read-only** token for your platform.

* **GitHub**: a fine-grained personal access token, created by an organisation owner. Resource owner: your organisation. Repository access: all repositories. Permissions: Contents read, Metadata read, Pull requests read. Nothing else.
* **GitLab**: a personal or group access token with `read_api` and `read_repository`.
* **Bitbucket**: an app password with Repositories: Read and Pull requests: Read, plus the email of that account.

## Quick start

```bash
git clone https://github.com/Gilddata-Labs/code-profiler.git
cd code-profiler
cp .env.example .env
```

Open `.env` and fill in two lines:

```
ORG=your-org-name
GITHUB_TOKEN=github_pat_...
```

Then run, with Docker:

```bash
docker compose run --rm profiler
```

or with Python:

```bash
pip install -r requirements.txt
./run.sh            # macOS and Linux
run.bat             # Windows
```

When it finishes you have `output/catalogue-<org>-<timestamp>.zip`. That zip is what you send to GildData.

With Docker, keep `OUTPUT_DIR` and `LOCAL_DIR` inside a folder Docker shares with containers (your home folder is shared by default on Docker Desktop; add others under Settings, Resources, File sharing).

To test the token before a real run (nothing is cloned):

```bash
./run.sh check                        # Python
docker compose run --rm profiler check --github-org your-org-name --github-token "$GITHUB_TOKEN"
```

## Settings

Everything is read from `.env` (or the environment). Only `ORG` and the token are required.

| Variable | Default | Meaning |
|---|---|---|
| `PLATFORM` | `github` | `github`, `gitlab` or `bitbucket` |
| `ORG` | | GitHub organisation, GitLab group or Bitbucket workspace |
| `GITHUB_TOKEN`, `GITLAB_TOKEN`, `BITBUCKET_TOKEN`, `BITBUCKET_EMAIL` | | Read-only credentials for the platform |
| `REPOS` | all | Comma-separated `owner/name` list to profile only some repositories |
| `LOCAL_DIR` | | Folder of already cloned repositories; profiles offline, no token needed |
| `INCLUDE_ARCHIVED`, `INCLUDE_FORKS` | `false` | Include archived repositories or forks |
| `WORKERS` | `4` | Repositories processed in parallel |
| `OUTPUT_DIR` | `./output` | Where the zip is written |
| `LABEL` | `ORG` | Name used in the zip file name |
| `MAX_PRS` | all | Cap on merged pull requests read per repository; `1000` gives a fast first run on large organisations |
| `GITHUB_HOST`, `GITLAB_HOST` | | Self-hosted servers |
| `EXTRA_ARGS` | | Any other flag, for example `--include-history-scan` to scan secrets across all commits, or `--report` for HTML report cards |

Run `./run.sh --help` for every flag of the underlying command.

## What you get

`output/catalogue-<org>-<timestamp>.zip` contains:

| Path | Contents |
|---|---|
| `catalogue.xlsx` | The catalogue, one row per repository, with a Field Guide tab that explains every column |
| `summary.csv` | The same rows as plain CSV |
| `qc/qc_report.csv` | Quality flags per repository (see below) |
| `qc/missing_fields.csv` | Columns the script cannot compute and who should fill them in |
| `qc/duplicate_groups.csv` | Repositories that share history or large parts of their files |
| `evidence/<repo>/` | The raw numbers behind each row: `scc.json`, `jscpd-report.json`, `git_stats.json`, `pr_stats.json`, `structure.json`, `file_classes.csv`, `secrets_pii.json`, `build_hints.json`, `for_check.json` |
| `manifest.json` | Tool versions, the command behind each column, run settings |
| `logs/extract.log`, `logs/failures.json` | Per-repository timings and any failures, so problems can be diagnosed without a second run |
| `gildscore.html` | Report cards, one per repository, only with `--report` |

### Quality flags

The `qc/qc_report.csv` file marks things a buyer will ask about. They are observations, not errors:

| Flag | Meaning |
|---|---|
| `EMPTY` | No code or no commits |
| `SHALLOW` | Five or fewer real commits, usually a snapshot import |
| `ZERO DUP` | Duplication exactly 0, which buyers audit |
| `NA>LOC`, `DATA HEAVY` | Non-authored or data lines dominate the repository |
| `DUPLICATE GROUP` | Shares history or files with another repository in the run |
| `SECRETS`, `PII` | Keys, passwords, emails, phone numbers or ID numbers found in the working tree. Paths are in `evidence/<repo>/secrets_pii.json`. Clean these before any delivery. |
| `PUBLIC RISK` | Looks like a public or open-source repository |
| `FEATURE BRANCH` | Default branch is not a main-style branch |
| `NO README`, `LOW PARSER COVERAGE`, `EXTRACT FAILED` | Description could not be read, structure counts are lower bounds, or the repository failed (details in `logs/`) |

### Reading the results

* Logical LOC is what `scc` reports on the default branch, including generated and vendored files. Non-Authored LOC is the part of that which is generated, vendored or dependencies. Both can be reproduced with the commands in `manifest.json`.
* Pull request columns come from the platform API. Reviewed PRs counts merged pull requests with at least one approval.
* Contributors counts people, not email addresses: the same name or the same email local part is merged. The raw identities are in `evidence/<repo>/git_stats.json`.
* A row with `EXTRACT FAILED` has the error class in `qc_report.csv` and the traceback in `logs/extract.log` when run with `-v`.

## How long it takes

Most repositories take seconds. Time is dominated by the platform API for pull request data: roughly 0.2 seconds per merged pull request. A repository with 9,000 merged pull requests takes about 30 minutes; an organisation of 100 repositories of ordinary size takes under an hour with `WORKERS=4`. Progress is printed every 20 pages of pull requests and after every repository.

If a run stops (network, laptop closed), run the same command again with `--resume <output folder>/catalogue-<org>-<timestamp>` appended through `EXTRA_ARGS`: finished repositories are kept and pull request pages already read are not fetched again.

## Without Docker

Python 3.9+ and git are required. The optional tools make the numbers match buyers' own checks exactly; without them a built-in counter is used and `manifest.json` says so.

```bash
pip install -r requirements.txt           # openpyxl, tree-sitter grammars
npm install -g jscpd@4.0.5                # duplication
# scc 4.1.0 and gitleaks 8.30.1: single binaries from their GitHub release pages, or
brew install scc gitleaks                 # macOS
```

## Troubleshooting

| Symptom | What to do |
|---|---|
| `check` shows fewer repositories than you expect | The token was created by someone who cannot see all repositories, or "All repositories" was not selected. Ask an organisation owner to create it. |
| `Rate limited ... waiting` lines | Normal on large organisations; the profiler waits and continues. |
| `Network error ... retrying` | Also normal on a flaky connection. If the run dies, resume it (see above). |
| A repository shows `EXTRACT FAILED` | Look at `logs/failures.json`; timeouts and network errors are retried once automatically. Re-run with `-v` in `EXTRA_ARGS` for a traceback. |
| Memory pressure on a small machine | Lower `WORKERS` to 2. |
| Windows: `py` not found | Install Python from python.org with "Add to PATH" ticked, or use Docker. |

If something still does not work, send `logs/extract.log` and `logs/failures.json` from the output folder to your GildData contact. Never send your token.

## Configuration file

`config.example.json` lists every tunable: rare languages, pull request size bands, directories to exclude, bot patterns, author aliases (to merge one person's identities by hand), and the thresholds behind the quality flags. Copy it, edit it, and pass it with `EXTRA_ARGS=--config my-config.json`.

## Tests

```bash
python tests/test_offline.py     # 43 checks on fixture repositories, no network
```

## Version

2.0.1. See `CHANGELOG.md`.
