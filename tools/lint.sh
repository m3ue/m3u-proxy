#!/usr/bin/env bash
# Fix lint issues and format code, like `vendor/bin/pint --dirty` in the editor.
# Usage:
#   ./tools/lint.sh           fix and format in place
#   ./tools/lint.sh --check   only report, exactly as CI does (nothing is changed)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Same paths CI checks (.github/workflows/ci.yml).
PATHS=(src/ tests/)

# Prefer this repo's venv, so another project's venv on PATH isn't used by mistake.
if [[ -x venv/bin/ruff ]]; then
    RUFF=venv/bin/ruff
elif command -v ruff &>/dev/null; then
    RUFF=ruff
else
    echo "ruff not found. Install it with: venv/bin/pip install ruff"
    exit 1
fi

# CI pins ruff, and formatter output can change between versions. Warn on a mismatch.
CI_VERSION="$(sed -n 's/.*pip install ruff==\([0-9.]*\).*/\1/p' .github/workflows/ci.yml | head -n 1)"
LOCAL_VERSION="$("$RUFF" --version | awk '{print $2}')"
if [[ -n "$CI_VERSION" && "$LOCAL_VERSION" != "$CI_VERSION" ]]; then
    echo "Warning: using ruff $LOCAL_VERSION ($RUFF), CI uses $CI_VERSION."
    echo "         Install the same version: venv/bin/pip install ruff==$CI_VERSION"
fi

if [[ "${1:-}" == "--check" ]]; then
    echo "Checking lint..."
    "$RUFF" check "${PATHS[@]}"

    echo "Checking formatting..."
    "$RUFF" format --check "${PATHS[@]}"
else
    echo "Fixing lint issues..."
    "$RUFF" check --fix --unsafe-fixes "${PATHS[@]}"

    echo "Formatting code..."
    "$RUFF" format "${PATHS[@]}"
fi

echo "Done."
