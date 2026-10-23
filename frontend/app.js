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
        if (val == null || val === "") return "—";
        const num = Number(val);
        if (!Number.isFinite(num)) return "—";
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
        if (val == null || !Number.isFinite(num) || num === 0) return "—";
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
        if (val == null || !Number.isFinite(num)) return "—";
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

        const close = document.createElement("button");
        close.type = "button";
        close.className = "toast-close";
        close.setAttribute("aria-label", "Fermer la notification");
        close.innerHTML = '<svg aria-hidden="true"><use href="#icon-close"></use></svg>';
        toast.append(icon, copy, close);
        stack.prepend(toast);
        let timer;
        let leaving = false;
        const remove = () => {
            if (leaving) return;
            leaving = true;
            window.clearTimeout(timer);
            document.removeEventListener("visibilitychange", visibility);
            toast.classList.add("leaving");
            window.setTimeout(() => toast.remove(), 180);
        };
        const pause = () => window.clearTimeout(timer);
        const resume = () => {
            pause();
            if (!document.hidden && !toast.contains(document.activeElement) && !toast.matches(":hover")) {
                timer = window.setTimeout(remove, type === "error" ? 10000 : 6000);
            }
        };
        const visibility = () => document.hidden ? pause() : resume();
        close.addEventListener("click", () => {
            if (toast.contains(document.activeElement)) {
                const page = $(".page.active");
                page?.querySelector("button:not(:disabled), input:not([type=hidden]), select")?.focus();
            }
            remove();
        });
        toast.addEventListener("pointerenter", pause);
        toast.addEventListener("pointerleave", resume);
        toast.addEventListener("focusin", pause);
        toast.addEventListener("focusout", () => window.setTimeout(resume, 0));
        document.addEventListener("visibilitychange", visibility);
        toast.dismiss = remove;
        resume();

        while (stack.children.length > 4) {
            const oldest = stack.lastElementChild;
            oldest?.dismiss?.();
            oldest?.remove();
        }
    }

    function setFormError(prefix, message) {
        const alert = $(`#${prefix}-form-alert`);
        if (!alert) return;
        alert.hidden = !message;
        alert.textContent = message || "";
    }

    function markInvalid(selector, invalid) {
        const el = $(selector);
        if (!el) return;
        el.classList.toggle("invalid", invalid);
        el.setAttribute("aria-invalid", invalid ? "true" : "false");
    }

    function validateMin(prefix, fields) {
        for (const field of fields) {
            const value = Number.parseFloat($(field.selector)?.value);
            const invalid = !Number.isFinite(value) || value < field.min;
            markInvalid(field.selector, invalid);
            if (invalid) {
                setFormError(prefix, field.message);
                $(field.selector)?.focus();
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
        if (label) label.textContent = connected ? "Connecté" : "Déconnecté";
    }

    function setRunState(prefix, running) {
        const pill = $(`#${prefix}-state-pill`);
        const banner = $(`#${prefix}-run-banner`);
        if (pill) {
            pill.hidden = !running;
            pill.classList.toggle("running", running);
            pill.classList.toggle("idle", !running);
            pill.textContent = running ? "Active" : "";
        }
        if (banner) banner.hidden = !running;
    }

    function setBusy(buttonId, busy) {
        const btn = $(`#${buttonId}`);
        if (!btn) return;
        if (busy && btn.getAttribute("aria-busy") !== "true") btn.dataset.wasDisabled = String(btn.disabled);
        btn.setAttribute("aria-busy", busy ? "true" : "false");
        btn.disabled = busy || btn.dataset.wasDisabled === "true";
        if (!busy) delete btn.dataset.wasDisabled;
    }

    function updateGridButtons() {
        // Multi-bot: Start reste toujours actif (déployer un autre token);
        // Stop (global) n'est actif que si au moins un bot tourne.
        const startBtn = $("#grid-start-btn");
        const stopBtn = $("#grid-stop-btn");
        if (startBtn) startBtn.disabled = startBtn.getAttribute("aria-busy") === "true";
        if (stopBtn) stopBtn.disabled = !gridRunning || stopBtn.getAttribute("aria-busy") === "true";
        setRunState("grid", gridRunning);
    }

    function updateGridStrategy() {
        const strategy = $("#grid-strategy")?.value || "bbo";
        const offsetField = $("#grid-offset-field");
        if (offsetField) offsetField.hidden = strategy !== "mid";
        const adaptiveField = $("#grid-adaptive-field");
        if (adaptiveField) adaptiveField.hidden = strategy !== "grid_adaptive";
        setText("grid-size-help", strategy === "grid_adaptive"
            ? "Inventaire limité à une taille d’ordre. Quantité fixée au lancement et arrondie au lot supérieur. Les fills partiels peuvent être complétés jusqu’au plafond."
            : "Inventaire limité à une taille d'ordre. Retour à plat avant une nouvelle entrée.");
        if (strategy === "grid_adaptive") {
            setText("grid-heading", "MM Grid Adaptive");
            setText("grid-description", "Moteur original : grille adaptative au régime, à la volatilité et à l’inventaire.");
            setText("grid-strategy-hint", "Safe BBO : spread protégé par les frais + 0,35 bp et l’ATR. La tendance adapte le spread sans couper les quotes. Pause de clôture 15 h 25–16 h 45, heure de Paris, lun–ven.");
            return;
        }
        if (strategy === "mid") {
            setText("grid-heading", "MID Order Book");
            setText("grid-description", "Market making classique : achat et vente à une distance fixe du mid, réglable en bps.");
            setText("grid-strategy-hint", "Même exécution que BBO. Prix = mid ± distance, arrondis au tick, en post-only.");
            return;
        }
        setText("grid-heading", "BBO Order Book");
        setText("grid-description", "Quotes au meilleur bid et ask du carnet, avec contrôles de risque et d'inventaire.");
        setText("grid-strategy-hint", "Achat au meilleur bid, vente au meilleur ask. Suivi à chaque tick; suspension si un contrôle de risque bloque.");
    }

    function updateWcButtons() {
        const startBtn = $("#wc-start-btn");
        const stopBtn = $("#wc-stop-btn");
        if (startBtn) startBtn.disabled = wcRunning || startBtn.getAttribute("aria-busy") === "true";
        if (stopBtn) stopBtn.disabled = !wcRunning || stopBtn.getAttribute("aria-busy") === "true";
        setRunState("wc", wcRunning);
    }

    function switchPage(pageName) {
        $$(".nav-tab").forEach((tab) => {
            const active = tab.dataset.page === pageName;
            tab.classList.toggle("active", active);
            tab.setAttribute("aria-selected", active ? "true" : "false");
            tab.tabIndex = active ? 0 : -1;
        });
        $$(".page").forEach((page) => {
            const active = page.id === `page-${pageName}`;
            page.classList.toggle("active", active);
            page.hidden = !active;
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
            empty.textContent = "Aucun token trouvé";
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
                    closeTokenPicker(true);
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
        $("#grid-token-menu").hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        renderTokenOptions($("#grid-token-search")?.value || "");
        requestAnimationFrame(() => $("#grid-token-search")?.focus());
    }

    function closeTokenPicker(restoreFocus = false) {
        const picker = $("#grid-token-picker");
        const trigger = $("#grid-token-trigger");
        if (!picker || !trigger) return;
        picker.classList.remove("open");
        $("#grid-token-menu").hidden = true;
        trigger.setAttribute("aria-expanded", "false");
        if (restoreFocus) trigger.focus();
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
                    closeWcMarketPicker(true);
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
        $("#wc-market-menu").hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        renderWcMarketOptions($("#wc-market-search")?.value || "");
        requestAnimationFrame(() => $("#wc-market-search")?.focus());
    }

    function closeWcMarketPicker(restoreFocus = false) {
        const picker = $("#wc-market-picker");
        const trigger = $("#wc-market-trigger");
        if (!picker || !trigger) return;
        picker.classList.remove("open");
        $("#wc-market-menu").hidden = true;
        trigger.setAttribute("aria-expanded", "false");
        if (restoreFocus) trigger.focus();
    }

    function bindPickerKeyboard(menuId, searchId, close) {
        const menu = $(`#${menuId}`);
        menu.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                close(true);
                return;
            }
            const options = Array.from(menu.querySelectorAll(".token-option"));
            const index = options.indexOf(document.activeElement);
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                const next = index < 0 ? (event.key === "ArrowDown" ? 0 : options.length - 1)
                    : (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
                options.forEach((option, i) => { option.tabIndex = i === next ? 0 : -1; });
                options[next]?.focus();
            } else if (index >= 0 && (event.key === "Home" || event.key === "End")) {
                event.preventDefault();
                options[event.key === "Home" ? 0 : options.length - 1]?.focus();
            } else if (index >= 0 && event.key === "Tab" && event.shiftKey) {
                event.preventDefault();
                $(`#${searchId}`).focus();
            }
        });
        menu.addEventListener("focusout", () => {
            window.setTimeout(() => {
                if (!menu.parentElement.contains(document.activeElement)) close();
            }, 0);
        });
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
        setText("grid-run-duration", data.duration || "00:00:00");

        const bots = Array.isArray(data.bots) ? data.bots : [];
        const active = bots.filter((bot) => bot.running);
        const sumKnown = (field) => active.length && active.every((bot) => typeof bot[field] === "number" && Number.isFinite(bot[field]))
            ? active.reduce((sum, bot) => sum + bot[field], 0) : null;
        const volume = sumKnown("volume_usd");
        const fees = sumKnown("fees");
        const realized = sumKnown("realized_pnl");
        const net = fees != null && realized != null ? realized - fees : null;
        setText("overview-volume", formatUSD(volume));
        setText("overview-pnl", formatUSD(net, true));
        setPnLColor($("#overview-pnl"), net);
        setText("overview-cost", volume > 0 && net != null ? formatUSD(-net / volume * 1e6) : "—");
        setText("session-count", `${active.length} active${active.length === 1 ? "" : "s"} · ${bots.length} au total`);
        $("#grid-empty-state").hidden = bots.length > 0;
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

        // Keep each card and its focused controls across live state changes.
        const signature = bots.map((b) => b.token).join("|");
        if (container.dataset.tokens !== signature) {
            container.dataset.tokens = signature;
            const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
            container.innerHTML = bots.map((b) => `
              <article class="bot-card" data-token="${escapeHtml(b.token)}">
                <header class="bot-card-head">
                  <span class="bot-dot" aria-hidden="true"></span>
                  <strong class="bot-card-token" data-f="title"></strong>
                  <span class="bot-card-duration" data-f="duration">00:00:00</span>
                  <button class="btn btn-stop bot-card-stop" type="button" data-token="${escapeHtml(b.token)}" aria-label="Arrêter ${escapeHtml(b.token)}">
                    Arrêter
                  </button>
                </header>
                <div class="bot-card-grid">
                  <div class="bot-stat"><span>Mid</span><strong data-f="mid">—</strong></div>
                  <div class="bot-stat"><span>Volume</span><strong data-f="vol">—</strong></div>
                  <div class="bot-stat"><span>Net PnL</span><strong data-f="pnl">—</strong></div>
                  <div class="bot-stat"><span>Realized PnL</span><strong data-f="rpnl">—</strong></div>
                  <div class="bot-stat"><span>Fees</span><strong data-f="fees">—</strong></div>
                  <div class="bot-stat"><span>Cost/1M</span><strong data-f="cost">—</strong></div>
                  <div class="bot-stat"><span>Fills</span><strong data-f="fills">—</strong></div>
                  <div class="bot-stat"><span>Fills/h</span><strong data-f="fills-rate">—</strong></div>
                  <div class="bot-stat"><span>Place / Modify / Cancel</span><strong data-f="order-actions">—</strong></div>
                  <div class="bot-stat"><span>Actions/h</span><strong data-f="actions-rate">—</strong></div>
                  <div class="bot-stat"><span>Position</span><strong data-f="pos">—</strong></div>
                  <div class="bot-stat"><span>Spread</span><strong data-f="spread">—</strong></div>
                  <div class="bot-stat"><span>Markout B/A</span><strong data-f="markout">—</strong></div>
                </div>
                <footer class="bot-card-action" data-f="action"></footer>
              </article>`).join("");
            container.querySelectorAll(".bot-card-stop").forEach((btn) => {
                btn.addEventListener("click", () => stopGridBot(btn.dataset.token));
            });
        }

        bots.forEach((b) => {
            const card = Array.from(container.children).find((item) => item.dataset.token === String(b.token));
            if (!card) return;
            card.classList.toggle("stopped", !b.running);
            card.classList.toggle("failed", Boolean(b.error));
            const set = (f, v) => {
                const el = card.querySelector(`[data-f="${f}"]`);
                if (el && el.textContent !== v) el.textContent = v;
            };
            const modeLabel = b.strategy === "mid" ? `MID · ${Number(b.offset_bps || 0).toFixed(1)} BPS`
                : b.strategy === "grid_adaptive" ? `GRID ADAPTIVE${b.grid_regime ? ` · ${String(b.grid_regime).toUpperCase()}` : ""}`
                : b.strategy === "bbo" ? "BBO" : "ANCIEN MODE";
            const stateLabel = b.phase === "failed" ? "ÉCHEC" : !b.running ? "ARRÊTÉ" : b.phase === "connecting" ? "CONNEXION" : "LIVE";
            set("title", `${b.token} · ${modeLabel} · ${stateLabel}`);
            set("duration", b.duration || "00:00:00");
            set("mid", formatPrice(b.mid_price));
            set("vol", formatUSD(b.volume_usd));
            set("pnl", b.net_pnl == null ? "—" : formatUSD(b.net_pnl, true));
            const pnlEl = card.querySelector('[data-f="pnl"]');
            if (pnlEl) setPnLColor(pnlEl, b.net_pnl);
            set("fees", formatUSD(b.fees));
            set("rpnl", b.realized_pnl == null ? "—" : formatUSD(b.realized_pnl, true));
            set("cost", !b.volume_usd || b.cost_per_1m == null ? "—" : formatUSD(b.cost_per_1m));
            set("fills", String(b.fills || 0));
            set("fills-rate", b.fills_per_hour == null ? "—" : Number(b.fills_per_hour).toFixed(1));
            set("order-actions", b.order_placements == null ? "—" : `${b.order_placements} / ${b.order_modifications} / ${b.order_cancellations}`);
            set("actions-rate", b.order_actions_per_hour == null ? "—" : Number(b.order_actions_per_hour).toFixed(1));
            set("pos", formatUSD(b.position_usd));
            set("spread", formatBPS(b.spread_bps));
            set("markout", b.strategy === "grid_adaptive" ? "—" : `${formatBPS(b.markout_bid_bps)} / ${formatBPS(b.markout_ask_bps)}`);
            set("action", b.error ? `⚠ ${b.error}` : (b.action || ""));
            const stopBtn = card.querySelector(".bot-card-stop");
            if (stopBtn) {
                if (!b.running && document.activeElement === stopBtn) {
                    card.tabIndex = -1;
                    card.focus({ preventScroll: true });
                }
                stopBtn.hidden = !b.running;
            }
        });
    }

    function updateWorldcupDashboard(data) {
        if (!data || data.type !== "worldcup_stats") return;

        setText("wc-duration", data.duration || "00:00:00");
        setText("wc-run-duration", data.duration || "00:00:00");
        setText("wc-volume", formatUSD(data.volume_usd));
        setText("wc-fees", formatUSD(data.fees));
        setText("wc-cost-1m", data.volume_usd > 0 ? formatUSD(data.cost_per_1m) : "—");
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

    async function startGridBot() {
        const token = $("#grid-token")?.value || selectedGridToken.key || "SP500";
        const strategy = $("#grid-strategy")?.value || "bbo";
        const order_size_usd = Number.parseFloat($("#grid-order-size")?.value) || 15;
        const offset_bps = Number.parseFloat($("#grid-offset-bps")?.value);

        if (!validateMin("grid", [
            { selector: "#grid-order-size", min: 10, fallback: order_size_usd, message: "Order USD must be at least 10." },
        ])) {
            showToast("error", "Check market making settings", "Correct the highlighted field before starting.");
            return;
        }
        const offsetError = $("#grid-offset-error");
        const offsetInput = $("#grid-offset-bps");
        if (offsetError) offsetError.hidden = true;
        offsetInput?.removeAttribute("aria-invalid");
        if (strategy === "mid" && (!Number.isFinite(offset_bps) || offset_bps < 0 || offset_bps >= 10000)) {
            if (offsetError) {
                offsetError.textContent = "Saisis une distance entre 0 inclus et 10 000 bps exclus.";
                offsetError.hidden = false;
            }
            offsetInput?.setAttribute("aria-invalid", "true");
            offsetInput?.focus();
            showToast("error", "Check MID offset", "Distance must be between 0 and 10,000 bps (exclusive).");
            return;
        }

        setBusy("grid-start-btn", true);

        try {
            const body = { token, strategy, mode: "live", order_size_usd };
            if (strategy === "mid") body.offset_bps = offset_bps;
            if (strategy === "grid_adaptive") {
                const input = $("#grid-maker-fee-bps");
                const fee = Number.parseFloat(input?.value);
                const error = $("#grid-adaptive-error");
                if (error) error.hidden = true;
                input?.removeAttribute("aria-invalid");
                if (!Number.isFinite(fee) || fee < 0 || fee >= 10000) {
                    if (error) {
                        error.textContent = "Saisis des frais entre 0 inclus et 10 000 bps exclus.";
                        error.hidden = false;
                    }
                    input?.setAttribute("aria-invalid", "true");
                    input?.focus();
                    return;
                }
                body.grid_maker_fee_bps = fee;
            }
            await fetchJson(`${API_BASE}/api/grid/start`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            gridRunning = true;
            setText("grid-run-token", token);
            updateGridButtons();
            showToast("info", "Connexion à Hyperliquid", `${token} · le démarrage est en cours. L'état et les erreurs restent visibles dans la carte.`);
        } catch (error) {
            console.error("Market maker start failed:", error);
            setFormError("grid", error.message);
            showToast("error", "Market maker start failed", error.message);
        } finally {
            setBusy("grid-start-btn", false);
            updateGridButtons();
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
            updateGridButtons();
        }
    }

    async function startWorldcupBot() {
        const order_size_usd = Number.parseFloat($("#wc-order-size")?.value) || 50;
        // En %, même convention que --max-spread-pct (0.02 = 0.02% = 2 bps)
        const max_spread_pct = Number.parseFloat($("#wc-spread")?.value);
        const max_position_usd = Number.parseFloat($("#wc-max-pos")?.value) || 250;
        const market = $("#wc-market")?.value || "ALL";

        if (!validateMin("wc", [
            { selector: "#wc-order-size", min: 10, fallback: order_size_usd, message: "Order USD must be at least 10." },
            { selector: "#wc-max-pos", min: 10, fallback: max_position_usd, message: "Max Pos USD must be at least 10." },
            { selector: "#wc-spread", min: 0, message: "Le spread doit être un nombre positif ou nul." },
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
            setFormError("wc", error.message);
            showToast("error", "WorldCup start failed", error.message);
        } finally {
            setBusy("wc-start-btn", false);
            updateWcButtons();
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
            updateWcButtons();
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
        if (!gate.open) gate.showModal();
        const err = $("#access-gate-error");
        if (err) {
            err.textContent = message || "";
            err.hidden = !message;
        }
        requestAnimationFrame(() => $("#access-code-input")?.focus());
    }

    function hideAccessGate() {
        const gate = $("#access-gate");
        if (gate) {
            gate.close();
            gate.hidden = true;
        }
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

        $("#grid-strategy")?.addEventListener("change", updateGridStrategy);
        updateGridStrategy();

        $$(".nav-tab").forEach((tab) => {
            tab.addEventListener("click", () => switchPage(tab.dataset.page));
            tab.addEventListener("keydown", (event) => {
                const tabs = $$(".nav-tab");
                const index = tabs.indexOf(tab);
                let next;
                if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
                if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
                if (event.key === "Home") next = 0;
                if (event.key === "End") next = tabs.length - 1;
                if (next == null) return;
                event.preventDefault();
                switchPage(tabs[next].dataset.page);
                tabs[next].focus();
            });
        });

        document.addEventListener("keydown", () => { document.documentElement.dataset.input = "keyboard"; });
        document.addEventListener("pointerdown", () => { document.documentElement.dataset.input = "pointer"; });
        $("#access-gate").addEventListener("cancel", (event) => event.preventDefault());
        $("#access-gate").addEventListener("keydown", (event) => {
            if (event.key !== "Tab") return;
            const controls = Array.from(event.currentTarget.querySelectorAll("input, button:not(:disabled)"));
            const first = controls[0];
            const last = controls[controls.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        });
        bindPickerKeyboard("grid-token-menu", "grid-token-search", closeTokenPicker);
        bindPickerKeyboard("wc-market-menu", "wc-market-search", closeWcMarketPicker);

        $("#grid-token-trigger")?.addEventListener("click", () => {
            const picker = $("#grid-token-picker");
            if (picker?.classList.contains("open")) closeTokenPicker();
            else openTokenPicker();
        });

        $("#grid-token-search")?.addEventListener("input", (event) => {
            renderTokenOptions(event.target.value);
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
