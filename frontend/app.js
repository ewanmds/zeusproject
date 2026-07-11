(() => {
    "use strict";

    const isFile = window.location.protocol === "file:";
    const configuredApiBase = String(
        window.ZEUS_CONFIG?.apiBase || window.ZEUS_API_BASE || ""
    ).trim();
    const configuredWsUrl = String(
        window.ZEUS_CONFIG?.wsUrl || window.ZEUS_WS_URL || ""
    ).trim();
    // Code d'accès: fourni par config.js (build Vercel) OU saisi sur la page
    // d'accueil et mémorisé dans localStorage (pas besoin de le retaper).
    const TOKEN_STORAGE_KEY = "zeus_access_code";
    let apiToken = String(
        window.ZEUS_CONFIG?.apiToken || window.ZEUS_API_TOKEN || ""
    ).trim();
    if (!apiToken) {
        try { apiToken = (localStorage.getItem(TOKEN_STORAGE_KEY) || "").trim(); } catch { /* private mode */ }
    }
    const localApiBase = isFile ? "http://localhost:8080" : window.location.origin;
    const API_BASE = (configuredApiBase || localApiBase).replace(/\/+$/, "");
    function buildWsUrl() {
        if (configuredWsUrl) return configuredWsUrl;
        const url = new URL(`${API_BASE}/ws`);
        url.protocol = new URL(API_BASE).protocol === "https:" ? "wss:" : "ws:";
        if (apiToken) url.searchParams.set("token", apiToken);
        return url.toString();
    }

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
        // Multi-bot: Start reste toujours actif (déployer un autre token);
        // Stop (global) n'est actif que si au moins un bot tourne.
        const startBtn = $("#grid-start-btn");
        const stopBtn = $("#grid-stop-btn");
        if (startBtn) startBtn.disabled = false;
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

    async function fetchJson(url, options = {}) {
        const headers = new Headers(options.headers || {});
        if (apiToken) headers.set("X-API-Token", apiToken);
        const resp = await fetch(url, { ...options, headers });
        let data = {};
        try {
            data = await resp.json();
        } catch {
            data = {};
        }
        if (resp.status === 401) {
            lockApp("Code d'accès invalide — entre-le à nouveau.");
            throw new Error("Code d'accès invalide");
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

    /* --------- WorldCup market picker (même composant que le token picker) --------- */
    const WC_MARKETS = [
        { key: "ALL", label: "Tous les marchés", logo: "WC", subtitle: "panier HIP-4 · outcome NO" },
        { key: "France", label: "France", logo: "FR", subtitle: "2026 champion · NO" },
        { key: "Argentina", label: "Argentina", logo: "AR", subtitle: "2026 champion · NO" },
        { key: "Spain", label: "Spain", logo: "ES", subtitle: "2026 champion · NO" },
        { key: "England", label: "England", logo: "EN", subtitle: "2026 champion · NO" },
        { key: "Portugal", label: "Portugal", logo: "PT", subtitle: "2026 champion · NO" },
        { key: "Germany", label: "Germany", logo: "DE", subtitle: "2026 champion · NO" },
        { key: "Brazil", label: "Brazil", logo: "BR", subtitle: "2026 champion · NO" },
        { key: "Netherlands", label: "Netherlands", logo: "NL", subtitle: "2026 champion · NO" },
        { key: "USA", label: "USA", logo: "US", subtitle: "2026 champion · NO" },
        { key: "Norway", label: "Norway", logo: "NO", subtitle: "2026 champion · NO" },
        { key: "Morocco", label: "Morocco", logo: "MA", subtitle: "2026 champion · NO" },
    ];
    let selectedWcMarket = WC_MARKETS[0];

    function setSelectedWcMarket(market) {
        selectedWcMarket = market;
        const hidden = $("#wc-market");
        if (hidden) hidden.value = market.key;
        setText("wc-market-logo", market.logo);
        setText("wc-market-name", market.label);
        setText("wc-market-symbol", market.subtitle);
        renderWcMarketOptions($("#wc-market-search")?.value || "");
    }

    function renderWcMarketOptions(filter = "") {
        const container = $("#wc-market-options");
        if (!container) return;
        const needle = filter.trim().toLowerCase();
        const shown = WC_MARKETS.filter((market) => {
            if (!needle) return true;
            return `${market.key} ${market.label}`.toLowerCase().includes(needle);
        });

        if (shown.length === 0) {
            const empty = document.createElement("div");
            empty.className = "empty-option";
            empty.textContent = "Aucun marché trouvé";
            container.replaceChildren(empty);
            return;
        }

        container.replaceChildren(
            ...shown.map((market, index) => {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "token-option";
                button.setAttribute("role", "option");
                button.tabIndex = index === 0 ? 0 : -1;
                button.setAttribute("aria-selected", market.key === selectedWcMarket.key ? "true" : "false");
                button.dataset.market = market.key;

                const logo = document.createElement("span");
                logo.className = "token-logo";
                logo.textContent = market.logo;

                const meta = document.createElement("span");
                meta.className = "token-meta";

                const name = document.createElement("strong");
                name.textContent = market.label;

                const subtitle = document.createElement("span");
                subtitle.textContent = market.subtitle;

                meta.append(name, subtitle);
                button.append(logo, meta);
                button.addEventListener("click", () => {
                    setSelectedWcMarket(market);
                    closeWcMarketPicker();
                });
                return button;
            })
        );
    }

    function openWcMarketPicker() {
        const picker = $("#wc-market-picker");
        const trigger = $("#wc-market-trigger");
        if (!picker || !trigger) return;
        picker.classList.add("open");
        trigger.setAttribute("aria-expanded", "true");
        renderWcMarketOptions($("#wc-market-search")?.value || "");
        requestAnimationFrame(() => $("#wc-market-search")?.focus());
    }

    function closeWcMarketPicker() {
        const picker = $("#wc-market-picker");
        const trigger = $("#wc-market-trigger");
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
            ws = new WebSocket(buildWsUrl());
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
        // Plus de cartes agrégées: tout l'affichage vit dans les cartes
        // par token (renderBotsList). Seul le bandeau de session reste.
        setText("grid-run-duration", data.duration || "00:00:00");

        const bots = Array.isArray(data.bots) ? data.bots : [];
        const runningTokens = bots.filter((b) => b.running).map((b) => b.token);
        const token = runningTokens.length
            ? runningTokens.join(" · ")
            : (data.config?.token || selectedGridToken.key);
        setText("grid-run-token", token);

        renderBotsList(bots);

        if (typeof data.running === "boolean") {
            gridRunning = data.running;
            updateGridButtons();
        }
    }

    function renderBotsList(bots) {
        const container = $("#grid-bots-list");
        if (!container) return;
        if (!bots.length) {
            container.hidden = true;
            container.innerHTML = "";
            container.dataset.tokens = "";
            return;
        }
        container.hidden = false;

        // Reconstruire le DOM seulement si la liste de tokens change; sinon
        // ne mettre à jour que les valeurs (pas de flicker à 1 Hz).
        const signature = bots.map((b) => b.token).join("|");
        if (container.dataset.tokens !== signature) {
            container.dataset.tokens = signature;
            container.innerHTML = bots.map((b) => `
              <article class="bot-card" data-token="${b.token}">
                <header class="bot-card-head">
                  <span class="bot-dot" aria-hidden="true"></span>
                  <strong class="bot-card-token">${b.token}</strong>
                  <span class="bot-card-duration" data-f="duration">00:00:00</span>
                  <button class="btn btn-stop bot-card-stop" type="button" data-token="${b.token}">
                    Stop
                  </button>
                </header>
                <div class="bot-card-grid">
                  <div class="bot-stat"><span>Mid</span><strong data-f="mid">—</strong></div>
                  <div class="bot-stat"><span>Volume</span><strong data-f="vol">—</strong></div>
                  <div class="bot-stat"><span>Net PnL</span><strong data-f="pnl">—</strong></div>
                  <div class="bot-stat"><span>Fees</span><strong data-f="fees">—</strong></div>
                  <div class="bot-stat"><span>Cost/1M</span><strong data-f="cost">—</strong></div>
                  <div class="bot-stat"><span>Fills</span><strong data-f="fills">—</strong></div>
                  <div class="bot-stat"><span>Position</span><strong data-f="pos">—</strong></div>
                  <div class="bot-stat"><span>Spread</span><strong data-f="spread">—</strong></div>
                </div>
                <footer class="bot-card-action" data-f="action"></footer>
              </article>`).join("");
            container.querySelectorAll(".bot-card-stop").forEach((btn) => {
                btn.addEventListener("click", () => stopGridBot(btn.dataset.token));
            });
        }

        bots.forEach((b) => {
            const card = container.querySelector(`.bot-card[data-token="${b.token}"]`);
            if (!card) return;
            card.classList.toggle("stopped", !b.running);
            const set = (f, v) => {
                const el = card.querySelector(`[data-f="${f}"]`);
                if (el && el.textContent !== v) el.textContent = v;
            };
            set("duration", b.duration || "00:00:00");
            set("mid", formatPrice(b.mid_price));
            set("vol", formatUSD(b.volume_usd));
            set("pnl", formatUSD(b.net_pnl, true));
            const pnlEl = card.querySelector('[data-f="pnl"]');
            if (pnlEl) setPnLColor(pnlEl, b.net_pnl);
            set("fees", formatUSD(b.fees));
            set("cost", formatUSD(b.cost_per_1m));
            set("fills", String(b.fills || 0));
            set("pos", formatUSD(b.position_usd));
            set("spread", formatBPS(b.spread_bps));
            set("action", b.error ? `⚠ ${b.error}` : (b.action || ""));
            const stopBtn = card.querySelector(".bot-card-stop");
            if (stopBtn) stopBtn.style.display = b.running ? "" : "none";
        });
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
        const order_size_usd = Number.parseFloat($("#grid-order-size")?.value) || 15;

        if (!validateMin("grid", [
            { selector: "#grid-order-size", min: 10, fallback: order_size_usd, message: "Order USD must be at least 10." },
        ])) {
            showToast("error", "Check OBI settings", "Correct the highlighted field before starting.");
            return;
        }

        setBusy("grid-start-btn", true);

        try {
            // Max position hardcodée côté bot: 1 x order (MAX_INVENTORY_ORDERS = 1).
            await fetchJson(`${API_BASE}/api/grid/start`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ token, order_size_usd }),
            });
            gridRunning = true;
            setText("grid-run-token", token);
            updateGridButtons();
            showToast("success", "OBI bot started", `${token} is now running.`);
        } catch (error) {
            console.error("OBI start failed:", error);
            showToast("error", "OBI start failed", error.message);
        } finally {
            setBusy("grid-start-btn", false);
        }
    }

    async function stopGridBot(token) {
        // token précis → stoppe ce bot; sans token → stoppe TOUS les bots.
        setBusy("grid-stop-btn", true);
        try {
            await fetchJson(`${API_BASE}/api/grid/stop`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(token ? { token } : {}),
            });
            showToast("info", token ? `Bot ${token} stopped` : "All bots stopped",
                "Cancel all + flatten market exécutés par le bot avant l'arrêt.");
        } catch (error) {
            console.error("Stop failed:", error);
            showToast("error", "Stop failed", error.message);
        } finally {
            setBusy("grid-stop-btn", false);
        }
    }

    async function startWorldcupBot() {
        const order_size_usd = Number.parseFloat($("#wc-order-size")?.value) || 50;
        // En %, même convention que --max-spread-pct (0.02 = 0.02% = 2 bps)
        const max_spread_pct = Number.parseFloat($("#wc-spread")?.value) || 0.02;
        const max_position_usd = Number.parseFloat($("#wc-max-pos")?.value) || 250;
        const market = $("#wc-market")?.value || "ALL";

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
                body: JSON.stringify({ order_size_usd, max_spread_pct, max_position_usd, market }),
            });
            wcRunning = true;
            updateWcButtons();
            const marketTxt = market === "ALL" ? "tous les marchés" : market;
            showToast("success", "WorldCup bot started", `${marketTxt} · spread < ${max_spread_pct}%`);
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

    // ------------------------------------------------------------ access gate
    let appBooted = false;

    function bootApp() {
        if (appBooted) return;
        appBooted = true;
        loadGridTokens();
        loadInitialStatus();
        connectWebSocket();
    }

    function showAccessGate(message) {
        const gate = $("#access-gate");
        if (!gate) return;
        gate.hidden = false;
        const err = $("#access-gate-error");
        if (err) {
            err.textContent = message || "";
            err.hidden = !message;
        }
        requestAnimationFrame(() => $("#access-code-input")?.focus());
    }

    function hideAccessGate() {
        const gate = $("#access-gate");
        if (gate) gate.hidden = true;
    }

    function lockApp(message) {
        try { localStorage.removeItem(TOKEN_STORAGE_KEY); } catch { /* private mode */ }
        apiToken = "";
        if (ws) {
            try { ws.close(); } catch { /* déjà fermé */ }
            ws = null;
        }
        showAccessGate(message);
    }

    async function submitAccessCode() {
        const input = $("#access-code-input");
        const code = (input?.value || "").trim();
        if (!code) {
            showAccessGate("Entre le code d'accès.");
            return;
        }
        setBusy("access-code-submit", true);
        apiToken = code;
        try {
            await fetchJson(`${API_BASE}/api/health`);
            try { localStorage.setItem(TOKEN_STORAGE_KEY, code); } catch { /* private mode */ }
            hideAccessGate();
            showToast("success", "Accès autorisé", "Code mémorisé sur ce navigateur.");
            bootApp();
            if (ws) {
                try { ws.close(); } catch { /* reconnexion avec token */ }
                ws = null;
            }
            connectWebSocket();
        } catch (error) {
            if (String(error.message).includes("invalide")) {
                // 401: lockApp a déjà réaffiché la porte avec le message.
            } else {
                // Backend injoignable: on mémorise quand même — un 401 plus
                // tard re-verrouillera si le code est faux.
                try { localStorage.setItem(TOKEN_STORAGE_KEY, code); } catch { /* private mode */ }
                hideAccessGate();
                showToast("error", "Backend injoignable",
                    "Code mémorisé. Utilise le bouton Backend pour retester la connexion.");
                bootApp();
            }
        } finally {
            setBusy("access-code-submit", false);
        }
    }

    async function checkBackend() {
        setBusy("backend-connect-btn", true);
        try {
            await fetchJson(`${API_BASE}/api/health`);
            showToast("success", "Backend en ligne", API_BASE);
            if (!appBooted) bootApp();
            if (!ws || (ws.readyState !== WebSocket.OPEN && ws.readyState !== WebSocket.CONNECTING)) {
                connectWebSocket();
            }
            loadInitialStatus();
        } catch (error) {
            showToast("error", "Backend injoignable", `${API_BASE} — ${error.message}`);
        } finally {
            setBusy("backend-connect-btn", false);
        }
    }

    async function initAccess() {
        if (apiToken) {
            hideAccessGate();
            bootApp();
            return;
        }
        try {
            // Backend sans code configuré (usage local): entrer directement.
            await fetchJson(`${API_BASE}/api/health`);
            bootApp();
        } catch {
            // 401 → lockApp a montré la porte; réseau → montrer la porte aussi.
            showAccessGate();
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
            const wcPicker = $("#wc-market-picker");
            if (wcPicker && !wcPicker.contains(event.target)) closeWcMarketPicker();
        });

        $("#wc-market-trigger")?.addEventListener("click", () => {
            const picker = $("#wc-market-picker");
            if (picker?.classList.contains("open")) closeWcMarketPicker();
            else openWcMarketPicker();
        });

        $("#wc-market-search")?.addEventListener("input", (event) => {
            renderWcMarketOptions(event.target.value);
        });

        $("#wc-market-search")?.addEventListener("keydown", (event) => {
            if (event.key === "Escape") closeWcMarketPicker();
            if (event.key === "ArrowDown") {
                event.preventDefault();
                $("#wc-market-options .token-option")?.focus();
            }
        });

        setSelectedWcMarket(selectedWcMarket);

        $("#grid-start-btn")?.addEventListener("click", startGridBot);
        $("#grid-stop-btn")?.addEventListener("click", () => stopGridBot());
        $("#wc-start-btn")?.addEventListener("click", startWorldcupBot);
        $("#wc-stop-btn")?.addEventListener("click", stopWorldcupBot);

        gridTokens = [selectedGridToken];
        setSelectedGridToken(selectedGridToken);
        updateGridButtons();
        updateWcButtons();

        $("#access-code-submit")?.addEventListener("click", submitAccessCode);
        $("#access-code-input")?.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                submitAccessCode();
            }
        });
        $("#backend-connect-btn")?.addEventListener("click", checkBackend);

        initAccess();
    });
})();
