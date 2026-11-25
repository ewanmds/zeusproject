"""Side-effect-free project configuration helpers.

The multiuser workspace imports request models and manager classes from the
legacy server module. Importing that module must not load the operator `.env`,
create the private-dashboard manager, or start its recovery thread.
"""
import os
from pathlib import Path

from zeus import PROJECT_ROOT

BASE_DIR = PROJECT_ROOT


def load_dotenv(path: Path) -> None:
    """Read a .env file and populate os.environ (does not overwrite)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if not key:
            continue
        value = value.strip()
        # Quoted values keep their content verbatim; an unquoted trailing
        # comment (`TOKEN=abc  # rotate`) is not part of the value.
        if value[:1] in ('"', "'"):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)
