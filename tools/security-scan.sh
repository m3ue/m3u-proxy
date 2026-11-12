#!/usr/bin/env bash
# Check dependencies for known vulnerabilities, like `composer security:scan` in the editor.
# CI runs this same script (.github/workflows/ci.yml), so a pass here means a pass there,
# unless a new advisory is published in between.
# Usage:
#   ./tools/security-scan.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Prefer this repo's venv, so another project's venv on PATH isn't used by mistake.
if [[ -x venv/bin/pip-audit ]]; then
    PIP_AUDIT=venv/bin/pip-audit
elif command -v pip-audit &>/dev/null; then
    PIP_AUDIT=pip-audit
else
    echo "pip-audit not found. Install it with: venv/bin/pip install pip-audit"
    exit 1
fi

# Reviewed exceptions for the test dependencies only, each with its tracking issue.
# Never add one for the runtime dependencies: those ship in the image.
# - PYSEC-2026-1845 (GHSA-6w46-j5rx-g56g): pytest <9.0.3 tmpdir handling. Test-only,
#   never in the runtime image, and needs local access. The fix is blocked on the
#   httpx 0.28 upgrade; remove this once #92 lands.
TEST_EXCEPTIONS=(
    --ignore-vuln PYSEC-2026-1845
)

echo "Auditing runtime dependencies (shipped in the image)..."
"$PIP_AUDIT" -r requirements.txt --skip-editable

echo "Auditing test dependencies..."
"$PIP_AUDIT" -r requirements-dev.txt --skip-editable "${TEST_EXCEPTIONS[@]}"

echo "Done."
