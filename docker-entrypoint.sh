#!/usr/bin/env bash
# Container entry point. With no arguments it builds the command from the environment
# (same variables as .env.example). With arguments it passes them straight to the profiler.
set -euo pipefail

if [ "$#" -gt 0 ]; then
  exec gilddata-profiler "$@"
fi

args=()
PLATFORM=${PLATFORM:-github}
if [ -n "${LOCAL_DIR:-}" ]; then
  # docker-compose.yml mounts the host folder at /repos and sets LOCAL_DIR=/repos
  if [ -z "$(ls -A /repos 2>/dev/null)" ]; then echo "LOCAL_DIR is set but /repos is empty: check the path in .env." >&2; exit 1; fi
  args+=(--local /repos)
else
  case "$PLATFORM" in
    github)    TOKEN=${GITHUB_TOKEN:-} ;;
    gitlab)    TOKEN=${GITLAB_TOKEN:-} ;;
    bitbucket) TOKEN=${BITBUCKET_TOKEN:-} ;;
    *) echo "PLATFORM must be github, gitlab or bitbucket (got '$PLATFORM')." >&2; exit 1 ;;
  esac
  if [ -z "$TOKEN" ]; then echo "No token for $PLATFORM in the environment. See .env.example." >&2; exit 1; fi
  if [ -z "${ORG:-}" ] && [ -z "${REPOS:-}" ]; then echo "Set ORG or REPOS in the environment." >&2; exit 1; fi
  args+=(--platform "$PLATFORM" --token "$TOKEN")
  [ -n "${ORG:-}" ] && args+=(--org "$ORG")
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
  [ "$PLATFORM" = bitbucket ] && [ -n "${BITBUCKET_EMAIL:-}" ] && args+=(--bitbucket-email "$BITBUCKET_EMAIL")
  [ -n "${GITHUB_HOST:-}" ] && args+=(--github-host "$GITHUB_HOST")
  [ -n "${GITLAB_HOST:-}" ] && args+=(--gitlab-host "$GITLAB_HOST")
fi
[ "${INCLUDE_ARCHIVED:-false}" = true ] && args+=(--include-archived)
[ "${INCLUDE_FORKS:-false}" = true ] && args+=(--include-forks)
args+=(--workers "${WORKERS:-4}" --out /out --keep-work)
[ -n "${LABEL:-}" ] && args+=(--label "$LABEL")
[ -n "${MAX_PRS:-}" ] && args+=(--max-prs "$MAX_PRS")
if [ -n "${EXTRA_ARGS:-}" ]; then
  # shellcheck disable=SC2206
  extra=( $EXTRA_ARGS ); args+=("${extra[@]}")
fi
echo "Profiling with workers=${WORKERS:-4}, output in /out (mounted from OUTPUT_DIR on the host)"
exec gilddata-profiler "${args[@]}"
