#!/usr/bin/env bash
# Preview what the changelog would look like for the next release.
# Usage: ./tools/preview-changelog.sh [from-tag]
#   from-tag: Optional. The tag to diff from (defaults to the latest git tag).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Determine the base tag
if [ "${1:-}" != "" ]; then
  FROM_TAG="$1"
else
  FROM_TAG="$(git describe --tags --abbrev=0 2>/dev/null || echo "")"
fi

if [ -z "$FROM_TAG" ]; then
  echo "No tags found. Showing all commits."
  LOG_RANGE="HEAD"
else
  echo "Comparing: $FROM_TAG...HEAD"
  LOG_RANGE="$FROM_TAG..HEAD"
fi

# Collect commits
COMMITS="$(git log "$LOG_RANGE" --pretty=format:"%s|||%h|||%an" --no-merges 2>/dev/null)"

if [ -z "$COMMITS" ]; then
  echo ""
  echo "No commits since $FROM_TAG."
  exit 0
fi

# Category buckets
FEATURES=()
FIXES=()
PERF=()
DOCS=()
OTHER=()

while IFS='|||' read -r subject hash author; do
  [ -z "$subject" ] && continue

  # Match conventional commit prefixes
  type="$(echo "$subject" | grep -oE '^(feat|fix|perf|docs|chore|refactor|test|ci|build|style)(\([^)]+\))?' | head -1 || true)"
  prefix="$(echo "$type" | grep -oE '^[a-z]+' || true)"

  # Strip conventional commit prefix from display
  if [ -n "$prefix" ]; then
    display="$(echo "$subject" | sed -E 's/^[a-z]+(\([^)]+\))?!?: //')"
  else
    display="$subject"
  fi

  entry="- $display (\`$hash\`) — $author"

  case "$prefix" in
    feat)        FEATURES+=("$entry") ;;
    fix)         FIXES+=("$entry") ;;
    perf)        PERF+=("$entry") ;;
    docs)        DOCS+=("$entry") ;;
    chore|ci|build|style) ;; # silently skip noise
    *)           OTHER+=("$entry") ;;
  esac
done <<< "$COMMITS"

echo ""
echo "========================================"
echo " Changelog Preview"
if [ -n "$FROM_TAG" ]; then
  echo " Since: $FROM_TAG"
fi
echo "========================================"

print_section() {
  local title="$1"
  shift
  local items=("$@")
  if [ "${#items[@]}" -gt 0 ]; then
    echo ""
    echo "## $title"
    for item in "${items[@]}"; do
      echo "$item"
    done
  fi
}

print_section "New Features"    "${FEATURES[@]+"${FEATURES[@]}"}"
print_section "Bug Fixes"       "${FIXES[@]+"${FIXES[@]}"}"
print_section "Performance"     "${PERF[@]+"${PERF[@]}"}"
print_section "Documentation"   "${DOCS[@]+"${DOCS[@]}"}"
print_section "Other Changes"   "${OTHER[@]+"${OTHER[@]}"}"

echo ""
echo "========================================"
TOTAL=$(echo "$COMMITS" | grep -c '|||' || true)
echo " Total commits: $TOTAL"
echo "========================================"
