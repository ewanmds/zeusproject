"""Zeus process/telemetry adapter. Pricing and reconciliation stay in original.py."""
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

from market_catalog import TOKENS
from startup_network import post_info
from . import original as engine

_locks = []


def paths(symbol):
    safe = ''.join(c if c.isalnum() else '_' for c in symbol)
    root = Path(tempfile.gettempdir())
    return root / f'hl_obi_stats_{safe}.json', root / f'hl_obi_stop_{safe}.flag'


def acquire_lock(symbol):
    """Same per-symbol lock as BBO/MID, plus the standalone original's lock."""
    safe = ''.join(c if c.isalnum() else '_' for c in symbol)
    handle = open(Path(tempfile.gettempdir()) / f'hl_obi_bot_{safe}.lock', 'a+b')
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
        _locks.append(handle)
    except OSError:
        handle.close()
        raise RuntimeError(f'Un bot tourne déjà sur {symbol}')
    if not engine.acquire_instance_lock(symbol):
        raise RuntimeError(f'Le moteur original tourne déjà sur {symbol}')


def configure(args):
    if not args.live:
        raise ValueError('Le démarrage réel doit être explicite')
    if not math.isfinite(args.notional) or args.notional < 10.5:
        raise ValueError('Notionnel minimal : 10,50 USD')
    if not math.isfinite(args.grid_maker_fee_bps) or not 0 <= args.grid_maker_fee_bps < 10000:
        raise ValueError('Estimation des frais maker invalide')
    token = args.token.strip().upper()
    if token not in TOKENS:
        raise ValueError(f'Marché inconnu : {token}')
    symbol = TOKENS[token]['symbol']
    dex = symbol.split(':', 1)[0]
    raw, contexts = post_info(engine.BASE_URL, {'type': 'metaAndAssetCtxs', 'dex': dex})
    for index, item in enumerate(raw.get('universe', [])):
        if engine._qualified_hip3_symbol(dex, item.get('name', '')) != symbol:
            continue
        if item.get('isDelisted') or index >= len(contexts):
            raise ValueError(f'Contrat indisponible : {symbol}')
        mid = float(contexts[index].get('midPx') or 0)
        if not math.isfinite(mid) or mid <= 0:
            raise ValueError('Mid invalide pour le dimensionnement')
        decimals = int(item['szDecimals'])
        step = 10.0 ** -decimals
        price_decimals = max(0, 6 - decimals)
        tick = max(engine._derive_tick_size(mid), 10.0 ** -price_decimals)
        engine.SYMBOL, engine.PERP_DEX = symbol, dex
        engine.TICK_SIZE, engine.MIN_SIZE = tick, step
        engine.SIZE_DECIMALS = decimals
        engine.PRICE_DECIMALS = price_decimals
        engine.ORDER_SIZE = engine._round_up_to_step(args.notional / mid, step, decimals)
        engine.LEVERAGE = min(max(1, int(float(os.getenv('OBI_LEVERAGE', '20')))), int(item['maxLeverage']))
        engine.IS_CROSS = not (item.get('onlyIsolated') or str(item.get('marginMode', '')).lower() == 'nocross')
        # Original default profile, explicit so legacy shell variables cannot select another mode.
        engine.GRID_QUOTE_MODE = 'safe_bbo'
        engine.JOIN_BOOK_SPREAD_WHEN_SAFE = True
        # Trend still widens the spread; only its quote-cutoff is disabled.
        engine.TREND_FILTER_ENABLED = False
        engine.FIXED_HALF_SPREAD_TICKS = 0
        engine.MAKER_FEE_BPS = args.grid_maker_fee_bps
        engine.NUM_LEVELS, engine.MAX_INVENTORY_ORDERS = 1, 1
        engine.GRID_RESET_TICKS = 8.0
        engine.MIN_QUOTE_CANCEL_SEC = 2.0
        engine.JOURNAL_DIR = str(Path(__file__).resolve().parents[1] / 'logs' / 'mm_grid')
        return symbol
    raise ValueError(f'Contrat introuvable : {symbol}')


class CountedExchange:
    def __init__(self, client, bot):
        self.client, self.bot = client, bot

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _count(self, placements=0, cancellations=0):
        with self.bot.lock:
            self.bot.order_placements += placements
            self.bot.order_cancellations += cancellations
            self.bot.order_requests += 1

    def bulk_orders(self, requests):
        self._count(placements=len(requests))
        return self.client.bulk_orders(requests)

    def bulk_cancel(self, requests):
        self._count(cancellations=len(requests))
        return self.client.bulk_cancel(requests)

    def order(self, *args, **kwargs):
        self._count(placements=1)
        return self.client.order(*args, **kwargs)


class ZeusGrid(engine.GridMarketMaker):
    def __init__(self):
        self.order_placements = self.order_cancellations = self.order_requests = 0
        self.last_stats_write = 0
        self.integration_error = ''
        self.stats_path, self.stop_path = paths(engine.SYMBOL)
        super().__init__()

    def _set_leverage(self):
        # Parent constructor calls this before any order can be submitted.
        self.exchange = CountedExchange(self.exchange, self)
        self.exchange.client.timeout = 10
        self.info.timeout = 10
        super()._set_leverage()

    def write_stats(self, phase='running'):
        with self.lock:
            metrics = self._metrics_locked()
            data = dict(symbol=engine.SYMBOL, strategy='grid_adaptive', live=True,
                        running=self.running, phase=phase, ts=time.time(), start_time=self.start_time,
                        mid=self.mid, position=self.position, position_usd=self.position * self.mid,
                        entry=self.entry_price, upnl=self.unrealized_pnl, equity=self.equity,
                        volume=self.total_volume, fees=self.total_fees, rpnl=self.session_pnl,
                        cost_per_1m=metrics['realized_cpm'], fills=self.fills_count,
                        order_placements=self.order_placements, order_modifications=0,
                        order_cancellations=self.order_cancellations, order_requests=self.order_requests,
                        notional=engine.ORDER_SIZE * self.mid,
                        spread_bps=(self.best_ask - self.best_bid) / self.mid * 10000 if self.mid else 0,
                        action=self.last_action, error=self.integration_error or self.last_order_error,
                        grid_regime='warmup' if not self.regime.ready else 'trend' if self.regime.is_trend else 'chop',
                        grid_trend_score=self.regime.trend_score, grid_atr=self.regime.atr,
                        grid_half_spread_bps=self.cur_half_spread / self.mid * 10000 if self.mid else 0,
                        grid_maker_fee_bps=engine.MAKER_FEE_BPS, funding_included=False,
                        maker_volume=self.maker_volume, taker_volume=self.taker_volume)
        temporary = self.stats_path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(data, allow_nan=False), encoding='utf-8')
        os.replace(temporary, self.stats_path)

    def control(self):
        if self.stop_path.exists():
            self.stop_path.unlink(missing_ok=True)
            self.running = False
        if time.time() - self.last_stats_write >= 1:
            self.last_stats_write = time.time()
            self.write_stats('running' if self.ws_ready else 'connecting')

    def run(self):
        """Original engine cycles and cleanup, with Zeus stop sentinel and stats."""
        failed = False
        try:
            while self.running and not self.ws_ready:
                self.control()
                if self.running:
                    self._refresh_bbo_from_rest()
                    self.wake.wait(0.5)
            if self.running:
                threading.Thread(target=self._sync_thread, daemon=True).start()
            while self.running:
                self.wake.wait(engine.LOOP_INTERVAL_SEC)
                self.wake.clear()
                self.control()
                if self.running:
                    self.loop_once()
        except Exception as exc:
            failed = True
            self.integration_error = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            self.running = False
            # Use the original REST union cancellation + reduce-only IOC close.
            try:
                self.flatten('stop')
                self.sync_recent_fills(force=True)
                self.sync_position()
                self._record_metrics(force=True)
                if self.order_book.orders or self.order_book.uncertain or abs(self.position) >= engine.MIN_SIZE * 0.5:
                    failed = True
                    self.integration_error = 'Arrêt à vérifier : ordres ou position non confirmés à zéro'
            except Exception as exc:
                failed = True
                self.integration_error = f'Arrêt incomplet : {type(exc).__name__}: {exc}'
            finally:
                try:
                    self.ws_info.disconnect_websocket()
                except Exception:
                    pass
                self.journal.emit('session_end', funding_included=False)
                self.journal.close()
                self.write_stats('failed' if failed else 'stopped')


def run(args):
    symbol = TOKENS.get(args.token.strip().upper(), {}).get('symbol')
    if symbol is None:
        raise ValueError('Marché inconnu')
    stats_path, _ = paths(symbol)
    started = time.time()
    acquire_lock(symbol)
    def startup(phase, error=''):
        data = dict(symbol=symbol, strategy='grid_adaptive', start_time=started,
                    ts=time.time(), live=True, running=phase != 'failed', phase=phase,
                    action='Connexion à Hyperliquid', error=error)
        temporary = stats_path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(data), encoding='utf-8')
        os.replace(temporary, stats_path)
    startup('connecting')
    try:
        configure(args)
        ZeusGrid().run()
    except Exception as exc:
        # Preserve final accounting/error if run() already wrote it.
        current = json.loads(stats_path.read_text(encoding='utf-8'))
        if current.get('phase') != 'failed':
            startup('failed', f'{type(exc).__name__}: {exc}')
        raise
