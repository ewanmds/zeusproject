"""Best-effort atomic telemetry: a busy Windows reader must not stop trading."""
import json
import logging
import os
from pathlib import Path
import tempfile
import time

_last_warning = {}
_logger = logging.getLogger(__name__)


def write_json_snapshot(path, data):
    """Publish a complete snapshot, retaining the previous one on I/O failure.

    Use a unique sibling file and close it before replace (required on Windows).
    Retry sharing violations briefly; persistent telemetry errors are nonfatal.
    Serialization errors are deliberately not hidden as filesystem failures.
    """
    payload = json.dumps(data, allow_nan=False)
    target = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=target.parent, prefix=target.name + '.',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        for attempt in range(3):
            try:
                os.replace(temporary, target)
                return True
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.01 * (attempt + 1))
    except OSError as exc:
        now = time.monotonic()
        key = str(target)
        if now - _last_warning.get(key, float('-inf')) >= 30:
            _last_warning[key] = now
            _logger.warning('Statistiques non publiées ; prochain essai au prochain snapshot : %s', exc)
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
