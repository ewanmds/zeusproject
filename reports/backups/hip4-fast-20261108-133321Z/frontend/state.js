/* Ordered server snapshots and display-only clocks. No trading decisions here. */
(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    else root.ZeusState = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
    "use strict";
    const finite = (value) => typeof value === "number" && Number.isFinite(value);

    function seconds(row) {
        if (finite(row?.duration_sec)) return Math.max(0, row.duration_sec);
        const parts = String(row?.duration || "").match(/^(\d+):(\d{2}):(\d{2})$/);
        return parts ? Number(parts[1])*3600+Number(parts[2])*60+Number(parts[3]) : 0;
    }

    // Older running servers publish lifetime totals at the root. Derive the
    // current view from their worker rows, without restarting live execution.
    function legacyOutcome(data) {
        const rows = data.bots.filter((bot) => bot.running || bot.cleanup_confirmed !== true);
        const active = rows.filter((bot) => bot.running);
        const duration_sec = Math.max(0, ...active.map(seconds));
        const stale = Boolean(data.stale || active.some((bot) => bot.stale));
        const fields = ["volume_usd", "net_pnl", "fees", "fills"];
        const complete = !stale && rows.every((bot) => bot.accounting_complete !== false &&
            fields.every((field) => finite(bot[field])));
        const view = { ...data, bots: rows, running: active.length > 0, active_markets: active.length,
            duration_sec, duration: clock(duration_sec), stale, accounting_complete: complete };
        for (const field of fields) view[field] = complete ? rows.reduce((sum, bot) => sum+bot[field], 0) : null;
        view.cost_per_1m = complete && view.volume_usd > 0 ? -view.net_pnl/view.volume_usd*1e6 : null;
        return view;
    }

    function createStore({ now = () => Date.now(), staleAfterMs = 6000 } = {}) {
        const states = new Map();
        function accept(kind, data) {
            if (!data || typeof data !== "object") return false;
            const old = states.get(kind);
            const versioned = typeof data.source_id === "string" && finite(data.revision);
            if (old?.versioned && !versioned) return false;
            if (versioned && old?.versioned) {
                if (data.source_id === old.data.source_id && data.revision < old.data.revision) return false;
                if (data.source_id !== old.data.source_id && old.retired.has(data.source_id)) return false;
                if (data.source_id !== old.data.source_id && finite(data.published_at) &&
                        finite(old.data.published_at) && data.published_at < old.data.published_at) return false;
            }
            const retired = new Set(old?.retired || []);
            if (versioned && old?.versioned && old.data.source_id !== data.source_id) retired.add(old.data.source_id);
            states.set(kind, { data, receivedAt: now(), versioned, retired });
            return true;
        }
        function view(kind) {
            const saved = states.get(kind);
            if (!saved) return null;
            const data = saved.data;
            const stale = Boolean(data.stale || data.active_metrics?.stale || now()-saved.receivedAt > staleAfterMs);
            if (kind === "outcome" && data.active_metrics && Array.isArray(data.display_bots)) {
                return { ...data, ...data.active_metrics, bots: data.display_bots, stale };
            }
            if (kind === "outcome" && Array.isArray(data.bots)) {
                const projected = legacyOutcome(data);
                return { ...projected, stale: stale || projected.stale };
            }
            return { ...data, stale };
        }
        function age(kind) {
            const saved = states.get(kind);
            return saved ? Math.max(0, now()-saved.receivedAt) : Infinity;
        }
        function elapsed(kind, row) {
            const saved = states.get(kind);
            if (!saved) return row?.duration || "00:00:00";
            const base = seconds(row);
            const delta = row.running && !row.stale && !view(kind)?.stale ? Math.max(0, now()-saved.receivedAt)/1000 : 0;
            return clock(base+delta);
        }
        return { accept, view, age, elapsed };
    }
    function clock(seconds) {
        seconds = Math.floor(Math.max(0, Number(seconds) || 0));
        return [Math.floor(seconds/3600), Math.floor(seconds/60)%60, seconds%60]
            .map((part) => String(part).padStart(2, "0")).join(":");
    }
    // Rebuilding a NumberFormat per cell is ~36x slower than reusing one; a
    // live dashboard formats hundreds of cells per second.
    const numberFormats = new Map();
    function numberFormat(digits) {
        let format = numberFormats.get(digits);
        if (!format) {
            format = new Intl.NumberFormat("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
            numberFormats.set(digits, format);
        }
        return format;
    }
    function currency(value, showSign = false) {
        if (value == null || value === "" || !Number.isFinite(Number(value))) return "—";
        let number = Number(value);
        const magnitude = Math.abs(number);
        const digits = magnitude > 0 && magnitude < .01 ? Math.min(8, Math.max(4, 1-Math.floor(Math.log10(magnitude)))) : 2;
        number = Number(number.toFixed(digits));
        const formatted = numberFormat(number === 0 ? 2 : digits).format(Math.abs(number));
        return `${number < 0 ? "-" : showSign && number > 0 ? "+" : ""}$${formatted}`;
    }
    function errorText(value) {
        const text = String(value || "");
        return /HTTPSConnectionPool|Read timed out|Hyperliquid unreachable|ConnectTimeout|ReadTimeout/.test(text)
            ? "Hyperliquid unavailable. Verify stop before restarting." : text;
    }
    return { createStore, currency, clock, errorText };
});
