"""Container healthcheck: exits non-zero when the bot heartbeat is missing or stale."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from pca.config import get_settings

MAX_AGE_SECONDS = 90


def main() -> int:
    path = Path(get_settings().heartbeat_path)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return 1
    return 0 if age < MAX_AGE_SECONDS else 1


if __name__ == "__main__":
    sys.exit(main())
