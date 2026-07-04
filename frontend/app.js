(() => {
    "use strict";

    const isFile = window.location.protocol === "file:";
    const apiHost = isFile ? "localhost:8080" : window.location.host;
    const apiProtocol = isFile ? "http:" : window.location.protocol;
    const wsProtocol = apiProtocol === "https:" ? "wss:" : "ws:";
    const API_BASE = `${apiProtocol}//${apiHost}`;
    const WS_URL = `${wsProtocol}//${apiHost}/ws`;

    let ws = null;
    let reconnectTimer = null;
    let gridRunning = false;
    let wcRunning = false;
    let gridTokens = [];
    let selectedGridToken = normalizeToken({ key: "SP500", symbol: "xyz:SP500" });

    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => Array.from(document.querySelectorAll(sel));

    function formatUSD(val, showSign = false) {
        const num = Number(val);
        if (!Number.isFinite(num)) return "$0.00";
        const formatted = Math.abs(num).toLocaleString("en-US", {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        });
        if (showSign && num > 0) return `+$${formatted}`;
        if (num < 0) return `-$${formatted}`;
        return `$${formatted}`;
    }

    function formatPrice(val) {
        const num = Number(val);
        if (!Number.isFinite(num) || num === 0) return "--";
        if (Math.abs(num) >= 1000) {
            return `$${num.toLocaleString("en-US", {
                minimumFractionDigits: 2,
                maximumFractionDigits: 2,
            })}`;
        }
        if (Math.abs(num) >= 1) return `$${num.toFixed(4)}`;
        return `$${num.toFixed(6)}`;
    }

    function formatBPS(val) {
        const num = Number(val);
        if (!Number.isFinite(num)) return "-- BPS";
        return `${num.toFixed(1)} BPS`;
    }

    function setText(id, value) {
        const el = $(`#${id}`);
        if (el) el.textContent = value;
    }

    function showToast(type, title, message = "") {
        const stack = $("#toast-stack");
        if (!stack) return;

        const toast = document.createElement("div");
        toast.className = `toast ${type}`;
        toast.setAttribute("role", type === "error" ? "alert" : "status");

        const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        icon.setAttribute("aria-hidden", "true");
        const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
        let iconHref = "#icon-info";
        if (type === "error") iconHref = "#icon-alert";
        else if (type === "success") iconHref = "#icon-check";
        use.setAttribute("href", iconHref);
        icon.appendChild(use);

        const copy = document.createElement("div");
        const heading = document.createElement("strong");
        heading.textContent = title;
        const detail = document.createElement("span");
        detail.textContent = message;
        copy.append(heading);
        if (message) copy.append(detail);

        toast.append(icon, copy);
        stack.prepend(toast);
        window.setTimeout(() => toast.remove(), 4500);

        while (stack.children.length > 4) {
            stack.lastElementChild?.remove();
        }
    }

    function setFormError(prefix, message) {
        const alert = $(`#${prefix}-form-alert`);
        if (!alert) return;
        alert.hidden = !message;
        alert.textContent = message || "";
    }

    function readNumber(selector, fallback = 0) {
        const value = Number.parseFloat($(selector)?.value);
        return Number.isFinite(value) ? value : fallback;
    }

    function markInvalid(selector, invalid) {
        const el = $(selector);
        if (!el) return;
        el.classList.toggle("invalid", invalid);
        el.setAttribute("aria-invalid", invalid ? "true" : "false");
    }

    function validateMin(prefix, fields) {
        for (const field of fields) {
            const value = readNumber(field.selector, field.fallback);
            const invalid = value < field.min;
            markInvalid(field.selector, invalid);
            if (invalid) {
                setFormError(prefix, field.message);
                return false;
            }
        }
        setFormError(prefix, "");
        return true;
    }

    function setPnLColor(el, val) {
        if (!el) return;
        const num = Number(val);
        el.classList.remove("positive", "negative");
        if (Number.isFinite(num) && num > 0.001) el.classList.add("positive");
        if (Number.isFinite(num) && num < -0.001) el.classList.add("negative");
    }

    function setConnectionStatus(connected) {
        const shell = $("#connection-status");
        const label = shell?.querySelector(".status-text");
        shell?.classList.toggle("connected", connected);
        if (label) label.textContent = connected ? "Connected" : "Disconnected";
    }

    function setRunState(prefix, running) {
        const pill = $(`#${prefix}-state-pill`);
        const banner = $(`#${prefix}-run-banner`);
        if (pill) {
            pill.hidden = !running;
            pill.classList.toggle("running", running);
            pill.classList.toggle("idle", !running);
            pill.textContent = running ? "Running" : "";
        }
        if (banner) banner.hidden = !running;
    }

    function setBusy(buttonId, busy) {
        const btn = $(`#${buttonId}`);
        if (btn) btn.setAttribute("aria-busy", busy ? "true" : "false");
    }

    function updateGridButtons() {
        const startBtn = $("#grid-start-btn");
        const stopBtn = $("#grid-stop-btn");
        if (startBtn) startBtn.disabled = gridRunning;
        if (stopBtn) stopBtn.disabled = !gridRunning;
        setRunState("grid", gridRunning);
    }

    function updateWcButtons() {
        const startBtn = $("#wc-start-btn");
        const stopBtn = $("#wc-stop-btn");
        if (startBtn) startBtn.disabled = wcRunning;
        if (stopBtn) stopBtn.disabled = !wcRunning;
        setRunState("wc", wcRunning);
    }

    function switchPage(pageName) {
        $$(".nav-tab").forEach((tab) => {
            const active = tab.dataset.page === pageName;
            tab.classList.toggle("active", active);
            tab.setAttribute("aria-selected", active ? "true" : "false");
        });
        $$(".page").forEach((page) => {
            page.classList.toggle("active", page.id === `page-${pageName}`);
        });
    }

    async function fetchJson(url, options) {
        const resp = await fetch(url, options);
        let data = {};
        try {
            data = await resp.json();
        } catch {
            data = {};
        }
        if (!resp.ok) {
            throw new Error(data.message || data.detail || `HTTP ${resp.status}`);
        }
        return data;
    }

    function logoText(key) {
        const cleaned = String(key || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
        return (cleaned.slice(0, 2) || "?").padEnd(2, " ");
    }

    function normalizeToken(raw) {
        if (typeof raw === "string") {
            return {
                key: raw,
                symbol: `xyz:${raw}`,
                logo: logoText(raw),
                mid: 0,
                maxLeverage: null,
                isCross: true,
            };
        }
        const key = String(raw?.key || raw?.symbol || "SP500").split(":").pop().toUpperCase();
        return {
            key,
            symbol: raw?.symbol || `xyz:${key}`,
            logo: raw?.logo || logoText(key),
            mid: Number(raw?.mid || 0),
            maxLeverage: raw?.max_leverage || raw?.maxLeverage || null,
            isCross: raw?.is_cross !== false,
        };
    }

    function tokenSubtitle(token) {
        const parts = [token.symbol];
        if (token.mid) parts.push(formatPrice(token.mid));
        if (token.maxLeverage) parts.push(`${token.maxLeverage}x`);
        if (!token.isCross) parts.push("isolated");
        return parts.join(" / ");
    }

    function setSelectedGridToken(token) {
        selectedGridToken = normalizeToken(token);
        const hidden = $("#grid-token");
        if (hidden) hidden.value = selectedGridToken.key;
        setText("grid-token-logo", selectedGridToken.logo);
        setText("grid-token-name", selectedGridToken.key);
        setText("grid-token-symbol", tokenSubtitle(selectedGridToken));
        setText("grid-run-token", selectedGridToken.key);
        renderTokenOptions($("#grid-token-search")?.value || "");
    }

    function renderTokenOptions(filter = "") {
        const container = $("#grid-token-options");
        if (!container) return;
        const needle = filter.trim().toLowerCase();
        const shown = gridTokens
            .filter((token) => {
                if (!needle) return true;
                return `${token.key} ${token.symbol}`.toLowerCase().includes(needle);
            })
            .slice(0, 140);

        if (shown.length === 0) {
            const empty = document.createElement("div");
            empty.className = "empty-option";
            empty.textContent = "No HIP-3 token found";
            container.replaceChildren(empty);
            return;
        }

        container.replaceChildren(
            ...shown.map((token, index) => {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "token-option";
                button.setAttribute("role", "option");
                button.tabIndex = index === 0 ? 0 : -1;
                button.setAttribute("aria-selected", token.key === selectedGridToken.key ? "true" : "false");
                button.dataset.token = token.key;

                const logo = document.createElement("span");
                logo.className = "token-logo";
                logo.textContent = token.logo;

                const meta = document.createElement("span");
                meta.className = "token-meta";

                const name = document.createElement("strong");
                name.textContent = token.key;

                const subtitle = document.createElement("span");
                subtitle.textContent = tokenSubtitle(token);

                meta.append(name, subtitle);
                button.append(logo, meta);
                button.addEventListener("click", () => {
                    setSelectedGridToken(token);
                    closeTokenPicker();
                });
                return button;
            })
        );
    }

    function openTokenPicker() {
        const picker = $("#grid-token-picker");
        const trigger = $("#grid-token-trigger");
        if (!picker || !trigger) return;
        picker.classList.add("open");
        trigger.setAttribute("aria-expanded", "true");
        renderTokenOptions($("#grid-token-search")?.value || "");
        requestAnimationFrame(() => $("#grid-token-search")?.focus());
    }

    function closeTokenPicker() {
        const picker = $("#grid-token-picker");
        const trigger = $("#grid-token-trigger");
        if (!picker || !trigger) return;
        picker.classList.remove("open");
        trigger.setAttribute("aria-expanded", "false");
    }

    async function loadGridTokens() {
        setText("grid-token-count", "Loading");
        try {
            const data = await fetchJson(`${API_BASE}/api/grid/tokens`);
            const tokens = Array.isArray(data.tokens) ? data.tokens.map(normalizeToken) : [];
            if (tokens.length === 0) return;
            gridTokens = tokens.sort((a, b) => a.key.localeCompare(b.key));
            const preferred = gridTokens.find((token) => token.key === selectedGridToken.key) || gridTokens[0];
            setSelectedGridToken(preferred);
            setText("grid-token-count", `${gridTokens.length} tokens`);
        } catch (error) {
            console.warn("Token list unavailable:", error);
            gridTokens = [selectedGridToken];
            setSelectedGridToken(selectedGridToken);
            setText("grid-token-count", "Fallback");
            showToast("error", "Token catalog unavailable", error.message);
        }
    }

    async function loadInitialStatus() {
        try {
            const [grid, wc] = await Promise.all([
                fetchJson(`${API_BASE}/api/grid/status`),
                fetchJson(`${API_BASE}/api/worldcup/status`),
            ]);
            updateGridDashboard(grid);
            updateWorldcupDashboard(wc);
        } catch (error) {
            console.warn("Initial status unavailable:", error);
            showToast("error", "Status unavailable", error.message);
        }
    }

    function connectWebSocket() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

        try {
            ws = new WebSocket(WS_URL);
        } catch {
            scheduleReconnect();
            return;
        }

        ws.onopen = () => {
            setConnectionStatus(true);
            showToast("success", "WebSocket connected", "Live stats are streaming.");
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
        };

        ws.onclose = () => {
            setConnectionStatus(false);
            showToast("error", "WebSocket disconnected", "Reconnecting automatically.");
            scheduleReconnect();
        };

        ws.onerror = () => {
            setConnectionStatus(false);
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === "grid_stats") updateGridDashboard(data);
                if (data.type === "worldcup_stats") updateWorldcupDashboard(data);
            } catch (error) {
                console.warn("WebSocket parse error:", error);
            }
        };
    }

    function scheduleReconnect() {
        if (reconnectTimer) return;
        reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            connectWebSocket();
        }, 3000);
    }

    function updateGridDashboard(data) {
        setText("grid-duration", data.duration || "00:00:00");
        setText("grid-run-duration", data.duration || "00:00:00");
        setText("grid-volume", formatUSD(data.volume_usd));
        setText("grid-fees", formatUSD(data.fees));
        setText("grid-cost-1m", formatUSD(data.cost_per_1m));
        setText("grid-fills", String(data.fills || 0));
        setText("grid-mid-price", formatPrice(data.mid_price));
        setText("grid-spread-display", formatBPS(data.spread_bps));
        setText("grid-position", formatUSD(data.position_usd));

        const pnl = $("#grid-pnl");
        if (pnl) {
            pnl.textContent = formatUSD(data.net_pnl, true);
            setPnLColor(pnl, data.net_pnl);
        }

        const token = data.config?.token || selectedGridToken.key;
        setText("grid-run-token", token);

        if (typeof data.running === "boolean") {
            gridRunning = data.running;
            updateGridButtons();
        }
    }

    function updateWorldcupDashboard(data) {
        if (!data || data.type !== "worldcup_stats") return;

        setText("wc-duration", data.duration || "00:00:00");
        setText("wc-run-duration", data.duration || "00:00:00");
        setText("wc-volume", formatUSD(data.volume_usd));
        setText("wc-fees", formatUSD(data.fees));
        setText("wc-cost-1m", formatUSD(data.cost_per_1m));
        setText("wc-fills", String(data.fills || 0));
        setText("wc-mid-price", formatPrice(data.mid_price));
        setText("wc-spread-display", formatBPS(data.spread_bps));
        setText("wc-position", formatUSD(data.position_usd));

        const pnl = $("#wc-pnl");
        if (pnl) {
            pnl.textContent = formatUSD(data.net_pnl, true);
            setPnLColor(pnl, data.net_pnl);
        }

        if (typeof data.running === "boolean") {
            wcRunning = data.running;
            updateWcButtons();
        }
    }

    // Shader and spotlight features removed in line with ui-ux-pro-max design system.

    async function startGridBot() {
        const token = $("#grid-token")?.value || selectedGridToken.key || "SP500";
        const spread_bps = Number.parseFloat($("#grid-spread")?.value) || 0;
        const offset_bps = Number.parseFloat($("#grid-offset")?.value) || 0;
        const order_size_usd = Number.parseFloat($("#grid-order-size")?.value) || 15;
        const max_position_usd = Number.parseFloat($("#grid-max-pos")?.value) || 500;
        const num_levels = 1;

        if (!validateMin("grid", [
            { selector: "#grid-order-size", min: 10, fallback: order_size_usd, message: "Order USD must be at least 10." },
            { selector: "#grid-max-pos", min: 10, fallback: max_position_usd, message: "Max Pos USD must be at least 10." },
        ])) {
            showToast("error", "Check Grid settings", "Correct the highlighted field before starting.");
            return;
        }

        setBusy("grid-start-btn", true);

        try {
            await fetchJson(`${API_BASE}/api/grid/start`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    token,
                    spread_bps,
                    offset_bps,
                    order_size_usd,
                    num_levels,
                    max_position_usd,
                }),
            });
            gridRunning = true;
            setText("grid-run-token", token);
            updateGridButtons();
            showToast("success", "Grid bot started", `${token} is now running.`);
        } catch (error) {
            console.error("Grid start failed:", error);
            showToast("error", "Grid start failed", error.message);
        } finally {
            setBusy("grid-start-btn", false);
        }
    }

    async function stopGridBot() {
        setBusy("grid-stop-btn", true);
        try {
            await fetchJson(`${API_BASE}/api/grid/stop`, { method: "POST" });
            gridRunning = false;
            updateGridButtons();
            showToast("info", "Grid bot stopped", "Open orders are handled by the backend stop flow.");
        } catch (error) {
            console.error("Grid stop failed:", error);
            showToast("error", "Grid stop failed", error.message);
        } finally {
            setBusy("grid-stop-btn", false);
        }
    }

    async function startWorldcupBot() {
        const order_size_usd = Number.parseFloat($("#wc-order-size")?.value) || 45;
        const max_spread_bps = Number.parseFloat($("#wc-spread")?.value) || 2.0;
        const max_position_usd = Number.parseFloat($("#wc-max-pos")?.value) || 250;

        if (!validateMin("wc", [
            { selector: "#wc-order-size", min: 10, fallback: order_size_usd, message: "Order USD must be at least 10." },
            { selector: "#wc-max-pos", min: 10, fallback: max_position_usd, message: "Max Pos USD must be at least 10." },
        ])) {
            showToast("error", "Check WorldCup settings", "Correct the highlighted field before starting.");
            return;
        }

        setBusy("wc-start-btn", true);

        try {
            await fetchJson(`${API_BASE}/api/worldcup/start`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ order_size_usd, max_spread_bps, max_position_usd }),
            });
            wcRunning = true;
            updateWcButtons();
            showToast("success", "WorldCup bot started", "WorldCup strategy is now running.");
        } catch (error) {
            console.error("WorldCup start failed:", error);
            showToast("error", "WorldCup start failed", error.message);
        } finally {
            setBusy("wc-start-btn", false);
        }
    }

    async function stopWorldcupBot() {
        setBusy("wc-stop-btn", true);
        try {
            await fetchJson(`${API_BASE}/api/worldcup/stop`, { method: "POST" });
            wcRunning = false;
            updateWcButtons();
            showToast("info", "WorldCup bot stopped", "The strategy has been stopped.");
        } catch (error) {
            console.error("WorldCup stop failed:", error);
            showToast("error", "WorldCup stop failed", error.message);
        } finally {
            setBusy("wc-stop-btn", false);
        }
    }

    document.addEventListener("DOMContentLoaded", () => {

        $$(".nav-tab").forEach((tab) => {
            tab.addEventListener("click", () => switchPage(tab.dataset.page));
        });

        $("#grid-token-trigger")?.addEventListener("click", () => {
            const picker = $("#grid-token-picker");
            if (picker?.classList.contains("open")) closeTokenPicker();
            else openTokenPicker();
        });

        $("#grid-token-search")?.addEventListener("input", (event) => {
            renderTokenOptions(event.target.value);
        });

        $("#grid-token-search")?.addEventListener("keydown", (event) => {
            if (event.key === "Escape") closeTokenPicker();
            if (event.key === "ArrowDown") {
                event.preventDefault();
                $("#grid-token-options .token-option")?.focus();
            }
        });

        document.addEventListener("click", (event) => {
            const picker = $("#grid-token-picker");
            if (picker && !picker.contains(event.target)) closeTokenPicker();
        });

        $("#grid-start-btn")?.addEventListener("click", startGridBot);
        $("#grid-stop-btn")?.addEventListener("click", stopGridBot);
        $("#wc-start-btn")?.addEventListener("click", startWorldcupBot);
        $("#wc-stop-btn")?.addEventListener("click", stopWorldcupBot);

        gridTokens = [selectedGridToken];
        setSelectedGridToken(selectedGridToken);
        updateGridButtons();
        updateWcButtons();

        loadGridTokens();
        loadInitialStatus();
        connectWebSocket();
    });
})();
