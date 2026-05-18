"""
env_loader — load the repo's `.env` file into `os.environ` at import time.

Use:
    import core.env_loader   # noqa: F401   # must be the FIRST import

That single import line at the top of any entry point (CLI script, Streamlit
app, test) populates `os.environ` from the repo's `.env` file. After that,
every downstream module reading `os.environ.get("GCP_PROJECT_ID")` etc. just
works.

By design:
  - Reads only `<repo_root>/.env`. No env-file search paths, no override
    chains — one file, one rule.
  - Does NOT override variables already set in the shell environment.
    (`load_dotenv(override=False)`.) This matters for CI where vars are
    injected by the runner, and for `GOOGLE_APPLICATION_CREDENTIALS` which
    you may want to override per-run.
  - Silent no-op if `.env` doesn't exist (e.g. unit tests, sandbox runs).
  - Idempotent — importing twice is safe; the second call is a noop because
    the env is already populated.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"


def _load() -> None:
    if not _ENV_FILE.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        # python-dotenv not installed. Surface a clear log line but don't
        # crash — unit tests and sandbox runs should keep working.
        logger.warning(
            "env_loader: .env exists at %s but python-dotenv is not installed. "
            "Install it via `make install` (it's in requirements.txt).",
            _ENV_FILE,
        )
        return
    load_dotenv(_ENV_FILE, override=False)
    logger.debug("env_loader: loaded %s into os.environ (no override)", _ENV_FILE)


# Run on import. The whole point of this module is the side effect.
_load()
