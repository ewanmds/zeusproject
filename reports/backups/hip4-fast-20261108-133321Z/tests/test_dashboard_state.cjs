// No DOM or network: ordering and display clocks use an injected clock.
const assert = require('node:assert/strict');
const { createStore, currency } = require('../frontend/state.js');
let time = 1000;
const store = createStore({ now: () => time });
const frame = (revision, extra = {}) => ({ source_id: 'server-a', revision,
    published_at: revision, running: true, duration_sec: 10, ...extra });
assert(store.accept('outcome', frame(5)));
assert(!store.accept('outcome', frame(4, { running: false })));
assert(!store.accept('outcome', { running: false }));
assert.equal(store.view('outcome').running, true);
time += 2000;
assert.equal(store.elapsed('outcome', store.view('outcome')), '00:00:12');
assert(store.accept('grid', frame(8)));
time += 4500;
assert.equal(store.view('outcome').stale, true);
assert.equal(store.view('grid').stale, false); // One source cannot refresh another.
assert.equal(store.elapsed('outcome', store.view('outcome')), '00:00:10');
assert(store.accept('outcome', frame(1, { source_id: 'server-b', published_at: 10 })));
assert(!store.accept('outcome', frame(99, { published_at: 11 })));
assert(store.accept('outcome', frame(2, { source_id: 'server-b', published_at: 12,
    volume_usd: 999, duration_sec: 999,
    active_metrics: { volume_usd: 20, duration_sec: 50 }, display_bots: [{ session_id: 'active' }] })));
assert.equal(store.view('outcome').volume_usd, 20);
assert.equal(store.view('outcome').duration_sec, 50);
assert.equal(store.view('outcome').bots[0].session_id, 'active');
assert.equal(currency(-.004827), '-$0.0048');
assert.equal(currency(-0), '$0.00');
assert.equal(currency(null), '\u2014');
assert.equal(currency(20.3), '$20.30');
console.log('PASS: ordered snapshots, source restarts, independent freshness, clocks, precision');

// Regression: an old server's 3-hour historical duration must not appear on a
// newly launched worker, nor inflate the new worker's financial counters.
const legacy = createStore({ now: () => time });
const closed = { session_id: 'history', running: false, cleanup_confirmed: true,
    duration: '03:03:50', duration_sec: 11030, volume_usd: 900, fees: 9, net_pnl: -9, fills: 90 };
const current = { session_id: 'current', running: true, cleanup_confirmed: false,
    duration: '00:02:06', duration_sec: 126, volume_usd: 25, fees: .01, net_pnl: -.01, fills: 1 };
legacy.accept('outcome', { duration: closed.duration, volume_usd: 925, net_pnl: -9.01, bots: [closed, current] });
assert.equal(legacy.view('outcome').duration, '00:02:06');
assert.equal(legacy.view('outcome').volume_usd, 25);
assert.equal(legacy.view('outcome').fills, 1);
time += 1000;
assert.equal(legacy.elapsed('outcome', legacy.view('outcome')), '00:02:07');
legacy.accept('outcome', { duration: closed.duration, bots: [closed, { ...current, session_id: 'restart',
    duration: '00:00:00', duration_sec: .2, volume_usd: 0, fees: 0, net_pnl: 0, fills: 0 }] });
assert.equal(legacy.view('outcome').duration, '00:00:00');
assert.equal(legacy.view('outcome').volume_usd, 0);
legacy.accept('outcome', { duration: closed.duration, bots: [closed] });
assert.equal(legacy.view('outcome').duration, '00:00:00');
assert.equal(legacy.view('outcome').volume_usd, 0);
assert.equal(legacy.view('outcome').running, false);
console.log('PASS: legacy server lifetime totals excluded; new session resets clocks and counters');
const { errorText } = require('../frontend/state.js');
assert.equal(errorText("HTTPSConnectionPool(host='api.hyperliquid.xyz'): Read timed out"),
    'Hyperliquid unavailable. Verify stop before restarting.');
assert.equal(errorText('Residual position; stop unconfirmed'), 'Residual position; stop unconfirmed');
