"""Read-only collection and offline calibration commands. Never sends orders."""
import argparse
import json
import os
from pathlib import Path
import signal
import time

from .core import SYMBOL
from .replay import calibrate
from .runtime import DATA_DIR, Runtime, atomic_json


def collect(args):
    # Import the transport directly, never OBI_VOLUME_BOT/Exchange or a wallet.
    from dotenv import load_dotenv
    import websocket
    from startup_network import post_info
    load_dotenv()
    account = args.account or os.getenv("WALLET_ADDRESS", "")
    if not account:
        raise ValueError("read-only account address required (--account or WALLET_ADDRESS)")
    runtime = Runtime(lambda payload: post_info("https://api.hyperliquid.xyz", payload), account, args.directory, require_calibration=True)
    def on_open(socket):
        for kind in ("l2Book", "trades", "activeAssetCtx"):
            socket.send(json.dumps({"method": "subscribe", "subscription": {"type": kind, "coin": SYMBOL}}))
    def on_message(_socket, message):
        try:
            event = json.loads(message)
            kind, data = event.get("channel"), event.get("data", {})
            if kind == "l2Book":
                runtime.book(*data["levels"], float(data["time"]) / 1000)
            elif kind == "trades":
                runtime.trades(data)
            elif kind == "activeAssetCtx":
                runtime.context(data)
        except Exception as exc:
            runtime.journal.record("gap", {"reason": type(exc).__name__})
    socket = websocket.WebSocketApp("wss://api.hyperliquid.xyz/ws", on_open=on_open, on_message=on_message)
    def stop(*_args):
        socket.close()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"Read-only {SYMBOL}: no exchange/signing client. Recording in {args.directory}", flush=True)
    try:
        socket.run_forever(ping_interval=30, ping_timeout=10)
    finally:
        runtime.close()
    return 1 if runtime.journal.error else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    recording = commands.add_parser("collect", help="public feed + read-only account fee query")
    recording.add_argument("--account", help="public trading-account address; never a private key")
    recording.add_argument("--directory", type=Path, default=DATA_DIR)
    replay = commands.add_parser("calibrate", help="three closed UTC sessions; offline, no orders")
    replay.add_argument("sessions", nargs=3, type=Path)
    replay.add_argument("--output", type=Path, default=DATA_DIR / "calibration.json")
    args = parser.parse_args()
    try:
        if args.command == "collect":
            return collect(args)
        report = calibrate(args.sessions, args.output)
        print(json.dumps({"accepted": report["accepted"], "reasons": report["reasons"], "offset": report["offset"]}))
        return 0 if report["accepted"] else 2
    except (ValueError, KeyError, OSError, TypeError) as exc:
        if args.command == "calibrate":
            # Invalidate any previous acceptance, rather than leaving a stale
            # success file behind after a failed recalibration.
            atomic_json(args.output, {"accepted": False, "reasons": [str(exc)]})
        parser.exit(2, f"Calibration/collection blocked: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
