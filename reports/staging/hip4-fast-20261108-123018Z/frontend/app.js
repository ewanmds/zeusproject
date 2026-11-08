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
    let hasConnectedOnce = false;
    let gridRunning = false;
    let outcomeRunning = false;
    let outcomeSessions = [];
    let outcomeState = { can_start: false, phase: "connecting" };
    let gridSessions = [];
    let gridTokens = [];
    let selectedGridToken = normalizeToken({ key: "SP500", symbol: "xyz:SP500" });
    const dashboardState = window.ZeusState.createStore();
    const pendingRenders = new Map();
    let renderFrame = 0;

    function receiveDashboard(kind, data) {
        if (!dashboardState.accept(kind, data)) return;
        const view = dashboardState.view(kind);
        if (kind === "outcome") {
            outcomeState = view;
            outcomeSessions = Array.isArray(view.bots) ? view.bots : [];
            outcomeRunning = Boolean(view.running);
        } else {
            gridSessions = Array.isArray(view.bots) ? view.bots : [];
            gridRunning = Boolean(view.running);
        }
        scheduleDashboardRender(kind, view);
        updateFreshness();
    }

    function flushDashboardRenders() {
        if (renderFrame) return;
        renderFrame = requestAnimationFrame(() => {
            renderFrame = 0;
            const renders = Array.from(pendingRenders);
            pendingRenders.clear();
            for (const [name, state] of renders) {
                if (name === "outcome") renderOutcomeDashboard(state);
                else renderGridDashboard(state);
            }
        });
    }

    function scheduleDashboardRender(kind, view) {
        pendingRenders.set(kind, view);
        // A hidden tab keeps the latest snapshot but does not rebuild its DOM.
        if (!document.hidden) flushDashboardRenders();
    }

    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => Array.from(document.querySelectorAll(sel));

    function debounce(fn, ms = 120) {
        let timer = 0;
        return (...args) => {
            clearTimeout(timer);
            timer = window.setTimeout(() => fn(...args), ms);
        };
    }

    // Public catalogs change slowly but cost 38-152 KB: paint the last copy
    // immediately, then replace it with the fresh one in the background.
    const CATALOG_CACHE_KEY = "zeus_catalog_v1_";
    function readCatalogCache(name) {
        try {
            const raw = localStorage.getItem(CATALOG_CACHE_KEY + name);
            return raw ? JSON.parse(raw) : null;
        } catch {
            return null;
        }
    }
    function writeCatalogCache(name, data) {
        try {
            localStorage.setItem(CATALOG_CACHE_KEY + name, JSON.stringify(data));
        } catch {
            /* private mode or quota: caching stays optional */
        }
    }

    function formatUSD(val, showSign = false) {
        return window.ZeusState.currency(val, showSign);
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

    function formatMIDNumber(val) {
        if (val == null || val === "") return "—";
        const num = Number(val);
        return Number.isFinite(num) ? String(num) : "—";
    }

    function formatMIDBPS(val) {
        const value = formatMIDNumber(val);
        return value === "—" ? value : `${value} BPS`;
    }

    function formatMIDPair(bid, ask, formatter = formatPrice) {
        return bid == null && ask == null ? "—" : `${formatter(bid)} / ${formatter(ask)}`;
    }

    function setText(id, value) {
        const el = $(`#${id}`);
        if (el && el.textContent !== String(value)) el.textContent = value;
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
        close.setAttribute("aria-label", "Dismiss notification");
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
        wsConnected = connected;
        updateFreshness();
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
        const token = $("#grid-token")?.value;
        const blocked = gridSessions.some((bot) => (bot.token === token || bot.symbol === selectedGridToken.symbol) && bot.can_start === false);
        if (startBtn) startBtn.disabled = blocked || startBtn.getAttribute("aria-busy") === "true";
        if (stopBtn) stopBtn.disabled = !gridRunning || stopBtn.getAttribute("aria-busy") === "true";
        setRunState("grid", gridRunning);
        updateReconcileButton();
    }

    function updateReconcileButton() {
        const button = $("#sessions-reconcile-btn");
        if (button) button.hidden = !(outcomeSessions.some((bot) => bot.phase === "failed" && bot.cleanup_confirmed !== true)
            || gridSessions.some((bot) => bot.phase === "failed" && bot.cleanup_confirmed !== true));
    }

    function updateGridStrategy() {
        const strategy = $("#grid-strategy")?.value || "bbo";
        const offsetField = $("#grid-offset-field");
        const amountInput = $("#grid-order-size");
        if (amountInput) amountInput.min = strategy === "freqtrade" ? "40" : "10";
        if (offsetField) offsetField.hidden = strategy !== "mid";
        const exitField = $("#grid-exit-protection-field");
        const exitToggle = $("#grid-protect-exits");
        if (exitField) exitField.hidden = strategy !== "mid";
        if (exitToggle) exitToggle.disabled = strategy !== "mid";
        const executionHelp = $("#grid-execution-help");
        if (executionHelp) executionHelp.hidden = strategy === "freqtrade";
        ["grid-offset-help", "grid-exit-protection-help"].forEach((id) => {
            const help = $(`#${id}`);
            if (help) help.hidden = strategy !== "mid";
        });
        if (strategy === "freqtrade") {
            setText("grid-heading", "Freqtrade");
            return;
        }
        if (strategy === "mid") {
            setText("grid-heading", "MID");
            return;
        }
        setText("grid-heading", "BBO");
    }

    let outcomeAutoSnapshot = null;
    let outcomeAutoBusy = false;
    let outcomeAutoEpoch = 0;
    let outcomeAutoPending = null;
    const outcomeIsAuto = () => $("#outcome-selection-mode")?.value === "auto";

    function renderOutcomeCandidates(candidates) {
        const list = $("#outcome-auto-candidates");
        const existing = new Map(Array.from(list.children, (item) => [item.dataset.market, item]));
        const visible = candidates.slice(0, 3);
        const keep = new Set(visible.map((item) => item.market));
        for (const [index, candidate] of visible.entries()) {
            let item = existing.get(candidate.market);
            if (!item) {
                item = document.createElement("li");
                item.dataset.market = candidate.market;
                item.innerHTML = "<strong></strong><small></small><span></span>";
            }
            const values = [candidate.name, candidate.market,
                `${formatUSD(candidate.volume_usd_1m)}/min · queue ${Math.round(candidate.queue_proxy_sec)}s · ${candidate.spread_bps.toFixed(2)} bps`];
            Array.from(item.children).forEach((child, i) => {
                if (child.textContent !== values[i]) child.textContent = values[i];
            });
            item.title = `${candidate.events_5m} trade events · estimated queue ${Math.round(candidate.queue_proxy_sec)}s`;
            if (list.children[index] !== item) list.insertBefore(item, list.children[index] || null);
        }
        for (const [market, item] of existing) if (!keep.has(market)) item.remove();
    }

    async function refreshOutcomeAuto() {
        if (!outcomeIsAuto() || outcomeAutoBusy || document.hidden) return;
        outcomeAutoBusy = true;
        const epoch = outcomeAutoEpoch;
        const side = selectedOutcomeSide();
        try {
            const query = new URLSearchParams({ side: outcomeAutoPending?.outcome_side || side,
                order_usd: outcomeAutoPending?.order_size_usd ?? $("#outcome-order-size").value,
                max_spread_bps: outcomeAutoPending?.auto_max_spread_bps ?? $("#outcome-auto-spread").value });
            const snapshot = await fetchJson(`${API_BASE}/api/outcomes/auto?${query}`);
            if (!outcomeIsAuto() || epoch !== outcomeAutoEpoch || side !== selectedOutcomeSide()) return;
            outcomeAutoSnapshot = snapshot;
            const candidates = snapshot.candidates || [];
            renderOutcomeCandidates(candidates);
            setText("outcome-auto-status", !snapshot.connected ? "Connecting…"
                : snapshot.catalog_stale ? "Refreshing markets…"
                : snapshot.warming_up ? `Observing · ${Math.floor(snapshot.observed_sec)} / 60s · ${snapshot.markets_observed}${snapshot.markets_total ? ` / ${snapshot.markets_total}` : ""} markets`
                : candidates.length ? "Shortest queue · updated live"
                : "Waiting for eligible flow…");
        } catch (error) {
            if (epoch === outcomeAutoEpoch) {
                outcomeAutoSnapshot = null;
                $("#outcome-auto-candidates").replaceChildren();
                setText("outcome-auto-status", `Scanner unavailable: ${error.message}`);
            }
        } finally {
            outcomeAutoBusy = false;
            updateOutcomeButtons();
            await launchPendingOutcome();
        }
    }

    function changeOutcomeSelection() {
        outcomeAutoEpoch++;
        outcomeAutoSnapshot = null;
        const auto = outcomeIsAuto();
        $("#outcome-auto-panel").hidden = !auto;
        $("#outcome-manual-market").hidden = auto;
        $("#outcome-market-picker").hidden = auto;
        if (auto) {
            closeOutcomeMarketPicker();
            $("#outcome-exec-mode").value = "maker";
            $("#outcome-auto-candidates").replaceChildren();
            setText("outcome-auto-status", "Scanning…");
            refreshOutcomeAuto();
        }
        updateOutcomeButtons();
    }

    function updateOutcomeButtons() {
        const startBtn = $("#outcome-start-btn");
        const stopBtn = $("#outcome-stop-btn");
        const auto = outcomeIsAuto();
        const autoFresh = outcomeAutoSnapshot?.ready && Date.now()/1000 - outcomeAutoSnapshot.updated_at < 20;
        const market = auto ? outcomeAutoSnapshot?.candidates?.[0]?.market : selectedOutcomeCoin();
        const selected = outcomeSessions.find((bot) => bot.market === market);
        const blocked = selected && (selected.running || selected.cleanup_confirmed !== true);
        const pending = Boolean(outcomeAutoPending);
        if (startBtn) startBtn.disabled = startBtn.getAttribute("aria-busy") === "true" || !pending && (!auto && blocked || outcomeState.stale || outcomeState.can_start === false);
        setText("outcome-start-label", pending ? "Cancel Auto" : auto ? "Start Auto" : !selectedOutcomeMarket.key ? "Choose market" : "Start");
        setText("outcome-start-help", pending ? "Auto armed · waiting for eligible flow." : auto ? (autoFresh ? "" : "Start Auto to wait for a market.") : !selectedOutcomeMarket.key ? "Select a market." : blocked ? "Market busy · verify its session." : "");
        if (stopBtn) stopBtn.disabled = (!outcomeRunning && !pending) || stopBtn.getAttribute("aria-busy") === "true";
        const modeSelect = $("#outcome-exec-mode");
        const busy = pending || startBtn?.getAttribute("aria-busy") === "true";
        if (modeSelect) modeSelect.disabled = auto || busy;
        ["outcome-selection-mode", "outcome-auto-spread", "outcome-order-size", "outcome-max-pos"].forEach((id) => {
            if ($(`#${id}`)) $(`#${id}`).disabled = busy;
        });
        $$("input[name='outcome-side']").forEach((input) => {
            input.disabled = busy || !auto && input.value === "no" && selectedOutcomeMarket.sides?.length === 1;
        });
        updateOutcomeExecutionMode();
        setRunState("outcome", outcomeRunning);
        updateReconcileButton();
    }

    function updateOutcomeExecutionMode() {
        const maker = $("#outcome-exec-mode")?.value === "maker";
        const auto = outcomeIsAuto();
        const spreadField = $("#outcome-spread-field");
        const spreadInput = $("#outcome-spread");
        const makerField = $("#outcome-maker-spread-field");
        const makerInput = $("#outcome-maker-spread");
        if (makerField) makerField.hidden = !maker || auto;
        if (makerInput) {
            makerInput.required = maker && !auto;
            makerInput.disabled = !maker || auto || $("#outcome-start-btn")?.getAttribute("aria-busy") === "true";
            if (!maker) makerInput.classList.remove("invalid");
        }
        if (spreadField) spreadField.hidden = maker;
        if (spreadInput) {
            spreadInput.disabled = maker;
            spreadInput.required = !maker;
            if (maker) spreadInput.classList.remove("invalid");
        }
        setText("outcome-exec-help", auto ? "Post-only bid / ask. Auto switches only after the old market is flat." : maker
            ? "Post-only. Quote spread sets the distance between bid and ask."
            : "Immediate execution, within the slippage limit.");
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
        updateTabIndicator();
        window.scrollTo({ top: 0, behavior: "smooth" });
    }

    function updateTabIndicator() {
        const nav = $(".strategy-tabs");
        const active = $(".nav-tab.active");
        if (!nav || !active) return;
        const navRect = nav.getBoundingClientRect();
        const tabRect = active.getBoundingClientRect();
        nav.style.setProperty("--tab-x", `${tabRect.left - navRect.left}px`);
        nav.style.setProperty("--tab-w", `${tabRect.width}px`);
    }

    let wsConnected = false;
    function updateFreshness() {
        const el = $("#data-freshness");
        if (!el) return;
        const kind = $(".nav-tab.active")?.dataset.page === "outcome" ? "outcome" : "grid";
        const age = dashboardState.age(kind);
        const view = dashboardState.view(kind);
        const fresh = age < 6000 && !view?.stale;
        const reachable = wsConnected || Math.min(dashboardState.age("grid"), dashboardState.age("outcome")) < 6000;
        const shell = $("#connection-status");
        shell?.classList.toggle("connected", reachable);
        const label = shell?.querySelector(".status-text");
        if (label) label.textContent = wsConnected ? "Connected" : reachable ? "Polling" : "Disconnected";
        const text = !Number.isFinite(age) ? "Waiting for data" : fresh ? "Updated just now" : `Delayed · ${Math.round(age/1000)}s`;
        if (el.textContent !== text) el.textContent = text;
        el.classList.toggle("offline", !reachable);
        el.classList.toggle("stale", !fresh && Number.isFinite(age));
        for (const name of ["grid", "outcome"]) {
            const current = dashboardState.view(name);
            if (!current) continue;
            if (name === "outcome" && current.stale !== outcomeState.stale) {
                outcomeState = current;
                scheduleDashboardRender(name, current);
            }
        }
        updateDashboardClocks();
    }

    function updateDashboardClocks() {
        const outcome = dashboardState.view("outcome");
        if (outcome) {
            const text = dashboardState.elapsed("outcome", outcome);
            setText("outcome-duration", text);
            setText("outcome-run-duration", text);
            for (const card of $$("#outcome-bots-list .bot-card")) {
                const row = outcomeSessions.find((item) => item.session_id === card.dataset.sessionId);
                const value = row && dashboardState.elapsed("outcome", row);
                const label = card.querySelector('[data-f="duration"]');
                if (label && value && label.textContent !== value) label.textContent = value;
            }
        }
        const grid = dashboardState.view("grid");
        if (grid) setText("grid-run-duration", dashboardState.elapsed("grid", grid));
    }

    function clearSkeleton(...ids) {
        ids.forEach((id) => $(`#${id}`)?.removeAttribute("data-skeleton"));
    }

    function flashButtonSuccess(id) {
        const button = $(`#${id}`);
        if (!button) return;
        const use = button.querySelector("use");
        const previous = use?.getAttribute("href");
        button.classList.add("is-success");
        if (use) use.setAttribute("href", "#icon-check");
        window.setTimeout(() => {
            button.classList.remove("is-success");
            if (use && previous) use.setAttribute("href", previous);
        }, 1200);
    }

    async function fetchJson(url, options = {}) {
        const headers = new Headers(options.headers || {});
        if (apiToken) headers.set("X-API-Token", apiToken);
        const apiRead = url.includes("/api/") && !options.method;
        const controller = apiRead ? new AbortController() : null;
        const timeout = controller ? setTimeout(() => controller.abort(), 10000) : null;
        let resp;
        let data = {};
        try {
            resp = await fetch(url, { cache: "no-store", ...options, headers, ...(controller ? { signal: controller.signal } : {}) });
            try { data = await resp.json(); }
            catch (error) {
                if (error.name === "AbortError") throw error;
                throw new Error("Invalid server response");
            }
        } finally {
            if (timeout) clearTimeout(timeout);
        }
        if (resp.status === 401) {
            lockApp("Invalid access code — please enter it again.");
            throw new Error("Invalid access code");
        }
        if (!resp.ok) {
            throw new Error(data.message || data.detail || `HTTP ${resp.status}`);
        }
        return data;
    }

    const DEX_ORDER = ["xyz", "para", "mkts", "io"];
    let activeGridDex = "all";

    function logoText(ticker) {
        const cleaned = String(ticker || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
        return (cleaned.slice(0, 2) || "?").padEnd(2, " ");
    }

    function stripAccents(value) {
        return String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    }

    function formatCompactUSD(value) {
        const num = Number(value || 0);
        if (!Number.isFinite(num) || num <= 0) return "—";
        if (num >= 1e9) return `$${(num / 1e9).toFixed(1)}B`;
        if (num >= 1e6) return `$${(num / 1e6).toFixed(1)}M`;
        if (num >= 1e3) return `$${(num / 1e3).toFixed(0)}K`;
        return `$${num.toFixed(0)}`;
    }

    function paintTokenLogo(box, token) {
        if (!box) return;
        box.replaceChildren();
        if (!token.logoUrl) {
            box.textContent = token.logo;
            return;
        }
        const img = document.createElement("img");
        img.alt = "";
        img.loading = "lazy";
        img.decoding = "async";
        img.addEventListener("error", () => {
            img.remove();
            box.textContent = token.logo;
        }, { once: true });
        img.src = token.logoUrl;
        box.append(img);
    }

    function tokenLogoNode(token, className = "token-logo") {
        const box = document.createElement("span");
        box.className = className;
        paintTokenLogo(box, token);
        return box;
    }

    function normalizeToken(raw) {
        if (typeof raw === "string") {
            return {
                key: raw.toUpperCase(),
                symbol: `xyz:${raw}`,
                dex: "xyz",
                ticker: raw.toUpperCase(),
                name: `xyz:${raw}`,
                logo: logoText(raw),
                logoUrl: "",
                mid: 0,
                maxLeverage: null,
                isCross: true,
                dayVolume: 0,
            };
        }
        const symbol = String(raw?.symbol || "").trim();
        const symbolParts = symbol.includes(":") ? symbol.split(":", 2) : ["xyz", symbol];
        const key = String(raw?.key || symbol || "SP500").trim().toUpperCase();
        const dex = String(raw?.dex || symbolParts[0] || "xyz").toLowerCase();
        const ticker = String(raw?.ticker || symbolParts[1] || key.split(":").pop() || key).toUpperCase();
        return {
            key,
            symbol: symbol || `${dex}:${ticker}`,
            dex,
            ticker,
            name: String(raw?.name || symbol || `${dex}:${ticker}`),
            logo: raw?.logo || logoText(ticker),
            logoUrl: raw?.logo_url || raw?.logoUrl || "",
            mid: Number(raw?.mid || 0),
            maxLeverage: raw?.max_leverage || raw?.maxLeverage || null,
            isCross: raw?.is_cross !== false,
            dayVolume: Number(raw?.day_volume || 0),
        };
    }

    function tokenSubtitle(token) {
        const parts = [token.dex || "xyz"];
        if (token.dayVolume > 0) parts.push(`${formatCompactUSD(token.dayVolume)} vol`);
        if (token.mid) parts.push(formatPrice(token.mid));
        if (token.maxLeverage) parts.push(`${token.maxLeverage}x`);
        if (!token.isCross) parts.push("isolated");
        return parts.join(" · ");
    }

    function setSelectedGridToken(token) {
        selectedGridToken = normalizeToken(token);
        const hidden = $("#grid-token");
        if (hidden) hidden.value = selectedGridToken.key;
        paintTokenLogo($("#grid-token-logo"), selectedGridToken);
        setText("grid-token-name", selectedGridToken.ticker);
        setText("grid-token-symbol", tokenSubtitle(selectedGridToken));
        setText("grid-run-token", selectedGridToken.key);
        renderTokenOptions($("#grid-token-search")?.value || "");
        updateGridButtons();
    }

    function gridDexCounts() {
        const counts = new Map();
        gridTokens.forEach((token) => counts.set(token.dex, (counts.get(token.dex) || 0) + 1));
        const ordered = [
            ...DEX_ORDER.filter((dex) => counts.has(dex)),
            ...Array.from(counts.keys()).filter((dex) => !DEX_ORDER.includes(dex)).sort(),
        ];
        return { counts, ordered };
    }

    function renderDexChips() {
        const container = $("#grid-token-dexes");
        if (!container) return;
        const { counts, ordered } = gridDexCounts();
        if (activeGridDex !== "all" && !counts.has(activeGridDex)) activeGridDex = "all";
        const entries = [["all", "All", gridTokens.length],
            ...ordered.map((dex) => [dex, dex, counts.get(dex)])];
        container.replaceChildren(...entries.map(([dex, label, count]) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "dex-chip";
            button.dataset.dex = dex;
            button.setAttribute("aria-pressed", activeGridDex === dex ? "true" : "false");
            button.textContent = `${label} ${count}`;
            button.addEventListener("click", (event) => {
                event.stopPropagation();
                activeGridDex = dex;
                renderDexChips();
                renderTokenOptions($("#grid-token-search")?.value || "");
                $("#grid-token-search")?.focus();
            });
            return button;
        }));
    }

    let gridTokenOptionsSignature = "";
    function renderTokenOptions(filter = "") {
        const container = $("#grid-token-options");
        if (!container) return;
        // Rebuilding a closed menu on every selection was pure DOM work; the
        // picker renders itself when it opens.
        if (!$("#grid-token-picker")?.classList.contains("open")) return;

        // Build each catalog once, then filter by toggling visibility. The old
        // code rebuilt ~1,600 nodes on every open and every keystroke.
        const signature = gridTokens.map((token) => token.key).join("|");
        if (signature !== gridTokenOptionsSignature) {
            gridTokenOptionsSignature = signature;
            container.replaceChildren(...gridTokens.map((token) => {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "token-option";
                button.setAttribute("role", "option");
                button.tabIndex = -1;
                button.dataset.token = token.key;
                button.dataset.dex = token.dex;
                // Normalised once so typing only scans precomputed text.
                button.dataset.search = stripAccents(
                    `${token.ticker} ${token.name} ${token.symbol} ${token.dex} ${token.key}`
                ).toLowerCase();

                const logo = tokenLogoNode(token);

                const meta = document.createElement("span");
                meta.className = "token-meta";

                const title = document.createElement("span");
                title.className = "token-title";
                const name = document.createElement("strong");
                name.textContent = token.ticker;
                const dex = document.createElement("span");
                dex.className = "token-dex";
                dex.textContent = token.dex;
                title.append(name, dex);

                const subtitle = document.createElement("span");
                subtitle.className = "token-subtitle";
                subtitle.textContent = tokenSubtitle(token);

                const volume = document.createElement("span");
                volume.className = "token-vol";
                volume.textContent = token.dayVolume > 0 ? formatCompactUSD(token.dayVolume) : "";

                meta.append(title, subtitle);
                button.append(logo, meta, volume);
                button.addEventListener("click", (event) => {
                    event.stopPropagation();
                    // Close first so selecting does not rebuild the whole list.
                    closeTokenPicker(true);
                    setSelectedGridToken(token);
                });
                return button;
            }));
        }

        const terms = stripAccents(filter).toLowerCase().split(/\s+/).filter(Boolean);
        let shown = 0;
        for (const button of container.querySelectorAll(".token-option")) {
            const match = (activeGridDex === "all" || button.dataset.dex === activeGridDex)
                && terms.every((term) => button.dataset.search.includes(term));
            button.style.display = match ? "" : "none";
            button.setAttribute("aria-selected", button.dataset.token === selectedGridToken.key ? "true" : "false");
            button.tabIndex = match && shown === 0 ? 0 : -1;
            if (match) shown++;
        }
        let empty = container.querySelector(".empty-option");
        if (!empty) {
            empty = document.createElement("div");
            empty.className = "empty-option";
            empty.textContent = "No markets found";
            container.append(empty);
        }
        empty.style.display = shown ? "none" : "";

        const filtering = terms.length > 0 || activeGridDex !== "all";
        setText("grid-token-results", filtering
            ? `${shown} result${shown === 1 ? "" : "s"}`
            : `${shown} market${shown === 1 ? "" : "s"}`);
    }

    function openTokenPicker() {
        const picker = $("#grid-token-picker");
        const trigger = $("#grid-token-trigger");
        if (!picker || !trigger) return;
        picker.classList.add("open");
        $("#grid-token-menu").hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        renderDexChips();
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

    /* --------- Outcome market picker (catalogue HIP-4 dynamique) --------- */
    const OUTCOME_EMPTY_MARKET = {
        key: "",
        name: "Select a market",
        logo: "OU",
        logoUrl: "",
        category: "all",
        categoryLabel: "Out",
        questionTitle: "",
        subcategory: "",
        sides: [],
        sideIndex: 1,
        sideName: "No",
        mid0: 0,
        mid1: 0,
        underlying: "",
        isFallback: false,
    };
    let outcomeMarkets = [];
    let outcomeCategories = [];
    let outcomeCatalogLoaded = false;
    let outcomeCatalogPromise = null;
    let selectedOutcomeMarket = OUTCOME_EMPTY_MARKET;
    let activeOutcomeCategory = "all";

    function selectedOutcomeSide() {
        return $("input[name='outcome-side']:checked")?.value || "no";
    }

    function selectedOutcomeCoin() {
        if (!selectedOutcomeMarket.key) return "";
        const id = Math.floor(Number(selectedOutcomeMarket.key.slice(1)) / 10);
        return `#${id * 10 + (selectedOutcomeSide() === "yes" ? 0 : 1)}`;
    }

    function normalizeOutcome(raw) {
        const sides = Array.isArray(raw?.sides) && raw.sides.length > 0
            ? raw.sides.map((side) => String(side))
            : ["Yes", "No"];
        const id = Number(raw?.id || 0) || null;
        return {
            id,
            key: String(raw?.key || (id ? `#${id}1` : "")),
            name: String(raw?.name || (id ? `Outcome ${id}` : "Outcome")),
            questionTitle: String(raw?.question_title || ""),
            category: String(raw?.category || "other").toLowerCase(),
            categoryLabel: String(raw?.category_label || raw?.category || "Other"),
            subcategory: String(raw?.subcategory || ""),
            sides,
            sideIndex: Number(raw?.side_index ?? 1),
            sideName: String(raw?.side_name || sides[1] || "No"),
            mid0: Number(raw?.mid0 || 0),
            mid1: Number(raw?.mid1 || 0),
            underlying: String(raw?.underlying || ""),
            isFallback: Boolean(raw?.is_fallback),
            logo: String(raw?.logo || logoText(raw?.name || "")),
            logoUrl: String(raw?.logo_url || ""),
        };
    }

    function outcomeProbability(market, sideIndex) {
        if (!market || !market.key) return "—";
        const value = sideIndex === 0 ? market.mid0 : market.mid1;
        const other = sideIndex === 0 ? market.mid1 : market.mid0;
        if ((!Number.isFinite(value) || value <= 0) && (!Number.isFinite(other) || other <= 0)) return "—";
        const pct = Math.max(0, value || 0) * 100;
        return `${pct.toFixed(pct >= 10 ? 1 : 2)}%`;
    }

    function outcomeTriggerSubtitle(market) {
        if (!market || !market.key) {
            return outcomeMarkets.length > 0
                ? `${outcomeMarkets.length} markets · deployer out`
                : "HIP-4 catalog · out";
        }
        const parts = [];
        if (market.questionTitle) parts.push(market.questionTitle);
        else if (market.categoryLabel) parts.push(market.categoryLabel);
        const sideIndex = selectedOutcomeSide() === "yes" ? 0 : 1;
        const prob = outcomeProbability(market, sideIndex);
        parts.push(`${sideIndex === 0 ? "Yes" : "No"}${prob !== "—" ? ` ${prob}` : ""}`);
        return parts.join(" · ") || "HIP-4 market";
    }

    function outcomeOptionSubtitle(market) {
        if (!market.key) return "Select a specific market";
        if (market.questionTitle) return market.questionTitle;
        const parts = [];
        if (market.underlying) parts.push(market.underlying);
        if (market.subcategory && market.subcategory.toLowerCase() !== "n/a") {
            parts.push(market.subcategory);
        }
        if (parts.length === 0) parts.push(market.categoryLabel);
        return parts.filter(Boolean).join(" · ");
    }

    function paintOutcomeLogo(box, market) {
        paintTokenLogo(box, { logo: market.logo || "?", logoUrl: market.logoUrl || "" });
    }

    function outcomeCategoryLabel(key) {
        const declared = outcomeCategories.find((category) => category.key === key);
        if (declared?.label) return declared.label;
        const row = outcomeMarkets.find((market) => market.category === key);
        return row?.categoryLabel || key;
    }

    function renderOutcomeCategoryChips() {
        const container = $("#outcome-market-categories");
        if (!container) return;
        const counts = new Map();
        outcomeMarkets.forEach((market) => {
            counts.set(market.category, (counts.get(market.category) || 0) + 1);
        });
        if (activeOutcomeCategory !== "all" && !counts.has(activeOutcomeCategory)) {
            activeOutcomeCategory = "all";
        }
        const keys = ["all"];
        outcomeCategories.forEach((category) => {
            if (counts.has(category.key) && !keys.includes(category.key)) keys.push(category.key);
        });
        counts.forEach((_, key) => {
            if (!keys.includes(key)) keys.push(key);
        });
        const entries = keys.map((key) => [
            key,
            key === "all" ? "All" : outcomeCategoryLabel(key),
            key === "all" ? outcomeMarkets.length : counts.get(key) || 0,
        ]);
        container.replaceChildren(...entries.map(([key, label, count]) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "dex-chip";
            button.dataset.category = key;
            button.setAttribute("aria-pressed", activeOutcomeCategory === key ? "true" : "false");
            button.textContent = `${label} ${count}`;
            button.addEventListener("click", (event) => {
                event.stopPropagation();
                activeOutcomeCategory = key;
                renderOutcomeCategoryChips();
                renderOutcomeMarketOptions($("#outcome-market-search")?.value || "");
                $("#outcome-market-search")?.focus();
            });
            return button;
        }));
    }

    function setSelectedOutcomeMarket(market) {
        selectedOutcomeMarket = market || OUTCOME_EMPTY_MARKET;
        if (selectedOutcomeMarket.sides?.length === 1) $("#outcome-side-yes").checked = true;
        const hidden = $("#outcome-market");
        if (hidden) hidden.value = selectedOutcomeCoin();
        setText("outcome-side-yes-prob", outcomeProbability(selectedOutcomeMarket, 0));
        setText("outcome-side-no-prob", selectedOutcomeMarket.sides?.length === 1 ? "Indisponible" : outcomeProbability(selectedOutcomeMarket, 1));
        paintOutcomeLogo($("#outcome-market-logo"), selectedOutcomeMarket);
        setText("outcome-market-name", selectedOutcomeMarket.name);
        setText("outcome-market-symbol", outcomeTriggerSubtitle(selectedOutcomeMarket));
        renderOutcomeMarketOptions($("#outcome-market-search")?.value || "");
        updateOutcomeButtons();
    }

    let outcomeMarketOptionsSignature = "";
    function renderOutcomeMarketOptions(filter = "") {
        const container = $("#outcome-market-options");
        if (!container) return;
        if (!$("#outcome-market-picker")?.classList.contains("open")) return;

        // Build the catalog rows once; side changes rebuild because the price
        // column depends on the selected side.
        const yes = selectedOutcomeSide() === "yes";
        const signature = (yes ? "y" : "n") + ":" + outcomeMarkets.map((market) => market.key).join("|");
        if (signature !== outcomeMarketOptionsSignature) {
            outcomeMarketOptionsSignature = signature;
            container.replaceChildren(...outcomeMarkets.map((market) => {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "token-option";
                button.setAttribute("role", "option");
                button.tabIndex = -1;
                button.dataset.market = market.key;
                button.dataset.category = market.category || "other";
                button.dataset.search = stripAccents(
                    `${market.name} ${market.questionTitle} ${market.categoryLabel} `
                    + `${market.subcategory} ${market.sides.join(" ")} ${market.key} `
                    + `${market.id || ""} ${market.underlying}`
                ).toLowerCase();

                const logo = tokenLogoNode({ logo: market.logo, logoUrl: market.logoUrl });

                const meta = document.createElement("span");
                meta.className = "token-meta";

                const title = document.createElement("span");
                title.className = "token-title";
                const name = document.createElement("strong");
                name.textContent = market.name;
                const badge = document.createElement("span");
                badge.className = "token-dex";
                badge.textContent = market.categoryLabel || "out";
                title.append(name, badge);

                const subtitle = document.createElement("span");
                subtitle.className = "token-subtitle";
                subtitle.textContent = outcomeOptionSubtitle(market);

                const price = document.createElement("span");
                price.className = "token-vol";
                price.textContent = `${yes ? "Yes" : "No"} ${outcomeProbability(market, yes ? 0 : 1)}`;

                meta.append(title, subtitle);
                button.append(logo, meta, price);
                button.addEventListener("click", (event) => {
                    event.stopPropagation();
                    closeOutcomeMarketPicker(true);
                    setSelectedOutcomeMarket(market);
                });
                return button;
            }));
        }

        const terms = stripAccents(filter).toLowerCase().split(/\s+/).filter(Boolean);
        let shown = 0;
        for (const button of container.querySelectorAll(".token-option")) {
            const match = (activeOutcomeCategory === "all" || button.dataset.category === activeOutcomeCategory)
                && terms.every((term) => button.dataset.search.includes(term));
            button.style.display = match ? "" : "none";
            button.setAttribute("aria-selected", button.dataset.market === selectedOutcomeMarket.key ? "true" : "false");
            button.tabIndex = match && shown === 0 ? 0 : -1;
            if (match) shown++;
        }
        let empty = container.querySelector(".empty-option");
        if (!empty) {
            empty = document.createElement("div");
            empty.className = "empty-option";
            empty.textContent = "No markets found";
            container.append(empty);
        }
        empty.style.display = shown ? "none" : "";

        const filtering = terms.length > 0 || activeOutcomeCategory !== "all";
        setText("outcome-market-results", filtering
            ? `${shown} result${shown === 1 ? "" : "s"}`
            : `${shown} market${shown === 1 ? "" : "s"}`);
    }

    function applyOutcomeCatalog(data) {
        const markets = Array.isArray(data?.outcomes) ? data.outcomes.map(normalizeOutcome) : [];
        if (markets.length === 0) return false;
        outcomeMarkets = markets;
        // Fresh probabilities must repaint the open picker's price column.
        outcomeMarketOptionsSignature = "";
        outcomeCategories = Array.isArray(data.categories)
            ? data.categories.filter((category) => category && category.key)
            : [];
        outcomeCatalogLoaded = true;
        activeOutcomeCategory = "all";
        renderOutcomeCategoryChips();
        const current = outcomeMarkets.find((market) => market.key === selectedOutcomeMarket.key);
        setSelectedOutcomeMarket(current || OUTCOME_EMPTY_MARKET);
        setText("outcome-market-count", `${markets.length} markets`);
        return true;
    }

    async function loadOutcomeMarkets() {
        const cached = readCatalogCache("outcome");
        const painted = applyOutcomeCatalog(cached);
        if (!painted) setText("outcome-market-count", "Loading…");
        try {
            const data = await fetchJson(`${API_BASE}/api/outcomes/markets`);
            if (!Array.isArray(data.outcomes) || data.outcomes.length === 0) {
                throw new Error(data.error || "No HIP-4 markets available");
            }
            applyOutcomeCatalog(data);
            writeCatalogCache("outcome", data);
            return true;
        } catch (error) {
            if (painted) return true;   // keep the cached list until the next refresh
            outcomeCatalogLoaded = false;
            setText("outcome-market-count", "Unavailable");
            renderOutcomeCategoryChips();
            setSelectedOutcomeMarket(selectedOutcomeMarket);
            showToast("error", "Outcome catalog unavailable", error.message);
            return false;
        }
    }

    function ensureOutcomeCatalog() {
        if (outcomeCatalogLoaded || outcomeCatalogPromise) return outcomeCatalogPromise;
        outcomeCatalogPromise = loadOutcomeMarkets().finally(() => {
            outcomeCatalogPromise = null;
        });
        return outcomeCatalogPromise;
    }

    function openOutcomeMarketPicker() {
        const picker = $("#outcome-market-picker");
        const trigger = $("#outcome-market-trigger");
        if (!picker || !trigger) return;
        picker.classList.add("open");
        $("#outcome-market-menu").hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        ensureOutcomeCatalog();
        renderOutcomeCategoryChips();
        renderOutcomeMarketOptions($("#outcome-market-search")?.value || "");
        requestAnimationFrame(() => $("#outcome-market-search")?.focus());
    }

    function closeOutcomeMarketPicker(restoreFocus = false) {
        const picker = $("#outcome-market-picker");
        const trigger = $("#outcome-market-trigger");
        if (!picker || !trigger) return;
        picker.classList.remove("open");
        $("#outcome-market-menu").hidden = true;
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
            const options = Array.from(menu.querySelectorAll(".token-option"))
                .filter((option) => option.style.display !== "none");
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

    function applyGridTokenCatalog(data) {
        const tokens = Array.isArray(data?.tokens) ? data.tokens.map(normalizeToken) : [];
        if (tokens.length === 0) return false;
        // Markets sorted by 24h volume descending (then ticker/dex).
        gridTokens = tokens.sort((a, b) =>
            (b.dayVolume - a.dayVolume)
            || a.ticker.localeCompare(b.ticker)
            || a.dex.localeCompare(b.dex));
        // Fresh mids/volumes must repaint option subtitles on the next open.
        gridTokenOptionsSignature = "";
        const preferred = gridTokens.find((token) => token.key === selectedGridToken.key)
            || gridTokens.find((token) => token.key === "SP500")
            || gridTokens[0];
        activeGridDex = "all";
        renderDexChips();
        setSelectedGridToken(preferred);
        const dexCount = new Set(gridTokens.map((token) => token.dex)).size;
        setText("grid-token-count", `${gridTokens.length} markets · ${dexCount} dex`);
        return true;
    }

    async function loadGridTokens() {
        const cached = readCatalogCache("grid");
        const painted = applyGridTokenCatalog(cached);
        if (!painted) setText("grid-token-count", "Loading");
        try {
            const data = await fetchJson(`${API_BASE}/api/grid/tokens`);
            if (!applyGridTokenCatalog(data)) return;
            writeCatalogCache("grid", data);
        } catch (error) {
            if (painted) return;        // keep the cached list until the next refresh
            gridTokens = [selectedGridToken];
            renderDexChips();
            setSelectedGridToken(selectedGridToken);
            setText("grid-token-count", "Fallback");
            showToast("error", "Token catalog unavailable", error.message);
        }
    }

    let statusRefreshPromise = null;
    function loadInitialStatus({ silent = false } = {}) {
        if (statusRefreshPromise) return statusRefreshPromise;
        statusRefreshPromise = Promise.allSettled([
            fetchJson(`${API_BASE}/api/grid/status`).then(updateGridDashboard),
            fetchJson(`${API_BASE}/api/outcomes/status`).then(updateOutcomeDashboard),
        ]).then((results) => {
            if (!silent && results.every((result) => result.status === "rejected")) {
                showToast("error", "Status unavailable", results[0].reason.message);
            }
        }).finally(() => { statusRefreshPromise = null; });
        return statusRefreshPromise;
    }

    function connectWebSocket() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

        try {
            ws = new WebSocket(buildWsUrl());
        } catch {
            scheduleReconnect();
            return;
        }

        const socket = ws;
        socket.onopen = () => {
            if (ws !== socket) return;
            setConnectionStatus(true);
            if (hasConnectedOnce) {
                showToast("success", "Back online", "Live stats are streaming again.");
            }
            hasConnectedOnce = true;
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
        };

        socket.onclose = () => {
            if (ws !== socket) return;
            setConnectionStatus(false);
            loadInitialStatus({ silent: true });
            if (hasConnectedOnce) {
                showToast("error", "Connection lost", "Reconnecting automatically.");
            }
            scheduleReconnect();
        };

        socket.onerror = () => {
            if (ws !== socket) return;
            setConnectionStatus(false);
        };

        socket.onmessage = (event) => {
            if (ws !== socket) return;
            try {
                const data = JSON.parse(event.data);
                if (data.type === "grid_stats") updateGridDashboard(data);
                if (data.type === "worldcup_stats") updateOutcomeDashboard(data);
            } catch (error) {
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
        if (data?.type === "grid_stats") receiveDashboard("grid", data);
    }

    function renderGridDashboard(data) {
        setText("grid-run-duration", data.duration || "00:00:00");

        const bots = Array.isArray(data.bots) ? data.bots : [];
        gridSessions = bots;
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
        setText("overview-cost", volume > 0 && net != null && active.every((bot) => bot.accounting_complete !== false) ? formatUSD(-net / volume * 1e6) : "—");
        clearSkeleton("overview-volume", "overview-pnl", "overview-cost");
        setText("session-count", `${active.length} active · ${bots.length} total`);
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
                  <button class="btn btn-stop bot-card-stop" type="button" data-token="${escapeHtml(b.token)}" aria-label="Stop ${escapeHtml(b.token)}">
                    Stop
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
                  <div class="bot-stat" data-mid-quote hidden><span>Opening target bid / ask</span><strong data-f="mid-target">—</strong></div>
                  <div class="bot-stat" data-mid-quote hidden><span>Live quote bid / ask</span><strong data-f="mid-resting">—</strong></div>
                  <div class="bot-stat" data-mid-quote hidden><span>Effective distance bid / ask</span><strong data-f="mid-distance">—</strong></div>
                  <div class="bot-stat" data-mid-quote hidden><span>MID opening reprice step</span><strong data-f="mid-reprice">—</strong></div>
                  <div class="bot-stat"><span>Markout B/A</span><strong data-f="markout">—</strong></div>
                  <div class="bot-stat"><span>Required edge / side</span><strong data-f="edge">—</strong></div>
                </div>
                <footer class="bot-card-action" data-f="action"></footer>
              </article>`).join("");
            container.querySelectorAll(".bot-card-stop").forEach((btn) => {
                btn.addEventListener("click", () => stopGridBot(btn.dataset.token));
            });
        }

        const cards = new Map(Array.from(container.children).map((card) => [card.dataset.token, card]));
        bots.forEach((b) => {
            const card = cards.get(String(b.token));
            if (!card) return;
            const riskNotice = /^(?:max drawdown exceeded|session stop-loss triggered)\b/i.test(String(b.error || "").trim());
            card.classList.toggle("stopped", !b.running);
            card.classList.toggle("failed", (b.phase === "failed" || !b.phase && !b.running && Boolean(b.error)) && !riskNotice);
            const set = (f, v) => {
                const el = card.querySelector(`[data-f="${f}"]`);
                if (el && el.textContent !== v) el.textContent = v;
            };
            const modeLabel = b.strategy === "mid" ? `MID · ${formatMIDBPS(b.offset_bps)}${b.mid_exit_policy === "hold_to_profit" ? " · HOLD PROFIT" : b.mid_protect_exits ? ` · EXIT GUARD ${Number(b.mid_exit_protect_sec || 30)}S` : ""}`
                : b.strategy === "freqtrade" ? "FREQTRADE"
                : b.strategy === "bbo" ? "BBO" : "ANCIEN MODE";
            const stateLabel = b.stale ? "STALE STATE" : b.phase === "failed" ? (b.cleanup_confirmed ? "FAILED · FLAT" : "FAILED · STOP UNCONFIRMED") : b.phase === "stopping" ? "STOPPING" : !b.running ? "STOPPED" : b.phase === "connecting" ? "CONNECTING" : "LIVE";
            const executionLabel = b.execution_policy === "maker_only_auto" ? ` · MAKER${b.execution_transport === "websocket" ? " / WS" : ""}` : "";
            set("title", `${b.token} · ${modeLabel}${executionLabel} · ${stateLabel}`);
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
            card.querySelectorAll("[data-mid-quote]").forEach((el) => { el.hidden = b.strategy !== "mid"; });
            if (b.strategy === "mid") {
                const quote = b.mid_quote || {};
                set("mid-target", formatMIDPair(quote.target_bid, quote.target_ask));
                set("mid-resting", formatMIDPair(quote.resting_bid, quote.resting_ask));
                set("mid-distance", formatMIDPair(quote.target_bid_distance_bps, quote.target_ask_distance_bps, formatMIDBPS));
                const step = formatMIDBPS(quote.reprice_threshold_bps);
                const ticks = formatMIDNumber(quote.reprice_threshold_ticks);
                set("mid-reprice", step === "—" && ticks === "—" ? "—"
                    : `${step} / ${ticks}${ticks === "—" ? "" : Number(quote.reprice_threshold_ticks) === 1 ? " tick" : " ticks"}`);
            }
            set("markout", `${formatBPS(b.markout_bid_bps)} / ${formatBPS(b.markout_ask_bps)}`);
            set("edge", b.edge_required_bps ? `${formatBPS(b.edge_required_bps)} / ${formatBPS(b.quote_half_spread_bps)}` : "—");
            const passiveRisk = b.risk_exit_reason ? `Maker exit: ${b.risk_exit_reason}` : "";
            set("action", [b.error, b.cleanup_error, b.accounting_error].filter(Boolean).join(" · ") || [passiveRisk, b.warning].filter(Boolean).join(" · ") || b.action || "");
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

    function renderOutcomeSessions(bots) {
        const container = $("#outcome-bots-list");
        if (!container) return;
        bots = bots.filter((bot) => bot.running || bot.cleanup_confirmed !== true || bot.needs_attention);
        if ($("#outcome-idle")) $("#outcome-idle").hidden = bots.length > 0;
        setText("outcome-sessions-count", `${bots.filter((bot) => bot.running).length} active`);
        const keep = new Set(bots.map((bot) => bot.session_id));
        const cards = new Map(Array.from(container.querySelectorAll(".bot-card"))
            .map((card) => [card.dataset.sessionId, card]));
        for (const [sessionId, card] of cards) {
            if (!keep.has(sessionId)) {
                if (card.contains(document.activeElement)) $("#outcome-sessions-heading")?.focus();
                card.remove();
                cards.delete(sessionId);
            }
        }
        const marketNameById = new Map();
        outcomeMarkets.forEach((market) => {
            const id = Math.floor(Number(String(market.key || "").slice(1)) / 10);
            if (!Number.isNaN(id)) marketNameById.set(id, market.name);
        });
        bots.forEach((bot) => {
            let card = cards.get(bot.session_id);
            if (!card) {
                card = document.createElement("article");
                card.className = "bot-card";
                card.dataset.sessionId = bot.session_id;
                card.innerHTML = `<header class="bot-card-head"><span class="bot-dot" aria-hidden="true"></span><strong class="bot-card-token" data-f="title"></strong><span class="bot-card-duration" data-f="duration"></span><button class="btn btn-stop bot-card-stop" type="button">Stop</button></header><p class="bot-card-action" data-f="settings"></p><div class="bot-card-grid"><div class="bot-stat"><span>State</span><strong data-f="state"></strong></div><div class="bot-stat"><span>Volume</span><strong data-f="volume_usd"></strong></div><div class="bot-stat"><span>Net PnL</span><strong data-f="net_pnl"></strong></div><div class="bot-stat"><span>Fees</span><strong data-f="fees"></strong></div><div class="bot-stat"><span>Cost / 1M</span><strong data-f="cost_per_1m"></strong></div><div class="bot-stat"><span>Fills</span><strong data-f="fills"></strong></div><div class="bot-stat"><span>Position</span><strong data-f="position_usd"></strong></div></div><footer class="bot-card-action" data-f="error"></footer>`;
                card.querySelector("button").addEventListener("click", () => stopOutcomeBot(card.dataset.sessionId));
                const quotes = document.createElement("p");
                quotes.className = "bot-card-quotes";
                quotes.dataset.f = "quotes";
                quotes.hidden = true;
                card.querySelector('[data-f="settings"]').after(quotes);
                container.append(card);
                cards.set(bot.session_id, card);
            }
            const set = (key, value) => {
                const element = card.querySelector(`[data-f="${key}"]`);
                if (element.textContent !== String(value)) element.textContent = value;
            };
            const config = bot.config || {};
            const name = bot.market_name || marketNameById.get(Math.floor(Number(String(bot.market || "").slice(1)) / 10)) || bot.market;
            const side = config.outcome_side || (Number(bot.market?.slice(1)) % 10 === 0 ? "yes" : "no");
            set("title", `${name} · ${side === "yes" ? "Yes" : "No"}`);
            set("duration", bot.duration || "00:00:00");
            const autoMode = config.selection_mode === "auto";
            set("settings", `${autoMode ? "Auto flow · " : ""}${config.exec_mode === "maker" ? "Limit / Maker" : "Market / Taker"}${config.two_sided ? " · Bid + Ask" : ""} · order ${formatUSD(config.order_size_usd)} · cap ${formatUSD(config.max_position_usd)} · ${autoMode ? `best bid / ask · rotate on low fills${bot.auto_rotations ? ` · ${bot.auto_rotations} switches` : ""}` : config.exec_mode === "maker" ? `target spread ${config.maker_spread_pct}%` : `max spread ${config.max_spread_pct ?? config.max_spread_bps / 100}%`}`);
            set("state", bot.stale || outcomeState.stale ? "Stale data" : ({ connecting: "Starting", running: bot.execution_state || "Active", rotating: "Switching market", blocked: "Verification required", recovering: "Verifying · auto-recovery", stopping: "Stopping", stopped: "Stop confirmed", failed: bot.cleanup_confirmed ? "Error · flat" : "Stop unconfirmed" }[bot.phase] || bot.phase));
            const quote = !bot.stale && !outcomeState.stale && bot.running ? bot.quotes?.[0] : null;
            const quoteLabel = card.querySelector('[data-f="quotes"]');
            set("quotes", window.ZeusState.quoteText(quote));
            quoteLabel.hidden = !quote;
            card.querySelector('[data-f="state"]').title = Number.isFinite(bot.account_sync_ms)
                ? `Account sync ${Math.round(bot.account_sync_ms)} ms${Number.isFinite(bot.orders_sent) ? ` · ${bot.orders_sent} orders · ${bot.cancels_sent} cancels` : ""}` : "";
            for (const key of ["volume_usd", "net_pnl", "fees", "cost_per_1m", "position_usd"]) set(key, bot.accounting_complete === false ? "—" : formatUSD(bot[key]));
            set("fills", bot.accounting_complete === false ? "—" : String(bot.fills || 0));
            const errors = [bot.error, bot.recovery_error, bot.cleanup_error].filter(Boolean);
            set("error", [...new Set(errors.map(window.ZeusState.errorText)), bot.auto_rotation_reason,
                !bot.error && !bot.recovery_error ? bot.blocked_reason || (bot.execution_state ? "" : bot.last_action || bot.status) : ""].filter(Boolean).join(" · "));
            card.querySelector('[data-f="error"]').title = errors.join(" · ");
            card.classList.toggle("stopped", !bot.running);
            card.classList.toggle("failed", bot.phase === "failed");
            setPnLColor(card.querySelector('[data-f="net_pnl"]'), bot.net_pnl);
            const button = card.querySelector("button");
            button.setAttribute("aria-label", `Stop ${name} ${side === "yes" ? "Yes" : "No"}`);
            if (!bot.running && document.activeElement === button) { card.tabIndex = -1; card.focus({ preventScroll: true }); }
            button.hidden = !bot.running;
            button.disabled = bot.phase === "stopping" || button.getAttribute("aria-busy") === "true";
        });
    }

    function updateOutcomeDashboard(data) {
        if (!data || data.type !== "worldcup_stats") return;
        receiveDashboard("outcome", data);
    }

    function renderOutcomeDashboard(data) {
        outcomeState = data;

        outcomeSessions = Array.isArray(data.bots) ? data.bots : [];
        renderOutcomeSessions(outcomeSessions);
        setText("outcome-duration", data.duration || "00:00:00");
        setText("outcome-run-duration", data.duration || "00:00:00");
        setText("outcome-volume", data.accounting_complete === false ? "—" : formatUSD(data.volume_usd));
        setText("outcome-fees", data.accounting_complete === false ? "—" : formatUSD(data.fees));
        setText("outcome-cost-1m", data.accounting_complete !== false && data.volume_usd > 0 ? formatUSD(data.cost_per_1m) : "—");
        setText("outcome-fills", data.accounting_complete === false ? "—" : String(data.fills || 0));
        clearSkeleton("outcome-pnl", "outcome-duration", "outcome-volume", "outcome-fees", "outcome-cost-1m", "outcome-fills");

        const pnl = $("#outcome-pnl");
        if (pnl) {
            pnl.textContent = data.accounting_complete === false ? "—" : formatUSD(data.net_pnl, true);
            setPnLColor(pnl, data.net_pnl);
        }

        if (typeof data.running === "boolean") {
            outcomeRunning = data.running;
            updateOutcomeButtons();
        }
        const incident = [...new Set([data.error, data.recovery_error, data.cleanup_error].filter(Boolean).map(window.ZeusState.errorText)), data.stale ? "Live data delayed." : ""].filter(Boolean).join(" · ");
        setFormError("outcome", incident);
        const banner = $("#outcome-run-banner");
        const stateLabel = data.stale ? "Stale data" : `${data.active_markets || 0} active Outcome bot(s)`;
        if (banner) {
            banner.hidden = !data.running && data.phase !== "failed";
            const title = banner.querySelector("strong");
            if (title) title.textContent = stateLabel;
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
                offsetError.textContent = "Enter a distance from 0 up to (but not including) 10,000 bps.";
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
            if (strategy === "mid") {
                body.offset_bps = offset_bps;
                body.mid_protect_exits = $("#grid-protect-exits")?.checked === true;
                body.mid_exit_protect_sec = 30;
            }
            const response = await fetchJson(`${API_BASE}/api/grid/start`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            if (response.state) updateGridDashboard(response.state);
            else await loadInitialStatus({ silent: true });
            updateGridButtons();
            flashButtonSuccess("grid-start-btn");
            showToast("info", "Connecting to Hyperliquid", `${token} · starting now. State and errors stay visible in the card.`);
        } catch (error) {
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
            const response = await fetchJson(`${API_BASE}/api/grid/stop`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(token ? { token } : {}),
            });
            if (response.state) updateGridDashboard(response.state);
            else await loadInitialStatus({ silent: true });
            showToast("info", token ? `Bot ${token} stopped` : "All bots stopped",
                "Manual stop: the bot cancels every open order and closes the position reduce-only before exiting.");
        } catch (error) {
            showToast("error", "Stop failed", error.message);
        } finally {
            setBusy("grid-stop-btn", false);
            updateGridButtons();
        }
    }

    async function startOutcomeBot() {
        if (outcomeAutoPending) { cancelPendingOutcome(); return; }
        const auto = outcomeIsAuto();
        const selection_mode = auto ? "auto" : "manual";
        const auto_max_spread_bps = Number($("#outcome-auto-spread")?.value || 10);
        const exec_mode = $("#outcome-exec-mode")?.value || "taker";
        const maker_spread_pct = Number.parseFloat($("#outcome-maker-spread")?.value);
        const order_size_usd = Number.parseFloat($("#outcome-order-size")?.value) || 50;
        // En %, même convention que --max-spread-pct (0.02 = 0.02% = 2 bps)
        const max_spread_pct = Number.parseFloat($("#outcome-spread")?.value);
        const max_position_usd = Number.parseFloat($("#outcome-max-pos")?.value) || 250;
        let market = auto ? outcomeAutoSnapshot?.candidates?.[0]?.market : $("#outcome-market")?.value || "";
        const outcome_side = selectedOutcomeSide();
        if (!auto && !market) { openOutcomeMarketPicker(); return; }

        if (!validateMin("outcome", [
            { selector: "#outcome-order-size", min: 10, fallback: order_size_usd, message: "Order USD must be at least 10." },
            { selector: "#outcome-max-pos", min: 10, fallback: max_position_usd, message: "Max Pos USD must be at least 10." },
            ...(exec_mode === "taker" ? [{ selector: "#outcome-spread", min: 0, message: "Spread must be a positive number or zero." }] : []),
            ...(exec_mode === "maker" && !auto ? [{ selector: "#outcome-maker-spread", min: 0, message: "Spread limit must be a positive number or zero." }] : []),
            ...(auto ? [{ selector: "#outcome-auto-spread", min: .01, message: "Auto spread must be between 0.01 and 100 bps." }] : []),
        ])) {
            showToast("error", "Check Outcome settings", "Correct the highlighted field before starting.");
            return;
        }

        if (auto && auto_max_spread_bps > 100) { setFormError("outcome", "Auto spread must be between 0.01 and 100 bps."); return; }
        const payload = { selection_mode, auto_max_spread_bps, two_sided: exec_mode === "maker", auto_rotate: auto,
            ...(auto ? { auto_rotation_after_sec: 180 } : {}),
            order_size_usd, ...(exec_mode === "taker" ? { max_spread_pct } : { maker_spread_pct }), max_position_usd, market, exec_mode, outcome_side };
        if (auto) {
            outcomeAutoPending = Object.freeze(payload);
            updateOutcomeButtons();
            await launchPendingOutcome();
            if (outcomeAutoPending) refreshOutcomeAuto();
            return;
        }
        await submitOutcomeStart(payload);
    }

    function cancelPendingOutcome() {
        outcomeAutoPending = null;
        outcomeAutoEpoch++; // Discard a preview already in flight.
        updateOutcomeButtons();
    }

    async function launchPendingOutcome() {
        const payload = outcomeAutoPending;
        const snap = outcomeAutoSnapshot;
        if (!payload || !snap?.ready || Date.now()/1000-snap.updated_at >= 20
                || outcomeState.stale || outcomeState.can_start === false
                || snap.side && snap.side !== payload.outcome_side) return;
        const candidate = snap.candidates?.find((item) => !outcomeSessions.some((bot) =>
            bot.market === item.market && (bot.running || bot.cleanup_confirmed !== true)));
        if (!candidate) return;
        outcomeAutoPending = null; // Exactly one launch per explicit Start Auto.
        await submitOutcomeStart({ ...payload, market: candidate.market });
    }

    async function submitOutcomeStart(payload) {
        let { market } = payload;
        const { selection_mode, auto_max_spread_bps, order_size_usd, max_position_usd,
            exec_mode, outcome_side, maker_spread_pct, max_spread_pct } = payload;
        setBusy("outcome-start-btn", true);
        updateOutcomeButtons();

        try {
            const started = await fetchJson(`${API_BASE}/api/outcomes/start`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            market = started.market || market;
            const marketName = outcomeMarkets.find((item) => Math.floor(Number(item.key.slice(1)) / 10) === Math.floor(Number(market.slice(1)) / 10))?.name || market;
            updateOutcomeDashboard(started.state || await fetchJson(`${API_BASE}/api/outcomes/status`));
            const marketTxt = `${marketName} · ${outcome_side === "yes" ? "Yes" : "No"}`;
            flashButtonSuccess("outcome-start-btn");
            showToast("success", "Starting bot", `${marketTxt} · ${exec_mode === "maker" ? "Maker" : "Taker"}`);
        } catch (error) {
            setFormError("outcome", error.message);
            showToast("error", "Outcome start failed", error.message);
        } finally {
            setBusy("outcome-start-btn", false);
            updateOutcomeButtons();
        }
    }

    async function stopOutcomeBot(sessionId = null) {
        if (!sessionId && outcomeAutoPending) {
            cancelPendingOutcome();
            if (!outcomeRunning) { showToast("info", "Auto canceled", ""); return; }
        }
        const button = sessionId ? Array.from($("#outcome-bots-list")?.children || []).find((card) => card.dataset.sessionId === sessionId)?.querySelector("button") : $("#outcome-stop-btn");
        if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); }
        try {
            const response = await fetchJson(`${API_BASE}/api/outcomes/stop`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(sessionId ? { session_id: sessionId } : {}) });
            const state = response.state || await fetchJson(`${API_BASE}/api/outcomes/status`);
            updateOutcomeDashboard(state);
            const verified = response.stopped_session_ids;
            const targets = (state.bots || []).filter((bot) => !sessionId || (verified ? verified.includes(bot.session_id) : bot.session_id === sessionId));
            if ((!targets.length && !verified) || targets.some((bot) => bot.running || bot.cleanup_confirmed !== true)) throw new Error("Stop not confirmed: verify orders and position.");
            showToast("info", "Stop confirmed", sessionId ? "The selected bot is stopped." : "All Outcome bots are stopped.");
        } catch (error) {
            showToast("error", "Outcome stop failed", error.message);
        } finally {
            if (button) button.removeAttribute("aria-busy");
            updateOutcomeButtons();
            renderOutcomeSessions(outcomeSessions);
        }
    }

    // ------------------------------------------------------- stop confirmation
    let pendingStopScope = null;

    function openStopConfirm(scope) {
        const dialog = $("#confirm-stop");
        if (!dialog) return;
        pendingStopScope = scope;
        const isOutcome = scope === "outcome";
        const count = isOutcome
            ? outcomeSessions.filter((bot) => bot.running).length
            : gridSessions.filter((bot) => bot.running).length;
        const armed = isOutcome && Boolean(outcomeAutoPending);
        const noun = isOutcome ? "Outcome" : "market-making";
        setText("confirm-stop-title", armed && count === 0 ? "Cancel Auto?" : `Stop all ${noun} sessions?`);
        setText("confirm-stop-message", armed ? "Cancel the pending launch and stop any active Outcome sessions." : count > 0
            ? `${count} active ${noun} session${count === 1 ? "" : "s"} will be stopped. This cancels every open order and closes each position reduce-only.`
            : `No active ${noun} sessions right now.`);
        const accept = $("#confirm-stop-accept");
        if (accept) accept.textContent = armed && count === 0 ? "Cancel Auto" : count > 0 ? `Stop ${count}` : "Stop all";
        dialog.showModal();
        requestAnimationFrame(() => $("#confirm-stop-cancel")?.focus());
    }

    function closeStopConfirm() {
        const dialog = $("#confirm-stop");
        if (dialog?.open) dialog.close();
        pendingStopScope = null;
    }

    function openMetricsGuide() {
        const dialog = $("#metrics-guide");
        if (dialog && !dialog.open) dialog.showModal();
    }

    // ------------------------------------------------------------ access gate
    let appBooted = false;

    function bootApp() {
        if (appBooted) return;
        appBooted = true;
        loadGridTokens();
        ensureOutcomeCatalog();
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
            showAccessGate("Enter the access code.");
            return;
        }
        setBusy("access-code-submit", true);
        apiToken = code;
        try {
            await fetchJson(`${API_BASE}/api/health`);
            try { localStorage.setItem(TOKEN_STORAGE_KEY, code); } catch { /* private mode */ }
            hideAccessGate();
            showToast("success", "Access granted", "Code saved in this browser.");
            bootApp();
            if (ws) {
                try { ws.close(); } catch { /* reconnexion avec token */ }
                ws = null;
            }
            connectWebSocket();
        } catch (error) {
            if (String(error.message).includes("Invalid access code")) {
                // 401: lockApp a déjà réaffiché la porte avec le message.
            } else {
                // Backend injoignable: on mémorise quand même — un 401 plus
                // tard re-verrouillera si le code est faux.
                try { localStorage.setItem(TOKEN_STORAGE_KEY, code); } catch { /* private mode */ }
                hideAccessGate();
                showToast("error", "Backend unreachable",
                    "Code saved. Use the Reconnect button to test the connection again.");
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
            showToast("success", "Backend online", API_BASE);
            if (!appBooted) bootApp();
            if (!ws || (ws.readyState !== WebSocket.OPEN && ws.readyState !== WebSocket.CONNECTING)) {
                connectWebSocket();
            }
            loadInitialStatus();
        } catch (error) {
            showToast("error", "Backend unreachable", `${API_BASE} — ${error.message}`);
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
        bindPickerKeyboard("outcome-market-menu", "outcome-market-search", closeOutcomeMarketPicker);

        $("#grid-token-trigger")?.addEventListener("click", () => {
            const picker = $("#grid-token-picker");
            if (picker?.classList.contains("open")) closeTokenPicker();
            else openTokenPicker();
        });

        const renderTokenOptionsSoon = debounce((value) => renderTokenOptions(value));
        const renderOutcomeMarketOptionsSoon = debounce((value) => renderOutcomeMarketOptions(value));
        $("#grid-token-search")?.addEventListener("input", (event) => {
            const clear = $("#grid-token-search-clear");
            if (clear) clear.hidden = !event.target.value;
            renderTokenOptionsSoon(event.target.value);
        });

        $("#grid-token-search-clear")?.addEventListener("click", () => {
            const input = $("#grid-token-search");
            if (input) {
                input.value = "";
                input.focus();
            }
            const clear = $("#grid-token-search-clear");
            if (clear) clear.hidden = true;
            renderTokenOptions("");
        });

        document.addEventListener("click", (event) => {
            const picker = $("#grid-token-picker");
            if (picker && !picker.contains(event.target)) closeTokenPicker();
            const outcomePicker = $("#outcome-market-picker");
            if (outcomePicker && !outcomePicker.contains(event.target)) closeOutcomeMarketPicker();
        });

        $("#outcome-market-trigger")?.addEventListener("click", () => {
            const picker = $("#outcome-market-picker");
            if (picker?.classList.contains("open")) closeOutcomeMarketPicker();
            else openOutcomeMarketPicker();
        });

        $("#outcome-market-search")?.addEventListener("input", (event) => {
            const clear = $("#outcome-market-search-clear");
            if (clear) clear.hidden = !event.target.value;
            renderOutcomeMarketOptionsSoon(event.target.value);
        });

        $("#outcome-market-search-clear")?.addEventListener("click", () => {
            const input = $("#outcome-market-search");
            if (input) {
                input.value = "";
                input.focus();
            }
            const clear = $("#outcome-market-search-clear");
            if (clear) clear.hidden = true;
            renderOutcomeMarketOptions("");
        });

        renderOutcomeCategoryChips();
        setSelectedOutcomeMarket(selectedOutcomeMarket);

        $("#grid-start-btn")?.addEventListener("click", startGridBot);
        $("#grid-stop-btn")?.addEventListener("click", () => openStopConfirm("grid"));
        $("#outcome-selection-mode")?.addEventListener("change", changeOutcomeSelection);
        ["outcome-auto-spread", "outcome-order-size"].forEach((id) => $(`#${id}`)?.addEventListener("input", () => {
            if (outcomeIsAuto()) changeOutcomeSelection();
        }));
        $$("input[name='outcome-side']").forEach((input) => input.addEventListener("change", () => {
            if (outcomeIsAuto()) changeOutcomeSelection();
        }));
        window.setInterval(() => { if (appBooted && outcomeIsAuto()) refreshOutcomeAuto(); }, 10000);
        $("#outcome-start-btn")?.addEventListener("click", (event) => {
            if (!outcomeIsAuto() && !selectedOutcomeMarket.key) event.stopPropagation();
            startOutcomeBot();
        });
        $("#outcome-stop-btn")?.addEventListener("click", () => openStopConfirm("outcome"));

        $("#confirm-stop-cancel")?.addEventListener("click", closeStopConfirm);
        $("#confirm-stop-accept")?.addEventListener("click", async () => {
            const scope = pendingStopScope;
            closeStopConfirm();
            if (scope === "grid") await stopGridBot();
            else if (scope === "outcome") await stopOutcomeBot();
        });
        $("#confirm-stop")?.addEventListener("cancel", () => { pendingStopScope = null; });
        $("#confirm-stop")?.addEventListener("click", (event) => {
            if (event.target === event.currentTarget) closeStopConfirm();
        });

        $$(".metrics-guide-btn").forEach((button) => button.addEventListener("click", openMetricsGuide));
        $("#metrics-guide-close")?.addEventListener("click", () => $("#metrics-guide")?.close());
        $("#metrics-guide")?.addEventListener("click", (event) => {
            if (event.target === event.currentTarget) event.currentTarget.close();
        });
        $("#grid-empty-cta")?.addEventListener("click", () => openTokenPicker());

        window.addEventListener("resize", updateTabIndicator);
        document.fonts?.ready.then(updateTabIndicator);
        window.setInterval(updateFreshness, 1000);
        window.setInterval(() => {
            if (appBooted && !document.hidden && (dashboardState.age("grid") > 2000 || dashboardState.age("outcome") > 2000)) {
                loadInitialStatus({ silent: true });
            }
        }, 3000);
        document.addEventListener("visibilitychange", () => {
            if (document.hidden) return;
            flushDashboardRenders();
            if (appBooted) { loadInitialStatus({ silent: true }); connectWebSocket(); }
        });
        $("#outcome-exec-mode")?.addEventListener("change", updateOutcomeExecutionMode);
        $$("input[name='outcome-side']").forEach((input) => input.addEventListener("change", () => setSelectedOutcomeMarket(selectedOutcomeMarket)));
        $("#sessions-reconcile-btn")?.addEventListener("click", async () => {
            setBusy("sessions-reconcile-btn", true);
            try {
                const state = await fetchJson(`${API_BASE}/api/sessions/reconcile`, { method: "POST" });
                await loadInitialStatus();
                showToast(state.can_start ? "success" : "error", "Stop verification", state.can_start ? "Orders and positions verified. Manual restart is possible." : "Stop not confirmed: " + state.unresolved.join(", "));
            } catch (error) { showToast("error", "Verification failed", error.message); }
            finally { setBusy("sessions-reconcile-btn", false); }
        });

        gridTokens = [selectedGridToken];
        renderDexChips();
        setSelectedGridToken(selectedGridToken);
        updateGridButtons();
        updateOutcomeButtons();
        requestAnimationFrame(updateTabIndicator);
        updateFreshness();

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
