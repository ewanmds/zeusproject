"""Explicit live entry point for the original adaptive grid engine."""
import argparse
import sys

# Windows uses a legacy charmap for redirected child-process output.
# The original engine prints Unicode during startup, before its first quote.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token', default='SP500')
    parser.add_argument('--strategy', choices=['grid_adaptive'], default='grid_adaptive')
    parser.add_argument('--notional', type=float, default=15.0)
    parser.add_argument('--grid-maker-fee-bps', type=float, default=0.288)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('Ce moteur original exige --live explicite ; aucun client ni ordre créé.')
    from grid_adaptive.runtime import run
    run(args)


if __name__ == '__main__':
    main()
