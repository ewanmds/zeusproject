"""Matched fake reads: periodic inline control versus the MID worker path."""
import argparse
import json
from pathlib import Path
import sys
import time
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_mid_maintenance import OwnerTests, poll_result, POSITION
from benchmark_mid import percentiles


def measure(mode, iterations, delay_ms):
    cycles, completions, read_counts = [], [], []
    for _ in range(iterations):
        fixture = OwnerTests()
        fixture.setUp()
        bot, worker, client = fixture.bot, fixture.worker, fixture.client
        client.block = None
        original_post = client.post
        def delayed(path, payload):
            time.sleep(delay_ms / 1000)
            return original_post(path, payload)
        client.post = delayed
        bot.info.user_state.side_effect = lambda *a, **k: delayed('/info', {'type':'clearinghouseState'})
        bot.info.spot_user_state.side_effect = lambda *a, **k: delayed('/info', {'type':'spotClearinghouseState'})
        bot.info.open_orders.side_effect = lambda *a, **k: delayed('/info', {'type':'openOrders'})
        bot.compute_intent = Mock(return_value=(None, None, 'measurement: no orders'))
        bot._maintenance_position_request_at = bot._maintenance_orders_request_at = 0
        bot.last_quote_ts = 0
        try:
            started = time.perf_counter_ns()
            if mode == 'inline_control':
                # The same foreground call sequence used by the former MID loop.
                bot.sync_position()
                bot.reconcile_orders(force=True)
            else:
                bot._maintenance_tick()
            bot.apply_intent()
            cycles.append((time.perf_counter_ns() - started) / 1e6)
            if mode == 'worker':
                result = poll_result(worker)
                if not bot._apply_maintenance_result(result):
                    raise AssertionError('Fake worker state was not applied')
            completions.append((time.perf_counter_ns() - started) / 1e6)
            read_counts.append(len(client.calls))
            if fixture.raw.bulk_orders.called or fixture.raw.bulk_modify_orders_new.called or fixture.raw.bulk_cancel.called:
                raise AssertionError('Benchmark attempted a trading action')
        finally:
            fixture.doCleanups()
    return {'samples': iterations, 'decision_cycle_ms':percentiles(cycles),
            'all_reads_completed_ms':percentiles(completions), 'read_counts':sorted(set(read_counts)),
            'trading_requests':0}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--iterations', type=int, default=50)
    parser.add_argument('--delay-ms', type=float, default=10)
    args=parser.parse_args()
    data={'model':'Each of the same three fake info reads sleeps delay_ms; no live exchange, order or fill.',
          'delay_ms':args.delay_ms,
          'inline_control':measure('inline_control',args.iterations,args.delay_ms),
          'worker':measure('worker',args.iterations,args.delay_ms)}
    Path(args.output).write_text(json.dumps(data,indent=2),encoding='utf-8')
    print(json.dumps(data,indent=2))
