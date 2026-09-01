"""Environment variable loader for Haike Video.

Loads .env file and provides typed access to environment configuration.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


def load_env(project_root: Optional[Path] = None) -> None:
    """Load local settings in portable, non-destructive precedence order.

    The repository keeps credentials in ``.env.secrets.local`` so the
    project can be copied or published without exposing provider keys.  A
    developer's existing ``.env.local`` still wins over the checked-in
    template, while process-level environment variables always win because
    every load uses ``override=False``.
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent
    for env_path in (
        project_root / ".env.secrets.local",
        project_root / ".env.local",
        project_root / ".env",
    ):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    """Get an environment variable with optional default."""
    return os.environ.get(key, default)


def require_env(key: str) -> str:
    """Get a required environment variable. Raises if missing."""
    value = os.environ.get(key)
    if value is None:
        raise EnvironmentError(f"Required environment variable {key!r} is not set")
    return value
