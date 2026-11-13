#!/usr/bin/env bash
# Runs the profiler using the settings in .env (or the environment).
#   ./run.sh            full run
#   ./run.sh check      only test tokens and access, nothing is cloned
#   ./run.sh --help     every flag of the underlying command
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "python3 was not found. Install Python 3.9 or newer, or use Docker (see README)." >&2; exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Python 3.9 or newer is needed (found $("$PY" --version 2>&1))." >&2; exit 1
fi
if ! command -v git >/dev/null 2>&1; then
  echo "git was not found. Install git and try again." >&2; exit 1
fi

MODE=run
if [ "${1:-}" = "check" ]; then MODE=check; shift; fi
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then exec "$PY" gilddata_profiler.py --help; fi

args=()
PLATFORM=${PLATFORM:-github}
if [ -n "${LOCAL_DIR:-}" ]; then
  args+=(--local "$LOCAL_DIR")
else
  case "$PLATFORM" in
    github)    TOKEN_VAR=GITHUB_TOKEN ;;
    gitlab)    TOKEN_VAR=GITLAB_TOKEN ;;
    bitbucket) TOKEN_VAR=BITBUCKET_TOKEN ;;
    *) echo "PLATFORM must be github, gitlab or bitbucket (got '$PLATFORM')." >&2; exit 1 ;;
  esac
  if [ -z "${!TOKEN_VAR:-}" ]; then
    echo "$TOKEN_VAR is empty. Put a read-only token in .env (see .env.example)." >&2; exit 1
  fi
  args+=(--platform "$PLATFORM" --token "${!TOKEN_VAR}")
  if [ -n "${ORG:-}" ]; then args+=(--org "$ORG"); fi
  if [ -n "${REPOS:-}" ]; then
    IFS=',' read -ra list <<< "$REPOS"
    for r in "${list[@]}"; do
      r=$(echo "$r" | xargs)
      case "$PLATFORM" in
        github)    args+=(--github-repo "$r") ;;
        gitlab)    args+=(--gitlab-project "$r") ;;
        bitbucket) args+=(--bitbucket-repo "$r") ;;
      esac
    done
  fi
  if [ -z "${ORG:-}" ] && [ -z "${REPOS:-}" ]; then
    echo "Set ORG (or REPOS, or LOCAL_DIR) in .env." >&2; exit 1
  fi
  [ "$PLATFORM" = bitbucket ] && [ -n "${BITBUCKET_EMAIL:-}" ] && args+=(--bitbucket-email "$BITBUCKET_EMAIL")
  [ -n "${GITHUB_HOST:-}" ] && args+=(--github-host "$GITHUB_HOST")
  [ -n "${GITLAB_HOST:-}" ] && args+=(--gitlab-host "$GITLAB_HOST")
fi

if [ "$MODE" = check ]; then
  # check only understands platform, org and token flags
  check_args=()
  [ -n "${LOCAL_DIR:-}" ] && { echo "LOCAL_DIR is set: nothing to check, run ./run.sh directly."; exit 0; }
  case "$PLATFORM" in
    github)    check_args+=(--github-token "${GITHUB_TOKEN}") ; [ -n "${ORG:-}" ] && check_args+=(--github-org "$ORG") ;;
    gitlab)    check_args+=(--gitlab-token "${GITLAB_TOKEN}") ; [ -n "${ORG:-}" ] && check_args+=(--gitlab-group "$ORG") ;;
    bitbucket) check_args+=(--bitbucket-token "${BITBUCKET_TOKEN}" --bitbucket-email "${BITBUCKET_EMAIL:-}") ; [ -n "${ORG:-}" ] && check_args+=(--bitbucket-workspace "$ORG") ;;
  esac
  [ -n "${GITHUB_HOST:-}" ] && check_args+=(--github-host "$GITHUB_HOST")
  [ -n "${GITLAB_HOST:-}" ] && check_args+=(--gitlab-host "$GITLAB_HOST")
  exec "$PY" gilddata_profiler.py check "${check_args[@]}"
fi

[ "${INCLUDE_ARCHIVED:-false}" = true ] && args+=(--include-archived)
[ "${INCLUDE_FORKS:-false}" = true ] && args+=(--include-forks)
args+=(--workers "${WORKERS:-4}" --out "${OUTPUT_DIR:-./output}" --keep-work)
[ -n "${LABEL:-}" ] && args+=(--label "$LABEL")
[ -n "${MAX_PRS:-}" ] && args+=(--max-prs "$MAX_PRS")
if [ -n "${EXTRA_ARGS:-}" ]; then
  # shellcheck disable=SC2206
  extra=( $EXTRA_ARGS ); args+=("${extra[@]}")
fi

echo "Profiling with workers=${WORKERS:-4}, output in ${OUTPUT_DIR:-./output}"
exec "$PY" gilddata_profiler.py "${args[@]}" "$@"
