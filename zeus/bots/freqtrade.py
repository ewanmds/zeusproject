"""Zeus adapter for the preserved Go market maker. Never starts at import time."""
import argparse
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from zeus import PROJECT_ROOT
from zeus.markets import TOKENS
from zeus.network import post_info

ROOT = PROJECT_ROOT / 'integrations' / 'freqtrade'
_lock_handle = None
FIELDS = {'phase', 'running', 'position_known', 'position', 'position_usd', 'entry',
          'accounting_complete',
          'mid', 'spread_bps', 'upnl', 'action', 'error', 'volume', 'fees', 'rpnl',
          'fills', 'order_placements', 'order_modifications', 'order_cancellations',
          'order_requests', 'cost_per_1m'}


def resolve_token(token):
    key = token.strip().upper()
    if key not in TOKENS:
        raise ValueError(f'Marché inconnu : {key}')
    return TOKENS[key]['symbol']


def _safe(symbol):
    return ''.join(c if c.isalnum() else '_' for c in symbol)


def stats_file_path(symbol):
    return str(Path(tempfile.gettempdir()) / f'hl_obi_stats_{_safe(symbol)}.json')


def stop_file_path(symbol):
    return str(Path(tempfile.gettempdir()) / f'hl_obi_stop_{_safe(symbol)}.flag')


def binary_path():
    if platform.machine().lower() not in {'amd64', 'x86_64'}:
        raise ValueError('Architecture Freqtrade non prise en charge : un binaire amd64 est requis')
    return ROOT / 'bin' / ('freqtrade-windows-amd64.exe' if os.name == 'nt' else 'freqtrade-linux-amd64')


def engine_digest():
    digest = hashlib.sha256()
    for path in sorted((ROOT / 'engine').rglob('*')):
        if path.is_file() and ((path.suffix == '.go' and not path.name.endswith('_test.go')) or path.name in {'go.mod', 'go.sum'}):
            digest.update(path.relative_to(ROOT / 'engine').as_posix().encode())
            # Git's Windows newline conversion must not invalidate a source build.
            digest.update(path.read_bytes().replace(b'\r\n', b'\n'))
    return digest.hexdigest()


def require_binary():
    path = binary_path()
    if not path.is_file() or (os.name != 'nt' and not os.access(path, os.X_OK)):
        raise ValueError('Binaire Freqtrade absent : python -m zeus.bots.freqtrade --build')
    try:
        manifest = json.loads((ROOT / 'bin' / 'build.json').read_text())
        valid = (manifest['source'] == engine_digest() and manifest['binaries'][path.name] == hashlib.sha256(path.read_bytes()).hexdigest())
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise ValueError('Binaire Freqtrade périmé ou non vérifié : python -m zeus.bots.freqtrade --build')
    return path


def confirm_flat(symbol, account):
    """Fresh read-only confirmation before replacing an unconfirmed stopped run."""
    if not account:
        return False
    dex, bare = symbol.split(':', 1)
    state = post_info('https://api.hyperliquid.xyz', {'type': 'clearinghouseState', 'user': account, 'dex': dex})
    if not isinstance(state, dict) or not isinstance(state.get('assetPositions'), list):
        return False
    for item in state['assetPositions']:
        if not isinstance(item, dict) or not isinstance(item.get('position'), dict):
            return False
        position = item.get('position', {})
        if not position.get('coin'):
            return False
        if position['coin'] in (symbol, bare):
            try:
                size = float(position['szi'])
            except (KeyError, TypeError, ValueError):
                return False
            if not math.isfinite(size) or size != 0:
                return False
    orders = post_info('https://api.hyperliquid.xyz', {'type': 'openOrders', 'user': account, 'dex': dex})
    return isinstance(orders, list) and all(isinstance(order, dict) and order.get('coin') and order['coin'] not in (symbol, bare) for order in orders)


def build():
    """Build explicit host and Linux artifacts, never run the trading engine."""
    import shutil
    go = shutil.which('go')
    if go is None:
        candidate = ROOT / '.build' / 'go' / 'bin' / ('go.exe' if os.name == 'nt' else 'go')
        if candidate.is_file():
            go = str(candidate)
    if go is None:
        raise RuntimeError('Go 1.25+ requis pour compiler Freqtrade')
    (ROOT / 'bin').mkdir(exist_ok=True)
    platforms = [('windows', 'freqtrade-windows-amd64.exe'), ('linux', 'freqtrade-linux-amd64')]
    for target, filename in platforms:
        env = os.environ.copy()
        env.update(GOOS=target, GOARCH='amd64', CGO_ENABLED='0')
        subprocess.run([go, 'build', '-trimpath', '-o', str(ROOT / 'bin' / filename), './cmd/bot'],
                       cwd=ROOT / 'engine', env=env, check=True)
    if os.name != 'nt':
        binary_path().chmod(0o755)
    manifest = dict(source=engine_digest(), binaries={name: hashlib.sha256((ROOT / 'bin' / name).read_bytes()).hexdigest() for _, name in platforms})
    (ROOT / 'bin' / 'build.json').write_text(json.dumps(manifest), encoding='utf-8')


def acquire_lock(symbol):
    global _lock_handle
    handle = open(Path(tempfile.gettempdir()) / f'hl_obi_bot_{_safe(symbol)}.lock', 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(f'Un bot tourne déjà sur {symbol}')
    _lock_handle = handle


def publish(path, state):
    """A Windows sharing violation must never tear down the engine."""
    payload = json.dumps(state, allow_nan=False)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=Path(path).parent,
                                         prefix=Path(path).name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        for attempt in range(3):
            try:
                os.replace(temporary, path)
                return True
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.01 * (attempt + 1))
    except OSError:
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def session_config(symbol, amount, metadata):
    if not math.isfinite(amount) or amount < 10:
        raise ValueError('Montant minimal : 10 USD')
    dex, bare = symbol.split(':', 1)
    universe, contexts = metadata
    selected = next(((i, row) for i, row in enumerate(universe['universe'])
                     if row.get('name') in (symbol, bare)), None)
    if selected is None:
        raise ValueError('Contrat introuvable')
    index, contract = selected
    if contract.get('isDelisted') or index >= len(contexts):
        raise ValueError('Contrat indisponible')
    mid = float(contexts[index].get('midPx') or 0)
    step = 10 ** -int(contract['szDecimals'])
    if not math.isfinite(mid) or mid <= 0 or math.floor(amount / mid / step) * step * mid < 10:
        raise ValueError('Montant insuffisant après arrondi à la précision du contrat')
    config = json.loads((ROOT / 'profile.json').read_text(encoding='utf-8'))
    config.pop('env', None)
    config.pop('wallet_address', None)
    config['data_source'] = 'public'
    config['signal_pauses_enabled'] = False
    config['strategy']['assets'] = [symbol]
    config['strategy']['spread_engine_enabled'] = False
    for section in ('market_making', 'execution', 'risk'):
        config[section].pop('per_asset', None)
    config['market_making'].update(quote_notional=amount, ladder_levels=1,
                                  adaptive_notional_enabled=False, trading_schedule_enabled=False,
                                  volume_burst_enabled=False, wall_front_enabled=False)
    config['risk'].update(max_position_notional=amount, max_spread_exposure=amount,
                          max_aggregate_exposure=amount, max_open_orders=2,
                          session_flatten_on_shutdown=True, session_flatten_timeout_sec=15)
    config['execution'].update(live_mode=True, leverage=min(int(os.getenv('OBI_LEVERAGE', '20')),
                                                           int(contract['maxLeverage'])))
    config['postmortem'] = {'enabled': False}
    config['rate_limit']['disabled'] = False
    return config


def accept_snapshot(raw, session, symbol):
    if not isinstance(raw, dict) or raw.get('protocol') != 1 or raw.get('session_id') != session or raw.get('symbol') != symbol:
        return None
    result = {key: raw[key] for key in FIELDS if key in raw}
    if any(isinstance(value, float) and not math.isfinite(value) for value in result.values()):
        return None
    if any(not isinstance(value, (str, bool, int, float, type(None))) for value in result.values()):
        return None
    return result


def preflight(binary, config, flag, stop):
    # These two code paths only validate/read; terminating them cannot submit orders.
    with subprocess.Popen([str(binary), '-config', str(config), flag], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL) as process:
        deadline = time.monotonic() + 60
        while process.poll() is None:
            if stop.is_set() or time.monotonic() > deadline:
                process.terminate()
                process.wait(timeout=10)
                raise RuntimeError('Démarrage annulé ou contrôle préalable expiré ; aucun ordre lancé')
            stop.wait(0.1)
        if process.returncode != 0:
            raise RuntimeError(f'Contrôle Freqtrade échoué ({flag}) ; vérifier la configuration, le réseau et l’API wallet')


def run(token, amount):
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / '.env')
    symbol = resolve_token(token)
    acquire_lock(symbol)
    binary = require_binary()
    stop = threading.Event()
    stop_path = Path(stop_file_path(symbol))
    session = uuid.uuid4().hex
    state = dict(symbol=symbol, strategy='freqtrade', live=True, start_time=time.time(),
                 phase='connecting', running=True, position_known=False,
                 action='Validation Freqtrade', error='', volume=0, fees=0, rpnl=0, fills=0,
                 cost_per_1m=None, notional=amount)
    def write():
        state['ts'] = time.time()
        publish(stats_file_path(symbol), state)
    def halted(*_):
        stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, halted)
    write()
    child = None
    temporary = None
    try:
        metadata = post_info('https://api.hyperliquid.xyz', {'type': 'metaAndAssetCtxs', 'dex': symbol.split(':')[0]})
        config = session_config(symbol, amount, metadata)
        temporary = tempfile.TemporaryDirectory(prefix='zeus_freqtrade_')
        directory = temporary.name
        config_path = Path(directory) / 'config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        go_stop = Path(directory) / 'stop.flag'
        # Observe the Zeus sentinel during read-only preflight as well.
        def watch():
            while not stop.wait(0.1):
                if stop_path.exists():
                    stop.set()
        threading.Thread(target=watch, daemon=True).start()
        preflight(binary, config_path, '-validate', stop)
        preflight(binary, config_path, '-check-account', stop)
        if stop.is_set():
            raise RuntimeError('Démarrage annulé avant ordres')
        child = subprocess.Popen([str(binary), '-config', str(config_path), '-zeus-session', session,
                                  '-zeus-symbol', symbol, '-zeus-stop-file', str(go_stop)],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 encoding='utf-8', errors='replace', bufsize=1)
        updates = queue.Queue(maxsize=32)
        def read():
            while True:
                line = child.stdout.readline(65537)
                if not line:
                    break
                if len(line) > 65536:
                    continue
                try:
                    snapshot = accept_snapshot(json.loads(line), session, symbol)
                except ValueError:
                    continue
                if snapshot is not None:
                    # Keep the newest cumulative state, never block the Go writer.
                    if updates.full():
                        try:
                            updates.get_nowait()
                        except queue.Empty:
                            pass
                    updates.put_nowait(snapshot)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        while child.poll() is None or reader.is_alive() or not updates.empty():
            if stop.is_set():
                go_stop.touch(exist_ok=True)
            try:
                state.update(updates.get(timeout=0.1))
                write()
            except queue.Empty:
                pass
        child.wait()
        child.stdout.close()
        if child.returncode != 0 or state.get('phase') != 'stopped':
            state.update(phase='failed', running=False, position_known=False,
                         error=state.get('error') or f'Freqtrade interrompu (code {child.returncode}) ; état à vérifier')
    except Exception as exc:
        state.update(phase='failed', running=False, position_known=False, error=str(exc))
        if child is not None and child.poll() is None:
            go_stop.touch(exist_ok=True)
            child.wait(timeout=90)
    finally:
        stop.set()
        if temporary is not None:
            temporary.cleanup()
        write()
    return 0 if state.get('phase') == 'stopped' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--token', default='SP500')
    parser.add_argument('--notional', type=float, default=15)
    parser.add_argument('--strategy', choices=['freqtrade'], default='freqtrade')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.build:
        build()
        return 0
    if not args.live:
        parser.error('--live est obligatoire ; aucun ordre lancé')
    if not math.isfinite(args.notional) or args.notional < 10:
        parser.error('--notional doit être fini et supérieur ou égal à 10 USD')
    return run(args.token, args.notional)


if __name__ == '__main__':
    sys.exit(main())
