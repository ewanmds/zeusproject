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
from zeus.safety import SafetyLatch, safe_error, supervisor_alive
from zeus.markets import resolve_symbol
from zeus.network import post_info

ROOT = PROJECT_ROOT / 'integrations' / 'freqtrade'
_lock_handle = None
FIELDS = {'cleanup_confirmed', 'cleanup_error', 'can_start', 'quoting_enabled', 'phase', 'running', 'position_known', 'position', 'position_usd', 'entry',
          'accounting_complete',
          'mid', 'spread_bps', 'upnl', 'action', 'error', 'volume', 'fees', 'rpnl',
          'fills', 'order_placements', 'order_modifications', 'order_cancellations',
          'order_requests', 'cost_per_1m',
          'markout_bid_bps', 'markout_ask_bps', 'quote_half_spread_bps',
          'edge_required_bps', 'edge_margin_bps', 'measured_fee_bps'}


def derive_metrics(state):
    """Add per-session unit economics the dashboard can display directly."""
    volume = state.get('volume') or 0
    fees = state.get('fees') or 0
    rpnl = state.get('rpnl') or 0
    state['fee_bps'] = fees / volume * 1e4 if volume > 0 else None
    state['net_pnl'] = rpnl - fees


def resolve_token(token):
    """Resolve a preset, a bare ticker or a dex-qualified HIP-3 market."""
    try:
        return resolve_symbol(token)
    except ValueError as exc:
        raise ValueError(f'Marché inconnu : {token} ({exc})') from exc


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
        manifest = json.loads((ROOT / 'bin' / 'build.json').read_text(encoding='utf-8'))
        source = manifest['source']
        expected = manifest['binaries'][path.name]
        if not isinstance(source, str) or not isinstance(expected, str):
            raise TypeError
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('Binaire Freqtrade non vérifié (manifeste de compilation absent ou illisible) : '
                         'python -m zeus.bots.freqtrade --build') from None
    if expected.lower() != hashlib.sha256(path.read_bytes()).hexdigest():
        raise ValueError('Binaire Freqtrade non vérifié (empreinte différente du manifeste) : '
                         'python -m zeus.bots.freqtrade --build')
    if source != engine_digest():
        raise ValueError('Binaire Freqtrade périmé (sources Go modifiées depuis la compilation) : '
                         'python -m zeus.bots.freqtrade --build')
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


def _write_manifest(manifest):
    """Replace the build manifest atomically so it always matches the binaries."""
    target = ROOT / 'bin' / 'build.json'
    temporary = target.with_name(target.name + '.tmp')
    temporary.write_text(json.dumps(manifest), encoding='utf-8')
    os.replace(temporary, target)


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
    binaries = ROOT / 'bin'
    binaries.mkdir(parents=True, exist_ok=True)
    build_dir = ROOT / '.build'
    build_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='zeus_freqtrade_build_', dir=build_dir))
    platforms = [('windows', 'freqtrade-windows-amd64.exe'), ('linux', 'freqtrade-linux-amd64')]
    try:
        for target, filename in platforms:
            env = os.environ.copy()
            env.update(GOOS=target, GOARCH='amd64', CGO_ENABLED='0')
            subprocess.run([go, 'build', '-trimpath', '-o', str(staging / filename), './cmd/bot'],
                           cwd=ROOT / 'engine', env=env, check=True)
        if os.name != 'nt':
            (staging / platforms[-1][1]).chmod(0o755)
        # Ne remplacer les binaires qu'une fois les deux compilations réussies :
        # une compilation interrompue ne peut plus laisser un mélange incohérent.
        try:
            for _, filename in platforms:
                os.replace(staging / filename, binaries / filename)
        except PermissionError:
            raise RuntimeError('Binaire Freqtrade verrouillé par un bot en cours ; '
                               'arrêter le bot puis relancer --build') from None
        manifest = dict(source=engine_digest(),
                        binaries={name: hashlib.sha256((binaries / name).read_bytes()).hexdigest()
                                  for _, name in platforms})
        _write_manifest(manifest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    require_binary()


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
    # Zeus keeps a conservative 40 USD minimum even though the generated
    # single order quotes the full configured budget on each side.
    if not math.isfinite(amount) or amount < 40:
        raise ValueError('Montant minimal Freqtrade : 40 USD (100 % par côté, un ordre par côté)')
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
    config['signal_pauses_enabled'] = True  # Parité Bureau : pauses/widen sur signaux actifs
    config['strategy']['assets'] = [symbol]
    config['strategy']['spread_engine_enabled'] = False
    for section in ('market_making', 'execution', 'risk'):
        config[section].pop('per_asset', None)
    # The dashboard amount is both the position cap and the notional quoted on
    # each side. A single fill may therefore consume the full position budget.
    quote_notional = round(amount, 2)
    levels = 1
    config['market_making'].update(quote_notional=quote_notional, ladder_levels=levels,
                                  adaptive_notional_enabled=False, trading_schedule_enabled=False,
                                  volume_burst_enabled=False, wall_front_enabled=False)
    config['risk'].update(max_position_notional=amount,
                          max_spread_exposure=round(amount * 1.1, 2),
                          max_aggregate_exposure=round(amount * 1.1, 2),
                          max_open_orders=2 * levels + 2,
                          # Zeus must not cancel a live session for account-level
                          # equity drawdown or session PnL. Position, data-health,
                          # account and manual-stop protections remain active.
                          max_drawdown_pct=100.0, max_consecutive_halts=0,
                          session_stop_loss_bps=0.0,
                          session_flatten_on_shutdown=True, session_flatten_timeout_sec=15,
                          # A transient API/order-state race must not end the Zeus
                          # session permanently. Recovery is bounded and cancels
                          # all orders before resuming.
                          fail_closed_cooldown_sec=15, max_fail_closed_retries=5)
    config['execution'].update(live_mode=True, leverage=min(int(os.getenv('OBI_LEVERAGE', '30')),
                                                           int(contract['maxLeverage'])))
    config['postmortem'] = {'enabled': False}
    config['rate_limit']['disabled'] = True  # Parité avec config.live.yaml du Bureau ; les limites d'échange restent actives
    return config


# Edge gate reference values (bps). They mirror the engine preset: measured fee
# floor + adverse-selection buffer + half the target net edge per round trip.
EDGE_FEE_BPS = 0.5
EDGE_ADVERSE_BPS = 0.75
EDGE_TARGET_BPS = 1.0
def required_half_spread_bps():
    return EDGE_FEE_BPS + EDGE_ADVERSE_BPS + EDGE_TARGET_BPS / 2.0


def market_eligibility(symbol):
    """Read the live book for diagnostics before the engine starts.

    The Go edge gate controls each live quote. A narrow snapshot is a warning,
    not proof that every later quote is uneconomic.
    """
    book = post_info('https://api.hyperliquid.xyz', {'type': 'l2Book', 'coin': symbol})
    levels = book.get('levels') if isinstance(book, dict) else None
    if not isinstance(levels, list) or len(levels) != 2 or not levels[0] or not levels[1]:
        raise ValueError('Carnet indisponible : impossible de vérifier le spread venu')
    try:
        best_bid = float(levels[0][0]['px'])
        best_ask = float(levels[1][0]['px'])
    except (KeyError, TypeError, ValueError, IndexError):
        raise ValueError('Carnet invalide : prix venu illisible') from None
    mid = (best_bid + best_ask) / 2.0
    if not math.isfinite(mid) or mid <= 0 or best_ask <= best_bid:
        raise ValueError('Carnet invalide : spread venu incohérent')
    venue_half = (best_ask - best_bid) / mid * 10000.0 / 2.0
    required = required_half_spread_bps()
    return {'venue_half_bps': venue_half, 'required_half_bps': required,
            'warning': venue_half < required}


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
    stop = threading.Event()
    stop_path = Path(stop_file_path(symbol))
    session = uuid.uuid4().hex
    state = dict(symbol=symbol, strategy='freqtrade', live=True, start_time=time.time(),
                 phase='connecting', running=True, position_known=False,
                 action='Validation Freqtrade', error='', volume=0, fees=0, rpnl=0, fills=0,
                 cost_per_1m=None, notional=amount)
    safety = SafetyLatch(path=os.getenv("ZEUS_SAFETY_FILE"))
    first_error = ""
    def write():
        state['ts'] = time.time()
        derive_metrics(state)
        if safety.path:
            safety.phase = state["phase"]
            safety.error = state.get("error", "")
            safety.cleanup_error = state.get("cleanup_error", "")
            safety.cleanup_confirmed = state.get("cleanup_confirmed") is True
            safety.publish()
        publish(stats_file_path(symbol), state)
    def halted(*_):
        stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, halted)
    write()
    child = None
    temporary = None
    try:
        binary = require_binary()
        metadata = post_info('https://api.hyperliquid.xyz', {'type': 'metaAndAssetCtxs', 'dex': symbol.split(':')[0]})
        config = session_config(symbol, amount, metadata)
        edge = market_eligibility(symbol)
        edge_note = ' | cotations élargies par le moteur' if edge['warning'] else ''
        state.update(action=f"FREQTRADE | spread venu {edge['venue_half_bps']:.2f} bps, edge requis {edge['required_half_bps']:.2f} bps{edge_note}")
        write()
        temporary = tempfile.TemporaryDirectory(prefix='zeus_freqtrade_')
        directory = temporary.name
        config_path = Path(directory) / 'config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        go_stop = Path(directory) / 'stop.flag'
        # Observe the Zeus sentinel during read-only preflight as well.
        def watch():
            while not stop.wait(0.1):
                if stop_path.exists() or not supervisor_alive():
                    stop.set()
        threading.Thread(target=watch, daemon=True).start()
        # Même séquence que start-live.ps1 du Bureau : un seul -check-account,
        # puis démarrage direct (pas de -validate supplémentaire).
        preflight(binary, config_path, '-check-account', stop)
        if not confirm_flat(symbol, os.getenv("WALLET_ADDRESS", "")):
            raise RuntimeError("Existing orders/position or unreadable account; no session started")
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
        stop_deadline = None
        while child.poll() is None or reader.is_alive() or not updates.empty():
            if stop.is_set():
                go_stop.touch(exist_ok=True)
                if stop_deadline is None:
                    stop_deadline = time.monotonic() + 90
                if child.poll() is None and time.monotonic() >= stop_deadline:
                    raise TimeoutError("Freqtrade stop not confirmed; child process still alive")
            try:
                snapshot = updates.get(timeout=0.1)
                if snapshot.get("error") and not first_error:
                    first_error = safe_error(snapshot["error"])
                    stop.set()
                state.update(snapshot)
                if first_error:
                    state["error"] = first_error
                write()

            except queue.Empty:
                pass
        child.wait()
        child.stdout.close()
        if child.returncode != 0 or state.get('cleanup_confirmed') is not True:
            state.update(phase='failed', running=False, position_known=False,
                         error=state.get('error') or f'Freqtrade interrompu (code {child.returncode}) ; état à vérifier')
    except BaseException as exc:
        state.update(phase='failed', running=False, cleanup_confirmed=False, can_start=False, position_known=False, error=first_error or safe_error(exc))
        if child is not None and child.poll() is None:
            go_stop.touch(exist_ok=True)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                state["cleanup_error"] = "Go child still alive; sentinel retained"
    finally:
        stop.set()
        if temporary is not None:
            if child is not None and child.poll() is None:
                temporary._finalizer.detach()  # retain stop sentinel for the surviving child
                if safety.path:
                    safety.metadata["child_pid"] = child.pid
            else:
                temporary.cleanup()
        write()
    return 0 if state.get('cleanup_confirmed') is True else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--token', default='SP500')
    parser.add_argument('--notional', type=float, default=40)
    parser.add_argument('--strategy', choices=['freqtrade'], default='freqtrade')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.build:
        build()
        return 0
    if not args.live:
        parser.error('--live est obligatoire ; aucun ordre lancé')
    if not math.isfinite(args.notional) or args.notional < 40:
        parser.error('--notional doit être fini et supérieur ou égal à 40 USD')
    return run(args.token, args.notional)


if __name__ == '__main__':
    sys.exit(main())
