"""Deployment admission guard backed by a live OS lock, not a stale marker."""
import os
from pathlib import Path
import re

from zeus import PROJECT_ROOT
from zeus.process_control import ProcessLock


def _legacy_update_running(pid):
    """Recognize the original updater only; PID reuse cannot keep it blocked."""
    if os.name != 'posix':
        return False
    try:
        with Path(f'/proc/{pid}/cmdline').open('rb') as stream:
            args = stream.read(4096).split(b'\0')
        return (bool(args) and Path(os.fsdecode(args[0])).name == 'bash'
                and any(os.fsdecode(arg).endswith('deploy/refresh-workspace.sh') for arg in args[1:]))
    except OSError:
        return False


def maintenance_active(root=None):
    root = Path(root or os.environ.get('ZEUS_MAINTENANCE_ROOT') or PROJECT_ROOT/'.zeus-state')
    lock = root/'maintenance.lock'
    if lock.exists():
        try:
            with ProcessLock(lock, timeout=0):
                pass
        except (TimeoutError, OSError):
            return True
    # Compatibility with the first updater: ignore abandoned/malformed markers.
    try:
        with (root/'maintenance').open(encoding='utf-8') as stream:
            marker = stream.read(128).strip()
    except OSError:
        return False
    match = re.fullmatch(r'Workspace update ([1-9][0-9]*)', marker)
    return bool(match and _legacy_update_running(int(match[1])))
