"""Source revision exposed for deployment verification. No account state."""
from functools import lru_cache
import subprocess
from zeus import PROJECT_ROOT


@lru_cache(maxsize=1)
def build_revision():
    try:
        return subprocess.check_output(['git', 'rev-parse', '--short=12', 'HEAD'],
            cwd=PROJECT_ROOT, timeout=2, stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
