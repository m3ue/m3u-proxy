"""Run tests against the defaults CI sees, not a developer's local .env.

The settings object reads .env at import, so a local API_TOKEN or REDIS_ENABLED
would otherwise turn passing API tests into 401s or switch the pooling mode.
Real environment variables still apply, as they do in CI.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402

# Update in place: modules import this same object and must keep seeing it.
config.settings.__dict__.update(config.Settings(_env_file=None).__dict__)
