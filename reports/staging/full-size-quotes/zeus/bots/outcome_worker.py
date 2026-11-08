"""One supervised Outcome market per OS process. Importing never starts a bot."""
import argparse
import json
import os
from pathlib import Path
import threading
import time

from zeus.process_control import ProcessLock, account_key
from zeus.safety import atomic_json, safe_error, supervisor_alive


def build_config(public, safety_file, stop_file=None, funds_root=None):
    from zeus.bots.worldcup import BotConfig
    market = str(public["market"])
    if not market.startswith("#") or not market[1:].isdigit():
        raise ValueError("Select one exact Outcome market")
    encoded_side = int(market[1:]) % 10
    if encoded_side not in (0, 1):
        raise ValueError("Unsupported Outcome side")
    side = public.get("outcome_side") or ("yes" if encoded_side == 0 else "no")
    if side not in {"yes", "no"} or (side == "yes") != (encoded_side == 0):
        raise ValueError("Outcome side does not match its exact market coin")
    return BotConfig(
        base_url="https://api.hyperliquid.xyz", perp_dex="xyz", live=True,
        symbols=[market], keywords=[], max_symbols=1,
        order_usd=public["order_size_usd"], min_order_usd=public["order_size_usd"], close_min_order_usd=0,
        max_spread_pct=public.get("max_spread_pct") if public.get("max_spread_pct") is not None else public.get("max_spread_bps", 2) / 100,
        max_position_usd=public["max_position_usd"], min_action_interval_sec=0.03,
        stale_sec=1.2, refresh_hz=12, stop_loss_usd=50, take_profit_usd=0,
        rest_bbo_hz=4, rest_workers=24, rest_max_per_sweep=2, rest_429_backoff_sec=2.5,
        market_slippage_bps=5, market_cycle_cooldown_sec=0.12, account_sync_sec=2,
        account_address=os.getenv("WALLET_ADDRESS_OUTCOME", "").strip(),
        private_key=os.getenv("PRIVATE_KEY_OUTCOME", "").strip(),
        scan_only=False, worldcup=True, countries=[], outcome_side=side,
        exec_mode=public.get("exec_mode", "taker"), maker_offset_pct=0,
        maker_spread_pct=public.get("maker_spread_pct", 0.005), safety_file=str(safety_file),
        auto_max_spread_bps=public.get("auto_max_spread_bps", 10) if public.get("selection_mode") == "auto" else None,
        two_sided=public.get("two_sided", False),
        full_size_quotes=public.get("full_size_quotes", public.get("selection_mode") == "auto"),
        rotation_file=str(Path(stop_file).with_name("drain.json")) if stop_file else None,
        risk_pnl_carry=public.get("rotation_pnl_carry", 0),
        stop_file=str(stop_file) if stop_file else None,
        funds_root=str(funds_root) if funds_root else None,
    )


def run_worker(config_path, bot_factory=None):
    """bot_factory is injectable for offline process/lifecycle tests."""
    runtime = Path(config_path).resolve().parent
    public = json.loads(Path(config_path).read_text(encoding="utf-8"))
    journal = Path(public["safety_file"])
    stop_file, stats_file = runtime / "stop", runtime / "stats.json"
    cfg = build_config(public["config"], journal, stop_file, public["funds_root"])
    if not cfg.account_address or not cfg.private_key:
        raise ValueError("Outcome credentials unavailable in worker environment")
    lock_path = Path(public["locks_root"]) / f"{account_key(cfg.account_address)}-{cfg.symbols[0][1:]}.lock"
    finished = threading.Event()
    bot = None
    started = time.time()
    metadata = json.loads(journal.read_text(encoding="utf-8"))

    def publish():
        snap = bot.export_dashboard_stats() if bot is not None else {}
        if snap is None:
            return  # never refresh the timestamp of stale data
        lifecycle = bot.safety.snapshot() if bot is not None else metadata
        atomic_json(stats_file, {**snap, **lifecycle, "session_id": metadata["session_id"],
                    "market": cfg.symbols[0], "pid": os.getpid(), "started": started,
                    "ts": time.time(), "config": public["config"]})

    def monitor():
        while not finished.wait(0.25):
            if bot is not None:
                if stop_file.exists() or not supervisor_alive():
                    bot.request_stop()
                try:
                    publish()
                except Exception as exc:
                    bot.safety.fail("Snapshot publication failed: " + safe_error(exc))

    watcher = None
    instance_lock = ProcessLock(lock_path, timeout=0)
    acquired = False
    try:
        instance_lock.__enter__()
        acquired = True
        if bot_factory is None:
            from zeus.bots.worldcup import HyperliquidSpreadBot
            bot_factory = HyperliquidSpreadBot
        atomic_json(journal, {**metadata, "pid": os.getpid()})
        publish()
        # Constructor may block on API reads. The persisted stop file is
        # checked by the execution gate, including before its first order.
        bot = bot_factory(cfg)
        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        if stop_file.exists() or not supervisor_alive():
            bot.request_stop()
        else:
            bot.run()
    except BaseException as exc:
        if bot is not None:
            bot.safety.fail(exc)
        else:
            metadata.update(phase="failed", error=safe_error(exc), cleanup_confirmed=False, can_start=False)
            atomic_json(journal, {**metadata, "pid": os.getpid(), "ts": time.time()})
    finally:
        try:
            try:
                if bot is not None:
                    bot.shutdown()
            finally:
                finished.set()
                if watcher:
                    watcher.join(2)
                publish()
        finally:
            if acquired:
                instance_lock.__exit__(None, None, None)
    return 0 if bot is not None and bot.safety.cleanup_confirmed else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    return run_worker(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
