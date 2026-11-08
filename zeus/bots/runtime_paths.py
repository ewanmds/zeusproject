"""Credential-free paths shared by the dashboard and execution children."""
import os
import tempfile


def grid_path(symbol, kind, root=None):
    safe = ''.join(c if c.isalnum() else '_' for c in symbol)
    names = {'stats': f'hl_obi_stats_{safe}.json', 'stop': f'hl_obi_stop_{safe}.flag',
             'lock': f'hl_obi_bot_{safe}.lock'}
    location = root or (os.getenv('ZEUS_INSTANCE_DIR') if kind == 'lock' else None) or os.getenv('ZEUS_RUNTIME_DIR') or tempfile.gettempdir()
    return os.path.join(location, names[kind])
