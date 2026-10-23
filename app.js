// Échappe aussi les guillemets : sûr en texte comme en valeur d'attribut.
function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Valeur d'attribut HTML (équivalent d'escapeHtml, gardé pour les appels existants).
function _escAttr(str) {
  return String(str ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Auto-select all text on focus for number inputs
document.addEventListener("focus", (e) => {
  if (e.target && e.target.tagName === "INPUT" && e.target.type === "number") {
    e.target.select();
  }
}, true);

const GLITCH_SYMBOLS = "!@#$%^&*()_+-=[]{}|;:<>?/~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";

function glitchText(el) {
  const original = el.dataset.glitchOriginal || el.textContent;
  el.dataset.glitchOriginal = original;
  if (el._glitchActive) return;
  el._glitchActive = true;

  const chars = original.split("");
  const resolved = new Set();
  const order = chars.map((_, i) => i).sort(() => Math.random() - 0.5);
  let idx = 0;
  let delay = 170;

  const scrambleTimer = setInterval(() => {
    const out = chars.map((c, i) =>
      resolved.has(i) || c === " " ? c : GLITCH_SYMBOLS[Math.floor(Math.random() * GLITCH_SYMBOLS.length)]
    );
    el.textContent = out.join("");
  }, 32);

  function resolveNext() {
    if (idx >= order.length) {
      clearInterval(scrambleTimer);
      el.textContent = original;
      el._glitchActive = false;
      return;
    }
    resolved.add(order[idx++]);
    delay = Math.max(60, delay * 0.82);
    el._glitchResolveTimer = setTimeout(resolveNext, delay);
  }

  el._glitchScrambleTimer = scrambleTimer;
  resolveNext();
}

function glitchTextCancel(el) {
  if (el._glitchScrambleTimer) clearInterval(el._glitchScrambleTimer);
  if (el._glitchResolveTimer) clearTimeout(el._glitchResolveTimer);
  el._glitchActive = false;
  if (el.dataset.glitchOriginal) el.textContent = el.dataset.glitchOriginal;
}

function attachGlitchHover(el) {
  el.addEventListener("mouseenter", () => glitchText(el));
  el.addEventListener("mouseleave", () => glitchTextCancel(el));
}

function glitchTextFast(el) {
  const original = el.dataset.glitchOriginal || el.textContent;
  el.dataset.glitchOriginal = original;
  if (el._glitchActive) return;
  el._glitchActive = true;

  const chars = original.split("");
  const resolved = new Set();
  const order = chars.map((_, i) => i).sort(() => Math.random() - 0.5);
  let idx = 0;
  let delay = 40;

  const scrambleTimer = setInterval(() => {
    const out = chars.map((c, i) =>
      resolved.has(i) || c === " " ? c : GLITCH_SYMBOLS[Math.floor(Math.random() * GLITCH_SYMBOLS.length)]
    );
    el.textContent = out.join("");
  }, 30);

  function resolveNext() {
    if (idx >= order.length) {
      clearInterval(scrambleTimer);
      el.textContent = original;
      el._glitchActive = false;
      return;
    }
    resolved.add(order[idx++]);
    delay = Math.max(15, delay * 0.8);
    el._glitchResolveTimer = setTimeout(resolveNext, delay);
  }

  el._glitchScrambleTimer = scrambleTimer;
  resolveNext();
}

function attachGlitchHoverFast(el) {
  el.addEventListener("mouseenter", () => glitchTextFast(el));
  el.addEventListener("mouseleave", () => glitchTextCancel(el));
}

// Multi-address token storage (source of truth)
let walletAuthTokens = {};   // { normalizedAddress: tokenString }
let walletAuthExpires = {};  // { normalizedAddress: expiresAtMs }
// Backward-compat globals : always synced to the active DEX's address
let walletAuthToken = "";
let walletAuthAccount = "";
let walletAuthExpiresAtMs = 0;
let walletAuthCheckInFlight = false;
// Boot grace period: a 401 in the first few seconds (e.g. during init's
// parallel approval probes) should not nuke the freshly-restored session.
// We let the boot probes settle and only enforce 401-clears on subsequent
// user-driven calls.
const _AUTH_BOOT_GRACE_MS = 5000;
const _AUTH_BOOT_STARTED_AT = Date.now();
function _isAuthBootGrace() { return Date.now() - _AUTH_BOOT_STARTED_AT < _AUTH_BOOT_GRACE_MS; }
let sessionPnlTracker = {
  key: "",
  realized: 0,
  positions: {}, // coin -> { size, pnl }
};
let statsDrawerOpen = false;
let statsInFlight = false;
let lastStatsSeries = [];
let statsChartMeta = null;
const AUTH_TOKEN_STORAGE_KEY = "mega_wallet_auth_token";
const AUTH_ACCOUNT_STORAGE_KEY = "mega_wallet_auth_account";
const AUTH_EXPIRES_STORAGE_KEY = "mega_wallet_auth_expires";
const LOGO_VARIANT_STORAGE_KEY = "mega_logo_variant";
const STARFIELD_STORAGE_KEY = "mega_starfield_enabled_v2";
const TARGET_VOL_STORAGE_KEY = "mega_target_vol";
const ACTIVE_SESSION_TARGET_VOL_STORAGE_KEY = "mega_active_session_target_vol";
const EXCHANGE_MODE_STORAGE_KEY = "mega_exchange_mode";
const ADVANCED_MM_ENABLED_STORAGE_KEY = "mega_advanced_mm_enabled";
const ADVANCED_MM_CONFIG_STORAGE_KEY = "mega_advanced_mm_config_v1";
const DEX_STATE_STORAGE_KEY = "mega_dex_state_v1";
const ACTIVE_DEX_STORAGE_KEY = "mega_active_dex";
const SELECTED_DEXES_STORAGE_KEY = "mega_selected_dexes";

// ── Multi-DEX State ─────────────────────────────────────────────────────────
const MAX_SLOTS = 12;
// The last slots (MM_SLOTS..MAX_SLOTS-1) are reserved exclusively for HIP-4 :
// slot 6 = predictions, slots 7 to 11 = one outcomes session (taker or maker)
// per tab of the HIP-4 card. Regular MM/perp slots are 0..MM_SLOTS-1 (still 6).
const MM_SLOTS = 6;
// Primary slot per DEX : used for config sync, metrics, and first start
// RISEx prend le slot 3 : le 2 reste réservé à Nado. QFEX prend le 4.
const PRIMARY_DEX_SLOT = { hyperliquid: 0, extended: 1, nado: 2, risex: 3, qfex: 4 };
const DEX_MODE_MAP = { hyperliquid: "hyperliquid_mainnet", extended: "extended_mainnet", nado: "nado_mainnet", risex: "risex_mainnet", qfex: "qfex_mainnet" };
const MODE_TO_DEX = { hyperliquid_mainnet: "hyperliquid", hyperliquid_testnet: "hyperliquid", extended: "extended", extended_mainnet: "extended", nado: "nado", nado_mainnet: "nado", risex: "risex", risex_mainnet: "risex", qfex: "qfex", qfex_mainnet: "qfex" };
const DEX_LABELS = { hyperliquid: "Hyperliquid", extended: "Extended", nado: "Nado", risex: "RISEx", qfex: "QFEX" };
// Routes DELETE des credentials stockés par venue (« Remove key » du tiroir Wallets).
const REMOVE_KEY_ENDPOINTS = {
  hyperliquid: "/api/agent/key",
  extended: "/api/extended/credentials",
  qfex: "/api/qfex/credentials",
};


let _dexState = {
  hyperliquid: { connected: false, address: "", builderApproved: false, agentApproved: false },
  extended:    { connected: false, address: "", credentialsSaved: false, builderApproved: false },
  nado:        { connected: false, address: "", agentApproved: false },
  risex:       { connected: false, address: "", builderApproved: false, signerReady: false },
  qfex:        { connected: false, address: "", credentialsSaved: false },
};
let _activeDex = "hyperliquid";
let _selectedDexes = new Set(["hyperliquid"]);

function dexSlotNumber(dex) { return PRIMARY_DEX_SLOT[dex] ?? 0; }
function activeDexSlot() { return PRIMARY_DEX_SLOT[_activeDex] ?? 0; }
function _findFreeSlot() {
  const used = new Set(runningSlots.filter(s => s.running || s.degraded || s.standby).map(s => s.slot));
  for (let i = 0; i < MM_SLOTS; i++) { if (!used.has(i)) return i; }  // never the reserved HIP-4 slot
  return -1;
}
function activeDexMode() { return DEX_MODE_MAP[_activeDex] || "hyperliquid_mainnet"; }
function dexFromMode(mode) { return MODE_TO_DEX[String(mode).trim().toLowerCase()] || "hyperliquid"; }

function isDexConnected(dex) { return !!(_dexState[dex] && _dexState[dex].connected); }
function connectedDexes() { return Object.keys(_dexState).filter(d => _dexState[d].connected); }

function _tokenForAddress(addr) {
  if (!addr) return "";
  const norm = addr.toLowerCase();
  return walletAuthTokens[norm] || "";
}

function _tokenForDex(dex) {
  const addr = _dexState[dex]?.address || "";
  const tok = addr ? _tokenForAddress(addr) : "";
  if (tok) return tok;
  // Jamais de requête sans header quand un token existe : un 401 sur requête
  // nue (mismatch _dexState.address ↔ walletAuthTokens) wipait toute la session.
  return walletAuthToken || Object.values(walletAuthTokens)[0] || "";
}

function _syncPrimaryAuthGlobals() {
  // Sync the legacy globals to the active DEX's address
  const activeDexAddr = (_dexState[_activeDex]?.address || "").toLowerCase();
  if (activeDexAddr && walletAuthTokens[activeDexAddr]) {
    walletAuthToken = walletAuthTokens[activeDexAddr];
    walletAuthAccount = activeDexAddr;
    walletAuthExpiresAtMs = walletAuthExpires[activeDexAddr] || 0;
  } else {
    // Fallback: use first available token
    const firstAddr = Object.keys(walletAuthTokens)[0] || "";
    walletAuthToken = firstAddr ? walletAuthTokens[firstAddr] : "";
    walletAuthAccount = firstAddr;
    walletAuthExpiresAtMs = firstAddr ? (walletAuthExpires[firstAddr] || 0) : 0;
  }
}

function _activateAuthForAccount(account) {
  const norm = normalizeAccount(account || "");
  if (!norm) return false;
  const token = walletAuthTokens[norm] || "";
  const expires = Number(walletAuthExpires[norm] || 0);
  if (!token || expires <= Date.now()) return false;
  walletAuthToken = token;
  walletAuthAccount = norm;
  walletAuthExpiresAtMs = expires;
  return true;
}

function _storeTokenForAddress(addr, token, expiresAtMs) {
  const norm = addr.toLowerCase();
  walletAuthTokens[norm] = token;
  walletAuthExpires[norm] = expiresAtMs;
  _activateAuthForAccount(norm) || _syncPrimaryAuthGlobals();
  // Kick status polls immediately on auth success so the running-session badges
  // reconcile right away (otherwise the user waits up to STATUS_POLL_MS seeing
  // "no active session" while the bot is actually running on the server).
  setTimeout(() => {
    try { if (typeof refreshStatus === "function") refreshStatus(); } catch (_) {}
    try { if (window.HIP4 && typeof window.HIP4.pollNow === "function") window.HIP4.pollNow(); } catch (_) {}
  }, 0);
}

// Bouton « Exchanges » du header : nombre d'exchanges connectés sur ceux proposés.
const WALLETS_BTN_VENUES = ["hyperliquid", "extended", "risex", "qfex"];
function updateWalletsBtnCount() {
  const el = document.getElementById("header_wallets_count");
  if (!el) return;
  const n = WALLETS_BTN_VENUES.filter((d) => _dexState[d]?.connected).length;
  el.textContent = `${n}/${WALLETS_BTN_VENUES.length}`;
  el.classList.toggle("is-none", n === 0);
  const btn = document.getElementById("header_wallets_btn");
  if (btn) btn.setAttribute("aria-label", `Exchanges: ${n} of ${WALLETS_BTN_VENUES.length} connected`);
}

function persistDexState() {
  updateWalletsBtnCount();
  try {
    localStorage.setItem(DEX_STATE_STORAGE_KEY, JSON.stringify(_dexState));
    localStorage.setItem(ACTIVE_DEX_STORAGE_KEY, _activeDex);
    localStorage.setItem(SELECTED_DEXES_STORAGE_KEY, JSON.stringify([..._selectedDexes]));
  } catch (_) {}
}

function restoreDexState() {
  try {
    const raw = localStorage.getItem(DEX_STATE_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      for (const dex of Object.keys(_dexState)) {
        if (parsed[dex]) Object.assign(_dexState[dex], parsed[dex]);
      }
    }
    const ad = localStorage.getItem(ACTIVE_DEX_STORAGE_KEY);
    if (ad && PRIMARY_DEX_SLOT[ad] !== undefined) _activeDex = ad;
    const sd = localStorage.getItem(SELECTED_DEXES_STORAGE_KEY);
    if (sd) {
      const arr = JSON.parse(sd);
      if (Array.isArray(arr) && arr.length) _selectedDexes = new Set(arr.filter(d => PRIMARY_DEX_SLOT[d] !== undefined));
    }
  } catch (_) {}
  // Venues sélectionnables : Hyperliquid, Extended, RISEx et QFEX (Nado reste retiré).
  const allowed = new Set(["hyperliquid", "extended", "risex", "qfex"]);
  if (!allowed.has(_activeDex)) _activeDex = "hyperliquid";
  _selectedDexes = new Set([..._selectedDexes].filter(d => allowed.has(d)));
  if (_selectedDexes.size === 0) _selectedDexes = new Set(["hyperliquid"]);
  // Les deux call-sites de restoreDexState s'exécutent après restoreWalletAuthSession,
  // donc walletAuthTokens est peuplé : réconcilier ici couvre tous les chemins de boot.
  _reconcileDexAuthState();
}

// Au boot, _dexState (mega_dex_state_v1) et walletAuthTokens (mega_wallet_auth_token)
// sont deux entrées localStorage indépendantes qui peuvent diverger (token expiré
// d'un côté, adresse résiduelle de l'autre). Sans réconciliation, _tokenForDex
// part sur une adresse sans token → 401 → wipe de session au premier poll.
function _reconcileDexAuthState() {
  const tokenAddrs = Object.keys(walletAuthTokens);
  let changed = false;
  for (const dex of Object.keys(_dexState)) {
    const st = _dexState[dex];
    if (!st || !st.address) continue;
    const norm = String(st.address).toLowerCase();
    if (walletAuthTokens[norm]) continue;
    // Jamais de réattribution à un autre wallet (l'ancien code donnait l'unique token
    // restant à toutes les venues : une venue sur B passait sur A, avec les drapeaux
    // de B). Sans token, la venue est déconnectée ; l'utilisateur la reconnecte.
    st.connected = false;
    st.address = "";
    changed = true;
  }
  if (changed) persistDexState();
}

function resetDexState() {
  _dexState = {
    hyperliquid: { connected: false, address: "", builderApproved: false, agentApproved: false },
    extended:    { connected: false, address: "", credentialsSaved: false, builderApproved: false },
    nado:        { connected: false, address: "", agentApproved: false },
    risex:       { connected: false, address: "", builderApproved: false, signerReady: false },
    qfex:        { connected: false, address: "", credentialsSaved: false },
  };
  _activeDex = "hyperliquid";
  _selectedDexes = new Set(["hyperliquid"]);
  persistDexState();
}

// ── SPA Router ──────────────────────────────────────────────────────────────
// Pair Trading (dn) retiré du frontend : l'ancienne route /pairs retombe sur Market Making.
const SPA_ROUTES = { "/": "landing", "/market-making": "mm", "/pairs": "mm", "/hip4vol": "hip4vol", "/delta": "delta" };
const ROUTE_PATHS = { mm: "/market-making", outcomes: "/outcomes", hip4vol: "/hip4vol", delta: "/delta" };

function getCurrentView() {
  if (location.pathname === "/outcomes" || location.pathname.startsWith("/outcomes/")) return "outcomes";
  if (location.pathname === "/hip4vol") return "hip4vol";
  return SPA_ROUTES[location.pathname] || "landing";
}

function navigate(path, opts) {
  if (location.pathname === path) return;
  if (opts?.replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
  applyRoute();
}

function applyRoute() {
  // L'ancien mode "Predictions bot" est fusionné dans Outcomes.
  if (location.pathname === "/predictions" || location.pathname === "/earn") {
    history.replaceState(null, "", "/outcomes");
  }
  const view = getCurrentView();
  const body = document.body;
  const landingWrap = document.getElementById("landing_wrap");
  const connectPage = document.getElementById("connect_page");
  const mainPage = document.getElementById("main_page");
  const hip4Page = document.getElementById("hip4_page");
  const headerNav = document.getElementById("header_nav");
  const outcomesPage = document.getElementById("outcomes_page");
  const hip4volPage = document.getElementById("hip4vol_page");
  const deltaPage = document.getElementById("delta_page");

  // Outcome markets dedicated route (/outcomes[/<id>]) - section auto-contenue
  // (design HIP-4) avec son propre chrome ; on masque le header/mode Tradoor.
  if (view === "outcomes") {
    body.classList.remove("landing", "landing-space");
    body.classList.add("main-space");
    [landingWrap, connectPage, mainPage, hip4Page, hip4volPage, deltaPage].forEach((e) => e && e.classList.add("hidden"));
    if (hip4Page) hip4Page.setAttribute("aria-hidden", "true");
    if (hip4volPage) { hip4volPage.setAttribute("aria-hidden", "true"); _hip4volOnLeave(); }
    if (deltaPage) { deltaPage.setAttribute("aria-hidden", "true"); window.DELTA?.onLeave(); }
    // Le bot MM HIP-4 vit désormais dans Outcomes : polling status/balance actifs ici.
    if (window.HIP4MM) window.HIP4MM.onOutcomesEnter();
    // Cohérence : on garde le header Tradoor + le mode selector visibles (comme MM/Predictions).
    if (headerNav) headerNav.classList.remove("hidden");
    showModeSelector();
    body.classList.add("oc-mode");
    if (outcomesPage) { outcomesPage.classList.remove("hidden"); outcomesPage.setAttribute("aria-hidden", "false"); }
    document.querySelectorAll(".mode-sel-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.mode === "outcomes");
    });
    requestAnimationFrame(() => _positionModeBrackets(true));
    const ocId = location.pathname.replace(/^\/outcomes\/?/, "");
    if (window.OUTCOMES) window.OUTCOMES.onEnter(ocId || "");
    const sNavBtn = document.getElementById("stats_nav_btn");
    if (sNavBtn) sNavBtn.classList.remove("hidden");
    updateHeaderWalletDisplay();
    return;
  } else if (outcomesPage) {
    body.classList.remove("oc-mode");
    outcomesPage.classList.add("hidden");
    outcomesPage.setAttribute("aria-hidden", "true");
    if (window.OUTCOMES && typeof window.OUTCOMES.onLeave === "function") window.OUTCOMES.onLeave();
    if (window.HIP4MM) window.HIP4MM.onOutcomesLeave();
  }

  // HIP-4 Volume Bot (/hip4vol)
  if (view === "hip4vol") {
    body.classList.remove("landing", "landing-space");
    body.classList.add("main-space");
    [landingWrap, connectPage, mainPage, hip4Page, deltaPage].forEach((e) => e && e.classList.add("hidden"));
    if (hip4Page) hip4Page.setAttribute("aria-hidden", "true");
    if (deltaPage) { deltaPage.setAttribute("aria-hidden", "true"); window.DELTA?.onLeave(); }
    if (headerNav) headerNav.classList.remove("hidden");
    showModeSelector();
    if (hip4volPage) { hip4volPage.classList.remove("hidden"); hip4volPage.setAttribute("aria-hidden", "false"); }
    document.querySelectorAll(".mode-sel-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.mode === "hip4vol");
    });
    requestAnimationFrame(() => _positionModeBrackets(true));
    const sNavBtn = document.getElementById("stats_nav_btn");
    if (sNavBtn) sNavBtn.classList.remove("hidden");
    _hip4volOnEnter();
    updateHeaderWalletDisplay();
    return;
  } else if (hip4volPage) {
    hip4volPage.classList.add("hidden");
    hip4volPage.setAttribute("aria-hidden", "true");
    _hip4volOnLeave();
  }

  // Delta (/delta) : long sur une plateforme, short sur l'autre, reverse sur minuterie.
  if (view === "delta") {
    body.classList.remove("landing", "landing-space");
    body.classList.add("main-space");
    [landingWrap, connectPage, mainPage, hip4Page].forEach((e) => e && e.classList.add("hidden"));
    if (hip4Page) hip4Page.setAttribute("aria-hidden", "true");
    if (headerNav) headerNav.classList.remove("hidden");
    showModeSelector();
    if (deltaPage) { deltaPage.classList.remove("hidden"); deltaPage.setAttribute("aria-hidden", "false"); }
    document.querySelectorAll(".mode-sel-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.mode === "delta");
    });
    requestAnimationFrame(() => _positionModeBrackets(true));
    const sNavBtn = document.getElementById("stats_nav_btn");
    if (sNavBtn) sNavBtn.classList.remove("hidden");
    window.DELTA?.onEnter();
    updateHeaderWalletDisplay();
    return;
  } else if (deltaPage) {
    deltaPage.classList.add("hidden");
    deltaPage.setAttribute("aria-hidden", "true");
    window.DELTA?.onLeave();
  }

  // #hip4_page ne sert plus que d'hôte à la vue session MM (ouverte depuis Outcomes).
  if (hip4Page) { hip4Page.classList.add("hidden"); hip4Page.setAttribute("aria-hidden", "true"); }

  if (view === "landing") {
    body.classList.add("landing", "landing-space");
    body.classList.remove("main-space");
    if (landingWrap) landingWrap.classList.remove("hidden");
    if (connectPage) connectPage.classList.remove("hidden");
    if (mainPage) mainPage.classList.add("hidden");
    // Cleanup when arriving at landing
    clearError("connect_error");
    clearError("main_error");
    setTitleHyperliquidOs(false);
    setSignatureStepsVisible(false);
    setStatsDrawerOpen(false);
    const sNavBtn = document.getElementById("stats_nav_btn");
    if (sNavBtn) sNavBtn.classList.add("hidden");
    // Close any open dropdown
    document.querySelectorAll(".header-dropdown.open").forEach((dd) => dd.classList.remove("open"));
    document.querySelectorAll(".header-nav-btn.active, .header-icon-btn.active, .header-wallet-btn.active").forEach((b) => b.classList.remove("active"));
    applyLogoVariant(body.getAttribute("data-logo-variant") || "v4", { persist: false });
  } else {
    body.classList.remove("landing", "landing-space");
    body.classList.add("main-space");
    if (landingWrap) landingWrap.classList.add("hidden");
    if (connectPage) connectPage.classList.add("hidden");
    // Switch layout via setActiveBotType
    if (_activeBotType !== view) {
      setActiveBotType(view);
    } else {
      if (mainPage) mainPage.classList.toggle("hidden", view !== "mm");
    }
    const sNavBtn = document.getElementById("stats_nav_btn");
    if (sNavBtn) sNavBtn.classList.remove("hidden");
    const gc = document.getElementById("galaxy-bg-canvas");
    if (gc) { gc.style.opacity = ""; gc.style.filter = ""; }
  }

  // Header + mode selector always visible
  if (headerNav) headerNav.classList.remove("hidden");
  showModeSelector();

  // Sync mode selector active state with route (on landing, highlight _activeBotType)
  const activePath = location.pathname;
  document.querySelectorAll(".mode-sel-btn").forEach((btn) => {
    const isActive = view === "landing"
      ? btn.dataset.mode === _activeBotType
      : activePath === ROUTE_PATHS[btn.dataset.mode];
    btn.classList.toggle("active", isActive);
  });
  requestAnimationFrame(() => _positionModeBrackets(false));

  updateHeaderWalletDisplay();
}

function updateHeaderWalletDisplay() {
  const connected = Object.keys(walletAuthTokens).length > 0 || !!walletAuthToken;
  const connectBtn = document.getElementById("header_connect_btn");
  const walletBtn = document.getElementById("header_wallet_btn");
  const walletsBtn = document.getElementById("header_wallets_btn");
  const addrSpan = document.getElementById("header_wallet_addr");
  if (connected) {
    if (connectBtn) connectBtn.classList.add("hidden");
    if (walletsBtn) walletsBtn.classList.remove("hidden");
    updateWalletsBtnCount();
    if (walletBtn) {
      walletBtn.classList.remove("hidden");
      // Show the active DEX's address
      const addr = _dexState[_activeDex]?.address || walletAuthAccount || "";
      if (addrSpan) addrSpan.textContent = addr ? `${addr.slice(0, 6)}...${addr.slice(-4)}` : "";
    }
  } else {
    if (connectBtn) connectBtn.classList.remove("hidden");
    if (walletBtn) walletBtn.classList.add("hidden");
    if (walletsBtn) walletsBtn.classList.add("hidden");
    setAvailableNotionalConnectHint();
  }
}

function activateAfterConnect() {
  updateHeaderWalletDisplay();
  const mode = document.getElementById("mode")?.value || "";
  setTitleHyperliquidOs(mode === "hyperliquid_mainnet");
  updateMarketSourceForMode();
  updateDexSelectorUI();
  clearError("connect_error");
  clearError("main_error");
  latestAvailableNotional = null;
  // Arm the grace window so the first metrics fetch after connect can keep the
  // spinner instead of flashing "0.00" while the (sometimes slow) xyz/km
  // clearinghouse query resolves.
  lastSourceChangedAt = Date.now();
  if (!applyCachedMetricsForCurrentSelection()) {
    setNotionalLoading(document.getElementById("available_notional"));
  }
  renderRunningSlots();
  if (walletAuthToken) refreshHlSubaccounts().catch(() => {});
  // La venue choisie au Connect (ex. RISEx) peut différer de la liste chargée au
  // démarrage (HL) : recharger, sinon le sélecteur garde « BTC » d'Hyperliquid.
  ensureMarketsForCurrentMode().catch(() => {});
  refreshMetrics();
  if (getCurrentView() === "outcomes" && window.HIP4 && typeof window.HIP4.refreshUsdcBalance === "function") {
    window.HIP4.refreshUsdcBalance();
  }
  setTimeout(_maybeTelegramAutoPopup, 600);
}

window.addEventListener("popstate", () => applyRoute());

// Config du fond WebGL (galaxy-bg). Extrait pour pouvoir (ré)initialiser à la
// demande au lieu de créer le contexte une fois pour toutes.
const GALAXY_BG_CONFIG = {
  hueShift:          140,
  density:           1.0,
  glowIntensity:     0.3,
  twinkleIntensity:  0.3,
  starSpeed:         0.15,
  speed:             0.5,
  rotationSpeed:     0.01,
  mouseRepulsion:    true,
  repulsionStrength: 0.9,
  saturation:        0.0,
};
let _galaxyBgDestroy = null;
// Alloue le contexte WebGL galaxy-bg UNIQUEMENT quand les étoiles sont actives.
// Avant, initGalaxyBg() était appelé au boot pour tout le monde : le contexte
// WebGL restait alloué même en "starfield-off" (le shader skippait juste le draw),
// gaspillant de la mémoire GPU et alourdissant le compositing de la page — ce qui
// aggrave le repaint (écran blanc 2-3s) quand un onglet d'arrière-plan revient.
function _ensureGalaxyBg() {
  if (_galaxyBgDestroy) return;                      // déjà actif
  if (typeof initGalaxyBg !== "function") return;
  try {
    _galaxyBgDestroy = initGalaxyBg(GALAXY_BG_CONFIG) || null;
  } catch (_) {
    _galaxyBgDestroy = null;
  }
}
function _teardownGalaxyBg() {
  if (!_galaxyBgDestroy) return;
  try { _galaxyBgDestroy(); } catch (_) {}           // cancelRAF + WEBGL_lose_context
  _galaxyBgDestroy = null;
}

function setStarfieldEnabled(enabled) {
  const on = !!enabled;
  document.body.classList.toggle("starfield-off", !on);
  // Allouer / libérer le contexte WebGL à la demande (au lieu de seulement
  // masquer le rendu) : off = pas de contexte GPU du tout.
  if (on) _ensureGalaxyBg(); else _teardownGalaxyBg();
  try {
    localStorage.setItem(STARFIELD_STORAGE_KEY, on ? "1" : "0");
  } catch (_) {
    // ignore storage failures
  }
}

function getInitialStarfieldEnabled() {
  // Le toggle UI a été retiré : sans off-switch, honorer un ancien localStorage
  // "1" laisserait le canvas WebGL allumé à vie pour ces utilisateurs.
  // Feature désactivée pour tous.
  return false;
}

function initVisualsOnce() {
  if (visualsInited) return;
  visualsInited = true;
  // N'alloue le contexte WebGL galaxy-bg que si les étoiles sont réellement
  // activées (défaut OFF) — sinon aucun contexte GPU n'est créé.
  if (getInitialStarfieldEnabled()) _ensureGalaxyBg();
  initCursorSun();
  initClickSpark();
  initTitleParallax();
  // ── Sky dimming + reddening on scroll (works on all pages) ──
  const _skyOv = document.getElementById("sky_dim_overlay");
  if (_skyOv) {
    let _skyRaf = false;
    function _updateSkyDim() {
      _skyRaf = false;
      const scrollY = window.scrollY || 0;
      const vh = window.innerHeight || 800;
      const isLanding = document.body.classList.contains("landing");
      const fadeEnd = isLanding ? vh * 1.2 : vh * 0.35;
      const t = Math.min(1, scrollY / fadeEnd);
      _skyOv.style.opacity = t.toFixed(3);
    }
    window.addEventListener("scroll", () => { if (!_skyRaf) { _skyRaf = true; requestAnimationFrame(_updateSkyDim); } }, { passive: true });
    _updateSkyDim();
  }
}

// Onglet en arrière-plan : geler l'activité visuelle pour réduire la pression
// GPU/mémoire qui pousse le navigateur à purger la texture de la page (→ écran
// blanc 2-3s au retour). `body.tab-hidden` met les animations infinies en pause
// (CSS) et on libère le contexte WebGL galaxy-bg s'il tournait ; on restaure à
// l'affichage. Le rAF est déjà gelé nativement en arrière-plan, mais libérer le
// contexte WebGL réduit l'empreinte mémoire pendant que l'onglet dort.
document.addEventListener("visibilitychange", () => {
  const hidden = document.hidden;
  document.body.classList.toggle("tab-hidden", hidden);
  if (hidden) {
    _teardownGalaxyBg();
  } else if (!document.body.classList.contains("starfield-off")) {
    _ensureGalaxyBg();
  }
});

const BUILDER_ADDRESS = "0x92345453cE2000642d7D4ceeae4FcCC6c2E41d23".toLowerCase();
const BUILDER_FEE_TENTHS_BPS = 10;
const BUILDER_FEE_BPS = BUILDER_FEE_TENTHS_BPS / 10;
const BUILDER_FEE_PER_MILLION_USD = (BUILDER_FEE_TENTHS_BPS / 100000.0) * 1000000.0;
const EXTENDED_BUILDER_ADDRESS = "0x145498F409F3952bCb9ab14Be8fF02e4F590becc".toLowerCase();
const EXTENDED_BUILDER_ID = 5004; // id du compte Extended qui perçoit les builder fees
const EXTENDED_BUILDER_FEE_TENTHS_BPS = 10;
const EXTENDED_BUILDER_FEE_BPS = EXTENDED_BUILDER_FEE_TENTHS_BPS / 10;
const EXTENDED_BUILDER_FEE_PER_MILLION_USD = (EXTENDED_BUILDER_FEE_TENTHS_BPS / 100000.0) * 1000000.0;
// RISEx : builder `tradoor` (id 62). Le fee est imposé par le serveur au démarrage
// (jamais écrit dans le TOML) ; base 1 bp, avant remises.
const RISEX_BUILDER_ID = 62;
const RISEX_BUILDER_FEE_BPS = 1;
// Chaîne RISE (4153) : le wallet doit y être pour signer les données typées RISEx.
const RISE_CHAIN_ID_HEX = "0x1039";
const RISE_CHAIN_PARAMS = {
  chainId: RISE_CHAIN_ID_HEX,
  chainName: "RISE",
  rpcUrls: ["https://rpc.risechain.com"],
  blockExplorerUrls: ["https://explorer.risechain.com"],
  nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
};
const RISEX_DEPOSIT_URL = "https://rise.trade";
// signer/submit : transaction relayée puis attente de l'API RISEx (jusqu'à ~2 min).
const RISEX_SUBMIT_TIMEOUT_MS = 180000;
const RISEX_STATUS_CACHE_TTL_MS = 60000;
const RISEX_QUOTA_WARN_THRESHOLD = 1000;
const PLATFORM_FEE_RATE = 0.0001; // 1 bps = 100 USD / 1M
const AGENT_NAME = "TRADOR";
const HL_SIGNATURE_CHAIN_ID_HEX = "0x66eee";
const HL_SIGNATURE_CHAIN = {
  chainId: "0x66eee",
  chainName: "Arbitrum Sepolia",
  nativeCurrency: { name: "ETH", symbol: "ETH", decimals: 18 },
  rpcUrls: ["https://sepolia-rollup.arbitrum.io/rpc"],
  blockExplorerUrls: ["https://sepolia.arbiscan.io"],
};
const NADO_CHAIN_ID_HEX = "0xdef1"; // 57073 = Ink L2 mainnet
const NADO_CHAIN = {
  chainId: NADO_CHAIN_ID_HEX,
  chainName: "Ink",
  nativeCurrency: { name: "ETH", symbol: "ETH", decimals: 18 },
  rpcUrls: ["https://rpc-gel.inkonchain.com"],
  blockExplorerUrls: ["https://explorer.inkonchain.com"],
};

function authHeaders(extra = {}, targetDex = "") {
  const headers = { ...extra };
  const token = targetDex ? _tokenForDex(targetDex) : walletAuthToken;
  if (token) {
    headers["X-Mega-Auth"] = token;
  }
  return headers;
}

const API_TIMEOUT_MS = 12000;
const API_STATUS_TIMEOUT_MS = 7000;

async function fetchWithTimeout(path, options = {}, timeoutMs = API_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), Math.max(1000, Number(timeoutMs) || API_TIMEOUT_MS));
  try {
    const opts = { ...options, signal: controller.signal };
    return await fetch(path, opts);
  } catch (err) {
    if (err && err.name === "AbortError") {
      const toErr = new Error(`Request timeout after ${Math.round((Number(timeoutMs) || API_TIMEOUT_MS) / 1000)}s`);
      toErr.status = 408;
      throw toErr;
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
  }
}

// Un 401 isolé ne prouve pas que la session est invalide : il peut venir d'une
// requête partie sans header (mismatch _dexState ↔ tokens), d'une course au boot
// ou d'un redémarrage serveur transitoire. Avant de wiper le token (et donc de
// déconnecter l'utilisateur - qui perd la vue de son bot running), on confirme
// auprès du serveur avec LE BON couple token+account. Clear uniquement sur un
// authenticated:false explicite (HTTP 200).
let _authVerifyInFlight = false;
let _lastAuthVerifyTs = 0;
async function _confirmAuthInvalidThenClear(account) {
  const norm = normalizeAccount(account || "") || walletAuthAccount;
  const token = _tokenForAddress(norm) || walletAuthToken;
  if (!norm || !token) return;            // rien à vérifier → ne rien wiper
  if (_authVerifyInFlight || Date.now() - _lastAuthVerifyTs < 10000) return;
  _authVerifyInFlight = true;
  _lastAuthVerifyTs = Date.now();
  try {
    // fetch direct (PAS apiGet : éviter la récursion du handler 401)
    const res = await fetchWithTimeout(
      `/api/auth/status?account_address=${encodeURIComponent(norm)}`,
      { headers: { "X-Mega-Auth": token } }, API_STATUS_TIMEOUT_MS);
    if (!res.ok) return;                  // 5xx/timeout → on garde le token
    const st = await res.json();
    if (st && st.authenticated === false) {
      clearWalletAuthForAccount(norm);
      setWalletAuthStatus("1/3 Signature expired. Sign again.", "warn");
      showError("connect_error", "Wallet session expired. Click Connect & Sign Wallet.");
      showError("main_error", "Wallet session expired. Reconnect and sign your wallet.");
      updateWalletAuthButtonState();
    }
  } catch (_) {
    // transient → keep token
  } finally {
    _authVerifyInFlight = false;
  }
}

// Routes et slots propres à Hyperliquid (HIP-4, Outcomes, sous-comptes HL) : sans
// venue explicite, ils partaient avec le token de la venue ACTIVE. Avec RISEx sur un
// autre wallet, une session HIP-4 démarrait ou se lisait sur le mauvais compte.
function _inferDexForPath(path) {
  const p = String(path || "");
  if (/^\/api\/(hip4|hip4vol|hip4mm|outcomes|hl\/)/.test(p)) return "hyperliquid";
  const m = p.match(/[?&]slot=(\d+)/);
  if (m && Number(m[1]) >= MM_SLOTS) return "hyperliquid";
  return "";
}

async function apiGet(path, opts = {}) {
  const res = await fetchWithTimeout(path, { headers: authHeaders({}, opts.dex || _inferDexForPath(path)) }, API_TIMEOUT_MS);
  if (!res.ok) {
    const raw = await res.text();
    let msg = raw;
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && parsed.detail) {
        // Un 422 FastAPI renvoie detail = [{loc, msg, ...}] : ne jamais passer
        // un objet à new Error (affiche "[object Object]" dans le modal).
        msg = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
      }
    } catch (_) {
      // keep raw text
    }
    if (res.status === 401) {
      // During the boot grace window the auto-connect routine fires several
      // approval probes in parallel; one of them 401-ing on a stale-but-still-
      // valid token shouldn't wipe the whole session. Outside the grace window,
      // confirm with the server before clearing anything (fire-and-forget).
      if (!_isAuthBootGrace()) {
        _confirmAuthInvalidThenClear(_extractAccountFromRequest(path, null));
      } else {
        console.warn("[auth] 401 during boot grace, keeping session:", path);
      }
    }
    const err = new Error(msg || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

async function statsApiGet(path) {
  const res = await fetchWithTimeout(path, { headers: authHeaders() }, API_TIMEOUT_MS);
  if (!res.ok) {
    const raw = await res.text();
    let msg = raw;
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && parsed.detail) {
        // Un 422 FastAPI renvoie detail = [{loc, msg, ...}] : ne jamais passer
        // un objet à new Error (affiche "[object Object]" dans le modal).
        msg = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
      }
    } catch (_) {
      // keep raw
    }
    const err = new Error(msg || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

async function apiPost(path, body, opts = {}) {
  const timeoutMs = Number(opts.timeoutMs) > 0 ? Number(opts.timeoutMs) : API_TIMEOUT_MS;
  const res = await fetchWithTimeout(path, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }, opts.dex || _inferDexForPath(path)),
    body: JSON.stringify(body || {}),
  }, timeoutMs);
  if (!res.ok) {
    const raw = await res.text();
    let msg = raw;
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && parsed.detail) {
        // Un 422 FastAPI renvoie detail = [{loc, msg, ...}] : ne jamais passer
        // un objet à new Error (affiche "[object Object]" dans le modal).
        msg = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
      }
    } catch (_) {
      // keep raw text
    }
    if (res.status === 401) {
      if (!_isAuthBootGrace()) {
        // Confirmation serveur avant tout clear (voir _confirmAuthInvalidThenClear).
        _confirmAuthInvalidThenClear(_extractAccountFromRequest(path, body));
      } else {
        console.warn("[auth] 401 during boot grace, keeping session:", path);
      }
    }
    const err = new Error(msg || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

let latestAvailableNotional = null;
let lastSourceChangedAt = 0;
let currentSessionVolumeUsd = 0;
let activeSessionTargetVolumeUsd = null;
let lastUiClickToneAt = 0;
let metricsInFlight = false;
let statusInFlight = false;
let stopRequestInFlight = false;
let showStoppedStatusAfterManualStop = false;
let liveConfigSyncTimer = null;
let hasStoredApiKeyForAccount = false;
let encryptedStoreEnabled = false;
let builderApprovedForAccount = false;
let approvedBuilderFeeTenthsBps = BUILDER_FEE_TENTHS_BPS;
let approvedExtBuilderFeeTenthsBps = EXTENDED_BUILDER_FEE_TENTHS_BPS;
let agentApprovedForAccount = false;
let nadoAgentApprovedForAccount = false;
let nadoAgentStatusCache = { key: "", ts: 0, value: null };
let builderStatusCache = { key: "", ts: 0, value: null };
let agentStatusCache = { key: "", ts: 0, value: null };
let nftHolderStatus = false;
let setupAllInFlight = false;
let setupAllLockedUntilMs = 0;
let hyperspaceTransitionInFlight = false;
let visualsInited = false;
const APPROVAL_STATUS_CACHE_TTL_MS = 15000;
const EXTENDED_CREDENTIALS_CACHE_TTL_MS = 15000;
const MARKET_CACHE_TTL_MS = 60000;
const METRICS_CACHE_TTL_MS = 20000;
const marketCache = new Map();
const metricsCache = new Map();
let marketLeverageMap = new Map();
// Venue|source de la dernière liste de marchés chargée dans #market_symbol.
let _marketsLoadedKey = "";
const lastSelectedSymbolBySource = new Map();
const HYPERSPACE_POST_LOAD_HOLD_MS = 1000;
const HS_PRE_MS = 170;
const HS_WARP_MS = 700;
const HS_RELEASE_MS = 460;
let extendedCredentialsReadyForAccount = false;
let extendedCredentialsStatusCache = { key: "", ts: 0, value: null };
// RISEx : état lu sur GET /api/risex/status (builder approuvé chez RISEx, signer du bot).
let risexStatusCache = { key: "", ts: 0, value: null };
let risexBuilderApprovedForAccount = false;
let risexSignerReadyForAccount = false;
let risexHasCollateral = null;
// QFEX : clé API collée, vérifiée côté serveur (GET /api/qfex/status).
let qfexStatusCache = { key: "", ts: 0, value: null };
let qfexCredentialsReadyForAccount = false;
let connectStepActionInFlight = false;
const HS_MAX_ZOOM = 1.14;
const HS_FINAL_PUNCH_ZOOM = 1.24;
const HS_STREAK_SPEED_BASE = 0.35;
const HS_STREAK_SPEED_MAX = 5.2;
const HS_FLASH_PEAK = 0.96;
const HS_VIGNETTE_MAX = 0.9;
const FAST_XYZ_SYMBOLS = [
  "xyz:SPCX",
  "xyz:SP500",
  "xyz:XYZ100",
  "xyz:AAPL", "xyz:TSLA", "xyz:NVDA", "xyz:MSFT", "xyz:GOLD",
  "xyz:AMZN", "xyz:GOOGL", "xyz:META", "xyz:MSTR", "xyz:SILVER",
  "xyz:EUR", "xyz:USDJPY", "xyz:CL", "xyz:NATGAS", "xyz:PLTR",
  "xyz:COIN", "xyz:HOOD", "xyz:AMD", "xyz:INTC", "xyz:GME",
  "xyz:NFLX", "xyz:BABA", "xyz:TSM", "xyz:MU", "xyz:ORCL",
  "xyz:LLY", "xyz:COST", "xyz:DXY", "xyz:JP225", "xyz:COPPER",
  "xyz:URANIUM", "xyz:URNM", "xyz:USAR", "xyz:RIVN",
  "xyz:ALUMINIUM", "xyz:PLATINUM", "xyz:PALLADIUM", "xyz:CRCL", "xyz:CRWV",
  "xyz:SKHX", "xyz:SMSN", "xyz:SNDK", "xyz:SOFTBANK", "xyz:HYUNDAI",
  "xyz:EWJ", "xyz:EWY", "xyz:KIOXIA", "xyz:KR200",
  "xyz:BRENTOIL", "xyz:HIMS", "xyz:DKNG", "xyz:LITE", "xyz:XLE",
  "xyz:BX", "xyz:PURRDAT", "xyz:MRVL", "xyz:RKLB", "xyz:BIRD",
  "xyz:DRAM", "xyz:CBRS", "xyz:EWZ", "xyz:ZM", "xyz:EBAY",
  "xyz:ARM", "xyz:EWT", "xyz:GBP", "xyz:BB",
];
const FAST_XYZ_LEVERAGE = {
  "xyz:AAPL": 10.0, "xyz:TSLA": 10.0, "xyz:NVDA": 10.0, "xyz:MSFT": 10.0, "xyz:GOLD": 20.0,
  "xyz:AMZN": 10.0, "xyz:GOOGL": 10.0, "xyz:META": 10.0, "xyz:MSTR": 10.0, "xyz:SILVER": 20.0,
  "xyz:EUR": 50.0, "xyz:USDJPY": 50.0, "xyz:CL": 20.0, "xyz:NATGAS": 10.0, "xyz:PLTR": 10.0,
  "xyz:COIN": 10.0, "xyz:HOOD": 10.0, "xyz:AMD": 10.0, "xyz:INTC": 10.0, "xyz:GME": 10.0,
  "xyz:NFLX": 10.0, "xyz:BABA": 10.0, "xyz:TSM": 10.0, "xyz:MU": 10.0, "xyz:ORCL": 10.0,
  "xyz:LLY": 10.0, "xyz:COST": 20.0, "xyz:DXY": 20.0, "xyz:JP225": 20.0, "xyz:COPPER": 20.0,
  "xyz:XYZ100": 30.0, "xyz:URANIUM": 10.0, "xyz:URNM": 10.0, "xyz:USAR": 10.0, "xyz:RIVN": 10.0,
  "xyz:ALUMINIUM": 20.0, "xyz:PLATINUM": 20.0, "xyz:PALLADIUM": 20.0, "xyz:CRCL": 10.0, "xyz:CRWV": 10.0,
  "xyz:SKHX": 10.0, "xyz:SMSN": 10.0, "xyz:SNDK": 10.0, "xyz:SOFTBANK": 10.0, "xyz:HYUNDAI": 10.0,
  "xyz:EWJ": 20.0, "xyz:EWY": 20.0, "xyz:KIOXIA": 10.0, "xyz:KR200": 20.0,
  "xyz:SP500": 50.0,
  "xyz:SPCX": 5.0,
  "xyz:BRENTOIL": 20.0, "xyz:HIMS": 10.0, "xyz:DKNG": 10.0, "xyz:LITE": 10.0, "xyz:XLE": 20.0,
  "xyz:BX": 10.0, "xyz:PURRDAT": 10.0, "xyz:MRVL": 10.0, "xyz:RKLB": 10.0, "xyz:BIRD": 10.0,
  "xyz:DRAM": 20.0, "xyz:CBRS": 10.0, "xyz:EWZ": 20.0, "xyz:ZM": 10.0, "xyz:EBAY": 10.0,
  "xyz:ARM": 10.0, "xyz:EWT": 20.0, "xyz:GBP": 50.0, "xyz:BB": 10.0,
};
const FAST_TOP20_SYMBOLS = [
  "BTC", "ETH", "SOL", "HYPE", "BNB", "XRP", "DOGE", "AVAX",
  "SUI", "LINK", "TRX", "TRUMP", "ADA", "PEPE", "TON",
  "WIF", "SEI", "APT", "INJ", "NEAR", "LIT",
];
const FAST_TOP20_LEVERAGE = Object.fromEntries(FAST_TOP20_SYMBOLS.map((s) => [s, 25.0]));
FAST_TOP20_LEVERAGE["LIT"] = 5.0;
const FAST_EXTENDED_SYMBOLS = [
  // Marchés non-RFQ uniquement (les marchés RFQ n'ont pas de carnet → pas de MM).
  // Top 20 crypto par volume + tous les RWA à carnet (2026-08-05).
  "BTC-USD", "HYPE-USD", "LIT-USD", "ETH-USD", "XMR-USD",
  "NEAR-USD", "SOL-USD", "PUMP-USD", "XLM-USD", "FARTCOIN-USD",
  "BNB-USD", "CRV-USD", "XRP-USD", "DOT-USD", "ZEC-USD",
  "ADA-USD", "AAVE-USD", "ENA-USD", "DOGE-USD", "TAO-USD",
  "SNDK_24_5-USD", "MU_24_5-USD", "XAU-USD", "XPT-USD", "XCU-USD",
  "XBR-USD", "MSTR_24_5-USD", "SPX500m-USD", "XAG-USD", "WTI-USD",
  "EUR-USD", "TECH100m-USD", "USDJPY-USD", "XNG-USD", "NVDA_24_5-USD",
];
const FAST_EXTENDED_LEVERAGE = {
  "BTC-USD": 50.0, "HYPE-USD": 50.0, "LIT-USD": 25.0, "ETH-USD": 50.0, "XMR-USD": 25.0,
  "NEAR-USD": 25.0, "SOL-USD": 50.0, "PUMP-USD": 50.0, "XLM-USD": 10.0, "FARTCOIN-USD": 25.0,
  "BNB-USD": 50.0, "CRV-USD": 25.0, "XRP-USD": 50.0, "DOT-USD": 25.0, "ZEC-USD": 10.0,
  "ADA-USD": 50.0, "AAVE-USD": 50.0, "ENA-USD": 50.0, "DOGE-USD": 50.0, "TAO-USD": 25.0,
  "SNDK_24_5-USD": 10.0, "MU_24_5-USD": 10.0, "XAU-USD": 25.0, "XPT-USD": 10.0, "XCU-USD": 20.0,
  "XBR-USD": 10.0, "MSTR_24_5-USD": 10.0, "SPX500m-USD": 25.0, "XAG-USD": 10.0, "WTI-USD": 10.0,
  "EUR-USD": 100.0, "TECH100m-USD": 16.66, "USDJPY-USD": 25.0, "XNG-USD": 5.0, "NVDA_24_5-USD": 10.0,
};
const FAST_EXTENDED_TRADFI = [
  "SNDK_24_5-USD", "MU_24_5-USD", "XAU-USD", "XPT-USD", "XCU-USD",
  "XBR-USD", "MSTR_24_5-USD", "SPX500m-USD", "XAG-USD", "WTI-USD",
  "EUR-USD", "TECH100m-USD", "USDJPY-USD", "XNG-USD", "NVDA_24_5-USD",
];
const FAST_NADO_SYMBOLS = [
  "XAG-PERP", "WTI-PERP",
  "BTC-PERP", "ETH-PERP", "SOL-PERP", "HYPE-PERP", "TAO-PERP",
  "XPL-PERP", "ZEC-PERP", "BNB-PERP", "FARTCOIN-PERP", "LIT-PERP",
  "XRP-PERP", "ENA-PERP", "XAUT-PERP", "PUMP-PERP", "MON-PERP",
  "WLFI-PERP", "ASTER-PERP", "AAVE-PERP", "SUI-PERP", "ONDO-PERP",
];
const FAST_NADO_LEVERAGE = {
  "XAG-PERP": 10, "WTI-PERP": 10,
  "BTC-PERP": 40, "ETH-PERP": 40, "SOL-PERP": 20, "HYPE-PERP": 10,
  "TAO-PERP": 10, "XPL-PERP": 10, "ZEC-PERP": 10, "BNB-PERP": 20,
  "FARTCOIN-PERP": 10, "LIT-PERP": 10, "XRP-PERP": 20, "ENA-PERP": 10,
  "XAUT-PERP": 10, "PUMP-PERP": 10, "MON-PERP": 10, "WLFI-PERP": 5,
  "ASTER-PERP": 5, "AAVE-PERP": 10, "SUI-PERP": 10, "ONDO-PERP": 10,
};
// RISEx : affichage instantané avant la réponse de /api/markets (même liste que le
// repli serveur RISEX_FALLBACK_SYMBOLS), groupes Crypto / TradFi.
const FAST_RISEX_SYMBOLS = [
  "BTC/USDC", "ETH/USDC", "SOL/USDC", "HYPE/USDC", "BNB/USDC", "XRP/USDC", "DOGE/USDC",
  "XAU/USDC", "XAG/USDC", "CL/USDC", "QQQ/USDC", "SPY/USDC", "TSLA/USDC", "NVDA/USDC",
];
const FAST_RISEX_TRADFI = ["XAU/USDC", "XAG/USDC", "CL/USDC", "QQQ/USDC", "SPY/USDC", "TSLA/USDC", "NVDA/USDC"];
const FAST_RISEX_LEVERAGE = Object.fromEntries(
  FAST_RISEX_SYMBOLS.map((s) => [s, s === "BTC/USDC" || s === "ETH/USDC" ? 25.0 : 10.0]),
);
// QFEX : liste instantanée (même liste que QFEX_FALLBACK_SYMBOLS côté serveur), mélange
// de produits à 0 bp maker (indices, grandes actions) et de produits payants (métaux, énergie).
const FAST_QFEX_SYMBOLS = [
  "US500-USD", "US100-USD", "NVDA-USD", "TSLA-USD", "AAPL-USD", "MSFT-USD", "META-USD",
  "AMZN-USD", "GOOGL-USD", "GOLD-USD", "SILVER-USD", "CL-USD", "NATGAS-USD",
];
const FAST_QFEX_LEVERAGE = Object.fromEntries(FAST_QFEX_SYMBOLS.map((s) => [
  s, (s === "US500-USD" || s === "US100-USD") ? 30.0 : (["GOLD-USD", "SILVER-USD", "NATGAS-USD"].includes(s) ? 20.0 : 10.0),
]));
const NADO_BOOSTED_SYMBOLS = new Set(["XAG-PERP", "WTI-PERP"]);
const NADO_OI_CAP_SYMBOLS = new Set(["XAG-PERP", "WTI-PERP"]);
const FAST_KINETIQ_SYMBOLS = [
  "mkts:US500",
  "mkts:USTECH",
];
// EntropyIO (dex HIP-3 "io") : marchés actifs par volume 24 h (délistés exclus), tous en
// marge isolée uniquement, levier max par marché (meta dex=io, 2026-10-20). Même rôle que
// FAST_XYZ_SYMBOLS : affichés tout de suite, remplacés par la réponse de /api/markets.
const FAST_IO_LEVERAGE = {
  "io:SNDK": 15.0, "io:ANTH": 6.0, "io:NBIS": 15.0, "io:PRL": 3.0, "io:DRAM": 25.0, "io:OAI": 6.0,
  "io:EWY": 25.0, "io:IONQ": 10.0, "io:TCNT": 10.0, "io:XIAOMI": 10.0, "io:GPRO": 5.0,
};
const FAST_IO_SYMBOLS = Object.keys(FAST_IO_LEVERAGE);
// Dex HIP-3 de Hyperliquid : table unique pour app.js et delta.js (source du sélecteur ->
// dex on-chain, libellé, logo). À garder alignée sur le registre serveur (hl_dexs.py),
// voir tests/test_hip3_sources_js.py. `delta` = identifiant de source de /api/delta/markets ;
// `fast` / `fastLeverage` = liste instantanée de loadMarkets (Kinetiq a sa propre branche).
const HIP3_SOURCES = {
  hip3: { dex: "mkts", label: "Kinetiq", logo: "markets.svg", delta: "kinetiq" },
  xyz: { dex: "xyz", label: "Trade.xyz", logo: "tradexyz.svg", delta: "xyz", fast: FAST_XYZ_SYMBOLS, fastLeverage: FAST_XYZ_LEVERAGE },
  io: { dex: "io", label: "EntropyIO", logo: "entropyio.svg", delta: "io", fast: FAST_IO_SYMBOLS, fastLeverage: FAST_IO_LEVERAGE },
};
// Dex HIP-3 d'un symbole préfixé ("io:SNDK" -> "io"), "" pour un symbole sans dex HIP-3
// ("BTC", "ext:..."). "km:" reste l'ancien alias de Kinetiq, gardé tel quel.
function hip3DexOfSymbol(symbol) {
  const m = /^([a-z0-9]+):/i.exec(String(symbol || "").trim());
  if (!m) return "";
  const prefix = m[1].toLowerCase();
  if (prefix === "km") return "km";
  return Object.values(HIP3_SOURCES).some((e) => e.dex === prefix) ? prefix : "";
}
// Marchés en marge isolée uniquement (only_isolated de /api/markets, cumulé sur les
// sources : les symboles HIP-3 sont préfixés, donc uniques). Tous les marchés io le sont.
const marketOnlyIsolated = new Map();
function isOnlyIsolatedMarket(symbol) {
  const sym = String(symbol || "");
  if (marketOnlyIsolated.has(sym)) return !!marketOnlyIsolated.get(sym);
  return hip3DexOfSymbol(sym) === "io";
}
const DEFAULT_MM = {
  spreadMinBps: 0.3,
  spreadMaxBps: 2.0,
  minOrderFloorUsd: 20,
  minOrderRatio: 0.01,
  maxOrderRatio: 0.03,
  defaultLeverage: 25,
};
const RISK_MODE_PRESETS = {
  aggressive: {
    profile: "aggressive_volume",
    targetVol: 300000,
    flashClass: "flash-aggressive",
    maxPosPct: 0.50,
    orderMinPct: 0.03,
    orderMaxPct: 0.20,
    maxLossPct: 0.50,
  },
  classic: {
    profile: "",
    targetVol: 200000,
    flashClass: "flash-classic",
    maxPosPct: 0.40,
    orderMinPct: 0.025,
    orderMaxPct: 0.15,
    maxLossPct: 0.40,
  },
  conservative: {
    profile: "conservative_pnl",
    targetVol: 100000,
    flashClass: "flash-conservative",
    maxPosPct: 0.30,
    orderMinPct: 0.02,
    orderMaxPct: 0.10,
    maxLossPct: 0.30,
  },
};
const ADVANCED_MM_PROFILE_PRESETS = {
  aggressive: { spreadMin: 0.2, spreadMax: 1.4, posMin: 80, posMax: 220 },
  classic: { spreadMin: DEFAULT_MM.spreadMinBps, spreadMax: DEFAULT_MM.spreadMaxBps, posMin: 20, posMax: 100 },
  conservative: { spreadMin: 0.8, spreadMax: 3.2, posMin: 25, posMax: 90 },
};
let selectedRiskMode = "conservative";
let selectedDirectionBias = "neutral"; // "long" | "neutral" | "short"
let selectedQuoteMode = "sniper"; // "sniper" | "grid" | "fortress"
// true dès que l'utilisateur choisit un mode manuellement (clic). Empêche le
// polling de statut (refreshStatus) d'écraser sa sélection non sauvegardée avec
// le quote_mode de la session en cours. Remis à false aux chargements de config
// (silent), qui reflètent légitimement le backend.
let quoteModeUserTouched = false;
let riskModeSwitchLockedUntil = 0;
let advancedMmEnabled = false;
const CLICK_SOUND_SELECTOR = "button, .btn, .help-dot, .tab-btn, select, input[type='checkbox'], input[type='radio'], input[type='range']";

// ── Multi-slot state ──────────────────────────────────────
// runningSlots: populated from API status polling, NOT persisted
// [{slot, symbol, running, degraded, standby, sessionPnl, elapsedText, schedProgress}]
let runningSlots = [];
let _currentSchedule = null; // {enabled, mode, start_time, stop_time, timezone_offset_min}

function _getRunningSlot(symbol) {
  return runningSlots.find(s => s.symbol === symbol);
}
function anySlotRunning() { return runningSlots.some(s => s.running || s.degraded || s.standby); }
function _activeDexSlotRunning() { return runningSlots.some(s => s.dex === _activeDex && (s.running || s.degraded || s.standby)); }
function _currentSymbol() { return normalizeSelectedSymbol(document.getElementById("market_symbol")?.value || ""); }
function _isCurrentTokenRunning() { const sym = _currentSymbol(); return !!runningSlots.find(s => (s.running || s.degraded) && s.symbol === sym); }
function _isCurrentTokenStandby() { const sym = _currentSymbol(); return !!runningSlots.find(s => s.standby && !s.running && s.symbol === sym); }
let _globalTargetStopTriggered = false;

async function _cancelStandby() {
  if (!_currentSchedule) return;
  await _saveSchedule({ ..._currentSchedule, standby: false });
  runningSlots = runningSlots.filter(s => !s.standby);
  renderRunningSlots();
  _syncMainActionBtn();
}

async function _stopAllSlots() {
  try {
    // Also cancel any standby slots
    if (runningSlots.some(s => s.standby)) {
      try { await _cancelStandby(); } catch (_) {}
    }
    await apiPost("/api/stop?slot=all");
    refreshPnlHistory();
    // Stop is async on server : let status polling detect when bots have stopped
  } catch (err) {
    console.error("[GLOBAL TARGET] stop all failed:", err);
  }
}

function _openSlotTargetEditor(anchorEl, slotId, minVol, currentTarget) {
  // Remove any existing editor
  document.querySelectorAll(".slot-vol-editor").forEach(el => {
    const hidden = el.previousElementSibling;
    if (hidden && hidden.classList.contains("slot-vol-row")) hidden.style.display = "";
    el.remove();
  });
  const volRow = anchorEl.closest(".slot-vol-row");
  if (!volRow) return;
  const editor = document.createElement("span");
  editor.className = "slot-vol-editor";
  const minVal = Math.max(1, minVol);
  editor.innerHTML = `
    <input type="number" class="slot-vol-input" min="${minVal}" step="1000" value="${currentTarget || Math.max(minVal, 25000)}" />
    <button type="button" class="slot-vol-save" title="Save">✓</button>
    <button type="button" class="slot-vol-cancel" title="Cancel">✕</button>
  `;
  volRow.style.display = "none";
  volRow.parentNode.insertBefore(editor, volRow.nextSibling);
  const input = editor.querySelector(".slot-vol-input");
  input.focus();
  input.select();
  function closeEditor() { volRow.style.display = ""; editor.remove(); }

  async function save() {
    const val = parseFloat(input.value);
    if (!Number.isFinite(val) || val < minVal) {
      input.classList.add("input-error");
      input.title = `Minimum: ${formatCompactVolume(minVal)}`;
      return;
    }
    editor.querySelector(".slot-vol-save").disabled = true;
    try {
      await apiPost(`/api/config/target_volume?slot=${slotId}`, { target_session_volume_usd: val });
      const rs = runningSlots.find(s => s.slot === slotId);
      if (rs) rs.targetVolume = val;
      closeEditor();
      renderRunningSlots();
    } catch (err) {
      input.classList.add("input-error");
      editor.querySelector(".slot-vol-save").disabled = false;
    }
  }
  editor.querySelector(".slot-vol-save").addEventListener("click", save);
  editor.querySelector(".slot-vol-cancel").addEventListener("click", closeEditor);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); save(); }
    if (e.key === "Escape") { e.preventDefault(); closeEditor(); }
  });
  // Prevent click from bubbling to card
  editor.addEventListener("click", e => e.stopPropagation());
}

// ---------------------------------------------------------------------------
// Schedule (auto-start / auto-stop) : UI helpers
// ---------------------------------------------------------------------------

async function _saveSchedule(data) {
  try {
    data.timezone_offset_min = new Date().getTimezoneOffset();
    // Le serveur lie le schedule au slot + mode de la config au moment du save
    // (consent guard : l'auto-start refuse si le mode a changé depuis).
    if (!Number.isInteger(data.slot)) data.slot = _mmAutosaveTargetSlot();
    await apiPost("/api/schedule", data);
    _currentSchedule = data;
    renderRunningSlots();
  } catch (err) {
    showError("main_error", `Schedule save failed: ${err.message || err}`);
  }
}

function _openScheduleEditor(anchorEl, slotId) {
  // Remove any existing schedule editor
  document.querySelectorAll(".slot-sched-editor").forEach(el => el.remove());
  const card = anchorEl.closest(".slot-card");
  if (!card) return;
  const sched = _currentSchedule || {};
  const editor = document.createElement("div");
  editor.className = "slot-sched-editor";
  editor.innerHTML = `
    <div class="sched-row">
      <label class="sched-label">Start</label>
      <input type="time" class="sched-time-input" data-field="start" value="${sched.start_time || ""}" />
      <label class="sched-label">Stop</label>
      <input type="time" class="sched-time-input" data-field="stop" value="${sched.stop_time || ""}" />
    </div>
    <div class="sched-row sched-row-bottom">
      <label class="sched-mode-label">
        <input type="checkbox" class="sched-mode-check" ${sched.mode === "oneshot" ? "checked" : ""} />
        One-shot
      </label>
      <button type="button" class="sched-save-btn" title="Enable schedule">✓</button>
      <button type="button" class="sched-cancel-btn" title="Cancel">✕</button>
    </div>
  `;
  // Insert after the slot-info block
  const info = card.querySelector(".slot-info");
  if (info) info.appendChild(editor);
  else card.appendChild(editor);

  editor.addEventListener("click", e => e.stopPropagation());

  editor.querySelector(".sched-save-btn").addEventListener("click", async () => {
    const startVal = editor.querySelector('[data-field="start"]').value || "";
    const stopVal = editor.querySelector('[data-field="stop"]').value || "";
    if (!startVal && !stopVal) return;
    const isOneshot = editor.querySelector(".sched-mode-check").checked;
    await _saveSchedule({
      enabled: true,
      mode: isOneshot ? "oneshot" : "recurring",
      start_time: startVal,
      stop_time: stopVal,
      slot: Number.isInteger(slotId) ? slotId : undefined,
    });
    editor.remove();
  });
  editor.querySelector(".sched-cancel-btn").addEventListener("click", () => editor.remove());
}

const _CLOCK_SVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`;

function _renderScheduleBadge(sched) {
  if (!sched || !sched.enabled) return "";
  const parts = [];
  if (sched.start_time) parts.push(sched.start_time);
  if (sched.stop_time) parts.push(sched.stop_time);
  const timeStr = parts.join(" → ");
  const modeStr = sched.mode === "oneshot" ? "once" : "daily";
  return `<span class="slot-sched-badge" title="Schedule: ${timeStr} (${modeStr})">${_CLOCK_SVG} ${timeStr}</span>`;
}

/** Toggle between Target Volume and Schedule panels in the config area. */
let _scheduleMode = false;
let _scheduleUiInitDone = false;
let _schedHintUntil = 0; // hint transitoire (applyOptimalConfig) préservé du reset de _syncScheduleUI
function _setStopMode(isSchedule, { skipServer } = {}) {
  _scheduleMode = isSchedule;
  const volPanel = document.getElementById("stop_mode_vol");
  const schedPanel = document.getElementById("stop_mode_sched");
  const btnVol = document.getElementById("stop_mode_btn_vol");
  const btnSched = document.getElementById("stop_mode_btn_sched");
  if (volPanel) volPanel.classList.toggle("panel-hidden", isSchedule);
  if (schedPanel) schedPanel.classList.toggle("panel-hidden", !isSchedule);
  if (btnVol) btnVol.classList.toggle("active", !isSchedule);
  if (btnSched) btnSched.classList.toggle("active", isSchedule);
  // Switching to Target Volume disables the schedule + clears standby (single server call)
  if (!isSchedule && !skipServer && _currentSchedule && _currentSchedule.enabled) {
    const sched = _currentSchedule;
    runningSlots = runningSlots.filter(s => !s.standby);
    renderRunningSlots();
    _saveSchedule({ enabled: false, mode: sched.mode || "recurring",
      start_time: sched.start_time || "", stop_time: sched.stop_time || "",
      standby: false }).then(() => _syncMainActionBtn()).catch(() => {});
  }
}

function _syncScheduleUI() {
  const sched = _currentSchedule || {};
  const startEl = document.getElementById("sched_start_time");
  const stopEl = document.getElementById("sched_stop_time");
  const recurToggle = document.getElementById("sched_recurring_toggle");
  const statusEl = document.getElementById("sched_status_text");
  if (!_scheduleMode) {
    if (startEl) startEl.value = sched.start_time || "";
    if (stopEl) stopEl.value = sched.stop_time || "";
  }
  // Don't override toggle while user is editing (schedule panel visible)
  if (recurToggle && !_scheduleMode) {
    const isRecur = sched.enabled && sched.mode === "recurring";
    recurToggle.classList.toggle("active", isRecur);
    recurToggle.title = isRecur ? "Recurring: ON \u2014 runs every day. Click to switch to one-shot." : "Recurring: OFF \u2014 runs once then stops. Click to repeat daily.";
  }
  if (statusEl) {
    if (sched.enabled) {
      const parts = [];
      if (sched.start_time) parts.push(sched.start_time);
      if (sched.stop_time) parts.push(sched.stop_time);
      statusEl.textContent = `Active: ${parts.join(" \u2192 ")} (${sched.mode === "recurring" ? "daily" : "once"})`;
      statusEl.className = "sched-status-text sched-active";
    } else if (_schedHintUntil <= Date.now()) {
      statusEl.textContent = "";
      statusEl.className = "sched-status-text";
    }
  }
  // Auto-switch to schedule panel once on first load (not on every poll)
  if (sched.enabled && !_scheduleMode && !_scheduleUiInitDone) {
    _scheduleUiInitDone = true;
    _setStopMode(true, { skipServer: true });
  }
}

async function _saveScheduleFromConfig() {
  const startEl = document.getElementById("sched_start_time");
  const stopEl = document.getElementById("sched_stop_time");
  const recurToggle = document.getElementById("sched_recurring_toggle");
  const startVal = startEl?.value || "";
  const stopVal = stopEl?.value || "";
  if (!startVal && !stopVal) {
    showError("main_error", "Set at least a start or stop time.");
    return;
  }
  const isRecurring = recurToggle?.classList.contains("active");
  await _saveSchedule({
    enabled: true,
    mode: isRecurring ? "recurring" : "oneshot",
    start_time: startVal,
    stop_time: stopVal,
  });
  _syncScheduleUI();
}

function formatUsd(v) {
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (!Number.isFinite(n)) return "-";
  const abs = Math.abs(n);
  const decimals = abs > 0 && abs < 0.01 ? 4 : 2;
  return `${n >= 0 ? "+" : ""}${n.toFixed(decimals)} $`;
}

function formatUsdPlain(v) {
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (!Number.isFinite(n)) return "-";
  const abs = Math.abs(n);
  const decimals = abs > 0 && abs < 0.01 ? 4 : 2;
  return `${n.toFixed(decimals)} $`;
}

function formatCompactVolume(v) {
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (!Number.isFinite(n)) return "-";
  const a = Math.abs(n);
  if (a >= 1000000) return `${(n / 1000000).toFixed(1).replace(/\\.0$/, "")}M`;
  if (a >= 1000) return `${(n / 1000).toFixed(1).replace(/\\.0$/, "")}k`;
  return `${n.toFixed(2).replace(/\\.00$/, "")}`;
}

function isExtendedMode(modeValue = "") {
  const live = modeValue || document.getElementById("mode")?.value || "";
  const mode = String(live).trim().toLowerCase();
  return mode === "extended" || mode === "extended_mainnet";
}

function isNadoMode(modeValue = "") {
  const live = modeValue || document.getElementById("mode")?.value || "";
  const mode = String(live).trim().toLowerCase();
  return mode === "nado" || mode === "nado_mainnet";
}

function isRisexMode(modeValue = "") {
  const live = modeValue || document.getElementById("mode")?.value || "";
  const mode = String(live).trim().toLowerCase();
  return mode === "risex" || mode === "risex_mainnet";
}

function isQfexMode(modeValue = "") {
  const live = modeValue || document.getElementById("mode")?.value || "";
  const mode = String(live).trim().toLowerCase();
  return mode === "qfex" || mode === "qfex_mainnet";
}

function activeFeeVenueLabel(modeValue = "") {
  if (isExtendedMode(modeValue)) return "Extended";
  if (isRisexMode(modeValue)) return "RISEx";
  if (isQfexMode(modeValue)) return "QFEX";
  if (isNadoMode(modeValue)) return "Nado";
  const source = document.getElementById("market_source")?.value || "top20";
  return HIP3_SOURCES[source]?.label || "Hyperliquid";
}

function updatePnlPerMillionLabel(modeValue = "") {
  const el = document.getElementById("stat_pnl_per_million_label");
  if (!el) return;
  el.textContent = `PnL per $100k Volume`;
}

function platformFeesUsdFromVolume(volumeUsd) {
  const v = typeof volumeUsd === "string" ? parseFloat(volumeUsd) : Number(volumeUsd);
  if (!Number.isFinite(v) || v <= 0) return 0;
  return v * PLATFORM_FEE_RATE;
}

function resetSessionPnlTracker(sessionKey = "") {
  sessionPnlTracker = { key: sessionKey, realized: 0, positions: {} };
}

function trackedSessionPnl(session, positions) {
  const startedAt = String(session?.started_at || "");
  const mode = String(session?.mode || "");
  const symbol = String(session?.symbol || "");
  const key = `${mode}|${symbol}|${startedAt}`;
  if (!session?.active || !startedAt) {
    resetSessionPnlTracker("");
    return { total: 0, realized: 0, unrealized: 0 };
  }
  if (sessionPnlTracker.key !== key) {
    resetSessionPnlTracker(key);
  }

  const prev = sessionPnlTracker.positions || {};
  const curr = {};
  let unrealized = 0;
  (positions || []).forEach((p) => {
    const coin = String(p?.coin || "");
    const size = typeof p?.szi === "string" ? parseFloat(p.szi) : Number(p?.szi);
    const pnl = typeof p?.pnl === "string" ? parseFloat(p.pnl) : Number(p?.pnl);
    if (!coin || !Number.isFinite(size) || size === 0) return;
    curr[coin] = { size, pnl: Number.isFinite(pnl) ? pnl : 0 };
    unrealized += Number.isFinite(pnl) ? pnl : 0;
  });

  // Realize PnL when positions are reduced/closed/flipped.
  Object.entries(prev).forEach(([coin, oldPos]) => {
    const oldSize = Number(oldPos?.size || 0);
    const oldPnl = Number(oldPos?.pnl || 0);
    const now = curr[coin];
    if (!now) {
      sessionPnlTracker.realized += oldPnl;
      return;
    }
    const newSize = Number(now.size || 0);
    if (oldSize === 0) return;
    const sameSide = Math.sign(oldSize) === Math.sign(newSize);
    if (!sameSide) {
      sessionPnlTracker.realized += oldPnl;
      return;
    }
    if (Math.abs(newSize) < Math.abs(oldSize)) {
      const reduced = (Math.abs(oldSize) - Math.abs(newSize)) / Math.abs(oldSize);
      sessionPnlTracker.realized += oldPnl * reduced;
    }
  });

  sessionPnlTracker.positions = curr;
  return {
    total: sessionPnlTracker.realized + unrealized,
    realized: sessionPnlTracker.realized,
    unrealized,
  };
}

function persistRiskInputs() {
  try {
    const raw = document.getElementById("target_vol")?.value ?? "";
    const v = _parseFormattedNum(raw);
    if (v > 0) {
      localStorage.setItem(TARGET_VOL_STORAGE_KEY, String(v));
    }
  } catch (_) {
    // ignore storage failures
  }
}

function restoreRiskInputsFromStorage() {
  try {
    const targetVolSaved = localStorage.getItem(TARGET_VOL_STORAGE_KEY);
    const targetVolInput = document.getElementById("target_vol");
    if (targetVolInput && targetVolSaved !== null && String(targetVolSaved).trim() !== "") {
      targetVolInput.value = String(targetVolSaved);
    }
  } catch (_) {
    // ignore storage failures
  }
  syncRangeSlidersFromInputs();
}

/** Keep range sliders in sync after any programmatic input value change. */
function syncRangeSlidersFromInputs() {
  const volNum = document.getElementById("target_vol");
  const volRange = document.getElementById("target_vol_range");
  const lossNum = document.getElementById("max_loss");
  const lossRange = document.getElementById("max_loss_range");
  if (volRange && volNum) {
    const v = _parseFormattedNum(volNum.value);
    if (v > 0) {
      volRange.value = Math.min(Math.max(v, 10000), 5000000);
      volNum.value = _fmtNum(v);
    }
    _syncRangeTrack(volRange);
  }
  if (lossRange && lossNum) {
    const v = _parseFormattedNum(lossNum.value);
    if (v > 0) {
      lossRange.value = Math.min(Math.max(v, 5), 100);
      lossNum.value = _fmtNum(v);
    }
    _syncRangeTrack(lossRange);
  }
}
/** Parse a possibly formatted number string (e.g. "3,030,000" or "3030000") */
function _parseFormattedNum(s) {
  return parseFloat(String(s).replace(/,/g, "")) || 0;
}
/** Format number with comma thousands separator */
function _fmtNum(v) {
  const n = Math.round(Number(v) || 0);
  return n.toLocaleString("en-US");
}

function _syncRangeTrack(slider) {
  const min = parseFloat(slider.min), max = parseFloat(slider.max);
  const pct = ((parseFloat(slider.value) - min) / (max - min)) * 100;
  // Gradient: orange at low → cyan at high
  const t = pct / 100;
  const r = Math.round(255 * (1 - t) + 53 * t);
  const g = Math.round(140 * (1 - t) + 240 * t);
  const b = Math.round(60 * (1 - t) + 194 * t);
  const end = `rgb(${r},${g},${b})`;
  slider.style.background = `linear-gradient(90deg, rgb(255,140,60) 0%, ${end} ${pct}%, rgba(255,255,255,0.06) ${pct}%)`;
}

function setAdvancedMmError(message = "") {
  const err = document.getElementById("advanced_mm_error");
  if (!err) return;
  const msg = String(message || "").trim();
  err.textContent = msg;
  err.classList.toggle("hidden", !msg);
}

function setRiskModeLocked(locked) {
  const on = !!locked;
  const selector = document.getElementById("risk_mode_selector");
  const buttons = ["mode_aggressive", "mode_classic", "mode_conservative"]
    .map((id) => document.getElementById(id))
    .filter((el) => el instanceof HTMLButtonElement);
  if (selector) {
    selector.classList.toggle("is-locked", on);
    selector.setAttribute("aria-disabled", on ? "true" : "false");
  }
  buttons.forEach((btn) => {
    btn.disabled = on;
    btn.setAttribute("aria-disabled", on ? "true" : "false");
  });
}

function setAdvancedMmUiEnabled(enabled) {
  const on = !!enabled;
  advancedMmEnabled = on;
  const checkbox = document.getElementById("advanced_mm_enabled");
  const presets = document.getElementById("preset_sections");
  const advSection = document.getElementById("advanced_mm_section");
  const fields = document.getElementById("advanced_mm_fields");
  if (checkbox instanceof HTMLInputElement) {
    checkbox.checked = on;
    checkbox.setAttribute("aria-expanded", on ? "true" : "false");
  }
  if (presets) presets.classList.toggle("hidden", on);
  const biasWrap = document.querySelector(".direction-bias-wrap");
  if (biasWrap) biasWrap.classList.toggle("hidden", on);
  if (advSection) advSection.classList.toggle("hidden", !on);
  const advBiasSelect = document.getElementById("advanced_direction_bias");
  if (advBiasSelect && on) advBiasSelect.value = selectedDirectionBias;
  if (fields) {
    fields.setAttribute("aria-disabled", on ? "false" : "true");
    const nodes = fields.querySelectorAll("input, select");
    nodes.forEach((node) => {
      if (node instanceof HTMLInputElement || node instanceof HTMLSelectElement) {
        node.disabled = !on;
      }
    });
  }
  setRiskModeLocked(on);
  // Always render tabs when section becomes visible : even if fetch hasn't returned yet
  if (on && typeof renderAdvancedStrategies === "function") renderAdvancedStrategies();
}

function dynamicClassicPositionRange() {
  let sessionNotional = parseFloat(document.getElementById("session_notional")?.value || "0");
  if (!Number.isFinite(sessionNotional) || sessionNotional <= 0) sessionNotional = 100;
  const leverage = currentSelectedLeverage();
  const sizing = computeDynamicSizing(sessionNotional, leverage, "classic");
  return { posMin: sizing.orderMin, posMax: sizing.orderMax };
}

function getEffectiveProfileValues(mode = selectedRiskMode) {
  const key = mode === "aggressive" ? "aggressive" : mode === "classic" ? "classic" : "conservative";
  const base = ADVANCED_MM_PROFILE_PRESETS[key];
  if (!base) return { spreadMin: 0.3, spreadMax: 2.0, posMin: 20, posMax: 100 };
  let sessionNotional = parseFloat(document.getElementById("session_notional")?.value || "0");
  if (!Number.isFinite(sessionNotional) || sessionNotional <= 0) sessionNotional = 100;
  const sizing = computeDynamicSizing(sessionNotional, currentSelectedLeverage(), key);
  return { ...base, posMin: sizing.orderMin, posMax: sizing.orderMax };
}

function prefillAdvancedMmFromCurrentProfile() {
  const values = getEffectiveProfileValues(selectedRiskMode);
  const spreadMinInput = document.getElementById("advanced_spread_min_bps");
  const spreadMaxInput = document.getElementById("advanced_spread_max_bps");
  const posMinInput = document.getElementById("advanced_pos_min_usd");
  const posMaxInput = document.getElementById("advanced_pos_max_usd");
  if (spreadMinInput) spreadMinInput.value = String(Number(values.spreadMin).toFixed(2));
  if (spreadMaxInput) spreadMaxInput.value = String(Number(values.spreadMax).toFixed(2));
  if (posMinInput) posMinInput.value = String(Math.round(Number(values.posMin) * 100) / 100);
  if (posMaxInput) posMaxInput.value = String(Math.round(Number(values.posMax) * 100) / 100);
}

function readAdvancedMmConfigFromUi() {
  const spreadMin = parseFloat(document.getElementById("advanced_spread_min_bps")?.value || "0");
  const spreadMax = parseFloat(document.getElementById("advanced_spread_max_bps")?.value || "0");
  const posMin = parseFloat(document.getElementById("advanced_pos_min_usd")?.value || "0");
  const posMax = parseFloat(document.getElementById("advanced_pos_max_usd")?.value || "0");
  const speedRaw = String(document.getElementById("advanced_engine_speed")?.value || "normal").toLowerCase();
  const engineSpeed = speedRaw === "fast" ? "fast" : "normal";
  const biasRaw = String(document.getElementById("advanced_direction_bias")?.value || "neutral").toLowerCase();
  const directionBias = biasRaw === "long" ? "long" : biasRaw === "short" ? "short" : "neutral";
  return { spreadMin, spreadMax, posMin, posMax, engineSpeed, directionBias, quoteMode: selectedQuoteMode };
}

function validateAdvancedMmConfig(config) {
  const cfg = config || readAdvancedMmConfigFromUi();
  if (!(cfg.spreadMin > 0)) return "Spread min must be greater than 0.";
  if (!(cfg.spreadMax > 0)) return "Spread max must be greater than 0.";
  if (cfg.spreadMin > cfg.spreadMax) return "Spread min cannot be greater than spread max.";
  if (!(cfg.spreadMin >= 0.05 && cfg.spreadMax <= 50)) return "Spread values must stay between 0.05 and 50 bps.";
  if (!(cfg.posMin > 0)) return "Position size min must be greater than 0.";
  if (!(cfg.posMax > 0)) return "Position size max must be greater than 0.";
  if (cfg.posMin > cfg.posMax) return "Position size min cannot be greater than position size max.";
  if (!(cfg.posMin >= 12 && cfg.posMax <= 1000000)) return "Position size must stay between 12 and 1,000,000 USD (Hyperliquid rejects orders under $10 notional).";
  return "";
}

function persistAdvancedMmSettings() {
  try {
    localStorage.setItem(ADVANCED_MM_ENABLED_STORAGE_KEY, advancedMmEnabled ? "1" : "0");
    localStorage.setItem(ADVANCED_MM_CONFIG_STORAGE_KEY, JSON.stringify(readAdvancedMmConfigFromUi()));
  } catch (_) {
    // ignore storage failures
  }
}

function restoreAdvancedMmSettings() {
  const speedEl = document.getElementById("advanced_engine_speed");
  if (speedEl instanceof HTMLSelectElement) speedEl.value = "normal";
  try {
    const raw = localStorage.getItem(ADVANCED_MM_CONFIG_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        const spreadMinInput = document.getElementById("advanced_spread_min_bps");
        const spreadMaxInput = document.getElementById("advanced_spread_max_bps");
        const posMinInput = document.getElementById("advanced_pos_min_usd");
        const posMaxInput = document.getElementById("advanced_pos_max_usd");
        if (spreadMinInput && Number.isFinite(Number(parsed.spreadMin))) spreadMinInput.value = String(parsed.spreadMin);
        if (spreadMaxInput && Number.isFinite(Number(parsed.spreadMax))) spreadMaxInput.value = String(parsed.spreadMax);
        if (posMinInput && Number.isFinite(Number(parsed.posMin))) posMinInput.value = String(parsed.posMin);
        if (posMaxInput && Number.isFinite(Number(parsed.posMax))) posMaxInput.value = String(parsed.posMax);
        if (speedEl instanceof HTMLSelectElement && typeof parsed.engineSpeed === "string") {
          speedEl.value = parsed.engineSpeed === "fast" ? "fast" : "normal";
        }
        const biasEl = document.getElementById("advanced_direction_bias");
        if (biasEl instanceof HTMLSelectElement && typeof parsed.directionBias === "string") {
          const b = parsed.directionBias === "long" ? "long" : parsed.directionBias === "short" ? "short" : "neutral";
          biasEl.value = b;
          applyDirectionBias(b, { silent: true });
        }
        if (typeof parsed.quoteMode === "string") {
          applyQuoteMode(parsed.quoteMode, { silent: true });
        }
      }
    }
  } catch (_) {
    // ignore storage failures
  }
  let enabled = false;
  try {
    enabled = localStorage.getItem(ADVANCED_MM_ENABLED_STORAGE_KEY) === "1";
  } catch (_) {
    enabled = false;
  }
  if (enabled) {
    // Keep values realistic in case session notional/leverage changed since previous run.
    const err = validateAdvancedMmConfig(readAdvancedMmConfigFromUi());
    if (err) {
      enabled = false;
      prefillAdvancedMmFromCurrentProfile();
    }
  }
  setAdvancedMmUiEnabled(enabled);
  setAdvancedMmError("");
  renderAdvancedStrategies();
}

// ---------------------------------------------------------------------------
// Advanced settings strategies : 3 slots per wallet, tab-based editor
// ---------------------------------------------------------------------------

const ADVANCED_STRATEGY_COUNT = 3;
const ADVANCED_SELECTED_STRATEGY_KEY = "mega_advanced_selected_strategy_idx";
const ADVANCED_STRATEGY_MIGRATED_KEY = "mega_advanced_strategy_migrated_v1";

let advancedStrategiesState = null;       // {slots: [...]} fetched from server
let advancedStrategiesLoaded = false;
let selectedStrategyIdx = -1;             // index of the strategy currently being edited
let _advancedStrategyAutoSaveTimer = null;
let _advancedStrategyAutoSaveInFlight = false;
let _advancedStrategyPendingSave = false;
let _advancedStrategyLastFrozenState = null;

function _readSelectedStrategyIdxFromStorage() {
  try {
    const v = parseInt(localStorage.getItem(ADVANCED_SELECTED_STRATEGY_KEY), 10);
    if (Number.isFinite(v) && v >= 0 && v < ADVANCED_STRATEGY_COUNT) return v;
  } catch (_) {}
  return -1;
}

function _persistSelectedStrategyIdx(idx) {
  try {
    if (idx >= 0 && idx < ADVANCED_STRATEGY_COUNT) {
      localStorage.setItem(ADVANCED_SELECTED_STRATEGY_KEY, String(idx));
    } else {
      localStorage.removeItem(ADVANCED_SELECTED_STRATEGY_KEY);
    }
  } catch (_) {}
}

function advancedStrategiesAreFrozen() {
  try { return typeof _isCurrentTokenRunning === "function" && _isCurrentTokenRunning(); }
  catch (_) { return false; }
}

async function fetchAdvancedPresets({ force = false } = {}) {
  if (!walletAuthToken || !walletAuthAccount) return null;
  if (advancedStrategiesLoaded && !force) return advancedStrategiesState;
  try {
    const data = await apiGet("/api/advanced_presets");
    if (!data || !Array.isArray(data.slots)) return null;
    advancedStrategiesState = data;
    advancedStrategiesLoaded = true;
    await _maybeMigrateLegacyAdvancedSettings();
    renderAdvancedStrategies();
    if (advancedMmEnabled && selectedStrategyIdx < 0) autoSelectInitialStrategy();
    return advancedStrategiesState;
  } catch (_) {
    return null;
  }
}

function _coerceLegacySettings(raw) {
  const spreadMin = Number(raw.spreadMin);
  const spreadMax = Number(raw.spreadMax);
  const posMin = Number(raw.posMin);
  const posMax = Number(raw.posMax);
  if (!(spreadMin > 0) || !(spreadMax > 0) || !(posMin > 0) || !(posMax > 0)) return null;
  if (spreadMin > spreadMax || posMin > posMax) return null;
  return {
    spreadMin, spreadMax, posMin, posMax,
    engineSpeed: raw.engineSpeed === "fast" ? "fast" : "normal",
    directionBias: raw.directionBias === "long" ? "long" : raw.directionBias === "short" ? "short" : "neutral",
    quoteMode: ["grid", "fortress", "adaptive"].includes(raw.quoteMode) ? raw.quoteMode : "sniper",
    enabled: true,
  };
}

async function _maybeMigrateLegacyAdvancedSettings() {
  try {
    if (localStorage.getItem(ADVANCED_STRATEGY_MIGRATED_KEY) === "1") return;
  } catch (_) { return; }
  const slots = advancedStrategiesState && advancedStrategiesState.slots;
  if (!Array.isArray(slots) || !slots[0] || !slots[0].empty) {
    try { localStorage.setItem(ADVANCED_STRATEGY_MIGRATED_KEY, "1"); } catch (_) {}
    return;
  }
  let parsed = null;
  try {
    const raw = localStorage.getItem(ADVANCED_MM_CONFIG_STORAGE_KEY);
    if (raw) parsed = JSON.parse(raw);
  } catch (_) { parsed = null; }
  if (!parsed || typeof parsed !== "object") {
    try { localStorage.setItem(ADVANCED_STRATEGY_MIGRATED_KEY, "1"); } catch (_) {}
    return;
  }
  const settings = _coerceLegacySettings(parsed);
  if (!settings) {
    try { localStorage.setItem(ADVANCED_STRATEGY_MIGRATED_KEY, "1"); } catch (_) {}
    return;
  }
  const authAtStart = walletAuthAccount;
  try {
    const resp = await apiPost("/api/advanced_presets", { slot: 0, action: "save", settings });
    if (walletAuthAccount !== authAtStart) return;
    if (resp && Array.isArray(resp.slots)) advancedStrategiesState = resp;
    try { localStorage.setItem(ADVANCED_STRATEGY_MIGRATED_KEY, "1"); } catch (_) {}
  } catch (_) {
    // Retry next load
  }
}

function _formatStrategySummary(slot) {
  if (!slot || slot.empty || !slot.settings) return "Empty : click to create";
  const s = slot.settings;
  const fmt = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return "-";
    return Number.isInteger(n) ? String(n) : n.toFixed(n >= 10 ? 1 : 2);
  };
  const spreads = `${fmt(s.spreadMin)}-${fmt(s.spreadMax)} bps`;
  const pos = `$${fmt(s.posMin)}-${fmt(s.posMax)}`;
  const meta = [
    s.engineSpeed === "fast" ? "Fast" : "Normal",
    (s.directionBias || "neutral").charAt(0).toUpperCase() + (s.directionBias || "neutral").slice(1),
  ].join(" · ");
  return `${spreads} · ${pos}\n${meta}`;
}

function _formatSavedAgo(savedAt) {
  const ts = Number(savedAt) || 0;
  if (!ts) return "";
  const nowSec = Date.now() / 1000;
  const delta = Math.max(0, Math.floor(nowSec - ts));
  if (delta < 5) return "Saved just now";
  if (delta < 60) return `Saved ${delta}s ago`;
  if (delta < 3600) return `Saved ${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `Saved ${Math.floor(delta / 3600)}h ago`;
  return `Saved ${Math.floor(delta / 86400)}d ago`;
}

const _TRASH_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 4h11"/><path d="M6 4V2.5h4V4"/><path d="M3.5 4l.7 9.5a1 1 0 0 0 1 .9h5.6a1 1 0 0 0 1-.9L12.5 4"/><path d="M6.5 7v5"/><path d="M9.5 7v5"/></svg>';

function renderAdvancedStrategies() {
  const tabsContainer = document.getElementById("advanced_strategies_tabs");
  if (!tabsContainer) return;
  const slots = (advancedStrategiesState && advancedStrategiesState.slots) || [];
  const frozen = advancedStrategiesAreFrozen();
  tabsContainer.innerHTML = "";
  for (let i = 0; i < ADVANCED_STRATEGY_COUNT; i += 1) {
    const slot = slots[i] || { name: `Strategy ${i + 1}`, empty: true };
    const isSelected = i === selectedStrategyIdx;
    const isEmpty = !!slot.empty;

    const tab = document.createElement("div");
    tab.className = "advanced-strategy-tab";
    tab.dataset.slot = String(i);
    tab.setAttribute("role", "tab");
    if (isSelected) tab.classList.add("is-selected");
    if (frozen) tab.classList.add("is-frozen");
    if (isEmpty) tab.classList.add("is-empty");
    // Empty + non-selected → render as "+" create card (slots 2 and 3 only : slot 1 is always a real tab)
    if (isEmpty && !isSelected && i > 0) {
      tab.classList.add("is-create");
      tab.setAttribute("aria-label", `Create strategy ${i + 1}`);
      const plus = document.createElement("div");
      plus.className = "advanced-strategy-plus";
      plus.textContent = "+";
      const createLabel = document.createElement("div");
      createLabel.className = "advanced-strategy-create-label";
      createLabel.textContent = "Create strategy";
      tab.appendChild(plus);
      tab.appendChild(createLabel);
      tab.addEventListener("click", () => {
        if (frozen) return;
        selectStrategySlot(i);
      });
      tabsContainer.appendChild(tab);
      continue;
    }

    const head = document.createElement("div");
    head.className = "advanced-strategy-head";

    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "advanced-strategy-name";
    nameInput.value = slot.name || `Strategy ${i + 1}`;
    nameInput.maxLength = 40;
    nameInput.readOnly = !isSelected || frozen;
    nameInput.tabIndex = isSelected ? 0 : -1;
    nameInput.addEventListener("blur", () => renameStrategySlot(i, nameInput.value));
    nameInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); nameInput.blur(); }
    });
    nameInput.addEventListener("mousedown", (ev) => {
      if (isSelected && !frozen) ev.stopPropagation();
    });
    head.appendChild(nameInput);

    if (isSelected && !isEmpty) {
      const trash = document.createElement("button");
      trash.type = "button";
      trash.className = "advanced-strategy-trash";
      trash.setAttribute("aria-label", "Reset strategy");
      trash.title = "Reset strategy";
      trash.innerHTML = _TRASH_SVG;
      trash.disabled = frozen;
      trash.addEventListener("click", (ev) => {
        ev.stopPropagation();
        resetCurrentStrategy();
      });
      head.appendChild(trash);
    }

    tab.appendChild(head);

    if (!isEmpty) {
      const summary = document.createElement("div");
      summary.className = "advanced-strategy-summary";
      summary.textContent = _formatStrategySummary(slot);
      summary.style.whiteSpace = "pre-line";
      tab.appendChild(summary);
    }

    if (isSelected && !isEmpty && slot.savedAt) {
      const savedTs = document.createElement("div");
      savedTs.className = "advanced-strategy-saved-ts";
      savedTs.dataset.savedAt = String(slot.savedAt);
      savedTs.textContent = _formatSavedAgo(slot.savedAt);
      tab.appendChild(savedTs);
    }

    tab.addEventListener("click", () => {
      if (frozen) return;
      if (isSelected) return;
      selectStrategySlot(i);
    });

    tabsContainer.appendChild(tab);
  }
  updateAdvancedStrategyEditorVisibility();
  _ensureSavedTsTicker();
}

let _savedTsTickerHandle = null;
function _ensureSavedTsTicker() {
  if (_savedTsTickerHandle) return;
  _savedTsTickerHandle = window.setInterval(() => {
    const nodes = document.querySelectorAll(".advanced-strategy-saved-ts");
    if (!nodes.length) return;
    nodes.forEach((n) => {
      const ts = Number(n.dataset.savedAt) || 0;
      if (ts) n.textContent = _formatSavedAgo(ts);
    });
  }, 10000);
}

function updateAdvancedStrategyEditorVisibility() {
  const fields = document.getElementById("advanced_mm_fields");
  const footer = document.getElementById("advanced_strategy_footer");
  const banner = document.getElementById("advanced_strategies_banner");
  const show = advancedMmEnabled && selectedStrategyIdx >= 0;
  if (fields) fields.classList.toggle("hidden", !show);
  if (footer) footer.classList.toggle("hidden", !show);
  const frozen = advancedStrategiesAreFrozen();
  if (banner) {
    if (frozen && advancedMmEnabled) {
      banner.classList.remove("hidden");
      const sym = (typeof _currentSymbol === "function" ? _currentSymbol() : "") || "";
      banner.textContent = `Bot running${sym ? " on " + sym : ""} : stop to edit strategies`;
    } else {
      banner.classList.add("hidden");
    }
  }
  _setAdvancedStrategyFieldsDisabled(frozen);
}

function _setAdvancedStrategyFieldsDisabled(disabled) {
  ["advanced_spread_min_bps", "advanced_spread_max_bps", "advanced_pos_min_usd", "advanced_pos_max_usd", "advanced_engine_speed", "advanced_direction_bias"].forEach((id) => {
    const el = document.getElementById(id);
    if (el instanceof HTMLInputElement || el instanceof HTMLSelectElement) el.disabled = !!disabled;
  });
  const resetBtn = document.getElementById("advanced_strategy_reset_btn");
  if (resetBtn) resetBtn.disabled = !!disabled;
}

function selectStrategySlot(slotIdx) {
  if (slotIdx < 0 || slotIdx >= ADVANCED_STRATEGY_COUNT) return;
  if (advancedStrategiesAreFrozen()) return;
  if (slotIdx === selectedStrategyIdx) return; // re-click = no-op
  const slots = (advancedStrategiesState && advancedStrategiesState.slots) || [];
  const slot = slots[slotIdx];
  if (!slot) return;

  selectedStrategyIdx = slotIdx;
  _persistSelectedStrategyIdx(slotIdx);

  if (slot.empty) {
    // Empty slot: reset fields to hardcoded defaults. Do NOT auto-save yet -
    // the slot stays empty on server until the user actually edits a field.
    _applyStrategyToUi({
      spreadMin: 0.30, spreadMax: 2.00,
      posMin: 20, posMax: 100,
      engineSpeed: "normal", directionBias: "neutral", quoteMode: "sniper",
    });
  } else if (slot.settings) {
    _applyStrategyToUi(slot.settings);
  }

  renderAdvancedStrategies();
  setAdvancedMmError(validateAdvancedMmConfig(readAdvancedMmConfigFromUi()));
}

function _applyStrategyToUi(s) {
  const map = {
    advanced_spread_min_bps: s.spreadMin,
    advanced_spread_max_bps: s.spreadMax,
    advanced_pos_min_usd: s.posMin,
    advanced_pos_max_usd: s.posMax,
  };
  Object.keys(map).forEach((id) => {
    const el = document.getElementById(id);
    if (el instanceof HTMLInputElement && Number.isFinite(Number(map[id]))) {
      el.value = String(Number(map[id]));
    }
  });
  const speedEl = document.getElementById("advanced_engine_speed");
  if (speedEl instanceof HTMLSelectElement && typeof s.engineSpeed === "string") {
    speedEl.value = s.engineSpeed === "fast" ? "fast" : "normal";
  }
  const biasEl = document.getElementById("advanced_direction_bias");
  if (biasEl instanceof HTMLSelectElement && typeof s.directionBias === "string") {
    const b = s.directionBias === "long" ? "long" : s.directionBias === "short" ? "short" : "neutral";
    biasEl.value = b;
    applyDirectionBias(b, { silent: true });
  }
  if (typeof s.quoteMode === "string") {
    applyQuoteMode(s.quoteMode, { silent: true });
  }
  persistAdvancedMmSettings();
}

function _scheduleAutoSave(delay = 350) {
  if (_advancedStrategyAutoSaveTimer) clearTimeout(_advancedStrategyAutoSaveTimer);
  _advancedStrategyAutoSaveTimer = setTimeout(() => {
    _advancedStrategyAutoSaveTimer = null;
    _runStrategyAutoSave();
  }, delay);
}

async function _runStrategyAutoSave() {
  if (selectedStrategyIdx < 0) return;
  if (!walletAuthToken || !walletAuthAccount) return;
  const cfg = readAdvancedMmConfigFromUi();
  const err = validateAdvancedMmConfig(cfg);
  if (err) { setAdvancedMmError(err); return; }
  if (_advancedStrategyAutoSaveInFlight) {
    _advancedStrategyPendingSave = true;
    return;
  }
  _advancedStrategyAutoSaveInFlight = true;
  const slotIdx = selectedStrategyIdx;
  const authAtStart = walletAuthAccount;
  try {
    const resp = await apiPost("/api/advanced_presets", {
      slot: slotIdx,
      action: "save",
      settings: { ...cfg, enabled: true },
    });
    if (walletAuthAccount !== authAtStart) return;
    if (resp && Array.isArray(resp.slots)) {
      advancedStrategiesState = resp;
    }
    _flashStrategySaved(slotIdx);
  } catch (e) {
    setAdvancedMmError(`Save failed: ${e.message || e}`);
  } finally {
    _advancedStrategyAutoSaveInFlight = false;
    if (_advancedStrategyPendingSave) {
      _advancedStrategyPendingSave = false;
      _scheduleAutoSave(50);
    }
  }
}

function _flashStrategySaved(slotIdx) {
  const tabs = document.querySelectorAll("#advanced_strategies_tabs .advanced-strategy-tab");
  const tab = tabs[slotIdx];
  if (!tab) return;
  tab.classList.remove("pulse-saved");
  void tab.offsetWidth; // restart animation
  tab.classList.add("pulse-saved");
  setTimeout(() => { tab.classList.remove("pulse-saved"); }, 600);
}

async function renameStrategySlot(slotIdx, newName) {
  const slots = (advancedStrategiesState && advancedStrategiesState.slots) || [];
  const slot = slots[slotIdx];
  if (!slot) return;
  const trimmed = String(newName || "").trim().slice(0, 40);
  if (!trimmed || trimmed === slot.name) {
    renderAdvancedStrategies();
    return;
  }
  const authAtStart = walletAuthAccount;
  try {
    const resp = await apiPost("/api/advanced_presets", {
      slot: slotIdx,
      action: "rename",
      name: trimmed,
    });
    if (walletAuthAccount !== authAtStart) return;
    if (!document.getElementById("advanced_strategies_tabs")) return;
    if (resp && Array.isArray(resp.slots)) {
      advancedStrategiesState = resp;
      renderAdvancedStrategies();
    }
  } catch (e) {
    setAdvancedMmError(`Rename failed: ${e.message || e}`);
  }
}

async function resetCurrentStrategy() {
  if (selectedStrategyIdx < 0) return;
  if (advancedStrategiesAreFrozen()) return;
  const slotIdx = selectedStrategyIdx;
  const authAtStart = walletAuthAccount;
  try {
    const resp = await apiPost("/api/advanced_presets", { slot: slotIdx, action: "reset" });
    if (walletAuthAccount !== authAtStart) return;
    if (resp && Array.isArray(resp.slots)) advancedStrategiesState = resp;
    selectedStrategyIdx = -1;
    _persistSelectedStrategyIdx(-1);
    renderAdvancedStrategies();
  } catch (e) {
    setAdvancedMmError(`Reset failed: ${e.message || e}`);
  }
}

function autoSelectInitialStrategy() {
  if (advancedStrategiesAreFrozen()) { renderAdvancedStrategies(); return; }
  const slots = (advancedStrategiesState && advancedStrategiesState.slots) || [];
  const stored = _readSelectedStrategyIdxFromStorage();
  if (stored >= 0 && slots[stored] && !slots[stored].empty) {
    selectStrategySlot(stored);
    return;
  }
  // First-time ever (or all slots empty): auto-select slot 0 with current UI values
  selectStrategySlot(0);
}

async function openStrategiesAfterAdvancedToggleOn() {
  if (!advancedStrategiesLoaded) {
    await fetchAdvancedPresets();
  }
  // If fetch already auto-selected (via fetchAdvancedPresets), nothing to do
  if (selectedStrategyIdx < 0) autoSelectInitialStrategy();
  else renderAdvancedStrategies();
}

function resetAdvancedPresetsClientState() {
  advancedStrategiesState = null;
  advancedStrategiesLoaded = false;
  selectedStrategyIdx = -1;
  if (_advancedStrategyAutoSaveTimer) {
    clearTimeout(_advancedStrategyAutoSaveTimer);
    _advancedStrategyAutoSaveTimer = null;
  }
  _advancedStrategyAutoSaveInFlight = false;
  _advancedStrategyPendingSave = false;
  const tabs = document.getElementById("advanced_strategies_tabs");
  if (tabs) tabs.innerHTML = "";
  const fields = document.getElementById("advanced_mm_fields");
  if (fields) fields.classList.add("hidden");
  const footer = document.getElementById("advanced_strategy_footer");
  if (footer) footer.classList.add("hidden");
  const banner = document.getElementById("advanced_strategies_banner");
  if (banner) banner.classList.add("hidden");
}

function persistActiveSessionTargetVolume(value) {
  try {
    if (Number.isFinite(Number(value)) && Number(value) > 0) {
      localStorage.setItem(ACTIVE_SESSION_TARGET_VOL_STORAGE_KEY, String(value));
    } else {
      localStorage.removeItem(ACTIVE_SESSION_TARGET_VOL_STORAGE_KEY);
    }
  } catch (_) {
    // ignore storage failures
  }
}

function restoreActiveSessionTargetVolume() {
  try {
    const raw = localStorage.getItem(ACTIVE_SESSION_TARGET_VOL_STORAGE_KEY);
    const val = Number(raw);
    if (Number.isFinite(val) && val > 0) {
      activeSessionTargetVolumeUsd = val;
    }
  } catch (_) {
    // ignore storage failures
  }
}

function currentBuilderSettings(modeValue = "") {
  if (isQfexMode(modeValue)) {
    // QFEX : aucun frais Tradoor en plus, le builder touche une part des frais QFEX.
    return { address: "", builderId: 0, feeBps: 0, feeTenthsBps: 0, feePerMillionUsd: 0 };
  }
  if (isRisexMode(modeValue)) {
    // Indicatif : le fee réel est imposé par le serveur (remises comprises).
    return {
      address: "",
      builderId: RISEX_BUILDER_ID,
      feeBps: RISEX_BUILDER_FEE_BPS,
      feeTenthsBps: RISEX_BUILDER_FEE_BPS * 10,
      feePerMillionUsd: RISEX_BUILDER_FEE_BPS * 100,
    };
  }
  if (isExtendedMode(modeValue)) {
    return {
      address: EXTENDED_BUILDER_ADDRESS,
      builderId: EXTENDED_BUILDER_ID,
      feeBps: EXTENDED_BUILDER_FEE_BPS,
      feeTenthsBps: EXTENDED_BUILDER_FEE_TENTHS_BPS,
      feePerMillionUsd: EXTENDED_BUILDER_FEE_PER_MILLION_USD,
    };
  }
  return {
    address: BUILDER_ADDRESS,
    builderId: 0,
    feeBps: BUILDER_FEE_BPS,
    feeTenthsBps: BUILDER_FEE_TENTHS_BPS,
    feePerMillionUsd: BUILDER_FEE_PER_MILLION_USD,
  };
}

function getNotionalUnit() {
  if (isNadoMode()) return "USDT";
  return "USDC";
}

function formatNotionalWithUnit(v) {
  const n = typeof v === "string" ? parseFloat(v) : v;
  if (Number.isNaN(n) || n === null || n === undefined) return "-";
  return `${n.toFixed(2)} ${getNotionalUnit()}`;
}

function _isWalletConnected() {
  return Object.keys(walletAuthTokens).length > 0 || !!walletAuthToken;
}

function setAvailableNotionalConnectHint() {
  const el = document.getElementById("available_notional");
  if (!el) return;
  el.innerHTML = '<span class="notional-connect-hint">Connect wallet to view</span>';
}

function setNotionalLoading(el) {
  if (!el) return;
  if (el.id === "available_notional" && !_isWalletConnected()) {
    setAvailableNotionalConnectHint();
    return;
  }
  el.innerHTML = '<span class="notional-spinner"></span>';
}

function setNotionalValue(el, v) {
  if (!el) return;
  el.textContent = formatNotionalWithUnit(v);
}

function updateNotionalTokenIcon() {
  const icon = document.getElementById("notional_token_icon");
  if (!icon) return;
  const unit = getNotionalUnit();
  if (unit === "USDT") { icon.src = "/web/usdt.png"; icon.alt = "USDT"; icon.classList.remove("hidden-icon"); }
  else if (unit === "USDC") { icon.src = "/web/usdc.png"; icon.alt = "USDC"; icon.classList.remove("hidden-icon"); }
  else { icon.classList.add("hidden-icon"); }
}

function updateNotionalUnitUI() {
  const unit = getNotionalUnit();
  const sessionLabel = document.getElementById("session_notional_label");
  if (sessionLabel) {
    sessionLabel.textContent = `Session Notional (${unit})`;
  }
  const availableLabel = document.getElementById("available_notional_label");
  if (availableLabel) {
    availableLabel.textContent = `Available Notional (${unit})`;
  }
  const availableEl = document.getElementById("available_notional");
  if (availableEl && latestAvailableNotional !== null) {
    setNotionalValue(availableEl, latestAvailableNotional);
  }
  updateNotionalTokenIcon();
}

function currentSelectedLeverage() {
  const symbol = document.getElementById("market_symbol")?.value || "";
  const lev = Number(marketLeverageMap.get(symbol));
  if (Number.isFinite(lev) && lev > 0) return lev;
  return DEFAULT_MM.defaultLeverage;
}

// Levier max RISEx de *symbol* : carte des marchés RISEx si chargée, sinon repli
// FAST_RISEX_LEVERAGE, jamais au-dessus de ce que la venue autorise.
function _risexLeverageFor(symbol) {
  const live = Number(marketLeverageMap.get(symbol));
  const fast = Number(FAST_RISEX_LEVERAGE[symbol]);
  if (Number.isFinite(live) && live > 0 && String(symbol).includes("/")) return live;
  if (Number.isFinite(fast) && fast > 0) return fast;
  return 10;
}

function _isQfexSymbol(symbol) {
  const sym = String(symbol || "");
  if (!/^[A-Z0-9]+-USD$/.test(sym)) return false;
  const cached = marketCache.get(`qfex_mainnet|top20`);
  const live = cached && Array.isArray(cached.symbols) && cached.ts > 0 ? cached.symbols : null;
  return (live || FAST_QFEX_SYMBOLS).includes(sym);
}

function _qfexLeverageFor(symbol) {
  const live = Number(marketLeverageMap.get(symbol));
  const fast = Number(FAST_QFEX_LEVERAGE[symbol]);
  if (Number.isFinite(live) && live > 0 && /-USD$/.test(String(symbol))) return live;
  if (Number.isFinite(fast) && fast > 0) return fast;
  return 10;
}

function updateLeverageUI() {
  const el = document.getElementById("selected_leverage");
  if (!el) return;
  const lev = currentSelectedLeverage();
  el.textContent = `Pair Max Leverage: ${lev.toFixed(1).replace(/\\.0$/, "")}x`;
  // Marché HIP-3 en marge isolée uniquement : le levier est plafonné par marché.
  const symbol = document.getElementById("market_symbol")?.value || "";
  if (!isExtendedMode() && !isNadoMode() && !isRisexMode() && !isQfexMode() && isOnlyIsolatedMarket(symbol)) {
    el.textContent += " · Isolated margin only";
    el.title = "This market only supports isolated margin: the leverage is capped per market.";
  } else {
    el.removeAttribute("title");
  }
  updateMinOrderHint();
}

// Min trade size du marché sélectionné (Extended : minOrderSize de la venue).
// Affiché sous le sizing pour que l'utilisateur ne configure pas un order size
// sous le minimum (le bot skipperait toutes les quotes).
function updateMinOrderHint() {
  const el = document.getElementById("selected_min_order");
  if (!el) return;
  const symbol = document.getElementById("market_symbol")?.value || "";
  const qty = Number(marketMinOrderSizeMap[symbol]);
  const usd = Number(marketMinOrderUsdMap[symbol]);
  if (!Number.isFinite(qty) || qty <= 0) {
    el.classList.add("hidden");
    el.textContent = "";
    return;
  }
  // RISEx nomme ses marchés "BTC/USDC" : on retire aussi le "/USDC".
  const shortName = String(symbol).replace(/_24_\d+(?=-)/, "").replace(/[-/](USDC|USDH|USD)$/i, "");
  const usdText = Number.isFinite(usd) && usd > 0 ? ` (~$${usd.toFixed(2)})` : "";
  el.textContent = `Min Order: ${qty} ${shortName}${usdText}`;
  // RISEx : un placement de quota par ordre, rentabilisé vers 45 $ par ordre.
  if (isRisexMode()) {
    el.textContent += " · $45+ orders recommended";
    el.title = RISEX_ORDER_SIZE_HINT;
  } else {
    el.removeAttribute("title");
  }
  el.classList.remove("hidden");
}

function applyLeverageMap(symbols, maxLeverageObj) {
  marketLeverageMap = new Map();
  const src = maxLeverageObj && typeof maxLeverageObj === "object" ? maxLeverageObj : {};
  symbols.forEach((s) => {
    const direct = Number(src[s]);
    const upper = Number(src[String(s).toUpperCase()]);
    const lev = Number.isFinite(direct) && direct > 0
      ? direct
      : Number.isFinite(upper) && upper > 0
        ? upper
        : DEFAULT_MM.defaultLeverage;
    marketLeverageMap.set(s, lev);
  });
  updateLeverageUI();
  updateDynamicMaxLoss();
}

function getMetricsCacheKey(source, modeValue = "") {
  const src = String(source || document.getElementById("market_source")?.value || "top20");
  const mode = String(modeValue || document.getElementById("mode")?.value || "hyperliquid_mainnet");
  const account = typeof getSelectedTradingAccount === "function" ? getSelectedTradingAccount() : "";
  return `${mode}|${src}|${account}`;
}

function applyCachedMetricsForCurrentSelection() {
  const source = document.getElementById("market_source")?.value || "top20";
  const key = getMetricsCacheKey(source);
  const cached = metricsCache.get(key);
  if (!cached) return false;
  if (Date.now() - Number(cached.ts || 0) > METRICS_CACHE_TTL_MS) return false;
  const value = Number(cached.availableNotional);
  if (!Number.isFinite(value)) return false;
  // Don't paint a non-positive cached value: a stale "0" would defeat the loading
  // state and re-introduce the "0 USDC" flash. Let the caller show the spinner and
  // wait for a live reading instead.
  if (value <= 0.01) return false;
  latestAvailableNotional = value;
  const availableEl = document.getElementById("available_notional");
  setNotionalValue(availableEl, value);
  const notionalInput = document.getElementById("session_notional");
  if (notionalInput) notionalInput.max = String(Math.round(value * 100) / 100);
  const notionalMaxBtn = document.getElementById("session_notional_max");
  if (notionalMaxBtn) notionalMaxBtn.disabled = !(value > 0);
  return true;
}

function showError(id, message) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = message;
  el.classList.remove("hidden");
}

function clearError(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = "";
  el.classList.add("hidden");
}

function formatUsdCompact(v) {
  const n = typeof v === "string" ? parseFloat(v) : Number(v);
  if (!Number.isFinite(n)) return "-";
  return `${formatCompactVolume(n)} $`;
}

function setStatsDrawerOpen(open) {
  statsDrawerOpen = !!open;
  const drawer = document.getElementById("stats_drawer");
  const overlay = document.getElementById("stats_overlay");
  if (!drawer) return;
  drawer.classList.toggle("hidden", !statsDrawerOpen);
  drawer.setAttribute("aria-hidden", statsDrawerOpen ? "false" : "true");
  if (overlay) {
    overlay.classList.toggle("open", statsDrawerOpen);
    overlay.setAttribute("aria-hidden", statsDrawerOpen ? "false" : "true");
  }
}

/* ─── HIP-4 volume drawer (CSV) ───────────────────────────── */
let hipVolDrawerOpen = false;

function setHipVolDrawerOpen(open) {
  hipVolDrawerOpen = !!open;
  const drawer = document.getElementById("hipvol_drawer");
  const overlay = document.getElementById("hipvol_overlay");
  if (!drawer) return;
  if (hipVolDrawerOpen) {
    drawer.classList.remove("hidden");
    requestAnimationFrame(() => drawer.classList.add("visible"));
  } else {
    drawer.classList.remove("visible");
    drawer.classList.add("closing");
    const onEnd = () => { drawer.classList.remove("closing"); drawer.classList.add("hidden"); drawer.removeEventListener("transitionend", onEnd); };
    drawer.addEventListener("transitionend", onEnd);
  }
  drawer.setAttribute("aria-hidden", hipVolDrawerOpen ? "false" : "true");
  if (overlay) {
    overlay.classList.toggle("open", hipVolDrawerOpen);
    overlay.setAttribute("aria-hidden", hipVolDrawerOpen ? "false" : "true");
  }
  document.body.classList.toggle("kpoints-open", hipVolDrawerOpen);
  if (hipVolDrawerOpen) loadHip4PlatformVolume();
}

let _hip4PlatformVolTs = 0;
let _hip4PlatformVol24hUsd = null; // dernière valeur connue, réutilisée pour le calcul de part
async function loadHip4PlatformVolume() {
  const el = document.getElementById("hipvol_platform_value");
  if (!el) return;
  // Throttle: la donnée serveur est déjà cachée 90s, inutile de re-fetch à chaque ouverture.
  const nowMs = Date.now();
  if (el.dataset.loaded === "1" && nowMs - _hip4PlatformVolTs < 90000) return;
  if (!el.textContent || el.textContent === "…") el.textContent = "…";
  try {
    const res = await fetch("/api/hip4/platform_volume", { headers: { "Accept": "application/json" } });
    const d = await res.json();
    const v = typeof d.total_24h_usd === "number" ? d.total_24h_usd : null;
    if (v == null) { el.textContent = "n/a"; return; }
    _hip4PlatformVol24hUsd = v;
    el.textContent = "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 });
    el.title = `${d.outcome_count || 0} outcomes · ${d.market_count || 0} markets`;
    el.dataset.loaded = "1";
    _hip4PlatformVolTs = nowMs;
  } catch (e) {
    el.textContent = "n/a";
  }
}

function setStatsChartTooltipCore({
  tooltipId,
  canvasId,
  lineLabel = "Volume",
  visible,
  x = 0,
  y = 0,
  date = "",
  total = "",
}) {
  const el = document.getElementById(tooltipId);
  if (!el) return;
  if (!visible) {
    el.classList.add("hidden");
    return;
  }
  const extraLine = String(lineLabel || "").trim()
    ? `<div class="line">${lineLabel}</div>`
    : "";
  el.innerHTML = `<div class="date">${date}</div><div class="total">${total}</div>${extraLine}`;
  const canvas = document.getElementById(canvasId);
  const wrap = canvas ? canvas.closest(".stats-chart-wrap") : null;
  const wrapW = wrap ? wrap.clientWidth : 0;
  const wrapH = wrap ? wrap.clientHeight : 0;
  const baseLeft = (canvas ? canvas.offsetLeft : 0) + Number(x || 0);
  const baseTop = (canvas ? canvas.offsetTop : 0) + Number(y || 0);
  const gap = 12;
  const pad = 8;
  // Measure tooltip first (while visible) then clamp in chart wrapper.
  el.classList.remove("hidden");
  const tipW = el.offsetWidth || 160;
  const tipH = el.offsetHeight || 96;
  let left = baseLeft + gap;
  let top = baseTop - tipH - gap;
  if (wrapW > 0) {
    if (left + tipW > wrapW - pad) {
      left = baseLeft - tipW - gap;
    }
    left = Math.max(pad, Math.min(left, wrapW - tipW - pad));
  }
  if (wrapH > 0) {
    if (top < pad) {
      top = baseTop + gap;
    }
    top = Math.max(pad, Math.min(top, wrapH - tipH - pad));
  }
  el.style.left = `${Math.round(left)}px`;
  el.style.top = `${Math.round(top)}px`;
  el.classList.remove("hidden");
}

function setStatsChartTooltip(opts) {
  setStatsChartTooltipCore({
    tooltipId: "stats_chart_tooltip",
    canvasId: "stats_daily_volume_chart",
    lineLabel: "",
    ...opts,
  });
}

function setStatsFeesChartTooltip(opts) {
  // kept as no-op for any lingering calls
}

function drawStatsBarChart({
  canvasId,
  series,
  valueKey,
  maxFormatter,
  gradientTop,
  gradientBottom,
}) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const parent = canvas.parentElement;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const cssWidth = Math.max(280, Math.round((parent?.clientWidth || 340) - 2));
  const cssHeight = 320;
  canvas.width = Math.max(1, Math.round(cssWidth * dpr));
  canvas.height = Math.max(1, Math.round(cssHeight * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const points = Array.isArray(series) ? series : [];
  const data = points.map((p) => {
    const v = Number(p && p[valueKey]);
    return Number.isFinite(v) && v > 0 ? v : 0;
  });
  const maxValue = Math.max(1, ...data);

  const left = 38;
  const right = 10;
  const top = 12;
  const bottom = 34;
  const plotW = Math.max(10, cssWidth - left - right);
  const plotH = Math.max(10, cssHeight - top - bottom);

  ctx.strokeStyle = "rgba(255,255,255,0.14)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(left, top);
  ctx.lineTo(left, cssHeight - bottom);
  ctx.lineTo(cssWidth - right, cssHeight - bottom);
  ctx.stroke();

  ctx.fillStyle = "rgba(255,255,255,0.6)";
  ctx.font = "11px Inter, sans-serif";
  ctx.textAlign = "right";
  ctx.fillText("0", left - 6, cssHeight - bottom + 4);
  ctx.fillText(maxFormatter(maxValue), left - 6, top + 4);

  if (!data.length) {
    return null;
  }

  const step = plotW / data.length;
  const barW = Math.max(2, Math.min(12, step * 0.68));
  const grad = ctx.createLinearGradient(0, top, 0, cssHeight - bottom);
  grad.addColorStop(0, gradientTop);
  grad.addColorStop(1, gradientBottom);
  ctx.fillStyle = grad;

  for (let i = 0; i < data.length; i += 1) {
    const value = data[i];
    const h = (value / maxValue) * plotH;
    const x = left + i * step + (step - barW) * 0.5;
    const y = cssHeight - bottom - h;
    ctx.fillRect(x, y, barW, Math.max(1, h));
  }

  return {
    left,
    right,
    step,
    barW,
    cssWidth,
    cssHeight,
    points,
    data,
  };
}

function drawStatsDailyVolumeChart(series) {
  const points = Array.isArray(series) ? series : [];
  const chartSeries = points.map((p) => ({
    ...p,
    value: Number(p && p.volume_usd),
  }));
  statsChartMeta = drawStatsBarChart({
    canvasId: "stats_daily_volume_chart",
    series: chartSeries,
    valueKey: "value",
    maxFormatter: (v) => formatCompactVolume(v),
    gradientTop: "rgba(99, 192, 255, 0.92)",
    gradientBottom: "rgba(255, 152, 72, 0.88)",
  });
  if (!statsChartMeta) return;
  const canvas = document.getElementById("stats_daily_volume_chart");
  const ctx = canvas?.getContext("2d");
  if (!ctx) return;
  const left = statsChartMeta.left;
  const step = statsChartMeta.step;
  const cssHeight = statsChartMeta.cssHeight;

  const fmtDay = (raw) => {
    const s = String(raw || "");
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
    if (!m) return "";
    return String(Number(m[3]));
  };
  const every = 1;
  ctx.fillStyle = "rgba(215, 228, 246, 0.68)";
  ctx.font = "9px Inter, sans-serif";
  ctx.textAlign = "center";
  for (let i = 0; i < points.length; i += every) {
    const label = fmtDay(points[i] && points[i].date);
    if (!label) continue;
    const x = left + i * step + step * 0.5;
    ctx.fillText(label, x, cssHeight - 6);
  }
}

// Fees chart removed : replaced by annualized fees card

async function refreshStatsPanel() {
  if (!statsDrawerOpen || statsInFlight) return;
  statsInFlight = true;
  clearError("stats_error");
  try {
    const account = document.getElementById("account_address")?.value?.trim() || "";
    const overviewUrl = account
      ? `/api/admin/stats/overview?account=${encodeURIComponent(account)}`
      : "/api/admin/stats/overview";
    const [overview, daily] = await Promise.all([
      statsApiGet(overviewUrl),
      statsApiGet("/api/admin/stats/daily_volume?days=30"),
    ]);
    const uniqueEl = document.getElementById("stats_unique_addresses");
    const dailyEl = document.getElementById("stats_daily_volume");
    const allTimeEl = document.getElementById("stats_all_time_volume");
    const dailyFeesEl = document.getElementById("stats_daily_fees");
    const allTimeFeesEl = document.getElementById("stats_all_time_fees");
    const dailyVolumeUsd = Number(overview && overview.daily_volume_usd);
    const allTimeVolumeUsd = Number(overview && overview.all_time_volume_usd);
    const backendDailyFees = Number(overview && overview.daily_fees_usd);
    const dailyFeesUsd = Number.isFinite(backendDailyFees) && backendDailyFees >= 0
      ? backendDailyFees : platformFeesUsdFromVolume(dailyVolumeUsd);
    const backendAllTimeFees = Number(overview && overview.all_time_fees_usd);
    const allTimeFeesUsd = Number.isFinite(backendAllTimeFees) && backendAllTimeFees >= 0
      ? backendAllTimeFees : platformFeesUsdFromVolume(allTimeVolumeUsd);
    if (uniqueEl) {
      const n = Number(overview && overview.unique_connected_addresses);
      uniqueEl.textContent = Number.isFinite(n) ? n.toLocaleString("en-US") : "0";
    }
    if (dailyEl) dailyEl.textContent = formatUsdCompact(overview && overview.daily_volume_usd);
    if (allTimeEl) allTimeEl.textContent = formatUsdCompact(overview && overview.all_time_volume_usd);
    if (dailyFeesEl) dailyFeesEl.textContent = formatUsdCompact(dailyFeesUsd);
    if (allTimeFeesEl) allTimeFeesEl.textContent = formatUsdCompact(allTimeFeesUsd);
    const avgPpmEl = document.getElementById("stats_avg_ppm");
    if (avgPpmEl) {
      const ppm = Number(overview && overview.avg_pnl_per_million_usd) / 10.0;
      if (Number.isFinite(ppm)) {
        avgPpmEl.textContent = `${ppm >= 0 ? "+" : ""}${ppm.toFixed(2)} $ / $100k`;
        avgPpmEl.style.color = ppm >= 0 ? "#4ade80" : "#f87171";
      }
    }
    const podiumSection = document.getElementById("podium_section");
    const topWallets = Array.isArray(overview && overview.top_wallets) ? overview.top_wallets : [];
    if (podiumSection) {
      const mapping = [
        { rank: 1, data: topWallets[0] },
        { rank: 2, data: topWallets[1] },
        { rank: 3, data: topWallets[2] },
      ];
      for (const { rank, data } of mapping) {
        const addrEl = document.getElementById(`podium_addr_${rank}`);
        const volEl = document.getElementById(`podium_vol_${rank}`);
        if (addrEl) addrEl.textContent = (data && data.address) || "\u2014";
        if (volEl) volEl.textContent = data ? formatUsdCompact(data.volume_usd) : "\u2014";
      }
    }
    const rankCard = document.getElementById("stats_your_rank_card");
    const rankEl = document.getElementById("stats_your_rank");
    if (rankCard && rankEl) {
      const rank = overview && overview.your_rank;
      rankCard.classList.remove("stat--rank-gold", "stat--rank-silver", "stat--rank-bronze");
      if (Number.isFinite(rank) && rank > 0) {
        rankEl.textContent = `#${rank}`;
        if (rank === 1) rankCard.classList.add("stat--rank-gold");
        else if (rank === 2) rankCard.classList.add("stat--rank-silver");
        else if (rank === 3) rankCard.classList.add("stat--rank-bronze");
        rankCard.style.display = "";
      } else {
        rankCard.style.display = "none";
      }
    }
    lastStatsSeries = Array.isArray(daily && daily.series) ? daily.series : [];
    // Annualized fees: sum fees over 30d series, extrapolate to 365d
    const fees30d = lastStatsSeries.reduce((sum, p) => {
      const f = Number(p && p.fees_usd);
      return sum + (Number.isFinite(f) && f > 0 ? f : platformFeesUsdFromVolume(Number(p && p.volume_usd)));
    }, 0);
    const annualizedFees = fees30d * (365 / 30);
    const annualizedFeesEl = document.getElementById("stats_annualized_fees");
    if (annualizedFeesEl) annualizedFeesEl.textContent = formatUsdCompact(annualizedFees);
    drawStatsDailyVolumeChart(lastStatsSeries);
    setStatsChartTooltip({ visible: false });
  } catch (e) {
    showError("stats_error", `Stats load failed: ${e.message || e}`);
  } finally {
    statsInFlight = false;
  }
}

async function openStatsPanel() {
  setStatsDrawerOpen(true);
  await refreshStatsPanel();
}

/* ── "Coming soon" flash on disabled nav buttons ──────────────────────── */
function _flashComingSoon(btn) {
  if (btn._comingSoonActive) return;
  btn._comingSoonActive = true;
  const orig = btn.textContent;
  btn.textContent = "Coming soon";
  setTimeout(() => { btn.textContent = orig; btn._comingSoonActive = false; }, 1200);
}

/* ── Competition drawer ─────────────────────────────────────────────────── */
let compDrawerOpen = false;
let compTimerInterval = null;
let compConfettiRaf = null;

function setCompPageOpen(open) {
  compDrawerOpen = !!open;
  const page = document.getElementById("comp_page");
  if (!page) return;
  page.classList.toggle("hidden", !compDrawerOpen);
  page.setAttribute("aria-hidden", compDrawerOpen ? "false" : "true");
  if (compDrawerOpen) {
    refreshCompetition();
    startCompCountdown();
    startCompConfetti();
  } else {
    stopCompCountdown();
    stopCompConfetti();
  }
}

function startCompCountdown() {
  stopCompCountdown();
  updateCompCountdown();
  compTimerInterval = setInterval(updateCompCountdown, 1000);
}
function stopCompCountdown() {
  if (compTimerInterval) { clearInterval(compTimerInterval); compTimerInterval = null; }
}

function updateCompCountdown() {
  const startEl = document.getElementById("comp_dates");
  const statusEl = document.getElementById("comp_timer_status");
  const daysEl = document.getElementById("comp_days");
  const hrsEl = document.getElementById("comp_hours");
  const minEl = document.getElementById("comp_mins");
  const secEl = document.getElementById("comp_secs");
  if (!daysEl) return;

  // Read dates from last API response or fallback
  const start = _compData ? new Date(_compData.start) : new Date("2026-03-29T21:00:00Z");
  const end = _compData ? new Date(_compData.end) : new Date("2026-04-03T21:00:00Z");
  const now = new Date();

  if (startEl) {
    const fmt = (d) => d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "Europe/Paris" });
    startEl.textContent = `${fmt(start)} \u2014 ${fmt(end)} (Paris time)`;
  }

  let diff, label, cls;
  if (now < start) {
    diff = start - now;
    label = "Starts in";
    cls = "comp-timer-status--upcoming";
  } else if (now < end) {
    diff = end - now;
    label = "Live \u2022 Ends in";
    cls = "comp-timer-status--live";
  } else {
    diff = 0;
    label = "Competition ended";
    cls = "comp-timer-status--ended";
  }

  const totalSec = Math.max(0, Math.floor(diff / 1000));
  const d = Math.floor(totalSec / 86400);
  const h = Math.floor((totalSec % 86400) / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  daysEl.textContent = String(d).padStart(2, "0");
  hrsEl.textContent = String(h).padStart(2, "0");
  minEl.textContent = String(m).padStart(2, "0");
  secEl.textContent = String(s).padStart(2, "0");

  if (statusEl) {
    statusEl.textContent = label;
    statusEl.className = "comp-timer-status " + cls;
  }
}

let _compData = null;

async function refreshCompetition() {
  try {
    const account = document.getElementById("account_address")?.value?.trim() || "";
    const url = account
      ? `/api/competition?account=${encodeURIComponent(account)}`
      : "/api/competition";
    const data = await statsApiGet(url);
    _compData = data;

    // Podium
    const lb = Array.isArray(data.leaderboard) ? data.leaderboard : [];
    const byRank = {};
    lb.forEach(e => { byRank[e.rank] = e; });
    for (const r of [1, 2, 3]) {
      const addrEl = document.getElementById(`comp_addr_${r}`);
      const volEl = document.getElementById(`comp_vol_${r}`);
      const entry = byRank[r];
      if (addrEl) addrEl.textContent = entry ? entry.address : "\u2014";
      if (volEl) volEl.textContent = entry ? formatUsdCompact(entry.volume_usd) : "\u2014";
    }

    // Your rank
    const rankCard = document.getElementById("comp_your_rank");
    const rankVal = document.getElementById("comp_your_rank_val");
    const rankVol = document.getElementById("comp_your_rank_vol");
    const connectHint = document.getElementById("comp_connect_hint");

    if (rankCard) {
      rankCard.className = "comp-your-rank";  // reset
      if (account && data.your_rank) {
        rankCard.style.display = "";
        if (connectHint) connectHint.style.display = "none";
        if (rankVal) rankVal.textContent = `#${data.your_rank}`;
        if (rankVol) rankVol.textContent = formatUsdCompact(data.your_volume_usd || 0);
        if (data.your_rank === 1) rankCard.classList.add("comp-your-rank--gold");
        else if (data.your_rank === 2) rankCard.classList.add("comp-your-rank--silver");
        else if (data.your_rank === 3) rankCard.classList.add("comp-your-rank--bronze");
      } else if (account) {
        // Connected but no volume yet
        rankCard.style.display = "";
        if (connectHint) connectHint.style.display = "none";
        if (rankVal) rankVal.textContent = "\u2014";
        if (rankVol) rankVol.textContent = "$0";
      } else {
        rankCard.style.display = "none";
        if (connectHint) connectHint.style.display = "";
      }
    }

    // Meta
    const metaEl = document.getElementById("comp_meta");
    if (metaEl) {
      metaEl.textContent = `${data.total_participants || 0} participants \u00B7 ${formatUsdCompact(data.total_volume_usd || 0)} weekly volume`;
    }
  } catch (e) {
    console.warn("[Competition] refresh failed:", e);
  }
}

/* ── Confetti ───────────────────────────────────────── */
function startCompConfetti() {
  stopCompConfetti();
  const canvas = document.getElementById("comp_confetti");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const particles = [];
  const colors = ["#ffd700", "#c0c0c0", "#cd7f32", "rgba(53,240,194,0.6)"];

  function resize() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
  }
  resize();

  for (let i = 0; i < 35; i++) {
    particles.push({
      x: Math.random() * canvas.width,
      y: Math.random() * canvas.height,
      r: Math.random() * 2.5 + 1,
      dx: (Math.random() - 0.5) * 0.3,
      dy: Math.random() * 0.4 + 0.15,
      color: colors[Math.floor(Math.random() * colors.length)],
      opacity: Math.random() * 0.4 + 0.1,
    });
  }

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const p of particles) {
      ctx.globalAlpha = p.opacity;
      ctx.fillStyle = p.color;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
      p.x += p.dx;
      p.y += p.dy;
      if (p.y > canvas.height + 4) { p.y = -4; p.x = Math.random() * canvas.width; }
      if (p.x < -4) p.x = canvas.width + 4;
      if (p.x > canvas.width + 4) p.x = -4;
    }
    ctx.globalAlpha = 1;
    compConfettiRaf = requestAnimationFrame(draw);
  }
  compConfettiRaf = requestAnimationFrame(draw);
}

function stopCompConfetti() {
  if (compConfettiRaf) { cancelAnimationFrame(compConfettiRaf); compConfettiRaf = null; }
}

/* ── Global milestone toast ────────────────────────────────────────────── */
function _msShowToast(msg, type) {
  let container = document.getElementById("ms_toast_container");
  if (!container) {
    container = document.createElement("div");
    container.id = "ms_toast_container";
    container.className = "ms-toast-container";
    document.body.appendChild(container);
  }
  const toast = document.createElement("div");
  toast.className = `ms-toast ms-toast--${type || "info"}`;
  toast.textContent = msg;
  container.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("ms-toast--visible"));
  setTimeout(() => {
    toast.classList.remove("ms-toast--visible");
    setTimeout(() => toast.remove(), 400);
  }, 4000);
}

/* ── Milestones page ───────────────────────────────────────────────────── */
let msPageOpen = false;
let msParticlesRaf = null;
let _msIntroAborted = false;
let _msIntroRunning = false;
let _msIntroCleanupFn = null;
let _msIntroPauseTimers = [];
let _msLiveTimer = null;
let _msLastVolume = null;
let _msLastCompleted = new Set();
let _msCachedData = null;
let _msActiveTier = null;
// Milestones réservé aux anciens utilisateurs : le bouton n'apparaît que pour
// les comptes ayant déjà du volume (ou >=1 milestone) depuis MILESTONES_START.
let _msEligible = false;

function setMilestonesPageOpen(open) {
  // Réservé aux anciens utilisateurs : on n'ouvre jamais la page pour un compte non éligible.
  if (open && !_msEligible) return;
  msPageOpen = !!open;
  const page = document.getElementById("ms_page");
  if (!page) return;
  page.classList.toggle("hidden", !msPageOpen);
  page.setAttribute("aria-hidden", msPageOpen ? "false" : "true");
  const bar = document.getElementById("ms_bottom_bar");
  if (bar) bar.classList.toggle("hidden", !msPageOpen);
  if (msPageOpen) {
    _startMsParticles();
    let introSeen = false;
    try { introSeen = localStorage.getItem("ms_intro_done") === "1"; } catch (_) {}
    const prefersReduced = typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!introSeen && !prefersReduced) {
      _msSpotlightIntro();
    } else {
      refreshMilestones();
    }
    _msStartLiveTimer();
  } else {
    _msStopLiveTimer();
    _stopMsParticles();
    _msIntroCleanup();
  }
}

async function refreshMilestones() {
  const path = document.getElementById("ms_path");
  if (!path) return;

  const account = document.getElementById("account_address")?.value?.trim() || "";

  if (!account) {
    // Show path with all tiers at 0 progress
    const fallback = {
      volume_usd: 0,
      tiers: [
        { target: 100000, title: "Explorer", reward: "Unlock referral codes", completed: false, progress_pct: 0 },
        { target: 1000000, title: "Dolphin", reward: 'Discord role "Tradoor Enjoyor"', completed: false, progress_pct: 0 },
        { target: 5000000, title: "Shark", reward: "Referral code 20% fees rebate, 20% for refs", completed: false, progress_pct: 0 },
        { target: 10000000, title: "Whale", reward: "$300 USDC", completed: false, progress_pct: 0 },
        { target: 100000000, title: "Legend", reward: '$3,000 USDC + Discord role "Tradoor Beast"', completed: false, progress_pct: 0 },
      ],
    };
    _renderMsPath(path, fallback);
    _updateMsBottomBar(0, fallback.tiers);
    return;
  }

  try {
    // Use preloaded cache if available, fetch fresh in background
    let data;
    if (_msCachedData) {
      data = _msCachedData;
      _msCachedData = null;
    } else {
      data = await statsApiGet(`/api/milestones?account=${encodeURIComponent(account)}`);
    }
    _msLastVolume = data.volume_usd || 0;
    _msLastCompleted = new Set((data.tiers || []).filter(t => t.completed).map(t => t.target));
    _renderMsPath(path, data);
    _updateMsBottomBar(data.volume_usd, data.tiers);
    // Update rank display
    const rankWrap = document.getElementById("ms_rank_wrap");
    const rankVal = document.getElementById("ms_rank_value");
    if (rankWrap && rankVal) {
      if (data.rank && data.total_users) {
        rankWrap.style.display = "";
        rankVal.textContent = `#${data.rank} / ${data.total_users}`;
      } else {
        rankWrap.style.display = "none";
      }
    }
    setTimeout(() => {
      const active = document.querySelector(".ms-step--active");
      if (active) active.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 500);
  } catch (e) {
    console.warn("[Milestones] refresh failed:", e);
  }
}

function _renderMsPath(pathEl, data) {
  const tiers = data.tiers || [];
  const vol = data.volume_usd || 0;

  // Display from top (Legend) to bottom (Explorer)
  const reversed = [...tiers].reverse();
  const firstNonCompleted = tiers.findIndex(t => !t.completed);

  pathEl.innerHTML = "";
  reversed.forEach((tier, i) => {
    const origIdx = tiers.length - 1 - i;
    let state = "locked";
    if (tier.completed) state = "completed";
    else if (origIdx === firstNonCompleted) state = "active";

    const key = (tier.title || "").toLowerCase();
    const isLast = i === reversed.length - 1;

    const amt = tier.target >= 1e6 ? `$${tier.target / 1e6}M` : `$${tier.target / 1e3}K`;

    // Progress ring math (matches 72px orb)
    const R = 30, C = 2 * Math.PI * R;
    const offset = C - (C * (tier.progress_pct || 0) / 100);

    let orbInner = "";
    if (state === "completed") {
      orbInner = '<svg class="ms-step-check-svg" viewBox="0 0 24 24"><path d="M4 12l6 6L20 6"/></svg>';
    } else if (state === "active") {
      orbInner = `<svg class="ms-step-ring" viewBox="0 0 72 72"><circle class="ms-step-ring-bg" cx="36" cy="36" r="${R}"/><circle class="ms-step-ring-fill" cx="36" cy="36" r="${R}" stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${offset.toFixed(1)}"/></svg><span class="ms-step-pct-label">${Math.round(tier.progress_pct)}%</span>`;
    } else {
      orbInner = '<svg class="ms-step-lock" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/></svg>';
    }

    const rewardHtml = tier.reward ? `<div class="ms-step-reward">${tier.reward}</div>` : "";

    let detail = "";
    if (state === "active") {
      detail = `<div class="ms-step-detail">${formatUsdCompact(vol)} / ${amt}</div>`;
    } else if (state === "completed") {
      const completedAt = tier.completed_at ? new Date(tier.completed_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "";
      detail = `<div class="ms-step-detail">Completed${completedAt ? " on " + completedAt : ""}</div>`;
    }
    // Action button for Explorer when completed
    let actionHtml = "";
    if (state === "completed" && tier.title === "Explorer") {
      actionHtml = '<button class="ms-step-action" onclick="setReferralModalOpen(true)">Open Referral</button>';
    }

    // Connector height : same for all milestones
    let connector = "";
    const h = 350;
    // Fill level: completed=1, active=progress%, locked=0
    let fill = 0;
    if (state === "completed") fill = 1;
    else if (state === "active") fill = (tier.progress_pct || 0) / 100;
    connector = `<div class="ms-step-connector" style="--ms-connector-h:${h}px;--ms-fill:${fill.toFixed(3)}"></div>`;

    const step = document.createElement("div");
    step.className = `ms-step ms-step--${state}`;
    step.dataset.tier = key;
    step.style.animationDelay = `${(reversed.length - 1 - i) * 0.12 + 0.1}s`;

    step.innerHTML =
      `<div class="ms-step-node-wrap"><div class="ms-step-orb">${orbInner}</div>` +
      `<div class="ms-step-info">${rewardHtml}<div class="ms-step-amount">${amt}</div>` +
      `<div class="ms-step-title">${tier.title}</div>${detail}${actionHtml}</div></div>` +
      `<div class="ms-step-connector-col">${connector}</div>`;
    pathEl.appendChild(step);
  });

  // Add "Start" node at the very bottom
  const startStep = document.createElement("div");
  startStep.className = "ms-step ms-step--completed";
  startStep.dataset.tier = "start";
  startStep.style.animationDelay = `${reversed.length * 0.12 + 0.1}s`;
  startStep.innerHTML =
    '<div class="ms-step-node-wrap"><div class="ms-step-orb"><span style="color:rgba(255,255,255,0.4);font-size:18px">\u25B2</span></div>' +
    '<div class="ms-step-info"><div class="ms-step-amount">$0</div>' +
    '<div class="ms-step-title">Start</div>' +
    '<div class="ms-step-detail">Your journey begins here</div></div></div>';
  pathEl.appendChild(startStep);

  // Update pillar background color based on active tier
  const activeTierData = data.tiers.find(t => !t.completed);
  const newActiveTier = activeTierData ? (activeTierData.title || "").toLowerCase() : "legend";
  if (newActiveTier !== _msActiveTier) {
    _msActiveTier = newActiveTier;
    _msUpdatePillarColor(newActiveTier);
  }
}

function _updateMsBottomBar(vol, tiers) {
  const volEl = document.getElementById("ms_vol_value");
  const nextEl = document.getElementById("ms_next_label");
  const fillEl = document.getElementById("ms_progress_fill");
  const pctEl = document.getElementById("ms_pct");

  if (volEl) volEl.textContent = formatUsdCompact(vol || 0);

  if (!tiers || !tiers.length) {
    if (nextEl) nextEl.textContent = "$100K \u2014 Explorer";
    if (fillEl) fillEl.style.width = "0%";
    if (pctEl) pctEl.textContent = "0%";
    return;
  }

  const next = tiers.find(t => !t.completed);
  if (next) {
    const a = next.target >= 1e6 ? `$${next.target / 1e6}M` : `$${next.target / 1e3}K`;
    if (nextEl) nextEl.textContent = `${a} \u2014 ${next.title}`;
    if (fillEl) fillEl.style.width = `${next.progress_pct}%`;
    if (pctEl) pctEl.textContent = `${Math.round(next.progress_pct)}%`;
  } else {
    if (nextEl) nextEl.textContent = "All milestones reached!";
    if (fillEl) fillEl.style.width = "100%";
    if (pctEl) pctEl.textContent = "100%";
  }
}

/* ── Light Pillar background (WebGL shader) ───────── */
let _msGlState = null;

function _startMsParticles() {
  _stopMsParticles();

  // Reuse existing GL state (context survives close/reopen)
  if (_msGlState) {
    _msGlStartLoop();
    return;
  }

  const canvas = document.getElementById("ms_particles");
  if (!canvas) return;

  // Render at half resolution for performance
  const DPR = 0.5;
  function resize() {
    canvas.width = Math.round(window.innerWidth * DPR);
    canvas.height = Math.round(window.innerHeight * DPR);
    if (_msGlState) {
      _msGlState.gl.viewport(0, 0, canvas.width, canvas.height);
      _msGlState.gl.uniform2f(_msGlState.uRes, canvas.width, canvas.height);
    }
  }
  resize();

  const gl = canvas.getContext("webgl", { alpha: true, antialias: false, depth: false, stencil: false, premultipliedAlpha: false });
  if (!gl) return;
  gl.viewport(0, 0, canvas.width, canvas.height);

  const vsSrc = "attribute vec2 a_p;varying vec2 vUv;void main(){vUv=a_p*.5+.5;gl_Position=vec4(a_p,0,1);}";

  const fsSrc = `precision highp float;
uniform float uTime;
uniform vec2 uResolution;
uniform vec3 uTopColor;
uniform vec3 uBottomColor;
uniform float uIntensity;
uniform float uGlowAmount;
uniform float uPillarWidth;
uniform float uPillarHeight;
uniform float uNoiseIntensity;
uniform float uRotCos;
uniform float uRotSin;
uniform float uPillarRotCos;
uniform float uPillarRotSin;
uniform float uWaveSin;
uniform float uWaveCos;
varying vec2 vUv;
vec3 _tanh(vec3 x){vec3 e=exp(2.0*clamp(x,-10.0,10.0));return(e-1.0)/(e+1.0);}
void main(){
  vec2 uv=(vUv*2.0-1.0)*vec2(uResolution.x/uResolution.y,1.0);
  uv=vec2(uPillarRotCos*uv.x-uPillarRotSin*uv.y,uPillarRotSin*uv.x+uPillarRotCos*uv.y);
  vec3 ro=vec3(0,0,-10);
  vec3 rd=normalize(vec3(uv,1));
  vec3 col=vec3(0);
  float t=0.1;
  for(int i=0;i<60;i++){
    vec3 p=ro+rd*t;
    p.xz=vec2(uRotCos*p.x-uRotSin*p.z,uRotSin*p.x+uRotCos*p.z);
    vec3 q=p;
    q.y=p.y*uPillarHeight+uTime;
    float freq=1.0;float amp=1.0;
    for(int j=0;j<3;j++){
      q.xz=vec2(uWaveCos*q.x-uWaveSin*q.z,uWaveSin*q.x+uWaveCos*q.z);
      q+=cos(q.zxy*freq-uTime*float(j)*2.0)*amp;
      freq*=2.0;amp*=0.5;
    }
    float d=length(cos(q.xz))-0.2;
    float bound=length(p.xz)-uPillarWidth;
    float k=4.0;
    float h=max(k-abs(d-bound),0.0);
    d=max(d,bound)+h*h*0.0625/k;
    d=abs(d)*0.15+0.01;
    float grad=clamp((15.0-p.y)/30.0,0.0,1.0);
    col+=mix(uBottomColor,uTopColor,grad)/d;
    t+=d;
    if(t>50.0)break;
  }
  col=_tanh(col*uGlowAmount/(uPillarWidth/3.0));
  col-=fract(sin(dot(gl_FragCoord.xy,vec2(12.9898,78.233)))*43758.5453)/15.0*uNoiseIntensity;
  gl_FragColor=vec4(col*uIntensity,1);
}`;

  function mkShader(type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      console.warn("[LightPillar] shader error:", gl.getShaderInfoLog(s));
      gl.deleteShader(s);
      return null;
    }
    return s;
  }
  const vs = mkShader(gl.VERTEX_SHADER, vsSrc);
  const fs = mkShader(gl.FRAGMENT_SHADER, fsSrc);
  if (!vs || !fs) return;

  const prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    console.warn("[LightPillar] link error:", gl.getProgramInfoLog(prog));
    return;
  }
  gl.useProgram(prog);

  // Full-screen quad
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
  const aP = gl.getAttribLocation(prog, "a_p");
  gl.enableVertexAttribArray(aP);
  gl.vertexAttribPointer(aP, 2, gl.FLOAT, false, 0, 0);

  // Uniforms
  const uTime = gl.getUniformLocation(prog, "uTime");
  const uRes  = gl.getUniformLocation(prog, "uResolution");
  const uRC   = gl.getUniformLocation(prog, "uRotCos");
  const uRS   = gl.getUniformLocation(prog, "uRotSin");
  const uTopCol = gl.getUniformLocation(prog, "uTopColor");
  const uBotCol = gl.getUniformLocation(prog, "uBottomColor");

  const pillarRotRad = 159 * Math.PI / 180;
  gl.uniform2f(uRes, canvas.width, canvas.height);
  gl.uniform3f(gl.getUniformLocation(prog, "uTopColor"), 0.776, 0.459, 0.165);
  gl.uniform3f(gl.getUniformLocation(prog, "uBottomColor"), 0.929, 0.502, 0.008);
  gl.uniform1f(gl.getUniformLocation(prog, "uIntensity"), 0.6);
  gl.uniform1f(gl.getUniformLocation(prog, "uGlowAmount"), 0.004);
  gl.uniform1f(gl.getUniformLocation(prog, "uPillarWidth"), 4.8);
  gl.uniform1f(gl.getUniformLocation(prog, "uPillarHeight"), 0.4);
  gl.uniform1f(gl.getUniformLocation(prog, "uNoiseIntensity"), 0.5);
  gl.uniform1f(gl.getUniformLocation(prog, "uPillarRotCos"), Math.cos(pillarRotRad));
  gl.uniform1f(gl.getUniformLocation(prog, "uPillarRotSin"), Math.sin(pillarRotRad));
  gl.uniform1f(gl.getUniformLocation(prog, "uWaveSin"), Math.sin(0.4));
  gl.uniform1f(gl.getUniformLocation(prog, "uWaveCos"), Math.cos(0.4));

  _msGlState = { gl, prog, vs, fs, buf, uRes, uTime, uRC, uRS, uTopCol, uBotCol, time: 0, resizeFn: resize };
  _msGlStartLoop();
}

function _msGlStartLoop() {
  if (!_msGlState) return;
  window.addEventListener("resize", _msGlState.resizeFn, { passive: true });
  _msGlState.resizeFn();
  const st = _msGlState;
  function draw() {
    st.time += 0.016;
    st.gl.uniform1f(st.uTime, st.time);
    st.gl.uniform1f(st.uRC, Math.cos(st.time * 0.3));
    st.gl.uniform1f(st.uRS, Math.sin(st.time * 0.3));
    st.gl.drawArrays(st.gl.TRIANGLE_STRIP, 0, 4);
    msParticlesRaf = requestAnimationFrame(draw);
  }
  msParticlesRaf = requestAnimationFrame(draw);
}

function _stopMsParticles() {
  if (msParticlesRaf) { cancelAnimationFrame(msParticlesRaf); msParticlesRaf = null; }
  if (_msGlState) {
    window.removeEventListener("resize", _msGlState.resizeFn);
  }
}

/* ── Spotlight intro animation ────────────────────── */
function _msIntroSmoothScrollWithProgress(target, duration, pFrom, pTo, fillEl, pctEl) {
  return new Promise(resolve => {
    if (_msIntroAborted) { resolve(); return; }
    const page = document.getElementById("ms_page");
    if (!page) { resolve(); return; }
    const start = page.scrollTop;
    const delta = target - start;
    const t0 = performance.now();
    function tick(now) {
      if (_msIntroAborted) { resolve(); return; }
      const p = Math.min((now - t0) / duration, 1);
      const ease = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
      page.scrollTop = start + delta * ease;
      const prog = pFrom + (pTo - pFrom) * ease;
      const pct = Math.round(prog * 100);
      if (fillEl) fillEl.style.width = `${pct}%`;
      if (pctEl) pctEl.textContent = `${pct}%`;
      _msIntroUpdateVerticalFill(prog);
      if (p < 1) requestAnimationFrame(tick); else resolve();
    }
    requestAnimationFrame(tick);
  });
}

function _msIntroUpdateVerticalFill(globalProgress) {
  const connectors = document.querySelectorAll(".ms-intro-active .ms-step-connector");
  const n = connectors.length;
  if (!n) return;
  for (let i = 0; i < n; i++) {
    const rank = (n - 1 - i) / n;
    const segStart = rank;
    const segEnd = segStart + 1 / n;
    const fill = Math.max(0, Math.min(1, (globalProgress - segStart) / (segEnd - segStart)));
    connectors[i].style.setProperty("--ms-fill", fill.toFixed(3));
  }
}

function _msIntroSmoothScroll(target, duration) {
  return new Promise(resolve => {
    if (_msIntroAborted) { resolve(); return; }
    const page = document.getElementById("ms_page");
    if (!page) { resolve(); return; }
    const start = page.scrollTop;
    const delta = target - start;
    if (Math.abs(delta) < 2) { resolve(); return; }
    const t0 = performance.now();
    function tick(now) {
      if (_msIntroAborted) { resolve(); return; }
      const p = Math.min((now - t0) / duration, 1);
      const ease = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
      page.scrollTop = start + delta * ease;
      if (p < 1) requestAnimationFrame(tick); else resolve();
    }
    requestAnimationFrame(tick);
  });
}

function _msIntroMoveSpot(overlay, toX, toY, toR, duration) {
  return new Promise(resolve => {
    if (_msIntroAborted) { resolve(); return; }
    const s = overlay.style;
    const fromX = parseFloat(s.getPropertyValue("--spot-x")) || window.innerWidth / 2;
    const fromY = parseFloat(s.getPropertyValue("--spot-y")) || window.innerHeight + 100;
    const fromR = parseFloat(s.getPropertyValue("--spot-r-val")) || 140;
    const t0 = performance.now();
    function tick(now) {
      if (_msIntroAborted) { resolve(); return; }
      const p = Math.min((now - t0) / duration, 1);
      const ease = 1 - Math.pow(1 - p, 3);
      const cx = fromX + (toX - fromX) * ease;
      const cy = fromY + (toY - fromY) * ease;
      const cr = fromR + (toR - fromR) * ease;
      s.setProperty("--spot-x", cx + "px");
      s.setProperty("--spot-y", cy + "px");
      s.setProperty("--spot-r-val", String(cr));
      s.setProperty("--spot-r", cr + "px");
      if (p < 1) requestAnimationFrame(tick); else resolve();
    }
    requestAnimationFrame(tick);
  });
}

function _msIntroPause(ms) {
  return new Promise(resolve => {
    if (_msIntroAborted) { resolve(); return; }
    const id = setTimeout(resolve, ms);
    _msIntroPauseTimers.push(id);
  });
}

function _msIntroCountUp(el, target, duration) {
  if (!el || _msIntroAborted) return;
  const fmt = target >= 1e6 ? (v) => `$${(v / 1e6).toFixed(v >= 1e6 ? 0 : 1)}M`
            : (v) => `$${(v / 1e3).toFixed(v >= 1e3 ? 0 : 0)}K`;
  const t0 = performance.now();
  function tick(now) {
    if (_msIntroAborted) { el.textContent = fmt(target); return; }
    const p = Math.min((now - t0) / duration, 1);
    const ease = 1 - Math.pow(1 - p, 3);
    el.textContent = fmt(Math.round(target * ease));
    if (p < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function _msIntroNodeCenter(tierName) {
  const step = document.querySelector(`.ms-step[data-tier="${tierName}"]`);
  if (!step) return { x: window.innerWidth / 2, y: window.innerHeight / 2 };
  const orb = step.querySelector(".ms-step-orb");
  const rect = (orb || step).getBoundingClientRect();
  return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
}

/* ── Live refresh (polls every 5s when page is open) ── */
function _msStartLiveTimer() {
  _msStopLiveTimer();
  _msLiveTimer = setInterval(_msLiveRefresh, 5000);
}
function _msStopLiveTimer() {
  if (_msLiveTimer) { clearInterval(_msLiveTimer); _msLiveTimer = null; }
}
async function _msLiveRefresh() {
  if (!msPageOpen || _msIntroRunning) return;
  const account = document.getElementById("account_address")?.value?.trim() || "";
  if (!account) return;
  try {
    const data = await statsApiGet(`/api/milestones?account=${encodeURIComponent(account)}`);
    const newVol = data.volume_usd || 0;
    const oldVol = _msLastVolume;
    _msLastVolume = newVol;

    // Update bottom bar
    _updateMsBottomBar(newVol, data.tiers);

    // Flash % if changed
    if (oldVol !== null && newVol !== oldVol) {
      const pctEl = document.getElementById("ms_pct");
      if (pctEl) {
        pctEl.classList.remove("ms-pct-flash");
        void pctEl.offsetWidth;
        pctEl.classList.add("ms-pct-flash");
        setTimeout(() => pctEl.classList.remove("ms-pct-flash"), 500);
      }
    }

    // Check for newly completed milestones
    const newCompleted = new Set();
    (data.tiers || []).forEach(t => { if (t.completed) newCompleted.add(t.target); });
    for (const target of newCompleted) {
      if (!_msLastCompleted.has(target)) {
        const tier = data.tiers.find(t => t.target === target);
        const key = (tier?.title || "").toLowerCase();
        const step = document.querySelector(`.ms-step[data-tier="${key}"]`);
        if (step) _msCelebrateMilestone(step);
        // Gold confetti burst for Legend
        if (key === "legend") {
          const field = document.createElement("div");
          field.className = "ms-intro-confetti-field";
          document.getElementById("ms_page")?.appendChild(field);
          _msIntroGoldBurst(field);
          setTimeout(() => field.remove(), 6000);
        }
        // Global toast notification (visible on any page)
        const amt = target >= 1e6 ? `$${target / 1e6}M` : `$${target / 1e3}K`;
        _msShowToast(`Milestone reached: ${amt} : ${tier?.title || ""}`, "success");
      }
    }
    _msLastCompleted = newCompleted;

    // Update connector fills
    (data.tiers || []).forEach(t => {
      const key = (t.title || "").toLowerCase();
      const step = document.querySelector(`.ms-step[data-tier="${key}"]`);
      if (!step) return;
      const conn = step.querySelector(".ms-step-connector");
      if (!conn) return;
      let fill = 0;
      if (t.completed) fill = 1;
      else if (t.progress_pct > 0) fill = t.progress_pct / 100;
      conn.style.setProperty("--ms-fill", fill.toFixed(3));
    });

    // Update pillar color if tier changed
    const activeTier = (data.tiers || []).find(t => !t.completed);
    const tierKey = activeTier ? (activeTier.title || "").toLowerCase() : "legend";
    if (tierKey !== _msActiveTier) {
      _msActiveTier = tierKey;
      _msUpdatePillarColor(tierKey);
    }
  } catch (_) {}
}

/* ── LightPillar color per tier ───────────────────── */
const _MS_PILLAR_COLORS = {
  explorer: [[0.776, 0.459, 0.165], [0.929, 0.502, 0.008]],
  dolphin:  [[0.165, 0.361, 0.776], [0.008, 0.376, 0.929]],
  shark:    [[0.420, 0.165, 0.776], [0.545, 0.008, 0.929]],
  whale:    [[0.776, 0.459, 0.165], [0.929, 0.314, 0.008]],
  legend:   [[0.776, 0.631, 0.165], [0.929, 0.816, 0.008]],
};

function _msUpdatePillarColor(tierKey) {
  if (!_msGlState || !_msGlState.uTopCol || !_msGlState.uBotCol) return;
  const colors = _MS_PILLAR_COLORS[tierKey] || _MS_PILLAR_COLORS.explorer;
  const gl = _msGlState.gl;
  gl.useProgram(_msGlState.prog);
  gl.uniform3f(_msGlState.uTopCol, colors[0][0], colors[0][1], colors[0][2]);
  gl.uniform3f(_msGlState.uBotCol, colors[1][0], colors[1][1], colors[1][2]);
}

/* ── Preload milestones data ──────────────────────── */
function _msPreload(account) {
  if (!account) return;
  statsApiGet(`/api/milestones?account=${encodeURIComponent(account)}`)
    .then(d => { _msCachedData = d; })
    .catch(() => {});
}

/** Affiche le bouton Milestones uniquement pour les utilisateurs éligibles. */
function _applyMilestonesNavVisibility() {
  const btn = document.getElementById("menu_open_milestones");
  if (btn) btn.classList.toggle("hidden", !_msEligible);
}

/** Détermine l'éligibilité d'un compte (volume > 0 depuis MILESTONES_START) et
 *  masque/affiche le bouton Milestones en conséquence. */
function refreshMilestonesEligibility(account) {
  const acct = String(account || "").trim();
  if (!acct) {
    _msEligible = false;
    _applyMilestonesNavVisibility();
    return;
  }
  statsApiGet(`/api/milestones?account=${encodeURIComponent(acct)}`)
    .then(d => {
      _msCachedData = d;
      _msEligible = !!(d && (d.eligible ?? (Number(d.volume_usd) > 0)));
      _applyMilestonesNavVisibility();
    })
    .catch(() => { /* en cas d'échec, on laisse le bouton masqué */ });
}

/* ── Annonce one-shot "Tradoor Points are LIVE!" ───────── */
const _POINTS_LIVE_KEY = "tpoints_live_popup_v1";
let _pointsLiveRaf = null;

function _pointsLiveStopConfetti() {
  if (_pointsLiveRaf) { cancelAnimationFrame(_pointsLiveRaf); _pointsLiveRaf = null; }
}

function _pointsLiveConfetti() {
  _pointsLiveStopConfetti();
  const canvas = document.getElementById("points_live_confetti");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const r = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, r.width);
  canvas.height = Math.max(1, r.height);
  const colors = ["#ff8c3c", "#ffd9b0", "#35f0c2", "#ffffff", "#ff5a1e"];
  const parts = [];
  for (let i = 0; i < 90; i++) {
    parts.push({
      x: canvas.width / 2, y: canvas.height * 0.32,
      vx: (Math.random() - 0.5) * 9,
      vy: Math.random() * -8 - 2,
      s: Math.random() * 3 + 2,
      color: colors[Math.floor(Math.random() * colors.length)],
      life: 1,
    });
  }
  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    let alive = false;
    for (const p of parts) {
      p.vy += 0.22; // gravite
      p.x += p.vx; p.y += p.vy; p.life -= 0.008;
      if (p.life > 0 && p.y < canvas.height + 12) {
        alive = true;
        ctx.globalAlpha = Math.max(0, p.life);
        ctx.fillStyle = p.color;
        ctx.fillRect(p.x, p.y, p.s, p.s);
      }
    }
    ctx.globalAlpha = 1;
    _pointsLiveRaf = alive ? requestAnimationFrame(draw) : null;
  }
  _pointsLiveRaf = requestAnimationFrame(draw);
}

function setPointsLiveModalOpen(open) {
  const modal = document.getElementById("points_live_modal");
  const overlay = document.getElementById("points_live_overlay");
  if (!modal) return;
  modal.classList.toggle("hidden", !open);
  modal.setAttribute("aria-hidden", open ? "false" : "true");
  if (overlay) {
    overlay.classList.toggle("hidden", !open);
    overlay.setAttribute("aria-hidden", open ? "false" : "true");
  }
  if (open) _pointsLiveConfetti(); else _pointsLiveStopConfetti();
}

/** Affiche l'annonce une seule fois par navigateur, a la connexion d'un user,
 *  mais seulement si la saison de points est reellement lancee (sinon on
 *  re-verifiera a la prochaine connexion sans "bruler" l'affichage unique). */
async function maybeShowPointsLivePopup() {
  try { if (localStorage.getItem(_POINTS_LIVE_KEY) === "1") return; } catch (_) {}
  try {
    const resp = await fetchWithTimeout("/api/points/me", { headers: authHeaders() });
    if (!resp.ok) return;
    const d = await resp.json();
    if (!d || d.season_started !== true) return;
  } catch (_) { return; }
  try { localStorage.setItem(_POINTS_LIVE_KEY, "1"); } catch (_) {}
  setPointsLiveModalOpen(true);
}

function _msCelebrateMilestone(step) {
  if (!step) return;
  const orb = step.querySelector(".ms-step-orb");
  if (!orb) return;
  const color = getComputedStyle(step).getPropertyValue("--ms-color").trim() || "#ff8c3c";

  // 1. Orb pulse flash + check draw
  orb.classList.remove("ms-orb-celebrate");
  void orb.offsetWidth;
  orb.classList.add("ms-orb-celebrate");

  // 2. Shockwave ring
  const ring = document.createElement("div");
  ring.className = "ms-burst-ring";
  orb.appendChild(ring);
  setTimeout(() => ring.remove(), 800);

  // 3. Burst particles (radial explosion)
  const field = document.createElement("div");
  field.className = "ms-burst-field";
  orb.appendChild(field);
  for (let i = 0; i < 35; i++) {
    const p = document.createElement("div");
    p.className = "ms-burst-particle";
    const angle = Math.random() * Math.PI * 2;
    const dist = 40 + Math.random() * 60;
    const dx = Math.cos(angle) * dist;
    const dy = Math.sin(angle) * dist;
    const size = 3 + Math.random() * 5;
    const dur = 0.5 + Math.random() * 0.5;
    p.style.cssText = `width:${size}px;height:${size}px;background:${color};box-shadow:0 0 4px ${color};--dx:${dx.toFixed(0)}px;--dy:${dy.toFixed(0)}px;--dur:${dur.toFixed(2)}s`;
    field.appendChild(p);
  }
  setTimeout(() => field.remove(), 1500);
}

function _msIntroGoldBurst(container) {
  const colors = ["#ffd700", "#ffb800", "#fff4b0", "#ff9500", "#ffe066"];
  for (let i = 0; i < 80; i++) {
    const p = document.createElement("div");
    p.className = "ms-intro-confetti";
    const dur = 2.5 + Math.random() * 2;
    const delay = Math.random() * 0.3;
    p.style.cssText = `left:${30 + Math.random() * 40}%;width:${5 + Math.random() * 8}px;height:${4 + Math.random() * 7}px;background:${colors[Math.floor(Math.random() * colors.length)]};--dur:${dur}s;--delay:${delay}s;animation-delay:${delay}s`;
    container.appendChild(p);
    setTimeout(() => p.remove(), (dur + delay + 0.5) * 1000);
  }
}

async function _msSpotlightIntro() {
  if (_msIntroRunning) return;
  _msIntroRunning = true;
  _msIntroAborted = false;
  _msIntroPauseTimers = [];

  const page = document.getElementById("ms_page");
  if (!page) { _msIntroRunning = false; return; }

  // 1. Render milestones (populate DOM)
  await refreshMilestones();

  // 2. Suppress normal entry animations
  const pageInner = document.getElementById("ms_page_inner");
  if (pageInner) pageInner.classList.add("ms-intro-active");

  // Keep bottom bar visible during intro : show progress
  const bar = document.getElementById("ms_bottom_bar");
  if (bar) bar.classList.remove("hidden");

  // 3. Create overlay elements
  const overlay = document.createElement("div");
  overlay.className = "ms-intro-overlay";
  overlay.style.setProperty("--spot-x", `${window.innerWidth / 2}px`);
  overlay.style.setProperty("--spot-y", `${window.innerHeight + 100}px`);
  overlay.style.setProperty("--spot-r", "320px");
  overlay.style.setProperty("--spot-r-val", "320");
  page.appendChild(overlay);

  const flashEl = document.createElement("div");
  flashEl.className = "ms-intro-flash";
  page.appendChild(flashEl);

  const confettiField = document.createElement("div");
  confettiField.className = "ms-intro-confetti-field";
  page.appendChild(confettiField);

  const skipBtn = document.createElement("div");
  skipBtn.className = "ms-intro-skip";
  skipBtn.textContent = "Skip";
  page.appendChild(skipBtn);

  const tapHint = document.createElement("div");
  tapHint.className = "ms-intro-tap-hint";
  tapHint.textContent = "Tap anywhere to continue";
  page.appendChild(tapHint);

  // 4. Cleanup function
  function cleanup() {
    _msIntroAborted = true;
    _msIntroPauseTimers.forEach(id => clearTimeout(id));
    _msIntroPauseTimers = [];

    document.querySelectorAll(".ms-step").forEach(s => {
      s.classList.add("ms-intro-revealed");
      s.classList.remove("ms-intro-revealing");
    });
    if (pageInner) pageInner.classList.remove("ms-intro-active");
    document.querySelectorAll(".ms-step-connector").forEach(c => c.style.removeProperty("--ms-fill"));

    overlay.classList.add("fade-out");
    setTimeout(() => {
      overlay.remove();
      flashEl.remove();
      confettiField.remove();
      skipBtn.remove();
      tapHint.remove();
    }, 900);

    try { localStorage.setItem("ms_intro_done", "1"); } catch (_) {}

    // Reset bottom bar to real values
    refreshMilestones();

    setTimeout(() => {
      const active = document.querySelector(".ms-step--active");
      if (active) active.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 500);

    _msIntroRunning = false;
  }
  _msIntroCleanupFn = cleanup;
  skipBtn.addEventListener("click", cleanup);

  // 5. Tier order bottom to top
  const tierOrder = ["start", "explorer", "dolphin", "shark", "whale", "legend"];
  const fillEl = document.getElementById("ms_progress_fill");
  const pctEl = document.getElementById("ms_pct");

  // 6. Scroll to bottom instantly
  page.scrollTop = page.scrollHeight;
  await _msIntroPause(500);

  // 7. Spotlight sequence
  for (let i = 0; i < tierOrder.length; i++) {
    if (_msIntroAborted) break;
    const tier = tierOrder[i];
    const step = document.querySelector(`.ms-step[data-tier="${tier}"]`);
    if (!step) continue;

    // Scroll so node is centered : with smooth progress bar fill during scroll
    const stepRect = step.getBoundingClientRect();
    const pageRect = page.getBoundingClientRect();
    const targetScroll = page.scrollTop + stepRect.top - pageRect.top - window.innerHeight / 2 + stepRect.height / 2;
    const scrollDur = i === 0 ? 300 : 800;
    const pTo = i / (tierOrder.length - 1);
    const pFrom = i === 0 ? 0 : (i - 1) / (tierOrder.length - 1);

    await _msIntroSmoothScrollWithProgress(Math.max(0, targetScroll), scrollDur, pFrom, pTo, fillEl, pctEl);
    // Ensure fill is exact after scroll
    _msIntroUpdateVerticalFill(pTo);
    if (_msIntroAborted) break;

    // Move spotlight to node
    const center = _msIntroNodeCenter(tier);
    const spotR = tier === "legend" ? 360 : 320;
    await _msIntroMoveSpot(overlay, center.x, center.y, spotR, 400);
    if (_msIntroAborted) break;

    // Reveal milestone
    step.classList.add("ms-intro-revealing");
    step.classList.add("ms-intro-revealed");

    // Celebration effect (burst + check + ring)
    if (tier !== "start") {
      _msCelebrateMilestone(step);
    }

    // Counter effect on the amount
    if (tier !== "start") {
      const amtEl = step.querySelector(".ms-step-amount");
      const tierData = tierOrder[i];
      const targets = { explorer: 100000, dolphin: 1000000, shark: 5000000, whale: 10000000, legend: 100000000 };
      _msIntroCountUp(amtEl, targets[tier] || 0, 700);
    }

    if (tier === "legend") {
      await _msIntroPause(500);
      if (_msIntroAborted) break;
      // Golden flash
      flashEl.classList.remove("active");
      void flashEl.offsetWidth;
      flashEl.classList.add("active");
      // Confetti burst
      _msIntroGoldBurst(confettiField);
      // Expand spotlight to fill screen
      await _msIntroMoveSpot(overlay, center.x, center.y, Math.max(window.innerWidth, window.innerHeight) * 1.5, 1200);
      if (_msIntroAborted) break;
      await _msIntroPause(600);
    } else {
      await _msIntroPause(1000);
    }
  }

  if (!_msIntroAborted) {
    // 8. Show tap hint
    tapHint.classList.add("visible");
    skipBtn.remove();

    await new Promise(resolve => {
      function onClick() { page.removeEventListener("click", onClick); resolve(); }
      page.addEventListener("click", onClick);
      const check = setInterval(() => {
        if (_msIntroAborted) { clearInterval(check); page.removeEventListener("click", onClick); resolve(); }
      }, 200);
    });

    cleanup();
  }
}

function _msIntroCleanup() {
  if (_msIntroRunning && _msIntroCleanupFn) _msIntroCleanupFn();
}


function setHyperspaceVars(
  zoom = 1,
  brightness = 1,
  saturation = 1,
  streakOpacity = 0,
  glowOpacity = 0,
  vignetteOpacity = 0,
  flashOpacity = 0,
  shootingOpacity = 1,
) {
  const style = document.documentElement.style;
  style.setProperty("--hs-zoom", String(zoom));
  style.setProperty("--hs-brightness", String(brightness));
  style.setProperty("--hs-saturation", String(saturation));
  style.setProperty("--hs-streak-opacity", String(streakOpacity));
  style.setProperty("--hs-glow-opacity", String(glowOpacity));
  style.setProperty("--hs-vignette-opacity", String(vignetteOpacity));
  style.setProperty("--hs-flash-opacity", String(flashOpacity));
  style.setProperty("--hs-shoot-opacity", String(shootingOpacity));
}

function resetHyperspaceVars() {
  const style = document.documentElement.style;
  style.removeProperty("--hs-zoom");
  style.removeProperty("--hs-brightness");
  style.removeProperty("--hs-saturation");
  style.removeProperty("--hs-streak-opacity");
  style.removeProperty("--hs-glow-opacity");
  style.removeProperty("--hs-vignette-opacity");
  style.removeProperty("--hs-flash-opacity");
  style.removeProperty("--hs-shoot-opacity");
}

function resizeHyperspaceCanvas(canvas, ctx) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.max(1, Math.round(window.innerWidth));
  const height = Math.max(1, Math.round(window.innerHeight));
  const targetWidth = Math.max(1, Math.round(width * dpr));
  const targetHeight = Math.max(1, Math.round(height * dpr));
  if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
    canvas.width = targetWidth;
    canvas.height = targetHeight;
  }
  if (ctx) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  return { width, height };
}

function createHyperspaceStars(count) {
  const stars = new Array(count);
  for (let i = 0; i < count; i += 1) {
    stars[i] = {
      angle: Math.random() * Math.PI * 2,
      radial: Math.random() * 0.2,
      depth: 0.18 + Math.random() * 0.92,
      speed: 0.6 + Math.random() * 1.8,
      width: 0.28 + Math.random() * 1.7,
      length: 0.35 + Math.random() * 1.05,
      warm: Math.random() > 0.78,
    };
  }
  return stars;
}

function respawnHyperspaceStar(star, forceCenter = false) {
  star.angle = Math.random() * Math.PI * 2;
  star.radial = forceCenter ? Math.random() * 0.03 : Math.random() * 0.05;
  star.depth = 0.18 + Math.random() * 0.92;
  star.speed = 0.6 + Math.random() * 1.8;
  star.width = 0.28 + Math.random() * 1.7;
  star.length = 0.35 + Math.random() * 1.05;
  star.warm = Math.random() > 0.78;
}

function advanceHyperspaceStars(stars, dtSec, warpStrength) {
  if (!stars || !stars.length) return;
  const speed = HS_STREAK_SPEED_BASE + (HS_STREAK_SPEED_MAX - HS_STREAK_SPEED_BASE) * Math.pow(warpStrength, 2.1);
  for (let i = 0; i < stars.length; i += 1) {
    const s = stars[i];
    s.radial += dtSec * speed * (0.5 + s.depth * 0.85) * s.speed;
    if (s.radial > 1.16) {
      respawnHyperspaceStar(s, true);
    }
  }
}

function drawHyperspaceStreaks(ctx, stars, width, height, warpStrength) {
  ctx.clearRect(0, 0, width, height);
  if (warpStrength <= 0.02) return;
  const cx = width * 0.5;
  const cy = height * 0.5;
  const radius = Math.max(width, height) * 0.9;
  const streakLenBase = 6 + 240 * Math.pow(warpStrength, 1.65);
  ctx.save();
  ctx.translate(cx, cy);
  ctx.lineCap = "round";
  ctx.shadowBlur = 16 * warpStrength;
  ctx.shadowColor = "rgba(168, 224, 255, 0.92)";

  for (let i = 0; i < stars.length; i += 1) {
    const s = stars[i];
    const cosA = Math.cos(s.angle);
    const sinA = Math.sin(s.angle);
    const distance = Math.pow(Math.max(0, s.radial), 1.55) * radius;
    const length = streakLenBase * (0.45 + s.length) * (0.6 + s.radial * 0.9);
    const x1 = cosA * distance;
    const y1 = sinA * distance;
    const x0 = x1 - cosA * length;
    const y0 = y1 - sinA * length;
    const alpha = (0.1 + 0.92 * warpStrength) * (0.25 + s.depth * 0.75) * (0.35 + Math.min(1, s.radial));

    ctx.globalAlpha = alpha;
    ctx.lineWidth = (0.42 + s.width * 1.75) * (0.45 + warpStrength * 1.3);
    ctx.strokeStyle = s.warm ? "rgba(255, 188, 146, 1)" : "rgba(182, 232, 255, 1)";
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
  }

  ctx.restore();
}

function onHyperspaceComplete(activateMainPage) {
  if (typeof activateMainPage === "function") {
    try {
      activateMainPage();
    } catch (_) {
      // Keep transition cleanup deterministic even if the callback fails.
    }
  }
}

function startHyperspaceTransition() {
  if (hyperspaceTransitionInFlight) return null;

  const overlay = document.getElementById("hyperspace_overlay");
  const canvas = document.getElementById("hyperspace_canvas");
  if (!overlay || !canvas) {
    return {
      finish: async (activateMainPage) => {
        onHyperspaceComplete(activateMainPage);
      },
      cancel: async () => {},
    };
  }

  hyperspaceTransitionInFlight = true;
  const prefersReducedMotion =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const ctx = prefersReducedMotion ? null : canvas.getContext("2d");
  const stars = prefersReducedMotion ? null : createHyperspaceStars(300);
  const PRE_MS = HS_PRE_MS;
  const WARP_MS = HS_WARP_MS;
  const RELEASE_MS = prefersReducedMotion ? 150 : HS_RELEASE_MS;
  const CANCEL_MS = 160;

  let rafId = 0;
  let viewportWidth = 0;
  let viewportHeight = 0;
  let switchedToMain = false;
  let releaseRequested = false;
  let cancelRequested = false;
  let releaseStartedAt = 0;
  let cancelStartedAt = 0;
  let activateMainPageCb = null;

  let lastZoom = 1;
  let lastBrightness = 1;
  let lastSaturation = 1;
  let lastStreakOpacity = 0;
  let lastGlowOpacity = 0;
  let lastVignetteOpacity = 0;
  let lastShootingOpacity = 1;
  let lastWarpStrength = 0;
  let lastFrameTs = 0;

  let releaseFrom = null;
  let resolveDone;
  const donePromise = new Promise((resolve) => {
    resolveDone = resolve;
  });

  const applyAndRemember = (
    zoom,
    brightness,
    saturation,
    streakOpacity,
    glowOpacity,
    vignetteOpacity,
    flashOpacity,
    shootingOpacity,
    warpStrength,
  ) => {
    lastZoom = zoom;
    lastBrightness = brightness;
    lastSaturation = saturation;
    lastStreakOpacity = streakOpacity;
    lastGlowOpacity = glowOpacity;
    lastVignetteOpacity = vignetteOpacity;
    lastShootingOpacity = shootingOpacity;
    lastWarpStrength = warpStrength;
    setHyperspaceVars(
      zoom,
      brightness,
      saturation,
      streakOpacity,
      glowOpacity,
      vignetteOpacity,
      flashOpacity,
      shootingOpacity,
    );
  };

  const cleanup = () => {
    if (rafId) cancelAnimationFrame(rafId);
    if (ctx) {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
    }
    overlay.classList.remove("active");
    document.body.classList.remove("hyperspace-transitioning");
    document.body.classList.remove("hyperspace-reduced");
    resetHyperspaceVars();
    hyperspaceTransitionInFlight = false;
    resolveDone();
  };

  const easeOut = (t) => 1 - Math.pow(1 - t, 3);
  const easeIn = (t) => t * t * t;
  const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const introStartedAt = performance.now();

  document.body.classList.add("hyperspace-transitioning");
  if (prefersReducedMotion) document.body.classList.add("hyperspace-reduced");
  overlay.classList.add("active");
  // Prime initial state to avoid first-frame zoom hitch.
  applyAndRemember(1, 1, 1, 0, 0, 0, 0, 1, 0);

  if (ctx) {
    const size = resizeHyperspaceCanvas(canvas, ctx);
    viewportWidth = size.width;
    viewportHeight = size.height;
  }

  const tick = (now) => {
    const frameDtSec = Math.min(0.05, Math.max(0.001, (now - (lastFrameTs || now)) / 1000));
    lastFrameTs = now;
    let flashOpacity = 0;
    let warpStrength = lastWarpStrength;

    if (cancelRequested) {
      if (!cancelStartedAt) cancelStartedAt = now;
      const t = Math.min(1, (now - cancelStartedAt) / CANCEL_MS);
      const k = 1 - t;
      applyAndRemember(
        lastZoom * k + (1 - k) * 1,
        lastBrightness * k + (1 - k) * 1,
        lastSaturation * k + (1 - k) * 1,
        lastStreakOpacity * k,
        lastGlowOpacity * k,
        lastVignetteOpacity * k,
        0,
        lastShootingOpacity * k + (1 - k) * 1,
        lastWarpStrength * k,
      );
      warpStrength = lastWarpStrength * k;
      if (ctx && stars) {
        advanceHyperspaceStars(stars, frameDtSec, warpStrength);
        drawHyperspaceStreaks(ctx, stars, viewportWidth, viewportHeight, warpStrength);
      }
      if (t >= 1) {
        cleanup();
        return;
      }
      rafId = requestAnimationFrame(tick);
      return;
    }

    if (!releaseRequested) {
      const elapsed = now - introStartedAt;
      if (elapsed <= PRE_MS) {
        const t = easeIn(Math.min(1, elapsed / PRE_MS));
        const zoom = 1 + (HS_MAX_ZOOM - 1) * 0.34 * t;
        const brightness = 1 + 0.32 * t;
        const saturation = 1 + 0.24 * t;
        const streakOpacity = 0.18 * t;
        const glowOpacity = 0.24 * t;
        const vignetteOpacity = 0.14 * t;
        const shootingOpacity = 1 + 0.1 * t;
        warpStrength = 0.2 * t;
        applyAndRemember(
          zoom,
          brightness,
          saturation,
          streakOpacity,
          glowOpacity,
          vignetteOpacity,
          0,
          shootingOpacity,
          warpStrength,
        );
      } else if (elapsed <= PRE_MS + WARP_MS) {
        const tLinear = Math.min(1, (elapsed - PRE_MS) / WARP_MS);
        const t = easeInOut(tLinear);
        const speedRamp = easeIn(tLinear);
        const zoom = 1 + (HS_MAX_ZOOM - 1) * t;
        const brightness = 1.32 + 1.3 * t;
        const saturation = 1.24 + 0.74 * t;
        const streakOpacity = 0.18 + 0.82 * t;
        const glowOpacity = 0.24 + 0.58 * t;
        const vignetteOpacity = 0.14 + (HS_VIGNETTE_MAX - 0.14) * t;
        const shootingOpacity = 1.1 + 0.95 * t;
        warpStrength = 0.2 + 0.8 * speedRamp;
        applyAndRemember(
          zoom,
          brightness,
          saturation,
          streakOpacity,
          glowOpacity,
          vignetteOpacity,
          0,
          shootingOpacity,
          warpStrength,
        );
      } else {
        const zoom = HS_MAX_ZOOM;
        const brightness = 2.62;
        const saturation = 1.98;
        const streakOpacity = 1;
        const glowOpacity = 0.84;
        const vignetteOpacity = HS_VIGNETTE_MAX;
        const shootingOpacity = 2.0;
        warpStrength = 1;
        applyAndRemember(
          zoom,
          brightness,
          saturation,
          streakOpacity,
          glowOpacity,
          vignetteOpacity,
          0,
          shootingOpacity,
          warpStrength,
        );
      }
    } else {
      if (!releaseStartedAt) releaseStartedAt = now;
      const tRaw = Math.min(1, (now - releaseStartedAt) / RELEASE_MS);
      const from = releaseFrom || {
        zoom: lastZoom,
        brightness: lastBrightness,
        saturation: lastSaturation,
        streakOpacity: lastStreakOpacity,
        glowOpacity: lastGlowOpacity,
        vignetteOpacity: lastVignetteOpacity,
        shootingOpacity: lastShootingOpacity,
        warpStrength: lastWarpStrength,
      };
      const punchSplit = 0.38;
      let zoom = from.zoom;
      let brightness = from.brightness;
      let saturation = from.saturation;
      let streakOpacity = from.streakOpacity;
      let glowOpacity = from.glowOpacity;
      let vignetteOpacity = from.vignetteOpacity;
      let shootingOpacity = from.shootingOpacity;
      if (tRaw <= punchSplit) {
        const p = easeOut(tRaw / punchSplit);
        zoom = from.zoom + (HS_FINAL_PUNCH_ZOOM - from.zoom) * p;
        brightness = from.brightness + (3.3 - from.brightness) * p;
        saturation = from.saturation + (2.32 - from.saturation) * p;
        streakOpacity = from.streakOpacity + (1 - from.streakOpacity) * p;
        glowOpacity = from.glowOpacity + (0.98 - from.glowOpacity) * p;
        vignetteOpacity = from.vignetteOpacity + (HS_VIGNETTE_MAX - from.vignetteOpacity) * p;
        shootingOpacity = from.shootingOpacity + (2.22 - from.shootingOpacity) * p;
        warpStrength = from.warpStrength + (1 - from.warpStrength) * p;
        flashOpacity = HS_FLASH_PEAK * Math.exp(-Math.pow((p - 0.72) / 0.2, 2));
      } else {
        const q = easeOut((tRaw - punchSplit) / (1 - punchSplit));
        zoom = HS_FINAL_PUNCH_ZOOM + (1 - HS_FINAL_PUNCH_ZOOM) * q;
        brightness = 3.3 + (1 - 3.3) * q;
        saturation = 2.32 + (1 - 2.32) * q;
        streakOpacity = 1 * (1 - q);
        glowOpacity = 0.98 * (1 - q);
        vignetteOpacity = HS_VIGNETTE_MAX * (1 - q);
        shootingOpacity = 2.22 + (1 - 2.22) * q;
        warpStrength = 1 * (1 - q);
        flashOpacity = HS_FLASH_PEAK * 0.35 * Math.exp(-Math.pow((q - 0.12) / 0.22, 2));
      }
      applyAndRemember(
        zoom,
        brightness,
        saturation,
        streakOpacity,
        glowOpacity,
        vignetteOpacity,
        flashOpacity,
        shootingOpacity,
        warpStrength,
      );

      if (!switchedToMain && tRaw >= 0.44) {
        switchedToMain = true;
        onHyperspaceComplete(activateMainPageCb);
      }
      if (tRaw >= 1) {
        if (!switchedToMain) {
          switchedToMain = true;
          onHyperspaceComplete(activateMainPageCb);
        }
        cleanup();
        return;
      }
    }

    if (ctx && stars) {
      if (viewportWidth !== window.innerWidth || viewportHeight !== window.innerHeight) {
        const size = resizeHyperspaceCanvas(canvas, ctx);
        viewportWidth = size.width;
        viewportHeight = size.height;
      }
      advanceHyperspaceStars(stars, frameDtSec, warpStrength);
      drawHyperspaceStreaks(ctx, stars, viewportWidth, viewportHeight, warpStrength);
    }
    rafId = requestAnimationFrame(tick);
  };

  rafId = requestAnimationFrame(tick);

  return {
    finish: async (activateMainPage) => {
      if (!releaseRequested && !cancelRequested) {
        releaseRequested = true;
        activateMainPageCb = activateMainPage;
        releaseFrom = {
          zoom: lastZoom,
          brightness: lastBrightness,
          saturation: lastSaturation,
          streakOpacity: lastStreakOpacity,
          glowOpacity: lastGlowOpacity,
          vignetteOpacity: lastVignetteOpacity,
          shootingOpacity: lastShootingOpacity,
          warpStrength: lastWarpStrength,
        };
      }
      await donePromise;
    },
    cancel: async () => {
      if (!cancelRequested && !releaseRequested) {
        cancelRequested = true;
      }
      await donePromise;
    },
  };
}

async function refreshKeyStatus() {
  const accountValue = document.getElementById("account_address")?.value?.trim() || "";
  const hasValidAccount = accountValue.startsWith("0x") && accountValue.length >= 10;
  const extendedMode = isExtendedMode();
  let apiKeyForAccount = false;
  let agentKeyForAccount = false;
  let agentName = AGENT_NAME;
  let agentAddress = "";
  if (hasValidAccount) {
    _activateAuthForAccount(accountValue);
    try {
      const status = await apiGet(`/api/key_status?account_address=${encodeURIComponent(accountValue)}`);
      hasStoredApiKeyForAccount = !!status.key_for_account;
      apiKeyForAccount = !!status.api_key_for_account;
      agentKeyForAccount = !!status.agent_key_for_account;
      agentName = String(status.agent_name || AGENT_NAME);
      agentAddress = String(status.agent_address || "").trim();
      encryptedStoreEnabled = !!status.encrypted_store_enabled;
    } catch (_) {
      hasStoredApiKeyForAccount = false;
      encryptedStoreEnabled = false;
    }
  } else {
    hasStoredApiKeyForAccount = false;
    encryptedStoreEnabled = false;
  }
  const apiKeyInput = document.getElementById("api_key");
  const apiKeyStatus = document.getElementById("api_key_status");
  if (extendedMode) {
    if (apiKeyStatus) {
      apiKeyStatus.textContent = "";
      apiKeyStatus.classList.add("hidden");
    }
    return;
  }
  if (apiKeyInput) {
    if (agentKeyForAccount) {
      apiKeyInput.placeholder = "TRADOR agent key is stored (manual key optional)";
    } else if (hasStoredApiKeyForAccount) {
      apiKeyInput.placeholder = "Stored key found for this account (optional to re-enter)";
    } else {
      apiKeyInput.placeholder = "0x...";
    }
  }
  if (apiKeyStatus) {
    if (!hasValidAccount) {
      apiKeyStatus.textContent = "";
      apiKeyStatus.classList.add("hidden");
    } else if (agentKeyForAccount) {
      apiKeyStatus.textContent = "";
      apiKeyStatus.classList.add("hidden");
    } else if (!hasStoredApiKeyForAccount) {
      apiKeyStatus.textContent = "";
      apiKeyStatus.classList.add("hidden");
    } else if (encryptedStoreEnabled) {
      apiKeyStatus.textContent = apiKeyForAccount
        ? "Stored API key for this address is loaded from encrypted local storage."
        : "Stored key for this address is loaded from encrypted local storage.";
      apiKeyStatus.classList.remove("hidden");
    } else {
      apiKeyStatus.textContent = apiKeyForAccount
        ? "Stored API key for this address is loaded in server memory."
        : "Stored key for this address is loaded in server memory.";
      apiKeyStatus.classList.remove("hidden");
    }
  }
}

function normalizeAccount(value) {
  return String(value || "").trim().toLowerCase();
}

function updateProfileConnectionBadge() {
  const el = document.getElementById("profile_connection_badge");
  if (!el) return;
  const account = document.getElementById("account_address")?.value?.trim() || "";
  const connected = !!(account && isWalletAuthValidForAccount(account));
  el.textContent = connected ? "Connected" : "Disconnected";
  el.classList.toggle("connected", connected);
  el.classList.toggle("disconnected", !connected);
}

function setExtendedHelpVisibility(visible) {
  const show = !!visible;
  const compact = document.getElementById("connect_help_compact");
  const toggle = document.getElementById("connect_help_toggle");
  const details = document.getElementById("connect_help_details");
  if (compact) compact.classList.toggle("hidden", !show);
  if (toggle) {
    toggle.classList.toggle("hidden", !show);
    if (!show) {
      toggle.setAttribute("aria-expanded", "false");
      toggle.textContent = "How to get Extended API keys";
    }
  }
  if (details && !show) {
    details.classList.add("hidden");
  }
}

function updateConnectStepsState() {
  const step1 = document.getElementById("connect_step_1");
  const step2 = document.getElementById("connect_step_2");
  const step3 = document.getElementById("connect_step_3");
  if (!step1 || !step2 || !step3) return;
  const step1Label = step1.querySelector(".step-label");
  const step2Label = step2.querySelector(".step-label");
  const step3Label = step3.querySelector(".step-label");
  const mode = document.getElementById("mode")?.value || "";
  const account = document.getElementById("account_address")?.value?.trim() || "";
  const extended = isExtendedMode(mode);
  const nado = isNadoMode(mode);
  const risex = isRisexMode(mode);
  const qfex = isQfexMode(mode);

  if (step1Label) step1Label.textContent = "Connect wallet";
  if (step2Label) {
    step2Label.textContent = extended
      ? "Connect Extended"
      : qfex
        ? "Connect QFEX"
      : nado
        ? "Approve Nado agent"
        : "Approve builder fee";
  }
  if (step3Label) {
    step3Label.textContent = extended
      ? "Approve builder fee"
      : (nado || qfex)
        ? "Start bot"
        : risex
          ? "Connect RISEx"
          : "Approve TRADOR agent";
  }
  step1.title = "Click to connect and sign wallet";
  step2.title = extended
    ? "Click to connect Extended (wallet signatures)"
    : qfex
      ? "Click to paste your QFEX trade-only API key"
    : nado
      ? "Click to approve Nado linked signer"
      : "Click to approve builder fee";
  step3.title = extended
    ? "Click to approve builder fee"
    : (nado || qfex)
      ? "Click to start bot"
      : risex
        ? "Click to register the RISEx bot signer (wallet signatures)"
        : "Click to approve TRADOR agent";

  const step1Done = isWalletAuthValidForAccount(account);
  const step2Done = extended
    ? !!extendedCredentialsReadyForAccount
    : qfex
      ? !!qfexCredentialsReadyForAccount
    : nado
      ? !!nadoAgentApprovedForAccount
      : risex
        ? !!risexBuilderApprovedForAccount
        : !!builderApprovedForAccount;
  const step3Done = extended
    ? !!builderApprovedForAccount
    : (nado || qfex)
      ? false
      : risex
        ? !!risexSignerReadyForAccount
        : !!agentApprovedForAccount;
  const step3Active = step1Done && step2Done && !step3Done;
  const states = [
    { done: step1Done, active: !step1Done },
    { done: step2Done, active: step1Done && !step2Done },
    { done: step3Done, active: step3Active },
  ];

  const applyState = (el, { done = false, active = false } = {}) => {
    const wasDone = el.classList.contains("is-done");
    el.classList.toggle("is-done", !!done);
    el.classList.toggle("is-active", !!active && !done);
    el.classList.toggle("is-pending", !done && !active);
    if (done && !wasDone) {
      el.classList.remove("is-done-flash");
      void el.offsetWidth; // force reflow to restart animation
      el.classList.add("is-done-flash");
    } else if (!done) {
      el.classList.remove("is-done-flash");
    }
    const stateEl = el.querySelector(".step-state");
    if (stateEl) {
      stateEl.textContent = done ? "Done" : active ? "In progress" : "Pending";
    }
    el.setAttribute("aria-pressed", active ? "true" : "false");
  };

  applyState(step1, states[0]);
  applyState(step2, states[1]);
  applyState(step3, states[2]);

  // Update pipeline connectors
  const conn12 = document.getElementById("step_conn_12");
  const conn23 = document.getElementById("step_conn_23");
  if (conn12) conn12.classList.toggle("is-done", !!step1Done);
  if (conn23) conn23.classList.toggle("is-done", !!step2Done);

  [step1, step2, step3].forEach((step, idx) => {
    const done = states[idx].done;
    const busy = connectStepActionInFlight || setupAllInFlight || hyperspaceTransitionInFlight;
    step.disabled = !!done || busy;
    step.classList.toggle("is-busy", busy && !done);
  });
}

async function runConnectStepAction(stepNo) {
  if (setupAllInFlight || hyperspaceTransitionInFlight || connectStepActionInFlight) return false;
  const step = Number(stepNo || 0);
  if (![1, 2, 3].includes(step)) return false;
  clearError("connect_error");
  connectStepActionInFlight = true;
  updateConnectStepsState();
  try {
    const mode = document.getElementById("mode")?.value || "";
    if (step === 1) {
      await connectAndSignWallet({ reverify: true });
      await refreshWalletAuthStatus({ force: true });
      await refreshWalletNetworkBadge();
      return true;
    }
    if (step === 2) {
      if (isExtendedMode(mode)) {
        await connectExtendedWithSignatures();
        await refreshExtendedCredentialsStatus({ force: true });
      } else if (isQfexMode(mode)) {
        await _connectSingleDex("qfex");
        await refreshQfexStatus({ force: true });
      } else if (isRisexMode(mode)) {
        await approveRisexBuilderWithWallet();
        await refreshRisexStatus({ force: true });
      } else if (isNadoMode(mode)) {
        await approveNadoAgentWithWallet();
        await refreshNadoAgentStatus({ force: true });
      } else {
        await approveBuilderFeeWithWallet();
        await refreshBuilderStatus({ force: true });
      }
      return true;
    }
    if (isExtendedMode(mode)) {
      await approveBuilderFeeWithWallet();
      await refreshBuilderStatus({ force: true });
      return true;
    }
    if (isNadoMode(mode) || isQfexMode(mode)) {
      // Step 3 for Nado and QFEX = start bot, nothing to do here
      return false;
    }
    if (isRisexMode(mode)) {
      await connectRisexWithSignatures();
      await refreshRisexStatus({ force: true });
      return true;
    }
    await approveAgentWithWallet();
    await refreshAgentStatus({ force: true });
    return true;
  } catch (e) {
    showError("connect_error", e && e.message ? e.message : String(e));
    return false;
  } finally {
    connectStepActionInFlight = false;
    updateConnectStepsState();
  }
}

function toggleConnectHelpDetails() {
  const details = document.getElementById("connect_help_details");
  const btn = document.getElementById("connect_help_toggle");
  if (!details || !btn) return;
  const willOpen = details.classList.contains("hidden");
  details.classList.toggle("hidden", !willOpen);
  btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
  btn.textContent = willOpen ? "Hide API key steps" : "How to get Extended API keys";
}

function updateWalletAddressDisplay() {
  const display = document.getElementById("wallet_address_display");
  const setBoth = (value) => {
    if (display) display.textContent = value;
  };
  const account = document.getElementById("account_address")?.value?.trim() || "";
  if (!account) {
    setBoth("No wallet");
    return;
  }
  const normalized = account.toLowerCase();
  if (!/^0x[a-f0-9]{40}$/.test(normalized)) {
    setBoth(account);
    updateProfileConnectionBadge();
    return;
  }
  setBoth(`${normalized.slice(0, 5)}...`);
  updateProfileConnectionBadge();
}

function setConnectedAccount(account) {
  const input = document.getElementById("account_address");
  if (!input) return;
  input.value = String(account || "").trim();
  updateWalletAddressDisplay();
  updateConnectStepsState();
  refreshMilestonesEligibility(input.value);
  if (input.value) setTimeout(maybeShowPointsLivePopup, 900);
}

async function disconnectWalletProfile() {
  clearWalletAuthSession();
  // Reset wallet picker so user can re-select on next connect
  _tradoorActiveProvider = null;
  try { localStorage.removeItem("tradoor_wallet_rdns"); } catch (_) {}
  setConnectedAccount("");
  const apiKeyInput = document.getElementById("api_key");
  if (apiKeyInput) apiKeyInput.value = "";
  hasStoredApiKeyForAccount = false;
  builderApprovedForAccount = false;
  agentApprovedForAccount = false;
  nadoAgentApprovedForAccount = false;
  nadoAgentStatusCache = { key: "", ts: 0, value: null };
  nftHolderStatus = false;
  updateNftHolderBadge();
  approvedBuilderFeeTenthsBps = BUILDER_FEE_TENTHS_BPS;
  approvedExtBuilderFeeTenthsBps = EXTENDED_BUILDER_FEE_TENTHS_BPS;
  builderStatusCache = { key: "", ts: 0, value: null };
  agentStatusCache = { key: "", ts: 0, value: null };
  clearError("connect_error");
  setWalletAuthStatus("1/3 Wallet signature required.", "warn");
  setBuilderStatus("2/3 Approve builder fee.", "warn");
  setAgentStatus("3/3 Approve TRADOR agent.", "warn");
  updateWalletAuthButtonState();
  updateBuilderApproveButtonState(false);
  updateAgentApproveButtonState(false);
  setSignatureStepsVisible(false);
  // Clear pending timers
  if (liveConfigSyncTimer) { window.clearTimeout(liveConfigSyncTimer); liveConfigSyncTimer = null; }
  // Reset connect modal + setup-all flow state to avoid stale timers / stuck flags on reconnect
  if (_connectModalState && _connectModalState.closeTimer) {
    window.clearTimeout(_connectModalState.closeTimer);
    _connectModalState.closeTimer = null;
  }
  setupAllInFlight = false;
  setupAllLockedUntilMs = 0;
  if (typeof closeConnectModal === "function") closeConnectModal();
  if (_telegramVerifyPollTimer) { clearInterval(_telegramVerifyPollTimer); _telegramVerifyPollTimer = null; }
  if (_tsRefreshTimer) { clearInterval(_tsRefreshTimer); _tsRefreshTimer = null; }
  stopTsBadgePolling();
  setMilestonesPageOpen(false);
  resetAdvancedPresetsClientState();
  // Reset multi-slot & schedule state
  runningSlots = [];
  _currentSchedule = null;
  _scheduleMode = false;
  _scheduleUiInitDone = false;
  _setStopMode(false);
  renderRunningSlots();
  // Reset multi-DEX state
  resetDexState();
  syncModeCardsFromSelect();
  updateDexSelectorUI();
  await refreshKeyStatus();
  await refreshWalletNetworkBadge();
  updateHeaderWalletDisplay();
}

function clearWalletAuthSession() {
  walletAuthTokens = {};
  walletAuthExpires = {};
  walletAuthToken = "";
  walletAuthAccount = "";
  walletAuthExpiresAtMs = 0;
  try {
    localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
    localStorage.removeItem(AUTH_ACCOUNT_STORAGE_KEY);
    localStorage.removeItem(AUTH_EXPIRES_STORAGE_KEY);
  } catch (_) {}
  updateProfileConnectionBadge();
  updateConnectStepsState();
}

function clearWalletAuthForAccount(addr) {
  const norm = normalizeAccount(addr || "");
  if (!norm) return;
  delete walletAuthTokens[norm];
  delete walletAuthExpires[norm];
  if (walletAuthAccount === norm) {
    walletAuthToken = "";
    walletAuthAccount = "";
    walletAuthExpiresAtMs = 0;
    _syncPrimaryAuthGlobals();
  }
  persistWalletAuthSession();
  updateProfileConnectionBadge();
  updateConnectStepsState();
}

function _extractAccountFromRequest(path, body) {
  try {
    if (path) {
      const idx = path.indexOf("?");
      if (idx >= 0) {
        const params = new URLSearchParams(path.slice(idx + 1));
        const addr = params.get("account_address") || params.get("address") || params.get("account");
        if (addr) return addr;
      }
    }
    if (body && typeof body === "object") {
      if (typeof FormData !== "undefined" && body instanceof FormData) {
        return body.get("account_address") || body.get("address") || body.get("account") || "";
      }
      if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) {
        return body.get("account_address") || body.get("address") || body.get("account") || "";
      }
      return body.account_address || body.address || body.account || "";
    }
  } catch (_) {}
  return "";
}

function persistWalletAuthSession() {
  try {
    // Prune expired tokens
    const now = Date.now();
    for (const addr of Object.keys(walletAuthExpires)) {
      if (walletAuthExpires[addr] <= now) {
        delete walletAuthTokens[addr];
        delete walletAuthExpires[addr];
      }
    }
    if (!Object.keys(walletAuthTokens).length) {
      clearWalletAuthSession();
      return;
    }
    // New multi-token format
    localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, JSON.stringify({ tokens: walletAuthTokens, expires: walletAuthExpires }));
    // Legacy keys for backward compat
    localStorage.setItem(AUTH_ACCOUNT_STORAGE_KEY, walletAuthAccount);
    localStorage.setItem(AUTH_EXPIRES_STORAGE_KEY, String(walletAuthExpiresAtMs));
  } catch (_) {}
}

function restoreExchangeMode() {}  // Legacy no-op : replaced by _syncDexDomState

function restoreWalletAuthSession() {
  try {
    const raw = localStorage.getItem(AUTH_TOKEN_STORAGE_KEY) || "";
    const now = Date.now();

    // Try new multi-token format first
    if (raw.startsWith("{")) {
      const parsed = JSON.parse(raw);
      if (parsed.tokens && typeof parsed.tokens === "object") {
        walletAuthTokens = parsed.tokens;
        walletAuthExpires = parsed.expires || {};
        // Prune expired
        for (const addr of Object.keys(walletAuthExpires)) {
          if (walletAuthExpires[addr] <= now) {
            delete walletAuthTokens[addr];
            delete walletAuthExpires[addr];
          }
        }
        if (Object.keys(walletAuthTokens).length) {
          _syncPrimaryAuthGlobals();
          const accountInput = document.getElementById("account_address");
          if (accountInput && !accountInput.value.trim()) {
            accountInput.value = walletAuthAccount;
          }
          updateWalletAddressDisplay();
          return;
        }
      }
    }

    // Legacy single-token format
    const token = raw;
    const account = normalizeAccount(localStorage.getItem(AUTH_ACCOUNT_STORAGE_KEY) || "");
    const expiresRaw = parseInt(localStorage.getItem(AUTH_EXPIRES_STORAGE_KEY) || "0", 10);
    if (!token || !account || !Number.isFinite(expiresRaw) || expiresRaw <= now) {
      clearWalletAuthSession();
      return;
    }
    // Migrate to new format
    walletAuthTokens[account] = token;
    walletAuthExpires[account] = expiresRaw;
    _syncPrimaryAuthGlobals();
    const accountInput = document.getElementById("account_address");
    if (accountInput && !accountInput.value.trim()) {
      accountInput.value = account;
    }
    updateWalletAddressDisplay();
  } catch (_) {
    clearWalletAuthSession();
  }
}

function isWalletAuthValidForAccount(account) {
  const norm = normalizeAccount(account);
  if (!norm) return false;
  if (walletAuthToken && walletAuthAccount === norm && walletAuthExpiresAtMs > Date.now()) {
    return true;
  }
  return _activateAuthForAccount(norm);
}

function _syncHdrStep(id, tone) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("ok", "warn", "error");
  el.classList.add(tone);
}

function setWalletAuthStatus(message, tone = "warn") {
  const el = document.getElementById("wallet_auth_status");
  if (!el) return;
  el.textContent = tone === "ok" ? "1/3 Wallet ready" : "1/3 Wallet";
  el.title = message;
  el.classList.remove("ok", "warn", "error");
  el.classList.add(tone);
  _syncHdrStep("hdr_step_wallet", tone);
  updateProfileConnectionBadge();
  updateConnectStepsState();
}

function setBuilderStatus(message, tone = "warn") {
  const el = document.getElementById("builder_fee_status");
  if (!el) return;
  el.title = message;
  el.classList.remove("ok", "warn", "error");
  el.classList.add(tone);
  _syncHdrStep("hdr_step_builder", tone);
}

function setAgentStatus(message, tone = "warn") {
  const el = document.getElementById("agent_status");
  if (!el) return;
  el.title = message;
  el.classList.remove("ok", "warn", "error");
  el.classList.add(tone);
  _syncHdrStep("hdr_step_agent", tone);
}

function setExtendedCredentialsStatus(message, tone = "warn") {
  const el = document.getElementById("extended_credentials_status");
  if (!el) return;
  el.textContent = message;
  el.title = message;
  el.classList.remove("ok", "warn", "error");
  el.classList.add(tone);
  updateConnectStepsState();
}

function syncModeCardsFromSelect() {
  // Multi-select mode: sync card checked states from _selectedDexes
  const cardsWrap = document.getElementById("mode_cards");
  if (!cardsWrap) return;
  const cards = cardsWrap.querySelectorAll(".mode-card");
  cards.forEach((node) => {
    const el = node;
    if (!(el instanceof HTMLButtonElement)) return;
    const dex = el.dataset.dex || "";
    const checked = _selectedDexes.has(dex);
    el.classList.toggle("active", checked);
    el.setAttribute("aria-checked", checked ? "true" : "false");
    el.tabIndex = 0;
  });
  _updateConnectButtonCount();
}

function _updateConnectButtonCount() {
  const btn = document.getElementById("connect_continue");
  if (!btn || btn.classList.contains("loading")) return;
  const n = _selectedDexes.size;
  if (n === 0) {
    btn.textContent = "Select a platform";
    btn.disabled = true;
  } else {
    btn.textContent = n > 1 ? `Connect (${n})` : "Connect";
    btn.disabled = false;
  }
}

function selectModeCardValue(nextValue) {
  // Legacy compat: set mode select to the given value
  const modeSelect = document.getElementById("mode");
  if (!modeSelect) return;
  if (modeSelect.value !== nextValue) {
    modeSelect.value = nextValue;
    modeSelect.dispatchEvent(new Event("change", { bubbles: true }));
    return;
  }
  syncModeCardsFromSelect();
}

function toggleModeCard(dex) {
  if (_selectedDexes.has(dex)) {
    _selectedDexes.delete(dex);
  } else {
    _selectedDexes.add(dex);
  }
  // Keep mode select in sync with the first selected dex (for legacy compat)
  const modeSelect = document.getElementById("mode");
  if (modeSelect && _selectedDexes.size > 0) {
    const first = [..._selectedDexes][0];
    const modeVal = DEX_MODE_MAP[first] || "hyperliquid_mainnet";
    if (modeSelect.value !== modeVal) {
      modeSelect.value = modeVal;
      modeSelect.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }
  syncModeCardsFromSelect();
  persistDexState();
}

function initModeCards() {
  const modeSelect = document.getElementById("mode");
  const cardsWrap = document.getElementById("mode_cards");
  if (!modeSelect || !cardsWrap) return;
  const cards = Array.from(cardsWrap.querySelectorAll(".mode-card")).filter(
    (node) => node instanceof HTMLButtonElement,
  );
  if (!cards.length) return;

  // Restore selected dexes from localStorage
  restoreDexState();

  for (const card of cards) {
    card.addEventListener("click", () => {
      const dex = card.dataset.dex || "";
      if (!dex) return;
      toggleModeCard(dex);
    });
  }

  modeSelect.addEventListener("change", () => {
    syncModeCardsFromSelect();
    try { localStorage.setItem(EXCHANGE_MODE_STORAGE_KEY, modeSelect.value); } catch (_) {}
  });
  syncModeCardsFromSelect();
}

function updateModeSelectLogo() {
  syncModeCardsFromSelect();
}

/* ── Mode selector (MM / DN) ── */

const MODE_STORAGE_KEY = "tradoor_bot_mode";
let _activeBotType = "mm"; // "mm" | "dn" | "earn"

function getActiveBotType() { return _activeBotType; }

/** Brackets around the active mode button were removed; kept as a no-op so the
 *  existing call sites stay valid. */
function _positionModeBrackets(_animate) {}

function setActiveBotType(type) {
  if (_activeBotType === type) return;
  _activeBotType = type;
  try { localStorage.setItem(MODE_STORAGE_KEY, type); } catch (_) {}

  // Update selector buttons
  document.querySelectorAll(".mode-sel-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === type);
  });

  // Slide brackets to new active button
  _positionModeBrackets(true);

  // Toggle MM vs HIP-4 main pages
  const mmPage = document.getElementById("main_page");
  const hip4Page = document.getElementById("hip4_page");
  const hip4volPageEl = document.getElementById("hip4vol_page");
  if (mmPage) mmPage.classList.toggle("hidden", type !== "mm");
  if (hip4Page) {
    // hip4_page n'héberge plus que la vue session MM, pilotée par HIP4MM.
    hip4Page.classList.add("hidden");
    hip4Page.setAttribute("aria-hidden", "true");
  }
  if (hip4volPageEl) {
    // Ne pas écraser la vue hip4vol : au boot en deep-link (/hip4vol ou F5),
    // ce bloc tournait APRÈS applyRoute et re-cachait la page → écran noir.
    hip4volPageEl.classList.toggle("hidden", type !== "hip4vol");
    hip4volPageEl.setAttribute("aria-hidden", type !== "hip4vol" ? "true" : "false");
  }
  const deltaPageEl = document.getElementById("delta_page");
  if (deltaPageEl) {
    deltaPageEl.classList.toggle("hidden", type !== "delta");
    deltaPageEl.setAttribute("aria-hidden", type !== "delta" ? "true" : "false");
  }
}

function initModeSelector() {
  // URL takes precedence, then localStorage, then default "mm"
  const urlView = getCurrentView();
  if (urlView !== "landing" && urlView !== _activeBotType) {
    _activeBotType = urlView;
  } else {
    const saved = localStorage.getItem(MODE_STORAGE_KEY);
    // Migrate legacy "hip4" (Predictions bot fusionné dans Outcomes) → "mm".
    if (saved === "hip4") _activeBotType = "mm";
  }
  // Sync button active state with restored value
  document.querySelectorAll(".mode-sel-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === _activeBotType);
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      if (btn.disabled || btn.classList.contains("mode-sel-btn--disabled")) return;
      const route = ROUTE_PATHS[btn.dataset.mode];
      if (route) navigate(route);
    });
  });

  // Apply page visibility for restored mode
  const mmPage = document.getElementById("main_page");
  const hip4Page = document.getElementById("hip4_page");
  if (mmPage) mmPage.classList.toggle("hidden", _activeBotType !== "mm");
  if (hip4Page) {
    hip4Page.classList.add("hidden");
    hip4Page.setAttribute("aria-hidden", "true");
  }
  const hip4volPageInit = document.getElementById("hip4vol_page");
  if (hip4volPageInit) {
    // Deep-link /hip4vol (ou refresh) : _activeBotType vaut "hip4vol" — ne pas
    // re-cacher la page qu'applyRoute vient d'afficher (écran noir sinon).
    hip4volPageInit.classList.toggle("hidden", _activeBotType !== "hip4vol");
    hip4volPageInit.setAttribute("aria-hidden", _activeBotType !== "hip4vol" ? "true" : "false");
  }
  const deltaPageInit = document.getElementById("delta_page");
  if (deltaPageInit) {
    deltaPageInit.classList.toggle("hidden", _activeBotType !== "delta");
    deltaPageInit.setAttribute("aria-hidden", _activeBotType !== "delta" ? "true" : "false");
  }

  // Reposition brackets on resize
  window.addEventListener("resize", () => _positionModeBrackets(false));

}

function showModeSelector() {
  const sel = document.getElementById("mode_selector");
  if (sel) {
    sel.classList.remove("hidden");
    // Position brackets after layout is computed
    requestAnimationFrame(() => _positionModeBrackets(false));
  }
}



// ===========================================================================
// App dialog (replaces native alert/confirm)
// ===========================================================================

function _showAppDialog(msg, { confirm = false, error = false } = {}) {
  return new Promise(resolve => {
    const overlay = document.getElementById("app_dialog_overlay");
    const dialog = document.getElementById("app_dialog");
    const msgEl = document.getElementById("app_dialog_msg");
    const cancelBtn = document.getElementById("app_dialog_cancel");
    const okBtn = document.getElementById("app_dialog_ok");

    msgEl.textContent = msg;
    dialog.classList.toggle("app-dialog--error", error);
    cancelBtn.style.display = confirm ? "" : "none";

    overlay.classList.remove("hidden");
    overlay.setAttribute("aria-hidden", "false");
    dialog.classList.remove("hidden");
    dialog.setAttribute("aria-hidden", "false");
    okBtn.focus();

    function close(result) {
      overlay.classList.add("hidden");
      overlay.setAttribute("aria-hidden", "true");
      dialog.classList.add("hidden");
      dialog.setAttribute("aria-hidden", "true");
      cleanup();
      resolve(result);
    }
    function onOk() { close(true); }
    function onCancel() { close(false); }
    function onOverlay() { close(false); }
    function onKey(e) { if (e.key === "Escape") close(false); }
    function cleanup() {
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onOverlay);
      document.removeEventListener("keydown", onKey);
    }
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("click", onOverlay);
    document.addEventListener("keydown", onKey);
  });
}

function showAppAlert(msg, opts) { return _showAppDialog(msg, { error: true, ...opts }); }
function showAppConfirm(msg, opts) { return _showAppDialog(msg, { confirm: true, ...opts }); }

function updateMarketSourceForMode() {
  const mode = document.getElementById("mode")?.value || "";
  const source = document.getElementById("market_source");
  const selector = document.getElementById("source_selector");
  if (!source) return;
  const extended = isExtendedMode(mode);
  const nado = isNadoMode(mode);
  const risex = isRisexMode(mode);
  const qfex = isQfexMode(mode);
  if ((risex || qfex) && selectedQuoteMode !== "sniper") applyQuoteMode("sniper", { silent: true });
  if (extended || nado || risex || qfex) {
    source.value = "top20";
    source.disabled = true;
    if (selector) {
      selector.classList.add("is-disabled");
      selector.style.display = "none";
    }
  } else {
    source.disabled = false;
    if (selector) {
      selector.classList.remove("is-disabled");
      selector.style.display = "";
    }
  }
  document.body.classList.toggle("mode-extended", extended);
  document.body.classList.toggle("mode-nado", nado);
  document.body.classList.toggle("mode-risex", risex);
  document.body.classList.toggle("mode-qfex", qfex);
  // Extended supporte désormais tous les quote modes ; Nado, RISEx et QFEX restent en
  // sniper (main.py refuse les autres modes sur ces adapters).
  setQuoteModeSourceLocked(nado || risex || qfex);
  syncSourceSelectorFromSelect();
  if (typeof renderTradingAccountSelector === "function") renderTradingAccountSelector();
}

function syncSourceSelectorFromSelect() {
  const source = document.getElementById("market_source");
  const selector = document.getElementById("source_selector");
  if (!source || !selector) return;
  const value = String(source.value || "top20");
  selector.dataset.active = value;
  selector.classList.toggle("is-disabled", !!source.disabled);
  const sourceModes = ["top20", ...Object.keys(HIP3_SOURCES)];
  selector.classList.remove(...sourceModes.map((s) => `mode-${s}`));
  if (sourceModes.includes(value)) selector.classList.add(`mode-${value}`);
  const options = selector.querySelectorAll(".source-option");
  options.forEach((node) => {
    const el = node;
    if (!(el instanceof HTMLButtonElement)) return;
    const active = String(el.dataset.source || "") === value;
    el.classList.toggle("active", active);
    el.setAttribute("aria-selected", active ? "true" : "false");
    el.setAttribute("aria-pressed", active ? "true" : "false");
    el.tabIndex = active ? 0 : -1;
  });
  const hip3Notice = document.getElementById("hip3_notice");
  if (hip3Notice) {
    if (value === "hip3") {
      hip3Notice.classList.remove("notice-xyz");
      hip3Notice.innerHTML = 'ℹ️ Kinetiq Markets (mkts) requires <strong>Unified Account</strong> mode on <a href="https://app.hyperliquid.xyz" target="_blank" rel="noopener">app.hyperliquid.xyz</a> → Settings → make sure this is <strong>unchecked</strong>:<br><span class="hip3-notice-mockup"><span class="hip3-notice-mockup-label">Disable Unified Account Mode</span><span class="hip3-notice-mockup-checkbox"></span></span>';
      hip3Notice.style.display = "";
    } else if (value === "xyz") {
      hip3Notice.classList.add("notice-xyz");
      hip3Notice.innerHTML = 'ℹ️ Trade.xyz (xyz) also depends on Hyperliquid account routing. Keep <strong>Unified Account</strong> mode <strong>unchecked</strong> on <a href="https://app.hyperliquid.xyz" target="_blank" rel="noopener">app.hyperliquid.xyz</a> → Settings:<br><span class="hip3-notice-mockup"><span class="hip3-notice-mockup-label">Disable Unified Account Mode</span><span class="hip3-notice-mockup-checkbox"></span></span>';
      hip3Notice.style.display = "";
    } else if (value === "io") {
      hip3Notice.classList.add("notice-xyz");
      hip3Notice.innerHTML = 'ℹ️ EntropyIO (io) markets are <strong>isolated margin only</strong>, leverage capped per market. Keep <strong>Unified Account</strong> mode <strong>unchecked</strong> on <a href="https://app.hyperliquid.xyz" target="_blank" rel="noopener">app.hyperliquid.xyz</a> → Settings:<br><span class="hip3-notice-mockup"><span class="hip3-notice-mockup-label">Disable Unified Account Mode</span><span class="hip3-notice-mockup-checkbox"></span></span>';
      hip3Notice.style.display = "";
    } else {
      hip3Notice.classList.remove("notice-xyz");
      hip3Notice.style.display = "none";
    }
  }
}

// ── Trading Account selector (HL subaccounts, start form MM) ────────────────
// Sélection mémorisée PAR MODE (spec) : "mm" pour le start form classique,
// "hip4" pour le tab HIP-4 bot et /outcomes.
const HL_TRADING_ACCOUNT_STORAGE_PREFIX = "hl_trading_account_"; // + scope
let _hlSubaccounts = [];
let _selectedTradingAccount = { mm: "", hip4: "" };
// ── Sous-comptes Extended (index 1..2, marge isolée, clés dérivées par index)
const EXTENDED_TA_STORAGE_KEY = "extended_account_index";
let _extendedSubaccounts = []; // [{account_index, ready, vault}] du status serveur
let _extendedAccountIndex = (() => {
  try { return Math.max(0, Math.min(2, parseInt(localStorage.getItem(EXTENDED_TA_STORAGE_KEY) || "0", 10) || 0)); }
  catch (_) { return 0; }
})();

function getExtendedAccountIndex() {
  return isExtendedMode(document.getElementById("mode")?.value || "") ? _extendedAccountIndex : 0;
}

function setExtendedAccountIndex(idx) {
  _extendedAccountIndex = Math.max(0, Math.min(2, parseInt(idx, 10) || 0));
  try { localStorage.setItem(EXTENDED_TA_STORAGE_KEY, String(_extendedAccountIndex)); } catch (_) {}
  renderTradingAccountSelector();
  refreshMetrics();
}

function getSelectedTradingAccount(scope = "mm") { return _selectedTradingAccount[scope] || ""; }
function setSelectedTradingAccount(addr, scope = "mm") {
  _selectedTradingAccount[scope] = String(addr || "");
  try {
    localStorage.setItem(HL_TRADING_ACCOUNT_STORAGE_PREFIX + scope, _selectedTradingAccount[scope]);
  } catch (_) {}
  renderTradingAccountSelector();
  if (scope === "mm") refreshMetrics(); // le solde du panneau notional suit le compte
  if (scope === "hip4" && typeof _hip4volFetchAvail === "function") _hip4volFetchAvail(); // idem pour le solde HIP-4
}

async function refreshHlSubaccounts() {
  let ok = true;
  try {
    const d = await apiGet("/api/hl/subaccounts");
    _hlSubaccounts = Array.isArray(d?.subaccounts) ? d.subaccounts : [];
  } catch (_) {
    ok = false;
    // Fetch transitoire (429/500) : ne pas écraser la liste précédente, sinon
    // la boucle ci-dessous croirait à tort qu'un sub sélectionné a disparu et
    // persisterait un reset vers Main en localStorage.
  }
  // si un compte sélectionné n'existe plus, retomber sur Main (par scope) —
  // uniquement quand le fetch a réussi (sinon on ne sait pas si le sub existe).
  if (ok) {
    for (const scope of ["mm", "hip4"]) {
      const cur = _selectedTradingAccount[scope];
      if (cur && !_hlSubaccounts.some(s => s.address.toLowerCase() === cur.toLowerCase())) {
        setSelectedTradingAccount("", scope);
      }
    }
  }
  renderTradingAccountSelector();
}

// Rend tous les conteneurs de sélecteur de compte connus (chacun avec son
// scope indépendant) : le start form MM ("mm") et le tab HIP-4 bot ("hip4").
function renderTradingAccountSelector() {
  _renderTradingAccountSelectorFor("trading_account_wrap", "trading_account_selector", "mm");
  _renderTradingAccountSelectorFor("hip4vol_ta_wrap", "hip4vol_ta_selector", "hip4");
}

function _renderTradingAccountSelectorFor(wrapId, selId, scope) {
  const wrap = document.getElementById(wrapId);
  const sel = document.getElementById(selId);
  if (!wrap || !sel) return;
  const exchange = document.getElementById("mode")?.value || "";
  const extendedMm = scope === "mm" && isExtendedMode(exchange);
  if (scope === "mm") {
    // Visible sur Hyperliquid ET Extended (sous-comptes natifs par index).
    wrap.classList.toggle("hidden", !(exchange.startsWith("hyperliquid") || extendedMm));
  }
  // Session running sur le slot ciblé par ce scope → pills verrouillées
  // (pattern HIP-4 : le toggle taker/maker est déjà désactivé pendant une session).
  const targetSlot = scope === "hip4" ? HIP4VOL_SLOT : _mmAutosaveTargetSlot();
  const locked = !!_runningSessionOnSlot(targetSlot);
  sel.classList.toggle("ta-disabled", locked);
  sel.title = locked ? "Stop the running session to switch account" : "";
  const opts = sel.querySelectorAll(".ta-option");
  if (extendedMm) {
    // Rendu Extended : pills par account_index.
    opts.forEach((btn, i) => {
      delete btn.dataset.ta;
      if (i === 0) {
        btn.classList.toggle("active", _extendedAccountIndex === 0);
        btn.classList.remove("ta-empty");
        btn.innerHTML = `<span class="ta-label">Main</span>`;
        btn.dataset.taIndex = "0";
        return;
      }
      const sub = _extendedSubaccounts.find(s2 => s2.account_index === i);
      const subName = sub && sub.name ? String(sub.name) : `Sub ${i}`;
      if (sub && sub.ready) {
        btn.innerHTML = `<span class="ta-label">${escapeHtml(subName)}</span>`;
        btn.classList.remove("ta-empty");
        btn.dataset.taIndex = String(i);
        btn.title = "";
        btn.classList.toggle("active", _extendedAccountIndex === i);
      } else if (sub && sub.exists) {
        // Sous-compte existant côté Extended (créé via l'app officielle) mais
        // clés non dérivées ici : cliquer relance le connect sur cet index.
        btn.innerHTML =
          `<span class="ta-label">${escapeHtml(subName)}</span>` +
          `<span class="ta-sub-bal">activate</span>`;
        btn.classList.add("ta-empty");
        btn.dataset.taIndex = String(i);
        btn.title = "Activate this existing sub-account (wallet signatures)";
        btn.classList.remove("active");
      } else {
        btn.innerHTML = `<span class="ta-label">+ Sub ${i}</span>`;
        btn.classList.add("ta-empty");
        btn.dataset.taIndex = String(i);
        btn.title = "Create an Extended sub-account (wallet signatures)";
        btn.classList.remove("active");
      }
    });
    // Bouton de découverte : liste les comptes Extended du wallet (1 signature)
    // pour faire apparaître les subs créés via l'app officielle.
    let detect = document.getElementById("ta_ext_detect");
    if (!detect) {
      detect = document.createElement("button");
      detect.id = "ta_ext_detect";
      detect.type = "button";
      detect.className = "ta-ext-detect";
      detect.textContent = "↻ detect subs";
      detect.title = "List your Extended sub-accounts (one wallet signature)";
      detect.addEventListener("click", (e) => { e.stopPropagation(); void extendedDetectSubaccounts(detect); });
      wrap.appendChild(detect);
    }
    detect.classList.remove("hidden");
    return;
  }
  // Ne masquer le bouton detect que depuis le rendu du scope mm : le rendu
  // hip4 passe toujours par cette branche et le cacherait systématiquement.
  if (scope === "mm") document.getElementById("ta_ext_detect")?.classList.add("hidden");
  const selected = getSelectedTradingAccount(scope);
  opts.forEach((btn, i) => {
    if (i === 0) {
      btn.classList.toggle("active", !selected);
      btn.innerHTML = `<span class="ta-label">Main</span>`;
      return;
    }
    const sub = _hlSubaccounts[i - 1];
    if (sub) {
      btn.innerHTML =
        `<span class="ta-label">${escapeHtml(sub.name || `Sub ${i}`)}</span>` +
        `<span class="ta-sub-bal">$${_subTotalUsd(sub).toFixed(2)}</span>`;
      btn.classList.remove("ta-empty");
      btn.dataset.ta = sub.address;
      btn.title = "";
      btn.classList.toggle("active",
        !!selected && selected.toLowerCase() === sub.address.toLowerCase());
    } else {
      btn.innerHTML = `<span class="ta-label">+ Sub ${i}</span>`;
      btn.classList.add("ta-empty");
      btn.title = "Create a sub-account";
      delete btn.dataset.ta;
      btn.classList.remove("active");
    }
  });
}

// ── Sub-accounts section (drawer Connections) ───────────────────────────────
function _shortAddr(addr) {
  const a = String(addr || "");
  return a.length > 12 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a;
}

function _subTotalUsd(s) {
  return (s.perp_usd || 0) + (s.spot_usdc || 0);
}

function renderSubaccountsSection() {
  const list = document.getElementById("wallets_subaccounts_list");
  const createBtn = document.getElementById("subaccount_create_btn");
  const titleCount = document.getElementById("wallets_subaccounts_count");
  if (!list) return;
  list.innerHTML = "";
  _hlSubaccounts.forEach((s) => {
    const row = document.createElement("div");
    row.className = "wallets-subaccount-row";
    row.innerHTML =
      `<div class="sub-row-info">` +
        `<div class="sub-row-head"><span class="sub-name">${escapeHtml(s.name || "")}</span></div>` +
        `<div class="sub-balances"><span class="sub-bal">$${(s.perp_usd || 0).toFixed(2)}<em>perp</em></span>` +
        `<span class="sub-bal">$${(s.spot_usdc || 0).toFixed(2)}<em>spot</em></span>` +
        `<button type="button" class="sub-addr" data-copy-addr="${escapeHtml(s.address)}" ` +
        `title="Copy address">${_shortAddr(s.address)}</button></div>` +
      `</div>` +
      `<div class="sub-row-actions">` +
        `<button type="button" class="wallets-card-action" data-fund="${escapeHtml(s.address)}">Fund</button>` +
        `<button type="button" class="wallets-card-action sub-action-ghost" data-withdraw="${escapeHtml(s.address)}">Withdraw</button>` +
      `</div>`;
    list.appendChild(row);
  });
  if (!_hlSubaccounts.length) {
    const empty = document.createElement("div");
    empty.className = "sub-empty-note";
    empty.textContent = "Isolate your bot capital from your main account.";
    list.appendChild(empty);
  }
  if (titleCount) titleCount.textContent = `${_hlSubaccounts.length}/2`;
  if (createBtn) createBtn.classList.toggle("hidden", _hlSubaccounts.length >= 2);
}

// Ouvre le drawer directement sur la section Sub-accounts (flèche depuis les
// pills "+ Sub N") avec un bref surlignage pour guider l'œil.
function openSubaccountsManager() {
  if (typeof openWalletsDrawer === "function") openWalletsDrawer();
  const sec = document.getElementById("wallets_subaccounts");
  if (!sec) return;
  sec.scrollIntoView({ block: "nearest" });
  sec.classList.remove("sub-highlight");
  void sec.offsetWidth; // restart l'animation CSS
  sec.classList.add("sub-highlight");
}

async function onCreateSubaccount() {
  const name = `tradoor-sub-${_hlSubaccounts.length + 1}`;
  const createBtn = document.getElementById("subaccount_create_btn");
  if (createBtn) { createBtn.disabled = true; createBtn.textContent = "Creating…"; }
  try {
    await subaccountAction("create", { name });
    _msShowToast(`Sub-account ${name} created`, "success");
    await refreshHlSubaccounts();
    renderSubaccountsSection();
    renderTradingAccountSelector();
    // Flow : un sub vide ne sert à rien — enchaîne directement sur le funding.
    const created = _hlSubaccounts.find((s) => (s.name || "") === name) || _hlSubaccounts[_hlSubaccounts.length - 1];
    if (created) setSubtransferModalOpen(true, created.address);
  } catch (err) {
    _msShowToast(`Create failed: ${err?.message || err}`, "error");
  } finally {
    if (createBtn) { createBtn.disabled = false; createBtn.textContent = "Create sub-account"; }
  }
}

// Sub-account transfer modal (fund / withdraw) — même pattern que setReferralModalOpen.
let _subtransferTarget = ""; // sub-account address ciblé
let _subtransferDirection = "fund"; // "fund" (Main → Sub) | "withdraw" (Sub → Main)
let _subtransferKind = "perp"; // "perp" | "spot"

function setSubtransferModalOpen(open, subAddress) {
  const modal = document.getElementById("subtransfer_modal");
  const overlay = document.getElementById("subtransfer_modal_overlay");
  if (!modal) return;
  const isOpen = !!open;
  if (isOpen) {
    _subtransferTarget = subAddress || "";
    clearError("subtransfer_error");
    const amountEl = document.getElementById("subtransfer_amount");
    if (amountEl) amountEl.value = "";
    // Préchauffe les soldes master même en direction Withdraw : la détection
    // unified du submit (choix du rail spot) en dépend.
    if (!_masterBalancesCache.data || Date.now() - _masterBalancesCache.ts > 15000) {
      apiGet("/api/hl/master_balances")
        .then((d) => { _masterBalancesCache = { ts: Date.now(), data: d }; })
        .catch(() => {});
    }
    _updateSubtransferTargetLine();
    _updateSubtransferAvail();
  }
  modal.classList.toggle("hidden", !isOpen);
  modal.setAttribute("aria-hidden", isOpen ? "false" : "true");
  if (overlay) {
    overlay.classList.toggle("hidden", !isOpen);
    overlay.setAttribute("aria-hidden", isOpen ? "false" : "true");
  }
}

function _setSubtransferDirection(dir) {
  _subtransferDirection = dir;
  const fundBtn = document.getElementById("subtransfer_dir_fund");
  const withdrawBtn = document.getElementById("subtransfer_dir_withdraw");
  if (fundBtn) fundBtn.classList.toggle("active", dir === "fund");
  if (withdrawBtn) withdrawBtn.classList.toggle("active", dir === "withdraw");
  _updateSubtransferTargetLine();
  _updateSubtransferAvail();
}

// Identité du transfert : QUEL sub, dans QUEL sens — sinon avec deux subs
// l'utilisateur ne sait pas où part son argent.
function _updateSubtransferTargetLine() {
  const sub = _hlSubaccounts.find(
    (s) => s.address.toLowerCase() === (_subtransferTarget || "").toLowerCase());
  const name = sub ? (sub.name || _shortAddr(sub.address)) : _shortAddr(_subtransferTarget);
  const title = document.getElementById("subtransfer_title");
  if (title) title.textContent = _subtransferDirection === "fund" ? `Fund ${name}` : `Withdraw from ${name}`;
  const line = document.getElementById("subtransfer_target_line");
  if (line) {
    const from = _subtransferDirection === "fund" ? "Main" : escapeHtml(name);
    const to = _subtransferDirection === "fund" ? escapeHtml(name) : "Main";
    line.innerHTML = `${from} <span class="sub-arrow">→</span> ${to}` +
      (sub ? ` <span class="sub-addr-inline">${_shortAddr(sub.address)}</span>` : "");
  }
}

function _setSubtransferKind(kind) {
  _subtransferKind = kind;
  const perpBtn = document.getElementById("subtransfer_kind_perp");
  const spotBtn = document.getElementById("subtransfer_kind_spot");
  if (perpBtn) perpBtn.classList.toggle("active", kind === "perp");
  if (spotBtn) spotBtn.classList.toggle("active", kind === "spot");
  _updateSubtransferAvail();
}

// Disponible du compte SOURCE du transfert : main (fund) ou sub (withdraw).
let _masterBalancesCache = { ts: 0, data: null };
let _subtransferAvailUsd = null;

async function _updateSubtransferAvail() {
  const el = document.getElementById("subtransfer_avail");
  if (!el) return;
  _subtransferAvailUsd = null;
  el.textContent = "…";
  const dir = _subtransferDirection;
  const kind = _subtransferKind;
  const target = _subtransferTarget;
  // Comptes UNIFIED : l'API HL rapporte tout l'USDC côté spot (perp
  // withdrawable = 0) alors que la marge est partagée — on affiche alors le
  // même disponible des deux côtés.
  const pick = (perp, spot) => {
    const p = perp || 0, s = spot || 0;
    const unified = p < 1 && s > 0;
    return kind === "perp" ? (unified ? s : p) : s;
  };
  let avail = null;
  if (dir === "withdraw") {
    const sub = _hlSubaccounts.find(
      (s) => s.address.toLowerCase() === (target || "").toLowerCase());
    if (sub) avail = pick(sub.perp_usd, sub.spot_usdc);
  } else {
    try {
      if (!_masterBalancesCache.data || Date.now() - _masterBalancesCache.ts > 15000) {
        _masterBalancesCache = { ts: Date.now(), data: await apiGet("/api/hl/master_balances") };
      }
      const d = _masterBalancesCache.data;
      // Guard : l'utilisateur a pu changer d'onglet direction/kind pendant l'await.
      if (_subtransferDirection !== dir || _subtransferKind !== kind) return;
      if (d) avail = pick(d.perp_usd, d.spot_usdc);
    } catch (_) { avail = null; }
  }
  _subtransferAvailUsd = avail;
  el.textContent = avail === null ? "-" : `$${avail.toFixed(2)} USDC`;
}

async function onSubtransferSubmit() {
  clearError("subtransfer_error");
  const amountEl = document.getElementById("subtransfer_amount");
  const amount = parseFloat(amountEl?.value || "");
  if (!Number.isFinite(amount) || amount <= 0) {
    showError("subtransfer_error", "Enter a valid amount.");
    return;
  }
  if (!_subtransferTarget) {
    showError("subtransfer_error", "No sub-account selected.");
    return;
  }
  const submitBtn = document.getElementById("subtransfer_submit");
  if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = "Transferring…"; }
  try {
    // Comptes UNIFIED : HL désactive subAccountTransfer ET subAccountSpotTransfer
    // ("Action disabled when unified account is active") — seule l'action
    // user-signed sendAsset est acceptée (c'est ce que fait l'app HL). Elle se
    // signe sur la chaîne courante du wallet, comme l'approve agent.
    const d = _masterBalancesCache.data;
    const unified = d && (d.perp_usd || 0) < 1 && (d.spot_usdc || 0) > 0;
    if (unified) {
      const signatureChainId = await ensureWalletSignatureChain(HL_SIGNATURE_CHAIN_ID_HEX);
      await subaccountAction("send_asset", {
        sub_address: _subtransferTarget,
        is_deposit: _subtransferDirection === "fund",
        amount_usd: amount,
        rail: _subtransferKind,
        signature_chain_id: signatureChainId,
      });
    } else {
      const kind = _subtransferKind === "perp" ? "transfer" : "spot_transfer";
      await subaccountAction(kind, {
        sub_address: _subtransferTarget,
        is_deposit: _subtransferDirection === "fund",
        amount_usd: amount,
      });
    }
    _msShowToast("Transfer submitted", "success");
    _masterBalancesCache = { ts: 0, data: null };
    await refreshHlSubaccounts();
    renderSubaccountsSection();
    renderTradingAccountSelector();
    // Refetch immédiat des soldes affichés (le serveur vient de purger ses
    // caches) : max size HIP-4 + notional MM suivent le transfert sans délai.
    if (typeof _hip4volFetchAvail === "function") _hip4volFetchAvail();
    if (typeof refreshMetrics === "function") refreshMetrics();
    setSubtransferModalOpen(false);
  } catch (err) {
    showError("subtransfer_error", err?.message || String(err));
  } finally {
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = "Transfer"; }
  }
}

function initSubtransferModal() {
  const overlay = document.getElementById("subtransfer_modal_overlay");
  const closeBtn = document.getElementById("subtransfer_close");
  const dirFund = document.getElementById("subtransfer_dir_fund");
  const dirWithdraw = document.getElementById("subtransfer_dir_withdraw");
  const kindPerp = document.getElementById("subtransfer_kind_perp");
  const kindSpot = document.getElementById("subtransfer_kind_spot");
  const submitBtn = document.getElementById("subtransfer_submit");
  if (overlay) overlay.addEventListener("click", () => setSubtransferModalOpen(false));
  if (closeBtn) closeBtn.addEventListener("click", () => setSubtransferModalOpen(false));
  if (dirFund) dirFund.addEventListener("click", () => _setSubtransferDirection("fund"));
  if (dirWithdraw) dirWithdraw.addEventListener("click", () => _setSubtransferDirection("withdraw"));
  if (kindPerp) kindPerp.addEventListener("click", () => _setSubtransferKind("perp"));
  if (kindSpot) kindSpot.addEventListener("click", () => _setSubtransferKind("spot"));
  if (submitBtn) submitBtn.addEventListener("click", onSubtransferSubmit);
  const maxBtn = document.getElementById("subtransfer_max");
  if (maxBtn) maxBtn.addEventListener("click", () => {
    const amountEl = document.getElementById("subtransfer_amount");
    if (amountEl && Number.isFinite(_subtransferAvailUsd) && _subtransferAvailUsd > 0) {
      amountEl.value = (Math.floor(_subtransferAvailUsd * 100) / 100).toFixed(2);
    }
  });
}

// Slot MM ciblé par les autosaves du start form (même logique que scheduleLiveConfigSync).
function _mmAutosaveTargetSlot() {
  const rs = _getRunningSlot(_currentSymbol());
  return rs ? rs.slot : activeDexSlot();
}

// Session RUNNING (ou degraded) hébergée sur un slot donné, sinon null.
// Inclut la fenêtre optimiste post-start (~15s, entrée poussée avec running:false
// avant confirmation API) : la session vient d'être lancée avec un vault précis,
// le guard doit déjà le préserver et les pills être verrouillées.
function _runningSessionOnSlot(slot) {
  return runningSlots.find(s => s.slot === slot
    && (s.running || s.degraded || (s._optimisticUntil && s._optimisticUntil > Date.now()))) || null;
}

// vault_address à écrire dans le TOML. Sécurité trading : si le slot ciblé
// héberge une session RUNNING, on préserve TOUJOURS le compte avec lequel elle
// a été lancée (payload /api/status → slot.trading_account, stocké dans
// runningSlots[].tradingAccount par pollStatus). Sinon (nouveau start, slot
// libre) : la valeur du picker. Ainsi les autosaves live (scheduleLiveConfigSync,
// applyRisk, mode-change) ne peuvent pas injecter le compte "browsé" dans le
// picker dans la config d'une session en cours.
function vaultAddressForToml(slot = null) {
  const target = Number.isInteger(slot) ? slot : _mmAutosaveTargetSlot();
  const live = _runningSessionOnSlot(target);
  if (live && typeof live.tradingAccount === "string") return live.tradingAccount;
  return getSelectedTradingAccount("mm");
}

// CTA "Fund sub-account" (Task 12) : injecté dans les messages d'erreur de
// precheck du start (solde insuffisant) quand un subaccount est sélectionné
// pour le scope concerné ("mm" ou "hip4"). Ouvre la modal transfert (Task 10)
// pré-remplie sur ce subaccount.
function _fundSubCtaHtml(scope) {
  const addr = typeof getSelectedTradingAccount === "function" ? getSelectedTradingAccount(scope) : "";
  if (!addr) return "";
  return ` <a href="#" class="fund-sub-cta" data-fund-cta-scope="${escapeHtml(scope)}">Fund sub-account</a>`;
}
document.addEventListener("click", (e) => {
  const cta = e.target && e.target.closest ? e.target.closest(".fund-sub-cta") : null;
  if (!cta) return;
  e.preventDefault();
  const scope = cta.dataset.fundCtaScope || "mm";
  const addr = typeof getSelectedTradingAccount === "function" ? getSelectedTradingAccount(scope) : "";
  if (addr && typeof setSubtransferModalOpen === "function") setSubtransferModalOpen(true, addr);
});

function initTradingAccountSelector() {
  _bindTradingAccountSelector("trading_account_selector", "mm");
  _bindTradingAccountSelector("hip4vol_ta_selector", "hip4");
  // Boutons "Fund" à côté des titres Trading Account : ouvrent la fenêtre
  // wallet directement sur la section Sub-accounts.
  document.querySelectorAll("[data-open-subaccounts]").forEach((btn) => {
    btn.addEventListener("click", (e) => { e.stopPropagation(); openSubaccountsManager(); });
  });
  try {
    for (const scope of ["mm", "hip4"]) {
      _selectedTradingAccount[scope] =
        localStorage.getItem(HL_TRADING_ACCOUNT_STORAGE_PREFIX + scope) || "";
    }
  } catch (_) {}
}

function _bindTradingAccountSelector(selId, scope) {
  const sel = document.getElementById(selId);
  if (!sel) return;
  sel.addEventListener("click", (e) => {
    // Verrou session running (défense en profondeur : pointer-events est CSS-only)
    if (sel.classList.contains("ta-disabled")) return;
    const btn = e.target.closest(".ta-option");
    if (!btn) return;
    if (scope === "mm" && isExtendedMode(document.getElementById("mode")?.value || "")) {
      const idx = parseInt(btn.dataset.taIndex || "0", 10) || 0;
      if (btn.classList.contains("ta-empty")) { void createExtendedSubaccount(idx); return; }
      setExtendedAccountIndex(idx);
      return;
    }
    if (btn.classList.contains("ta-empty")) { openSubaccountsManager(); return; }
    setSelectedTradingAccount(btn.dataset.ta || "", scope);
  });
}

// ── DEX Selector (Market Making) ─────────────────────────────────────────────

function updateDexSelectorUI() {
  const selector = document.getElementById("dex_selector");
  if (!selector) return;
  const cards = selector.querySelectorAll(".dex-card");
  cards.forEach((card) => {
    const dex = card.dataset.dex || "";
    const active = dex === _activeDex;
    const connected = isDexConnected(dex);
    card.classList.toggle("active", active);
    card.classList.toggle("is-connected", connected);
    card.setAttribute("aria-selected", active ? "true" : "false");
  });
  selector.classList.remove("dex-hyperliquid", "dex-extended", "dex-nado", "dex-risex", "dex-qfex");
  selector.classList.add("dex-" + _activeDex);
}

// ── Wallets Drawer ──────────────────────────────────────────────────────────

function openWalletsDrawer() {
  const overlay = document.getElementById("wallets_overlay");
  const drawer = document.getElementById("wallets_drawer");
  if (!overlay || !drawer) return;
  syncWalletsDrawerUI();
  overlay.classList.remove("hidden");
  overlay.setAttribute("aria-hidden", "false");
  drawer.classList.remove("hidden");
  drawer.setAttribute("aria-hidden", "false");
  requestAnimationFrame(() => drawer.classList.add("visible"));
}

function closeWalletsDrawer() {
  const overlay = document.getElementById("wallets_overlay");
  const drawer = document.getElementById("wallets_drawer");
  if (!overlay || !drawer) return;
  drawer.classList.remove("visible");
  overlay.classList.add("hidden");
  overlay.setAttribute("aria-hidden", "true");
  // Wait for transition to hide
  setTimeout(() => {
    drawer.classList.add("hidden");
    drawer.setAttribute("aria-hidden", "true");
  }, 350);
}

function syncWalletsDrawerUI() {
  updateWalletsBtnCount();
  // Show connected address(es)
  const addrEl = document.getElementById("wallets_account_addr");
  if (addrEl) {
    const uniqueAddrs = new Set(
      Object.values(_dexState).filter(s => s.connected && s.address).map(s => s.address)
    );
    if (uniqueAddrs.size === 0) {
      const addr = walletAuthAccount || "";
      addrEl.textContent = addr ? `${addr.slice(0, 6)}...${addr.slice(-4)}` : "-";
      addrEl.title = addr;
    } else if (uniqueAddrs.size === 1) {
      const addr = [...uniqueAddrs][0];
      addrEl.textContent = `${addr.slice(0, 6)}...${addr.slice(-4)}`;
      addrEl.title = addr;
    } else {
      addrEl.textContent = `${uniqueAddrs.size} wallets`;
      addrEl.title = [...uniqueAddrs].join(", ");
    }
  }

  // Nado retiré du frontend : venues Hyperliquid, Extended, RISEx et QFEX.
  for (const dex of WALLETS_BTN_VENUES) {
    const badge = document.getElementById("wallet_badge_" + dex);
    const action = document.getElementById("wallet_action_" + dex);
    const disconnectBtn = document.getElementById("wallet_disconnect_" + dex);
    const infoEl = document.getElementById("wallet_info_" + dex);
    const card = action?.closest(".wallets-card");
    const connected = isDexConnected(dex);

    if (badge) {
      badge.textContent = connected ? "Connected" : "Not connected";
      badge.className = "wallets-card-badge " + (connected ? "badge-connected" : "badge-disconnected");
    }
    if (action) {
      if (connected) {
        action.textContent = "Connected";
        action.className = "wallets-card-action action-connected";
      } else {
        action.textContent = "Connect";
        action.className = "wallets-card-action";
      }
    }
    if (disconnectBtn) {
      disconnectBtn.classList.toggle("hidden", !connected);
      if (REMOVE_KEY_ENDPOINTS[dex]) document.getElementById("wallet_removekey_" + dex)?.classList.toggle("hidden", !connected);
    }
    if (infoEl) {
      if (connected) {
        const state = _dexState[dex] || {};
        const parts = [];
        // Show address for this DEX
        const dexAddr = state.address || "";
        if (dexAddr) parts.push(`${dexAddr.slice(0, 6)}...${dexAddr.slice(-4)}`);
        if (dex === "hyperliquid") {
          if (state.builderApproved) parts.push("Builder OK");
          if (state.agentApproved) parts.push("Agent OK");
        } else if (dex === "extended") {
          if (state.credentialsSaved) parts.push("Credentials OK");
          if (state.builderApproved) parts.push("Builder OK");
        } else if (dex === "nado") {
          if (state.agentApproved) parts.push("Linked signer OK");
        } else if (dex === "risex") {
          if (state.builderApproved) parts.push("Builder OK");
          const rxSigner = _risexCachedSignerFor(dexAddr);
          if (rxSigner && Number.isFinite(Number(rxSigner.expires_in_days))) {
            const days = Math.max(0, Math.floor(Number(rxSigner.expires_in_days)));
            parts.push(_risexSignerReady(rxSigner) ? `Signer OK (${days} d left)` : "Signer to renew");
          } else if (state.signerReady) {
            parts.push("Signer OK");
          }
        }
        infoEl.textContent = parts.join(" · ") || "";
      } else {
        infoEl.textContent = "";
      }
    }
    if (card) {
      card.classList.toggle("connected", connected);
    }
  }

  renderSubaccountsSection();
}

function _disconnectSingleDex(dex) {
  if (!_dexState[dex]) return;
  const oldAddr = (_dexState[dex].address || "").toLowerCase();

  // Reset this DEX's state
  if (dex === "hyperliquid") {
    _dexState.hyperliquid = { connected: false, address: "", builderApproved: false, agentApproved: false };
    builderApprovedForAccount = false;
    agentApprovedForAccount = false;
  } else if (dex === "extended") {
    _dexState.extended = { connected: false, address: "", credentialsSaved: false, builderApproved: false };
  } else if (dex === "nado") {
    _dexState.nado = { connected: false, address: "", agentApproved: false };
    nadoAgentApprovedForAccount = false;
  } else if (dex === "risex") {
    _dexState.risex = { connected: false, address: "", builderApproved: false, signerReady: false };
    risexBuilderApprovedForAccount = false;
    risexSignerReadyForAccount = false;
    risexHasCollateral = null;
    risexStatusCache = { key: "", ts: 0, value: null };
    renderRisexExpiryBanner();
  } else if (dex === "qfex") {
    // Déconnexion locale : la clé reste stockée côté serveur (« Remove key » la supprime).
    _dexState.qfex = { connected: false, address: "", credentialsSaved: false };
    qfexCredentialsReadyForAccount = false;
    qfexStatusCache = { key: "", ts: 0, value: null };
  }
  _selectedDexes.delete(dex);

  // Remove token only if no other DEX uses this address
  if (oldAddr) {
    const stillUsed = Object.values(_dexState).some(s => s.connected && s.address.toLowerCase() === oldAddr);
    if (!stillUsed) {
      delete walletAuthTokens[oldAddr];
      delete walletAuthExpires[oldAddr];
    }
  }
  _syncPrimaryAuthGlobals();
  persistDexState();
  persistWalletAuthSession();

  // If disconnected DEX was active, switch to another connected one
  if (_activeDex === dex) {
    const remaining = connectedDexes();
    if (remaining.length > 0) {
      switchActiveDex(remaining[0]);
    }
  }
  updateDexSelectorUI();
  syncWalletsDrawerUI();
  syncModeCardsFromSelect();
}

function initWalletsDrawer() {
  const walletsBtn = document.getElementById("header_wallets_btn");
  const closeBtn = document.getElementById("wallets_close_btn");
  const overlay = document.getElementById("wallets_overlay");

  if (walletsBtn) walletsBtn.addEventListener("click", openWalletsDrawer);
  if (closeBtn) closeBtn.addEventListener("click", closeWalletsDrawer);
  if (overlay) overlay.addEventListener("click", closeWalletsDrawer);

  // Per-DEX connect buttons (Nado retiré du frontend → Hyperliquid, Extended, RISEx, QFEX)
  for (const dex of WALLETS_BTN_VENUES) {
    const btn = document.getElementById("wallet_action_" + dex);
    if (btn) {
      btn.addEventListener("click", async () => {
        if (isDexConnected(dex)) return;
        if (btn.disabled) return;
        btn.disabled = true;
        btn.textContent = "Connecting...";
        btn.classList.add("action-connecting");
        try {
          await _connectSingleDex(dex);
          syncWalletsDrawerUI();
          updateDexSelectorUI();
        } catch (e) {
          btn.textContent = "Failed : retry";
          btn.classList.remove("action-connecting");
        } finally {
          btn.disabled = false;
        }
      });
    }

    // Per-DEX disconnect buttons
    const disconnectBtn = document.getElementById("wallet_disconnect_" + dex);
    if (disconnectBtn) {
      disconnectBtn.addEventListener("click", () => {
        _disconnectSingleDex(dex);
      });
    }
  }

  // QFEX : suppression de la clé stockée sur Tradoor (refusée pendant une session).
  const qfexRemoveBtn = document.getElementById("wallet_removekey_qfex");
  if (qfexRemoveBtn) {
    qfexRemoveBtn.addEventListener("click", async () => {
      if (!window.confirm("Delete the QFEX API key stored on Tradoor? You will need to paste a key again to trade on QFEX.")) return;
      qfexRemoveBtn.disabled = true;
      try {
        await removeQfexKey();
        _disconnectSingleDex("qfex");
        syncWalletsDrawerUI();
        updateDexSelectorUI();
      } catch (e) {
        window.alert(`Could not remove the key: ${e.message || e}`);
      } finally {
        qfexRemoveBtn.disabled = false;
      }
    });
  }

  // Hyperliquid / Extended : suppression de l'agent ou des credentials stockés (refusée pendant une session).
  for (const dex of ["hyperliquid", "extended"]) {
    const btn = document.getElementById("wallet_removekey_" + dex);
    if (!btn) continue;
    btn.addEventListener("click", async () => {
      const label = DEX_LABELS[dex] || dex;
      if (!window.confirm(`Delete the ${label} key stored on Tradoor? You will need to connect ${label} again to trade.`)) return;
      btn.disabled = true;
      try {
        await removeStoredVenueKey(dex);
        _disconnectSingleDex(dex);
        syncWalletsDrawerUI();
        updateDexSelectorUI();
      } catch (e) {
        window.alert(`Could not remove the key: ${e.message || e}`);
      } finally {
        btn.disabled = false;
      }
    });
  }

  // Bandeau de renouvellement du signer RISEx (page principale).
  const risexRenewBtn = document.getElementById("risex_expiry_renew");
  if (risexRenewBtn) risexRenewBtn.addEventListener("click", () => { _risexOpenRenewal(); });

  // Disconnect all button
  const disconnectAllBtn = document.getElementById("wallets_disconnect_all");
  if (disconnectAllBtn) {
    disconnectAllBtn.addEventListener("click", async () => {
      closeWalletsDrawer();
      await disconnectWalletProfile();
      showLandingPage();
    });
  }

  // Sub-accounts: create + fund/withdraw (délégation sur la liste)
  const subaccountCreateBtn = document.getElementById("subaccount_create_btn");
  if (subaccountCreateBtn) subaccountCreateBtn.addEventListener("click", onCreateSubaccount);
  const subaccountsList = document.getElementById("wallets_subaccounts_list");
  if (subaccountsList) {
    subaccountsList.addEventListener("click", (e) => {
      const copyBtn = e.target.closest("[data-copy-addr]");
      if (copyBtn) {
        const addr = copyBtn.dataset.copyAddr || "";
        (navigator.clipboard?.writeText(addr) || Promise.reject())
          .then(() => _msShowToast("Address copied", "success"))
          .catch(() => _msShowToast(addr, "info")); // fallback : montre l'adresse
        return;
      }
      const fundBtn = e.target.closest("[data-fund]");
      if (fundBtn) {
        _setSubtransferDirection("fund");
        _setSubtransferKind("perp");
        setSubtransferModalOpen(true, fundBtn.dataset.fund);
        return;
      }
      const withdrawBtn = e.target.closest("[data-withdraw]");
      if (withdrawBtn) {
        _setSubtransferDirection("withdraw");
        _setSubtransferKind("perp");
        setSubtransferModalOpen(true, withdrawBtn.dataset.withdraw);
      }
    });
  }

  // Keyboard close
  document.addEventListener("keydown", (e) => {
    const drawer = document.getElementById("wallets_drawer");
    if (e.key === "Escape" && drawer && drawer.classList.contains("visible")) {
      closeWalletsDrawer();
    }
  });
}

function initDexSelector() {
  const selector = document.getElementById("dex_selector");
  if (!selector) return;
  const tabs = Array.from(selector.querySelectorAll(".dex-card")).filter(
    (n) => n instanceof HTMLButtonElement,
  );
  for (const tab of tabs) {
    tab.addEventListener("click", async () => {
      const dex = tab.dataset.dex || "";
      if (!dex) return;
      if (isDexConnected(dex)) {
        switchActiveDex(dex);
        return;
      }
      // DEX not connected : open wallets drawer
      openWalletsDrawer();
    });
  }
  updateDexSelectorUI();
}

// Garde in-flight : un double-clic sur « Connect » lançait deux flux concurrents
// (deux modals, deux séries de signatures) qui se marchaient dessus.
let _connectSingleDexInFlight = false;

async function _connectSingleDex(dex) {
  if (_connectSingleDexInFlight) return;
  _connectSingleDexInFlight = true;
  try {
    return await _connectSingleDexInner(dex);
  } finally {
    _connectSingleDexInFlight = false;
  }
}

async function _connectSingleDexInner(dex) {
  const label = DEX_LABELS[dex] || dex;
  clearError("main_error");
  _cmBindHandlers();
  try {
    await _getWalletProvider();
  } catch (e) {
    showError("main_error", `${label}: ${e && e.message ? e.message : String(e)}`);
    return;
  }
  _connectModalState.flowRunning = true;
  openConnectModal(dex);
  try {
    // Run the 3 steps through the modal (non-Nado → 3 steps, Nado → step 3 is a no-op marked done).
    _syncDexDomState(dex);
    if (!await _cmRunStep(0)) return;
    _syncDexDomState(dex);
    try {
      await _cmRefreshApprovalsForDex(dex);
    } catch (_) {
      // Échec du refresh on-chain : ne pas faire confiance au cache. Remettre les
      // flags à false force la ré-exécution des étapes (pas de skip sur état incertain).
      builderApprovedForAccount = false;
      agentApprovedForAccount = false;
      extendedCredentialsReadyForAccount = false;
      nadoAgentApprovedForAccount = false;
      risexBuilderApprovedForAccount = false;
      risexSignerReadyForAccount = false;
      qfexCredentialsReadyForAccount = false;
    }
    if (!await _cmRunStep(1)) return;
    if (dex === "nado" || dex === "qfex") {
      setConnectModalStep(2, { state: "done" });
    } else {
      if (!await _cmRunStep(2)) return;
    }

    // Mark dex connected + persist config. Les flags d'approbation reflètent le
    // vrai statut on-chain (rafraîchi par les steps), pas un true hardcodé : sinon
    // un état périmé fait croire « prêt » alors que /api/start refusera.
    if (dex === "hyperliquid") {
      _dexState.hyperliquid.connected = true;
      _dexState.hyperliquid.address = _dexState.hyperliquid.address || walletAuthAccount;
      _dexState.hyperliquid.builderApproved = !!builderApprovedForAccount;
      _dexState.hyperliquid.agentApproved = !!agentApprovedForAccount;
    } else if (dex === "extended") {
      _dexState.extended.connected = true;
      _dexState.extended.address = _dexState.extended.address || walletAuthAccount;
      _dexState.extended.credentialsSaved = !!extendedCredentialsReadyForAccount;
      _dexState.extended.builderApproved = !!builderApprovedForAccount;
    } else if (dex === "nado") {
      _dexState.nado.connected = true;
      _dexState.nado.address = _dexState.nado.address || walletAuthAccount;
      _dexState.nado.agentApproved = !!nadoAgentApprovedForAccount;
    } else if (dex === "risex") {
      _dexState.risex.connected = true;
      _dexState.risex.address = _dexState.risex.address || walletAuthAccount;
      _dexState.risex.builderApproved = !!risexBuilderApprovedForAccount;
      _dexState.risex.signerReady = !!risexSignerReadyForAccount;
    } else if (dex === "qfex") {
      _dexState.qfex.connected = true;
      _dexState.qfex.address = _dexState.qfex.address || walletAuthAccount;
      _dexState.qfex.credentialsSaved = !!qfexCredentialsReadyForAccount;
    }

    _syncDexDomState(dex);
    try {
      const toml = buildToml({ allowDefaults: true, slot: dexSlotNumber(dex) });
      await apiPost("/api/config?slot=" + dexSlotNumber(dex), { toml });
    } catch (_) {}

    persistDexState();
    _activeDex = dex;
    _syncDexDomState(dex);
    updateDexSelectorUI();
    updateMarketSourceForMode();
    await loadMarkets({ force: true });
    updateLeverageUI();
    updateNotionalUnitUI();
    closeConnectModal({ delay: 420 });
  } catch (e) {
    showError("main_error", `${label} connection failed: ${e.message || e}`);
  } finally {
    _connectModalState.flowRunning = false;
    // Connexion abandonnée ou ratée : l'adresse posée par l'étape 0 ne doit pas
    // survivre (elle ferait sauter l'étape 0 au prochain essai, sur un autre compte),
    // et le DOM / l'auth reviennent à la venue active.
    if (_dexState[dex] && !_dexState[dex].connected) {
      _dexState[dex].address = "";
      _syncDexDomState(_activeDex);
    }
  }
}

/** Atomically sync #mode + #account_address + auth globals to a specific DEX.
 *  MUST be called before any function that reads these DOM elements. */
function _syncDexDomState(dex) {
  const modeSelect = document.getElementById("mode");
  if (modeSelect) modeSelect.value = DEX_MODE_MAP[dex] || "hyperliquid_mainnet";
  const accountInput = document.getElementById("account_address");
  const addr = (_dexState[dex]?.address || walletAuthAccount || "").toLowerCase();
  if (accountInput) accountInput.value = addr;
  // Sync auth globals to THIS dex (not necessarily _activeDex)
  if (addr && walletAuthTokens[addr]) {
    walletAuthToken = walletAuthTokens[addr];
    walletAuthAccount = addr;
    walletAuthExpiresAtMs = walletAuthExpires[addr] || 0;
  } else {
    _syncPrimaryAuthGlobals();
  }
}

async function switchActiveDex(dex) {
  if (!PRIMARY_DEX_SLOT.hasOwnProperty(dex)) return;
  if (dex === _activeDex) return;
  _activeDex = dex;
  persistDexState();
  _syncDexDomState(dex);
  try { localStorage.setItem(EXCHANGE_MODE_STORAGE_KEY, DEX_MODE_MAP[dex] || "hyperliquid_mainnet"); } catch (_) {}

  // Clear stale balance from previous DEX : show spinner until real data arrives
  latestAvailableNotional = null;
  setNotionalLoading(document.getElementById("available_notional"));

  // Update source selector visibility
  updateMarketSourceForMode();

  // Update DEX tab UI
  updateDexSelectorUI();

  // Reload markets for this DEX
  await loadMarkets({ force: true });
  updateLeverageUI();
  updateNotionalUnitUI();
  updatePnlPerMillionLabel();

  // Fetch metrics for the new slot immediately (don't wait for 3s polling).
  metricsInFlight = false;
  refreshMetrics();

  // Config for this slot will be loaded when refreshStatus runs next.
  // Do NOT call scheduleLiveConfigSync here : it would rebuild the TOML
  // from the previous DEX's UI state and overwrite the target slot's config.
}

function initSourceSelector() {
  const source = document.getElementById("market_source");
  const selector = document.getElementById("source_selector");
  if (!source || !selector) return;
  const items = Array.from(selector.querySelectorAll(".source-option")).filter((n) => n instanceof HTMLButtonElement);
  const applySource = (nextValue) => {
    const v = String(nextValue || "").trim();
    if (!v || source.disabled) return;
    // Clear stale account warning immediately on source switch.
    lastSourceChangedAt = Date.now();
    const warningEl = document.getElementById("account_create_warning");
    if (warningEl) { warningEl.innerHTML = ""; warningEl.classList.add("hidden"); }
    if (source.value !== v) {
      source.value = v;
      source.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      syncSourceSelectorFromSelect();
    }
  };
  items.forEach((btn, idx) => {
    const warmSource = () => {
      const sourceValue = String(btn.dataset.source || "");
      if (!sourceValue) return;
      const modeValue = document.getElementById("mode")?.value || "hyperliquid_mainnet";
      prewarmMarketSourcesForMode(modeValue, [sourceValue]);
    };
    btn.addEventListener("mouseenter", warmSource);
    btn.addEventListener("focus", warmSource);
    btn.addEventListener("click", () => applySource(btn.dataset.source || ""));
    btn.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        applySource(btn.dataset.source || "");
        return;
      }
      if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
        ev.preventDefault();
        const step = ev.key === "ArrowRight" ? 1 : -1;
        const next = (idx + step + items.length) % items.length;
        const target = items[next];
        if (target) {
          target.focus();
          applySource(target.dataset.source || "");
        }
      }
    });
  });
  source.addEventListener("change", () => {
    syncSourceSelectorFromSelect();
  });
  syncSourceSelectorFromSelect();
}

function setTokenPickerOpen(open) {
  const menu = document.getElementById("market_symbol_menu");
  const btn = document.getElementById("market_symbol_btn");
  const chevron = document.getElementById("market_symbol_chevron");
  if (!menu || !btn) return;
  menu.classList.toggle("hidden", !open);
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (chevron) chevron.setAttribute("aria-expanded", open ? "true" : "false");
}

let _tokenPickerActiveGroup = "all"; // "all" or optgroup label lowercased
let _tokenPickerHasGroups = false;   // set by applyMarketsToSelect
let _tokenPickerReduceOnly = new Set(); // symbols in reduce-only mode

function syncTokenPickerFromSelect(filterText = "") {
  const select = document.getElementById("market_symbol");
  const label = document.getElementById("market_symbol_label");
  const list = document.getElementById("market_symbol_list");
  const tabs = document.getElementById("market_symbol_group_tabs");
  if (!select || !label || !list) return;
  const current = select.options[select.selectedIndex];
  label.innerHTML = "";
  if (current) {
    const val = current.value || "";
    label.appendChild(_tokenIconImg(val, 20));
    label.appendChild(document.createTextNode(current.textContent || val));
  } else {
    label.textContent = "Select token";
  }

  const filter = String(filterText || "").trim().toLowerCase();
  const hasGroups = _tokenPickerHasGroups;
  // Tabs visibility is controlled only by applyMarketsToSelect, not here.

  list.innerHTML = "";

  const addOption = (option) => {
    const rawText = String(option.textContent || option.value || "");
    if (filter && !rawText.toLowerCase().includes(filter)) return;
    const item = document.createElement("button");
    item.type = "button";
    item.className = "token-picker-item";
    item.dataset.value = option.value;
    item.setAttribute("role", "option");
    const active = option.value === select.value;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", active ? "true" : "false");
    item.appendChild(_tokenIconImg(option.value, 18));
    item.appendChild(document.createTextNode(rawText));
    if (isNadoMode() && NADO_BOOSTED_SYMBOLS.has(option.value)) {
      const badge = document.createElement("span");
      badge.className = "token-boost-badge";
      badge.textContent = "4x pts";
      item.appendChild(badge);
    }
    if (isNadoMode() && NADO_OI_CAP_SYMBOLS.has(option.value)) {
      const oiBadge = document.createElement("span");
      oiBadge.className = "token-reduce-only-badge";
      oiBadge.textContent = "OI CAP";
      item.appendChild(oiBadge);
    }
    // Reduce-only badge + disable click
    if (_tokenPickerReduceOnly.has(option.value)) {
      const roBadge = document.createElement("span");
      roBadge.className = "token-reduce-only-badge";
      roBadge.textContent = "REDUCE ONLY";
      item.appendChild(roBadge);
      item.classList.add("token-picker-item--reduce-only");
      item.disabled = true;
    } else {
      item.addEventListener("click", () => {
        if (select.value !== option.value) {
          select.value = option.value;
          select.dispatchEvent(new Event("change", { bubbles: true }));
        }
        setTokenPickerOpen(false);
        requestAnimationFrame(() => setTokenPickerOpen(false));
      });
    }
    list.appendChild(item);
  };

  if (hasGroups) {
    for (const child of Array.from(select.children)) {
      if (child.tagName === "OPTGROUP") {
        const groupKey = (child.label || "").trim().toLowerCase();
        // If a group tab is active (not "all"), skip other groups
        if (_tokenPickerActiveGroup !== "all" && groupKey !== _tokenPickerActiveGroup) continue;
        const opts = Array.from(child.children).filter((o) => {
          const t = String(o.textContent || o.value || "").toLowerCase();
          return !filter || t.includes(filter);
        });
        if (!opts.length) continue;
        opts.forEach((o) => addOption(o));
      } else if (child.tagName === "OPTION") {
        addOption(child);
      }
    }
  } else {
    for (const option of Array.from(select.options)) {
      addOption(option);
    }
  }
}

function initTokenPicker() {
  const select = document.getElementById("market_symbol");
  const wrap = document.getElementById("token_picker");
  const btn = document.getElementById("market_symbol_btn");
  const chevron = document.getElementById("market_symbol_chevron");
  const menu = document.getElementById("market_symbol_menu");
  const search = document.getElementById("market_symbol_search");
  if (!select || !wrap || !btn || !menu || !search) return;

  // Tab click handlers
  const tabsEl = document.getElementById("market_symbol_group_tabs");
  if (tabsEl) {
    tabsEl.querySelectorAll(".token-picker-tab").forEach((tab) => {
      tab.addEventListener("mousedown", (e) => {
        // Prevent focus leaving the search input, which would trigger close handlers
        e.preventDefault();
      });
      tab.addEventListener("click", (e) => {
        e.stopPropagation();
        tabsEl.querySelectorAll(".token-picker-tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        _tokenPickerActiveGroup = (tab.dataset.group || "all").toLowerCase();
        syncTokenPickerFromSelect(search.value);
      });
    });
  }

  const openTokenPicker = (focusSearch = true) => {
    _tokenPickerActiveGroup = "all";
    if (tabsEl) {
      tabsEl.querySelectorAll(".token-picker-tab").forEach((t) => t.classList.toggle("active", t.dataset.group === "all"));
    }
    setTokenPickerOpen(true);
    search.value = "";
    syncTokenPickerFromSelect("");
    if (focusSearch) requestAnimationFrame(() => search.focus());
  };

  const moveActive = (dir) => {
    const items = Array.from(menu.querySelectorAll(".token-picker-item")).filter((n) => n instanceof HTMLButtonElement);
    if (!items.length) return;
    const idx = items.findIndex((n) => n.classList.contains("active"));
    const nextIdx = idx < 0 ? 0 : (idx + dir + items.length) % items.length;
    const target = items[nextIdx];
    if (!target) return;
    target.focus();
    const val = String(target.dataset.value || "");
    if (val && select.value !== val) {
      select.value = val;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
  };

  btn.addEventListener("click", () => {
    if (window._openMmTokenModal) {
      window._openMmTokenModal();
    } else {
      const willOpen = menu.classList.contains("hidden");
      if (willOpen) openTokenPicker(true);
      else setTokenPickerOpen(false);
    }
  });
  btn.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      if (menu.classList.contains("hidden")) {
        openTokenPicker(true);
      }
      moveActive(1);
    }
  });
  if (chevron) {
    chevron.addEventListener("click", (ev) => {
      ev.preventDefault();
      if (window._openMmTokenModal) {
        window._openMmTokenModal();
      } else {
        const willOpen = menu.classList.contains("hidden");
        if (willOpen) openTokenPicker(false);
        else setTokenPickerOpen(false);
      }
    });
  }

  search.addEventListener("input", () => {
    syncTokenPickerFromSelect(search.value);
  });
  search.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      moveActive(1);
      return;
    }
    if (ev.key === "ArrowUp") {
      ev.preventDefault();
      moveActive(-1);
      return;
    }
    if (ev.key === "Escape") {
      ev.preventDefault();
      setTokenPickerOpen(false);
      btn.focus();
    }
  });

  menu.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      const target = ev.target;
      if (target instanceof HTMLButtonElement && target.classList.contains("token-picker-item")) {
        ev.preventDefault();
        target.click();
        setTokenPickerOpen(false);
        btn.focus();
      }
      return;
    }
    if (ev.key === "Escape") {
      ev.preventDefault();
      setTokenPickerOpen(false);
      btn.focus();
    }
  });

  select.addEventListener("change", () => {
    syncTokenPickerFromSelect(search.value);
    setTokenPickerOpen(false);
    requestAnimationFrame(() => setTokenPickerOpen(false));
  });

  document.addEventListener("click", (ev) => {
    const target = ev.target;
    if (!(target instanceof Element)) return;
    if (!wrap.contains(target)) setTokenPickerOpen(false);
  });

  syncTokenPickerFromSelect("");
}

// ── MM Token Selector Modal ─────────────────────────────────────────────────

let _mmTokenModalSort = "volume";
let _mmTokenModalSortAsc = false;
// Groupes de marchés de la venue active (ex. Extended : All / TradFi) + onglet actif.
let _mmMarketGroups = null;
let _mmTokenModalGroup = "All";
// Min trade size par marché ({symbol: qty} / {symbol: usd}), servi par /api/markets.
let marketMinOrderSizeMap = {};
let marketMinOrderUsdMap = {};
// Stats venue par marché ({symbol: {price, change_pct, volume_usd}}) — utilisées
// par le token picker quand les données funding HL ne couvrent pas le marché.
let marketStatsMap = {};
let _mmTokenFavorites = (() => {
  try { return JSON.parse(localStorage.getItem("mm_token_fav") || "[]"); } catch (_) { return []; }
})();

function _fmtUsd(v) {
  if (v == null || isNaN(v) || v === 0) return "-";
  if (v >= 1e9) return "$" + (v / 1e9).toFixed(2) + "B";
  if (v >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
  if (v >= 1e3) return "$" + (v / 1e3).toFixed(1) + "K";
  return "$" + v.toFixed(0);
}

function _fmtPrice(v) {
  if (v == null || isNaN(v) || v === 0) return "-";
  if (v >= 1000) return "$" + v.toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (v >= 1) return "$" + v.toFixed(4);
  return "$" + v.toPrecision(4);
}

function _coinDisplay(coin) {
  // "xyz:AAPL-USDC" -> "AAPL"  /  "BTC" -> "BTC"
  let name = coin;
  if (name.includes(":")) name = name.split(":")[1];
  if (name.includes("-")) name = name.split("-")[0];
  return escapeHtml(name);
}

function _mmTokenModalRenderTabs() {
  const tabsEl = document.getElementById("mm_token_modal_tabs");
  if (!tabsEl) return;
  const groups = Array.isArray(_mmMarketGroups) ? _mmMarketGroups : null;
  if (!groups || groups.length < 2) {
    tabsEl.classList.add("hidden");
    tabsEl.innerHTML = "";
    return;
  }
  tabsEl.classList.remove("hidden");
  tabsEl.innerHTML = "";
  // Groupes disjoints (RISEx : Crypto / TradFi) : onglet « All » virtuel en tête,
  // _mmTokenModalGroupSet renvoie déjà null (tout afficher) pour "All".
  const tabs = groups.some((g) => g.label === "All") ? groups : [{ label: "All" }, ...groups];
  for (const group of tabs) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mm-token-modal-tab" + (group.label === _mmTokenModalGroup ? " active" : "");
    btn.dataset.group = group.label;
    btn.textContent = group.label;
    btn.addEventListener("mousedown", (e) => e.preventDefault());
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      _mmTokenModalGroup = group.label;
      _mmTokenModalRenderTabs();
      _mmTokenModalRender();
    });
    tabsEl.appendChild(btn);
  }
}

function _mmTokenModalGroupSet() {
  const groups = Array.isArray(_mmMarketGroups) ? _mmMarketGroups : null;
  if (!groups || _mmTokenModalGroup === "All") return null;
  const group = groups.find((g) => g.label === _mmTokenModalGroup);
  return group ? new Set(group.symbols) : null;
}

function _mmTokenModalRender() {
  const tbody = document.getElementById("mm_token_modal_body");
  const select = document.getElementById("market_symbol");
  if (!tbody || !select) return;
  const search = (document.getElementById("mm_token_modal_search")?.value || "").toLowerCase();
  const currentVal = select.value;
  const groupSet = _mmTokenModalGroupSet();

  let rows = [];
  const seen = new Set();
  for (const opt of Array.from(select.options)) {
    const val = opt.value;
    const label = opt.textContent || val;
    // Les optgroups (All + TradFi) dupliquent les symboles dans le select : dédup.
    if (seen.has(val)) continue;
    seen.add(val);
    if (groupSet && !groupSet.has(val)) continue;
    if (search && !label.toLowerCase().includes(search)) continue;
    const ms = marketStatsMap[val];
    const price = ms && Number.isFinite(ms.price) ? ms.price : NaN;
    const change = ms && Number.isFinite(ms.change_pct) ? ms.change_pct : NaN;
    const volume = ms && Number.isFinite(ms.volume_usd) ? ms.volume_usd : NaN;
    const isFav = _mmTokenFavorites.includes(val);
    const isReduceOnly = _tokenPickerReduceOnly.has(val);
    const isOiCap = typeof NADO_OI_CAP_SYMBOLS !== "undefined" && NADO_OI_CAP_SYMBOLS.has(val);
    const isBoosted = typeof NADO_BOOSTED_SYMBOLS !== "undefined" && isNadoMode() && NADO_BOOSTED_SYMBOLS.has(val);
    rows.push({ value: val, label, price, change, volume, isFav, isReduceOnly, isOiCap, isBoosted });
  }

  // Sort
  const sk = _mmTokenModalSort;
  const dir = _mmTokenModalSortAsc ? 1 : -1;
  rows.sort((a, b) => {
    if (a.isFav !== b.isFav) return a.isFav ? -1 : 1;
    let va, vb;
    if (sk === "symbol") { va = a.label; vb = b.label; return va < vb ? -dir : va > vb ? dir : 0; }
    if (sk === "price") { va = a.price || 0; vb = b.price || 0; }
    else if (sk === "change") { va = a.change || 0; vb = b.change || 0; }
    else if (sk === "volume") { va = a.volume || 0; vb = b.volume || 0; }
    else { va = a.volume || 0; vb = b.volume || 0; }
    return (va - vb) * dir;
  });

  // Sort indicators
  document.querySelectorAll("#mm_token_modal .mm-token-modal-table th").forEach(th => {
    th.classList.toggle("sort-active", th.dataset.sort === sk);
  });

  let html = "";
  for (const r of rows) {
    const sel = r.value === currentVal ? " selected" : "";
    const disc = r.isReduceOnly ? " disconnected" : "";
    const priceFmt = Number.isFinite(r.price) ? _fmtPrice(r.price) : "-";
    const changeFmt = Number.isFinite(r.change) ? (r.change >= 0 ? "+" : "") + r.change.toFixed(2) + "%" : "-";
    const changeCls = r.change > 0 ? "mm-tm-pos" : r.change < 0 ? "mm-tm-neg" : "";
    const volFmt = Number.isFinite(r.volume) ? _fmtUsd(r.volume) : "-";
    const favCls = r.isFav ? " is-fav" : "";

    let badges = "";
    if (r.isBoosted) badges += '<span class="token-boost-badge" style="margin-left:4px">4x pts</span>';
    if (r.isOiCap) badges += '<span class="token-reduce-only-badge" style="margin-left:4px">OI CAP</span>';
    if (r.isReduceOnly) badges += '<span class="token-reduce-only-badge" style="margin-left:4px">REDUCE ONLY</span>';

    html += `<tr class="${sel}${disc}" data-value="${_escAttr(r.value)}">
      <td class="mm-col-fav"><button type="button" class="mm-tm-fav-btn${favCls}" data-fav="${_escAttr(r.value)}">&#9733;</button></td>
      <td><div class="mm-tm-token-cell"><img class="token-icon" src="${_escAttr(_tokenIconUrl(r.value))}" data-fallback="${_escAttr(_tokenIconFallbackUrl(r.value))}" width="20" height="20" alt="" loading="lazy" onerror="_tokenIconError(this)">${escapeHtml(r.label)}${badges}</div></td>
      <td class="mm-col-right">${priceFmt}</td>
      <td class="mm-col-right ${changeCls}">${changeFmt}</td>
      <td class="mm-col-right">${volFmt}</td>
    </tr>`;
  }
  if (!html) html = '<tr><td colspan="5" style="text-align:center;padding:24px;color:rgba(239,245,255,0.3)">No tokens found</td></tr>';
  tbody.innerHTML = html;
}

function openMmTokenModal() {
  const overlay = document.getElementById("mm_token_modal_overlay");
  const modal = document.getElementById("mm_token_modal");
  const search = document.getElementById("mm_token_modal_search");
  if (!overlay || !modal) return;
  if (search) search.value = "";
  overlay.classList.remove("hidden");
  overlay.setAttribute("aria-hidden", "false");
  modal.classList.remove("hidden");
  _mmTokenModalGroup = "All";
  _mmTokenModalRenderTabs();
  _mmTokenModalRender();
  if (search) search.focus();
}
window._openMmTokenModal = openMmTokenModal;

function closeMmTokenModal() {
  const overlay = document.getElementById("mm_token_modal_overlay");
  const modal = document.getElementById("mm_token_modal");
  if (overlay) { overlay.classList.add("hidden"); overlay.setAttribute("aria-hidden", "true"); }
  if (modal) modal.classList.add("hidden");
}

// Event listeners
document.getElementById("mm_token_modal_close")?.addEventListener("click", closeMmTokenModal);
document.getElementById("mm_token_modal_overlay")?.addEventListener("click", closeMmTokenModal);
document.addEventListener("keydown", (e) => {
  const modal = document.getElementById("mm_token_modal");
  if (e.key === "Escape" && modal && !modal.classList.contains("hidden")) closeMmTokenModal();
});
document.getElementById("mm_token_modal_search")?.addEventListener("input", _mmTokenModalRender);

// Sort headers
document.querySelector("#mm_token_modal .mm-token-modal-table thead")?.addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  const key = th.dataset.sort;
  if (_mmTokenModalSort === key) _mmTokenModalSortAsc = !_mmTokenModalSortAsc;
  else { _mmTokenModalSort = key; _mmTokenModalSortAsc = key === "symbol"; }
  _mmTokenModalRender();
});

// Row click + fav toggle
document.getElementById("mm_token_modal_body")?.addEventListener("click", (e) => {
  const favBtn = e.target.closest(".mm-tm-fav-btn");
  if (favBtn) {
    e.stopPropagation();
    const val = favBtn.dataset.fav;
    const idx = _mmTokenFavorites.indexOf(val);
    if (idx >= 0) _mmTokenFavorites.splice(idx, 1); else _mmTokenFavorites.push(val);
    try { localStorage.setItem("mm_token_fav", JSON.stringify(_mmTokenFavorites)); } catch (_) {}
    _mmTokenModalRender();
    return;
  }
  const row = e.target.closest("tr[data-value]");
  if (!row || row.classList.contains("disconnected")) return;
  const val = row.dataset.value;
  const select = document.getElementById("market_symbol");
  if (select && select.value !== val) {
    select.value = val;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }
  closeMmTokenModal();
});

function updateModeGateUi() {
  const extended = isExtendedMode();
  updateModeSelectLogo();
  setExtendedHelpVisibility(extended);
  updateMarketSourceForMode();
  updateConnectButtonLabel();
  updateConnectStepsState();
}

function updateBuilderApproveButtonState(approved) {
  const btn = document.getElementById("builder_approve_btn");
  if (btn) {
    const ok = !!approved;
    btn.classList.toggle("connected", ok);
    btn.textContent = ok ? "Builder Fee Approved" : "Approve Builder Fee";
    updateSetupAllButtonState();
  }
  updateConnectButtonLabel();
}

function updateAgentApproveButtonState(approved) {
  const btn = document.getElementById("agent_approve_btn");
  if (btn) {
    const ok = !!approved;
    btn.classList.toggle("connected", ok);
    btn.textContent = ok ? "TRADOR Agent Approved" : "Approve TRADOR Agent";
    updateSetupAllButtonState();
  }
  updateConnectButtonLabel();
}

function updateSetupAllButtonState() {
  const btn = document.getElementById("setup_all_btn");
  if (!btn) return;
  let ready;
  if (isNadoMode()) {
    ready = !!nadoAgentApprovedForAccount;
  } else {
    ready = !!(builderApprovedForAccount && agentApprovedForAccount);
  }
  btn.classList.toggle("connected", ready);
  btn.textContent = ready ? "All Signatures Ready" : "Setup All Signatures";
}

function updateWalletAuthButtonState() {
  const btn = document.getElementById("wallet_sign_btn");
  if (!btn) return;
  const account = document.getElementById("account_address")?.value?.trim() || "";
  const connected = isWalletAuthValidForAccount(account);
  btn.classList.toggle("connected", connected);
  btn.textContent = connected ? "Wallet Signed" : "Connect & Sign Wallet";
  updateConnectButtonLabel();
}

function setSignatureStepsVisible(visible) {
  const row = document.getElementById("signature_steps");
  if (!row) return;
  row.classList.toggle("hidden", !visible);
}

function updateConnectButtonLabel() {
  const btn = document.getElementById("connect_continue");
  if (!btn || btn.classList.contains("loading")) return;
  // Multi-DEX mode: use _selectedDexes count
  const n = _selectedDexes.size;
  if (n === 0) {
    btn.textContent = "Select a platform";
    btn.disabled = true;
    return;
  }
  // Check if any connected dex exists (for "Enter" label)
  const anyConnected = connectedDexes().length > 0;
  const account = document.getElementById("account_address")?.value?.trim() || "";
  const hasAuth = isWalletAuthValidForAccount(account);
  if (anyConnected && hasAuth) {
    btn.textContent = "Enter";
  } else {
    btn.textContent = n > 1 ? `Connect (${n})` : "Connect";
  }
  btn.disabled = false;
}

function renderTitleWordmark(mode = "default") {
  // Logo is now a static SVG img, no dynamic wordmark needed
  void mode;
}

function setLandingTitleImageVariant(variant) {
  void variant;
  renderTitleWordmark("default");
}

function applyLogoVariant(variant, opts = {}) {
  const value = ["v1", "v2", "v3", "v4"].includes(String(variant)) ? String(variant) : "v1";
  document.body.setAttribute("data-logo-variant", value);
  document.querySelectorAll(".logo-card").forEach((el) => {
    const isActive = el.getAttribute("data-logo-variant") === value;
    el.classList.toggle("active", isActive);
  });
  if (opts.persist !== false) {
    try {
      localStorage.setItem(LOGO_VARIANT_STORAGE_KEY, value);
    } catch (_) {
      // ignore storage failures
    }
  }
  setLandingTitleImageVariant(value);
}

function initLogoVariantPicker() {
  // Default landing logo: white TRADOOR with eyes.
  applyLogoVariant("v4", { persist: false });
  document.querySelectorAll(".logo-card").forEach((el) => {
    el.addEventListener("click", () => {
      const variant = el.getAttribute("data-logo-variant") || "v1";
      applyLogoVariant(variant);
    });
  });
}

function setTitleHyperliquidOs(enabled) {
  renderTitleWordmark(enabled ? "hyperliquid" : "default");
}

async function refreshWalletAuthStatus(opts = {}) {
  const force = !!opts.force;
  if (walletAuthCheckInFlight && !force) return { authenticated: false };
  const account = document.getElementById("account_address")?.value?.trim() || "";
  if (!account || !account.startsWith("0x")) {
    setWalletAuthStatus("1/3 Wallet signature required.", "warn");
    updateWalletAuthButtonState();
    return { authenticated: false };
  }
  if (isWalletAuthValidForAccount(account) && !force) {
    setWalletAuthStatus("1/3 Wallet verified.", "ok");
    updateWalletAuthButtonState();
    return { authenticated: true };
  }
  _activateAuthForAccount(account);
  // La vérification ne vaut que si elle porte le token DU compte interrogé :
  // avec le token "actif" d'un autre compte, le serveur répond authenticated:false
  // (HTTP 200) et on déconnectait tout le monde sur un simple mismatch.
  const tokenForAcct = _tokenForAddress(normalizeAccount(account));
  if (!tokenForAcct) {
    setWalletAuthStatus("1/3 Wallet signature required.", "warn");
    updateWalletAuthButtonState();
    return { authenticated: false };
  }
  walletAuthCheckInFlight = true;
  try {
    const res = await fetchWithTimeout(
      `/api/auth/status?account_address=${encodeURIComponent(account)}`,
      { headers: { "X-Mega-Auth": tokenForAcct } }, API_TIMEOUT_MS);
    if (!res.ok) {
      const err = new Error(`HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    const status = await res.json();
    if (status && status.authenticated) {
      walletAuthAccount = normalizeAccount(status.account_address || account);
      const expMs = Date.parse(String(status.expires_at || ""));
      walletAuthExpiresAtMs = Number.isFinite(expMs) ? expMs : Date.now() + 5 * 60 * 1000;
      persistWalletAuthSession();
      setWalletAuthStatus("1/3 Wallet verified.", "ok");
      updateWalletAuthButtonState();
      return { authenticated: true };
    }
    // authenticated:false confirmé avec le bon couple token+account : ne clearer
    // QUE ce compte, les autres wallets restent connectés.
    clearWalletAuthForAccount(account);
    setWalletAuthStatus("1/3 Signature expired. Sign again.", "warn");
    updateWalletAuthButtonState();
    return { authenticated: false };
  } catch (e) {
    // Transient error (network, timeout, 5xx). Don't punish the user with a
    // forced logout - the 401 handlers in apiGet/apiPost will invalidate the
    // session if the server actually rejects a real call. Keeping the cached
    // token avoids the "I close the tab, come back, I'm signed out" symptom
    // when the boot revalidation hits a slow server or a flaky connection.
    console.warn("[auth] revalidate transient error, keeping cached token:", e?.message || e);
    setWalletAuthStatus("1/3 Wallet verified.", "ok");
    updateWalletAuthButtonState();
    return { authenticated: true, transient: true };
  } finally {
    walletAuthCheckInFlight = false;
  }
}

/* ── EIP-6963 Wallet Picker ──────────────────────────────────────────────── */
let _eip6963Providers = [];
let _tradoorActiveProvider = null;

function _detectEip6963Providers() {
  return new Promise((resolve) => {
    const collected = [];
    const handler = (e) => {
      if (e.detail && e.detail.info && e.detail.provider) {
        if (!collected.find(p => p.info.rdns === e.detail.info.rdns)) {
          collected.push(e.detail);
        }
      }
    };
    window.addEventListener("eip6963:announceProvider", handler);
    try { window.dispatchEvent(new Event("eip6963:requestProvider")); } catch (_) {}
    setTimeout(() => {
      window.removeEventListener("eip6963:announceProvider", handler);
      _eip6963Providers = collected;
      resolve(collected);
    }, 120);
  });
}

function _escapeHtmlSafe(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function _showWalletPicker(providers) {
  return new Promise((resolve, reject) => {
    const modal = document.getElementById("wallet_picker_modal");
    const list = document.getElementById("wp_list");
    const closeBtn = document.getElementById("wp_close");
    const overlay = document.getElementById("wp_overlay");
    if (!modal || !list) {
      resolve(providers[0] || null);
      return;
    }

    list.innerHTML = "";
    if (!providers || providers.length === 0) {
      list.innerHTML = '<div class="wp-empty">No wallet extension detected.<br/>Install Rabby, MetaMask, or another Ethereum wallet to continue.</div>';
    } else {
      providers.forEach((p) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "wp-item";
        const name = _escapeHtmlSafe(p.info.name || "Wallet");
        const icon = p.info.icon || "";
        const iconHtml = icon
          ? `<img class="wp-icon" src="${_escapeHtmlSafe(icon)}" alt="${name}" onerror="this.outerHTML='<div class=&quot;wp-icon-fallback&quot;>'+this.alt.charAt(0).toUpperCase()+'</div>'"/>`
          : `<div class="wp-icon-fallback">${name.charAt(0).toUpperCase()}</div>`;
        item.innerHTML = `${iconHtml}<div class="wp-name">${name}</div><div class="wp-arrow">&rarr;</div>`;
        item.addEventListener("click", () => {
          cleanup();
          try { localStorage.setItem("tradoor_wallet_rdns", p.info.rdns); } catch (_) {}
          resolve(p);
        });
        list.appendChild(item);
      });
    }

    function cleanup() {
      modal.classList.add("hidden");
      modal.setAttribute("aria-hidden", "true");
      if (closeBtn) closeBtn.removeEventListener("click", onClose);
      if (overlay) overlay.removeEventListener("click", onClose);
      document.removeEventListener("keydown", onKey);
    }
    function onClose() { cleanup(); reject(new Error("Wallet selection cancelled.")); }
    function onKey(e) { if (e.key === "Escape") onClose(); }

    if (closeBtn) closeBtn.addEventListener("click", onClose);
    if (overlay) overlay.addEventListener("click", onClose);
    document.addEventListener("keydown", onKey);

    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
  });
}

async function _getWalletProvider() {
  // If already selected in this session, reuse
  if (_tradoorActiveProvider) return _tradoorActiveProvider;

  // Discover EIP-6963 providers
  const providers = await _detectEip6963Providers();

  // No EIP-6963 wallets: fall back to window.ethereum (legacy injection)
  if (providers.length === 0) {
    if (window.ethereum && typeof window.ethereum.request === "function") {
      _tradoorActiveProvider = window.ethereum;
      return window.ethereum;
    }
    throw new Error("No Ethereum wallet found. Install Rabby, MetaMask, or another wallet extension.");
  }

  // Only one wallet: use it directly, no picker
  if (providers.length === 1) {
    _tradoorActiveProvider = providers[0].provider;
    try { window.ethereum = providers[0].provider; } catch (_) {}
    return providers[0].provider;
  }

  // Multiple wallets: check saved preference
  let savedRdns = null;
  try { savedRdns = localStorage.getItem("tradoor_wallet_rdns"); } catch (_) {}
  if (savedRdns) {
    const found = providers.find(p => p.info.rdns === savedRdns);
    if (found) {
      _tradoorActiveProvider = found.provider;
      try { window.ethereum = found.provider; } catch (_) {}
      return found.provider;
    }
  }

  // Show picker
  const chosen = await _showWalletPicker(providers);
  if (!chosen) {
    // Fallback if picker returned null
    _tradoorActiveProvider = window.ethereum;
    return window.ethereum;
  }
  _tradoorActiveProvider = chosen.provider;
  try { window.ethereum = chosen.provider; } catch (_) {}
  return chosen.provider;
}

// Re-verify on-chain (force=1, bypassing both the 15s frontend cache and the
// 30s backend grace) that the venue's approvals are still signed. Called at
// connection so an external revoke is caught instead of showing a stale "valid".
async function reverifyVenueApprovalsForced() {
  try {
    if (isNadoMode()) {
      await refreshNadoAgentStatus({ force: true });
    } else {
      await refreshBuilderStatus({ force: true });
      await refreshAgentStatus({ force: true });
    }
    if (isExtendedMode()) {
      await refreshExtendedCredentialsStatus({ force: true });
    }
  } catch (_) {}
}

// reverify: only the genuine top-level "connect wallet" action should re-verify
// approvals on-chain. The approval sub-steps call this to ensure wallet auth and
// must NOT re-trigger it (that produced a burst of forced /api/*/status calls
// per step and rate-limited HL). Default false.
async function connectAndSignWallet({ reverify = false } = {}) {
  const accountInput = document.getElementById("account_address");
  const existingAccount = accountInput?.value?.trim() || "";
  if (existingAccount && isWalletAuthValidForAccount(existingAccount)) {
    setWalletAuthStatus("1/3 Wallet verified.", "ok");
    updateWalletAuthButtonState();
    if (reverify) await reverifyVenueApprovalsForced();
    return true;
  }

  // Get provider via EIP-6963 picker (or fallback to window.ethereum)
  const provider = await _getWalletProvider();
  if (!provider || typeof provider.request !== "function") {
    throw new Error("No Ethereum wallet found. Open this page in a wallet-enabled browser.");
  }

  const accounts = await provider.request({ method: "eth_requestAccounts" });
  const walletAddress = Array.isArray(accounts) && accounts.length ? String(accounts[0]) : "";
  if (!walletAddress) {
    throw new Error("Wallet connection failed.");
  }
  setConnectedAccount(walletAddress);
  await refreshWalletNetworkBadge();
  const account = walletAddress;

  const challenge = await apiPost("/api/auth/challenge", { account_address: account });
  if (!challenge || !challenge.message || !challenge.nonce) {
    throw new Error("Failed to create wallet signature challenge.");
  }
  const signature = await provider.request({
    method: "personal_sign",
    params: [challenge.message, walletAddress],
  });
  const verify = await apiPost("/api/auth/verify", {
    account_address: account,
    nonce: challenge.nonce,
    signature,
  });
  if (!verify || !verify.authenticated || !verify.auth_token) {
    throw new Error("Wallet signature was rejected.");
  }
  const norm = normalizeAccount(verify.account_address || account);
  const expMs = Date.parse(String(verify.expires_at || ""));
  const expFinal = Number.isFinite(expMs) ? expMs : Date.now() + 5 * 60 * 1000;
  _storeTokenForAddress(norm, String(verify.auth_token), expFinal);
  persistWalletAuthSession();
  setWalletAuthStatus("1/3 Wallet verified.", "ok");
  updateWalletAuthButtonState();
  await refreshWalletNetworkBadge();
  if (reverify) await reverifyVenueApprovalsForced();
  return true;
}

async function connectAndSignWalletForDex(dex) {
  const provider = await _getWalletProvider();
  if (!provider || typeof provider.request !== "function") {
    throw new Error("No Ethereum wallet found.");
  }
  const accounts = await provider.request({ method: "eth_requestAccounts" });
  const walletAddress = Array.isArray(accounts) && accounts.length ? String(accounts[0]) : "";
  if (!walletAddress) throw new Error("Wallet connection failed.");

  const norm = normalizeAccount(walletAddress);
  // Venue déjà connectée (ré-approbation, renouvellement) : jamais de bascule
  // silencieuse sur un autre compte, ses approbations sont celles de l'ancien.
  const already = _dexState[dex]?.connected ? normalizeAccount(_dexState[dex].address || "") : "";
  if (already && already !== norm) {
    const label = DEX_LABELS[dex] || dex;
    throw new Error(
      `${label} is connected with ${already.slice(0, 6)}...${already.slice(-4)} but your wallet is on ` +
      `${norm.slice(0, 6)}...${norm.slice(-4)}. Switch account in the wallet, or disconnect ${label} first.`,
    );
  }

  // Reuse existing valid token for this address (validate server-side first).
  // Use raw fetch instead of apiGet to avoid triggering the 401 toast/clearWalletAuthSession
  // side effects - we want to silently re-challenge if the cached token was rejected.
  if (walletAuthTokens[norm] && walletAuthExpires[norm] > Date.now()) {
    let cacheStillValid = true;
    try {
      const res = await fetchWithTimeout(
        `/api/auth/status?account_address=${encodeURIComponent(norm)}`,
        { headers: { "X-Mega-Auth": walletAuthTokens[norm] } },
        API_TIMEOUT_MS,
      );
      if (res.status === 401) {
        cacheStillValid = false;
      } else if (res.ok) {
        const status = await res.json();
        if (!status || !status.authenticated) cacheStillValid = false;
      }
      // Other non-OK statuses: keep cache (best-effort, transient server issue).
    } catch (_) {
      // Network error: tolerate, keep cache.
    }
    if (cacheStillValid) {
      _dexState[dex].address = norm;
      setConnectedAccount(walletAddress);
      _syncPrimaryAuthGlobals();
      return true;
    }
    delete walletAuthTokens[norm];
    delete walletAuthExpires[norm];
  }

  // Challenge/verify flow
  setConnectedAccount(walletAddress);
  const challenge = await apiPost("/api/auth/challenge", { account_address: norm });
  if (!challenge?.message || !challenge?.nonce) throw new Error("Failed to create wallet signature challenge.");
  const signature = await provider.request({
    method: "personal_sign",
    params: [challenge.message, walletAddress],
  });
  const verify = await apiPost("/api/auth/verify", {
    account_address: norm,
    nonce: challenge.nonce,
    signature,
  });
  if (!verify?.authenticated || !verify?.auth_token) throw new Error("Wallet signature was rejected.");

  const expMs = Date.parse(String(verify.expires_at || ""));
  const expFinal = Number.isFinite(expMs) ? expMs : Date.now() + 5 * 60 * 1000;
  _storeTokenForAddress(norm, String(verify.auth_token), expFinal);
  _dexState[dex].address = norm;
  persistWalletAuthSession();
  return true;
}

async function refreshBuilderStatus(opts = {}) {
  if (isQfexMode()) {
    // QFEX : le builder code est attaché par le serveur, aucune approbation de l'utilisateur.
    return { approved: true, message: "Not required on QFEX." };
  }
  if (isRisexMode()) {
    // RISEx : une seule source de vérité, /api/risex/status (approbation lue chez
    // RISEx). Le fee de /api/builder/status est en unités RISEx (100 = 1 bp) et
    // fausserait l'affichage générique en bps.
    try {
      const st = await refreshRisexStatus({ force: !!opts.force });
      const approved = !!(st && st.builder && st.builder.approved);
      builderApprovedForAccount = approved;
      const message = approved ? "Builder fee approved on RISEx." : "Builder fee not approved on RISEx yet.";
      setBuilderStatus(approved ? `2/3 ${message}` : `2/3 Pending: ${message}`, approved ? "ok" : "warn");
      updateBuilderApproveButtonState(approved);
      return { approved, message };
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      builderApprovedForAccount = false;
      setBuilderStatus(`2/3 Check failed: ${msg}`, "error");
      updateBuilderApproveButtonState(false);
      return { approved: false, message: msg };
    }
  }
  if (isNadoMode()) {
    builderApprovedForAccount = true;
    builderStatusCache = { key: "", ts: 0, value: null };
    setBuilderStatus("2/3 N/A on Nado mode.", "ok");
    updateBuilderApproveButtonState(true);
    return { approved: true, message: "Not required on Nado mode." };
  }
  const force = !!opts.force;
  const account = document.getElementById("account_address")?.value?.trim() || "";
  const exchangeMode = document.getElementById("mode")?.value || "hyperliquid_mainnet";
  // Builder approval is account+exchange scoped; keep it independent from market source.
  const modeForStatus = exchangeMode;
  const settings = currentBuilderSettings(exchangeMode);
  const cacheKey = `${normalizeAccount(account)}|${modeForStatus}`;
  if (!account || !account.startsWith("0x")) {
    builderApprovedForAccount = false;
    builderStatusCache = { key: "", ts: 0, value: null };
    setBuilderStatus("2/3 Approve builder fee.", "warn");
    updateBuilderApproveButtonState(false);
    return { approved: false, message: "Account address missing." };
  }
  _activateAuthForAccount(account);
  if (
    !force &&
    builderStatusCache.key === cacheKey &&
    builderStatusCache.value &&
    Date.now() - builderStatusCache.ts < APPROVAL_STATUS_CACHE_TTL_MS
  ) {
    const cached = builderStatusCache.value;
    builderApprovedForAccount = !!cached.approved;
    if (cached.approved) {
      if (isExtendedMode(exchangeMode)) {
        setBuilderStatus(
          `2/3 Builder fee approved (${settings.feeBps.toFixed(1)} bps / ${settings.feePerMillionUsd.toFixed(0)} USD per 1M).`,
          "ok",
        );
      } else {
        setBuilderStatus(`2/3 Builder fee approved (${settings.feeBps.toFixed(1)} bps).`, "ok");
      }
      updateBuilderApproveButtonState(true);
    } else {
      setBuilderStatus(`2/3 Pending: ${cached.message || "Builder fee missing."}`, "warn");
      updateBuilderApproveButtonState(false);
    }
    return cached;
  }
  try {
    const data = await apiGet(
      `/api/builder/status?account_address=${encodeURIComponent(account)}&mode=${encodeURIComponent(modeForStatus)}${force ? "&force=1" : ""}`,
    );
    if (data && data.approved) {
      builderApprovedForAccount = true;
      const feeTenths = Number.isFinite(Number(data.builder_fee_tenths_bps)) ? Number(data.builder_fee_tenths_bps) : settings.feeTenthsBps;
      if (isExtendedMode(exchangeMode)) {
        approvedExtBuilderFeeTenthsBps = feeTenths;
      } else {
        approvedBuilderFeeTenthsBps = feeTenths;
      }
      const bps = Number.isFinite(Number(data.builder_fee_bps)) ? Number(data.builder_fee_bps) : settings.feeBps;
      if (isExtendedMode(exchangeMode)) {
        const perM = Number.isFinite(Number(data.builder_fee_per_million_usd))
          ? Number(data.builder_fee_per_million_usd)
          : settings.feePerMillionUsd;
        setBuilderStatus(
          `2/3 Builder fee approved (${bps.toFixed(1)} bps / ${perM.toFixed(0)} USD per 1M).`,
          "ok",
        );
      } else {
        setBuilderStatus(`2/3 Builder fee approved (${bps.toFixed(1)} bps).`, "ok");
      }
      updateBuilderApproveButtonState(true);
    } else {
      builderApprovedForAccount = false;
      const msg = data && data.message
        ? data.message
        : `Builder fee approval missing for ${settings.address}.`;
      setBuilderStatus(`2/3 Pending: ${msg}`, "warn");
      updateBuilderApproveButtonState(false);
    }
    const result = {
      approved: !!(data && data.approved),
      message: (data && data.message) || "",
    };
    builderStatusCache = { key: cacheKey, ts: Date.now(), value: result };
    return result;
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    builderApprovedForAccount = false;
    builderStatusCache = { key: cacheKey, ts: Date.now(), value: { approved: false, message: msg } };
    setBuilderStatus(`2/3 Check failed: ${msg}`, "error");
    updateBuilderApproveButtonState(false);
    return { approved: false, message: msg };
  }
}

// ── Referral system ─────────────────────────────────────────────────────────

let referralModalOpen = false;

/* ─── Telegram verification modal ─────────────────────────── */
let telegramModalOpen = false;
let _telegramVerifyPollTimer = null;
const _TG_DISMISS_KEY = "tg_popup_dismissed";

/** Auto-show Telegram modal after connect if not linked and not dismissed. */
async function _maybeTelegramAutoPopup() {
  if (localStorage.getItem(_TG_DISMISS_KEY) === "1") return;
  try {
    const resp = await fetchWithTimeout("/api/telegram/status", { headers: authHeaders() });
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.linked) return;
    setTelegramModalOpen(true);
  } catch (_) { /* silent */ }
}

function setTelegramModalOpen(open) {
  telegramModalOpen = !!open;
  const modal = document.getElementById("telegram_modal");
  const overlay = document.getElementById("telegram_overlay");
  if (!modal) return;
  modal.classList.toggle("hidden", !telegramModalOpen);
  modal.setAttribute("aria-hidden", telegramModalOpen ? "false" : "true");
  if (overlay) {
    overlay.classList.toggle("hidden", !telegramModalOpen);
    overlay.setAttribute("aria-hidden", telegramModalOpen ? "false" : "true");
  }
  if (telegramModalOpen) {
    const dc = document.getElementById("telegram_dismiss_check");
    if (dc) dc.checked = localStorage.getItem(_TG_DISMISS_KEY) === "1";
    _telegramRefreshState();
  } else {
    // Stop polling when modal closes
    if (_telegramVerifyPollTimer) { clearInterval(_telegramVerifyPollTimer); _telegramVerifyPollTimer = null; }
  }
}

async function _telegramRefreshState() {
  const unlinkedEl = document.getElementById("telegram_unlinked");
  const linkedEl = document.getElementById("telegram_linked");
  if (!unlinkedEl || !linkedEl) return;
  try {
    const resp = await fetchWithTimeout("/api/telegram/status", { headers: authHeaders() });
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.linked) {
      unlinkedEl.classList.add("hidden");
      linkedEl.classList.remove("hidden");
      const enabledInput = document.getElementById("telegram_enabled");
      if (enabledInput) enabledInput.checked = data.enabled;
      // Stop verify polling if running
      if (_telegramVerifyPollTimer) { clearInterval(_telegramVerifyPollTimer); _telegramVerifyPollTimer = null; }
    } else {
      unlinkedEl.classList.remove("hidden");
      linkedEl.classList.add("hidden");
      _telegramRequestCode();
    }
  } catch (_) { /* ignore */ }
}

// Signature fraîche du compte de la session active (liaison Telegram : le serveur exige
// une signature de moins de 15 min et répond 428 sinon).
async function _freshWalletSignature() {
  const norm = normalizeAccount(walletAuthAccount || "");
  const provider = await _getWalletProvider();
  if (!norm || !provider || typeof provider.request !== "function") {
    throw new Error("Connect your wallet first.");
  }
  const accounts = await provider.request({ method: "eth_requestAccounts" });
  const walletAddress = Array.isArray(accounts) && accounts.length ? String(accounts[0]) : "";
  if (normalizeAccount(walletAddress) !== norm) {
    throw new Error("Switch your wallet to the connected account and try again.");
  }
  const challenge = await apiPost("/api/auth/challenge", { account_address: norm });
  if (!challenge?.message || !challenge?.nonce) throw new Error("Failed to create wallet signature challenge.");
  const signature = await provider.request({ method: "personal_sign", params: [challenge.message, walletAddress] });
  const verify = await apiPost("/api/auth/verify", { account_address: norm, nonce: challenge.nonce, signature });
  if (!verify?.authenticated || !verify?.auth_token) throw new Error("Wallet signature was rejected.");
  const expMs = Date.parse(String(verify.expires_at || ""));
  _storeTokenForAddress(norm, String(verify.auth_token), Number.isFinite(expMs) ? expMs : Date.now() + 5 * 60 * 1000);
  persistWalletAuthSession();
}

async function _telegramRequestCode(_resigned = false) {
  const codeEl = document.getElementById("telegram_code");
  const verifyBtn = document.getElementById("telegram_verify_btn");
  const statusEl = document.getElementById("telegram_verify_status");
  if (statusEl) { statusEl.textContent = ""; statusEl.className = "telegram-verify-status"; }
  // Disable button until we have a valid deep link
  if (verifyBtn) { verifyBtn.removeAttribute("href"); verifyBtn.classList.add("telegram-verify-btn--disabled"); }
  try {
    const resp = await fetchWithTimeout("/api/telegram/verify", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: "{}",
    });
    if (resp.status === 428 && !_resigned) {
      // Session trop ancienne pour lier Telegram : une signature, puis un seul nouvel essai.
      if (statusEl) { statusEl.textContent = "Please confirm the signature in your wallet."; statusEl.className = "telegram-verify-status"; }
      try {
        await _freshWalletSignature();
      } catch (e) {
        if (statusEl) { statusEl.textContent = e?.message || "Signature cancelled."; statusEl.className = "telegram-verify-status telegram-verify-status--error"; }
        if (codeEl) codeEl.textContent = "------";
        return;
      }
      return _telegramRequestCode(true);
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (statusEl) { statusEl.textContent = err.detail || "Failed to generate code."; statusEl.className = "telegram-verify-status telegram-verify-status--error"; }
      if (codeEl) codeEl.textContent = "------";
      return;
    }
    const data = await resp.json();
    if (codeEl) codeEl.textContent = data.code || "------";
    if (verifyBtn && data.deep_link) {
      // Whitelist protocol - never trust an API-provided URL blindly.
      let safeHref = "";
      try {
        const u = new URL(data.deep_link, window.location.origin);
        if (u.protocol === "https:" || u.protocol === "tg:") {
          safeHref = u.href;
        }
      } catch (_) {
        safeHref = "";
      }
      if (safeHref) {
        verifyBtn.href = safeHref;
        verifyBtn.classList.remove("telegram-verify-btn--disabled");
      }
    }
    // Start polling for verification completion
    if (_telegramVerifyPollTimer) clearInterval(_telegramVerifyPollTimer);
    _telegramVerifyPollTimer = setInterval(_telegramCheckLinked, 3000);
  } catch (e) {
    if (statusEl) { statusEl.textContent = "Connection error."; statusEl.className = "telegram-verify-status telegram-verify-status--error"; }
  }
}

async function _telegramCheckLinked() {
  try {
    const resp = await fetchWithTimeout("/api/telegram/status", { headers: authHeaders() });
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.linked) {
      if (_telegramVerifyPollTimer) { clearInterval(_telegramVerifyPollTimer); _telegramVerifyPollTimer = null; }
      _telegramRefreshState();
    }
  } catch (_) { /* ignore */ }
}

async function _telegramToggle() {
  const enabledInput = document.getElementById("telegram_enabled");
  if (!enabledInput) return;
  try {
    await fetchWithTimeout("/api/telegram/toggle", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ enabled: enabledInput.checked }),
    });
  } catch (_) {
    // Revert on failure
    enabledInput.checked = !enabledInput.checked;
  }
}

async function _telegramUnlink() {
  try {
    const resp = await fetchWithTimeout("/api/telegram/unlink", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: "{}",
    });
    if (resp.ok) _telegramRefreshState();
  } catch (_) { /* ignore */ }
}

/* ─── Feedback modal ──────────────────────────────────────── */
let feedbackModalOpen = false;

function setFeedbackModalOpen(open) {
  feedbackModalOpen = !!open;
  const modal = document.getElementById("feedback_modal");
  const overlay = document.getElementById("feedback_overlay");
  if (!modal) return;
  modal.classList.toggle("hidden", !feedbackModalOpen);
  modal.setAttribute("aria-hidden", feedbackModalOpen ? "false" : "true");
  if (overlay) {
    overlay.classList.toggle("hidden", !feedbackModalOpen);
    overlay.setAttribute("aria-hidden", feedbackModalOpen ? "false" : "true");
  }
  if (feedbackModalOpen) {
    const statusEl = document.getElementById("feedback_status");
    if (statusEl) statusEl.textContent = "";
    _feedbackLoadHistory();
  }
}

async function _feedbackLoadHistory() {
  const container = document.getElementById("feedback_history");
  if (!container || !walletAuthToken) return;
  try {
    const data = await apiGet("/api/feedback/history");
    const messages = data.messages || [];
    if (!messages.length) {
      container.innerHTML = "";
      return;
    }
    const bubbles = [];
    for (const msg of messages) {
      const userTs = msg.ts ? new Date(msg.ts).toLocaleString() : "";
      bubbles.push(`<div class="feedback-history-bubble feedback-history-bubble--user"><div class="feedback-history-ts">You \u2014 ${_escHtml(userTs)}</div>${_escHtml(msg.message)}</div>`);
      for (const r of (msg.replies || [])) {
        const rTs = r.ts ? new Date(r.ts).toLocaleString() : "";
        bubbles.push(`<div class="feedback-history-bubble feedback-history-bubble--admin"><div class="feedback-history-ts">Trador \u2014 ${_escHtml(rTs)}</div>${_escHtml(r.message)}</div>`);
      }
    }
    container.innerHTML = bubbles.join("");
    container.scrollTop = container.scrollHeight;
  } catch (_) {
    container.innerHTML = "";
  }
}

function _escHtml(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

/* ─── Trading Score modal ─────────────────────────────────── */
let tradingScoreModalOpen = false;
let _tsRefreshTimer = null;
let _tsBadgeTimer = null;
let _tsLastData = null;

function setTradingScoreModalOpen(open) {
  tradingScoreModalOpen = !!open;
  const modal = document.getElementById("trading_score_modal");
  const overlay = document.getElementById("trading_score_overlay");
  if (!modal) return;
  modal.classList.toggle("hidden", !tradingScoreModalOpen);
  modal.setAttribute("aria-hidden", tradingScoreModalOpen ? "false" : "true");
  if (overlay) {
    overlay.classList.toggle("hidden", !tradingScoreModalOpen);
    overlay.setAttribute("aria-hidden", tradingScoreModalOpen ? "false" : "true");
  }
  if (tradingScoreModalOpen) {
    // Reset to 0 so animation replays from scratch
    const gv = document.getElementById("ts_gauge_value");
    const gf = document.getElementById("ts_gauge_fill");
    if (gv) gv.textContent = "0";
    if (gf) { gf.style.transition = "none"; gf.style.strokeDashoffset = Math.PI * 80; requestAnimationFrame(() => { gf.style.transition = ""; }); }
    refreshTradingScore();
    _tsRefreshTimer = setInterval(refreshTradingScore, 60000);
  } else {
    if (_tsRefreshTimer) { clearInterval(_tsRefreshTimer); _tsRefreshTimer = null; }
  }
}

function startTsBadgePolling() {
  if (_tsBadgeTimer) return;
  refreshTradingScoreBadge();
  _tsBadgeTimer = setInterval(refreshTradingScoreBadge, 60000);
}
function stopTsBadgePolling() {
  if (_tsBadgeTimer) { clearInterval(_tsBadgeTimer); _tsBadgeTimer = null; }
  const badge = document.getElementById("ts_badge");
  if (badge) { badge.textContent = "--"; badge.className = "ts-badge"; }
  const arcFill = document.getElementById("ts_badge_arc_fill");
  if (arcFill) arcFill.style.strokeDashoffset = "40.8";
}
async function refreshTradingScoreBadge() {
  try {
    const data = await apiGet("/api/trading_score");
    _tsLastData = data;
    _updateTsBadge(data.score);
  } catch (_) {}
}

async function refreshTradingScore() {
  const loadingEl = document.getElementById("ts_loading");
  const errorEl = document.getElementById("ts_error");
  if (loadingEl) loadingEl.classList.remove("hidden");
  if (errorEl) { errorEl.textContent = ""; errorEl.classList.add("hidden"); }

  try {
    const data = await apiGet("/api/trading_score");
    _tsLastData = data;
    if (loadingEl) loadingEl.classList.add("hidden");
    renderTradingScore(data);
  } catch (e) {
    if (loadingEl) loadingEl.classList.add("hidden");
    if (errorEl) { errorEl.textContent = "Failed to load trading score"; errorEl.classList.remove("hidden"); }
  }
}

function _updateTsBadge(score) {
  const badge = document.getElementById("ts_badge");
  if (!badge) return;
  badge.textContent = score;
  badge.className = "ts-badge " + (score >= 70 ? "ts-badge--high" : score >= 45 ? "ts-badge--mid" : "ts-badge--low");
  // Mini arc fill
  const arcFill = document.getElementById("ts_badge_arc_fill");
  if (arcFill) {
    const arcLen = 40.8;
    arcFill.style.strokeDasharray = arcLen;
    arcFill.style.strokeDashoffset = arcLen * (1 - score / 100);
  }
}

function _animateScoreValue(el, target) {
  const start = parseInt(el.textContent) || 0;
  const diff = target - start;
  if (diff === 0) { el.textContent = target; return; }
  const duration = 800;
  const t0 = performance.now();
  function step(now) {
    const p = Math.min((now - t0) / duration, 1);
    const ease = 1 - Math.pow(1 - p, 3); // ease-out cubic
    el.textContent = Math.round(start + diff * ease);
    if (p < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

function renderTradingScore(data) {
  // ── Badge ──
  _updateTsBadge(data.score);

  // ── Gauge ──
  const gaugeVal = document.getElementById("ts_gauge_value");
  const gaugeLabel = document.getElementById("ts_gauge_label");
  const gaugeFill = document.getElementById("ts_gauge_fill");
  if (gaugeVal) _animateScoreValue(gaugeVal, data.score);
  if (gaugeLabel) {
    gaugeLabel.textContent = data.label;
    gaugeLabel.className = "ts-gauge-label ts-gauge-label--" + data.label.toLowerCase();
  }
  if (gaugeFill) {
    const arcLen = Math.PI * 80;
    gaugeFill.style.strokeDasharray = arcLen;
    gaugeFill.style.strokeDashoffset = arcLen * (1 - data.score / 100);
  }

  // ── Factors ──
  const factorsEl = document.getElementById("ts_factors");
  if (factorsEl) {
    const order = ["hour", "volatility", "market_hours", "profile", "volume", "funding", "trend", "day"];
    const names = {
      hour: "Hour Performance",
      volatility: "Volatility",
      market_hours: "Market Hours",
      profile: "Best Profile",
      volume: "24h Volume",
      funding: "Funding Rate",
      trend: "Recent Trend",
      day: "Day of Week",
    };
    let html = "";
    for (const key of order) {
      const f = data.factors[key];
      if (!f) continue;
      const cls = f.score >= 70 ? "high" : f.score >= 45 ? "mid" : "low";
      html += `<div class="ts-factor-row">
        <div class="ts-factor-name">${names[key]}</div>
        <div class="ts-factor-bar-bg"><div class="ts-factor-bar-fill ts-factor-bar-fill--${cls}" style="width:${f.score}%"></div></div>
        <div class="ts-factor-score">${f.score}</div>
      </div>`;
    }
    factorsEl.innerHTML = html;
  }

  // ── Recommendations ──
  const recEl = document.getElementById("ts_recommendations");
  if (recEl && data.optimal_config) {
    const oc = data.optimal_config;
    const profileDisplay = oc.risk_mode === "conservative" ? "Conservative" : oc.risk_mode === "aggressive" ? "Aggressive" : "Classic";
    const biasDisplay = oc.direction_bias_label === "long" ? "Long" : oc.direction_bias_label === "short" ? "Short" : "Neutral";
    const schedDisplay = oc.recommended_schedule ? `${oc.recommended_schedule.start} – ${oc.recommended_schedule.stop}` : "-";
    recEl.innerHTML = `
      <div class="ts-rec-card">
        <div class="ts-rec-icon">&#x1F6E1;</div>
        <div class="ts-rec-label">Profile</div>
        <div class="ts-rec-value">${profileDisplay}</div>
      </div>
      <div class="ts-rec-card">
        <div class="ts-rec-icon">&#x2194;</div>
        <div class="ts-rec-label">Direction</div>
        <div class="ts-rec-value">${biasDisplay}</div>
      </div>
      <div class="ts-rec-card">
        <div class="ts-rec-icon">&#x1F552;</div>
        <div class="ts-rec-label">Schedule</div>
        <div class="ts-rec-value">${schedDisplay}</div>
      </div>`;
  }

  // ── Apply button ──
  const applyBtn = document.getElementById("ts_apply_btn");
  if (applyBtn) applyBtn.disabled = false;
}

function applyOptimalConfig() {
  if (!_tsLastData || !_tsLastData.optimal_config) return;
  const oc = _tsLastData.optimal_config;

  // Switch to Market Making mode if not already
  if (_activeBotType !== "mm") navigate(ROUTE_PATHS.mm);

  // Apply risk mode (UI only : does not trigger config save)
  if (oc.risk_mode) {
    applyRiskMode(oc.risk_mode, { force: true, silent: true });
  }

  // Apply direction bias (UI only)
  if (oc.direction_bias_label) {
    applyDirectionBias(oc.direction_bias_label, { silent: true });
  }
  // Apply quote mode (UI only)
  if (oc.quote_mode) {
    applyQuoteMode(oc.quote_mode, { silent: true });
  }

  // Switch to schedule panel + prefill recommended times — NEVER auto-enable:
  // arming a daily auto-start requires an explicit click on the schedule save
  // button. (Silently enabling here used to launch unwanted sessions every day.)
  if (oc.recommended_schedule) {
    _setStopMode(true, { skipServer: true });
    const startEl = document.getElementById("sched_start_time");
    const stopEl = document.getElementById("sched_stop_time");
    if (startEl) startEl.value = oc.recommended_schedule.start;
    if (stopEl) stopEl.value = oc.recommended_schedule.stop;
    const statusEl = document.getElementById("sched_status_text");
    if (statusEl) {
      _schedHintUntil = Date.now() + 30000;
      statusEl.textContent = "Suggested times filled in — press Save to enable the schedule.";
      statusEl.className = "sched-status-text";
    }
  }

  // Only sync config if we're on the correct DEX (avoid overwriting nado/extended config)
  // Wait a tick for navigate() to update _activeDex and #mode select
  setTimeout(() => {
    const modeSelect = document.getElementById("mode");
    if (modeSelect && modeSelect.value) {
      scheduleLiveConfigSync(250);
    }
  }, 100);

  setTradingScoreModalOpen(false);
}

/* ─── My Stats modal ──────────────────────────────────────── */
let myStatsModalOpen = false;

function setMyStatsModalOpen(open) {
  myStatsModalOpen = !!open;
  const modal = document.getElementById("my_stats_modal");
  const overlay = document.getElementById("my_stats_overlay");
  if (!modal) return;
  modal.classList.toggle("hidden", !myStatsModalOpen);
  modal.setAttribute("aria-hidden", myStatsModalOpen ? "false" : "true");
  if (overlay) {
    overlay.classList.toggle("hidden", !myStatsModalOpen);
    overlay.setAttribute("aria-hidden", myStatsModalOpen ? "false" : "true");
  }
  if (myStatsModalOpen) refreshMyStats();
}

function updateNftHolderBadge() {
  const badge = document.getElementById("nft_holder_badge");
  if (badge) badge.classList.toggle("hidden", !nftHolderStatus);
}

async function refreshMyStats() {
  const loadingEl = document.getElementById("myst_loading");
  const errorEl   = document.getElementById("myst_error");
  const gridEl    = document.getElementById("my_stats_modal")?.querySelector(".myst-grid");
  if (loadingEl) loadingEl.classList.remove("hidden");
  if (errorEl)   { errorEl.textContent = ""; errorEl.classList.add("hidden"); }
  if (gridEl)    gridEl.style.opacity = "0.4";
  try {
    const account = document.getElementById("account_address")?.value?.trim() || "";
    const refUrl = account
      ? `/api/referral/status?account_address=${encodeURIComponent(account)}`
      : "/api/referral/status";
    const [histData, refData] = await Promise.all([
      apiGet("/api/pnl_history"),
      apiGet(refUrl).catch(() => ({})),
    ]);
    const sessions = [...(histData.sessions || [])];
    if (histData.current_session?.active) sessions.unshift(histData.current_session);

    const totalVolume = sessions.reduce((s, r) => s + (parseFloat(r.session_volume_usd) || 0), 0);
    const totalFees   = sessions.reduce((s, r) => s + (parseFloat(r.platform_fees_usd ?? r.project_fees_usd) || 0), 0);
    // Total PnL: use pnl_total_usd_net (after trading fees), fallback for older entries.
    const totalPnl = sessions.reduce((sum, r) => {
      const net = parseFloat(r.pnl_total_usd_net ?? r.display_pnl_usd ?? r.pnl_total_usd);
      return sum + (Number.isFinite(net) ? net : 0);
    }, 0);
    // Decomposition brut/frais du PnL net. Les frais sont derives de
    // (gross - net) et JAMAIS lus dans platform_fees_usd : quand le PnL vient
    // d'une source deja nette (onchain_fills, websocket), le builderFee Tradoor
    // est deja compris dans les frais on-chain deduits, donc l'additionner le
    // compterait une seconde fois.
    let tradingFees = 0;
    let feesKnown = 0;
    for (const r of sessions) {
      const net = parseFloat(r.pnl_total_usd_net ?? r.display_pnl_usd ?? r.pnl_total_usd);
      const gross = parseFloat(r.pnl_total_usd_gross);
      if (!Number.isFinite(net) || !Number.isFinite(gross)) continue;
      const fees = gross - net;
      // Ecarte deux cas ou l'ecart brut/net ne dit rien des frais reels : un
      // vieux record sans brut enregistre (gross = 0, donc frais negatifs) et
      // une session dont la source de PnL est brute (net == gross alors que des
      // frais ont bien ete payes). Mieux vaut les exclure du total et le dire
      // que d'afficher un chiffre faux.
      if (!(fees > 0)) continue;
      tradingFees += fees;
      feesKnown += 1;
    }
    // Brut reconstitue depuis le net affiche : garantit que la ligne lue
    // (gross - fees = net) est toujours coherente avec la valeur au-dessus.
    const grossPnl = totalPnl + tradingFees;
    const netPer100k = totalVolume > 0 ? (totalPnl / totalVolume) * 100000 : null;
    const discountRate = parseFloat(refData.discount_rate) || 0;
    const nftDiscount = parseFloat(refData.nft_discount) || 0;
    const effectiveDiscount = Math.max(discountRate, nftDiscount);
    nftHolderStatus = !!refData.nft_holder;
    updateNftHolderBadge();
    // Use real accumulated rebate from backend if available, else estimate
    const realRebate = parseFloat(refData.rebate_earned_usd) || 0;
    const feesSaved = realRebate > 0
      ? realRebate
      : (effectiveDiscount > 0 && effectiveDiscount < 1)
        ? totalFees * effectiveDiscount / (1 - effectiveDiscount)
        : 0;

    const volEl = document.getElementById("myst_volume");
    const sessEl = document.getElementById("myst_sessions");
    const pnlEl = document.getElementById("myst_pnl");
    const savedEl = document.getElementById("myst_fees_saved");
    const discEl = document.getElementById("myst_discount_label");
    const commEl = document.getElementById("myst_commissions");
    const refsEl = document.getElementById("myst_refs_count");

    if (volEl)  volEl.textContent  = formatUsdPlain(totalVolume);
    if (sessEl) sessEl.textContent = `${sessions.length} session${sessions.length !== 1 ? "s" : ""}`;
    if (pnlEl) {
      pnlEl.textContent = formatUsd(totalPnl);
      pnlEl.className = "myst-value " + (totalPnl >= 0 ? "myst-value--green" : "myst-value--red");
    }
    const pnlSubEl = document.getElementById("myst_pnl_sub");
    if (pnlSubEl) {
      pnlSubEl.textContent = tradingFees > 0
        ? `${formatUsdPlain(grossPnl)} gross − ${formatUsdPlain(tradingFees)} fees`
        : " ";
    }
    const perEl = document.getElementById("myst_net_per_100k");
    const covEl = document.getElementById("myst_net_coverage");
    if (perEl) {
      perEl.textContent = netPer100k == null ? "-" : formatUsd(netPer100k);
      perEl.className = "myst-value " + (netPer100k == null
        ? ""
        : (netPer100k >= 0 ? "myst-value--green" : "myst-value--red"));
    }
    if (covEl) {
      covEl.textContent = sessions.length
        ? `net of fees · fees known on ${feesKnown}/${sessions.length} session${sessions.length !== 1 ? "s" : ""}`
        : " ";
    }
    if (savedEl) savedEl.textContent = feesSaved > 0 ? formatUsdPlain(feesSaved) : "$0.00";
    if (discEl)  discEl.textContent  = effectiveDiscount > 0
      ? `-${Math.round(effectiveDiscount * 100)}% fee discount`
      : "No discount";
    if (commEl) commEl.textContent = formatUsdPlain(parseFloat(refData.commission_earned_usd) || 0);
    const refsCount = parseInt(refData.referrals_count) || 0;
    if (refsEl) refsEl.textContent = `${refsCount} referral${refsCount !== 1 ? "s" : ""}`;

    if (gridEl) gridEl.style.opacity = "1";
  } catch (e) {
    if (errorEl) { errorEl.textContent = "Failed to load stats."; errorEl.classList.remove("hidden"); }
  } finally {
    if (loadingEl) loadingEl.classList.add("hidden");
  }
}

function setReferralModalOpen(open) {
  referralModalOpen = !!open;
  const modal = document.getElementById("referral_modal");
  const overlay = document.getElementById("referral_modal_overlay");
  if (!modal) return;
  modal.classList.toggle("hidden", !referralModalOpen);
  modal.setAttribute("aria-hidden", referralModalOpen ? "false" : "true");
  if (overlay) {
    overlay.classList.toggle("hidden", !referralModalOpen);
    overlay.setAttribute("aria-hidden", referralModalOpen ? "false" : "true");
  }
  if (referralModalOpen) refreshReferralStatus();
}

async function refreshReferralStatus() {
  const account = document.getElementById("account_address")?.value?.trim() || "";
  const notConnectedMsg = document.getElementById("referral_not_connected_msg");
  const lockedBlock = document.getElementById("referral_locked_block");
  const contentBlock = document.getElementById("referral_content");
  const connected = account && account.startsWith("0x") && isWalletAuthValidForAccount(account);

  const applySection = document.getElementById("referral_apply_section");
  if (notConnectedMsg) notConnectedMsg.classList.toggle("hidden", connected);
  const commBlock = document.getElementById("referral_commission_block");
  const tierBlock = document.getElementById("referral_tier_block");
  if (!connected) {
    if (lockedBlock) lockedBlock.classList.add("hidden");
    if (contentBlock) contentBlock.classList.add("hidden");
    if (applySection) applySection.classList.add("hidden");
    if (commBlock) commBlock.classList.add("hidden");
    if (tierBlock) tierBlock.classList.add("hidden");
    return;
  }
  try {
    const data = await apiGet(
      `/api/referral/status?account_address=${encodeURIComponent(account)}`,
    );
    const eligible = !!data.eligible;
    nftHolderStatus = !!data.nft_holder;
    updateNftHolderBadge();
    if (lockedBlock) lockedBlock.classList.toggle("hidden", eligible);
    if (contentBlock) contentBlock.classList.toggle("hidden", !eligible);

    if (!eligible) {
      const progressEl = document.getElementById("referral_locked_progress");
      if (progressEl) {
        const vol = Number(data.total_volume_usd || 0);
        const min = Number(data.min_volume_usd || 100000);
        const pct = Math.min(100, (vol / min) * 100).toFixed(1);
        progressEl.textContent = `$${vol.toLocaleString("en-US", { maximumFractionDigits: 0 })} / $${min.toLocaleString("en-US")} traded (${pct}%)`;
      }
    } else {
      const code = String(data.code || "");
      const link = `${window.location.origin}/?ref=${code}`;
      const linkEl = document.getElementById("referral_link_display");
      if (linkEl) linkEl.textContent = link;
      const statsEl = document.getElementById("referral_stats");
      if (statsEl) {
        const totalRefs = Number(data.referrals_count || 0);
        const qualifiedRefs = Number(data.qualified_referrals_count || 0);
        const parts = [];
        if (totalRefs > 0) parts.push(`${totalRefs} referee${totalRefs > 1 ? "s" : ""} (${qualifiedRefs} qualified)`);
        if (data.referred_by) parts.push(`Referred by ${String(data.referred_by).slice(0, 10)}…`);
        statsEl.textContent = parts.join(" · ");
      }
    }

    // Tier block : always visible when connected
    {
      const tierBlock = document.getElementById("referral_tier_block");
      const tier = Number(data.tier || 0);
      const discountRate = Number(data.discount_rate || 0);
      const nftDisc = Number(data.nft_discount || 0);
      const effectiveDisc = Math.max(discountRate, nftDisc);
      const qualifiedRefs = Number(data.qualified_referrals_count || 0);
      const totalRefs = Number(data.referrals_count || 0);
      const nextTier = data.next_tier != null ? Number(data.next_tier) : null;
      const tierNames = { 0: "No tier", 1: "Tier 1", 2: "Tier 2", 3: "Tier 3" };
      if (tierBlock) {
        tierBlock.classList.remove("hidden");
        const tierBadge = document.getElementById("referral_tier_badge");
        if (tierBadge) {
          tierBadge.className = `referral-tier-badge tier-${tier}`;
          tierBadge.textContent = tier > 0 ? tierNames[tier] : "No tier";
        }
        const badge = document.getElementById("referral_discount_badge");
        if (badge) {
          const dr = Math.round(effectiveDisc * 100);
          badge.classList.toggle("hidden", dr === 0);
          badge.textContent = `−${dr}% fee discount active`;
        }
        // Progress to next tier
        const progressWrap = document.getElementById("referral_tier_progress_wrap");
        if (progressWrap) {
          const hasNext = nextTier !== null && tier < 3;
          progressWrap.classList.toggle("hidden", !hasNext);
          if (hasNext) {
            const progressText = document.getElementById("referral_tier_progress_text");
            const nextLabel = document.getElementById("referral_tier_next_label");
            const bar = document.getElementById("referral_tier_progress_bar");
            if (nextTier === 1) {
              const pct = Math.min(100, totalRefs * 100);
              if (progressText) progressText.textContent = `${totalRefs}/1 referral`;
              if (nextLabel) nextLabel.textContent = `Tier 1 → −10%`;
              if (bar) bar.style.width = `${pct}%`;
            } else if (nextTier === 2) {
              const pct = Math.min(100, (qualifiedRefs / 5) * 100);
              if (progressText) progressText.textContent = `${qualifiedRefs}/5 qualified referrals (100k+)`;
              if (nextLabel) nextLabel.textContent = `Tier 2 → −20%`;
              if (bar) bar.style.width = `${pct}%`;
            } else {
              const pct = Math.min(100, (qualifiedRefs / 10) * 100);
              if (progressText) progressText.textContent = `${qualifiedRefs}/10 qualified referrals (100k+)`;
              if (nextLabel) nextLabel.textContent = `Tier 3 → −25%`;
              if (bar) bar.style.width = `${pct}%`;
            }
          }
        }
      }
    }

    // Commission block : always visible when connected
    const commBlock = document.getElementById("referral_commission_block");
    const earned = Number(data.commission_earned_usd || 0);
    const claimed = Number(data.commission_claimed_usd || 0);
    const claimable = Number(data.commission_claimable_usd || 0);
    const pending = Number(data.commission_pending_usd || 0);
    const minClaim = Number(data.commission_min_claim_usd || 10);
    const canClaim = !!data.commission_can_claim;
    if (commBlock) {
      commBlock.classList.remove("hidden");
      const fmt = (v) => `$${Number(v).toFixed(2)}`;
      const el = (id) => document.getElementById(id);
      if (el("referral_comm_earned")) el("referral_comm_earned").textContent = fmt(earned);
      if (el("referral_comm_claimed")) el("referral_comm_claimed").textContent = fmt(claimed);
      if (el("referral_comm_pending")) el("referral_comm_pending").textContent = fmt(pending);
      if (el("referral_comm_claimable")) el("referral_comm_claimable").textContent = fmt(claimable);
      // Claim button: commissions only
      const claimBtn = el("referral_claim_btn");
      if (claimBtn) {
        claimBtn.disabled = !canClaim;
        claimBtn.textContent = canClaim
          ? `Claim ${fmt(claimable)}`
          : `Claim (min ${fmt(minClaim)})`;
      }
    }

    // Apply section: always visible when connected
    if (applySection) applySection.classList.remove("hidden");
    const applyRow = document.getElementById("referral_apply_row");
    const appliedRow = document.getElementById("referral_applied_row");
    const appliedCodeEl = document.getElementById("referral_applied_code");
    if (data.referred_by) {
      // Already applied : show read-only code, hide form
      if (applyRow) applyRow.classList.add("hidden");
      if (appliedRow) appliedRow.classList.remove("hidden");
      if (appliedCodeEl) appliedCodeEl.textContent = data.referred_by_code || data.referred_by.slice(0, 10) + "…";
    } else {
      // Not yet applied : show form, hide read-only
      if (applyRow) applyRow.classList.remove("hidden");
      if (appliedRow) appliedRow.classList.add("hidden");
      // Auto-apply ref code from URL ?ref=xxx
      const urlRefCode = new URLSearchParams(window.location.search).get("ref");
      if (urlRefCode && urlRefCode.trim()) {
        const codeInput = document.getElementById("referral_code_input");
        if (codeInput) codeInput.value = urlRefCode.trim();
        try {
          await applyReferralCode();
          // Clean URL after successful apply
          const url = new URL(window.location);
          url.searchParams.delete("ref");
          window.history.replaceState({}, "", url.toString());
        } catch (_) {}
      }
    }
  } catch (e) {
    // silently ignore : not critical
  }
}

async function applyReferralCode() {
  const account = document.getElementById("account_address")?.value?.trim() || "";
  const codeInput = document.getElementById("referral_code_input");
  const msgEl = document.getElementById("referral_msg");
  const code = codeInput?.value?.trim() || "";
  if (!account || !account.startsWith("0x")) {
    if (msgEl) { msgEl.textContent = "Connect your wallet first."; msgEl.className = "referral-msg err"; msgEl.classList.remove("hidden"); }
    return;
  }
  if (!code) {
    if (msgEl) { msgEl.textContent = "Enter a referral code."; msgEl.className = "referral-msg err"; msgEl.classList.remove("hidden"); }
    return;
  }
  const applyBtn = document.getElementById("referral_apply_btn");
  if (applyBtn) applyBtn.disabled = true;
  try {
    await apiPost("/api/referral/apply", { account_address: account, code });
    if (codeInput) codeInput.value = "";
    // Refresh to get actual discount rate, then show dynamic message
    await refreshReferralStatus();
    const freshData = await apiGet(`/api/referral/status?account_address=${encodeURIComponent(account)}`);
    const pct = Math.round((Number(freshData.discount_rate) || 0.10) * 100);
    if (msgEl) { msgEl.textContent = `Code applied! −${pct}% fee discount is now active.`; msgEl.className = "referral-msg ok"; msgEl.classList.remove("hidden"); }
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    if (msgEl) { msgEl.textContent = msg; msgEl.className = "referral-msg err"; msgEl.classList.remove("hidden"); }
  } finally {
    if (applyBtn) applyBtn.disabled = false;
  }
}

function setEditCodeRowVisible(visible) {
  const row = document.getElementById("referral_set_code_row");
  const editBtn = document.getElementById("referral_edit_code_btn");
  if (row) row.classList.toggle("hidden", !visible);
  if (editBtn) editBtn.classList.toggle("hidden", !!visible);
}

async function setReferralCode() {
  const account = document.getElementById("account_address")?.value?.trim() || "";
  const input = document.getElementById("referral_new_code_input");
  const msgEl = document.getElementById("referral_set_code_msg");
  const code = input?.value?.trim().toLowerCase() || "";
  if (!code) {
    if (msgEl) { msgEl.textContent = "Enter a code."; msgEl.className = "referral-msg err"; msgEl.classList.remove("hidden"); }
    return;
  }
  const saveBtn = document.getElementById("referral_set_code_btn");
  if (saveBtn) saveBtn.disabled = true;
  try {
    const data = await apiPost("/api/referral/set_code", { account_address: account, code });
    if (input) input.value = "";
    setEditCodeRowVisible(false);
    const newCode = String(data.code || "");
    const link = `${window.location.origin}/?ref=${newCode}`;
    const linkEl = document.getElementById("referral_link_display");
    if (linkEl) linkEl.textContent = link;
    if (msgEl) { msgEl.textContent = "Code updated."; msgEl.className = "referral-msg ok"; msgEl.classList.remove("hidden"); }
    setTimeout(() => msgEl && msgEl.classList.add("hidden"), 3000);
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    if (msgEl) { msgEl.textContent = msg; msgEl.className = "referral-msg err"; msgEl.classList.remove("hidden"); }
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function refreshExtendedCredentialsStatus(opts = {}) {
  const force = !!opts.force;
  const account = document.getElementById("account_address")?.value?.trim() || "";
  if (!isExtendedMode()) {
    extendedCredentialsReadyForAccount = false;
    extendedCredentialsStatusCache = { key: "", ts: 0, value: null };
    updateConnectButtonLabel();
    updateConnectStepsState();
    return {
      ready: false,
      api_key_present: false,
      stark_public_key_present: false,
      stark_private_key_present: false,
      vault_address_present: false,
    };
  }
  if (!account || !account.startsWith("0x")) {
    extendedCredentialsReadyForAccount = false;
    extendedCredentialsStatusCache = { key: "", ts: 0, value: null };
    setExtendedCredentialsStatus("Extended credentials: account missing.", "warn");
    updateConnectButtonLabel();
    updateConnectStepsState();
    return {
      ready: false,
      api_key_present: false,
      stark_public_key_present: false,
      stark_private_key_present: false,
      vault_address_present: false,
    };
  }
  const cacheKey = normalizeAccount(account);
  _activateAuthForAccount(account);
  if (
    !force &&
    extendedCredentialsStatusCache.key === cacheKey &&
    extendedCredentialsStatusCache.value &&
    Date.now() - extendedCredentialsStatusCache.ts < EXTENDED_CREDENTIALS_CACHE_TTL_MS
  ) {
    const cached = extendedCredentialsStatusCache.value;
    extendedCredentialsReadyForAccount = !!cached.ready;
    if (cached.ready) {
      setExtendedCredentialsStatus("Extended credentials: ready.", "ok");
    } else {
      setExtendedCredentialsStatus("Extended credentials: missing fields.", "warn");
    }
    updateConnectButtonLabel();
    updateConnectStepsState();
    return cached;
  }
  try {
    const data = await apiGet(`/api/extended/credentials/status?account_address=${encodeURIComponent(account)}`);
    _extendedSubaccounts = Array.isArray(data && data.subaccounts) ? data.subaccounts : [];
    // Un sous-compte sélectionné qui n'existe plus → retour au compte principal.
    if (_extendedAccountIndex > 0 && !_extendedSubaccounts.some(s2 => s2.account_index === _extendedAccountIndex && s2.ready)) {
      _extendedAccountIndex = 0;
      try { localStorage.setItem(EXTENDED_TA_STORAGE_KEY, "0"); } catch (_) {}
    }
    renderTradingAccountSelector();
    const ready = !!(data && data.ready);
    const result = {
      ready,
      api_key_present: !!(data && data.api_key_present),
      stark_public_key_present: !!(data && data.stark_public_key_present),
      stark_private_key_present: !!(data && data.stark_private_key_present),
      vault_address_present: !!(data && data.vault_address_present),
    };
    extendedCredentialsReadyForAccount = ready;
    extendedCredentialsStatusCache = { key: cacheKey, ts: Date.now(), value: result };
    if (ready) {
      setExtendedCredentialsStatus("Extended credentials: ready.", "ok");
    } else {
      const missing = [];
      if (!result.api_key_present) missing.push("API key");
      if (!result.stark_public_key_present) missing.push("Stark public key");
      if (!result.stark_private_key_present) missing.push("Stark private key");
      if (!result.vault_address_present) missing.push("Vault number");
      setExtendedCredentialsStatus(`Extended credentials missing: ${missing.join(", ")}.`, "warn");
    }
    updateConnectButtonLabel();
    updateConnectStepsState();
    return result;
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    extendedCredentialsReadyForAccount = false;
    extendedCredentialsStatusCache = {
      key: cacheKey,
      ts: Date.now(),
      value: {
        ready: false,
        api_key_present: false,
        stark_public_key_present: false,
        stark_private_key_present: false,
        vault_address_present: false,
      },
    };
    setExtendedCredentialsStatus(`Extended credentials check failed: ${msg}`, "error");
    updateConnectButtonLabel();
    updateConnectStepsState();
    return extendedCredentialsStatusCache.value;
  }
}

function resolveHyperliquidChainLabel() {
  const mode = document.getElementById("mode")?.value || "hyperliquid_mainnet";
  return mode === "hyperliquid_testnet" ? "Testnet" : "Mainnet";
}

function normalizeChainIdHex(raw) {
  const text = String(raw || "").trim().toLowerCase();
  if (!text) return "";
  try {
    const n = text.startsWith("0x") ? parseInt(text, 16) : parseInt(text, 10);
    if (!Number.isFinite(n) || n <= 0) return "";
    return `0x${n.toString(16)}`;
  } catch (_) {
    return "";
  }
}

function chainLabelFromChainId(chainIdHex) {
  const id = normalizeChainIdHex(chainIdHex);
  if (!id) return "Unknown";
  if (id === normalizeChainIdHex(HL_SIGNATURE_CHAIN_ID_HEX)) return "Arb Sepolia";
  if (id === "0x1") return "Ethereum";
  if (id === "0xa4b1") return "Arbitrum";
  if (id === "0x89") return "Polygon";
  if (id === RISE_CHAIN_ID_HEX) return "RISE";
  return id;
}

function chainIconFromChainId(chainIdHex) {
  const id = normalizeChainIdHex(chainIdHex);
  if (!id) return "";
  if (id === normalizeChainIdHex(HL_SIGNATURE_CHAIN_ID_HEX)) return "./arb.svg";
  if (id === "0xa4b1") return "./arb.svg";
  if (id === "0x1") return "./eth.webp";
  return "";
}

function renderWalletNetworkBadge(badge, { label, title, tone = "warn", iconSrc = "" }) {
  badge.classList.toggle("ok", tone === "ok");
  badge.classList.toggle("warn", tone !== "ok");
  badge.textContent = "";
  if (iconSrc) {
    const img = document.createElement("img");
    img.src = iconSrc;
    img.alt = "";
    img.className = "wallet-network-icon";
    badge.appendChild(img);
  }
  if (!iconSrc) {
    const text = document.createElement("span");
    text.textContent = label;
    badge.appendChild(text);
  }
  badge.title = title || "";
}

async function refreshWalletNetworkBadge() {
  const badge = document.getElementById("wallet_network_badge");
  if (!badge) return;
  const provider = _tradoorActiveProvider || window.ethereum;
  if (!provider || typeof provider.request !== "function") {
    renderWalletNetworkBadge(badge, {
      label: "No wallet",
      tone: "warn",
      title: "No wallet detected",
    });
    return;
  }
  try {
    const currentRaw = await provider.request({ method: "eth_chainId" });
    const currentChain = normalizeChainIdHex(currentRaw);
    // RISEx signe sur la chaîne RISE (4153), les autres venues sur la chaîne HL.
    const expected = normalizeChainIdHex(isRisexMode() ? RISE_CHAIN_ID_HEX : HL_SIGNATURE_CHAIN_ID_HEX);
    const onExpected = !!(currentChain && expected && currentChain === expected);
    renderWalletNetworkBadge(badge, {
      label: chainLabelFromChainId(currentChain),
      tone: onExpected ? "ok" : "warn",
      iconSrc: chainIconFromChainId(currentChain),
      title: onExpected
        ? "Signature network is ready."
        : `Switch to ${chainLabelFromChainId(expected)} for signatures.`,
    });
  } catch (_) {
    renderWalletNetworkBadge(badge, {
      label: "Network ?",
      tone: "warn",
      title: "Unable to read wallet network",
    });
  }
}

async function ensureWalletSignatureChain(preferredChainId = HL_SIGNATURE_CHAIN_ID_HEX, addChainParams = null) {
  const provider = _tradoorActiveProvider || window.ethereum;
  if (!provider || typeof provider.request !== "function") {
    return normalizeChainIdHex(preferredChainId) || HL_SIGNATURE_CHAIN_ID_HEX;
  }
  const currentRaw = await provider.request({ method: "eth_chainId" });
  const currentChain = normalizeChainIdHex(currentRaw);
  const preferred = normalizeChainIdHex(preferredChainId) || HL_SIGNATURE_CHAIN_ID_HEX;
  if (currentChain === preferred) {
    return preferred;
  }
  try {
    await provider.request({
      method: "wallet_switchEthereumChain",
      params: [{ chainId: preferred }],
    });
    return preferred;
  } catch (err) {
    const code = Number(err && err.code);
    if (code === 4902) {
      try {
        await provider.request({
          method: "wallet_addEthereumChain",
          params: [addChainParams || HL_SIGNATURE_CHAIN],
        });
        await provider.request({
          method: "wallet_switchEthereumChain",
          params: [{ chainId: preferred }],
        });
        return preferred;
      } catch (_) {
        // Fallback below
      }
    }
    // Fallback: use current chain id to avoid MetaMask chain mismatch errors.
    return currentChain || preferred;
  }
}

async function signTypedDataWithWallet(address, typedData) {
  const provider = await _getWalletProvider();
  if (!provider || typeof provider.request !== "function") {
    throw new Error("No Ethereum wallet found.");
  }
  const payload = JSON.stringify(typedData);
  try {
    return await provider.request({
      method: "eth_signTypedData_v4",
      params: [address, payload],
    });
  } catch (err) {
    try {
      return await provider.request({
        method: "eth_signTypedData",
        params: [address, typedData],
      });
    } catch (_) {
      throw err;
    }
  }
}

async function approveBuilderFeeWithWallet(modeOverride) {
  // RISEx : approbation par signer temporaire, flux dédié (2 signatures EIP-712).
  if (isRisexMode(modeOverride || document.getElementById("mode")?.value || "")) {
    return approveRisexBuilderWithWallet();
  }
  const account = document.getElementById("account_address")?.value?.trim() || "";
  if (!account || !account.startsWith("0x") || account.length < 10) {
    throw new Error("Enter a valid account address first.");
  }
  _activateAuthForAccount(account);
  if (!isWalletAuthValidForAccount(account)) {
    await connectAndSignWallet();
  }
  const provider = await _getWalletProvider();
  if (!provider || typeof provider.request !== "function") {
    throw new Error("No Ethereum wallet found.");
  }
  const accounts = await provider.request({ method: "eth_requestAccounts" });
  const walletAddress = Array.isArray(accounts) && accounts.length ? String(accounts[0]) : "";
  if (!walletAddress) {
    throw new Error("Wallet connection failed.");
  }
  if (normalizeAccount(walletAddress) !== normalizeAccount(account)) {
    throw new Error("Connected wallet does not match account address.");
  }

  const mode = modeOverride || document.getElementById("mode")?.value || "hyperliquid_mainnet";
  if (isExtendedMode(mode)) {
    const challenge = await apiPost("/api/builder/approve_challenge", {
      account_address: account,
      mode,
    });
    if (!challenge || !challenge.message || !challenge.nonce) {
      throw new Error("Failed to create Extended builder approval challenge.");
    }
    const signature = await provider.request({
      method: "personal_sign",
      params: [challenge.message, walletAddress],
    });
    await apiPost("/api/builder/approve_submit", {
      account_address: account,
      mode,
      nonce: challenge.nonce,
      signature,
    });
    return true;
  }
  const signatureChainId = await ensureWalletSignatureChain(HL_SIGNATURE_CHAIN_ID_HEX);
  await refreshWalletNetworkBadge();
  const challenge = await apiPost("/api/builder/approve_challenge", {
    account_address: account,
    mode,
    signature_chain_id: signatureChainId,
  });
  if (!challenge || !challenge.typed_data || !challenge.nonce) {
    throw new Error("Failed to create builder approval challenge.");
  }
  const typedData = challenge.typed_data;
  if (!typedData.message || !typedData.domain || !typedData.types) {
    throw new Error("Invalid builder approval typed data.");
  }
  typedData.message.signatureChainId = normalizeChainIdHex(typedData.message.signatureChainId) || signatureChainId;
  if (!typedData.message.hyperliquidChain) {
    typedData.message.hyperliquidChain = resolveHyperliquidChainLabel();
  }
  const domainChain = parseInt(normalizeChainIdHex(typedData.message.signatureChainId), 16);
  if (Number.isFinite(domainChain) && domainChain > 0) {
    typedData.domain.chainId = domainChain;
  }
  const signature = await signTypedDataWithWallet(walletAddress, typedData);
  await apiPost("/api/builder/approve_submit", {
    account_address: account,
    mode,
    nonce: challenge.nonce,
    signature,
  });
  return true;
}

async function refreshAgentStatus(opts = {}) {
  if (isExtendedMode()) {
    // Keep agent state false on modes where agent approval is N/A to avoid
    // leaking a "done" state when switching back to Hyperliquid.
    agentApprovedForAccount = false;
    agentStatusCache = { key: "", ts: 0, value: null };
    setAgentStatus("3/3 N/A on Extended mode.", "ok");
    updateAgentApproveButtonState(false);
    return { approved: false, message: "Not required on Extended mode.", agent_address: "" };
  }
  if (isQfexMode()) {
    // Pas d'agent TRADOR sur QFEX : le bot utilise la clé de trading de l'utilisateur.
    agentApprovedForAccount = false;
    agentStatusCache = { key: "", ts: 0, value: null };
    setAgentStatus("3/3 N/A on QFEX mode.", "ok");
    updateAgentApproveButtonState(false);
    return { approved: false, message: "Not required on QFEX mode.", agent_address: "" };
  }
  if (isRisexMode()) {
    // Pas d'agent TRADOR sur RISEx : le bot signe avec un signer restreint au
    // trading perp (étape « Connect RISEx »). Même traitement qu'Extended.
    agentApprovedForAccount = false;
    agentStatusCache = { key: "", ts: 0, value: null };
    setAgentStatus("3/3 N/A on RISEx mode.", "ok");
    updateAgentApproveButtonState(false);
    return { approved: false, message: "Not required on RISEx mode.", agent_address: "" };
  }
  if (isNadoMode()) {
    agentApprovedForAccount = false;
    agentStatusCache = { key: "", ts: 0, value: null };
    setAgentStatus("3/3 N/A on Nado mode.", "ok");
    updateAgentApproveButtonState(false);
    return { approved: false, message: "Not required on Nado mode.", agent_address: "" };
  }
  const force = !!opts.force;
  const account = document.getElementById("account_address")?.value?.trim() || "";
  const mode = document.getElementById("mode")?.value || "hyperliquid_mainnet";
  const cacheKey = `${normalizeAccount(account)}|${mode}`;
  if (!account || !account.startsWith("0x")) {
    agentApprovedForAccount = false;
    agentStatusCache = { key: "", ts: 0, value: null };
    setAgentStatus("3/3 Approve TRADOR agent.", "warn");
    updateAgentApproveButtonState(false);
    return { approved: false, message: "Account address missing." };
  }
  _activateAuthForAccount(account);
  if (
    !force &&
    agentStatusCache.key === cacheKey &&
    agentStatusCache.value &&
    Date.now() - agentStatusCache.ts < APPROVAL_STATUS_CACHE_TTL_MS
  ) {
    const cached = agentStatusCache.value;
    agentApprovedForAccount = !!cached.approved;
    if (cached.approved) {
      const agentAddr = cached.agent_address ? ` (${cached.agent_address})` : "";
      setAgentStatus(`3/3 ${AGENT_NAME} agent approved${agentAddr}.`, "ok");
      updateAgentApproveButtonState(true);
    } else {
      setAgentStatus(`3/3 Pending: ${cached.message || `${AGENT_NAME} agent missing.`}`, "warn");
      updateAgentApproveButtonState(false);
    }
    return cached;
  }
  try {
    const data = await apiGet(
      `/api/agent/status?account_address=${encodeURIComponent(account)}&mode=${encodeURIComponent(mode)}${force ? "&force=1" : ""}`,
    );
    if (data && data.approved) {
      agentApprovedForAccount = true;
      const agentAddr = data.agent_address ? ` (${data.agent_address})` : "";
      setAgentStatus(`3/3 ${AGENT_NAME} agent approved${agentAddr}.`, "ok");
      updateAgentApproveButtonState(true);
    } else {
      agentApprovedForAccount = false;
      const msg = data && data.message ? data.message : `${AGENT_NAME} agent approval missing.`;
      setAgentStatus(`3/3 Pending: ${msg}`, "warn");
      updateAgentApproveButtonState(false);
    }
    const result = {
      approved: !!(data && data.approved),
      message: (data && data.message) || "",
      agent_address: data && data.agent_address ? data.agent_address : "",
    };
    agentStatusCache = { key: cacheKey, ts: Date.now(), value: result };
    return result;
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    agentApprovedForAccount = false;
    agentStatusCache = { key: cacheKey, ts: Date.now(), value: { approved: false, message: msg, agent_address: "" } };
    setAgentStatus(`3/3 Check failed: ${msg}`, "error");
    updateAgentApproveButtonState(false);
    return { approved: false, message: msg };
  }
}

async function approveAgentWithWallet() {
  const account = document.getElementById("account_address")?.value?.trim() || "";
  if (!account || !account.startsWith("0x") || account.length < 10) {
    throw new Error("Enter a valid account address first.");
  }
  _activateAuthForAccount(account);
  if (!isWalletAuthValidForAccount(account)) {
    await connectAndSignWallet();
  }
  const provider = await _getWalletProvider();
  if (!provider || typeof provider.request !== "function") {
    throw new Error("No Ethereum wallet found.");
  }
  const accounts = await provider.request({ method: "eth_requestAccounts" });
  const walletAddress = Array.isArray(accounts) && accounts.length ? String(accounts[0]) : "";
  if (!walletAddress) {
    throw new Error("Wallet connection failed.");
  }
  if (normalizeAccount(walletAddress) !== normalizeAccount(account)) {
    throw new Error("Connected wallet does not match account address.");
  }
  const mode = document.getElementById("mode")?.value || "hyperliquid_mainnet";
  const signatureChainId = await ensureWalletSignatureChain(HL_SIGNATURE_CHAIN_ID_HEX);
  await refreshWalletNetworkBadge();
  const challenge = await apiPost("/api/agent/approve_challenge", {
    account_address: account,
    mode,
    signature_chain_id: signatureChainId,
    agent_name: AGENT_NAME,
  });
  if (!challenge || !challenge.typed_data || !challenge.nonce) {
    throw new Error("Failed to create agent approval challenge.");
  }
  const typedData = challenge.typed_data;
  if (!typedData.message || !typedData.domain || !typedData.types) {
    throw new Error("Invalid agent approval typed data.");
  }
  typedData.message.signatureChainId = normalizeChainIdHex(typedData.message.signatureChainId) || signatureChainId;
  if (!typedData.message.hyperliquidChain) {
    typedData.message.hyperliquidChain = resolveHyperliquidChainLabel();
  }
  if (!typedData.message.agentName) {
    typedData.message.agentName = AGENT_NAME;
  }
  const domainChain = parseInt(normalizeChainIdHex(typedData.message.signatureChainId), 16);
  if (Number.isFinite(domainChain) && domainChain > 0) {
    typedData.domain.chainId = domainChain;
  }
  const signature = await signTypedDataWithWallet(walletAddress, typedData);
  await apiPost("/api/agent/approve_submit", {
    account_address: account,
    nonce: challenge.nonce,
    signature,
  });
  await refreshKeyStatus();
  return true;
}

// ── HL sub-accounts: generic challenge → sign → submit helper ──────────────
// Réutilisé par le drawer Connections (create/fund/withdraw) et par la Task 12
// (CTA "Fund" au precheck du start form).
async function subaccountAction(kind, params) {
  const addr = document.getElementById("account_address")?.value || "";
  const ch = await apiPost("/api/hl/subaccount/action_challenge",
                           Object.assign({ kind }, params));
  // Voie agent : le serveur a déjà signé (chainId 1337) et posté — rien à signer
  // côté wallet (certains wallets refusent un domaine EIP-712 ≠ chaîne active).
  if (ch && ch.done) return ch.response;
  const sig = await signTypedDataWithWallet(addr, ch.typed_data);
  return apiPost("/api/hl/subaccount/action_submit", { nonce: ch.nonce, signature: sig });
}

async function approveNadoAgentWithWallet() {
  const account = document.getElementById("account_address")?.value?.trim() || "";
  if (!account || !account.startsWith("0x") || account.length < 10) {
    throw new Error("Enter a valid account address first.");
  }
  _activateAuthForAccount(account);
  if (!isWalletAuthValidForAccount(account)) {
    await connectAndSignWallet();
  }
  const provider = await _getWalletProvider();
  if (!provider || typeof provider.request !== "function") {
    throw new Error("No Ethereum wallet found.");
  }
  const accounts = await provider.request({ method: "eth_requestAccounts" });
  const walletAddress = Array.isArray(accounts) && accounts.length ? String(accounts[0]) : "";
  if (!walletAddress) throw new Error("Wallet connection failed.");
  if (normalizeAccount(walletAddress) !== normalizeAccount(account)) {
    throw new Error("Connected wallet does not match account address.");
  }
  // Switch wallet to Ink L2 (chainId 57073) for EIP-712 signing
  await ensureWalletSignatureChain(NADO_CHAIN_ID_HEX, NADO_CHAIN);
  await refreshWalletNetworkBadge();
  const challenge = await apiPost("/api/nado/approve_challenge", {
    account_address: account,
  });
  if (!challenge || !challenge.typed_data || !challenge.nonce) {
    throw new Error("Failed to create Nado agent approval challenge.");
  }
  const typedData = challenge.typed_data;
  if (!typedData.message || !typedData.domain || !typedData.types) {
    throw new Error("Invalid Nado agent approval typed data.");
  }
  const signature = await signTypedDataWithWallet(walletAddress, typedData);
  await apiPost("/api/nado/approve_submit", {
    account_address: account,
    nonce: challenge.nonce,
    signature,
  });
  nadoAgentApprovedForAccount = true;
  updateConnectStepsState();
  return true;
}

async function refreshNadoAgentStatus(opts = {}) {
  const force = !!opts.force;
  const account = document.getElementById("account_address")?.value?.trim() || "";
  if (!account) { nadoAgentApprovedForAccount = false; return; }
  _activateAuthForAccount(account);
  // If already approved in this session (e.g. just approved), don't let a
  // stale on-chain query overwrite it : trust the approval for 30s.
  if (nadoAgentApprovedForAccount && !force) return;
  const key = account.toLowerCase();
  const now = Date.now();
  if (!force && nadoAgentStatusCache.key === key && now - nadoAgentStatusCache.ts < APPROVAL_STATUS_CACHE_TTL_MS) {
    nadoAgentApprovedForAccount = !!nadoAgentStatusCache.value?.approved;
    return;
  }
  try {
    const data = await apiGet(`/api/nado/agent/status?account_address=${encodeURIComponent(account)}`);
    nadoAgentStatusCache = { key, ts: now, value: data };
    // With force=true (e.g. pre-start check), always trust the on-chain result
    // so a stale linked signer is detected.  Without force, protect against
    // transient query failures overwriting a fresh in-session approval.
    if (force || !!data?.approved || !nadoAgentApprovedForAccount) {
      nadoAgentApprovedForAccount = !!data?.approved;
    }
  } catch (e) {
    // Don't reset to false if already approved in this session
    if (!nadoAgentApprovedForAccount) {
      nadoAgentApprovedForAccount = false;
    }
  }
}

async function _setupDexHyperliquid() {
  const builderStatus = await refreshBuilderStatus({ force: true });
  if (!builderStatus.approved) {
    await approveBuilderFeeWithWallet();
    await refreshBuilderStatus({ force: true });
  }
  const agentStatus = await refreshAgentStatus({ force: true });
  if (!agentStatus.approved) {
    await approveAgentWithWallet();
    await refreshAgentStatus({ force: true });
  }
  _dexState.hyperliquid.connected = true;
  _dexState.hyperliquid.address = _dexState.hyperliquid.address || walletAuthAccount;
  _dexState.hyperliquid.builderApproved = !!builderApprovedForAccount;
  _dexState.hyperliquid.agentApproved = !!agentApprovedForAccount;
}

// Pause entre deux demandes de signature : Rabby n'ouvre pas toujours sa popup
// quand une requête arrive pendant que la précédente se ferme (elle reste alors
// en notification silencieuse). Un court délai laisse la popup se re-armer.
function _walletSignPause() {
  return new Promise((resolve) => setTimeout(resolve, 900));
}

// Enrobe une demande de signature : si le wallet n'a pas répondu après 3,5 s,
// affiche un indice dans le modal (Rabby met parfois la demande en notification
// silencieuse au lieu de rouvrir sa popup — impossible à forcer depuis la page).
async function _signWithHint(fn) {
  const host = document.getElementById("cm_substeps");
  const timer = setTimeout(() => {
    if (!host) return;
    let hint = document.getElementById("cm_rabby_hint");
    if (!hint) {
      hint = document.createElement("div");
      hint.id = "cm_rabby_hint";
      hint.className = "cm-rabby-hint";
      hint.textContent = "No wallet popup? Click the Rabby icon in your browser — the signature request is waiting there.";
      host.insertAdjacentElement("afterend", hint);
    }
    hint.classList.remove("hidden");
  }, 3500);
  try {
    return await fn();
  } finally {
    clearTimeout(timer);
    document.getElementById("cm_rabby_hint")?.classList.add("hidden");
  }
}

// Connexion Extended « par signatures » : dérive les clés Stark d'une signature
// EIP-712 (même dérivation que l'app officielle), récupère le vault et crée
// l'API key via des signatures d'auth — plus aucun copier-coller de credentials.
async function connectExtendedWithSignatures({ onProgress, accountIndex = 0 } = {}) {
  const progress = typeof onProgress === "function" ? onProgress : () => {};
  const account = walletAuthAccount;
  if (!account) throw new Error("Connect your wallet first.");
  const provider = await _getWalletProvider();
  if (!provider || typeof provider.request !== "function") {
    throw new Error("No Ethereum wallet found.");
  }
  const accounts = await provider.request({ method: "eth_requestAccounts" });
  const walletAddress = Array.isArray(accounts) && accounts.length ? String(accounts[0]) : "";
  if (!walletAddress || normalizeAccount(walletAddress) !== normalizeAccount(account)) {
    throw new Error("Connected wallet does not match account address.");
  }

  const ch = await apiPost("/api/extended/connect/challenge", { account_address: account, account_index: accountIndex });
  if (!ch || !ch.nonce || !ch.derivation_typed_data) {
    throw new Error("Failed to create Extended connect challenge.");
  }

  progress("derive", "active");
  const derivationSig = await _signWithHint(() => signTypedDataWithWallet(walletAddress, ch.derivation_typed_data));
  progress("derive", "done");

  // Les messages d'auth Extended (`path@time`) sont validés avec une fenêtre de
  // fraîcheur côté venue : le timestamp doit être généré À L'INSTANT de chaque
  // signature (un timestamp figé au challenge expire pendant que l'utilisateur
  // signe les popups → 401 sur la création d'API key).
  const authTs = () => new Date().toISOString().slice(0, 19) + "Z";
  const authPaths = ch.auth_paths || {
    accounts: "/api/v1/user/accounts",
    api_key: "/api/v1/user/account/api-key",
  };

  progress("verify", "active");
  await _walletSignPause();
  const accountsTs = authTs();
  const accountsSig = await _signWithHint(() => provider.request({
    method: "personal_sign",
    params: [`${authPaths.accounts}@${accountsTs}`, walletAddress],
  }));
  await _walletSignPause();
  const apiKeyTs = authTs();
  const apiKeySig = await _signWithHint(() => provider.request({
    method: "personal_sign",
    params: [`${authPaths.api_key}@${apiKeyTs}`, walletAddress],
  }));

  const body = {
    account_address: account,
    nonce: ch.nonce,
    derivation_signature: derivationSig,
    accounts_auth_signature: accountsSig,
    accounts_auth_time: accountsTs,
    api_key_auth_signature: apiKeySig,
    api_key_auth_time: apiKeyTs,
  };
  // Les signatures de vérification sont faites : `verify` est terminé. L'appel
  // /api/extended/connect crée l'API key → c'est `apikey` qui doit porter l'erreur
  // (un 502 de création tombait auparavant sur `verify`).
  progress("verify", "done");
  progress("apikey", "active");
  let out = await apiPost("/api/extended/connect", body);
  if (out && out.needs_subaccount) {
    // Sous-compte inexistant : une signature d'auth de plus sur le chemin dédié
    // (pas de registration EIP-712 pour les subs), timestamp frais.
    progress("onboard", "active");
    await _walletSignPause();
    const subTs = authTs();
    const subSig = await _signWithHint(() => provider.request({
      method: "personal_sign",
      params: [`${authPaths.sub_onboard || "/auth/onboard/subaccount"}@${subTs}`, walletAddress],
    }));
    progress("onboard", "done");
    progress("apikey", "active");
    out = await apiPost("/api/extended/connect", {
      ...body,
      sub_onboard_auth_signature: subSig,
      sub_onboard_time: subTs,
    });
  } else if (out && out.needs_onboarding) {
    // Onboarding requis : `apikey` repasse en attente le temps de la création de
    // compte, sinon deux sous-étapes tourneraient en même temps.
    progress("apikey", "pending");
    progress("onboard", "active");
    // Même principe de fraîcheur pour la registration : timestamp régénéré au
    // moment de signer, injecté dans le typed data et transmis au serveur.
    await _walletSignPause();
    const regTs = authTs();
    const regTypedData = JSON.parse(JSON.stringify(ch.registration_typed_data));
    regTypedData.message.time = regTs;
    const registrationSig = await _signWithHint(() => signTypedDataWithWallet(walletAddress, regTypedData));
    progress("onboard", "done");
    progress("apikey", "active");
    out = await apiPost("/api/extended/connect", {
      ...body,
      registration_signature: registrationSig,
      registration_time: regTs,
    });
  }
  if (!out || out.status !== "connected" || !out.ready) {
    throw new Error("Extended connect failed. Please retry.");
  }
  progress("apikey", "done");
  progress("save", "done");
  return out;
}

// Crée le sous-compte Extended d'index N via le flux de signatures, puis le
// sélectionne comme compte de trading.
// Découverte des sous-comptes Extended : une seule signature d'auth L1, aucune
// dérivation de clés. Fait apparaître les subs créés via l'app officielle.
async function extendedDetectSubaccounts(btn) {
  const account = walletAuthAccount;
  if (!account) { showError("main_error", "Connect your wallet first."); return; }
  const prevLabel = btn ? btn.textContent : "";
  try {
    showError("main_error", "");
    if (btn) { btn.disabled = true; btn.textContent = "detecting…"; }
    const provider = await _getWalletProvider();
    const accounts = await provider.request({ method: "eth_requestAccounts" });
    const walletAddress = Array.isArray(accounts) && accounts.length ? String(accounts[0]) : "";
    if (!walletAddress || normalizeAccount(walletAddress) !== normalizeAccount(account)) {
      throw new Error("Connected wallet does not match account address.");
    }
    const ts = new Date().toISOString().slice(0, 19) + "Z";
    const sig = await _signWithHint(() => provider.request({
      method: "personal_sign",
      params: [`/api/v1/user/accounts@${ts}`, walletAddress],
    }));
    const data = await apiPost("/api/extended/subaccounts/refresh", {
      account_address: account,
      auth_signature: sig,
      auth_time: ts,
    });
    _extendedSubaccounts = Array.isArray(data && data.subaccounts) ? data.subaccounts : [];
    renderTradingAccountSelector();
  } catch (e) {
    showError("main_error", `Sub-account detection failed: ${e && e.message ? e.message : e}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = prevLabel || "↻ detect subs"; }
  }
}

async function createExtendedSubaccount(idx) {
  const n = Math.max(1, Math.min(2, parseInt(idx, 10) || 1));
  try {
    showError("main_error", "");
    await connectExtendedWithSignatures({ accountIndex: n });
    await refreshExtendedCredentialsStatus({ force: true });
    setExtendedAccountIndex(n);
  } catch (e) {
    showError("main_error", `Sub-account creation failed: ${e && e.message ? e.message : e}`);
  }
}

// ── RISEx : réseau, statut, signatures, bandeau d'expiration, quota ──────────
// Le compte ne fait que des signatures gratuites : le serveur prépare les données
// typées EIP-712, le wallet les signe sur la chaîne RISE (4153), le serveur
// relaie la transaction (gas payé par le wallet relais Tradoor).

// Erreur wallet lisible (4001 refus, -32002 demande déjà en attente).
function _risexWalletError(e) {
  const code = Number(e && e.code);
  if (code === -32002) {
    return new Error("A request is already pending in your wallet. Open the wallet extension, approve or reject it, then retry.");
  }
  if (code === 4001) return new Error("Request rejected in the wallet.");
  if (e instanceof Error) return e;
  const msg = e && e.message ? e.message : String(e);
  return new Error(Number.isFinite(code) ? `${msg} (code ${code})` : msg);
}

async function _risexReadChainId(provider) {
  try {
    return normalizeChainIdHex(await provider.request({ method: "eth_chainId" }));
  } catch (_) {
    return "";
  }
}

// Bascule le wallet sur RISE. Tout échec de bascule autre qu'un refus (4001) ou
// qu'une demande en attente (-32002) → ajout du réseau : tous les wallets ne
// renvoient pas 4902 pour un réseau inconnu. Re-bascule si eth_chainId ne suit pas.
async function ensureRiseChain() {
  const provider = await _getWalletProvider();
  if (!provider || typeof provider.request !== "function") {
    throw new Error("No Ethereum wallet found.");
  }
  if ((await _risexReadChainId(provider)) === RISE_CHAIN_ID_HEX) return RISE_CHAIN_ID_HEX;
  try {
    await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: RISE_CHAIN_ID_HEX }] });
  } catch (e) {
    const code = Number(e && e.code);
    if (code === 4001 || code === -32002) throw _risexWalletError(e);
    try {
      await provider.request({ method: "wallet_addEthereumChain", params: [RISE_CHAIN_PARAMS] });
    } catch (e2) {
      throw _risexWalletError(e2);
    }
  }
  if ((await _risexReadChainId(provider)) !== RISE_CHAIN_ID_HEX) {
    try {
      await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: RISE_CHAIN_ID_HEX }] });
    } catch (e3) {
      throw _risexWalletError(e3);
    }
    const now = await _risexReadChainId(provider);
    if (now && now !== RISE_CHAIN_ID_HEX) {
      throw new Error("Your wallet is not on the RISE network (chain 4153). Switch network in the wallet, then retry.");
    }
  }
  try { await refreshWalletNetworkBadge(); } catch (_) {}
  return RISE_CHAIN_ID_HEX;
}

function _risexShortAddr(addr) {
  const a = String(addr || "");
  return a.length > 12 ? `${a.slice(0, 6)}...${a.slice(-4)}` : a;
}

// Le serveur refuse une signature qui ne se recouvre pas vers le compte connecté :
// relire le compte actif du wallet juste avant chaque signature.
async function _risexAssertWalletAccount(account) {
  const provider = await _getWalletProvider();
  if (!provider || typeof provider.request !== "function") {
    throw new Error("No Ethereum wallet found.");
  }
  let accounts = [];
  try {
    accounts = await provider.request({ method: "eth_accounts" });
  } catch (e) {
    throw _risexWalletError(e);
  }
  const active = Array.isArray(accounts) && accounts.length ? String(accounts[0]) : "";
  if (!active) throw new Error("No account unlocked in your wallet. Unlock it, then retry.");
  if (normalizeAccount(active) !== normalizeAccount(account)) {
    throw new Error(
      `The active wallet account (${_risexShortAddr(active)}) is not the connected account (${_risexShortAddr(account)}). Switch account in the wallet, then retry.`,
    );
  }
  return active;
}

// Signature d'une donnée typée préparée par le serveur. Les grands entiers
// arrivent déjà en chaîne décimale (wallet_typed côté serveur) : ne rien convertir.
async function _risexSignTyped(account, typed) {
  if (!typed || typeof typed !== "object" || !typed.domain || !typed.types || !typed.message) {
    throw new Error("Invalid RISEx signature request from the server.");
  }
  await ensureRiseChain();
  const walletAddress = await _risexAssertWalletAccount(account);
  try {
    return await _signWithHint(() => signTypedDataWithWallet(walletAddress, typed));
  } catch (e) {
    throw _risexWalletError(e);
  }
}

function _risexAccount() {
  const account = (document.getElementById("account_address")?.value || "").trim() || walletAuthAccount || "";
  if (!account.startsWith("0x") || account.length < 10) throw new Error("Connect your wallet first.");
  return account;
}

// Signer prêt pour une session : présent, actif, restreint au trading perp et
// valable encore plus de 24 h (le serveur refuse le démarrage en dessous).
function _risexSignerReady(signer) {
  return !!(signer && signer.active && signer.restricted && Number(signer.expires_in_days) > 1);
}

// Renouvellement à proposer : expiration sous 7 jours, signer expiré ou inactif.
function _risexSignerNeedsRenewal(signer) {
  if (!signer) return false;
  return !!signer.expiring_soon || !signer.active || !(Number(signer.expires_in_days) > 0);
}

function _risexCachedStatusFor(account) {
  const key = normalizeAccount(account || "");
  if (!key || risexStatusCache.key !== key) return null;
  return risexStatusCache.value || null;
}

function _risexCachedSignerFor(account) {
  const st = _risexCachedStatusFor(account);
  return st ? st.signer || null : null;
}

function _applyRisexStatus(account, st) {
  const builderOk = !!(st && st.builder && st.builder.approved);
  const signerOk = _risexSignerReady(st && st.signer);
  risexBuilderApprovedForAccount = builderOk;
  risexSignerReadyForAccount = signerOk;
  risexHasCollateral = st && typeof st.has_collateral === "boolean" ? st.has_collateral : null;
  // Le flag générique ne suit RISEx que si la page vise RISEx sur ce compte.
  const pageAccount = (document.getElementById("account_address")?.value || "").trim();
  if (isRisexMode() && normalizeAccount(pageAccount) === normalizeAccount(account)) {
    builderApprovedForAccount = builderOk;
  }
  const rxAddr = _dexState.risex?.address || "";
  if (rxAddr && normalizeAccount(rxAddr) === normalizeAccount(account)) {
    _dexState.risex.builderApproved = builderOk;
    _dexState.risex.signerReady = signerOk;
  }
  renderRisexExpiryBanner();
  try { updateConnectStepsState(); } catch (_) {}
}

let _risexStatusPending = null;

// Lit GET /api/risex/status (cache 60 s, `force` relit chez RISEx). Lève en cas
// d'échec : les appelants décident (ne jamais sauter une étape sur état incertain).
async function refreshRisexStatus({ force = false, account = "" } = {}) {
  const acct = String(account || document.getElementById("account_address")?.value || "").trim();
  if (!acct.startsWith("0x") || acct.length < 10) {
    risexBuilderApprovedForAccount = false;
    risexSignerReadyForAccount = false;
    risexHasCollateral = null;
    return null;
  }
  const key = normalizeAccount(acct);
  if (
    !force &&
    risexStatusCache.key === key &&
    risexStatusCache.value &&
    Date.now() - risexStatusCache.ts < RISEX_STATUS_CACHE_TTL_MS
  ) {
    _applyRisexStatus(acct, risexStatusCache.value);
    return risexStatusCache.value;
  }
  // Une seule lecture non forcée en vol par compte (le poll des métriques en lance une).
  if (!force && _risexStatusPending && _risexStatusPending.key === key) return _risexStatusPending.promise;
  const promise = (async () => {
    const data = await apiGet(
      `/api/risex/status?account_address=${encodeURIComponent(acct)}&force=${force ? 1 : 0}`,
      { dex: "risex" },
    );
    risexStatusCache = { key, ts: Date.now(), value: data || null };
    _applyRisexStatus(acct, data || null);
    return data || null;
  })();
  if (!force) {
    _risexStatusPending = { key, promise };
    promise.finally(() => {
      if (_risexStatusPending && _risexStatusPending.promise === promise) _risexStatusPending = null;
    }).catch(() => {});
  }
  return promise;
}

function _risexInvalidateStatus() {
  risexStatusCache = { key: "", ts: 0, value: null };
}

// Étape « Approve builder fee » : RISEx exige la permission All pour approuver un
// builder. Le serveur crée un signer temporaire ; le wallet signe son
// enregistrement puis, d'avance, sa révocation. Le serveur approuve le builder
// avec ce signer puis le révoque : il n'est jamais stocké.
async function approveRisexBuilderWithWallet({ onProgress } = {}) {
  const progress = typeof onProgress === "function" ? onProgress : () => {};
  const account = _risexAccount();
  if (!isWalletAuthValidForAccount(account)) throw new Error("Connect and sign your wallet first.");
  progress("prepare", "active");
  const ch = await apiPost("/api/risex/builder/challenge", { account_address: account }, { dex: "risex" });
  if (!ch || !ch.register_typed || !ch.revoke_typed) {
    throw new Error("Failed to prepare the RISEx builder approval.");
  }
  progress("prepare", "done");
  progress("register", "active");
  const registerSig = await _risexSignTyped(account, ch.register_typed);
  progress("register", "done");
  progress("revoke", "active");
  await _walletSignPause();
  const revokeSig = await _risexSignTyped(account, ch.revoke_typed);
  progress("revoke", "done");
  progress("submit", "active");
  let res;
  try {
    res = await apiPost(
      "/api/risex/builder/submit",
      { account_address: account, register_sig: registerSig, revoke_sig: revokeSig },
      { dex: "risex", timeoutMs: RISEX_SUBMIT_TIMEOUT_MS },
    );
  } catch (e) {
    // Délai dépassé ou 5xx du proxy : le serveur a pu finir. On relit le statut.
    if (!_risexMaybeCompleted(e)) throw e;
    const st = await _risexPollStatus((s) => !!(s && s.builder && s.builder.approved));
    if (!st) throw e;
    res = { ok: true, recovered: true };
  }
  progress("submit", "done");
  _risexInvalidateStatus();
  if (res && res.ok && res.revoked === false) {
    // Le builder est approuvé ; seul le nettoyage du signer temporaire a échoué.
    const who = res.temp_signer ? ` (${res.temp_signer})` : "";
    _msShowToast(`Builder fee approved, but the temporary signer${who} could not be revoked. It expires within an hour; you can also revoke it on rise.trade.`, "info");
  }
  return res;
}

// Erreur de soumission après laquelle le serveur a pu aller au bout : délai
// dépassé côté navigateur, ou 502/504 du proxy pendant que le serveur travaille.
function _risexMaybeCompleted(err) {
  const status = Number(err && err.status);
  return !status || status >= 500;
}

// Relit le statut RISEx (force) jusqu'à ce que `done(status)` soit vrai, au plus ~60 s.
async function _risexPollStatus(done) {
  for (let i = 0; i < 12; i += 1) {
    _risexInvalidateStatus();
    let st = null;
    try { st = await refreshRisexStatus({ force: true }); } catch (_) { st = null; }
    if (done(st)) return st;
    await new Promise((r) => window.setTimeout(r, 5000));
  }
  return null;
}

// Étape « Connect RISEx » : signer du bot, 90 jours, limité au trading perp.
// 2 signatures (enregistrement + permission Perps), 3 au renouvellement
// (révocation de l'ancien signer, exécutée après stockage du nouveau).
async function connectRisexWithSignatures({ onProgress } = {}) {
  const progress = typeof onProgress === "function" ? onProgress : () => {};
  const account = _risexAccount();
  if (!isWalletAuthValidForAccount(account)) throw new Error("Connect and sign your wallet first.");
  const ch = await apiPost("/api/risex/signer/challenge", { account_address: account }, { dex: "risex" });
  if (!ch || !ch.register_typed || !ch.enable_typed) {
    throw new Error("Failed to prepare the RISEx signer registration.");
  }
  const renewal = !!ch.revoke_previous_typed;
  const total = renewal ? 3 : 2;
  if (renewal) progress("revoke_prev", "insert");

  progress("register", "active", `Signature 1/${total}`);
  const registerSig = await _risexSignTyped(account, ch.register_typed);
  progress("register", "done");

  progress("enable", "active", `Signature 2/${total}`);
  await _walletSignPause();
  const enableSig = await _risexSignTyped(account, ch.enable_typed);
  progress("enable", "done");

  let revokePrevSig = "";
  if (renewal) {
    progress("revoke_prev", "active", `Signature 3/${total}`);
    await _walletSignPause();
    revokePrevSig = await _risexSignTyped(account, ch.revoke_previous_typed);
    progress("revoke_prev", "done");
  }

  const body = { account_address: account, register_sig: registerSig, enable_sig: enableSig };
  if (renewal) body.revoke_previous_sig = revokePrevSig;
  progress("relay", "active");
  const t0 = Date.now();
  const ticker = window.setInterval(() => progress("relay", "tick", Math.round((Date.now() - t0) / 1000)), 1000);
  let res;
  try {
    res = await apiPost("/api/risex/signer/submit", body, { dex: "risex", timeoutMs: RISEX_SUBMIT_TIMEOUT_MS });
  } catch (e) {
    // Délai dépassé ou 5xx du proxy : le nouveau signer a pu être stocké quand même.
    // Relancer l'étape déclencherait un renouvellement et une transaction de plus.
    if (!_risexMaybeCompleted(e)) throw e;
    const want = String(ch.signer || "").toLowerCase();
    const st = await _risexPollStatus((s) => !!(s && s.signer && _risexSignerReady(s.signer)
      && String(s.signer.address || "").toLowerCase() === want));
    if (!st) throw e;
    res = { ok: true, signer: ch.signer, recovered: true };
  } finally {
    window.clearInterval(ticker);
  }
  progress("relay", "done");
  _risexInvalidateStatus();
  if (res && renewal && res.previous_revoked === false) {
    _msShowToast("New signer stored. The previous signer could not be revoked; it expires on its own.", "info");
  }
  return res;
}

// Bandeau de renouvellement (page principale) : signer RISEx qui expire sous
// 7 jours, expiré ou désactivé. Le bouton rouvre le modal sur « Connect RISEx ».
function renderRisexExpiryBanner() {
  const banner = document.getElementById("risex_expiry_banner");
  if (!banner) return;
  const textEl = document.getElementById("risex_expiry_text");
  const addr = _dexState.risex?.connected ? (_dexState.risex.address || "") : "";
  const signer = addr ? _risexCachedSignerFor(addr) : null;
  if (!signer || !_risexSignerNeedsRenewal(signer)) {
    banner.classList.add("hidden");
    return;
  }
  const days = Number(signer.expires_in_days);
  let text;
  if (!signer.active || !(days > 0)) {
    text = "Your RISEx bot signer has expired or was revoked. Renew it to start new sessions.";
  } else if (days < 1) {
    text = "Your RISEx bot signer expires in less than 24 h. New sessions are refused until you renew it.";
  } else {
    const n = Math.floor(days);
    text = `Your RISEx bot signer expires in ${n} day${n === 1 ? "" : "s"}. Renew it to keep trading.`;
  }
  if (textEl) textEl.textContent = text;
  banner.classList.remove("hidden");
}

function _risexOpenRenewal() {
  _cmOpenForReApproval("risex", {
    forceSteps: [2],
    subtitle: "Renew your RISEx bot signer (2 to 3 wallet signatures, no gas).",
    reason: "Signer expires soon. Click to renew.",
  });
}

// Quota de placements RISEx (par adresse, publié par le bot) : affiché pendant
// une session RISEx, alerte orange sous 1 000 placements.
// Budget de messages QFEX (12 000 unités / 60 s par compte), publié par le bot.
function renderQfexRateBudget(session) {
  const el = document.getElementById("qfex_rate");
  if (!el) return;
  const extras = session && typeof session.adapter_extras === "object" ? session.adapter_extras : null;
  const used = extras ? Number(extras.qfex_rate_units) : NaN;
  const limit = extras ? Number(extras.qfex_rate_limit) : NaN;
  const show = isQfexMode() && !!(session && session.active) && Number.isFinite(used) && Number.isFinite(limit) && limit > 0;
  if (!show) {
    el.classList.add("hidden");
    el.classList.remove("is-low");
    return;
  }
  const pct = Math.min(100, Math.max(0, (used / limit) * 100));
  const valueEl = document.getElementById("qfex_rate_value");
  const detailEl = document.getElementById("qfex_rate_detail");
  if (valueEl) valueEl.textContent = `${pct.toFixed(pct < 10 ? 1 : 0)}%`;
  const high = pct >= 80;
  if (detailEl) {
    const ws = extras.qfex_ws_connected === false ? "Trading connection lost, reconnecting. " : "";
    detailEl.textContent = `${ws}${high ? "Close to the QFEX limit: the bot slows down on its own." : ""}`;
  }
  el.classList.toggle("is-low", high || extras.qfex_ws_connected === false);
  el.classList.remove("hidden");
}

function renderRisexQuota(session) {
  const el = document.getElementById("risex_quota");
  if (!el) return;
  const extras = session && typeof session.adapter_extras === "object" ? session.adapter_extras : null;
  const remaining = extras ? Number(extras.risex_quota_remaining) : NaN;
  const running = !!(session && session.active);
  const show = isRisexMode() && running && extras && extras.risex_quota_remaining != null && Number.isFinite(remaining);
  if (!show) {
    el.classList.add("hidden");
    el.classList.remove("is-low");
    return;
  }
  const earned = Number(extras.risex_quota_earned);
  const valueEl = document.getElementById("risex_quota_value");
  const detailEl = document.getElementById("risex_quota_detail");
  if (valueEl) valueEl.textContent = Math.max(0, Math.round(remaining)).toLocaleString("en-US");
  const low = remaining < RISEX_QUOTA_WARN_THRESHOLD;
  if (detailEl) {
    const earnedTxt = extras.risex_quota_earned != null && Number.isFinite(earned)
      ? `${Math.round(earned).toLocaleString("en-US")} earned back. `
      : "";
    detailEl.textContent = low
      ? `${earnedTxt}Low quota: larger orders earn placements back faster.`
      : earnedTxt;
  }
  el.classList.toggle("is-low", low);
  el.classList.remove("hidden");
}

// Note affichée dans le modal de connexion sous les sous-étapes (texte fixe,
// le lien éventuel est construit en DOM).
function _cmSetNotice(text, link = null, tone = "info") {
  const host = document.getElementById("cm_substeps");
  let note = document.getElementById("cm_notice");
  if (!text) {
    if (note) note.remove();
    return;
  }
  if (!note) {
    note = document.createElement("div");
    note.id = "cm_notice";
    if (host) host.insertAdjacentElement("afterend", note);
    else document.getElementById("connect_modal")?.appendChild(note);
  }
  note.className = `cm-notice cm-notice--${tone}`;
  note.textContent = text;
  if (link && link.href) {
    note.appendChild(document.createTextNode(" "));
    const a = document.createElement("a");
    a.href = link.href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = link.label || link.href;
    note.appendChild(a);
  }
}

function _cmRisexProgress(key, state, detail = "") {
  const host = document.getElementById("cm_substeps");
  if (state === "insert") {
    if (key === "revoke_prev") cmInsertSubstepAfter("enable", { key: "revoke_prev", label: "Sign: revoke the previous signer" });
    return;
  }
  const el = host?.querySelector(`.cm-substep[data-key="${key}"]`);
  const labelEl = el?.querySelector(".cm-substep-label");
  if (labelEl && !labelEl.dataset.baseLabel) labelEl.dataset.baseLabel = labelEl.textContent || "";
  if (state === "tick") {
    if (labelEl) labelEl.textContent = `${labelEl.dataset.baseLabel} (${detail}s)`;
    return;
  }
  if (labelEl) {
    const base = labelEl.dataset.baseLabel || "";
    labelEl.textContent = state === "active" && detail ? `${base} · ${detail}` : base;
  }
  cmSubstepState(key, state, state === "error" ? detail : "");
}

function _cmMarkCurrentSubstepError(e) {
  const host = document.getElementById("cm_substeps");
  const current = host?.querySelector(".cm-substep.is-active") || host?.querySelector(".cm-substep.is-pending");
  if (current) cmSubstepState(current.dataset.key, "error", (e && e.message) ? e.message : String(e));
}

async function _cmRunRisexStep(idx) {
  if (idx === 1) {
    cmSetSubsteps([
      { key: "prepare", label: "Prepare a temporary signer" },
      { key: "register", label: "Sign: register the temporary signer" },
      { key: "revoke", label: "Sign: its revocation, in advance" },
      { key: "submit", label: "Approve the builder fee, then revoke the temporary signer" },
    ]);
    _cmSetNotice(
      "RISEx only lets a full-permission signer approve a builder fee. This page creates a temporary signer that approves the Tradoor fee and is revoked right after; it is never stored. The bot's own signer stays limited to perp trading.",
    );
    try {
      await approveRisexBuilderWithWallet({ onProgress: _cmRisexProgress });
    } catch (e) {
      _cmMarkCurrentSubstepError(e);
      throw e;
    }
    const st = await refreshRisexStatus({ force: true });
    if (!(st && st.builder && st.builder.approved)) {
      throw new Error("Builder fee approval not detected on RISEx yet. Retry in a few seconds.");
    }
    return;
  }
  if (idx === 2) {
    // Statut frais : dépôt présent ? relais configuré ?
    let st = null;
    try { st = await refreshRisexStatus({ force: true }); } catch (_) { st = null; }
    if (st && st.relayer_ok === false) {
      throw new Error("RISEx connection is temporarily unavailable on this server (relayer not configured). Retry later.");
    }
    cmSetSubsteps([
      { key: "register", label: "Sign: register the bot signer (90 days)" },
      { key: "enable", label: "Sign: limit it to perp trading" },
      { key: "relay", label: "Register on RISE, gas paid by Tradoor (up to 2 min)" },
    ]);
    if (st && st.has_collateral === false) {
      _cmSetNotice(
        "No USDC deposit found on this RISEx account yet. Deposit USDC first (required to register the bot signer), then retry:",
        { href: RISEX_DEPOSIT_URL, label: "rise.trade" },
        "warn",
      );
    } else {
      _cmSetNotice("The bot signer can only place and cancel perp orders: it cannot withdraw or transfer funds. Keep this window open until the registration completes.");
    }
    try {
      await connectRisexWithSignatures({ onProgress: _cmRisexProgress });
    } catch (e) {
      _cmMarkCurrentSubstepError(e);
      throw e;
    }
    const after = await refreshRisexStatus({ force: true });
    if (!_risexSignerReady(after && after.signer)) {
      const reason = after && after.signer && after.signer.reason ? ` (${after.signer.reason})` : "";
      throw new Error(`RISEx signer not ready after registration${reason}. Retry in a minute.`);
    }
    return;
  }
}

// ── QFEX : clé API collée (trading sans retrait), vérifiée par le serveur ──────────

const QFEX_STATUS_CACHE_TTL_MS = 60_000;
const QFEX_KEYS_URL = "https://qfex.com";

function _applyQfexStatus(account, st) {
  const ok = !!(st && st.credentials_saved && st.key_valid && st.trade_only);
  qfexCredentialsReadyForAccount = ok;
  const qxAddr = _dexState.qfex?.address || "";
  if (qxAddr && normalizeAccount(qxAddr) === normalizeAccount(account)) _dexState.qfex.credentialsSaved = ok;
  try { updateConnectStepsState(); } catch (_) {}
}

// Lit GET /api/qfex/status (cache 60 s). Lève en cas d'échec : les appelants
// décident (ne jamais sauter une étape sur état incertain).
async function refreshQfexStatus({ force = false, account = "" } = {}) {
  const acct = String(account || document.getElementById("account_address")?.value || "").trim();
  if (!acct.startsWith("0x") || acct.length < 10) {
    qfexCredentialsReadyForAccount = false;
    return null;
  }
  const key = normalizeAccount(acct);
  if (!force && qfexStatusCache.key === key && qfexStatusCache.value && Date.now() - qfexStatusCache.ts < QFEX_STATUS_CACHE_TTL_MS) {
    _applyQfexStatus(acct, qfexStatusCache.value);
    return qfexStatusCache.value;
  }
  const data = await apiGet(`/api/qfex/status?account_address=${encodeURIComponent(acct)}&force=${force ? 1 : 0}`, { dex: "qfex" });
  qfexStatusCache = { key, ts: Date.now(), value: data || null };
  _applyQfexStatus(acct, data || null);
  return data || null;
}

async function removeStoredVenueKey(dex) {
  const acct = String(_dexState[dex]?.address || document.getElementById("account_address")?.value || "").trim();
  if (!acct) return;
  const res = await fetchWithTimeout(`${REMOVE_KEY_ENDPOINTS[dex]}?account_address=${encodeURIComponent(acct)}`, {
    method: "DELETE", headers: authHeaders({}, dex),
  }, API_TIMEOUT_MS);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j && typeof j.detail === "string") msg = j.detail; } catch (_) {}
    throw new Error(msg);
  }
}

async function removeQfexKey() {
  const acct = String(_dexState.qfex?.address || document.getElementById("account_address")?.value || "").trim();
  if (!acct) return;
  const res = await fetchWithTimeout(`/api/qfex/credentials?account_address=${encodeURIComponent(acct)}`, {
    method: "DELETE", headers: authHeaders({}, "qfex"),
  }, API_TIMEOUT_MS);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j && typeof j.detail === "string") msg = j.detail; } catch (_) {}
    throw new Error(msg);
  }
  qfexStatusCache = { key: "", ts: 0, value: null };
  qfexCredentialsReadyForAccount = false;
}

let _cmQfexFormReject = null;

function _cmQfexFormRemove() {
  document.getElementById("cm_qfex_form")?.remove();
}

function _cmQfexFormAbort(message) {
  const reject = _cmQfexFormReject;
  _cmQfexFormReject = null;
  _cmQfexFormRemove();
  if (reject) reject(new Error(message));
}

// Formulaire de l'étape « Connect QFEX ». Résout une fois la clé enregistrée et
// vérifiée par le serveur ; rejette sur « Cancel ». Le secret ne quitte le champ
// que pour le POST, il n'est ni gardé ni journalisé côté page.
function _cmQfexKeyForm(account) {
  _cmQfexFormAbort("QFEX connection restarted.");
  const host = document.getElementById("cm_substeps");
  const form = document.createElement("form");
  form.id = "cm_qfex_form";
  form.className = "cm-qfex-form";
  form.autocomplete = "off";
  form.innerHTML = `
    <ol class="cm-qfex-guide">
      <li>On <a href="${QFEX_KEYS_URL}" target="_blank" rel="noopener noreferrer">qfex.com</a>, open <b>Developer Settings</b> and create an API key.</li>
      <li>Limit it to <b>one account</b>.</li>
      <li>Enable <b>Execute orders</b> and <b>View orders / positions / balance</b>.</li>
      <li>Do <b>not</b> enable <b>Deposit and withdraw</b>: Tradoor refuses keys that can move funds.</li>
    </ol>
    <label class="cm-qfex-field"><span>Public key</span>
      <input id="cm_qfex_pub" type="text" inputmode="text" spellcheck="false" autocapitalize="off" placeholder="qfex_pub_..." required></label>
    <label class="cm-qfex-field"><span>Secret key</span>
      <input id="cm_qfex_secret" type="password" spellcheck="false" autocapitalize="off" autocomplete="new-password" required></label>
    <div class="cm-qfex-error hidden" id="cm_qfex_error" role="alert"></div>
    <div class="cm-qfex-actions">
      <button type="button" class="cm-qfex-cancel" id="cm_qfex_cancel">Cancel</button>
      <button type="submit" class="cm-qfex-save" id="cm_qfex_save">Verify and save</button>
    </div>`;
  if (host) host.insertAdjacentElement("afterend", form);
  else document.getElementById("connect_modal")?.appendChild(form);
  const pubEl = form.querySelector("#cm_qfex_pub");
  const secEl = form.querySelector("#cm_qfex_secret");
  const errEl = form.querySelector("#cm_qfex_error");
  const saveBtn = form.querySelector("#cm_qfex_save");
  setTimeout(() => pubEl?.focus(), 0);
  return new Promise((resolve, reject) => {
    _cmQfexFormReject = reject;
    form.querySelector("#cm_qfex_cancel").addEventListener("click", () => {
      _cmQfexFormAbort("QFEX connection cancelled.");
    });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const publicKey = String(pubEl.value || "").trim();
      const secretKey = String(secEl.value || "").trim();
      errEl.classList.add("hidden");
      if (!/^qfex_pub_[A-Za-z0-9]{8,64}$/.test(publicKey)) {
        errEl.textContent = "The public key should look like qfex_pub_...";
        errEl.classList.remove("hidden");
        return;
      }
      if (secretKey.length < 16) {
        errEl.textContent = "Paste the full secret key shown once when the key was created.";
        errEl.classList.remove("hidden");
        return;
      }
      saveBtn.disabled = true;
      saveBtn.textContent = "Verifying…";
      try {
        await apiPost("/api/qfex/credentials", { account_address: account, public_key: publicKey, secret_key: secretKey },
          { dex: "qfex", timeoutMs: 45_000 });
        secEl.value = "";
        _cmQfexFormReject = null;
        _cmQfexFormRemove();
        resolve();
      } catch (e) {
        errEl.textContent = String(e && e.message ? e.message : e);
        errEl.classList.remove("hidden");
        saveBtn.disabled = false;
        saveBtn.textContent = "Verify and save";
      }
    });
  });
}

async function _cmRunQfexStep(idx) {
  if (idx !== 1) return;
  const account = String(_dexState.qfex?.address || document.getElementById("account_address")?.value || "").trim();
  let st = null;
  try { st = await refreshQfexStatus({ force: true, account }); } catch (_) { st = null; }
  if (st && st.credentials_saved && st.key_valid && st.trade_only) return;
  if (st && st.builder_code_configured === false) {
    throw new Error("QFEX is not available on this server yet. Retry later.");
  }
  if (st && st.credentials_saved && st.error) {
    _cmSetNotice(`Your saved QFEX key no longer works: ${st.error} Paste a new one.`, null, "warn");
  } else {
    _cmSetNotice("Tradoor only stores trade-only keys, encrypted. QFEX attributes your volume to Tradoor; your trading fees stay the same.");
  }
  await _cmQfexKeyForm(account);
  _cmSetNotice("");
  const after = await refreshQfexStatus({ force: true, account });
  if (!(after && after.credentials_saved && after.key_valid && after.trade_only)) {
    throw new Error(after && after.error ? after.error : "QFEX key not verified yet. Retry.");
  }
}

async function _setupDexExtended() {
  await refreshExtendedCredentialsStatus({ force: true });
  if (!extendedCredentialsReadyForAccount) {
    await connectExtendedWithSignatures();
    await refreshExtendedCredentialsStatus({ force: true });
  }
  if (!extendedCredentialsReadyForAccount) {
    throw new Error("Extended connect did not complete. Please retry.");
  }
  const modeSelect = document.getElementById("mode");
  const prevMode = modeSelect?.value;
  if (modeSelect) modeSelect.value = "extended_mainnet";
  try {
    const builderStatus = await refreshBuilderStatus({ force: true });
    if (!builderStatus.approved) {
      await approveBuilderFeeWithWallet();
      await refreshBuilderStatus({ force: true });
    }
  } finally {
    if (modeSelect && prevMode) modeSelect.value = prevMode;
  }
  _dexState.extended.connected = true;
  _dexState.extended.address = _dexState.extended.address || walletAuthAccount;
  _dexState.extended.credentialsSaved = !!extendedCredentialsReadyForAccount;
  _dexState.extended.builderApproved = !!builderApprovedForAccount;
}

async function _setupDexNado() {
  await refreshNadoAgentStatus({ force: true });
  if (!nadoAgentApprovedForAccount) {
    await approveNadoAgentWithWallet();
    await refreshNadoAgentStatus({ force: true });
  }
  _dexState.nado.connected = true;
  _dexState.nado.address = _dexState.nado.address || walletAuthAccount;
  _dexState.nado.agentApproved = !!nadoAgentApprovedForAccount;
}

// Force-refresh on-chain approvals for each connected DEX. Returns false as
// soon as one is missing - used to block auto-reconnect when the user revoked
// builder fee / agent externally (e.g. from the Hyperliquid UI).
async function _allApprovalsValidForConnectedDexes(connected) {
  const modeSelect = document.getElementById("mode");
  const prevMode = modeSelect?.value || "";
  try {
    for (const dex of connected) {
      if (modeSelect) {
        if (dex === "hyperliquid") modeSelect.value = "hyperliquid_mainnet";
        else if (dex === "extended") modeSelect.value = "extended_mainnet";
        else if (dex === "nado") modeSelect.value = "nado_mainnet";
        else if (dex === "risex") modeSelect.value = "risex_mainnet";
        else if (dex === "qfex") modeSelect.value = "qfex_mainnet";
      }
      if (dex === "hyperliquid") {
        const b = await refreshBuilderStatus({ force: true });
        if (!b || !b.approved) return false;
        const a = await refreshAgentStatus({ force: true });
        if (!a || !a.approved) return false;
      } else if (dex === "extended") {
        await refreshExtendedCredentialsStatus({ force: true });
        if (!extendedCredentialsReadyForAccount) return false;
        const b = await refreshBuilderStatus({ force: true });
        if (!b || !b.approved) return false;
      } else if (dex === "nado") {
        await refreshNadoAgentStatus({ force: true });
        if (!nadoAgentApprovedForAccount) return false;
      } else if (dex === "risex") {
        // Builder approuvé chez RISEx et signer du bot prêt (> 24 h). Un signer qui
        // expire sous 7 jours reste valide ici : le bandeau propose le renouvellement.
        const st = await refreshRisexStatus({ force: true, account: _dexState.risex?.address || "" });
        if (!st || !(st.builder && st.builder.approved)) return false;
        if (!_risexSignerReady(st.signer)) return false;
      } else if (dex === "qfex") {
        const st = await refreshQfexStatus({ force: true, account: _dexState.qfex?.address || "" });
        if (!st || !(st.credentials_saved && st.key_valid && st.trade_only)) return false;
      }
    }
    return true;
  } finally {
    if (modeSelect && prevMode) modeSelect.value = prevMode;
  }
}

// Same check for the legacy single-mode fallback path.
async function _allApprovalsValidForSavedMode(savedMode) {
  if (isExtendedMode(savedMode)) {
    await refreshExtendedCredentialsStatus({ force: true });
    if (!extendedCredentialsReadyForAccount) return false;
    const b = await refreshBuilderStatus({ force: true });
    return !!(b && b.approved);
  }
  if (isNadoMode(savedMode)) {
    await refreshNadoAgentStatus({ force: true });
    return !!nadoAgentApprovedForAccount;
  }
  // Hyperliquid / HIP-3
  const b = await refreshBuilderStatus({ force: true });
  if (!b || !b.approved) return false;
  const a = await refreshAgentStatus({ force: true });
  return !!(a && a.approved);
}

// ── Sous-étapes de l'étape 3 (Connect Extended) ────────────────────────────
function cmSetSubsteps(defs) {
  const host = document.getElementById("cm_substeps");
  if (!host) return;
  host.innerHTML = "";
  if (!Array.isArray(defs) || defs.length === 0) {
    host.classList.add("hidden");
    return;
  }
  for (const def of defs) host.appendChild(_cmBuildSubstepEl(def));
  host.classList.remove("hidden");
}

function _cmBuildSubstepEl(def) {
  const el = document.createElement("div");
  el.className = "cm-substep is-pending";
  el.dataset.key = String(def.key || "");
  el.innerHTML =
    '<span class="cm-substep-icon" aria-hidden="true">' +
      '<span class="cm-substep-spinner"></span>' +
      '<svg class="cm-substep-check" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>' +
      '<svg class="cm-substep-cross" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>' +
    "</span>" +
    '<span class="cm-substep-label"></span>' +
    '<span class="cm-substep-detail" role="alert"></span>';
  el.querySelector(".cm-substep-label").textContent = String(def.label || "");
  return el;
}

function cmSubstepState(key, state, detail = "") {
  const host = document.getElementById("cm_substeps");
  const el = host?.querySelector(`.cm-substep[data-key="${key}"]`);
  if (!el) return;
  el.classList.remove("is-pending", "is-active", "is-done", "is-error");
  el.classList.add("is-" + state);
  const detailEl = el.querySelector(".cm-substep-detail");
  if (detailEl) detailEl.textContent = state === "error" ? String(detail || "") : "";
}

function cmInsertSubstepAfter(afterKey, def) {
  const host = document.getElementById("cm_substeps");
  if (!host) return;
  if (host.querySelector(`.cm-substep[data-key="${def.key}"]`)) return;
  const after = host.querySelector(`.cm-substep[data-key="${afterKey}"]`);
  const el = _cmBuildSubstepEl(def);
  if (after && after.nextSibling) host.insertBefore(el, after.nextSibling);
  else host.appendChild(el);
}

// ── Connection modal ─────────────────────────────────────────────────────
// Orange `rgb(255,140,60)` active, green done, red error. Non-dismissible
// during flow; close button shown only on error. Auto-closes on success.
const CM_TOTAL_STEPS = 3;
const _connectModalState = {
  open: false,
  currentDex: "",
  currentStep: -1,       // 0..2 while an action is running
  flowRunning: false,    // true = runSetupAllSignatures in charge; click-to-retry disabled for other steps
  stepStates: ["pending", "pending", "pending"],
  closeTimer: null,
};

function _cmDexLabels(dex) {
  if (dex === "extended") return ["Connect wallet", "Approve builder fee", "Connect Extended"];
  if (dex === "nado")     return ["Connect wallet", "Approve Nado agent", "-"];
  if (dex === "risex")    return ["Connect wallet", "Approve builder fee", "Connect RISEx"];
  if (dex === "qfex")     return ["Connect wallet", "Connect QFEX", "-"];
  // hyperliquid + hip-3
  return ["Connect wallet", "Approve builder fee", "Approve TRADOR agent"];
}

function _cmStepAlreadyDone(dex, idx) {
  const account = document.getElementById("account_address")?.value?.trim() || "";
  // Étape 0 : seulement si CETTE venue a déjà son adresse. Après un Disconnect,
  // #account_address retombe sur le dernier wallet authentifié du navigateur (un
  // autre compte) : sauter l'étape faisait signer pour ce compte-là au lieu du
  // compte actif du wallet.
  if (idx === 0) {
    const own = _dexState[dex]?.connected ? (_dexState[dex].address || "") : "";
    return !!own && isWalletAuthValidForAccount(own);
  }
  if (dex === "hyperliquid") {
    if (idx === 1) return !!builderApprovedForAccount;
    if (idx === 2) return !!agentApprovedForAccount;
  }
  if (dex === "extended") {
    if (idx === 1) return !!builderApprovedForAccount;
    if (idx === 2) return !!extendedCredentialsReadyForAccount;
  }
  if (dex === "nado") {
    if (idx === 1) return !!nadoAgentApprovedForAccount;
    if (idx === 2) return true;
  }
  if (dex === "risex") {
    // Flags lus sur /api/risex/status : builder approuvé chez RISEx ; signer
    // présent, actif, restreint et valable encore plus de 24 h.
    if (idx === 1) return !!risexBuilderApprovedForAccount;
    if (idx === 2) return !!risexSignerReadyForAccount;
  }
  if (dex === "qfex") {
    // Clé enregistrée, valide et sans droit de retrait (lu sur /api/qfex/status).
    if (idx === 1) return !!qfexCredentialsReadyForAccount;
    if (idx === 2) return true;
  }
  return false;
}

function _dexToMode(dex) {
  if (dex === "extended") return "extended_mainnet";
  if (dex === "nado") return "nado_mainnet";
  if (dex === "risex") return "risex_mainnet";
  if (dex === "qfex") return "qfex_mainnet";
  return "hyperliquid_mainnet";
}

// After wallet sign, force-refresh on-chain approvals so already-approved
// steps can be auto-skipped (no wallet re-sign for builder/agent if they're
// still valid on-chain). Note: we assign modeSelect.value directly (no
// dispatchEvent) to silently refresh - the `change` listeners on #mode have
// heavy side-effects (markets reload, UI updates) and we don't want them here.
async function _cmRefreshApprovalsForDex(dex) {
  const modeSelect = document.getElementById("mode");
  const prevMode = modeSelect ? modeSelect.value : "";
  try {
    if (modeSelect) modeSelect.value = _dexToMode(dex);
    if (dex === "hyperliquid") {
      await Promise.all([
        refreshBuilderStatus({ force: true }),
        refreshAgentStatus({ force: true }),
      ]);
    } else if (dex === "extended") {
      await Promise.all([
        refreshExtendedCredentialsStatus({ force: true }),
        refreshBuilderStatus({ force: true }),
      ]);
    } else if (dex === "nado") {
      await refreshNadoAgentStatus({ force: true });
    } else if (dex === "risex") {
      await refreshRisexStatus({ force: true });
    } else if (dex === "qfex") {
      await refreshQfexStatus({ force: true });
    }
  } finally {
    if (modeSelect) modeSelect.value = prevMode;
  }
}

async function _cmRunStepAction(dex, idx) {
  // Toutes les actions d'étape lisent le #mode ambiant (approveBuilderFeeWithWallet,
  // refreshBuilderStatus, refreshExtendedCredentialsStatus…). On synchronise ici pour
  // que TOUTE exécution (flux complet, retry par clic, ré-approbation) vise bien `dex`.
  _syncDexDomState(dex);
  if (idx === 0) {
    await connectAndSignWalletForDex(dex);
    _syncDexDomState(dex);
    await refreshWalletAuthStatus({ force: true });
    return;
  }
  if (dex === "hyperliquid") {
    if (idx === 1) {
      await approveBuilderFeeWithWallet();
      const st = await refreshBuilderStatus({ force: true });
      if (!st || !st.approved) {
        throw new Error(st && st.message ? st.message : "Builder fee approval not detected on-chain.");
      }
      return;
    }
    if (idx === 2) {
      await approveAgentWithWallet();
      const st = await refreshAgentStatus({ force: true });
      if (!st || !st.approved) {
        throw new Error(st && st.message ? st.message : "Agent approval not detected on-chain.");
      }
      return;
    }
  }
  if (dex === "extended") {
    if (idx === 1) {
      await approveBuilderFeeWithWallet();
      const st = await refreshBuilderStatus({ force: true });
      if (!st || !st.approved) {
        throw new Error(st && st.message ? st.message : "Builder fee approval not detected.");
      }
      return;
    }
    if (idx === 2) {
      cmSetSubsteps([
        { key: "derive", label: "Derive trading keys" },
        { key: "verify", label: "Verify Extended account" },
        { key: "apikey", label: "Create API key" },
        { key: "save", label: "Saved" },
      ]);
      try {
        await connectExtendedWithSignatures({ onProgress: _cmExtendedProgress });
      } catch (e) {
        // Marque la première sous-étape encore active/pending en erreur.
        const host = document.getElementById("cm_substeps");
        const current = host?.querySelector(".cm-substep.is-active") || host?.querySelector(".cm-substep.is-pending");
        if (current) cmSubstepState(current.dataset.key, "error", (e && e.message) ? e.message : String(e));
        throw e;
      }
      await refreshExtendedCredentialsStatus({ force: true });
      if (!extendedCredentialsReadyForAccount) {
        throw new Error("Extended credentials not saved after connect.");
      }
      return;
    }
  }
  if (dex === "nado") {
    if (idx === 1) {
      await approveNadoAgentWithWallet();
      await refreshNadoAgentStatus({ force: true });
      if (!nadoAgentApprovedForAccount) {
        throw new Error("Nado linked signer approval not detected on-chain.");
      }
      return;
    }
    if (idx === 2) return; // no-op
  }
  if (dex === "qfex" && (idx === 1 || idx === 2)) {
    await _cmRunQfexStep(idx);
    return;
  }
  if (dex === "risex" && (idx === 1 || idx === 2)) {
    await _cmRunRisexStep(idx);
    return;
  }
  throw new Error(`No action for dex=${dex} step=${idx}`);
}

function _cmExtendedProgress(key, state, detail = "") {
  if (key === "onboard" && state === "active") {
    cmInsertSubstepAfter("verify", { key: "onboard", label: "Create Extended account" });
  }
  cmSubstepState(key, state, detail);
}

function _cmStepEl(idx) {
  return document.getElementById(`cm_step_${idx + 1}`);
}
function _cmConnectorEl(idx) {
  // connector BEFORE step idx+1 (i.e. between idx and idx+1)
  return document.getElementById(`cm_conn_${idx + 1}${idx + 2}`);
}

function _cmUpdateProgress() {
  const fill = document.getElementById("connect_modal_progress_fill");
  const total = CM_TOTAL_STEPS;
  const done = _connectModalState.stepStates.filter((s) => s === "done").length;
  const active = _connectModalState.stepStates.some((s) => s === "active") ? 0.35 : 0;
  const frac = Math.min(1, (done + active) / total);
  if (fill) fill.style.width = `${frac * 100}%`;
  const hasError = _connectModalState.stepStates.some((s) => s === "error");
  document.getElementById("connect_modal")?.classList.toggle("is-error", hasError);
}

function setConnectModalStep(idx, { state = "pending", label, error = "" } = {}) {
  const el = _cmStepEl(idx);
  if (!el) return;
  _connectModalState.stepStates[idx] = state;
  el.classList.remove("is-pending", "is-active", "is-done", "is-error");
  el.classList.add(`is-${state}`);
  const labelEl = el.querySelector(".cm-step-label");
  if (labelEl && typeof label === "string") labelEl.textContent = label;
  const stateEl = el.querySelector(".cm-step-state");
  if (stateEl) {
    stateEl.textContent =
      state === "done" ? "Done" :
      state === "active" ? "In progress" :
      state === "error" ? "Error" : "Pending";
  }
  const errorEl = el.querySelector(".cm-step-error");
  if (errorEl) errorEl.textContent = error || "";
  // Connector BEFORE this step (idx-1 → idx) fills when prev step is done.
  if (idx > 0) {
    const conn = _cmConnectorEl(idx - 1);
    if (conn) conn.classList.toggle("is-filled", _connectModalState.stepStates[idx - 1] === "done");
  }
  // Close btn: always available so the user can bail out at any time.
  document.getElementById("connect_modal_close")?.classList.remove("hidden");
  _cmUpdateProgress();
  // Auto-close when every step is done (covers direct-click paths that
  // don't go through runSetupAllSignatures).
  const allDone = _connectModalState.stepStates.every((s) => s === "done");
  if (allDone && _connectModalState.open && !_connectModalState.flowRunning) {
    closeConnectModal({ delay: 420 });
  }
}

function setConnectModalDexContext(dex, { resetSteps = true, subtitle = "" } = {}) {
  _connectModalState.currentDex = dex;
  const labels = _cmDexLabels(dex);
  for (let i = 0; i < CM_TOTAL_STEPS; i++) {
    if (resetSteps) _connectModalState.stepStates[i] = "pending";
    const el = _cmStepEl(i);
    if (!el) continue;
    const labelEl = el.querySelector(".cm-step-label");
    if (labelEl) labelEl.textContent = labels[i] || "";
    if (resetSteps) {
      el.classList.remove("is-active", "is-done", "is-error");
      el.classList.add("is-pending");
      const stateEl = el.querySelector(".cm-step-state");
      if (stateEl) stateEl.textContent = "Pending";
      const errorEl = el.querySelector(".cm-step-error");
      if (errorEl) errorEl.textContent = "";
    }
  }
  for (let i = 0; i < CM_TOTAL_STEPS - 1; i++) {
    const conn = _cmConnectorEl(i);
    if (conn) conn.classList.toggle("is-filled", _connectModalState.stepStates[i] === "done");
  }
  const dexLabel = DEX_LABELS[dex] || "";
  const dexEl = document.getElementById("connect_modal_current_dex");
  if (dexEl) dexEl.textContent = dexLabel ? `Configuring ${dexLabel}` : "";
  if (subtitle) {
    const subEl = document.getElementById("connect_modal_subtitle");
    if (subEl) subEl.textContent = subtitle;
  }
  _cmUpdateProgress();
  if (resetSteps) {
    cmSetSubsteps([]);
    _cmSetNotice("");
  }
}

function openConnectModal(dex = "", { subtitle = "Signing with your wallet" } = {}) {
  if (_connectModalState.closeTimer) {
    window.clearTimeout(_connectModalState.closeTimer);
    _connectModalState.closeTimer = null;
  }
  _connectModalState.open = true;
  const overlay = document.getElementById("connect_modal_overlay");
  const modal = document.getElementById("connect_modal");
  if (overlay) { overlay.classList.remove("hidden"); overlay.setAttribute("aria-hidden", "false"); }
  if (modal) { modal.classList.remove("hidden"); modal.setAttribute("aria-hidden", "false"); modal.classList.remove("is-error"); }
  document.body.classList.add("connect-modal-active");
  setConnectModalDexContext(dex || _activeDex || "hyperliquid", { resetSteps: true, subtitle });
  // Close button always available - user can bail out whenever.
  document.getElementById("connect_modal_close")?.classList.remove("hidden");
}

function closeConnectModal({ delay = 0 } = {}) {
  const doClose = () => {
    // Formulaire de clé QFEX en attente : le rejeter libère le flux de connexion
    // (sinon _connectSingleDexInFlight reste vrai jusqu'au rechargement).
    _cmQfexFormAbort("QFEX connection cancelled.");
    _connectModalState.closeTimer = null;
    _connectModalState.open = false;
    _connectModalState.currentStep = -1;
    _connectModalState.flowRunning = false;
    const overlay = document.getElementById("connect_modal_overlay");
    const modal = document.getElementById("connect_modal");
    if (overlay) { overlay.classList.add("hidden"); overlay.setAttribute("aria-hidden", "true"); }
    if (modal) { modal.classList.add("hidden"); modal.setAttribute("aria-hidden", "true"); }
    document.body.classList.remove("connect-modal-active");
  };
  if (_connectModalState.closeTimer) {
    window.clearTimeout(_connectModalState.closeTimer);
    _connectModalState.closeTimer = null;
  }
  if (delay > 0) {
    _connectModalState.closeTimer = window.setTimeout(doClose, delay);
  } else {
    doClose();
  }
}

// Run a specific step in the modal for the current dex. Updates UI + returns true on success.
async function _cmRunStep(idx, { force = false } = {}) {
  const dex = _connectModalState.currentDex || _activeDex || "hyperliquid";
  if (!force && _cmStepAlreadyDone(dex, idx)) {
    setConnectModalStep(idx, { state: "done" });
    return true;
  }
  setConnectModalStep(idx, { state: "active" });
  _connectModalState.currentStep = idx;
  try {
    await _cmRunStepAction(dex, idx);
    setConnectModalStep(idx, { state: "done" });
    return true;
  } catch (e) {
    const msg = (e && e.message) ? e.message : String(e);
    setConnectModalStep(idx, { state: "error", error: msg });
    return false;
  } finally {
    _connectModalState.currentStep = -1;
  }
}

// Open the modal for a re-approval flow (auto-reconnect detected missing approvals).
// Pre-marks already-valid steps as done and surfaces the missing one as active+error.
// Forces a fresh on-chain refresh first so the pending/done split reflects reality.
// `forceSteps` : étapes à refaire même si elles sont valides (renouvellement du
// signer RISEx avant son expiration), avec `reason` comme message d'étape.
async function _cmOpenForReApproval(dex, { forceSteps = [], subtitle = "", reason = "" } = {}) {
  _cmBindHandlers();
  const forced = new Set(Array.isArray(forceSteps) ? forceSteps : []);
  openConnectModal(dex, { subtitle: subtitle || "Re-approval required - one or more approvals were revoked on-chain." });
  // Le refresh et _cmStepAlreadyDone lisent le #mode / #account_address ambiants :
  // viser `dex` avant, puis restaurer la venue active (les actions d'étape
  // resynchronisent elles-mêmes via _cmRunStepAction).
  _syncDexDomState(dex);
  try { await _cmRefreshApprovalsForDex(dex); } catch (_) {}
  const labels = _cmDexLabels(dex);
  const missing = [];
  for (let i = 0; i < CM_TOTAL_STEPS; i++) {
    if (!forced.has(i) && _cmStepAlreadyDone(dex, i)) {
      setConnectModalStep(i, { state: "done", label: labels[i] });
    } else {
      missing.push(i);
    }
  }
  if (dex !== _activeDex) _syncDexDomState(_activeDex);
  if (missing.length === 0) {
    closeConnectModal({ delay: 300 });
    return;
  }
  const firstMissing = missing[0];
  setConnectModalStep(firstMissing, {
    state: "error",
    label: labels[firstMissing],
    error: forced.has(firstMissing) && reason ? reason : "Revoked on-chain. Click to re-approve.",
  });
  for (const i of missing.slice(1)) {
    setConnectModalStep(i, { state: "pending", label: labels[i] });
  }
}

// Click-to-run for any step (retry or direct jump).
async function _cmOnStepClick(idx) {
  if (_connectModalState.currentStep >= 0) return; // an action is already in flight
  if (_connectModalState.flowRunning) return;      // runSetupAllSignatures owns the flow
  const dex = _connectModalState.currentDex || _activeDex;
  await _cmRunStep(idx, { force: true });
  // L'action a synchronisé #mode / #account_address sur `dex` : revenir à la venue
  // active (ex. renouvellement RISEx lancé depuis le bandeau pendant une session HL).
  if (dex && dex !== _activeDex) _syncDexDomState(_activeDex);
}

function _cmBindHandlers() {
  for (let i = 0; i < CM_TOTAL_STEPS; i++) {
    const el = _cmStepEl(i);
    if (!el || el.dataset.cmBound === "1") continue;
    el.dataset.cmBound = "1";
    el.addEventListener("click", () => _cmOnStepClick(i));
  }
  const closeBtn = document.getElementById("connect_modal_close");
  if (closeBtn && closeBtn.dataset.cmBound !== "1") {
    closeBtn.dataset.cmBound = "1";
    closeBtn.addEventListener("click", () => closeConnectModal());
  }
}

// ── Choix de venue avant connexion ──────────────────────────────────────────
// Affiche la popup Hyperliquid / Extended et résout avec la venue choisie
// (null si fermée). La venue choisie devient la sélection active.
function chooseVenue() {
  return new Promise((resolve) => {
    const overlay = document.getElementById("venue_choice_overlay");
    const modal = document.getElementById("venue_choice_modal");
    if (!overlay || !modal) {
      resolve("hyperliquid");
      return;
    }
    const done = (value) => {
      overlay.classList.add("hidden");
      overlay.setAttribute("aria-hidden", "true");
      modal.classList.add("hidden");
      modal.setAttribute("aria-hidden", "true");
      document.removeEventListener("keydown", onKey);
      resolve(value);
    };
    const onKey = (e) => {
      if (e.key === "Escape") done(null);
    };
    overlay.classList.remove("hidden");
    overlay.setAttribute("aria-hidden", "false");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    modal.querySelectorAll(".venue-choice-card").forEach((btn) => {
      btn.onclick = () => done(btn.dataset.dex || null);
    });
    overlay.onclick = () => done(null);
    document.addEventListener("keydown", onKey);
  });
}

// Popup de choix + application de la sélection. Résout true si une venue a été
// choisie (la connexion peut continuer), false si l'utilisateur a fermé.
async function chooseVenueAndSelect() {
  const venue = await chooseVenue();
  if (!venue) return false;
  _selectedDexes = new Set([venue]);
  _activeDex = venue;
  persistDexState();
  updateDexSelectorUI();
  return true;
}

async function runSetupAllSignatures() {
  if (setupAllInFlight || Date.now() < setupAllLockedUntilMs) return false;
  setupAllInFlight = true;
  setupAllLockedUntilMs = Date.now() + 200;
  _cmBindHandlers();
  clearError("connect_error");
  let flowOk = true;
  try {
    const dexes = [..._selectedDexes];
    if (dexes.length === 0) return false;
    // Trigger the EIP-6963 wallet picker BEFORE opening the modal,
    // so the picker appears first and the modal only shows once a wallet is chosen.
    try {
      await _getWalletProvider();
    } catch (e) {
      showError("connect_error", e && e.message ? e.message : String(e));
      return false;
    }
    _connectModalState.flowRunning = true;
    openConnectModal(dexes[0]);
    for (const dex of dexes) {
      setConnectModalDexContext(dex, { resetSteps: true });
      // Step 1: wallet signature
      const s1 = await _cmRunStep(0);
      if (!s1) { flowOk = false; break; }
      _syncDexDomState(dex);
      // Mark the connected dex state for subsequent sync
      if (dex === "hyperliquid") _dexState.hyperliquid.address = _dexState.hyperliquid.address || walletAuthAccount;
      if (dex === "extended")    _dexState.extended.address    = _dexState.extended.address    || walletAuthAccount;
      if (dex === "nado")        _dexState.nado.address        = _dexState.nado.address        || walletAuthAccount;
      if (dex === "risex")       _dexState.risex.address       = _dexState.risex.address       || walletAuthAccount;
      if (dex === "qfex")        _dexState.qfex.address        = _dexState.qfex.address        || walletAuthAccount;
      // Re-check on-chain approvals before prompting - skip wallet re-sign if already approved.
      try {
      await _cmRefreshApprovalsForDex(dex);
    } catch (_) {
      // Échec du refresh on-chain : ne pas faire confiance au cache. Remettre les
      // flags à false force la ré-exécution des étapes (pas de skip sur état incertain).
      builderApprovedForAccount = false;
      agentApprovedForAccount = false;
      extendedCredentialsReadyForAccount = false;
      nadoAgentApprovedForAccount = false;
      risexBuilderApprovedForAccount = false;
      risexSignerReadyForAccount = false;
      qfexCredentialsReadyForAccount = false;
    }

      // Step 2
      const s2 = await _cmRunStep(1);
      if (!s2) { flowOk = false; break; }

      // Step 3 (noop for Nado and QFEX)
      if (dex === "nado" || dex === "qfex") {
        setConnectModalStep(2, { state: "done" });
      } else {
        const s3 = await _cmRunStep(2);
        if (!s3) { flowOk = false; break; }
      }

      // Mark dex as connected in memory (flags = vrai statut on-chain, pas true hardcodé)
      if (dex === "hyperliquid") {
        _dexState.hyperliquid.connected = true;
        _dexState.hyperliquid.builderApproved = !!builderApprovedForAccount;
        _dexState.hyperliquid.agentApproved = !!agentApprovedForAccount;
      } else if (dex === "extended") {
        _dexState.extended.connected = true;
        _dexState.extended.credentialsSaved = !!extendedCredentialsReadyForAccount;
        _dexState.extended.builderApproved = !!builderApprovedForAccount;
      } else if (dex === "nado") {
        _dexState.nado.connected = true;
        _dexState.nado.agentApproved = !!nadoAgentApprovedForAccount;
      } else if (dex === "risex") {
        _dexState.risex.connected = true;
        _dexState.risex.builderApproved = !!risexBuilderApprovedForAccount;
        _dexState.risex.signerReady = !!risexSignerReadyForAccount;
      } else if (dex === "qfex") {
        _dexState.qfex.connected = true;
        _dexState.qfex.credentialsSaved = !!qfexCredentialsReadyForAccount;
      }
      persistDexState();
    }

    if (!flowOk) return false;

    if (dexes.length > 0) _activeDex = dexes[0];
    persistDexState();
    await refreshWalletNetworkBadge();

    // Persist config per connected DEX slot
    for (const dex of dexes) {
      _syncDexDomState(dex);
      try {
        const toml = buildToml({ allowDefaults: true, slot: dexSlotNumber(dex) });
        await apiPost("/api/config?slot=" + dexSlotNumber(dex), { toml });
      } catch (_) {}
    }

    _syncDexDomState(_activeDex);
    closeConnectModal({ delay: 420 });
    return true;
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    showError("connect_error", `Connection failed: ${msg}`);
    _syncDexDomState(_activeDex);
    return false;
  } finally {
    _connectModalState.flowRunning = false;
    const restore = () => {
      setupAllInFlight = false;
      updateSetupAllButtonState();
      updateConnectStepsState();
    };
    const waitMs = Math.max(0, setupAllLockedUntilMs - Date.now());
    if (waitMs > 0) {
      window.setTimeout(restore, waitMs);
    } else {
      restore();
    }
  }
}

function parseNum(id) {
  const v = parseFloat(document.getElementById(id).value);
  return Number.isFinite(v) ? v : NaN;
}

function syncNotionalToMaxPos(force = false) {
  const notionalInput = document.getElementById("session_notional");
  if (!notionalInput) return;
  const n = parseFloat(notionalInput.value);
  if (!Number.isFinite(n) || n <= 0) return;
  // Size defaults are still derived from session notional in buildToml()
  // unless Advanced MM Profile is enabled.
}

function validateConnectionFields() {
  const mode = document.getElementById("mode").value;
  const account = document.getElementById("account_address").value.trim();
  if (!mode) return "Please choose a valid mode.";
  if (!account) return "Please connect your wallet first.";
  if (!account.startsWith("0x") || account.length < 10) return "Invalid account address.";
  if (!isWalletAuthValidForAccount(account)) return "Please connect and sign your wallet first.";
  if (isExtendedMode(mode)) {
    if (!builderApprovedForAccount) {
      console.warn("[validate] Extended builder not approved:", { mode, account, builderApprovedForAccount, cache: builderStatusCache });
      return "Please complete Signature 2/3 (Approve Builder Fee).";
    }
    if (!extendedCredentialsReadyForAccount) {
      return "Please connect Extended (wallet signatures) before continuing.";
    }
    return "";
  }
  if (isNadoMode(mode)) {
    if (!nadoAgentApprovedForAccount) return "Please complete Nado agent approval (Linked Signer).";
    return "";
  }
  if (isQfexMode(mode)) {
    if (!qfexCredentialsReadyForAccount) return "Please connect QFEX (paste a trade-only API key) before continuing.";
    return "";
  }
  if (isRisexMode(mode)) {
    if (!risexBuilderApprovedForAccount) return "Please complete the \"Approve builder fee\" step for RISEx.";
    if (!risexSignerReadyForAccount) {
      return "RISEx bot signer missing, expired or expiring within 24 h. Complete the \"Connect RISEx\" step.";
    }
    return "";
  }
  if (!builderApprovedForAccount) {
    console.warn("[validate] HL builder not approved:", { mode, account, builderApprovedForAccount, cache: builderStatusCache });
    return "Please complete Signature 2/3 (Approve Builder Fee).";
  }
  if (!agentApprovedForAccount) {
    console.warn("[validate] HL agent not approved:", { mode, account, agentApprovedForAccount, cache: agentStatusCache });
    return "Please complete Signature 3/3 (Approve TRADOR Agent).";
  }
  return "";
}

function validateStrategyFields() {
  const symbol = document.getElementById("market_symbol").value;
  if (!symbol) return "Please choose a token before starting.";
  const sessionNotional = parseNum("session_notional");
  const maxLoss = parseNum("max_loss");
  const targetVol = parseNum("target_vol");
  // Extended (bêta) : bornes prudentes le temps de roder la venue.
  const extendedBeta = isExtendedMode(document.getElementById("mode")?.value || "");
  if (extendedBeta) {
    if (!(sessionNotional >= 20)) return "Minimum session notional on Extended (beta) is $20.";
    if (sessionNotional > 500) return "Maximum session notional on Extended (beta) is $500.";
  } else if (!(sessionNotional >= 10)) {
    return "Minimum session notional is $10.";
  }
  if (Number.isFinite(latestAvailableNotional) && sessionNotional > latestAvailableNotional) {
    return "Session notional cannot exceed available notional.";
  }
  // maxLoss is optional : if empty, will use dynamic value from notional
  if (!(targetVol > 0)) return "Target session volume must be greater than 0.";
  if (isRisexMode(document.getElementById("mode")?.value || "")) {
    // RISEx : chaque ordre consomme un placement du quota par adresse ; sous le
    // minimum du marché l'ordre est refusé.
    const minUsd = Number(marketMinOrderUsdMap[symbol]);
    const orderMin = _effectiveOrderSizeUsdMin();
    if (Number.isFinite(minUsd) && minUsd > 0 && Number.isFinite(orderMin) && orderMin < minUsd) {
      return `Minimum order size on RISEx for ${_slotDisplayToken(symbol)} is about $${Math.ceil(minUsd)} (yours: $${orderMin}). Raise the session notional or the order size. ${RISEX_ORDER_SIZE_HINT}`;
    }
  }
  if (isQfexMode(document.getElementById("mode")?.value || "")) {
    // QFEX : sous la quantité minimale du marché, le bot n'envoie aucun ordre.
    const minUsd = Number(marketMinOrderUsdMap[symbol]);
    const orderMin = _effectiveOrderSizeUsdMin();
    if (Number.isFinite(minUsd) && minUsd > 0 && Number.isFinite(orderMin) && orderMin < minUsd) {
      return `Minimum order size on QFEX for ${_slotDisplayToken(symbol)} is about $${Math.ceil(minUsd)} (yours: $${orderMin}). Raise the session notional or the order size.`;
    }
  }
  if (advancedMmEnabled) {
    const err = validateAdvancedMmConfig(readAdvancedMmConfigFromUi());
    if (err) return `Advanced profile invalid: ${err}`;
  }
  return "";
}

const RISEX_ORDER_SIZE_HINT = "RISEx charges one placement per order from a per-address quota; orders of about $45+ earn their quota back at typical fill rates.";

// Même calcul que buildToml pour `order_size_usd_min` (profil de risque ou profil avancé).
function _effectiveOrderSizeUsdMin() {
  const sessionNotional = parseFloat(document.getElementById("session_notional")?.value || "");
  if (!Number.isFinite(sessionNotional) || sessionNotional <= 0) return NaN;
  const sizing = computeDynamicSizing(sessionNotional, currentSelectedLeverage(), selectedRiskMode);
  let orderMin = sizing.orderMin;
  if (advancedMmEnabled) {
    const cfg = readAdvancedMmConfigFromUi();
    if (!validateAdvancedMmConfig(cfg)) {
      orderMin = sizing.maxPos > 0 ? Math.min(cfg.posMin, sizing.maxPos) : cfg.posMin;
    }
  }
  return orderMin;
}

function tomlValue(v) {
  if (typeof v === "string") return `"${v}"`;
  if (typeof v === "boolean") return v ? "true" : "false";
  return `${v}`;
}

function normalizeSelectedSymbol(rawValue) {
  const raw = String(rawValue || "").trim();
  if (!raw) return "";
  // Préfixe de dex HIP-3 en minuscules (mkts:, km:, xyz:, io:), nom du marché en majuscules.
  const dex = hip3DexOfSymbol(raw);
  if (dex) {
    const tail = raw.split(":").slice(1).join(":");
    return `${dex}:${tail.toUpperCase()}`;
  }
  return raw.toUpperCase();
}

function getRiskPreset(mode) {
  if (mode === "aggressive") return RISK_MODE_PRESETS.aggressive;
  if (mode === "classic") return RISK_MODE_PRESETS.classic;
  return RISK_MODE_PRESETS.conservative;
}

function computeDynamicSizing(notional, leverage, mode) {
  const preset = getRiskPreset(mode);
  const bp = notional * leverage;
  const maxPos = Math.round(bp * preset.maxPosPct);
  // Un ordre ne doit jamais approcher le cap de position : au-delà d'~1/3 du cap,
  // un seul fill sature la position et le bot passe en réduction/halt en boucle
  // (les planchers 25/200 explosaient le ratio sur les petits notionals).
  const orderCap = Math.max(15, Math.floor((maxPos || 0) / 3)) || 25;
  return {
    maxPos,
    orderMin: Math.round(Math.min(Math.max(15, bp * preset.orderMinPct), orderCap)),
    orderMax: Math.round(Math.min(Math.max(25, bp * preset.orderMaxPct), orderCap)),
    maxLoss:  Math.max(30, Math.round(notional * preset.maxLossPct * 100) / 100),
  };
}

function updateDynamicMaxLoss() {
  // Only auto-fill if the user hasn't entered a custom value
  const maxLossInput = document.getElementById("max_loss");
  if (!maxLossInput) return;
  if (maxLossInput.value.trim() !== "") return; // user has a custom value, don't overwrite
}

function applyRiskMode(mode, opts = {}) {
  const force = !!opts.force;
  const silent = !!opts.silent;
  const persist = opts.persist !== false;
  const now = performance.now();
  if (!force && advancedMmEnabled) return false;
  if (!force && now < riskModeSwitchLockedUntil) return false;
  const next = mode === "aggressive" ? "aggressive" : mode === "classic" ? "classic" : "conservative";
  const preset = getRiskPreset(next);
  selectedRiskMode = next;

  const selector = document.getElementById("risk_mode_selector");
  const aggressiveBtn = document.getElementById("mode_aggressive");
  const classicBtn = document.getElementById("mode_classic");
  const conservativeBtn = document.getElementById("mode_conservative");
  if (selector) {
    selector.classList.toggle("mode-aggressive", next === "aggressive");
    selector.classList.toggle("mode-classic", next === "classic");
    selector.classList.toggle("mode-conservative", next === "conservative");
    if (!silent) {
      selector.classList.remove("flash-aggressive", "flash-classic", "flash-conservative");
      void selector.offsetWidth;
      selector.classList.add(preset.flashClass);
    }
  }
  if (aggressiveBtn) aggressiveBtn.classList.toggle("active", next === "aggressive");
  if (classicBtn) classicBtn.classList.toggle("active", next === "classic");
  if (conservativeBtn) conservativeBtn.classList.toggle("active", next === "conservative");

  const targetVolInput = document.getElementById("target_vol");
  updateDynamicMaxLoss();
  if (targetVolInput) targetVolInput.value = _fmtNum(preset.targetVol);
  syncRangeSlidersFromInputs();
  if (persist) {
    persistRiskInputs();
  }
  renderRunningSlots();
  scheduleLiveConfigSync(250);

  riskModeSwitchLockedUntil = now + 170;
  return true;
}

function applyDirectionBias(bias, opts = {}) {
  const silent = !!opts.silent;
  const next = bias === "long" ? "long" : bias === "short" ? "short" : "neutral";
  selectedDirectionBias = next;

  const selector = document.getElementById("direction_bias_selector");
  const longBtn = document.getElementById("bias_long");
  const neutralBtn = document.getElementById("bias_neutral");
  const shortBtn = document.getElementById("bias_short");
  if (selector) {
    selector.classList.toggle("bias-long", next === "long");
    selector.classList.toggle("bias-neutral", next === "neutral");
    selector.classList.toggle("bias-short", next === "short");
    if (!silent) {
      selector.classList.remove("flash-long", "flash-neutral", "flash-short");
      void selector.offsetWidth;
      selector.classList.add("flash-" + next);
    }
  }
  if (longBtn) longBtn.classList.toggle("active", next === "long");
  if (neutralBtn) neutralBtn.classList.toggle("active", next === "neutral");
  if (shortBtn) shortBtn.classList.toggle("active", next === "short");
  const advBias = document.getElementById("advanced_direction_bias");
  if (advBias) advBias.value = next;

  scheduleLiveConfigSync(250);
  return true;
}

function setDirectionBiasLocked(locked) {
  const on = !!locked;
  const selector = document.getElementById("direction_bias_selector");
  const buttons = ["bias_long", "bias_neutral", "bias_short"]
    .map((id) => document.getElementById(id))
    .filter((el) => el instanceof HTMLButtonElement);
  if (selector) {
    selector.classList.toggle("is-locked", on);
    selector.setAttribute("aria-disabled", on ? "true" : "false");
  }
  buttons.forEach((btn) => {
    btn.disabled = on;
    btn.setAttribute("aria-disabled", on ? "true" : "false");
  });
}

function applyQuoteMode(mode, opts = {}) {
  const silent = !!opts.silent;
  const next = ["sniper", "grid", "fortress", "adaptive"].includes(mode) ? mode : "sniper";

  // Force sniper for extended/nado, et RISEx (le bot n'accepte que sniper ; la config
  // l'impose déjà, le sélecteur doit l'afficher).
  const currentSource = document.getElementById("market_source")?.value || "";
  const _qmVenue = document.getElementById("mode")?.value || "";
  const sniperOnlyVenue = isRisexMode(_qmVenue) || isQfexMode(_qmVenue);
  if (next !== "sniper" && (currentSource === "extended" || currentSource === "nado" || sniperOnlyVenue)) {
    if (!silent) applyQuoteMode("sniper", { silent: true });
    return true;
  }

  selectedQuoteMode = next;
  // Clic utilisateur => verrouille la sélection contre le sync auto du poll.
  // Chargement silent (config backend/preset) => relâche le verrou.
  quoteModeUserTouched = !silent;

  const selector = document.getElementById("quote_mode_selector");
  const sniperBtn = document.getElementById("qm_sniper");
  const gridBtn = document.getElementById("qm_grid");
  const fortressBtn = document.getElementById("qm_fortress");
  const adaptiveBtn = document.getElementById("qm_adaptive");
  if (selector) {
    selector.classList.toggle("mode-sniper", next === "sniper");
    selector.classList.toggle("mode-grid", next === "grid");
    selector.classList.toggle("mode-fortress", next === "fortress");
    selector.classList.toggle("mode-adaptive", next === "adaptive");
    if (!silent) {
      selector.classList.remove("flash-sniper", "flash-grid", "flash-fortress", "flash-adaptive");
      void selector.offsetWidth;
      selector.classList.add("flash-" + next);
    }
  }
  if (sniperBtn) sniperBtn.classList.toggle("active", next === "sniper");
  if (gridBtn) gridBtn.classList.toggle("active", next === "grid");
  if (fortressBtn) fortressBtn.classList.toggle("active", next === "fortress");
  if (adaptiveBtn) adaptiveBtn.classList.toggle("active", next === "adaptive");

  scheduleLiveConfigSync(250);
  return true;
}

function setQuoteModeLocked(locked) {
  const on = !!locked;
  const selector = document.getElementById("quote_mode_selector");
  const buttons = ["qm_sniper", "qm_grid", "qm_fortress", "qm_adaptive"]
    .map((id) => document.getElementById(id))
    .filter((el) => el instanceof HTMLButtonElement);
  if (selector) {
    selector.classList.toggle("is-locked", on);
    selector.setAttribute("aria-disabled", on ? "true" : "false");
  }
  buttons.forEach((btn) => {
    btn.disabled = on;
    btn.setAttribute("aria-disabled", on ? "true" : "false");
  });
}

function setQuoteModeSourceLocked(isExtendedOrNado) {
  // Grid/Fortress/Adaptive not supported on extended/nado : lock those options
  const gridBtn = document.getElementById("qm_grid");
  const fortressBtn = document.getElementById("qm_fortress");
  const adaptiveBtn = document.getElementById("qm_adaptive");
  if (gridBtn) {
    gridBtn.disabled = isExtendedOrNado;
    gridBtn.style.opacity = isExtendedOrNado ? "0.3" : "";
  }
  if (fortressBtn) {
    fortressBtn.disabled = isExtendedOrNado;
    fortressBtn.style.opacity = isExtendedOrNado ? "0.3" : "";
  }
  if (adaptiveBtn) {
    adaptiveBtn.disabled = isExtendedOrNado;
    adaptiveBtn.style.opacity = isExtendedOrNado ? "0.3" : "";
  }
  if (isExtendedOrNado && selectedQuoteMode !== "sniper") {
    applyQuoteMode("sniper", { silent: true });
  }
}

function buildToml(opts = {}) {
  const allowDefaults = !!opts.allowDefaults;
  const mode = document.getElementById("mode").value;
  const account = document.getElementById("account_address").value.trim();
  const enableTrading = document.getElementById("enable_trading").value === "true";
  let symbol = normalizeSelectedSymbol(document.getElementById("market_symbol").value);
  const isExtended = isExtendedMode(mode);
  const isNado = isNadoMode(mode);
  const isRisex = isRisexMode(mode);
  const isQfex = isQfexMode(mode);
  if (!symbol && allowDefaults) {
    // Connection flow should not be blocked when markets haven't loaded yet.
    symbol = isExtended ? "BTC-PERP" : isNado ? "BTC-PERP" : isRisex ? "BTC/USDC" : isQfex ? "US500-USD" : "BTC";
  }
  // QFEX : symbole de la liste QFEX (refdata en cache, sinon liste instantanée). La forme
  // `XXX-USD` ne suffit pas : BTC-USD est un symbole Extended. Un symbole d'une autre
  // venue resté dans le sélecteur est remplacé pendant la connexion, refusé sinon.
  if (isQfex && allowDefaults && symbol && !_isQfexSymbol(symbol)) symbol = "US500-USD";
  if (isQfex && symbol && !_isQfexSymbol(symbol)) {
    throw new Error(`${symbol} is not a QFEX market. Pick a QFEX market such as US500-USD.`);
  }
  // Pendant la connexion, le sélecteur peut encore lister les marchés d'une autre
  // venue ("BTC") : un symbole RISEx a toujours la forme "BTC/USDC".
  if (isRisex && allowDefaults && symbol && !symbol.includes("/")) symbol = "BTC/USDC";
  if (isRisex && symbol && (!symbol.includes("/") || symbol.includes(":"))) {
    throw new Error(`${symbol} is not a RISEx market. Pick a RISEx market such as BTC/USDC.`);
  }
  let sessionNotional = parseFloat(document.getElementById("session_notional").value);
  if (!symbol) throw new Error("Missing token: please choose a token.");
  if (!account) throw new Error("Missing account address.");
  if (!Number.isFinite(sessionNotional) || sessionNotional <= 0) {
    if (!allowDefaults) {
      throw new Error("Invalid session notional.");
    }
    // Allow connection to proceed even when notional is not set yet.
    sessionNotional = 100;
  }

  // RISEx : le levier se lit sur le symbole RISEx final. Pendant la connexion, le
  // sélecteur peut encore afficher « BTC » d'Hyperliquid (40x) alors que BTC/USDC
  // plafonne à 25x : les tailles étaient calculées sur le mauvais levier.
  const leverage = isRisex ? _risexLeverageFor(symbol) : isQfex ? _qfexLeverageFor(symbol) : currentSelectedLeverage();
  const sizing = computeDynamicSizing(sessionNotional, leverage, selectedRiskMode);
  let maxPos = sizing.maxPos;
  let orderMax = sizing.orderMax;
  let orderMin = sizing.orderMin;
  let spreadMin = DEFAULT_MM.spreadMinBps;
  let spreadMax = DEFAULT_MM.spreadMaxBps;

  // maxLoss: use user input if provided, else fallback to dynamic sizing
  const userMaxLoss = _parseFormattedNum(document.getElementById("max_loss")?.value);
  // RISEx : pas de plancher à 30 $ (il dépasse un petit collatéral) ; 30 % du
  // collatéral engagé selon le profil, au moins 5 $.
  const autoMaxLoss = (isRisex || isQfex)
    ? Math.max(5, Math.round(sessionNotional * getRiskPreset(selectedRiskMode).maxLossPct * 100) / 100)
    : sizing.maxLoss;
  const maxLoss = Number.isFinite(userMaxLoss) && userMaxLoss > 0 ? userMaxLoss : autoMaxLoss;
  const targetVol = _parseFormattedNum(document.getElementById("target_vol")?.value);
  const minRequote = 2.0;
  let engineCancelReplaceMs = 1000;
  let engineRepriceThresholdBps = 0.6;
  let engineCooldownMs = 450;

  const isMainnet = mode === "hyperliquid_mainnet";
  const symbolDex = hip3DexOfSymbol(symbol);
  const perpDexs = symbolDex ? [symbolDex] : [];
  const rest = isMainnet ? "https://api.hyperliquid.xyz" : "https://api.hyperliquid-testnet.xyz";
  const ws = isMainnet ? "wss://api.hyperliquid.xyz/ws" : "wss://api.hyperliquid-testnet.xyz/ws";

  let profile = getRiskPreset(selectedRiskMode).profile;
  if (advancedMmEnabled) {
    const advancedCfg = readAdvancedMmConfigFromUi();
    const advancedErr = validateAdvancedMmConfig(advancedCfg);
    if (advancedErr) {
      if (!allowDefaults) throw new Error(`Advanced profile invalid: ${advancedErr}`);
    } else {
      spreadMin = advancedCfg.spreadMin;
      spreadMax = advancedCfg.spreadMax;
      orderMin = maxPos > 0 ? Math.min(advancedCfg.posMin, maxPos) : advancedCfg.posMin;
      orderMax = maxPos > 0 ? Math.min(advancedCfg.posMax, maxPos) : advancedCfg.posMax;
      profile = "";
      if (advancedCfg.engineSpeed === "fast") {
        engineCancelReplaceMs = 700;
        engineRepriceThresholdBps = 0.4;
        engineCooldownMs = 250;
      }
    }
  }
  const lines = [
    `mode = ${tomlValue(mode)}`,
    // RISEx : le bot attend exchanges = ["risex"] ; seul le mode sniper est supporté.
    `exchanges = [${tomlValue(isRisex ? "risex" : isQfex ? "qfex" : mode)}]`,
    `quote_mode = ${tomlValue((isRisex || isQfex) ? "sniper" : selectedQuoteMode)}`,
    "",
    "[[markets]]",
    `symbol = ${tomlValue(symbol)}`,
    `max_position_usd = ${maxPos}`,
    `order_size_usd_min = ${orderMin}`,
    `order_size_usd_max = ${orderMax}`,
    `quote_spread_bps_min = ${spreadMin}`,
    `quote_spread_bps_max = ${spreadMax}`,
    `inventory_skew_bps = 10`,
    `direction_bias = ${selectedDirectionBias === "long" ? 1 : selectedDirectionBias === "short" ? -1 : 0}`,
    `max_open_orders_per_side = 2`,
    `price_decimals = 2`,
    "",
    "[risk]",
    `max_session_loss_usd = ${maxLoss}`,
    `max_drawdown_usd = 300`,
    `starting_equity_usd = 10000`,
    `max_inventory_utilization = 1.25`,
    `max_consecutive_negative_markouts = 10`,
    `max_reject_ratio = 0.35`,
    "",
    "[engine]",
    `target_session_volume_usd = ${targetVol}`,
    `cancel_replace_interval_ms = ${engineCancelReplaceMs}`,
    `volatility_halt_bps = 150`,
    `funding_limit_bps = 20`,
    `trading_hours_utc = [0, 24]`,
    `paper_mode = false`,
    `status_interval_sec = 2`,
    `quote_reprice_threshold_bps = ${engineRepriceThresholdBps}`,
    `quote_cooldown_ms = ${engineCooldownMs}`,
    `vol_ewma_alpha = 0.25`,
    `toxicity_ewma_alpha = 0.25`,
    `toxicity_widen_bps_max = 2.5`,
    `size_reduction_on_toxicity = 0.65`,
    `maker_fee_bps = 0.15`,
    `grid_spacing_bps = 2.0`,
    "",
  ];
  if (isExtended) {
    lines.push(
      "[extended]",
      `rest_url = "https://api.starknet.extended.exchange"`,
      `ws_url = "wss://api.starknet.extended.exchange/ws"`,
      `account_address = ${tomlValue(account)}`,
      `enable_trading = ${tomlValue(enableTrading)}`,
      `api_key_env = "EXTENDED_API_KEY"`,
      `stark_public_key_env = "EXTENDED_STARK_PUBLIC_KEY"`,
      `stark_private_key_env = "EXTENDED_STARK_PRIVATE_KEY"`,
      `vault_address_env = "EXTENDED_VAULT_ADDRESS"`,
      `request_timeout_sec = 10.0`,
      `max_retries = 2`,
      `backoff_sec = 0.35`,
      `min_requote_interval_sec = ${minRequote}`,
      `min_price_move_bps = 2.0`,
      `post_only = true`,
      `tif = "GTC"`,
      `size_decimals = 6`,
      `builder_address = ${tomlValue(EXTENDED_BUILDER_ADDRESS)}`,
      `builder_id = ${EXTENDED_BUILDER_ID}`,
      `builder_fee_tenths_bps = ${approvedExtBuilderFeeTenthsBps}`,
      `account_index = ${getExtendedAccountIndex()}`,
      "",
    );
  } else if (isRisex) {
    // Contrat serveur (_validate_config_toml) : URL mainnet exacte, variables de clé
    // dédiées, signer restreint obligatoire, `account` = wallet authentifié. Le
    // builder (id 62) et son fee sont imposés par le serveur : jamais dans le TOML.
    lines.push(
      "[risex]",
      `api_url = "https://api.rise.trade"`,
      `account = ${tomlValue(account)}`,
      `signer_private_key_env = "RISEX_SIGNER_PRIVATE_KEY"`,
      `signer_expiration_env = "RISEX_SIGNER_EXPIRATION"`,
      `require_restricted_signer = true`,
      `enable_trading = ${tomlValue(enableTrading)}`,
      `leverage = ${leverage}`,
      `min_requote_interval_sec = ${minRequote}`,
      `min_price_move_bps = 2.0`,
      `close_slippage_bps = 50.0`,
      `quota_reserve = 200`,
      "",
    );
  } else if (isQfex) {
    // Contrat serveur (_validate_config_toml) : URLs de production exactes, variables
    // de clé dédiées, clé sans retrait obligatoire, `account_address` = wallet
    // authentifié. Le builder code est imposé par le serveur : jamais dans le TOML.
    lines.push(
      "[qfex]",
      `rest_url = "https://api.qfex.com"`,
      `ws_url = "wss://trade.qfex.com"`,
      `account_address = ${tomlValue(account)}`,
      `public_key_env = "QFEX_API_PUBLIC_KEY"`,
      `secret_key_env = "QFEX_API_SECRET_KEY"`,
      `builder_code_env = "QFEX_BUILDER_CODE"`,
      `require_trade_only_key = true`,
      `enable_trading = ${tomlValue(enableTrading)}`,
      `leverage = ${leverage}`,
      `min_requote_interval_sec = ${minRequote}`,
      // Pas de quota par ordre chez QFEX (contrairement à RISEx) et des indices qui
      // bougent par dixièmes de bp : recote dès 0,5 bp de mouvement.
      `min_price_move_bps = 0.5`,
      `close_slippage_bps = 50.0`,
      "",
    );
  } else if (isNado) {
    lines.push(
      "[nado]",
      `gateway_url = "https://gateway.prod.nado.xyz/v1"`,
      `private_key_env = "NADO_PRIVATE_KEY"`,
      `account_address = ${tomlValue(account)}`,
      `subaccount = "default"`,
      `leverage = 10.0`,
      `enable_trading = ${tomlValue(enableTrading)}`,
      `min_requote_interval_sec = ${minRequote}`,
      `min_price_move_bps = 2.0`,
      `request_timeout_sec = 10.0`,
      `post_only = true`,
      `builder_id = 2800`,
      `builder_fee_tenths_bps = 10`,
      "",
    );
  } else {
    lines.push(
      "[hyperliquid]",
      `rest_url = ${tomlValue(rest)}`,
      `ws_url = ${tomlValue(ws)}`,
      `private_key_env = "HL_PRIVATE_KEY"`,
      `vault_address = ${tomlValue(vaultAddressForToml(Number.isInteger(opts.slot) ? opts.slot : null))}`,
      `account_address = ${tomlValue(account)}`,
      `enable_trading = ${tomlValue(enableTrading)}`,
      `perp_dexs = [${perpDexs.map((d) => tomlValue(d)).join(", ")}]`,
      // Levier posé par le bot (plafonné au max du marché côté bot) : celui du sizing.
      `leverage = ${Math.max(1, Math.floor(Number(leverage) || 1))}`,
      `min_requote_interval_sec = ${minRequote}`,
      `min_price_move_bps = 2.0`,
      `post_only = true`,
      `tif = "Alo"`,
      `builder_address = ${tomlValue(BUILDER_ADDRESS)}`,
      `builder_fee_tenths_bps = ${approvedBuilderFeeTenthsBps}`,
      "",
    );
  }
  if (profile) {
    lines.splice(1, 0, `mm_profile = ${tomlValue(profile)}`);
  }
  return lines.join("\n");
}

function scheduleLiveConfigSync(delayMs = 500) {
  if (!document.body.classList.contains("main-space")) return;
  if (liveConfigSyncTimer) {
    window.clearTimeout(liveConfigSyncTimer);
  }
  liveConfigSyncTimer = window.setTimeout(async () => {
    liveConfigSyncTimer = null;
    try {
      _syncDexDomState(_activeDex);
      const rs = _getRunningSlot(_currentSymbol());
      const syncSlot = rs ? rs.slot : activeDexSlot();
      // Never let the trading form autosave clobber the reserved HIP-4 slot
      // (this is what raced and overwrote a prediction config with xyz:SPCX).
      if (syncSlot >= MM_SLOTS) return;
      const toml = buildToml({ slot: syncSlot });
      await apiPost(`/api/config?slot=${syncSlot}`, { toml });
    } catch (_) {
      // Best effort: keep editing fluid even if live sync fails.
    }
  }, Math.max(100, Number(delayMs) || 500));
}

function _tokenIconCoin(value) {
  if (!value) return "BTC";
  let coin = String(value);
  coin = coin.replace(/^[a-z0-9]+:/i, "");                // xyz:, mkts:, km:, io:, ext:, nado:
  coin = coin.replace(/-(USDC|USDH|USD|PERP)$/i, "");
  coin = coin.replace(/\/(USDC|USD)$/i, "");          // RISEx : "BTC/USDC"
  // Suffixe RWA Extended ("AMZN_24_5" → "AMZN") pour que le fallback
  // financialmodelingprep trouve le logo de l'action.
  coin = coin.replace(/_24_\d+$/, "");
  return coin;
}

// CoinCap couvre les cryptos (HYPE, ZEC, WLD, XRP, TON…) ; FMP en fallback pour
// les tickers TradFi (AAPL, TSLA…) qui n'existent pas chez CoinCap.
// io (EntropyIO) : actions et pré-IPO, jamais de crypto, donc FMP directement (PRL existe
// chez CoinCap pour un autre jeton). Ticker FMP explicite quand le nom du marché n'est pas
// le ticker boursier ; "" = aucun logo fiable (ANTH chez FMP est Anthera, TCNT est Toucan).
const HIP3_ICON_TICKERS = {
  "io:TCNT": "TCEHY", "io:XIAOMI": "1810.HK", "io:ANTH": "", "io:OAI": "", "io:PRL": "",
};
function _tokenIconUrl(value) {
  if (hip3DexOfSymbol(value) === "io") return _tokenIconFallbackUrl(value);
  return "https://assets.coincap.io/assets/icons/" + _tokenIconCoin(value).toLowerCase() + "@2x.png";
}

function _tokenIconFallbackUrl(value) {
  const key = normalizeSelectedSymbol(value);
  if (Object.prototype.hasOwnProperty.call(HIP3_ICON_TICKERS, key)) {
    const ticker = HIP3_ICON_TICKERS[key];
    return ticker ? "https://financialmodelingprep.com/image-stock/" + ticker + ".png" : "";
  }
  return "https://financialmodelingprep.com/image-stock/" + _tokenIconCoin(value).toUpperCase() + ".png";
}

function _tokenIconError(img) {
  const fb = img.getAttribute("data-fallback");
  if (fb && img.src !== fb) {
    img.removeAttribute("data-fallback");
    img.src = fb;
    return;
  }
  img.onerror = null;
  img.style.display = "none";
}

function _tokenIconImg(value, size = 18) {
  const img = document.createElement("img");
  img.src = _tokenIconUrl(value);
  img.setAttribute("data-fallback", _tokenIconFallbackUrl(value));
  img.alt = "";
  img.width = size;
  img.height = size;
  img.className = "token-icon";
  img.loading = "lazy";
  img.onerror = function () { _tokenIconError(this); };
  return img;
}

function _makeMarketOption(s) {
  const opt = document.createElement("option");
  opt.value = s;
  if (typeof s === "string" && hip3DexOfSymbol(s)) {
    // HIP-3 : "mkts:US500" → "US500", "xyz:AAPL-USDC" → "AAPL", "io:SNDK" → "SNDK".
    opt.textContent = s.slice(s.indexOf(":") + 1).replace(/-(USDC|USDH|USD)$/i, "");
  } else if (typeof s === "string") {
    // Extended : "AMZN_24_5-USD" → "AMZN", "BTC-USD" → "BTC" (la value garde
    // le nom de marché complet, seul l'affichage est raccourci).
    opt.textContent = s.replace(/_24_\d+(?=-)/, "").replace(/-(USDC|USDH|USD)$/i, "");
  } else {
    opt.textContent = s;
  }
  return opt;
}

// symbols can be a plain array OR an object with { groups: [{label, symbols}], allSymbols }
function applyMarketsToSelect(select, symbols, previousValue = "") {
  select.innerHTML = "";
  let allSymbols;
  if (symbols && !Array.isArray(symbols) && Array.isArray(symbols.groups)) {
    // Grouped: render optgroups
    allSymbols = symbols.allSymbols || symbols.groups.flatMap((g) => g.symbols);
    symbols.groups.forEach((group) => {
      const grp = document.createElement("optgroup");
      grp.label = group.label;
      group.symbols.forEach((s) => grp.appendChild(_makeMarketOption(s)));
      select.appendChild(grp);
    });
  } else {
    // Flat list
    allSymbols = symbols;
    symbols.forEach((s) => select.appendChild(_makeMarketOption(s)));
  }
  if (previousValue && allSymbols.includes(previousValue)) {
    select.value = previousValue;
  } else if (allSymbols.length > 0) {
    select.value = allSymbols[0];
  }
  // Update group state and tabs visibility
  const hasGroups = symbols && !Array.isArray(symbols) && Array.isArray(symbols.groups);
  // Groupes exposés au token modal (#mm_token_modal) pour ses onglets All/TradFi.
  _mmMarketGroups = hasGroups ? symbols.groups : null;
  _mmTokenModalGroup = "All";
  _tokenPickerHasGroups = hasGroups;
  _tokenPickerActiveGroup = "all";
  const tabsEl2 = document.getElementById("market_symbol_group_tabs");
  if (tabsEl2) {
    tabsEl2.classList.toggle("hidden", !hasGroups);
    tabsEl2.querySelectorAll(".token-picker-tab").forEach((t) => t.classList.toggle("active", t.dataset.group === "all"));
  }
  // Force repaint: some mobile browsers keep the old display after options rebuild.
  const chosen = select.value;
  select.value = "";
  void select.offsetWidth;
  select.value = chosen;
  syncTokenPickerFromSelect("");
}

function animateMarketSymbolChange() {
  const btn = document.getElementById("market_symbol_btn");
  if (!btn) return;
  btn.classList.remove("token-switch-anim");
  void btn.offsetWidth;
  btn.classList.add("token-switch-anim");
}

async function fetchMarketsFromApi(source, exchangeOverride = "") {
  const exchange = exchangeOverride || document.getElementById("mode")?.value || "hyperliquid_mainnet";
  const data = await apiGet(
    `/api/markets?mode=${encodeURIComponent(source)}&exchange=${encodeURIComponent(exchange)}`,
  );
  const symbols = Array.isArray(data.symbols) ? data.symbols : [];
  const maxLeverage = data && typeof data.max_leverage === "object" ? data.max_leverage : {};
  const groups = Array.isArray(data.groups) ? data.groups : null;
  const reduceOnly = Array.isArray(data.reduce_only) ? new Set(data.reduce_only) : new Set();
  if (data && typeof data.min_order_size === "object" && data.min_order_size) {
    marketMinOrderSizeMap = data.min_order_size;
    marketMinOrderUsdMap = (typeof data.min_order_usd === "object" && data.min_order_usd) || {};
    updateMinOrderHint();
  }
  if (data && typeof data.market_stats === "object" && data.market_stats) {
    marketStatsMap = data.market_stats;
  }
  // only_isolated : objet { symbole: bool } ou liste des symboles isolés uniquement.
  const onlyIsolated = data ? data.only_isolated : null;
  if (Array.isArray(onlyIsolated)) {
    symbols.forEach((s) => marketOnlyIsolated.set(s, onlyIsolated.includes(s)));
  } else if (onlyIsolated && typeof onlyIsolated === "object") {
    Object.entries(onlyIsolated).forEach(([s, v]) => marketOnlyIsolated.set(s, !!v));
  }
  if (onlyIsolated) updateLeverageUI();
  return { symbols, maxLeverage, groups, reduceOnly };
}

function prewarmMarketSourcesForMode(exchangeMode, sources = ["top20", ...Object.keys(HIP3_SOURCES)]) {
  const mode = String(exchangeMode || document.getElementById("mode")?.value || "hyperliquid_mainnet");
  // Extended/Nado/RISEx/QFEX only ever serve "top20" (HIP-3 sources 400 on those modes), skip prewarming them.
  if (isExtendedMode(mode) || isNadoMode(mode) || isRisexMode(mode) || isQfexMode(mode)) sources = ["top20"];
  const now = Date.now();
  const fastLev = Object.fromEntries(FAST_KINETIQ_SYMBOLS.map((s) => [s, DEFAULT_MM.defaultLeverage]));
  marketCache.set(`${mode}|hip3`, { symbols: FAST_KINETIQ_SYMBOLS, maxLeverage: fastLev, ts: now });
  sources.forEach((source) => {
    const src = String(source || "");
    if (!src || src === "coming_soon") return;
    const key = `${mode}|${src}`;
    const cached = marketCache.get(key);
    if (cached && now - cached.ts < MARKET_CACHE_TTL_MS) return;
    void fetchMarketsFromApi(src, mode)
      .then((payload) => {
        const symbols = payload.symbols || [];
        if (!symbols.length) return;
        marketCache.set(key, { symbols, groups: payload.groups || null, maxLeverage: payload.maxLeverage || {}, ts: Date.now() });
      })
      .catch(() => {});
  });
}

// Recharge la liste si elle ne correspond pas à la venue affichée : au retour sur la
// page, la venue connectée (ex. RISEx) est restaurée après le premier chargement (HL),
// et le sélecteur gardait « BTC » d'Hyperliquid pour une session RISEx.
async function ensureMarketsForCurrentMode() {
  const mode = document.getElementById("mode")?.value || "hyperliquid_mainnet";
  const source = document.getElementById("market_source")?.value || "";
  const select = document.getElementById("market_symbol");
  if (!select?.options?.length || _marketsLoadedKey !== `${mode}|${source}`) {
    await loadMarkets({ force: true });
    updateLeverageUI();
    return true;
  }
  return false;
}

async function loadMarkets(opts = {}) {
  const force = !!opts.force;
  const background = !!opts.background;
  const exchangeMode = document.getElementById("mode")?.value || "hyperliquid_mainnet";
  const source = document.getElementById("market_source").value;
  const select = document.getElementById("market_symbol");
  const prev = lastSelectedSymbolBySource.get(source) || select.value;
  const cacheKey = `${exchangeMode}|${source}`;
  _marketsLoadedKey = cacheKey;
  if (source === "coming_soon") {
    select.innerHTML = "";
    select.disabled = true;
    applyLeverageMap([], {});
    syncTokenPickerFromSelect("");
    return;
  }
  // Instant fallback for Extended mode (like FAST_KINETIQ_SYMBOLS for Kinetiq).
  const extended = isExtendedMode(exchangeMode);
  if (extended && source === "top20" && !marketCache.get(cacheKey)) {
    select.disabled = false;
    const fastGrouped = {
      groups: [
        { label: "All", symbols: FAST_EXTENDED_SYMBOLS },
        { label: "TradFi", symbols: FAST_EXTENDED_TRADFI },
      ],
      allSymbols: FAST_EXTENDED_SYMBOLS,
    };
    applyMarketsToSelect(select, fastGrouped, prev);
    applyLeverageMap(FAST_EXTENDED_SYMBOLS, FAST_EXTENDED_LEVERAGE);
    marketCache.set(cacheKey, { symbols: FAST_EXTENDED_SYMBOLS, groups: fastGrouped.groups, maxLeverage: FAST_EXTENDED_LEVERAGE, ts: 0 });
    void fetchMarketsFromApi(source)
      .then((payload) => {
        const ts = Date.now();
        const symbols = payload.symbols || [];
        const maxLeverage = payload.maxLeverage || {};
        if (symbols.length) {
          const groups = payload.groups || null;
          marketCache.set(cacheKey, { symbols, groups, maxLeverage, ts });
          const liveExchange = document.getElementById("mode")?.value || "";
          if (isExtendedMode(liveExchange)) {
            const liveSelect = document.getElementById("market_symbol");
            if (liveSelect) {
              const toApply = groups ? { groups, allSymbols: symbols } : symbols;
              applyMarketsToSelect(liveSelect, toApply, liveSelect.value);
              applyLeverageMap(symbols, maxLeverage);
            }
          }
        }
      })
      .catch(() => {});
    return;
  }
  // Instant fallback for RISEx mode (groupes Crypto / TradFi, comme Extended).
  const risex = isRisexMode(exchangeMode);
  if (risex && source === "top20" && !marketCache.get(cacheKey)) {
    select.disabled = false;
    const fastGrouped = {
      groups: [
        { label: "Crypto", symbols: FAST_RISEX_SYMBOLS.filter((s) => !FAST_RISEX_TRADFI.includes(s)) },
        { label: "TradFi", symbols: FAST_RISEX_TRADFI },
      ],
      allSymbols: FAST_RISEX_SYMBOLS,
    };
    applyMarketsToSelect(select, fastGrouped, prev);
    applyLeverageMap(FAST_RISEX_SYMBOLS, FAST_RISEX_LEVERAGE);
    marketCache.set(cacheKey, { symbols: FAST_RISEX_SYMBOLS, groups: fastGrouped.groups, maxLeverage: FAST_RISEX_LEVERAGE, ts: 0 });
    void fetchMarketsFromApi(source, exchangeMode)
      .then((payload) => {
        const ts = Date.now();
        const symbols = payload.symbols || [];
        const maxLeverage = payload.maxLeverage || {};
        if (symbols.length) {
          const groups = payload.groups || null;
          marketCache.set(cacheKey, { symbols, groups, maxLeverage, ts });
          // Relire la venue après l'await : l'utilisateur a pu changer de DEX.
          const liveExchange = document.getElementById("mode")?.value || "";
          const liveSource = document.getElementById("market_source")?.value || "";
          if (isRisexMode(liveExchange) && liveSource === source) {
            const liveSelect = document.getElementById("market_symbol");
            if (liveSelect) {
              const toApply = groups ? { groups, allSymbols: symbols } : symbols;
              applyMarketsToSelect(liveSelect, toApply, liveSelect.value);
              applyLeverageMap(symbols, maxLeverage);
            }
          }
        }
      })
      .catch(() => {});
    return;
  }
  // Instant fallback for QFEX mode (groupes Indices / Equities / Commodities côté API).
  const qfex = isQfexMode(exchangeMode);
  if (qfex && source === "top20" && !marketCache.get(cacheKey)) {
    select.disabled = false;
    // Symbole d'une autre venue (BTC, BTC-USD d'Extended…) : jamais repris sur QFEX.
    // Un marché QFEX hors liste rapide (AMD-USD…) est gardé en attente de la liste
    // de l'API, qui seule peut le confirmer.
    const wantedQfex = /^[A-Z0-9]+-USD$/.test(String(prev || "")) ? String(prev) : "";
    const qfexPrev = FAST_QFEX_SYMBOLS.includes(prev) ? prev : "US500-USD";
    applyMarketsToSelect(select, FAST_QFEX_SYMBOLS, qfexPrev);
    applyLeverageMap(FAST_QFEX_SYMBOLS, FAST_QFEX_LEVERAGE);
    marketCache.set(cacheKey, { symbols: FAST_QFEX_SYMBOLS, maxLeverage: FAST_QFEX_LEVERAGE, ts: 0 });
    void fetchMarketsFromApi(source, exchangeMode)
      .then((payload) => {
        const ts = Date.now();
        const symbols = payload.symbols || [];
        const maxLeverage = payload.maxLeverage || {};
        if (symbols.length) {
          const groups = payload.groups && payload.groups.length ? payload.groups : null;
          marketCache.set(cacheKey, { symbols, groups, maxLeverage, ts });
          // Relire la venue après l'await : l'utilisateur a pu changer de DEX.
          const liveExchange = document.getElementById("mode")?.value || "";
          const liveSource = document.getElementById("market_source")?.value || "";
          if (isQfexMode(liveExchange) && liveSource === source) {
            const liveSelect = document.getElementById("market_symbol");
            if (liveSelect) {
              const toApply = groups ? { groups, allSymbols: symbols } : symbols;
              // Choix de l'utilisateur d'abord s'il est confirmé par l'API, puis la valeur
              // courante (changée entre-temps), puis US500-USD.
              const keep = symbols.includes(wantedQfex) && liveSelect.value === qfexPrev ? wantedQfex
                : symbols.includes(liveSelect.value) ? liveSelect.value
                  : (symbols.includes("US500-USD") ? "US500-USD" : symbols[0]);
              applyMarketsToSelect(liveSelect, toApply, keep);
              applyLeverageMap(symbols, maxLeverage);
            }
          }
        }
      })
      .catch(() => {});
    return;
  }
  // Instant fallback for Nado mode
  const nado = isNadoMode(exchangeMode);
  if (nado && source === "top20" && !marketCache.get(cacheKey)) {
    select.disabled = false;
    applyMarketsToSelect(select, FAST_NADO_SYMBOLS, prev);
    applyLeverageMap(FAST_NADO_SYMBOLS, FAST_NADO_LEVERAGE);
    marketCache.set(cacheKey, { symbols: FAST_NADO_SYMBOLS, maxLeverage: FAST_NADO_LEVERAGE, ts: 0 });
    void fetchMarketsFromApi(source)
      .then((payload) => {
        const ts = Date.now();
        const symbols = payload.symbols || [];
        const maxLeverage = payload.maxLeverage || {};
        if (symbols.length) {
          const groups = payload.groups || null;
          const reduceOnly = payload.reduceOnly || new Set();
          marketCache.set(cacheKey, { symbols, groups, maxLeverage, reduceOnly, ts });
          const liveExchange = document.getElementById("mode")?.value || "";
          if (isNadoMode(liveExchange)) {
            _tokenPickerReduceOnly = reduceOnly;
            const liveSelect = document.getElementById("market_symbol");
            if (liveSelect) {
              const toApply = groups ? { groups, allSymbols: symbols } : symbols;
              applyMarketsToSelect(liveSelect, toApply, liveSelect.value);
              applyLeverageMap(symbols, maxLeverage);
            }
          }
        }
      })
      .catch(() => {});
    return;
  }
  const cached = marketCache.get(cacheKey);
  const now = Date.now();
  if (!force && cached) {
    select.disabled = false;
    _tokenPickerReduceOnly = cached.reduceOnly || new Set();
    applyLeverageMap(cached.symbols, cached.maxLeverage || {});
    const cachedToApply = cached.groups ? { groups: cached.groups, allSymbols: cached.symbols } : cached.symbols;
    applyMarketsToSelect(select, cachedToApply, prev);
    if (now - cached.ts < MARKET_CACHE_TTL_MS) {
      return;
    }
    // Cache stale: keep UI instant, then reconcile in background.
    void fetchMarketsFromApi(source)
      .then((payload) => {
        const ts = Date.now();
        const symbols = payload.symbols || [];
        const maxLeverage = payload.maxLeverage || {};
        if (!symbols.length) return;
        const groups = payload.groups || null;
        const reduceOnly = payload.reduceOnly || new Set();
        marketCache.set(cacheKey, { symbols, groups, maxLeverage, reduceOnly, ts });
        const liveSource = document.getElementById("market_source")?.value;
        const liveExchange = document.getElementById("mode")?.value || "hyperliquid_mainnet";
        if (liveSource === source && liveExchange === exchangeMode) {
          _tokenPickerReduceOnly = reduceOnly;
          const liveSelect = document.getElementById("market_symbol");
          if (liveSelect) {
            const livePrev = liveSelect.value;
            const toApply = groups ? { groups, allSymbols: symbols } : symbols;
            applyMarketsToSelect(liveSelect, toApply, livePrev);
            applyLeverageMap(symbols, maxLeverage);
          }
        }
      })
      .catch(() => {});
    return;
  }
  if (!force && source === "top20" && !cached) {
    // Instant UI response for Hyperliquid top20 : show fast list, reconcile from API.
    select.disabled = false;
    applyMarketsToSelect(select, FAST_TOP20_SYMBOLS, prev);
    applyLeverageMap(FAST_TOP20_SYMBOLS, FAST_TOP20_LEVERAGE);
    marketCache.set(cacheKey, { symbols: FAST_TOP20_SYMBOLS, maxLeverage: FAST_TOP20_LEVERAGE, ts: 0 });
    void fetchMarketsFromApi(source)
      .then((payload) => {
        const ts = Date.now();
        const symbols = payload.symbols || [];
        const maxLeverage = payload.maxLeverage || {};
        if (symbols.length) {
          marketCache.set(cacheKey, { symbols, maxLeverage, ts });
          const liveSource = document.getElementById("market_source")?.value;
          const liveExchange = document.getElementById("mode")?.value || "hyperliquid_mainnet";
          if (liveSource === source && liveExchange === exchangeMode) {
            const liveSelect = document.getElementById("market_symbol");
            if (liveSelect) {
              const livePrev = liveSelect.value;
              applyMarketsToSelect(liveSelect, symbols, livePrev);
              applyLeverageMap(symbols, maxLeverage);
            }
          }
        }
      })
      .catch(() => {});
    return;
  }
  const fastHip3 = HIP3_SOURCES[source];
  if (!force && fastHip3?.fast && !cached) {
    // Instant UI response for Trade.xyz / EntropyIO sources, then reconcile from API.
    select.disabled = false;
    applyMarketsToSelect(select, fastHip3.fast, prev);
    applyLeverageMap(fastHip3.fast, fastHip3.fastLeverage);
    marketCache.set(cacheKey, { symbols: fastHip3.fast, maxLeverage: fastHip3.fastLeverage, ts: 0 });
    void fetchMarketsFromApi(source)
      .then((payload) => {
        const ts = Date.now();
        const symbols = payload.symbols || [];
        const maxLeverage = payload.maxLeverage || {};
        if (symbols.length) {
          marketCache.set(cacheKey, { symbols, maxLeverage, ts });
          const liveSource = document.getElementById("market_source")?.value;
          const liveExchange = document.getElementById("mode")?.value || "hyperliquid_mainnet";
          if (liveSource === source && liveExchange === exchangeMode) {
            const liveSelect = document.getElementById("market_symbol");
            if (liveSelect) {
              const livePrev = liveSelect.value;
              applyMarketsToSelect(liveSelect, symbols, livePrev);
              applyLeverageMap(symbols, maxLeverage);
            }
          }
        }
      })
      .catch(() => {});
    return;
  }
  if (!force && source === "hip3" && !cached) {
    // Instant UI response for Kinetiq source, then reconcile from API.
    select.disabled = false;
    applyMarketsToSelect(select, FAST_KINETIQ_SYMBOLS, prev);
    const fastLev = Object.fromEntries(FAST_KINETIQ_SYMBOLS.map((s) => [s, DEFAULT_MM.defaultLeverage]));
    applyLeverageMap(FAST_KINETIQ_SYMBOLS, fastLev);
    marketCache.set(cacheKey, { symbols: FAST_KINETIQ_SYMBOLS, maxLeverage: fastLev, ts: now });
    void fetchMarketsFromApi(source)
      .then((payload) => {
        const ts = Date.now();
        const symbols = payload.symbols || [];
        const maxLeverage = payload.maxLeverage || {};
        if (symbols.length) {
          marketCache.set(cacheKey, { symbols, maxLeverage, ts });
          const liveSource = document.getElementById("market_source")?.value;
          const liveExchange = document.getElementById("mode")?.value || "hyperliquid_mainnet";
          if (liveSource === source && liveExchange === exchangeMode) {
            const liveSelect = document.getElementById("market_symbol");
            if (liveSelect) {
              const livePrev = liveSelect.value;
              applyMarketsToSelect(liveSelect, symbols, livePrev);
              applyLeverageMap(symbols, maxLeverage);
            }
          }
        }
      })
      .catch(() => {});
    return;
  }
  if (background) {
    // Non-blocking refresh path for prewarm.
    void fetchMarketsFromApi(source)
      .then((payload) => {
        const ts = Date.now();
        marketCache.set(cacheKey, { symbols: payload.symbols || [], maxLeverage: payload.maxLeverage || {}, ts });
      })
      .catch(() => {});
    return;
  }
  try {
    const payload = await fetchMarketsFromApi(source);
    const symbols = payload.symbols || [];
    const maxLeverage = payload.maxLeverage || {};
    const groups = payload.groups || null;
    marketCache.set(cacheKey, { symbols, groups, maxLeverage, ts: now });
    select.disabled = false;
    const toApply = groups ? { groups, allSymbols: symbols } : symbols;
    applyMarketsToSelect(select, toApply, prev);
    applyLeverageMap(symbols, maxLeverage);
  } catch (err) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "Failed to load markets";
    select.appendChild(opt);
    syncTokenPickerFromSelect("");
    applyLeverageMap([], {});
  }
}

// ── Slot UI helpers ───────────────────────────────────────
function _slotDisplayToken(symbol) {
  if (!symbol) return "-";
  if (hip3DexOfSymbol(symbol)) return symbol.slice(symbol.indexOf(":") + 1).replace(/-(USDC|USDH|USD)$/i, "");
  // Extended : "SNDK_24_5-USD" → "SNDK", "BTC-USD" → "BTC" (les symboles HL
  // nus comme "BTC" passent inchangés).
  // RISEx : "BTC/USDC" → "BTC".
  return symbol.replace(/_24_\d+(?=-)/, "").replace(/[-/](USDC|USDH|USD)$/i, "");
}

function _parseNum(v) {
  const n = typeof v === "string" ? parseFloat(v) : v;
  return Number.isFinite(n) ? n : null;
}

/** Render running-token cards + sync main action button state.
 *  Uses DOM-diffing: update existing cards in-place instead of rebuilding,
 *  so buttons stay alive during async operations and the UI doesn't flicker. */
let _slotStoppingSet = new Set(); // slot ids currently being stopped

// Badge affiché sur une carte de session RUNNING quand elle tourne sur un
// sub-account HL plutôt que le compte principal (Task 12). Lookup du nom dans
// _hlSubaccounts (Task 9), fallback générique "SUB" si l'adresse est inconnue
// (ex: sub supprimé côté HL depuis le lancement de la session).
function _sessionSubBadgeHtml(tradingAccount) {
  if (!tradingAccount || typeof tradingAccount !== "string") return "";
  const sub = (typeof _hlSubaccounts !== "undefined" ? _hlSubaccounts : [])
    .find(s => s.address && s.address.toLowerCase() === tradingAccount.toLowerCase());
  const label = sub && sub.name ? sub.name.toUpperCase() : "SUB";
  return `<span class="session-sub-badge">${escapeHtml(label)}</span>`;
}

function renderRunningSlots() {
  // Le verrou des pills Trading Account dépend de runningSlots : re-render à
  // chaque changement d'état des sessions (pollStatus, start, stop).
  if (typeof renderTradingAccountSelector === "function") renderTradingAccountSelector();
  const container = document.getElementById("running_slots");
  if (container) {
    const live = runningSlots.filter(s => s.running || s.degraded || s.standby);
    const existingCards = container.querySelectorAll(".slot-card[data-slot-id]");
    const existingById = new Map();
    existingCards.forEach(c => existingById.set(c.dataset.slotId, c));

    const liveIds = new Set(live.map(s => String(s.slot)));

    // Remove cards that are no longer live
    existingById.forEach((card, id) => {
      if (!liveIds.has(id)) card.remove();
    });

    // Update or create cards
    let insertBefore = container.querySelector(".slot-add-btn");
    live.forEach(slot => {
      const id = String(slot.slot);
      const pnlVal = typeof slot.sessionPnl === "number" && Number.isFinite(slot.sessionPnl) ? slot.sessionPnl : null;
      const pnlClass = pnlVal === null ? "zero" : pnlVal >= 0 ? "positive" : "negative";
      const pnlText = pnlVal !== null ? formatUsd(pnlVal) : "-";
      const volVal = typeof slot.sessionVolume === "number" && Number.isFinite(slot.sessionVolume) ? slot.sessionVolume : null;
      const tgtVal = typeof slot.targetVolume === "number" && Number.isFinite(slot.targetVolume) && slot.targetVolume > 0 ? slot.targetVolume : null;
      const volCurrent = volVal !== null ? formatCompactVolume(volVal) : "0";
      const volTarget = tgtVal ? formatCompactVolume(tgtVal) : "-";
      const volPct = (volVal !== null && tgtVal) ? Math.min(100, (volVal / tgtVal) * 100) : 0;

      const schedActive = _currentSchedule && _currentSchedule.enabled;
      const schedBadge = _renderScheduleBadge(_currentSchedule);

      const subBadgeHtml = _sessionSubBadgeHtml(slot.tradingAccount);

      let card = existingById.get(id);
      if (card) {
        // Update existing card in-place
        const tokenEl = card.querySelector(".slot-token");
        // textContent wipes children (e.g. the sub badge) - re-append after.
        if (tokenEl) tokenEl.textContent = _slotDisplayToken(slot.symbol);
        // Sub-account badge (Task 12) : ajouté/retiré/mis à jour selon trading_account
        if (subBadgeHtml && tokenEl) tokenEl.insertAdjacentHTML("beforeend", subBadgeHtml);
        // Volume row vs schedule badge
        const volRow = card.querySelector(".slot-vol-row");
        const schedEl = card.querySelector(".slot-sched-badge");
        if (schedActive) {
          if (volRow) volRow.style.display = "none";
          if (schedEl) {
            schedEl.outerHTML = schedBadge;
          } else {
            const info = card.querySelector(".slot-info");
            if (info && !info.querySelector(".slot-sched-badge")) info.insertAdjacentHTML("afterbegin", schedBadge);
          }
        } else {
          if (volRow) volRow.style.display = "";
          if (schedEl) schedEl.remove();
          const volEl = card.querySelector(".slot-vol");
          if (volEl) volEl.textContent = `${volCurrent} / ${volTarget}`;
          const editBtn = card.querySelector(".slot-vol-edit-btn");
          if (editBtn) {
            editBtn.dataset.slotMinVol = String(volVal || 0);
            editBtn.dataset.slotCurrentTarget = tgtVal ? String(tgtVal) : "";
          }
        }
        const barFill = card.querySelector(".slot-vol-fill");
        if (barFill) barFill.style.width = `${volPct.toFixed(1)}%`;
        // Add/remove volume bar if target changed
        const barWrap = card.querySelector(".slot-vol-bar");
        if (tgtVal && !schedActive && !barWrap) {
          const bar = document.createElement("div");
          bar.className = "slot-vol-bar";
          bar.innerHTML = `<div class="slot-vol-fill" style="width:${volPct.toFixed(1)}%"></div>`;
          const timer = card.querySelector(".slot-timer");
          if (timer) card.insertBefore(bar, timer);
        } else if ((schedActive || !tgtVal) && barWrap) {
          barWrap.remove();
        }
        const timerEl = card.querySelector(".slot-timer");
        if (timerEl) {
          if (slot.elapsedText === "waiting") {
            if (!timerEl.classList.contains("slot-timer-waiting")) {
              timerEl.innerHTML = 'waiting<span class="dot-1">.</span><span class="dot-2">.</span><span class="dot-3">.</span>';
              timerEl.classList.add("slot-timer-waiting");
            }
          } else {
            timerEl.textContent = slot.elapsedText || "--:--:--";
            timerEl.classList.remove("slot-timer-waiting");
          }
        }
        // Schedule progress bar
        const existSchedBar = card.querySelector(".slot-sched-bar");
        if (typeof slot.schedProgress === "number") {
          if (existSchedBar) {
            const fill = existSchedBar.querySelector(".slot-sched-fill");
            if (fill) fill.style.width = `${slot.schedProgress.toFixed(1)}%`;
          } else {
            const bar = document.createElement("div");
            bar.className = "slot-sched-bar";
            bar.innerHTML = `<div class="slot-sched-fill" style="width:${slot.schedProgress.toFixed(1)}%"></div>`;
            const timer = card.querySelector(".slot-timer");
            if (timer) card.insertBefore(bar, timer);
          }
        } else if (existSchedBar) {
          existSchedBar.remove();
        }
        // Update badge for standby ↔ running transitions
        const badgeEl = card.querySelector(".slot-badge");
        if (badgeEl) {
          if (slot.paused) {
            badgeEl.className = "slot-badge paused";
            badgeEl.textContent = "PAUSED";
          } else if (slot.standby) {
            badgeEl.className = "slot-badge standby";
            badgeEl.textContent = "SCHEDULED";
          } else {
            badgeEl.className = "slot-badge running";
            badgeEl.textContent = "LIVE";
          }
        }
        const stopBtn = card.querySelector(".slot-stop-btn");
        if (stopBtn && !_slotStoppingSet.has(id)) {
          stopBtn.textContent = slot.standby ? "Cancel" : "Stop";
        }
        // Update pause button state
        const pauseBtn = card.querySelector(".slot-pause-btn");
        if (pauseBtn) {
          pauseBtn.classList.toggle("active", !!slot.paused);
          pauseBtn.textContent = slot.paused ? "▶" : "⏸";
          pauseBtn.title = slot.paused ? "Resume quoting" : "Pause quoting";
        }
        // Don't touch stop button if it's in loading state
      } else {
        // Create new card
        card = document.createElement("div");
        card.className = "slot-card slot-running";
        card.dataset.slotId = id;
        const volRowHtml = schedActive
          ? schedBadge
          : `<span class="slot-vol-row">
              <span class="slot-vol">${volCurrent} / ${volTarget}</span>
              <button type="button" class="slot-vol-edit-btn" data-slot-edit-vol="${slot.slot}" data-slot-min-vol="${volVal || 0}" data-slot-current-target="${tgtVal || ""}" title="Edit target volume">edit</button>
            </span>`;
        // Use server-provided dex or fallback to slot mapping
        const slotDex = slot.dex || Object.keys(PRIMARY_DEX_SLOT).find(d => PRIMARY_DEX_SLOT[d] === slot.slot) || "hyperliquid";
        const slotDexLabel = DEX_LABELS[slotDex] || slotDex;
        card.innerHTML = `
          <div class="slot-token"><span class="slot-dex-badge slot-dex-${slotDex}">${escapeHtml(slotDexLabel)}</span> ${escapeHtml(_slotDisplayToken(slot.symbol))}${subBadgeHtml}</div>
          <div class="slot-info">
            ${volRowHtml}
            <button type="button" class="slot-sched-toggle" data-slot-sched="${slot.slot}" title="Schedule auto-start/stop">${_CLOCK_SVG}</button>
          </div>
          ${!schedActive && tgtVal ? `<div class="slot-vol-bar"><div class="slot-vol-fill" style="width:${volPct.toFixed(1)}%"></div></div>` : ""}
          ${typeof slot.schedProgress === "number" ? `<div class="slot-sched-bar"><div class="slot-sched-fill" style="width:${slot.schedProgress.toFixed(1)}%"></div></div>` : ""}
          ${slot.elapsedText === "waiting"
            ? '<span class="slot-timer slot-timer-waiting">waiting<span class="dot-1">.</span><span class="dot-2">.</span><span class="dot-3">.</span></span>'
            : `<span class="slot-timer">${slot.elapsedText || "--:--:--"}</span>`}
          <span class="slot-badge ${slot.paused ? "paused" : slot.standby ? "standby" : "running"}">${slot.paused ? "PAUSED" : slot.standby ? "SCHEDULED" : "LIVE"}</span>
          <button type="button" class="slot-action-btn slot-stop-btn" data-slot-stop="${slot.slot}">${slot.standby ? "Cancel" : "Stop"}</button>
          ${!slot.standby ? `<button type="button" class="slot-action-btn slot-pause-btn${slot.paused ? " active" : ""}" data-slot-pause="${slot.slot}" title="${slot.paused ? "Resume quoting" : "Pause quoting"}">${slot.paused ? "▶" : "⏸"}</button>` : ""}
          ${!slot.standby ? `<span class="slot-pause-help" data-tooltip="Pause stops all order placement and cancels resting orders. The position stays open. Resume to continue quoting.">?</span>` : ""}
        `;
        container.insertBefore(card, insertBefore);
      }
    });

    // Add-token button
    const existingAddBtn = container.querySelector(".slot-add-btn");
    if (live.length > 0 && live.length < MM_SLOTS) {
      if (!existingAddBtn) {
        const addBtn = document.createElement("button");
        addBtn.type = "button";
        addBtn.className = "slot-add-btn";
        addBtn.title = `Add token (${live.length}/${MM_SLOTS})`;
        addBtn.innerHTML = `<span class="slot-add-icon">+</span><span class="slot-add-label">Add token</span>`;
        addBtn.addEventListener("click", () => {
          setDirectionBiasLocked(false);
          window.scrollTo({ top: 0, behavior: "smooth" });
          setTimeout(() => {
            setTokenPickerOpen(true);
            syncTokenPickerFromSelect("");
            const search = document.getElementById("market_symbol_search");
            if (search) search.focus();
          }, 300);
        });
        container.appendChild(addBtn);
      } else {
        existingAddBtn.title = `Add token (${live.length}/${MM_SLOTS})`;
      }
    } else if (existingAddBtn) {
      existingAddBtn.remove();
    }
  }
  // Stop All: show only when 2+ running
  const stopAllBtn = document.getElementById("stop_all_btn");
  if (stopAllBtn) {
    const liveCount = runningSlots.filter(s => s.running || s.degraded).length;
    stopAllBtn.classList.toggle("hidden", liveCount < 2);
  }
  // Main action button: START if current token not running, STOP if it is
  _syncMainActionBtn();
}

function _syncMainActionBtn() {
  const btn = document.getElementById("main_action_btn");
  if (!btn) return;
  // If stopping, only resolve when bot is no longer running for current token
  if (btn.classList.contains("stopping")) {
    if (!_isCurrentTokenRunning()) {
      setActionBtnState("stopped");
    }
    return;
  }
  if (btn.classList.contains("loading")) {
    // Transition from "Starting..." to "Running" once the active DEX slot is running
    if (_activeDexSlotRunning()) {
      setActionBtnState("running");
    }
    return;
  }
  const label = btn.querySelector(".action-btn-label");
  const sym = _currentSymbol();
  const isRunning = _isCurrentTokenRunning();
  const isStandby = _isCurrentTokenStandby();
  btn.classList.remove("stopped", "running", "degraded", "armed");
  btn.disabled = false;
  if (isRunning) {
    btn.classList.add("running");
    if (label) label.textContent = "⏹ STOP BOT";
  } else if (isStandby) {
    btn.classList.add("armed");
    if (label) label.textContent = "✕ CANCEL SCHEDULE";
  } else {
    btn.classList.add("stopped");
    if (label) label.textContent = "▶ START BOT";
  }
  setDirectionBiasLocked(isRunning || isStandby);
  setQuoteModeLocked(isRunning || isStandby);
  if (typeof advancedStrategiesAreFrozen === "function") {
    const frozenNow = advancedStrategiesAreFrozen();
    if (frozenNow !== _advancedStrategyLastFrozenState) {
      _advancedStrategyLastFrozenState = frozenNow;
      renderAdvancedStrategies();
    } else {
      updateAdvancedStrategyEditorVisibility();
    }
  }
}

function setActionBtnState(state) {
  const btn = document.getElementById("main_action_btn");
  if (!btn) return;
  const label = btn.querySelector(".action-btn-label");
  btn.classList.remove("stopped", "running", "degraded", "loading", "stopping", "armed");
  btn.disabled = false;
  // (particle effect is fire-and-forget, no cleanup needed)
  if (state === "running") {
    btn.classList.add("running");
    if (label) label.textContent = "⏹ STOP BOT";
  } else if (state === "starting") {
    btn.classList.add("loading");
    btn.disabled = true;
    if (label) label.textContent = "Starting…";
  } else if (state === "loading") {
    btn.classList.add("loading");
    btn.disabled = true;
    if (label) label.textContent = "...";
  } else if (state === "stopping") {
    btn.classList.add("loading", "stopping");
    btn.disabled = true;
    if (label) label.textContent = "Stopping…";
  } else if (state === "degraded") {
    btn.classList.add("degraded");
    if (label) label.textContent = "⏹ STOP BOT";
  } else {
    btn.classList.add("stopped");
    if (label) label.textContent = "▶ START BOT";
  }
  setDirectionBiasLocked(state === "running" || state === "degraded");
}

function _spawnStartParticles(btn) {
  const panel = btn.closest("section.panel");
  if (!panel) return;

  // 1 : Shockwave ring expanding from button center
  const wave = document.createElement("div");
  wave.className = "launch-shockwave";
  panel.appendChild(wave);
  const btnRect = btn.getBoundingClientRect();
  const panelRect = panel.getBoundingClientRect();
  wave.style.left = (btnRect.left - panelRect.left + btnRect.width / 2) + "px";
  wave.style.top = (btnRect.top - panelRect.top + btnRect.height / 2) + "px";
  wave.addEventListener("animationend", () => wave.remove());

  // 2 : Central flash
  const flash = document.createElement("div");
  flash.className = "launch-flash";
  flash.style.left = wave.style.left;
  flash.style.top = wave.style.top;
  panel.appendChild(flash);
  flash.addEventListener("animationend", () => flash.remove());

  // 3 : Panel border glow pulse
  panel.classList.add("launch-glow");
  setTimeout(() => panel.classList.remove("launch-glow"), 1200);

  // 4 : Particles bursting outward across the panel
  const cx = btnRect.left + btnRect.width / 2;
  const cy = btnRect.top + btnRect.height / 2;
  const count = 45;
  for (let i = 0; i < count; i++) {
    const p = document.createElement("div");
    p.className = "start-particle";
    const angle = (Math.PI * 2 * i) / count + (Math.random() - 0.5) * 0.5;
    const dist = 60 + Math.random() * 140;
    const dx = Math.cos(angle) * dist;
    const dy = Math.sin(angle) * dist;
    const size = 3 + Math.random() * 5;
    const delay = Math.random() * 80;
    p.style.cssText = `left:${cx}px;top:${cy}px;width:${size}px;height:${size}px;--dx:${dx}px;--dy:${dy}px;animation-delay:${delay}ms;`;
    document.body.appendChild(p);
    p.addEventListener("animationend", () => p.remove());
  }

  // 5 : Micro-shake on the panel
  panel.classList.add("launch-shake");
  setTimeout(() => panel.classList.remove("launch-shake"), 400);
}

function _renderPerTokenBreakdown(perTokenPnl) {
  let container = document.getElementById("per_token_pnl");
  if (perTokenPnl.length < 2) {
    if (container) container.remove();
    return;
  }
  if (!container) {
    container = document.createElement("div");
    container.id = "per_token_pnl";
    container.className = "per-token-pnl";
    const kpiGrid = document.querySelector(".dashboard-kpi-grid");
    if (kpiGrid) kpiGrid.after(container);
    else return;
  }
  container.innerHTML = perTokenPnl.map(t => {
    const cls = t.pnl > 0 ? "pnl-pos" : t.pnl < 0 ? "pnl-neg" : "";
    return `<span class="per-token-item"><span class="per-token-symbol">${escapeHtml(_slotDisplayToken(t.symbol))}</span> <span class="${cls}">${formatUsd(t.pnl)}</span></span>`;
  }).join("");
}

function hideRiskChangesRow() {
  const row = document.getElementById("risk_changes_row");
  if (row) row.classList.add("hidden");
  const err = document.getElementById("risk_error");
  if (err) { err.textContent = ""; err.classList.add("hidden"); }
}

function formatSessionElapsed(totalSeconds) {
  const sec = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** Compute schedule-aware elapsed text and progress for slot 0.
 *  Returns { elapsedText, schedProgress } where:
 *  - elapsedText = "waiting" if before start_time, else HH:MM:SS from start_time
 *  - schedProgress = 0-100 if between start_time and stop_time, else null */
function _scheduleAwareElapsed(startedAtSec) {
  const baseElapsed = (Number.isFinite(startedAtSec) && startedAtSec > 0)
    ? formatSessionElapsed((Date.now() / 1000) - startedAtSec)
    : "--:--:--";
  const sched = _currentSchedule;
  if (!sched || !sched.enabled || !sched.start_time) {
    return { elapsedText: baseElapsed, schedProgress: null };
  }
  const now = new Date();
  const [sh, sm] = sched.start_time.split(":").map(Number);
  const schedStart = new Date(now);
  schedStart.setHours(sh, sm, 0, 0);
  // If schedStart is in the future but bot was manually started before → use base elapsed
  if (now < schedStart) {
    if (Number.isFinite(startedAtSec) && startedAtSec > 0 && startedAtSec * 1000 < schedStart.getTime()) {
      return { elapsedText: baseElapsed, schedProgress: null };
    }
    return { elapsedText: "waiting", schedProgress: null };
  }
  // If stop_time exists, check if the schedule window is already over
  if (sched.stop_time) {
    const [eh, em] = sched.stop_time.split(":").map(Number);
    const schedEnd = new Date(now);
    schedEnd.setHours(eh, em, 0, 0);
    if (schedEnd <= schedStart) schedEnd.setDate(schedEnd.getDate() + 1);
    if (now >= schedEnd) {
      // Past stop_time → schedule window is over, use normal timer
      return { elapsedText: baseElapsed, schedProgress: null };
    }
    // Between start_time and stop_time → count from schedule start + progress
    const totalMs = schedEnd - schedStart;
    const doneMs = now - schedStart;
    const schedProgress = Math.min(100, Math.max(0, (doneMs / totalMs) * 100));
    const elapsedText = formatSessionElapsed((now.getTime() - schedStart.getTime()) / 1000);
    return { elapsedText, schedProgress };
  }
  // No stop_time → count from schedule start, no progress bar
  const elapsedText = formatSessionElapsed((now.getTime() - schedStart.getTime()) / 1000);
  return { elapsedText, schedProgress: null };
}

function syncActionStatusUi({ running = false, stopInProgress = false, degraded = false, showStopped = false, elapsedText = "--:--:--" } = {}) {
  const row = document.querySelector(".actions-status");
  const badge = document.getElementById("run_state");
  const timerEl = document.getElementById("session_timer");
  const isDegraded = !!(stopInProgress || degraded);
  const live = !!(running || isDegraded);
  const shouldShow = live || !!showStopped;

  if (row) row.classList.toggle("hidden", !shouldShow);

  if (badge) {
    badge.classList.toggle("running", running);
    badge.classList.toggle("degraded", isDegraded && !running);
    badge.classList.toggle("stopped", !running && !isDegraded);
    if (running) badge.textContent = "RUNNING";
    else if (stopInProgress) badge.textContent = "STOPPING";
    else if (isDegraded) badge.textContent = "DEGRADED";
    else badge.textContent = "STOPPED";
  }

  if (timerEl) {
    timerEl.classList.toggle("hidden", !live);
    if (live && elapsedText === "waiting") {
      if (!timerEl.classList.contains("slot-timer-waiting")) {
        timerEl.innerHTML = 'waiting<span class="dot-1">.</span><span class="dot-2">.</span><span class="dot-3">.</span>';
        timerEl.classList.add("slot-timer-waiting");
      }
    } else {
      timerEl.textContent = live ? String(elapsedText || "--:--:--") : "--:--:--";
      timerEl.classList.remove("slot-timer-waiting");
    }
  }
}

function isBotRunning() {
  return anySlotRunning();
}

function updateRiskApplyValidation() {
  const applyRiskBtn = document.getElementById("apply_risk_changes");
  const riskErr = document.getElementById("risk_error");
  const targetVolInput = document.getElementById("target_vol");
  if (!applyRiskBtn || !riskErr || !targetVolInput) return;
  const running = isBotRunning();
  // Lock max_loss inputs while any bot is running (value is set at session start)
  const maxLossNum = document.getElementById("max_loss");
  const maxLossRange = document.getElementById("max_loss_range");
  if (maxLossNum) maxLossNum.disabled = running;
  if (maxLossRange) maxLossRange.disabled = running;
  if (!running) {
    applyRiskBtn.disabled = false;
    riskErr.textContent = "";
    riskErr.classList.add("hidden");
    return;
  }
  const targetVolVal = _parseFormattedNum(targetVolInput.value);
  const sessionVolDone = Number(currentSessionVolumeUsd) || 0;
  const invalid = Number.isFinite(targetVolVal) && targetVolVal < sessionVolDone;
  if (invalid) {
    applyRiskBtn.disabled = true;
    riskErr.textContent = `Target volume ($${targetVolVal.toFixed(0)}) cannot be lower than volume already done ($${sessionVolDone.toFixed(0)}).`;
    riskErr.classList.remove("hidden");
  } else {
    applyRiskBtn.disabled = false;
    riskErr.textContent = "";
    riskErr.classList.add("hidden");
  }
}

function pickStopReason(statusLike) {
  if (!statusLike || typeof statusLike !== "object") return "";
  const ignored = new Set(["stopped", "process_exited", "keyboard_interrupt", "manual"]);
  const reasons = [
    statusLike.last_stop_reason,
    statusLike.halt_reason,
    statusLike.stop_reason,
  ];
  for (const reason of reasons) {
    const r = String(reason || "").trim();
    if (r && !ignored.has(r)) return r;
  }
  return String(reasons.find((reason) => String(reason || "").trim()) || "").trim();
}

function stopReasonNoticeText(reason, subject = "Your bot") {
  const r = String(reason || "").trim();
  if (!r || r === "stopped" || r === "process_exited" || r === "keyboard_interrupt" || r === "manual") return "";
  if (r === "admin_stop_all") {
    return `${subject} has been stopped due to a fix update. Please restart it.`;
  }
  if (r === "server_shutdown") {
    return `${subject} was stopped because the server restarted; positions were closed.`;
  }
  if (r === "schedule") {
    return `${subject} reached its scheduled stop time.`;
  }
  if (r === "hip4_pre_settle") {
    return `${subject} was stopped before HIP-4 settlement and positions were flattened.`;
  }
  if (r.startsWith("watchdog:") || r === "watchdog_stale_heartbeat" || r.startsWith("orphan_heartbeat_stale:")) {
    return `${subject} was stopped by the watchdog (heartbeat went stale). Restart to retry.`;
  }
  if (r.startsWith("risk_halt:")) {
    return `${subject} was stopped by risk limits (${r.slice("risk_halt:".length)}).`;
  }
  if (r.startsWith("risk_watchdog_max_position:")) {
    return `${subject} was stopped by risk limits (max position ${r.slice("risk_watchdog_max_position:".length)}).`;
  }
  if (r.startsWith("risk_watchdog_max_session_loss:")) {
    return `${subject} was stopped by risk limits (max session loss ${r.slice("risk_watchdog_max_session_loss:".length)}).`;
  }
  if (r.startsWith("risk_watchdog_halt:")) {
    return `${subject} was stopped by risk limits (${r.slice("risk_watchdog_halt:".length)}).`;
  }
  if (r.startsWith("target_session_volume_reached:")) {
    return `${subject}: target volume reached.`;
  }
  if (r.startsWith("init_crash:")) {
    return `${subject} failed to start: ${r.slice("init_crash:".length)}.`;
  }
  if (r.startsWith("process_exit:")) {
    return `${subject} crashed (exit code ${r.slice("process_exit:".length)}).`;
  }
  if (r.includes("hl_rate_limit_exhausted")) {
    return `${subject} stopped: HL cumulative request limit reached. Place a few taker orders to free up the quota, then restart.`;
  }
  if (r.includes("adapter_timeout")) {
    return `${subject} stopped: too many consecutive adapter timeouts (network/proxy issue). Please retry.`;
  }
  if (r.includes("too_many_consecutive_cycle_errors")) {
    return `${subject} stopped: too many consecutive errors. Check logs and retry.`;
  }
  if (r.startsWith("fatal:")) {
    return `${subject} stopped: ${r.slice("fatal:".length)}.`;
  }
  return `${subject} stopped: ${r}`;
}

function updateAdaptiveRegimeBanner(data) {
  // Affiche le régime (chop/trend) du mode Adaptive dans le dashboard.
  // Source : payload /api/status (champ adaptive_regime, slot 0 ou per-slot).
  const banner = document.getElementById("adaptive_regime_banner");
  if (!banner) return;
  const slots = Array.isArray(data?.slots) ? data.slots : [];
  const src = slots.find(s => s && s.adaptive_regime && (s.running || s.process_running))
    || ((data?.adaptive_regime && (data.running || data.process_running)) ? data : null);
  const reg = src?.adaptive_regime;
  if (!reg || !reg.regime) {
    banner.classList.add("hidden");
    return;
  }
  const isTrend = String(reg.regime).toLowerCase() === "trend";
  const chip = document.getElementById("adaptive_regime_chip");
  if (chip) {
    chip.textContent = isTrend ? "TREND" : "CHOP";
    chip.classList.toggle("trend", isTrend);
  }
  const detail = document.getElementById("adaptive_regime_detail");
  if (detail) {
    const parts = [];
    if (reg.symbol) parts.push(reg.symbol);
    const score = Number(reg.trend_score);
    if (Number.isFinite(score)) parts.push(`trend score ${score.toFixed(2)}`);
    const atr = Number(reg.atr_bps);
    if (Number.isFinite(atr)) parts.push(`ATR ${atr.toFixed(1)} bps`);
    parts.push(isTrend ? "flatten-only, quoting paused" : "quoting the value area");
    detail.textContent = parts.join(" · ");
  }
  banner.classList.remove("hidden");
}

async function refreshStatus() {
  // Avoid background polling when landing is visible or the tab is hidden.
  if (!document.body.classList.contains("main-space") || document.hidden) return;
  if (!Object.keys(walletAuthTokens).length && !walletAuthToken) return;
  if (statusInFlight) return;
  statusInFlight = true;
  try {
    // Collect unique addresses with valid tokens
    const addrTokens = new Map();
    for (const dex of Object.keys(_dexState)) {
      const addr = (_dexState[dex]?.address || "").toLowerCase();
      if (addr && walletAuthTokens[addr] && !addrTokens.has(addr)) {
        addrTokens.set(addr, walletAuthTokens[addr]);
      }
    }
    // Fallback: use any available token
    if (addrTokens.size === 0) {
      if (walletAuthToken && walletAuthAccount) {
        addrTokens.set(walletAuthAccount, walletAuthToken);
      } else {
        // Last resort: use first token in the map
        for (const [addr, token] of Object.entries(walletAuthTokens)) {
          addrTokens.set(addr, token);
          break;
        }
      }
    }

    // Fetch status per address in parallel, merge slots
    let allApiSlots = [];
    let firstData = null;
    let anyFetchSucceeded = false;
    const statusResults = await Promise.allSettled(
      [...addrTokens.entries()].map(async ([addr, token]) => {
        const res = await fetchWithTimeout("/api/status", {
          headers: { "X-Mega-Auth": token }
        }, API_STATUS_TIMEOUT_MS);
        if (!res.ok) {
          if (res.status === 401) {
            // Pas de wipe direct : un 401 transitoire (redeploy, warmup serveur)
            // ne doit pas déconnecter l'UI d'une session qui tourne serveur-side.
            // Même pattern que apiGet/apiPost : grâce de boot, puis confirmation
            // serveur — clear uniquement sur authenticated:false explicite.
            if (!_isAuthBootGrace()) {
              _confirmAuthInvalidThenClear(addr);
            } else {
              console.warn("[auth] 401 during boot grace, keeping session: /api/status", addr);
            }
          }
          return null;
        }
        return res.json();
      })
    );
    for (const r of statusResults) {
      if (r.status !== "fulfilled" || !r.value) continue;
      anyFetchSucceeded = true;
      const data = r.value;
      if (!firstData) firstData = data;
      const apiSlots = Array.isArray(data.slots) ? data.slots : [];
      allApiSlots.push(...apiSlots);
      if (data.schedule && !firstData.schedule) firstData.schedule = data.schedule;
    }

    // If every status fetch failed (network blip, server overload, mid-deploy),
    // do NOT wipe `runningSlots`. The bot is still running server-side; clearing
    // the UI to "no active sessions" makes the user think it stopped and is the
    // root cause of "I don't see my session anymore" reports during transient
    // failures or reconnect flows. Keep the previous render and let the next
    // poll reconcile.
    if (!anyFetchSucceeded) {
      return;  // `finally` clears statusInFlight
    }

    const data = firstData || {};
    data.slots = allApiSlots;

    updateAdaptiveRegimeBanner(data);

    // ── Parse multi-slot status into runningSlots ──
    const apiSlots = Array.isArray(data.slots) ? data.slots : [];
    const newRunning = [];
    if (apiSlots.length > 0) {
      apiSlots.forEach(as => {
        const running = !!as.running;
        const degraded = !!as.degraded || !!as.stop_in_progress;
        const standby = !!as.standby;
        if (!running && !degraded && !standby) return;
        if (standby && !running) {
          newRunning.push({
            slot: Number(as.slot ?? 0),
            symbol: as.symbol || "",
            dex: as.dex || "",
            running: false, degraded: false, standby: true, paused: false,
            elapsedText: "waiting", schedProgress: null, sessionPnl: null,
          });
          return;
        }
        const slotId = Number(as.slot ?? 0);
        const startedAtSec = parseFloat(String(as.started_at || ""));
        let elapsed, schedProgress = null;
        if (slotId === 0) {
          const sa = _scheduleAwareElapsed(startedAtSec);
          elapsed = sa.elapsedText;
          schedProgress = sa.schedProgress;
        } else {
          elapsed = (Number.isFinite(startedAtSec) && startedAtSec > 0)
            ? formatSessionElapsed((Date.now() / 1000) - startedAtSec)
            : "--:--:--";
        }
        newRunning.push({
          slot: slotId,
          symbol: as.symbol || "",
          dex: as.dex || "",
          running,
          degraded,
          paused: !!as.paused,
          sessionPnl: null,
          elapsedText: elapsed,
          schedProgress,
          // Effective trading address of the live session ("" = main account).
          // undefined when the backend doesn't surface it (pre-upgrade payload).
          tradingAccount: typeof as.trading_account === "string" ? as.trading_account : undefined,
        });
      });
    } else if (data.running || data.degraded || data.stop_in_progress) {
      // Legacy fallback
      const startedAtSec = parseFloat(String(data.started_at || ""));
      const sa = _scheduleAwareElapsed(startedAtSec);
      newRunning.push({
        slot: 0,
        symbol: data.symbol || "",
        running: !!data.running,
        degraded: !!data.degraded || !!data.stop_in_progress,
        paused: !!data.paused,
        sessionPnl: null,
        elapsedText: sa.elapsedText,
        schedProgress: sa.schedProgress,
      });
    }
    // Preserve metrics data from previous poll (set by refreshMetrics)
    newRunning.forEach(ns => {
      const prev = runningSlots.find(p => p.slot === ns.slot);
      if (prev) {
        if (typeof prev.sessionPnl === "number") ns.sessionPnl = prev.sessionPnl;
        if (typeof prev.sessionVolume === "number") ns.sessionVolume = prev.sessionVolume;
        if (typeof prev.targetVolume === "number") ns.targetVolume = prev.targetVolume;
        // Keep the launch-time vault while the API payload doesn't surface it yet.
        if (ns.tradingAccount === undefined && typeof prev.tradingAccount === "string") ns.tradingAccount = prev.tradingAccount;
      }
    });
    // Keep optimistic slots (just started, API not yet reporting running) for a grace period
    // Surface the explicit stop reason when a running/optimistic slot disappears.
    runningSlots.forEach(prev => {
      if (!prev.running && !prev.degraded) return;
      const inNew = newRunning.find(n => n.slot === prev.slot);
      if (!inNew) {
        // Slot disappeared: prefer last_stop_reason because runtime stop_reason is often just "stopped".
        const apiSlot = apiSlots.find(s => Number(s.slot ?? 0) === prev.slot);
        const haltReason = pickStopReason(apiSlot);
        const msg = stopReasonNoticeText(haltReason, _slotDisplayToken(prev.symbol));
        if (msg) {
          // Surface controlled stops as well as crashes when a running slot disappears.
          showError("main_error", msg);
        }
        // Keep optimistic entry if still within grace period
        if (prev._optimisticUntil && Date.now() < prev._optimisticUntil) {
          newRunning.push(prev);
        }
      }
    });
    runningSlots = newRunning;

    const isAnyRunning = anySlotRunning();
    if (isAnyRunning && !Number.isFinite(activeSessionTargetVolumeUsd)) {
      const initialTarget = _parseFormattedNum(document.getElementById("target_vol")?.value);
      if (Number.isFinite(initialTarget) && initialTarget > 0) {
        activeSessionTargetVolumeUsd = initialTarget;
        persistActiveSessionTargetVolume(initialTarget);
      }
    }
    if (!isAnyRunning) {
      activeSessionTargetVolumeUsd = null;
      persistActiveSessionTargetVolume(null);
      _globalTargetStopTriggered = false;
    }

    // Capture schedule from status response
    if (data.schedule) {
      _currentSchedule = data.schedule;
    } else if (_currentSchedule === null) {
      _currentSchedule = { enabled: false, mode: "oneshot", start_time: "", stop_time: "" };
    }
    _syncScheduleUI();

    // Sync quote_mode from active slot status - sauf si l'utilisateur est en train
    // de choisir un mode (sélection non sauvegardée) : ne pas l'écraser.
    if (newRunning.length > 0 && !quoteModeUserTouched) {
      const activeSlot = apiSlots.find(s => s.slot === newRunning[0].slot);
      if (activeSlot && activeSlot.quote_mode && activeSlot.quote_mode !== selectedQuoteMode) {
        applyQuoteMode(activeSlot.quote_mode, { silent: true });
      }
    }

    renderRunningSlots();
    updateRiskApplyValidation();
  } catch (e) {
    if (e && e.status === 401) {
      showError("main_error", "Session expired. Reconnect your wallet to manage the bot.");
    }
  } finally {
    statusInFlight = false;
  }
}

async function refreshLogs() {
  try {
    const rs = _getRunningSlot(_currentSymbol());
    const logSlot = rs ? rs.slot : 0;
    const data = await apiGet(`/api/logs?slot=${logSlot}`);
    const logs = document.getElementById("logs");
    logs.textContent = data.lines.join("\n");
    logs.scrollTop = logs.scrollHeight;
  } catch (e) {
    // ignore
  }
}

async function refreshMetrics() {
  // Avoid expensive metrics polling when landing is visible or the tab is hidden.
  if (!document.body.classList.contains("main-space") || document.hidden) return;
  if (!walletAuthToken) return;
  if (metricsInFlight) return;
  metricsInFlight = true;
  try {
    const source = document.getElementById("market_source")?.value || "top20";
    const slot = activeDexSlot();
    const exchangeMode = document.getElementById("mode")?.value || "";
    const subaccount = getSelectedTradingAccount();
    const extIdx = isExtendedMode(exchangeMode) ? getExtendedAccountIndex() : 0;
    const data = await apiGet(`/api/metrics?market_source=${encodeURIComponent(source)}&slot=${slot}&mode=${encodeURIComponent(exchangeMode)}&subaccount=${encodeURIComponent(subaccount)}&account_index=${extIdx}`);
    const summary = data.summary || {};
    const availableEl = document.getElementById("available_notional");
    const availableRaw = typeof summary.availableNotional === "string"
      ? parseFloat(summary.availableNotional)
      : summary.availableNotional;
    const accountRaw = typeof summary.accountValue === "string"
      ? parseFloat(summary.accountValue)
      : summary.accountValue;
    const effectiveAvailable = Number.isFinite(availableRaw)
      ? availableRaw
      : Number.isFinite(accountRaw)
        ? accountRaw
        : null;
    const metricsCacheKey = getMetricsCacheKey(source);
    metricsCache.set(metricsCacheKey, { availableNotional: Number.isFinite(effectiveAvailable) ? effectiveAvailable : null, ts: Date.now() });
    // Re-read source and slot after async gap : discard if DEX or source changed during fetch.
    const liveSourceMetrics = document.getElementById("market_source")?.value || "top20";
    const liveSlot = activeDexSlot();
    if (liveSlot !== slot) return;
    if (liveSourceMetrics === source) {
      const prevPositive = Number.isFinite(latestAvailableNotional) && latestAvailableNotional > 0.01;
      latestAvailableNotional = Number.isFinite(effectiveAvailable) ? effectiveAvailable : null;
      // During the grace window after connect/source-switch, a 0/unknown reading is
      // almost always an unresolved first fetch (the xyz/km sub-dex clearinghouse
      // query is slow/flaky), not a real empty balance. Keep the spinner instead of
      // flashing "0.00" until a positive value arrives or the grace window elapses
      // (after which a genuine 0 shows, along with the deposit warning below).
      const valueStable = (Date.now() - lastSourceChangedAt) > 5000;
      const lowOrUnknown = !Number.isFinite(effectiveAvailable) || effectiveAvailable <= 0.01;
      if (lowOrUnknown && !valueStable && !prevPositive) {
        setNotionalLoading(availableEl);
      } else {
        setNotionalValue(availableEl, effectiveAvailable);
      }
    }
    const warningEl = document.getElementById("account_create_warning");
    if (warningEl) {
      const isLow = !Number.isFinite(effectiveAvailable) || effectiveAvailable <= 0.01;
      // Re-read source after async response to avoid stale-closure mismatch.
      const liveSource = document.getElementById("market_source")?.value || "top20";
      // Grace period: don't show warning for 5s after a source switch (avoids flicker while first fetch is in flight).
      const sourceStable = (Date.now() - lastSourceChangedAt) > 5000;
      // When the real blocker is a missing/revoked approval, don't mislead the user
      // into thinking they need to deposit funds. Surface the re-sign step instead.
      const risexVenue = isRisexMode();
      const qfexVenue = isQfexMode();
      const approvalsMissing = isNadoMode()
        ? !nadoAgentApprovedForAccount
        : qfexVenue
          ? !qfexCredentialsReadyForAccount
        : isExtendedMode()
          ? (!builderApprovedForAccount || !extendedCredentialsReadyForAccount)
          : risexVenue
            ? (!risexBuilderApprovedForAccount || !risexSignerReadyForAccount)
            : (!builderApprovedForAccount || !agentApprovedForAccount);
      if (isLow && sourceStable && !isBotRunning() && approvalsMissing && qfexVenue) {
        warningEl.textContent = "Connect QFEX (paste a trade-only API key) to trade.";
        warningEl.classList.remove("hidden");
      } else if (isLow && sourceStable && !isBotRunning() && qfexVenue) {
        warningEl.innerHTML = `You need to deposit <strong>USDC</strong> on <a href="${QFEX_KEYS_URL}" target="_blank" rel="noopener noreferrer">qfex.com</a> first.`;
        warningEl.classList.remove("hidden");
      } else if (isLow && sourceStable && !isBotRunning() && approvalsMissing && risexVenue) {
        warningEl.textContent = "Complete the RISEx connection (builder fee and bot signer) to trade.";
        warningEl.classList.remove("hidden");
      } else if (isLow && sourceStable && !isBotRunning() && approvalsMissing) {
        warningEl.innerHTML = `Re-sign your agent / builder approvals to trade.`;
        warningEl.classList.remove("hidden");
      } else if (isLow && sourceStable && !isBotRunning() && risexVenue) {
        warningEl.innerHTML = `You need to deposit <strong>USDC</strong> on <a href="${RISEX_DEPOSIT_URL}" target="_blank" rel="noopener noreferrer">rise.trade</a> first.`;
        warningEl.classList.remove("hidden");
      } else if (isLow && sourceStable && !isBotRunning() && liveSource === "xyz") {
        warningEl.innerHTML = `You need to create your account on <a href="https://app.trade.xyz/?market=XYZ100" target="_blank" rel="noopener noreferrer">Trade.xyz</a> first, then transfer your USDC to <strong>USDC (xyz) Perps</strong>.`;
        warningEl.classList.remove("hidden");
      } else if (isLow && sourceStable && !isBotRunning() && liveSource === "io") {
        warningEl.innerHTML = `Without Unified Account mode, transfer your USDC to <strong>EntropyIO Perps</strong> on <a href="https://app.hyperliquid.xyz" target="_blank" rel="noopener noreferrer">app.hyperliquid.xyz</a> first. EntropyIO markets are isolated margin only.`;
        warningEl.classList.remove("hidden");
      } else if (isLow && sourceStable && !isBotRunning() && liveSource === "hip3") {
        warningEl.innerHTML = `You need to create your account on <a href="https://markets.xyz/join/brokx" target="_blank" rel="noopener noreferrer">Kinetiq Markets</a> first.`;
        warningEl.classList.remove("hidden");
      } else if (isLow && sourceStable && !isBotRunning() && isNadoMode()) {
        warningEl.innerHTML = `You need to deposit <strong>USDT</strong> on <a href="https://app.nado.xyz" target="_blank" rel="noopener noreferrer">Nado</a> (Ink L2) first. Bridge USDT0 to Ink, then deposit on Nado.`;
        warningEl.classList.remove("hidden");
      } else {
        warningEl.innerHTML = "";
        warningEl.classList.add("hidden");
      }
    }
    const notionalInput = document.getElementById("session_notional");
    if (notionalInput && Number.isFinite(latestAvailableNotional) && latestAvailableNotional > 0) {
      notionalInput.max = String(Math.round(latestAvailableNotional * 100) / 100);
    }
    const notionalMaxBtn = document.getElementById("session_notional_max");
    if (notionalMaxBtn) {
      notionalMaxBtn.disabled = !(Number.isFinite(latestAvailableNotional) && latestAvailableNotional > 0);
    }

    const session = data.session || {};
    const sessionActive = !!session.active;
    renderRisexQuota(session);
    renderQfexRateBudget(session);
    // Bandeau d'expiration du signer RISEx : statut relu au plus toutes les 60 s.
    if (isDexConnected("risex") && _dexState.risex.address) {
      void refreshRisexStatus({ account: _dexState.risex.address }).catch(() => {});
    }

    // Show restart/stop notice
    const restartNotice = document.getElementById("restart_notice");
    if (restartNotice) {
      const lastStopReason = pickStopReason(session) || pickStopReason(data);
      const stopNoticeText = stopReasonNoticeText(lastStopReason, "Your bot");
      if (sessionActive) {
        restartNotice.classList.add("hidden");
      } else if (session.server_restarted) {
        restartNotice.textContent = "The server has restarted \u2014 your bot was stopped and positions were closed.";
        restartNotice.classList.remove("hidden");
      } else if (stopNoticeText) {
        restartNotice.textContent = stopNoticeText;
        restartNotice.classList.remove("hidden");
      } else {
        restartNotice.classList.add("hidden");
      }
    }
    const strategyPnlRaw =
      typeof session.strategy_pnl_usd === "string" ? parseFloat(session.strategy_pnl_usd) : session.strategy_pnl_usd;
    const kpiNetPnlRaw = session.kpi && typeof session.kpi.net_pnl === "string"
      ? parseFloat(session.kpi.net_pnl)
      : session.kpi?.net_pnl;
    const kpiFillsCount = Number(
      typeof session.kpi?.fills_count === "string" ? parseFloat(session.kpi.fills_count) : (session.kpi?.fills_count || 0)
    );
    const sessionDisplayPnl =
      typeof session.display_pnl_usd === "string" ? parseFloat(session.display_pnl_usd) : session.display_pnl_usd;
    const sessionFallbackPnl =
      typeof session.pnl_total_usd === "string" ? parseFloat(session.pnl_total_usd) : session.pnl_total_usd;
    const accountPnlRaw =
      typeof session.account_pnl_usd === "string" ? parseFloat(session.account_pnl_usd) : session.account_pnl_usd;
    const unrealizedPnl = (data.positions || []).reduce((sum, p) => {
      const v = typeof p.pnl === "string" ? parseFloat(p.pnl) : p.pnl;
      return sum + (Number.isFinite(v) ? v : 0);
    }, 0);
    const tracker = trackedSessionPnl(session, data.positions || []);
    const pnlEl = document.getElementById("stat_session_pnl");
    const volEl = document.getElementById("stat_session_volume");
    const targetVolRaw = _parseFormattedNum(document.getElementById("target_vol")?.value);
    const targetVolDraftNum = Number.isFinite(targetVolRaw) && targetVolRaw > 0 ? targetVolRaw : null;
    const sessionVolNum =
      typeof session.session_volume_usd === "string" ? parseFloat(session.session_volume_usd) : session.session_volume_usd;
    const engineVolNum =
      typeof session.engine_volume_usd === "string" ? parseFloat(session.engine_volume_usd) : session.engine_volume_usd;
    const kpiVolNumRaw = session.kpi && typeof session.kpi.fill_notional_usd === "string"
      ? parseFloat(session.kpi.fill_notional_usd)
      : session.kpi?.fill_notional_usd;
    let effectiveVolNum = null;
    if (Number.isFinite(sessionVolNum) && Number.isFinite(engineVolNum)) {
      effectiveVolNum = Math.max(sessionVolNum, engineVolNum);
    } else if (Number.isFinite(sessionVolNum)) {
      effectiveVolNum = sessionVolNum;
    } else if (Number.isFinite(engineVolNum)) {
      effectiveVolNum = engineVolNum;
    } else if (Number.isFinite(kpiVolNumRaw)) {
      effectiveVolNum = kpiVolNumRaw;
    }
    if (!Number.isFinite(effectiveVolNum) && sessionActive) {
      effectiveVolNum = 0;
    }
    currentSessionVolumeUsd = Number.isFinite(effectiveVolNum) ? effectiveVolNum : 0;
    // Sync target volume from server when session is active (covers Telegram-started sessions)
    const serverTargetVol = _parseNum(session.target_session_volume_usd);
    if (sessionActive && Number.isFinite(serverTargetVol) && serverTargetVol > 0) {
      activeSessionTargetVolumeUsd = serverTargetVol;
    }
    const targetVolNum = sessionActive && Number.isFinite(activeSessionTargetVolumeUsd)
      ? Number(activeSessionTargetVolumeUsd)
      : targetVolDraftNum;
    // Platform fees: 1 bps on session volume (always displayed when volume > 0)
    const backendProjectFeesRaw =
      typeof session.project_fees_usd === "string" ? parseFloat(session.project_fees_usd) : session.project_fees_usd;
    const backendPlatformFeesRaw =
      typeof session.platform_fees_usd === "string" ? parseFloat(session.platform_fees_usd) : session.platform_fees_usd;
    const platformFeesUsd = Number.isFinite(backendProjectFeesRaw)
      ? backendProjectFeesRaw
      : Number.isFinite(backendPlatformFeesRaw)
        ? backendPlatformFeesRaw
        : platformFeesUsdFromVolume(effectiveVolNum);
    const tradoorFeesEl = document.getElementById("stat_tradoor_fees");
    if (tradoorFeesEl) {
      const feesAbs = Math.abs(platformFeesUsd);
      tradoorFeesEl.textContent = Number.isFinite(effectiveVolNum) && effectiveVolNum > 0 ? `−${formatUsdPlain(feesAbs)}` : "-";
    }
    // Session PnL cumulative (realized + unrealized):
    // realized is cumulative from bot fills (strategy/kpi), so it does not reset after close.
    // add current unrealized from open positions.
    const isHyperliquidSession = String(session.mode || "").toLowerCase().includes("hyperliquid");
    let realizedPnl = NaN;
    const strategyCandidate = Number.isFinite(strategyPnlRaw)
      ? strategyPnlRaw
      : Number.isFinite(kpiNetPnlRaw)
        ? kpiNetPnlRaw
        : NaN;
    // Use strategy/kpi as realized source when available (Hyperliquid only).
    // For Extended/other venues, account delta is the most reliable source since
    // real orders don't emit FillEvents, so kpi.net_pnl stays at 0 or contains
    // only estimated spread PnL which is not accurate.
    if (!isHyperliquidSession && Number.isFinite(accountPnlRaw)) {
      // Extended/other venues: account delta is the primary PnL source.
      realizedPnl = accountPnlRaw;
    } else if (isHyperliquidSession && Number.isFinite(strategyCandidate) && Math.abs(strategyCandidate) > 1e-9) {
      realizedPnl = strategyCandidate;
    } else if (Number.isFinite(strategyCandidate)) {
      realizedPnl = strategyCandidate;
    }
    let pnlCardValue = NaN;
    const directionBias = parseInt(session.direction_bias, 10) || 0;
    // WebSocket PnL (real-time sum of closedPnl + unrealized) : highest priority when available.
    const wsPnlRaw = typeof session.ws_pnl_usd === "string" ? parseFloat(session.ws_pnl_usd) : session.ws_pnl_usd;
    const wsFillsCount = Number(session.ws_fills_count || 0);
    // Server-computed net PnL (uses priority cascade: onchain_fills > ws > engine > account > fallback > strategy)
    const serverPnlNet = typeof session.pnl_total_usd_net === "string" ? parseFloat(session.pnl_total_usd_net) : session.pnl_total_usd_net;
    const pnlSource = session.pnl_source || "none";
    if ((pnlSource === "onchain_fills" || pnlSource === "risex_fills" || pnlSource === "qfex_fills") && Number.isFinite(serverPnlNet)) {
      // risex_fills : somme des realized_pnl des trades RISEx de la session + latent.
      // On-chain realized PnL (sum of closedPnl from REST): authoritative, highest priority.
      pnlCardValue = serverPnlNet;
    } else if (Number.isFinite(serverPnlNet) && pnlSource === "websocket" && wsFillsCount > 0) {
      // Use server-computed fee-adjusted PnL (pure trading perf, builder fee removed)
      pnlCardValue = serverPnlNet;
    } else if (Number.isFinite(wsPnlRaw) && wsFillsCount > 0) {
      // Fallback to raw WS PnL if server net not available yet
      pnlCardValue = wsPnlRaw;
    } else if (pnlSource === "nado" && Number.isFinite(serverPnlNet)) {
      // Nado: equity delta from gateway polling : trust server PnL directly
      pnlCardValue = serverPnlNet;
    } else if ((pnlSource === "engine" || pnlSource === "account") && Number.isFinite(serverPnlNet) && kpiFillsCount > 0) {
      // Trust server-computed PnL when engine has fills (realized+unrealized tracking)
      pnlCardValue = serverPnlNet;
    } else if (isHyperliquidSession && directionBias === 0) {
      // Neutral MM fallback: tracker accumulates realized from position transitions (flip through 0).
      pnlCardValue = tracker.total;
    } else if (Number.isFinite(accountPnlRaw)) {
      // Direction bias, Extended, or non-HL: equity delta (already includes unrealized).
      pnlCardValue = accountPnlRaw;
    } else if (Number.isFinite(realizedPnl)) {
      pnlCardValue = realizedPnl + unrealizedPnl;
    }
    // Fallbacks if primary sources are temporarily unavailable. Le net serveur
    // passe avant le brut : le brut ignore les frais on-chain et affiche une
    // perte 2 à 5 fois plus petite que celle réellement subie.
    if (!Number.isFinite(pnlCardValue) && Number.isFinite(serverPnlNet)) {
      pnlCardValue = serverPnlNet;
    }
    if (!Number.isFinite(pnlCardValue) && Number.isFinite(sessionFallbackPnl)) {
      pnlCardValue = sessionFallbackPnl;
    }
    if (!Number.isFinite(pnlCardValue) && Number.isFinite(accountPnlRaw)) {
      pnlCardValue = accountPnlRaw;
    }
    if (!Number.isFinite(pnlCardValue) && sessionActive) {
      pnlCardValue = kpiFillsCount > 0 ? unrealizedPnl : 0;
    }
    // Hard reset at session start: until first traded volume/fill, keep displayed session PnL at 0.
    // Also check wsFillsCount : WS may detect fills before polling catches up.
    // Skip reset for Nado: equity delta is valid even before engine reports fills.
    // Only reset when the payload actually carries volume info (present and zero):
    // a partial payload with the volume fields missing (effectiveVolNum coerced to 0
    // above) must not wipe a real PnL down to 0 for one poll.
    const volKnown = Number.isFinite(sessionVolNum) || Number.isFinite(engineVolNum) || Number.isFinite(kpiVolNumRaw);
    if (
      sessionActive
      && pnlSource !== "nado"
      && volKnown
      && effectiveVolNum <= 0
      && (!Number.isFinite(kpiFillsCount) || kpiFillsCount <= 0)
      && wsFillsCount <= 0
    ) {
      pnlCardValue = 0;
    }
    if (pnlEl) {
      const pnlVal = pnlCardValue;
      pnlEl.textContent = formatUsd(pnlVal);
      pnlEl.classList.toggle("pnl-pos", pnlVal > 0);
      pnlEl.classList.toggle("pnl-neg", pnlVal < 0);
    }
    if (volEl) {
      if (Number.isFinite(effectiveVolNum) && targetVolNum) {
        volEl.textContent = `${formatCompactVolume(effectiveVolNum)} / ${formatCompactVolume(targetVolNum)}`;
      } else if (Number.isFinite(effectiveVolNum)) {
        volEl.textContent = `${formatCompactVolume(effectiveVolNum)} / -`;
      } else {
        volEl.textContent = "-";
      }
    }
    const ppmEl = document.getElementById("stat_pnl_per_million");
    let ppmComputed = null;
    if (ppmEl) {
      const pnlNum = pnlCardValue;
      const volNum = effectiveVolNum;
      if (Number.isFinite(pnlNum) && Number.isFinite(volNum) && volNum > 0) {
        const ppm = (pnlNum / volNum) * 100000.0;
        ppmComputed = ppm;
        ppmEl.textContent = `${ppm >= 0 ? "+" : ""}${ppm.toFixed(2)} $ / $100k`;
        ppmEl.classList.toggle("pnl-pos", ppm > 0);
        ppmEl.classList.toggle("pnl-neg", ppm < 0);
      } else if (sessionActive && Number.isFinite(pnlNum)) {
        ppmEl.textContent = "+0.00 $ / $1M";
        ppmComputed = 0;
        ppmEl.classList.remove("pnl-pos");
        ppmEl.classList.remove("pnl-neg");
      } else {
        ppmEl.textContent = "-";
        ppmEl.classList.remove("pnl-pos");
        ppmEl.classList.remove("pnl-neg");
      }
    }
    updateRiskApplyValidation();

    // ── Multi-slot: parse per-slot PnL from data.sessions ──
    const allSessions = Array.isArray(data.sessions) ? data.sessions : [];
    let multiSlotTotalPnl = 0;
    let multiSlotTotalVol = 0;
    let multiSlotCount = 0;
    const perTokenPnl = [];
    if (allSessions.length > 0) {
      const activeSessionsCount = allSessions.filter(s => !!s.active).length;
      allSessions.forEach(sess => {
        const slotId = Number(sess.slot ?? 0);
        const active = !!sess.active;
        // account_pnl_usd is the whole-account delta : only meaningful for single-slot.
        // With 2+ active slots it would count the whole account once per slot (double
        // counting in the summed total), so it is excluded as a fallback there.
        const displayPnl = _parseNum(sess.display_pnl_usd) ?? _parseNum(sess.pnl_total_usd)
          ?? (activeSessionsCount > 1 ? null : _parseNum(sess.account_pnl_usd));
        const vol = _parseNum(sess.session_volume_usd) ?? _parseNum(sess.engine_volume_usd) ?? 0;
        const targetVol = _parseNum(sess.target_session_volume_usd);
        // Update sessionPnl, sessionVolume, targetVolume on the running slot
        const rs = runningSlots.find(s => s.slot === slotId);
        if (rs) {
          rs.sessionPnl = active && Number.isFinite(displayPnl) ? displayPnl : null;
          rs.sessionVolume = active && Number.isFinite(vol) ? vol : null;
          rs.targetVolume = Number.isFinite(targetVol) && targetVol > 0 ? targetVol : null;
        }
        if (active) {
          multiSlotCount++;
          if (Number.isFinite(displayPnl)) multiSlotTotalPnl += displayPnl;
          if (Number.isFinite(vol)) multiSlotTotalVol += vol;
          perTokenPnl.push({ symbol: sess.symbol || "?", pnl: displayPnl || 0, vol: vol || 0 });
        }
      });
      // Single active slot: sync its card PnL with the dashboard's computed value
      // (tracker-based for HL, more accurate than server's display_pnl_usd)
      if (multiSlotCount === 1 && Number.isFinite(pnlCardValue)) {
        const singleSlot = runningSlots.find(s => s.running || s.degraded);
        if (singleSlot) singleSlot.sessionPnl = pnlCardValue;
      }
      if (multiSlotCount > 1) {
        // Dashboard total = sum of per-slot PnL. pnlCardValue only reflects the
        // active slot's session (/api/metrics main session), so using it here
        // under-reported the aggregate whenever 2+ slots were running.
        const dashboardPnl = multiSlotTotalPnl;
        if (pnlEl) {
          pnlEl.textContent = formatUsd(dashboardPnl);
          pnlEl.classList.toggle("pnl-pos", dashboardPnl > 0);
          pnlEl.classList.toggle("pnl-neg", dashboardPnl < 0);
        }
        const globalTarget = Number.isFinite(activeSessionTargetVolumeUsd) && activeSessionTargetVolumeUsd > 0
          ? activeSessionTargetVolumeUsd : null;
        if (volEl) {
          volEl.textContent = globalTarget
            ? `${formatCompactVolume(multiSlotTotalVol)} / ${formatCompactVolume(globalTarget)}`
            : `${formatCompactVolume(multiSlotTotalVol)} / -`;
        }
        // Auto-stop all if global total target volume reached
        if (globalTarget && multiSlotTotalVol >= globalTarget && !_globalTargetStopTriggered) {
          _globalTargetStopTriggered = true;
          console.log(`[GLOBAL TARGET] Total volume ${multiSlotTotalVol} >= target ${globalTarget}, stopping all slots`);
          _stopAllSlots();
        }
      }
    }
    // Per-token PnL breakdown disabled for now
    // _renderPerTokenBreakdown(perTokenPnl);
    renderRunningSlots();

    const positions = document.getElementById("positions");
    if (positions) {
      const frag = document.createDocumentFragment();
      (data.positions || []).forEach((p) => {
        const row = document.createElement("div");
        row.className = "row";
        const pnlVal = p.pnl ?? "-";
        const pnlNum = typeof pnlVal === "string" ? parseFloat(pnlVal) : pnlVal;
        const pnlClass = pnlNum > 0 ? "pnl-pos" : pnlNum < 0 ? "pnl-neg" : "";
        row.innerHTML = `
          <div>${escapeHtml(_slotDisplayToken(p.coin || "-"))}</div>
          <div>${escapeHtml(p.szi ?? "-")}</div>
          <div>${escapeHtml(p.entryPx ?? "-")}</div>
          <div>${escapeHtml(p.liqPx ?? "-")}</div>
          <div class="${pnlClass}">${escapeHtml(pnlVal)}</div>
          <div>${escapeHtml(p.value ?? "-")}</div>
          <div>${escapeHtml(p.roi ?? "-")}</div>
        `;
        frag.appendChild(row);
      });
      positions.replaceChildren(frag);
    }

    const title = document.getElementById("app_title");
    if (title) {
      const running = anySlotRunning();
      title.classList.toggle("active", running);
      title.classList.toggle("idle", !running);
    }

  } catch (e) {
  } finally {
    metricsInFlight = false;
  }
}

/* ─── PnL Share Card ──────────────────────────────────────── */
let _pnlShareIsOpen = false;
let _pnlShareData = null;
let _pnlShareOverlay = "green"; // "green" | "red" | "custom"
let _pnlShareCustomImg = null;
let _pnlShareLogoImg = null;

// Pre-load the Tradoor logo
(function _preloadShareLogo() {
  const img = new Image();
  img.src = "/web/tradoor-icon.svg";
  img.onload = () => { _pnlShareLogoImg = img; };
})();

function _pnlShareSetOpen(open) {
  _pnlShareIsOpen = !!open;
  const modal = document.getElementById("pnl_share_modal");
  const overlay = document.getElementById("pnl_share_overlay");
  if (modal) { modal.classList.toggle("hidden", !_pnlShareIsOpen); modal.setAttribute("aria-hidden", !_pnlShareIsOpen); }
  if (overlay) { overlay.classList.toggle("hidden", !_pnlShareIsOpen); overlay.setAttribute("aria-hidden", !_pnlShareIsOpen); }
  if (_pnlShareIsOpen && _pnlShareData) _pnlShareRender();
}

function _pnlShareOpen(sessionData) {
  _pnlShareData = sessionData;
  // Auto-pick overlay: green if PnL >= 0, red otherwise
  const pnl = sessionData.totalPnl || 0;
  _pnlShareOverlay = pnl >= 0 ? "green" : "red";
  // Update active button
  document.querySelectorAll(".pnl-share-ov-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.overlay === _pnlShareOverlay);
  });
  _pnlShareSetOpen(true);
}

function _pnlShareDrawStars(ctx, w, h, color, count) {
  for (let i = 0; i < count; i++) {
    const x = Math.random() * w;
    const y = Math.random() * h;
    const r = Math.random() * 1.8 + 0.3;
    const alpha = Math.random() * 0.6 + 0.2;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = color.replace("ALPHA", alpha.toFixed(2));
    ctx.fill();
  }
}

function _pnlShareRender() {
  const canvas = document.getElementById("pnl_share_canvas");
  if (!canvas || !_pnlShareData) return;
  const ctx = canvas.getContext("2d");
  const W = 600, H = 340;
  canvas.width = W;
  canvas.height = H;
  const d = _pnlShareData;
  const isPositive = (d.totalPnl || 0) >= 0;
  const showPnl = document.getElementById("pnl_share_opt_pnl")?.checked !== false;
  const showVol = document.getElementById("pnl_share_opt_volume")?.checked !== false;

  // --- Background gradient ---
  const bg = ctx.createLinearGradient(0, 0, W, H);
  if (_pnlShareOverlay === "green") {
    bg.addColorStop(0, "#080f0c");
    bg.addColorStop(1, "#0a1810");
  } else if (_pnlShareOverlay === "red") {
    bg.addColorStop(0, "#0f0808");
    bg.addColorStop(1, "#180a0a");
  } else {
    bg.addColorStop(0, "#0a0c14");
    bg.addColorStop(1, "#06080e");
  }
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, W, H);

  // --- Custom image overlay ---
  if (_pnlShareOverlay === "custom" && _pnlShareCustomImg) {
    ctx.globalAlpha = 0.18;
    const img = _pnlShareCustomImg;
    const scale = Math.max(W / img.width, H / img.height);
    const iw = img.width * scale, ih = img.height * scale;
    ctx.drawImage(img, (W - iw) / 2, (H - ih) / 2, iw, ih);
    ctx.globalAlpha = 1;
  }

  // --- Stars ---
  if (_pnlShareOverlay === "green") {
    _pnlShareDrawStars(ctx, W, H, "rgba(53,240,194,ALPHA)", 90);
    _pnlShareDrawStars(ctx, W, H, "rgba(255,255,255,ALPHA)", 30);
  } else if (_pnlShareOverlay === "red") {
    _pnlShareDrawStars(ctx, W, H, "rgba(255,92,92,ALPHA)", 90);
    _pnlShareDrawStars(ctx, W, H, "rgba(255,255,255,ALPHA)", 30);
  } else {
    _pnlShareDrawStars(ctx, W, H, "rgba(255,255,255,ALPHA)", 50);
  }

  // --- Diagonal streaks ---
  const streakColor = _pnlShareOverlay === "green" ? "rgba(53,240,194," : _pnlShareOverlay === "red" ? "rgba(255,92,92," : "rgba(255,255,255,";
  for (let i = 0; i < 5; i++) {
    const x = Math.random() * W;
    const y = Math.random() * H;
    const grad = ctx.createLinearGradient(x, y, x + 180, y + 180);
    grad.addColorStop(0, streakColor + "0)");
    grad.addColorStop(0.5, streakColor + (0.03 + Math.random() * 0.04).toFixed(2) + ")");
    grad.addColorStop(1, streakColor + "0)");
    ctx.fillStyle = grad;
    ctx.fillRect(x - 40, y - 2, 220, 3 + Math.random() * 2);
  }

  // --- Border glow ---
  const borderC = _pnlShareOverlay === "green" ? "rgba(53,240,194,0.15)" : _pnlShareOverlay === "red" ? "rgba(255,92,92,0.15)" : "rgba(255,255,255,0.08)";
  ctx.strokeStyle = borderC;
  ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, W - 1, H - 1);

  // --- Content ---
  const pad = 32;
  let curY = 38;

  // Token name
  const tokenRaw = String(d.symbol || "").replace(/-USDC$/i, "").replace(/-USD$/i, "");
  ctx.font = "bold 22px 'Inter', 'Segoe UI', sans-serif";
  ctx.fillStyle = "rgba(255,255,255,0.9)";
  ctx.fillText(tokenRaw, pad, curY);

  // Mode badge
  const modeLabel = String(d.mode || "").replace(/_mainnet$/i, "").replace(/_testnet$/i, "");
  if (modeLabel) {
    const tokenW = ctx.measureText(tokenRaw).width;
    ctx.font = "600 11px 'Inter', sans-serif";
    const badgeW = ctx.measureText(modeLabel.toUpperCase()).width + 14;
    ctx.fillStyle = "rgba(255,255,255,0.06)";
    _pnlShareRoundRect(ctx, pad + tokenW + 12, curY - 14, badgeW, 20, 4);
    ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,0.4)";
    ctx.fillText(modeLabel.toUpperCase(), pad + tokenW + 19, curY - 1);
  }

  // Date
  curY += 22;
  ctx.font = "500 12px 'IBM Plex Mono', monospace";
  ctx.fillStyle = "rgba(255,255,255,0.35)";
  const dateStr = d.startedAt ? new Date(d.startedAt).toLocaleString("en-US", { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "";
  const durStr = d.durationMin != null ? ` · ${Number(d.durationMin).toFixed(1)} min` : "";
  ctx.fillText(dateStr + durStr, pad, curY);

  // --- PnL (big) ---
  if (showPnl) {
    curY += 52;
    const pnlVal = d.totalPnl || 0;
    const pnlStr = `${pnlVal >= 0 ? "+" : ""}${pnlVal.toFixed(2)} $`;
    ctx.font = "bold 48px 'Inter', 'Segoe UI', sans-serif";
    const pnlColor = isPositive ? "#ff6b2c" : "#ff5c5c";
    ctx.fillStyle = pnlColor;
    // Glow
    ctx.shadowColor = pnlColor;
    ctx.shadowBlur = 30;
    ctx.fillText(pnlStr, pad, curY);
    ctx.shadowBlur = 0;

    // PnL percentage if start value available
    if (d.startAccountValue > 1) {
      const pct = (pnlVal / d.startAccountValue) * 100;
      curY += 32;
      ctx.font = "bold 20px 'Inter', sans-serif";
      ctx.fillStyle = isPositive ? "rgba(53,240,194,0.7)" : "rgba(255,92,92,0.7)";
      ctx.fillText(`${pct >= 0 ? "↑" : "↓"} ${Math.abs(pct).toFixed(2)}%`, pad, curY);
    }
  }

  // --- Volume + PnL/M$ row ---
  const bottomY = H - 30;
  ctx.font = "500 12px 'Inter', sans-serif";

  if (showVol && d.volume != null && Number.isFinite(d.volume)) {
    ctx.fillStyle = "rgba(255,255,255,0.35)";
    ctx.fillText("Volume", pad, bottomY - 16);
    ctx.font = "bold 15px 'Inter', sans-serif";
    ctx.fillStyle = "rgba(255,255,255,0.75)";
    ctx.fillText(`$${Number(d.volume).toLocaleString("en-US", { maximumFractionDigits: 0 })}`, pad, bottomY);
  }

  // PnL/M$
  if (showPnl && d.volume > 0) {
    const pnlPerM = ((d.totalPnl || 0) / d.volume) * 100_000;
    const pmX = showVol ? 200 : pad;
    ctx.font = "500 12px 'Inter', sans-serif";
    ctx.fillStyle = "rgba(255,255,255,0.35)";
    ctx.fillText("PnL / $100k", pmX, bottomY - 16);
    ctx.font = "bold 15px 'Inter', sans-serif";
    ctx.fillStyle = isPositive ? "rgba(53,240,194,0.8)" : "rgba(255,92,92,0.8)";
    ctx.fillText(`${pnlPerM >= 0 ? "+" : ""}${pnlPerM.toFixed(0)} $`, pmX, bottomY);
  }

  // --- Tradoor eyes logo bottom-right ---
  if (_pnlShareLogoImg) {
    const logoSize = 36;
    ctx.globalAlpha = 0.5;
    ctx.drawImage(_pnlShareLogoImg, W - pad - logoSize, bottomY - logoSize + 4, logoSize, logoSize);
    ctx.globalAlpha = 1;
  }
  // "Tradoor" text next to logo
  ctx.font = "600 11px 'Inter', sans-serif";
  ctx.fillStyle = "rgba(255,255,255,0.25)";
  ctx.fillText("Tradoor", W - pad - 36 - 52, bottomY);
}

function _pnlShareRoundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function _pnlShareDownload() {
  const canvas = document.getElementById("pnl_share_canvas");
  if (!canvas) return;
  const link = document.createElement("a");
  const token = String(_pnlShareData?.symbol || "pnl").replace(/[^a-zA-Z0-9]/g, "_");
  link.download = `tradoor_pnl_${token}.png`;
  link.href = canvas.toDataURL("image/png");
  link.click();
}

async function _pnlShareCopy() {
  const canvas = document.getElementById("pnl_share_canvas");
  const btn = document.getElementById("pnl_share_copy");
  if (!canvas) return;
  try {
    const blob = await new Promise(r => canvas.toBlob(r, "image/png"));
    if (blob) await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
    if (btn) { const orig = btn.textContent; btn.textContent = "Copied!"; setTimeout(() => { btn.textContent = orig; }, 1200); }
  } catch (_) {
    if (btn) { const orig = btn.textContent; btn.textContent = "Failed"; setTimeout(() => { btn.textContent = orig; }, 1200); }
  }
}

function _pnlShareTweet() {
  if (!_pnlShareData) return;
  const pnl = _pnlShareData.totalPnl || 0;
  const sym = String(_pnlShareData.symbol || "").replace(/-USDC$/i, "");
  const text = `Get exposed with the most efficient market making bot @tradoor0`;
  window.open(`https://x.com/intent/tweet?text=${encodeURIComponent(text)}`, "_blank");
}

async function refreshPnlHistory() {
  // Avoid background polling when landing is visible or the tab is hidden.
  if (!document.body.classList.contains("main-space") || document.hidden) return;
  if (!walletAuthToken) return;
  try {
    const data = await apiGet("/api/pnl_history");
    const rows = document.getElementById("session_history_rows");
    if (!rows) return;
    rows.innerHTML = "";
    const sessions = data.sessions || [];
    if (!sessions.length) {
      const empty = document.createElement("div");
      empty.className = "history-empty";
      empty.textContent = "No completed sessions yet.";
      rows.appendChild(empty);
      return;
    }

    const formatStartDate = (raw) => {
      const text = String(raw || "").trim();
      if (!text) return "-";
      const parsed = new Date(text);
      if (!Number.isFinite(parsed.getTime())) return text;
      return parsed.toLocaleString("en-US", {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      });
    };
    const normalizeModeLabel = (raw) => {
      const text = String(raw || "").trim();
      if (!text) return "-";
      return text.replace(/_mainnet$/i, "").replace(/_testnet$/i, "");
    };

    sessions.forEach((s) => {
      const row = document.createElement("div");
      row.className = "row row-history";
      // Use pnl_total_usd_net (after trading fees), fallback for older entries.
      const histNetPnl = parseFloat(s.pnl_total_usd_net ?? s.display_pnl_usd ?? s.pnl_total_usd);
      const histSessionPnlRaw =
        typeof s.pnl_total_usd === "string" ? parseFloat(s.pnl_total_usd) : s.pnl_total_usd;
      const histDisplayPnl = typeof s.display_pnl_usd === "string" ? parseFloat(s.display_pnl_usd) : s.display_pnl_usd;
      const totalPnlNum = Number.isFinite(histNetPnl) ? histNetPnl
        : Number.isFinite(histSessionPnlRaw) ? histSessionPnlRaw
        : Number.isFinite(histDisplayPnl) ? histDisplayPnl : 0;
      const histStartAcct = parseFloat(s.start_account_value) || 0;
      const histSessionVol = typeof s.session_volume_usd === "string" ? parseFloat(s.session_volume_usd) : s.session_volume_usd;
      const histEngineVol = typeof s.engine_volume_usd === "string" ? parseFloat(s.engine_volume_usd) : s.engine_volume_usd;
      let histVol = null;
      if (Number.isFinite(histSessionVol) && Number.isFinite(histEngineVol)) {
        histVol = Math.max(histSessionVol, histEngineVol);
      } else if (Number.isFinite(histSessionVol)) {
        histVol = histSessionVol;
      } else if (Number.isFinite(histEngineVol)) {
        histVol = histEngineVol;
      }
      const projectFeesNumRaw =
        typeof s.project_fees_usd === "string" ? parseFloat(s.project_fees_usd) : s.project_fees_usd;
      const feesNumRaw = typeof s.platform_fees_usd === "string" ? parseFloat(s.platform_fees_usd) : s.platform_fees_usd;
      // HIP-4 (predictions) carries the same builder fee as market making, so
      // fall back to the volume-based estimate like any other mode.
      const feesNum = Number.isFinite(projectFeesNumRaw)
        ? projectFeesNumRaw
        : Number.isFinite(feesNumRaw)
          ? feesNumRaw
          : platformFeesUsdFromVolume(histVol);
      const feesAbs = Math.abs(Number(feesNum) || 0);
      const durationMin =
        s.duration_min !== undefined && s.duration_min !== null
          ? s.duration_min
          : (s.duration_sec !== undefined && s.duration_sec !== null ? Number(s.duration_sec) / 60.0 : null);
      const pnlClass = totalPnlNum > 0 ? "pnl-pos" : totalPnlNum < 0 ? "pnl-neg" : "";
      row.innerHTML = `
        <div>${escapeHtml(formatStartDate(s.started_at))}</div>
        <div>${escapeHtml(durationMin !== null && Number.isFinite(Number(durationMin)) ? Number(durationMin).toFixed(2) : "-")}</div>
        <div>${escapeHtml(normalizeModeLabel(s.mode))}</div>
        <div>${escapeHtml(_slotDisplayToken(s.symbol || "-"))}</div>
        <div>${escapeHtml(formatUsdPlain(histVol))}</div>
        <div class="fees-neg">-${escapeHtml(formatUsdPlain(feesAbs))}</div>
        <div class="${pnlClass}">${escapeHtml(formatUsd(totalPnlNum))}</div>
        <div><button type="button" class="history-share-btn" title="Share PnL"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="8" width="18" height="13" rx="2"/><path d="M12 2v10"/><path d="M8 6l4-4 4 4"/></svg></button></div>
      `;
      // Wire share button
      const shareBtn = row.querySelector(".history-share-btn");
      if (shareBtn) {
        const shareData = {
          symbol: s.symbol || "",
          mode: s.mode || "",
          totalPnl: totalPnlNum,
          volume: histVol,
          startedAt: s.started_at,
          durationMin: durationMin,
          startAccountValue: histStartAcct,
        };
        shareBtn.addEventListener("click", (e) => { e.stopPropagation(); _pnlShareOpen(shareData); });
      }
      rows.appendChild(row);
    });
  } catch (_) {
    // ignore
  }
}

async function init() {
  const connectPage = document.getElementById("connect_page");
  const mainPage = document.getElementById("main_page");
  const changeBtn = document.getElementById("change_account");
  const connectBtn = document.getElementById("connect_continue");
  const landingWrap = document.getElementById("landing_wrap");
  const statsNavBtn = document.getElementById("stats_nav_btn");
  const statsCloseBtn = document.getElementById("stats_close_btn");
  const statsChartCanvas = document.getElementById("stats_daily_volume_chart");
  const statsFeesChartCanvas = null; // fees chart removed
  const headerNav = document.getElementById("header_nav");
  const menuOpenStatsBtn = document.getElementById("menu_open_stats");
  const headerConnectWrap = document.getElementById("header_connect_wrap");
  const connectHelpToggle = document.getElementById("connect_help_toggle");
  document.querySelectorAll(".menu-item-btn:not(.menu-item-btn--icon)").forEach(attachGlitchHover);
  document.querySelectorAll(".menu-item-btn--icon").forEach((btn) => {
    const span = btn.querySelector(".menu-item-glitch");
    if (span) {
      btn.addEventListener("mouseenter", () => glitchText(span));
      btn.addEventListener("mouseleave", () => glitchTextCancel(span));
    }
  });
  initVisualsOnce();
  initLandingScrollReveal();
  setStarfieldEnabled(getInitialStarfieldEnabled());
  // Apply route-based view (landing or app page)
  applyRoute();
  initLogoVariantPicker();
  const aggressiveModeBtn = document.getElementById("mode_aggressive");
  const classicModeBtn = document.getElementById("mode_classic");
  const conservativeModeBtn = document.getElementById("mode_conservative");
  const advancedMmEnabledEl = document.getElementById("advanced_mm_enabled");
  const advancedMmFieldIds = [
    "advanced_spread_min_bps",
    "advanced_spread_max_bps",
    "advanced_pos_min_usd",
    "advanced_pos_max_usd",
    "advanced_engine_speed",
    "advanced_direction_bias",
  ];
  if (aggressiveModeBtn) {
    aggressiveModeBtn.addEventListener("click", () => applyRiskMode("aggressive"));
  }
  if (classicModeBtn) {
    classicModeBtn.addEventListener("click", () => applyRiskMode("classic"));
  }
  if (conservativeModeBtn) {
    conservativeModeBtn.addEventListener("click", () => applyRiskMode("conservative"));
  }
  // Direction bias buttons
  const biasLongBtn = document.getElementById("bias_long");
  const biasNeutralBtn = document.getElementById("bias_neutral");
  const biasShortBtn = document.getElementById("bias_short");
  if (biasLongBtn) biasLongBtn.addEventListener("click", () => applyDirectionBias("long"));
  if (biasNeutralBtn) biasNeutralBtn.addEventListener("click", () => applyDirectionBias("neutral"));
  if (biasShortBtn) biasShortBtn.addEventListener("click", () => applyDirectionBias("short"));
  // Quote mode buttons
  const qmSniperBtn = document.getElementById("qm_sniper");
  const qmGridBtn = document.getElementById("qm_grid");
  const qmFortressBtn = document.getElementById("qm_fortress");
  const qmAdaptiveBtn = document.getElementById("qm_adaptive");
  if (qmSniperBtn) qmSniperBtn.addEventListener("click", () => applyQuoteMode("sniper"));
  if (qmGridBtn) qmGridBtn.addEventListener("click", () => applyQuoteMode("grid"));
  if (qmFortressBtn) qmFortressBtn.addEventListener("click", () => applyQuoteMode("fortress"));
  if (qmAdaptiveBtn) qmAdaptiveBtn.addEventListener("click", () => applyQuoteMode("adaptive"));

  // "?" hover tooltip : appended to <body> to avoid overflow/transform issues
  let infoTip = null;
  document.querySelectorAll(".risk-mode-info").forEach((el) => {
    el.addEventListener("mouseenter", () => {
      const text = el.dataset.info;
      if (!text) return;
      if (!infoTip) {
        infoTip = document.createElement("div");
        infoTip.className = "risk-mode-tooltip";
        document.body.appendChild(infoTip);
      }
      infoTip.textContent = text;
      const r = el.getBoundingClientRect();
      infoTip.style.top = (r.bottom + 6) + "px";
      infoTip.style.left = Math.max(8, Math.min(r.left + r.width / 2 - 115, window.innerWidth - 246)) + "px";
      infoTip.classList.add("is-visible");
    });
    el.addEventListener("mouseleave", () => {
      if (infoTip) infoTip.classList.remove("is-visible");
    });
  });

  // Default profile at load: Conservative PnL.
  applyRiskMode("conservative", { force: true, silent: true, persist: false });
  applyDirectionBias("neutral", { silent: true });
  applyQuoteMode("sniper", { silent: true });
  restoreRiskInputsFromStorage();
  restoreAdvancedMmSettings();
  restoreActiveSessionTargetVolume();
  restoreExchangeMode();
  restoreWalletAuthSession();  // Must be before initModeCards so token validity is known
  initModeCards();
  // Atomically sync DOM to restored active DEX (mode + account_address + auth)
  _syncDexDomState(_activeDex);
  initModeSelector();
  initDexSelector();
  initWalletsDrawer();
  initSubtransferModal();
  initTradingAccountSelector();
  updateMarketSourceForMode();
  initSourceSelector();
  initTokenPicker();
  setTitleHyperliquidOs(false);
  updateWalletAddressDisplay();
  updateProfileConnectionBadge();
  setSignatureStepsVisible(false);
  await refreshWalletNetworkBadge();
  await refreshKeyStatus();
  await refreshWalletAuthStatus();
  await refreshBuilderStatus();
  await refreshAgentStatus();
  await refreshNadoAgentStatus();
  await refreshExtendedCredentialsStatus();
  updateModeGateUi();
  updateConnectStepsState();
  // Restore persisted market_source before the first loadMarkets call so the
  // initial token list matches the saved source (not the default "top20").
  {
    const marketSourceEl = document.getElementById("market_source");
    const savedSource = localStorage.getItem("market_source");
    if (savedSource && marketSourceEl && !marketSourceEl.disabled) {
      const opt = marketSourceEl.querySelector(`option[value="${savedSource}"]`);
      if (opt && !opt.disabled && marketSourceEl.value !== savedSource) {
        marketSourceEl.value = savedSource;
        syncSourceSelectorFromSelect();
      }
    }
  }
  // Warm cache while user is on landing so "Continue" feels instant.
  loadMarkets().catch(() => {});
  // Prewarm all sources so market switch stays instant, including xyz.
  const initialMode = document.getElementById("mode")?.value || "hyperliquid_mainnet";
  prewarmMarketSourcesForMode(initialMode);
  const accountInput = document.getElementById("account_address");
  if (accountInput) {
    accountInput.addEventListener("input", () => {
      refreshKeyStatus();
      updateWalletAuthButtonState();
      updateBuilderApproveButtonState(false);
      updateAgentApproveButtonState(false);
      builderApprovedForAccount = false;
      agentApprovedForAccount = false;
      nadoAgentApprovedForAccount = false;
      nadoAgentStatusCache = { key: "", ts: 0, value: null };
      extendedCredentialsReadyForAccount = false;
      extendedCredentialsStatusCache = { key: "", ts: 0, value: null };
      setWalletAuthStatus("1/3 Signature expired. Sign again.", "warn");
      setBuilderStatus("2/3 Approve builder fee.", "warn");
      setAgentStatus("3/3 Approve TRADOR agent.", "warn");
      setExtendedCredentialsStatus("Extended credentials: missing", "warn");
    });
    accountInput.addEventListener("blur", () => {
      refreshKeyStatus();
      refreshWalletAuthStatus();
      refreshBuilderStatus();
      refreshAgentStatus();
      refreshExtendedCredentialsStatus();
    });
  }
  const profileDisconnectBtn = document.getElementById("profile_disconnect_btn");
  if (profileDisconnectBtn) {
    profileDisconnectBtn.addEventListener("click", async () => {
      await disconnectWalletProfile();
    });
  }
  const referralCopyBtn = document.getElementById("referral_copy_btn");
  if (referralCopyBtn) {
    const copyIconOriginal = referralCopyBtn.innerHTML;
    referralCopyBtn.addEventListener("click", () => {
      const link = document.getElementById("referral_link_display")?.textContent?.trim() || "";
      if (link && link !== "-") {
        navigator.clipboard.writeText(link).catch(() => {});
        referralCopyBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#ff6b2c" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="20 6 9 17 4 12"/></svg>`;
        referralCopyBtn.style.transform = "scale(1.25)";
        setTimeout(() => {
          referralCopyBtn.innerHTML = copyIconOriginal;
          referralCopyBtn.style.transform = "";
        }, 1500);
      }
    });
  }
  const referralApplyBtn = document.getElementById("referral_apply_btn");
  if (referralApplyBtn) {
    referralApplyBtn.addEventListener("click", async () => {
      await applyReferralCode();
    });
  }
  const referralCodeInput = document.getElementById("referral_code_input");
  if (referralCodeInput) {
    referralCodeInput.addEventListener("keydown", async (e) => {
      if (e.key === "Enter") await applyReferralCode();
    });
  }
  // Pre-fill ref code from URL ?ref=xxx
  const urlRef = new URLSearchParams(window.location.search).get("ref");
  if (urlRef && referralCodeInput) {
    referralCodeInput.value = urlRef.trim();
  }
  const editCodeBtn = document.getElementById("referral_edit_code_btn");
  if (editCodeBtn) {
    editCodeBtn.addEventListener("click", () => setEditCodeRowVisible(true));
  }
  const setCodeBtn = document.getElementById("referral_set_code_btn");
  if (setCodeBtn) {
    setCodeBtn.addEventListener("click", async () => await setReferralCode());
  }
  const setCodeCancel = document.getElementById("referral_set_code_cancel");
  if (setCodeCancel) {
    setCodeCancel.addEventListener("click", () => setEditCodeRowVisible(false));
  }
  const claimBtn = document.getElementById("referral_claim_btn");
  if (claimBtn) {
    claimBtn.addEventListener("click", async () => {
      const account = document.getElementById("account_address")?.value?.trim() || "";
      const msgEl = document.getElementById("referral_claim_msg");
      if (!account || !account.startsWith("0x")) {
        if (msgEl) { msgEl.textContent = "Connect your wallet first."; msgEl.className = "referral-msg err"; msgEl.classList.remove("hidden"); }
        return;
      }
      claimBtn.disabled = true;
      try {
        const data = await apiPost("/api/referral/claim", { account_address: account });
        if (msgEl) {
          msgEl.textContent = `Claim of $${Number(data.amount || 0).toFixed(2)} submitted! You will be paid within 24h.`;
          msgEl.className = "referral-msg ok";
          msgEl.classList.remove("hidden");
        }
        await refreshReferralStatus();
      } catch (e) {
        const msg = e?.detail || e?.message || String(e);
        if (msgEl) { msgEl.textContent = msg; msgEl.className = "referral-msg err"; msgEl.classList.remove("hidden"); }
        claimBtn.disabled = false;
      }
    });
  }
  const newCodeInput = document.getElementById("referral_new_code_input");
  if (newCodeInput) {
    newCodeInput.addEventListener("keydown", async (e) => {
      if (e.key === "Enter") await setReferralCode();
      if (e.key === "Escape") setEditCodeRowVisible(false);
    });
    newCodeInput.addEventListener("input", () => {
      newCodeInput.value = newCodeInput.value.toLowerCase().replace(/[^a-z0-9_-]/g, "");
    });
  }
  ["connect_step_1", "connect_step_2", "connect_step_3"].forEach((id, idx) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("click", async () => {
      await runConnectStepAction(idx + 1);
    });
  });

  if (advancedMmEnabledEl instanceof HTMLInputElement) {
    advancedMmEnabledEl.addEventListener("change", () => {
      const willEnable = !!advancedMmEnabledEl.checked;
      if (willEnable) {
        prefillAdvancedMmFromCurrentProfile();
      }
      setAdvancedMmUiEnabled(willEnable);
      const err = willEnable ? validateAdvancedMmConfig(readAdvancedMmConfigFromUi()) : "";
      setAdvancedMmError(err);
      persistAdvancedMmSettings();
      if (willEnable) {
        openStrategiesAfterAdvancedToggleOn();
      } else {
        renderAdvancedStrategies();
      }
    });
  }
  advancedMmFieldIds.forEach((id) => {
    const el = document.getElementById(id);
    if (!(el instanceof HTMLInputElement || el instanceof HTMLSelectElement)) return;
    const onAdvancedFieldChange = () => {
      if (!advancedMmEnabled) return;
      if (advancedStrategiesAreFrozen()) return;
      const err = validateAdvancedMmConfig(readAdvancedMmConfigFromUi());
      setAdvancedMmError(err);
      persistAdvancedMmSettings();
      if (selectedStrategyIdx >= 0 && !err) _scheduleAutoSave(350);
    };
    el.addEventListener("input", onAdvancedFieldChange);
    el.addEventListener("change", onAdvancedFieldChange);
  });
  const advBiasEl = document.getElementById("advanced_direction_bias");
  if (advBiasEl) {
    advBiasEl.addEventListener("change", () => {
      applyDirectionBias(advBiasEl.value, { silent: true });
    });
  }
  const marketSourceEl = document.getElementById("market_source");

  marketSourceEl.addEventListener("change", async () => {
    localStorage.setItem("market_source", marketSourceEl.value);
    const symSelect = document.getElementById("market_symbol");
    if (symSelect) symSelect.disabled = false;
    latestAvailableNotional = null;
    const notionalMaxBtn = document.getElementById("session_notional_max");
    if (notionalMaxBtn) notionalMaxBtn.disabled = true;
    updateNotionalUnitUI();
    updatePnlPerMillionLabel();
    if (!applyCachedMetricsForCurrentSelection()) {
      setNotionalLoading(document.getElementById("available_notional"));
    }
    const marketsTask = loadMarkets();
    // Reset guard so the fetch is not silently skipped when a previous poll is
    // still in flight (same pattern as switchActiveDex).
    metricsInFlight = false;
    const metricsTask = refreshMetrics();
    await marketsTask;
    updateLeverageUI();
    await metricsTask;
    await refreshBuilderStatus();
    await refreshAgentStatus();
    if (isExtendedMode()) {
      await refreshExtendedCredentialsStatus();
    }
  });
  document.getElementById("mode").addEventListener("change", async () => {
    updateModeGateUi();
    // Reset notional immediately to avoid stale cross-mode collateral display
    latestAvailableNotional = null;
    const _modeNotionalMaxBtn = document.getElementById("session_notional_max");
    if (_modeNotionalMaxBtn) _modeNotionalMaxBtn.disabled = true;
    updateNotionalUnitUI();
    setNotionalLoading(document.getElementById("available_notional"));
    updatePnlPerMillionLabel();
    await refreshWalletNetworkBadge();
    if (isNadoMode()) {
      await refreshNadoAgentStatus({ force: true });
    } else {
      await refreshBuilderStatus({ force: true });
      await refreshAgentStatus({ force: true });
    }
    if (isExtendedMode()) {
      await refreshExtendedCredentialsStatus({ force: true });
    }
    await loadMarkets({ force: true });
    prewarmMarketSourcesForMode(document.getElementById("mode")?.value || "hyperliquid_mainnet");
    // Save config immediately so backend reads the new mode, then refresh metrics
    if (liveConfigSyncTimer) { window.clearTimeout(liveConfigSyncTimer); liveConfigSyncTimer = null; }
    try {
      _syncDexDomState(_activeDex);
      const _rs = _getRunningSlot(_currentSymbol());
      const _syncSlot = _rs ? _rs.slot : activeDexSlot();
      await apiPost(`/api/config?slot=${_syncSlot}`, { toml: buildToml({ slot: _syncSlot }) });
    } catch (_) {}
    metricsInFlight = false;
    await refreshMetrics();
  });
  document.getElementById("market_symbol").addEventListener("change", () => {
    const source = document.getElementById("market_source")?.value || "top20";
    const sel = document.getElementById("market_symbol");
    if (sel) {
      const chosen = sel.value;
      lastSelectedSymbolBySource.set(source, chosen);
      // Fix: some browsers briefly show blank text after selection.
      sel.value = chosen;
      void sel.offsetWidth;
      sel.value = chosen;
    }
    animateMarketSymbolChange();
    updateLeverageUI();
    updateDynamicMaxLoss();
    renderRunningSlots();
  });
  const addClickFx = (id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("click", (e) => {
      e.currentTarget.classList.remove("clicked");
      void e.currentTarget.offsetWidth;
      e.currentTarget.classList.add("clicked");
      const wrap = e.currentTarget.closest(".select-wrap");
      if (wrap) {
        const rect = wrap.getBoundingClientRect();
        const rx = ((e.clientX - rect.left) / rect.width) * 100;
        const ry = ((e.clientY - rect.top) / rect.height) * 100;
        wrap.style.setProperty("--rx", `${rx}%`);
        wrap.style.setProperty("--ry", `${ry}%`);
        wrap.classList.remove("rippling");
        void wrap.offsetWidth;
        wrap.classList.add("rippling");
      }
    });
    el.addEventListener("focus", (e) => {
      const label = e.currentTarget.closest("label");
      if (label) label.classList.add("focused");
    });
    el.addEventListener("blur", (e) => {
      const label = e.currentTarget.closest("label");
      if (label) label.classList.remove("focused");
    });
  };
  addClickFx("mode_card_hyperliquid");
  addClickFx("mode_card_extended");
  addClickFx("mode_card_nado");
  addClickFx("market_source");
  addClickFx("market_symbol_btn");
  [
    "account_address",
    "api_key",
    "target_vol",
    "session_notional",
    "advanced_mm_enabled",
    "advanced_spread_min_bps",
    "advanced_spread_max_bps",
    "advanced_pos_min_usd",
    "advanced_pos_max_usd",
    "advanced_engine_speed",
  ].forEach(addClickFx);

  document.addEventListener("click", (e) => {
    const target = e.target;
    if (!(target instanceof Element)) return;
    const interactive = target.closest(CLICK_SOUND_SELECTOR);
    if (!interactive) return;
    if (interactive.id === "main_action_btn") return;
    if ("disabled" in interactive && interactive.disabled) return;
    const now = performance.now();
    if (now - lastUiClickToneAt < 45) return;
    lastUiClickToneAt = now;
    playTone(330, 0.035, 0.01, "triangle");
  });

  const notionalInput = document.getElementById("session_notional");
  const notionalMaxBtn = document.getElementById("session_notional_max");
  const targetVolInput = document.getElementById("target_vol");
  if (notionalInput) {
    notionalInput.addEventListener("input", () => {
      syncNotionalToMaxPos(false);
      updateDynamicMaxLoss();
      scheduleLiveConfigSync(500);
    });
  }
  if (notionalInput && notionalMaxBtn) {
    notionalMaxBtn.addEventListener("click", () => {
      if (!(Number.isFinite(latestAvailableNotional) && latestAvailableNotional > 0)) {
        showError("main_error", "Available notional is not ready yet.");
        return;
      }
      clearError("main_error");
      const maxSafeNotional = Math.max(0, latestAvailableNotional - 0.01);
      notionalInput.value = String(Math.round(maxSafeNotional * 100) / 100);
      syncNotionalToMaxPos(false);
      scheduleLiveConfigSync(200);
    });
  }
  function showRiskChangesRow() {
    const row = document.getElementById("risk_changes_row");
    if (row) row.classList.remove("hidden");
  }


  if (targetVolInput) {
    targetVolInput.addEventListener("input", () => {
      persistRiskInputs();
      if (_isCurrentTokenRunning()) { showRiskChangesRow(); updateRiskApplyValidation(); return; }
      scheduleLiveConfigSync(200);
    });
    targetVolInput.addEventListener("change", () => {
      persistRiskInputs();
      if (_isCurrentTokenRunning()) { showRiskChangesRow(); updateRiskApplyValidation(); return; }
      scheduleLiveConfigSync(80);
    });
  }

  const applyRiskBtn = document.getElementById("apply_risk_changes");
  if (applyRiskBtn) {
    applyRiskBtn.addEventListener("click", async () => {
      const riskErr = document.getElementById("risk_error");
      const targetVolVal = parseFloat(targetVolInput?.value || "0");
      const sessionVolDone = Number(currentSessionVolumeUsd) || 0;

      if (Number.isFinite(targetVolVal) && Number.isFinite(sessionVolDone) && targetVolVal < sessionVolDone) {
        if (riskErr) {
          riskErr.textContent = `Target volume ($${targetVolVal.toFixed(0)}) cannot be lower than volume already done ($${sessionVolDone.toFixed(0)}).`;
          riskErr.classList.remove("hidden");
        }
        updateRiskApplyValidation();
        return;
      }

      if (riskErr) { riskErr.textContent = ""; riskErr.classList.add("hidden"); }
      applyRiskBtn.disabled = true;
      applyRiskBtn.textContent = "Applying...";
      try {
        _syncDexDomState(_activeDex);
        const applyRs = _getRunningSlot(_currentSymbol());
        const applySlot = applyRs ? applyRs.slot : activeDexSlot();
        const toml = buildToml({ allowDefaults: true, slot: applySlot });
        await apiPost(`/api/config?slot=${applySlot}`, { toml });
        if (isBotRunning() && Number.isFinite(targetVolVal) && targetVolVal > 0) {
          activeSessionTargetVolumeUsd = targetVolVal;
          persistActiveSessionTargetVolume(targetVolVal);
        }
        hideRiskChangesRow();
      } catch (e) {
        if (riskErr) {
          riskErr.textContent = `Failed to apply: ${e.message || e}`;
          riskErr.classList.remove("hidden");
        }
      } finally {
        applyRiskBtn.disabled = false;
        applyRiskBtn.textContent = "Apply Changes";
        updateRiskApplyValidation();
      }
    });
  }
  syncNotionalToMaxPos(true);

  const apiHelpToggle = document.getElementById("api_help_toggle");
  const apiHelpInline = document.getElementById("api_help_inline");
  if (apiHelpToggle && apiHelpInline) {
    apiHelpToggle.addEventListener("click", () => {
      const willOpen = apiHelpInline.classList.contains("hidden");
      apiHelpInline.classList.toggle("hidden", !willOpen);
      apiHelpToggle.classList.toggle("active", willOpen);
      apiHelpToggle.setAttribute("aria-expanded", willOpen ? "true" : "false");
    });
  }
  if (connectHelpToggle) {
    connectHelpToggle.addEventListener("click", () => {
      toggleConnectHelpDetails();
    });
  }

  const overviewTab = document.getElementById("tab_overview");
  const historyTab = document.getElementById("tab_history");
  const myStatsTab = document.getElementById("tab_my_stats");
  const overviewView = document.getElementById("dashboard_overview");
  const historyView = document.getElementById("dashboard_history");
  const setDashboardTab = (name) => {
    const showHistory = name === "history";
    if (overviewView) overviewView.classList.toggle("hidden", showHistory);
    if (historyView) historyView.classList.toggle("hidden", !showHistory);
    if (overviewTab) overviewTab.classList.toggle("active", !showHistory && name !== "my_stats");
    if (historyTab) historyTab.classList.toggle("active", showHistory);
    if (myStatsTab) myStatsTab.classList.remove("active");
  };
  if (overviewTab) overviewTab.addEventListener("click", () => setDashboardTab("overview"));
  if (historyTab) historyTab.addEventListener("click", () => {
    setDashboardTab("history");
    refreshPnlHistory();
  });
  if (myStatsTab) myStatsTab.addEventListener("click", () => {
    setDashboardTab("overview");
    setMyStatsModalOpen(true);
  });
  setDashboardTab("overview");
  updateNotionalUnitUI();
  updatePnlPerMillionLabel();
  updateLeverageUI();
  updateWalletAuthButtonState();
  updateBuilderApproveButtonState(builderApprovedForAccount);
  updateAgentApproveButtonState(agentApprovedForAccount);
  updateSetupAllButtonState();

  if (window.ethereum && typeof window.ethereum.on === "function") {
    window.ethereum.on("chainChanged", () => {
      refreshWalletNetworkBadge().catch(() => {});
    });
  }
  if (statsNavBtn) {
    statsNavBtn.addEventListener("click", async () => {
      if (statsDrawerOpen) {
        setStatsDrawerOpen(false);
        return;
      }
      await openStatsPanel();
    });
  }
  if (statsCloseBtn) {
    statsCloseBtn.addEventListener("click", () => {
      setStatsDrawerOpen(false);
    });
  }
  const statsOverlay = document.getElementById("stats_overlay");
  if (statsOverlay) {
    statsOverlay.addEventListener("click", () => {
      setStatsDrawerOpen(false);
    });
  }
  const bindStatsChartInteractions = (canvas, metaGetter, tooltipSetter, valueFormatter, valueLabel = "Volume") => {
    if (!canvas) return;
    const findPointFromEvent = (ev) => {
      const meta = metaGetter();
      if (!meta || !Array.isArray(meta.points) || !meta.points.length) return null;
      const rect = canvas.getBoundingClientRect();
      const xRaw = ev.clientX - rect.left;
      const scaleX = rect.width > 0 ? (meta.cssWidth / rect.width) : 1;
      const x = xRaw * scaleX;
      if (x < meta.left || x > meta.cssWidth - meta.right) return null;
      // Robust nearest-bar matching to avoid any off-by-one on dense bars.
      let idx = 0;
      let bestDist = Number.POSITIVE_INFINITY;
      for (let i = 0; i < meta.points.length; i += 1) {
        const centerX = meta.left + i * meta.step + (meta.step * 0.5);
        const dist = Math.abs(x - centerX);
        if (dist < bestDist) {
          bestDist = dist;
          idx = i;
        }
      }
      const point = meta.points[idx] || {};
      const value = Number(meta.data[idx]);
      return {
        point,
        x: xRaw,
        y: ev.clientY - rect.top,
        value: Number.isFinite(value) ? value : 0,
      };
    };
    const formatTooltipDate = (raw) => {
      const text = String(raw || "");
      const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
      if (!m) return text || "-";
      const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
      return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
    };
    canvas.addEventListener("mousemove", (ev) => {
      const found = findPointFromEvent(ev);
      if (!found) {
        tooltipSetter({ visible: false });
        return;
      }
      tooltipSetter({
        visible: true,
        x: found.x,
        y: found.y,
        date: formatTooltipDate(found.point.date),
        total: `${valueLabel}: ${valueFormatter(found.value)}`,
      });
    });
    canvas.addEventListener("mouseleave", () => {
      tooltipSetter({ visible: false });
    });
    canvas.addEventListener("click", (ev) => {
      const found = findPointFromEvent(ev);
      if (!found) return;
      tooltipSetter({
        visible: true,
        x: found.x,
        y: found.y,
        date: formatTooltipDate(found.point.date),
        total: `${valueLabel}: ${valueFormatter(found.value)}`,
      });
    });
  };
  bindStatsChartInteractions(
    statsChartCanvas,
    () => statsChartMeta,
    setStatsChartTooltip,
    (v) => formatUsdCompact(v),
    "Volume",
  );
  bindStatsChartInteractions(
    statsFeesChartCanvas,
    () => statsFeesChartMeta,
    setStatsFeesChartTooltip,
    (v) => formatUsdCompact(v),
    "Fees",
  );
  /* ── Header dropdown logic ── */
  const closeAllDropdowns = () => {
    document.querySelectorAll(".header-dropdown.open").forEach((dd) => dd.classList.remove("open"));
    document.querySelectorAll(".header-nav-btn.active, .header-icon-btn.active").forEach((b) => b.classList.remove("active"));
  };
  const _initDropdownItem = (item) => {
    const btn = item.querySelector(".header-nav-btn") || item.querySelector(".header-icon-btn") || item.querySelector(".header-wallet-btn");
    const dd = item.querySelector(".header-dropdown");
    if (!btn || !dd) return;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const wasOpen = dd.classList.contains("open");
      closeAllDropdowns();
      if (!wasOpen) {
        dd.classList.add("open");
        btn.classList.add("active");
      }
    });
  };
  if (headerNav) {
    headerNav.querySelectorAll(".header-nav-item").forEach(_initDropdownItem);
  }
  if (headerConnectWrap) _initDropdownItem(headerConnectWrap);
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".header-nav-item") && !e.target.closest(".header-connect-wrap")) closeAllDropdowns();
  });

  /* ── Mobile nav drawer (<= 860px) ──
     Reuses .header-nav as-is: CSS turns it into a slide-down panel and the
     existing dropdown logic renders as an accordion inside it. */
  const mobileNavToggle = document.getElementById("mobile_nav_toggle");
  const setMobileNavOpen = (open) => {
    document.body.classList.toggle("mobile-nav-open", open);
    if (mobileNavToggle) mobileNavToggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) closeAllDropdowns();
  };
  if (mobileNavToggle) {
    mobileNavToggle.addEventListener("click", (e) => {
      e.stopPropagation();
      setMobileNavOpen(!document.body.classList.contains("mobile-nav-open"));
    });
  }
  if (headerNav) {
    // Any actual navigation/action inside the drawer closes it. The section
    // toggles (Analytics/Tools) must not, or the accordion could never open.
    headerNav.addEventListener("click", (e) => {
      if (!document.body.classList.contains("mobile-nav-open")) return;
      const btn = e.target.closest("a, button");
      if (!btn) return;
      if (btn.classList.contains("header-nav-btn") && btn.parentElement?.querySelector(".header-dropdown")) return;
      setMobileNavOpen(false);
    });
  }
  document.getElementById("mobile_nav_scrim")?.addEventListener("click", () => setMobileNavOpen(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && document.body.classList.contains("mobile-nav-open")) setMobileNavOpen(false);
  });
  // Resizing past the breakpoint must not leave the page locked (body:overflow
  // hidden) with an invisible drawer still flagged open.
  window.matchMedia("(min-width: 861px)").addEventListener("change", (ev) => {
    if (ev.matches) setMobileNavOpen(false);
  });

  const showLandingPage = () => {
    // Clear timers not handled by applyRoute
    if (liveConfigSyncTimer) { window.clearTimeout(liveConfigSyncTimer); liveConfigSyncTimer = null; }
    if (_tsRefreshTimer) { clearInterval(_tsRefreshTimer); _tsRefreshTimer = null; }
    stopTsBadgePolling();
    setMilestonesPageOpen(false);
    // If we're on the SPA (/market-making etc.), stay there and re-show the wallet picker
    if (location.pathname !== "/") {
      // Force main view to remain (hide any lingering landing_wrap)
      const landingWrap = document.getElementById("landing_wrap");
      const connectPage = document.getElementById("connect_page");
      if (landingWrap) landingWrap.classList.add("hidden");
      if (connectPage) connectPage.classList.add("hidden");
      applyRoute();
      // Re-open the wallet picker popup
      setTimeout(() => {
        const headerBtn = document.getElementById("header_connect_btn");
        if (headerBtn) headerBtn.click();
      }, 200);
      return;
    }
    // Only go to landing.html if we were already on "/"
    window.location.href = "/";
  };
  if (menuOpenStatsBtn) {
    menuOpenStatsBtn.addEventListener("click", async () => {
      closeAllDropdowns();
      await openStatsPanel();
    });
  }
  /* Disconnect handler */
  const menuDisconnectBtn = document.getElementById("menu_disconnect");
  if (menuDisconnectBtn) {
    menuDisconnectBtn.addEventListener("click", async () => {
      closeAllDropdowns();
      await disconnectWalletProfile();
      showLandingPage();
    });
  }
  /* Milestones page handlers */
  const menuOpenMsBtn = document.getElementById("menu_open_milestones");
  if (menuOpenMsBtn) {
    menuOpenMsBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      setMilestonesPageOpen(true);
    });
  }
  const msCloseBtn = document.getElementById("ms_close_btn");
  if (msCloseBtn) msCloseBtn.addEventListener("click", () => setMilestonesPageOpen(false));

  /* Points-live announcement handlers */
  const plClose = document.getElementById("points_live_close");
  if (plClose) plClose.addEventListener("click", () => setPointsLiveModalOpen(false));
  const plOverlay = document.getElementById("points_live_overlay");
  if (plOverlay) plOverlay.addEventListener("click", () => setPointsLiveModalOpen(false));
  const plCta = document.getElementById("points_live_cta");
  if (plCta) plCta.addEventListener("click", () => setPointsLiveModalOpen(false));

  /* Referral modal handlers */
  const menuOpenReferralBtn = document.getElementById("menu_open_referral");
  if (menuOpenReferralBtn) {
    menuOpenReferralBtn.addEventListener("click", () => {
      closeAllDropdowns();
      setReferralModalOpen(true);
    });
  }
  const referralModalCloseBtn = document.getElementById("referral_modal_close");
  if (referralModalCloseBtn) {
    referralModalCloseBtn.addEventListener("click", () => setReferralModalOpen(false));
  }
  const referralModalOverlay = document.getElementById("referral_modal_overlay");
  if (referralModalOverlay) {
    referralModalOverlay.addEventListener("click", () => setReferralModalOpen(false));
  }

  /* Trading Score modal handlers */
  const menuOpenTradingScore = document.getElementById("menu_open_trading_score");
  if (menuOpenTradingScore) {
    menuOpenTradingScore.addEventListener("click", () => {
      closeAllDropdowns();
      setTradingScoreModalOpen(true);
    });
  }
  const tsCloseBtn = document.getElementById("trading_score_close_btn");
  if (tsCloseBtn) tsCloseBtn.addEventListener("click", () => setTradingScoreModalOpen(false));
  const tsOverlay = document.getElementById("trading_score_overlay");
  if (tsOverlay) tsOverlay.addEventListener("click", () => setTradingScoreModalOpen(false));
  const tsApplyBtn = document.getElementById("ts_apply_btn");
  if (tsApplyBtn) tsApplyBtn.addEventListener("click", applyOptimalConfig);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && tradingScoreModalOpen) setTradingScoreModalOpen(false);
  });

  /* My Stats modal handlers */
  const menuOpenMyStats = document.getElementById("menu_open_my_stats");
  if (menuOpenMyStats) {
    menuOpenMyStats.addEventListener("click", () => {
      closeAllDropdowns();
      setMyStatsModalOpen(true);
    });
  }
  const myStatsCloseBtn = document.getElementById("my_stats_close_btn");
  if (myStatsCloseBtn) myStatsCloseBtn.addEventListener("click", () => setMyStatsModalOpen(false));
  const myStatsOverlay = document.getElementById("my_stats_overlay");
  if (myStatsOverlay) myStatsOverlay.addEventListener("click", () => setMyStatsModalOpen(false));

  /* Telegram modal handlers */
  const menuOpenTelegramBtn = document.getElementById("menu_open_telegram");
  if (menuOpenTelegramBtn) {
    menuOpenTelegramBtn.addEventListener("click", () => {
      closeAllDropdowns();
      setTelegramModalOpen(true);
    });
  }
  const telegramCloseBtn = document.getElementById("telegram_close_btn");
  if (telegramCloseBtn) telegramCloseBtn.addEventListener("click", () => setTelegramModalOpen(false));
  const telegramOverlay = document.getElementById("telegram_overlay");
  if (telegramOverlay) telegramOverlay.addEventListener("click", () => setTelegramModalOpen(false));
  const telegramEnabledInput = document.getElementById("telegram_enabled");
  if (telegramEnabledInput) telegramEnabledInput.addEventListener("change", _telegramToggle);
  const telegramUnlinkBtn = document.getElementById("telegram_unlink_btn");
  if (telegramUnlinkBtn) telegramUnlinkBtn.addEventListener("click", _telegramUnlink);
  const telegramDismissCheck = document.getElementById("telegram_dismiss_check");
  if (telegramDismissCheck) telegramDismissCheck.addEventListener("change", () => {
    if (telegramDismissCheck.checked) {
      localStorage.setItem(_TG_DISMISS_KEY, "1");
    } else {
      localStorage.removeItem(_TG_DISMISS_KEY);
    }
  });

  /* Feedback modal handlers */
  const menuOpenFeedbackBtn = document.getElementById("menu_open_feedback");
  if (menuOpenFeedbackBtn) {
    menuOpenFeedbackBtn.addEventListener("click", () => {
      closeAllDropdowns();
      setFeedbackModalOpen(true);
    });
  }
  const feedbackCloseBtn = document.getElementById("feedback_close_btn");
  if (feedbackCloseBtn) feedbackCloseBtn.addEventListener("click", () => setFeedbackModalOpen(false));
  const feedbackOverlay = document.getElementById("feedback_overlay");
  if (feedbackOverlay) feedbackOverlay.addEventListener("click", () => setFeedbackModalOpen(false));
  const feedbackSendBtn = document.getElementById("feedback_send_btn");
  if (feedbackSendBtn) {
    feedbackSendBtn.addEventListener("click", async () => {
      const textarea = document.getElementById("feedback_message");
      const statusEl = document.getElementById("feedback_status");
      const message = (textarea?.value || "").trim();
      if (!message) { statusEl.textContent = "Write a message first."; statusEl.style.color = "#ff6b6b"; return; }
      feedbackSendBtn.disabled = true;
      statusEl.textContent = "Sending…";
      statusEl.style.color = "rgba(255,255,255,0.5)";
      try {
        const resp = await fetchWithTimeout("/api/feedback", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ message }),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        statusEl.textContent = "Sent! Thank you.";
        statusEl.style.color = "#4ade80";
        textarea.value = "";
        _feedbackLoadHistory();
      } catch (e) {
        statusEl.textContent = e.message || "Failed to send.";
        statusEl.style.color = "#ff6b6b";
      } finally {
        feedbackSendBtn.disabled = false;
      }
    });
  }

  /* PnL Share modal handlers */
  const pnlShareCloseBtn = document.getElementById("pnl_share_close_btn");
  if (pnlShareCloseBtn) pnlShareCloseBtn.addEventListener("click", () => _pnlShareSetOpen(false));
  const pnlShareOverlay = document.getElementById("pnl_share_overlay");
  if (pnlShareOverlay) pnlShareOverlay.addEventListener("click", () => _pnlShareSetOpen(false));
  document.getElementById("pnl_share_download")?.addEventListener("click", _pnlShareDownload);
  document.getElementById("pnl_share_copy")?.addEventListener("click", _pnlShareCopy);
  document.getElementById("pnl_share_tweet")?.addEventListener("click", _pnlShareTweet);
  // Overlay buttons
  document.querySelectorAll(".pnl-share-ov-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      if (btn.dataset.overlay === "custom") {
        document.getElementById("pnl_share_custom_file")?.click();
        return;
      }
      _pnlShareOverlay = btn.dataset.overlay;
      document.querySelectorAll(".pnl-share-ov-btn").forEach(b => b.classList.toggle("active", b.dataset.overlay === _pnlShareOverlay));
      _pnlShareRender();
    });
  });
  // Custom image upload
  document.getElementById("pnl_share_custom_file")?.addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        _pnlShareCustomImg = img;
        _pnlShareOverlay = "custom";
        // Update thumbnail
        const customBtn = document.querySelector(".pnl-share-ov-btn--custom");
        if (customBtn) { customBtn.style.backgroundImage = `url(${img.src})`; customBtn.textContent = ""; }
        document.querySelectorAll(".pnl-share-ov-btn").forEach(b => b.classList.toggle("active", b.dataset.overlay === "custom"));
        _pnlShareRender();
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
    e.target.value = "";
  });
  // Display option checkboxes
  document.getElementById("pnl_share_opt_pnl")?.addEventListener("change", _pnlShareRender);
  document.getElementById("pnl_share_opt_volume")?.addEventListener("change", _pnlShareRender);

  const menuOpenHipVolBtn = document.getElementById("menu_open_hipvol");
  if (menuOpenHipVolBtn) {
    menuOpenHipVolBtn.addEventListener("click", () => {
      closeAllDropdowns();
      setHipVolDrawerOpen(true);
    });
  }
  const hipVolCloseBtn = document.getElementById("hipvol_close_btn");
  if (hipVolCloseBtn) {
    hipVolCloseBtn.addEventListener("click", () => setHipVolDrawerOpen(false));
  }
  const hipVolOverlayEl = document.getElementById("hipvol_overlay");
  if (hipVolOverlayEl) {
    hipVolOverlayEl.addEventListener("click", () => setHipVolDrawerOpen(false));
  }
  initHipVolDrawer();

  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (_pnlShareIsOpen) { _pnlShareSetOpen(false); return; }
    if (telegramModalOpen) { setTelegramModalOpen(false); return; }
    if (myStatsModalOpen)  { setMyStatsModalOpen(false);  return; }
    if (referralModalOpen) { setReferralModalOpen(false); return; }
    if (hipVolDrawerOpen) { setHipVolDrawerOpen(false); return; }
    const anyDropdownOpen = document.querySelector(".header-dropdown.open");
    if (anyDropdownOpen) { closeAllDropdowns(); return; }
  });
  window.addEventListener("resize", () => {
    if (statsDrawerOpen) {
      drawStatsDailyVolumeChart(lastStatsSeries);
    }
  });

  function showMainPageAfterConnect() {
    // Navigate to the correct app route if still on landing
    const view = getCurrentView();
    if (view === "landing") {
      navigate(ROUTE_PATHS[_activeBotType] || "/market-making");
    } else {
      applyRoute();
    }
    // Hyperspace arrival animation
    mainPage.classList.remove("hyperspace-arrival");
    void mainPage.offsetWidth;
    mainPage.classList.add("hyperspace-arrival");
    window.setTimeout(() => {
      mainPage.classList.remove("hyperspace-arrival");
    }, 420);
    closeAllDropdowns();
    // Sync wallets drawer & dex selector UI
    syncWalletsDrawerUI();
    updateDexSelectorUI();
    // Activate connected state (metrics, polling, etc.)
    activateAfterConnect();
    // Load user's saved advanced strategies (3 slots)
    fetchAdvancedPresets();
  }

  // Auto-connect: detect wallet via eth_accounts (no popup) and restore/re-auth session.
  (async () => {
    try {
      // 1. Detect wallet from provider (silent : no popup)
      if (window.ethereum && typeof window.ethereum.request === "function") {
        const accounts = await window.ethereum.request({ method: "eth_accounts" });
        const detectedAddr = Array.isArray(accounts) && accounts.length ? String(accounts[0]) : "";
        if (detectedAddr && detectedAddr.startsWith("0x")) {
          const accountInput = document.getElementById("account_address");
          if (accountInput && !accountInput.value.trim()) {
            setConnectedAccount(detectedAddr);
          }
        }
      }
      // 2. If we have a stored token, validate it with the server
      const account = document.getElementById("account_address")?.value?.trim() || "";
      if (walletAuthToken && walletAuthAccount && walletAuthExpiresAtMs > Date.now() && account) {
        const status = await refreshWalletAuthStatus({ force: true });
        if (status.authenticated) {
          // Restore multi-DEX state and auto-connect if any DEX was connected
          restoreDexState();
          const connected = connectedDexes();
          if (connected.length > 0) {
            _syncDexDomState(_activeDex);
            updateMarketSourceForMode();
            ensureMarketsForCurrentMode().catch(() => {});
            // Wallet auth is enough to inspect and stop an already-running bot.
            // Do not keep the user on the landing page while slower approval
            // checks run; those checks are only required before starting a new
            // session.
            showMainPageAfterConnect();
            _allApprovalsValidForConnectedDexes(connected).then((approvalsOk) => {
              if (!approvalsOk && getCurrentView() !== "outcomes" && !anySlotRunning()) _cmOpenForReApproval(connected[0] || _activeDex);
            }).catch(() => {});
            return;
          }
          // Legacy fallback: check saved mode
          const savedMode = document.getElementById("mode")?.value || "hyperliquid_mainnet";
          if (savedMode.startsWith("hyperliquid") || isExtendedMode(savedMode)) {
            const legacyDex = isExtendedMode(savedMode) ? "extended" : "hyperliquid";
            showMainPageAfterConnect();
            _allApprovalsValidForSavedMode(savedMode).then((approvalsOk) => {
              if (!approvalsOk && getCurrentView() !== "outcomes" && !anySlotRunning()) _cmOpenForReApproval(legacyDex);
            }).catch(() => {});
            return;
          }
        }
      }
    } catch (_) {
      // Auto-connect failed silently : user can connect manually
    }
  })();

  const setConnectLoading = (loading) => {
    if (!connectBtn) return;
    connectBtn.disabled = loading;
    connectBtn.classList.toggle("loading", loading);
    if (loading) {
      connectBtn.textContent = "Connecting...";
    } else {
      updateConnectButtonLabel();
    }
  };

  document.getElementById("connect_continue").addEventListener("click", async () => {
    if (hyperspaceTransitionInFlight || setupAllInFlight) return;
    // Choix de la venue (Hyperliquid / Extended) avant toute connexion wallet.
    if (!(await chooseVenueAndSelect())) return;
    clearError("connect_error");
    setConnectLoading(true);
    setSignatureStepsVisible(true);
    let transition = null;
    try {
      const setupOk = await runSetupAllSignatures();
      if (!setupOk) return;
      await refreshKeyStatus();
      await refreshWalletAuthStatus({ force: true });
      await refreshReferralStatus();

      // Load markets for the active dex
      const symbolSelect = document.getElementById("market_symbol");
      if (!symbolSelect || !symbolSelect.options || symbolSelect.options.length === 0) {
        await loadMarkets();
      }
      transition = startHyperspaceTransition();

      // Save HL API key if provided
      const account = document.getElementById("account_address").value.trim();
      const key = document.getElementById("api_key").value.trim();
      if (key && _selectedDexes.has("hyperliquid")) {
        await apiPost("/api/keys", { hl_private_key: key, account_address: account });
        hasStoredApiKeyForAccount = true;
      }

      if (transition) {
        await new Promise((resolve) => window.setTimeout(resolve, HYPERSPACE_POST_LOAD_HOLD_MS));
        await transition.finish(showMainPageAfterConnect);
      } else {
        showMainPageAfterConnect();
      }
    } catch (e) {
      if (transition) await transition.cancel();
      showError("connect_error", `Connection failed: ${e.message || e}`);
    } finally {
      setSignatureStepsVisible(false);
      setConnectLoading(false);
    }
  });
  // ── Logo home link ──
  const logoLink = document.getElementById("logo_home_link");
  if (logoLink) {
    logoLink.addEventListener("click", (e) => {
      e.preventDefault();
      window.location.href = "/";
    });
  }

  // ── Header "Connect Wallet" button ──
  const headerConnectBtn = document.getElementById("header_connect_btn");
  if (headerConnectBtn) {
    headerConnectBtn.addEventListener("click", async () => {
      if (setupAllInFlight) return;
      if (!(await chooseVenueAndSelect())) return;
      headerConnectBtn.classList.add("loading");
      headerConnectBtn.textContent = "Connecting...";
      try {
        const setupOk = await runSetupAllSignatures();
        if (!setupOk) return;
        await refreshKeyStatus();
        await refreshWalletAuthStatus({ force: true });
        await refreshReferralStatus();
        const mode = document.getElementById("mode")?.value || "hyperliquid_mainnet";
        await refreshBuilderStatus({ force: true });
        await refreshAgentStatus({ force: true });
        if (isExtendedMode(mode)) {
          await refreshExtendedCredentialsStatus({ force: true });
        }
        if (isRisexMode(mode)) {
          try { await refreshRisexStatus({ force: true }); } catch (_) {}
        }
        _syncDexDomState(_activeDex);
        await ensureMarketsForCurrentMode();
        const toml = buildToml({ allowDefaults: true, slot: activeDexSlot() });
        await apiPost(`/api/config?slot=${activeDexSlot()}`, { toml });
        showMainPageAfterConnect();
      } catch (e) {
        const errTarget = getCurrentView() === "landing" ? "connect_error" : "main_error";
        showError(errTarget, `Connection failed: ${e.message || e}`);
      } finally {
        headerConnectBtn.classList.remove("loading");
        headerConnectBtn.textContent = "Connect Wallet";
      }
    });
  }

  // ── Running slot card stop buttons (delegated) ──
  document.getElementById("running_slots")?.addEventListener("click", async (e) => {
    // ── Schedule toggle button ──
    const schedBtn = e.target.closest("[data-slot-sched]");
    if (schedBtn) {
      e.stopPropagation();
      const slotId = Number(schedBtn.dataset.slotSched);
      // If schedule is currently active, clicking disables it
      if (_currentSchedule && _currentSchedule.enabled) {
        await _saveSchedule({ enabled: false, mode: "recurring", start_time: "", stop_time: "", standby: false });
      } else {
        _openScheduleEditor(schedBtn, slotId);
      }
      return;
    }
    // ── Edit target volume pencil ──
    const editBtn = e.target.closest("[data-slot-edit-vol]");
    if (editBtn) {
      e.stopPropagation();
      const slotId = Number(editBtn.dataset.slotEditVol);
      const minVol = Math.ceil(parseFloat(editBtn.dataset.slotMinVol) || 0);
      const currentTarget = editBtn.dataset.slotCurrentTarget ? parseFloat(editBtn.dataset.slotCurrentTarget) : null;
      _openSlotTargetEditor(editBtn, slotId, minVol, currentTarget);
      return;
    }
    // ── Pause toggle ──
    const pauseBtn = e.target.closest("[data-slot-pause]");
    if (pauseBtn) {
      e.stopPropagation();
      const sid = Number(pauseBtn.dataset.slotPause);
      const pauseSlotObj = runningSlots.find(s => s.slot === sid);
      const pauseDex = (pauseSlotObj && pauseSlotObj.dex) || _activeDex;
      pauseBtn.disabled = true;
      try {
        _syncDexDomState(pauseDex);
        const desiredPaused = !(pauseSlotObj && pauseSlotObj.paused);
        const res = await apiPost(`/api/pause?slot=${sid}`, { paused: desiredPaused }, { dex: pauseDex });
        const nowPaused = !!res.paused;
        pauseBtn.classList.toggle("active", nowPaused);
        pauseBtn.textContent = nowPaused ? "▶" : "⏸";
        pauseBtn.title = nowPaused ? "Resume quoting" : "Pause quoting";
        playTone(nowPaused ? 330 : 440, 0.08);
      } catch (err) {
        showError("main_error", `Pause failed: ${err.message || err}`);
      } finally {
        _syncDexDomState(_activeDex);
        pauseBtn.disabled = false;
      }
      return;
    }
    // ── Stop single slot ──
    const stopBtn = e.target.closest("[data-slot-stop]");
    if (!stopBtn) return;
    const slotId = Number(stopBtn.dataset.slotStop);
    // Cancel standby : just clear the standby flag, don't call /api/stop
    const standbySlot = runningSlots.find(s => s.slot === slotId && s.standby);
    if (standbySlot) {
      try { await _cancelStandby(); } catch (err) {
        showError("main_error", `Cancel failed: ${err.message || err}`);
      }
      return;
    }
    if (_slotStoppingSet.has(slotId)) return;
    _slotStoppingSet.add(slotId);
    stopBtn.disabled = true;
    stopBtn.classList.add("loading");
    stopBtn.textContent = "";
    // Determine the DEX for this slot to use the correct auth token
    const stopSlotObj = runningSlots.find(s => s.slot === slotId);
    const slotDexName = (stopSlotObj && stopSlotObj.dex) || _activeDex;
    try {
      playTone(220, 0.12);
      clearError("main_error");
      _syncDexDomState(slotDexName);
      await apiPost(`/api/stop?slot=${slotId}`);
      _syncDexDomState(_activeDex);
      refreshPnlHistory();
    } catch (err) {
      showError("main_error", `Stop failed: ${err.message || err}`);
    } finally {
      _syncDexDomState(_activeDex);
      _slotStoppingSet.delete(slotId);
      // Re-query depuis le conteneur : la carte a pu être re-render pendant
      // l'await (le bouton capturé serait détaché du DOM).
      const currentBtn = document.getElementById("running_slots")?.querySelector(`[data-slot-stop="${slotId}"]`);
      if (currentBtn) {
        currentBtn.disabled = false;
        currentBtn.classList.remove("loading");
        currentBtn.textContent = "Stop";
      }
    }
  });
  // Stop All : send stop to each connected DEX with the right token
  document.getElementById("stop_all_btn")?.addEventListener("click", async () => {
    try {
      playTone(220, 0.12);
      clearError("main_error");
      const connected = connectedDexes();
      if (connected.length === 0) {
        await apiPost("/api/stop?slot=all");
      } else {
        // Stop every running slot across all connected DEXes
        const toStop = runningSlots.filter(s => (s.running || s.degraded) && connected.includes(s.dex));
        if (toStop.length) {
          for (const sl of toStop) {
            try {
              _syncDexDomState(sl.dex || _activeDex);
              await apiPost(`/api/stop?slot=${sl.slot}`);
            } catch (_) {}
          }
        } else {
          // Fallback: stop primary slots
          for (const dex of connected) {
            try {
              _syncDexDomState(dex);
              await apiPost(`/api/stop?slot=${dexSlotNumber(dex)}`);
            } catch (_) {}
          }
        }
        _syncDexDomState(_activeDex);
      }
      refreshPnlHistory();
    } catch (err) {
      showError("main_error", `Stop all failed: ${err.message || err}`);
    }
  });
  // ── Range slider ↔ number input sync ──
  const _targetVolRange = document.getElementById("target_vol_range");
  const _targetVolNum = document.getElementById("target_vol");
  const _maxLossRange = document.getElementById("max_loss_range");
  const _maxLossNum = document.getElementById("max_loss");

  if (_targetVolRange && _targetVolNum) {
    _syncRangeTrack(_targetVolRange);
    _targetVolRange.addEventListener("input", () => {
      _targetVolNum.value = _fmtNum(_targetVolRange.value);
      _syncRangeTrack(_targetVolRange);
      persistRiskInputs();
      scheduleLiveConfigSync(400);
    });
    _targetVolNum.addEventListener("focus", () => {
      _targetVolNum.value = String(_parseFormattedNum(_targetVolNum.value) || "");
    });
    _targetVolNum.addEventListener("blur", () => {
      const v = _parseFormattedNum(_targetVolNum.value);
      if (v > 0) _targetVolNum.value = _fmtNum(v);
      persistRiskInputs();
    });
    _targetVolNum.addEventListener("input", () => {
      const v = _parseFormattedNum(_targetVolNum.value);
      _targetVolRange.value = Math.min(Math.max(v, 10000), 5000000);
      _syncRangeTrack(_targetVolRange);
    });
  }
  if (_maxLossRange && _maxLossNum) {
    _syncRangeTrack(_maxLossRange);
    _maxLossRange.addEventListener("input", () => {
      _maxLossNum.value = _fmtNum(_maxLossRange.value);
      _syncRangeTrack(_maxLossRange);
    });
    _maxLossNum.addEventListener("focus", () => {
      const v = _parseFormattedNum(_maxLossNum.value);
      _maxLossNum.value = v > 0 ? String(v) : "";
    });
    _maxLossNum.addEventListener("blur", () => {
      const v = _parseFormattedNum(_maxLossNum.value);
      if (v > 0) _maxLossNum.value = _fmtNum(v);
    });
    _maxLossNum.addEventListener("input", () => {
      const v = _parseFormattedNum(_maxLossNum.value);
      _maxLossRange.value = Math.min(Math.max(v, 5), 100);
      _syncRangeTrack(_maxLossRange);
    });
  }

  // ── Schedule config toggle & save ──
  document.getElementById("stop_mode_btn_vol")?.addEventListener("click", () => _setStopMode(false));
  document.getElementById("stop_mode_btn_sched")?.addEventListener("click", () => _setStopMode(true));
  document.getElementById("sched_save_btn")?.addEventListener("click", () => _saveScheduleFromConfig());
  document.getElementById("sched_recurring_toggle")?.addEventListener("click", (e) => {
    const btn = e.currentTarget;
    btn.classList.toggle("active");
    const on = btn.classList.contains("active");
    btn.title = on ? "Recurring: ON \u2014 runs every day. Click to switch to one-shot." : "Recurring: OFF \u2014 runs once then stops. Click to repeat daily.";
  });

  // Main action button: START current token or STOP it if running
  document.getElementById("main_action_btn").addEventListener("click", async () => {
    const btn = document.getElementById("main_action_btn");
    const sym = _currentSymbol();
    // Find the running slot for this symbol, or any running slot on this DEX
    const rs = _getRunningSlot(sym);
    const isRunning = !!(rs && (rs.running || rs.degraded));
    const isStandby = !!(rs && rs.standby && !rs.running);

    if (isStandby) {
      // --- CANCEL standby ---
      try { await _cancelStandby(); } catch (err) {
        showError("main_error", `Cancel failed: ${err.message || err}`);
      }
      return;
    }

    if (isRunning) {
      // --- STOP current token ---
      if (stopRequestInFlight) return;
      stopRequestInFlight = true;
      setActionBtnState("stopping");
      const stopDex = rs.dex || _activeDex;
      try {
        playTone(220, 0.12);
        clearError("main_error");
        _syncDexDomState(stopDex);
        await apiPost(`/api/stop?slot=${rs.slot}`);
        _syncDexDomState(_activeDex);
        refreshPnlHistory();
        // Stop runs async on the server : keep "stopping" state and let status
        // polling resolve to "stopped" once the bot has fully stopped.
      } catch (e) {
        showError("main_error", `Stop failed: ${e.message || e}`);
        setActionBtnState("running");
      } finally {
        stopRequestInFlight = false;
      }
    } else {
      // --- START current token ---
      // Block start if schedule mode is selected but not saved yet
      if (_scheduleMode && !(_currentSchedule && _currentSchedule.enabled)) {
        showError("main_error", "Save your schedule first, or switch back to Target Volume.");
        return;
      }
      // Schedule standby: arm the schedule instead of starting the bot now
      if (_currentSchedule && _currentSchedule.enabled && _currentSchedule.start_time) {
        const now = new Date();
        const [sh, sm] = _currentSchedule.start_time.split(":").map(Number);
        const schedStart = new Date(now);
        schedStart.setHours(sh, sm, 0, 0);
        let needsStandby = now < schedStart; // start_time in the future today
        // Or entire window passed → next run is tomorrow
        if (!needsStandby && _currentSchedule.stop_time) {
          const [eh, em] = _currentSchedule.stop_time.split(":").map(Number);
          const schedEnd = new Date(now);
          schedEnd.setHours(eh, em, 0, 0);
          // Overnight schedule (stop < start): roll end to next day
          if (schedEnd <= schedStart) schedEnd.setDate(schedEnd.getDate() + 1);
          if (now >= schedEnd) needsStandby = true;
        }
        if (needsStandby) {
          try {
            await _saveSchedule({ ..._currentSchedule, standby: true });
            clearError("main_error");
            // Optimistic: inject waiting slot immediately
            const sym = normalizeSelectedSymbol(document.getElementById("market_symbol")?.value || "");
            const standbySlot = activeDexSlot();
            if (!runningSlots.find(s => s.slot === standbySlot)) {
              runningSlots.push({ slot: standbySlot, symbol: sym, dex: _activeDex, running: false, degraded: false, standby: true, elapsedText: "waiting", schedProgress: null, sessionPnl: null });
              renderRunningSlots();
            }
            _syncMainActionBtn();
          } catch (err) {
            showError("main_error", `Failed to arm schedule: ${err.message || err}`);
          }
          return;
        }
      }
      playMarioCoin();
      const extendedMode = _activeDex === "extended";
      const nadoMode = _activeDex === "nado";
      const risexMode = _activeDex === "risex";
      const qfexMode = _activeDex === "qfex";
      setActionBtnState("loading");
      _spawnStartParticles(btn);
      _syncDexDomState(_activeDex);
      await refreshKeyStatus();
      await refreshWalletAuthStatus({ force: true });
      if (nadoMode) {
        await refreshNadoAgentStatus({ force: true });
        if (!nadoAgentApprovedForAccount) {
          try {
            await approveNadoAgentWithWallet();
            await refreshNadoAgentStatus({ force: true });
          } catch (e) {
            showError("main_error", `Nado agent approval failed: ${e.message || e}`);
            setActionBtnState("stopped");
            return;
          }
        }
        // Ensure config is saved with nado mode
        _syncDexDomState(_activeDex);
        try {
          const toml = buildToml({ allowDefaults: true, slot: activeDexSlot() });
          await apiPost(`/api/config?slot=${activeDexSlot()}`, { toml });
        } catch (e) {
          showError("main_error", `Config save failed: ${e.message || e}`);
          setActionBtnState("stopped");
          return;
        }
      } else if (qfexMode) {
        // QFEX : clé enregistrée, valide et sans retrait (relue chez QFEX), puis
        // marchés et solde QFEX frais. Le serveur refait les mêmes gardes.
        let qxSt = null;
        try {
          qxSt = await refreshQfexStatus({ force: true });
        } catch (e) {
          showError("main_error", `QFEX status check failed: ${e.message || e}`);
          setActionBtnState("stopped");
          return;
        }
        if (!(qxSt && qxSt.credentials_saved && qxSt.key_valid && qxSt.trade_only)) {
          setActionBtnState("stopped");
          showError("main_error", (qxSt && qxSt.error) || "QFEX API key missing. Complete the \"Connect QFEX\" step before launching.");
          try { _connectSingleDex("qfex"); } catch (_) {}
          return;
        }
        if (!(Number(qxSt.available_balance) > 0)) {
          setActionBtnState("stopped");
          showError("main_error", "No available balance on QFEX. Deposit USDC on qfex.com, then start again.");
          return;
        }
        if (await ensureMarketsForCurrentMode()) {
          setActionBtnState("stopped");
          showError("main_error", "QFEX markets were just loaded. Check the selected token, then press Start again.");
          return;
        }
        metricsInFlight = false;
        try { await refreshMetrics(); } catch (_) {}
        if (!Number.isFinite(latestAvailableNotional)) {
          setActionBtnState("stopped");
          showError("main_error", "Could not read your QFEX balance yet. Wait a few seconds, then start again.");
          return;
        }
      } else if (risexMode) {
        // RISEx : statut frais (builder chez RISEx, signer du bot, dépôt) avant le
        // lancement. Le serveur refait les mêmes gardes ; ici on oriente l'utilisateur.
        let rxSt = null;
        try {
          rxSt = await refreshRisexStatus({ force: true });
        } catch (e) {
          showError("main_error", `RISEx status check failed: ${e.message || e}`);
          setActionBtnState("stopped");
          return;
        }
        if (!(rxSt && rxSt.builder && rxSt.builder.approved)) {
          setActionBtnState("stopped");
          showError("main_error", "RISEx builder fee not approved. Complete the \"Approve builder fee\" step before launching.");
          try { _connectSingleDex("risex"); } catch (_) {}
          return;
        }
        if (!_risexSignerReady(rxSt.signer)) {
          setActionBtnState("stopped");
          showError("main_error", "RISEx bot signer missing, expired or expiring within 24 h. Renew it before launching.");
          try { _risexOpenRenewal(); } catch (_) {}
          return;
        }
        if (rxSt.has_collateral === false) {
          setActionBtnState("stopped");
          showError("main_error", "No USDC deposit on this RISEx account. Deposit USDC on rise.trade, then start again.");
          return;
        }
        // Marchés et solde RISEx frais : le collatéral engagé est plafonné par le solde
        // libre RISEx, jamais par celui d'une autre venue resté à l'écran.
        if (await ensureMarketsForCurrentMode()) {
          // La liste vient d'être rechargée : le token sélectionné a pu changer.
          setActionBtnState("stopped");
          showError("main_error", "RISEx markets were just loaded. Check the selected token, then press Start again.");
          return;
        }
        metricsInFlight = false;
        try { await refreshMetrics(); } catch (_) {}
        if (!Number.isFinite(latestAvailableNotional)) {
          setActionBtnState("stopped");
          showError("main_error", "Could not read your RISEx balance yet. Wait a few seconds, then start again.");
          return;
        }
      } else {
        // Re-vérification on-chain FRAÎCHE avant le lancement (force). L'approbation
        // builder/agent a pu être révoquée depuis la connexion : sans ce gate, l'UI
        // affichait « connecté » puis /api/start renvoyait un 400 « builder fee revoked ».
        // En mode Extended il n'y a pas d'agent TRADOR : seul le builder fee est
        // requis on-chain (les credentials Extended sont vérifiés juste après).
        // Appeler refreshAgentStatus() ici renverrait {approved:false} et
        // bloquerait tout lancement Extended.
        const [bSt, aSt] = await Promise.all([
          refreshBuilderStatus({ force: true }),
          extendedMode ? Promise.resolve(null) : refreshAgentStatus({ force: true }),
        ]);
        const builderOk = bSt ? !!bSt.approved : !!builderApprovedForAccount;
        const agentOk = extendedMode ? true : (aSt ? !!aSt.approved : !!agentApprovedForAccount);
        if (!builderOk || !agentOk) {
          setActionBtnState("stopped");
          const missing = !builderOk ? "builder fee" : "TRADOR agent";
          showError(
            "main_error",
            `On-chain ${missing} approval missing or revoked. Re-approve before launching.`,
          );
          // Rouvrir le flux de connexion sur l'étape manquante plutôt que de subir un 400.
          try { _connectSingleDex(_activeDex); } catch (_) {}
          return;
        }
      }
      if (extendedMode) {
        await refreshExtendedCredentialsStatus({ force: true });
        if (!extendedCredentialsReadyForAccount) {
          showError("main_error", "Extended not connected. Open the wallets drawer and connect Extended (wallet signatures).");
          setActionBtnState("stopped");
          return;
        }
      }
      clearError("main_error");
      const validationErr = validateConnectionFields() || validateStrategyFields();
      if (validationErr) {
        showError("main_error", validationErr);
        setActionBtnState("stopped");
        return;
      }
      // Block if this exact symbol is already running
      const existingRun = runningSlots.find(s => (s.running || s.degraded) && s.symbol === sym);
      if (existingRun) {
        showError("main_error", `${_slotDisplayToken(sym)} is already running (slot ${existingRun.slot}).`);
        setActionBtnState("stopped");
        return;
      }
      // Use primary slot if free, otherwise find next available
      const primarySlot = activeDexSlot();
      const primaryBusy = runningSlots.some(s => s.slot === primarySlot && (s.running || s.degraded));
      const slotId = primaryBusy ? _findFreeSlot() : primarySlot;
      if (slotId < 0) {
        showError("main_error", `All ${MM_SLOTS} slots are in use. Stop a token first.`);
        setActionBtnState("stopped");
        return;
      }
      try {
        playTone(420, 0.08);
        // Ensure DOM is synced to active DEX before building config
        _syncDexDomState(_activeDex);
        const toml = buildToml({ slot: slotId });
        await apiPost(`/api/config?slot=${slotId}`, { toml });
        const key = document.getElementById("api_key").value.trim();
        const account = document.getElementById("account_address").value.trim();
        if (key && !extendedMode) {
          await apiPost("/api/keys", { hl_private_key: key, account_address: account });
          await refreshKeyStatus();
        }
        setActionBtnState("starting");
        // /api/start runs synchronous prechecks (builder/agent/USDH/open
        // position with 6-10s timeouts each) - total can exceed 12s when HL
        // is slow. Use a longer client-side timeout, and on residual abort
        // treat as soft success: backend.start() is idempotent and status
        // polling will reconcile.
        try {
          await apiPost(`/api/start?slot=${slotId}`, {}, { timeoutMs: 30000 });
        } catch (startErr) {
          if (!(startErr && startErr.status === 408)) throw startErr;
          console.warn("[start] client timeout - relying on status polling to reconcile");
        }
        // Register optimistic slot : status polling will upgrade to "running" once confirmed
        if (!runningSlots.find(s => s.slot === slotId)) {
          runningSlots.push({
            slot: slotId,
            symbol: sym,
            dex: _activeDex,
            running: false,
            degraded: false,
            sessionPnl: null,
            elapsedText: "starting…",
            // Vault the session was launched with : autosaves during the optimistic
            // window must preserve it even if the user switches the picker.
            tradingAccount: getSelectedTradingAccount("mm"),
            _optimisticUntil: Date.now() + 15000, // keep for 15s until API confirms
          });
          renderRunningSlots();
        }
        const targetVolVal = _parseFormattedNum(document.getElementById("target_vol")?.value);
        if (Number.isFinite(targetVolVal) && targetVolVal > 0) {
          activeSessionTargetVolumeUsd = targetVolVal;
          persistActiveSessionTargetVolume(targetVolVal);
        }
        refreshPnlHistory();
        // Stay in "starting" state : polling will switch to "running" once bot confirms
      } catch (e) {
        const msg = String(e.message || e);
        const el = document.getElementById("main_error");
        if (el) {
          const isBalanceErr = /insufficient|balance/i.test(msg);
          const cta = isBalanceErr ? _fundSubCtaHtml("mm") : "";
          el.innerHTML = `Start failed: ${escapeHtml(msg)}${cta}`;
          el.classList.remove("hidden");
        }
        setActionBtnState("stopped");
      }
    }
  });

  // main page loads markets after connect

  if (changeBtn) {
    changeBtn.addEventListener("click", () => showLandingPage());
  }
  setInterval(refreshStatus, 5000);
  setInterval(refreshMetrics, 8000);
  setInterval(refreshPnlHistory, 10000);
  setInterval(() => {
    if (statsDrawerOpen) {
      refreshStatsPanel();
    }
  }, 5000);
  // Call immediately on load so badge/PnL don't show stale HTML defaults.
  refreshStatus();
  refreshMetrics();
  refreshPnlHistory();
  startTsBadgePolling();
}

let audioCtx;
function playTone(freq, dur, volume = 0.02, wave = "sine") {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    const now = audioCtx.currentTime;
    const safeDur = Math.max(0.02, Number(dur) || 0.05);
    osc.type = wave;
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(Math.max(0.0001, volume), now + 0.006);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + safeDur);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start(now);
    osc.stop(now + safeDur + 0.01);
  } catch (_) {
    // ignore audio failures
  }
}

function playMarioCoin() {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const vol = 0.08;
    const notes = [
      { freq: 988,  start: 0.00, dur: 0.07 },
      { freq: 1319, start: 0.07, dur: 0.22 },
    ];
    const now = audioCtx.currentTime;
    for (const { freq, start, dur } of notes) {
      const osc  = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = "square";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, now + start);
      gain.gain.exponentialRampToValueAtTime(vol, now + start + 0.005);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + start + dur);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(now + start);
      osc.stop(now + start + dur + 0.01);
    }
  } catch (_) {
    // ignore audio failures
  }
}

function initStarfield() {
  const layers = [
    { id: "starfield-1", count: 800, speed: 0.15, alpha: 0.95 },
    { id: "starfield-2", count: 500, speed: 0.35, alpha: 0.75 },
    { id: "starfield-3", count: 320, speed: 0.55, alpha: 0.6 },
  ];

  const starLayers = [];
  let mouseX = 0.5;
  let mouseY = 0.5;
  let px = window.innerWidth / 2;
  let py = window.innerHeight / 2;

  function makeStars(width, height, count) {
    return Array.from({ length: count }, () => ({
      x: Math.random() * width,
      y: Math.random() * height,
      r: Math.random() * 1.6 + 0.2,
      a: Math.random() * 0.8 + 0.35,
      t: Math.random() * Math.PI * 2,
      s: Math.random() * 0.5 + 0.1,
    }));
  }

  function resize() {
    starLayers.length = 0;
    layers.forEach((layer) => {
      const canvas = document.getElementById(layer.id);
      if (!canvas) return;
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      starLayers.push({
        canvas,
        ctx,
        config: layer,
        stars: makeStars(canvas.width, canvas.height, layer.count),
      });
    });
  }

  function draw() {
    for (const layer of starLayers) {
      const { ctx, canvas, stars, config } = layer;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const offsetX = (mouseX - 0.5) * 70 * config.speed;
      const offsetY = (mouseY - 0.5) * 70 * config.speed;
      px += (mouseX * canvas.width - px) * 0.08;
      py += (mouseY * canvas.height - py) * 0.08;
      ctx.fillStyle = "#ffffff";
      for (const star of stars) {
        star.t += 0.008 * star.s;
        const twinkle = 0.55 + 0.35 * (Math.sin(star.t) * 0.5 + 0.5);
        const dx = star.x - px;
        const dy = star.y - py;
        const dist2 = dx * dx + dy * dy + 1;
        const pull = 1800 / dist2; // gravity strength
        const warpX = -dx * pull * config.speed;
        const warpY = -dy * pull * config.speed;
        ctx.globalAlpha = star.a * twinkle * config.alpha;
        ctx.beginPath();
        ctx.arc(star.x + offsetX + warpX, star.y + offsetY + warpY, star.r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    }
    requestAnimationFrame(draw);
  }

  window.addEventListener("mousemove", (e) => {
    mouseX = e.clientX / window.innerWidth;
    mouseY = e.clientY / window.innerHeight;
  });
  window.addEventListener("resize", resize);
  resize();
  draw();
}


function initMainGalaxies() {
  const canvas = document.getElementById("main-galaxies-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const prefersReducedMotion =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const TAU = Math.PI * 2;
  const galaxyTemplates = [
    {
      cx: 0.03, cy: 0.52, radius: 0.20, arms: 4, spiral: 7.0, flatten: 0.62,
      rotation: 0.26, rotSpeed: 0.01, driftX: 0.006, driftY: 0.010, phase: 1.4,
      core: "106,208,255", stars: ["194,236,255", "132,204,255", "98,168,236"], count: 420,
    },
    {
      cx: 0.2, cy: 0.26, radius: 0.28, arms: 5, spiral: 8.3, flatten: 0.54,
      rotation: 0.18, rotSpeed: 0.015, driftX: 0.018, driftY: 0.012, phase: 0.9,
      core: "126,224,255", stars: ["200,242,255", "140,214,255", "110,186,255"], count: 620,
    },
    {
      cx: 0.79, cy: 0.2, radius: 0.24, arms: 4, spiral: 7.4, flatten: 0.6,
      rotation: -0.28, rotSpeed: -0.013, driftX: 0.016, driftY: 0.012, phase: 2.1,
      core: "255,178,108", stars: ["255,224,184", "255,188,126", "255,140,80"], count: 560,
    },
    {
      cx: 0.28, cy: 0.78, radius: 0.26, arms: 4, spiral: 7.9, flatten: 0.57,
      rotation: 0.52, rotSpeed: 0.011, driftX: 0.014, driftY: 0.016, phase: 4.2,
      core: "255,128,182", stars: ["255,208,225", "252,144,188", "210,92,145"], count: 540,
    },
    {
      cx: 0.77, cy: 0.74, radius: 0.22, arms: 5, spiral: 8.8, flatten: 0.58,
      rotation: -0.44, rotSpeed: -0.012, driftX: 0.012, driftY: 0.016, phase: 5.5,
      core: "166,142,255", stars: ["218,206,255", "172,156,255", "124,108,224"], count: 500,
    },
    {
      cx: 0.97, cy: 0.50, radius: 0.20, arms: 4, spiral: 7.2, flatten: 0.63,
      rotation: -0.24, rotSpeed: -0.011, driftX: 0.006, driftY: 0.010, phase: 3.2,
      core: "255,176,114", stars: ["255,222,186", "255,186,130", "244,136,86"], count: 420,
    },
  ];

  let width = 0;
  let height = 0;
  let dpr = 1;
  let visibility = 0;

  function createGalaxy(template) {
    const particles = new Array(template.count);
    for (let i = 0; i < template.count; i += 1) {
      const arm = i % template.arms;
      const radius = Math.pow(Math.random(), 0.88);
      const armAngle = (arm / template.arms) * TAU;
      const twist = radius * template.spiral;
      const jitter = (Math.random() - 0.5) * (1.3 - radius) * 1.7;
      const theta = armAngle + twist + jitter;
      const x = Math.cos(theta) * radius;
      const y = Math.sin(theta) * radius * template.flatten;
      const innerGlow = Math.pow(1 - radius, 1.9);
      const size = 0.3 + innerGlow * 2.3 + Math.random() * 0.8;
      const alpha = 0.08 + innerGlow * 0.45 + Math.random() * 0.16;
      const color = template.stars[(Math.random() * template.stars.length) | 0];
      particles[i] = { x, y, size, alpha, color };
    }

    const dustCount = Math.max(120, (template.count * 0.35) | 0);
    const dust = new Array(dustCount);
    for (let i = 0; i < dustCount; i += 1) {
      const radius = Math.pow(Math.random(), 1.5);
      const theta = Math.random() * TAU;
      const x = Math.cos(theta) * radius;
      const y = Math.sin(theta) * radius * (template.flatten * 1.15);
      const size = 0.7 + Math.random() * 2.8;
      const alpha = 0.02 + (1 - radius) * 0.09;
      dust[i] = { x, y, size, alpha };
    }

    return {
      ...template,
      particles,
      dust,
    };
  }

  const galaxies = galaxyTemplates.map(createGalaxy);

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = Math.max(1, Math.round(window.innerWidth));
    height = Math.max(1, Math.round(window.innerHeight));
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.max(1, Math.round(height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function drawGalaxyBody(g, nowSec, alphaScale) {
    const driftX = Math.sin(nowSec * (0.08 + g.driftX) + g.phase) * width * g.driftX;
    const driftY = Math.cos(nowSec * (0.07 + g.driftY) + g.phase * 0.7) * height * g.driftY;
    const cx = width * g.cx + driftX;
    const cy = height * g.cy + driftY;
    const radiusPx = Math.min(width, height) * g.radius;
    const angle = prefersReducedMotion ? g.rotation : g.rotation + nowSec * g.rotSpeed;
    const c = Math.cos(angle);
    const s = Math.sin(angle);

    const coreGradient = ctx.createRadialGradient(cx, cy, 0, cx, cy, radiusPx * 0.96);
    coreGradient.addColorStop(0, `rgba(${g.core}, ${0.22 * alphaScale})`);
    coreGradient.addColorStop(0.35, `rgba(${g.core}, ${0.12 * alphaScale})`);
    coreGradient.addColorStop(1, "rgba(8, 10, 22, 0)");
    ctx.fillStyle = coreGradient;
    ctx.fillRect(cx - radiusPx, cy - radiusPx, radiusPx * 2, radiusPx * 2);

    ctx.globalCompositeOperation = "screen";
    for (let i = 0; i < g.dust.length; i += 1) {
      const p = g.dust[i];
      const px = p.x * radiusPx;
      const py = p.y * radiusPx;
      const rx = px * c - py * s;
      const ry = px * s + py * c;
      ctx.fillStyle = `rgba(${g.core}, ${p.alpha * alphaScale})`;
      ctx.fillRect(cx + rx, cy + ry, p.size, p.size);
    }

    for (let i = 0; i < g.particles.length; i += 1) {
      const p = g.particles[i];
      const px = p.x * radiusPx;
      const py = p.y * radiusPx;
      const rx = px * c - py * s;
      const ry = px * s + py * c;
      const starAlpha = Math.min(1, p.alpha * alphaScale);
      ctx.fillStyle = `rgba(${p.color}, ${starAlpha})`;
      ctx.fillRect(cx + rx, cy + ry, p.size, p.size);
    }

    const innerGlow = ctx.createRadialGradient(cx, cy, 0, cx, cy, radiusPx * 0.32);
    innerGlow.addColorStop(0, `rgba(255,255,255,${0.18 * alphaScale})`);
    innerGlow.addColorStop(0.52, `rgba(${g.core},${0.11 * alphaScale})`);
    innerGlow.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = innerGlow;
    ctx.fillRect(cx - radiusPx * 0.36, cy - radiusPx * 0.36, radiusPx * 0.72, radiusPx * 0.72);
  }

  function render(nowMs) {
    const nowSec = nowMs * 0.001;
    const isMainSpace = document.body.classList.contains("main-space");
    const target = isMainSpace ? 1 : 0;
    const lerp = prefersReducedMotion ? 0.2 : 0.07;
    visibility += (target - visibility) * lerp;
    if (Math.abs(target - visibility) < 0.0005) visibility = target;

    ctx.clearRect(0, 0, width, height);
    if (visibility > 0.004) {
      ctx.save();
      const alphaScale = Math.max(0, Math.min(1, visibility));
      for (let i = 0; i < galaxies.length; i += 1) {
        drawGalaxyBody(galaxies[i], nowSec, alphaScale);
      }
      ctx.restore();
    }
    requestAnimationFrame(render);
  }

  window.addEventListener("resize", resize);
  resize();
  requestAnimationFrame(render);
}

function initCursorSun() {
  const sun = document.getElementById("cursor-sun");
  const burst = document.getElementById("cursor-click-burst");
  if (!sun) return;
  let tx = window.innerWidth / 2;
  let ty = window.innerHeight / 2;

  const moveSun = () => {
    // Keep position via left/top so CSS pulse animation can keep controlling transform.
    sun.style.left = `${tx}px`;
    sun.style.top = `${ty}px`;
  };

  function animate() {
    moveSun();
    requestAnimationFrame(animate);
  }

  const onMove = (e) => {
    tx = e.clientX;
    ty = e.clientY;
    moveSun(); // immediate update between RAF ticks
  };
  window.addEventListener("pointermove", onMove, { passive: true });
  window.addEventListener("mousemove", onMove, { passive: true });

  void burst; // unused : click spark handled by initClickSpark

  animate();
}

function initClickSpark() {
  const canvas = document.getElementById("click-spark-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  const SPARK_COLOR  = "rgba(190, 225, 255, 0.92)";
  const SPARK_SIZE   = 11;   // line length at start
  const SPARK_RADIUS = 46;   // max outward distance
  const SPARK_COUNT  = 8;
  const DURATION     = 480;  // ms

  function easeOut(t) { return t * (2 - t); }

  function resize() {
    canvas.width  = window.innerWidth;
    canvas.height = window.innerHeight;
  }
  window.addEventListener("resize", resize);
  resize();

  let sparks = [];

  function draw(ts) {
    requestAnimationFrame(draw);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    sparks = sparks.filter(s => {
      const elapsed = ts - s.t0;
      if (elapsed >= DURATION) return false;
      const p    = elapsed / DURATION;
      const e    = easeOut(p);
      const dist = e * SPARK_RADIUS;
      const len  = SPARK_SIZE * (1 - e);
      const x1   = s.x + dist * Math.cos(s.a);
      const y1   = s.y + dist * Math.sin(s.a);
      const x2   = s.x + (dist + len) * Math.cos(s.a);
      const y2   = s.y + (dist + len) * Math.sin(s.a);
      ctx.globalAlpha = 1 - p;
      ctx.strokeStyle = SPARK_COLOR;
      ctx.lineWidth   = 1.5;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
      return true;
    });
    ctx.globalAlpha = 1;
  }
  requestAnimationFrame(draw);

  window.addEventListener("pointerdown", e => {
    if (document.body.classList.contains("starfield-off")) return;
    const now = performance.now();
    for (let i = 0; i < SPARK_COUNT; i++) {
      sparks.push({ x: e.clientX, y: e.clientY, a: (2 * Math.PI * i) / SPARK_COUNT, t0: now });
    }
  });
}

function initTitleParallax() {
  const hero = document.querySelector(".hero-box");
  const title = document.getElementById("app_title");
  if (!hero || !title) return;
  const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduceMotion) return;
  hero.addEventListener("mousemove", (e) => {
    const rect = hero.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / Math.max(1, rect.width)) - 0.5;
    const py = ((e.clientY - rect.top) / Math.max(1, rect.height)) - 0.5;
    title.style.setProperty("--title-tilt-x", `${(px * 5).toFixed(2)}deg`);
    title.style.setProperty("--title-tilt-y", `${(-py * 5).toFixed(2)}deg`);
  });
  hero.addEventListener("mouseleave", () => {
    title.style.setProperty("--title-tilt-x", "0deg");
    title.style.setProperty("--title-tilt-y", "0deg");
  });
}

function initLandingScrollReveal() {
  const hintEl = document.getElementById("scroll_hint");
  const manifesto = document.getElementById("landing_manifesto");
  const features = document.getElementById("landing_features");
  const numbers = document.getElementById("landing_numbers");
  if (!manifesto) return;

  // Prevent double init
  if (manifesto.dataset.revealReady === "1") return;
  manifesto.dataset.revealReady = "1";

  const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ── Stagger helper: add .is-visible to children with delay ──
  function staggerReveal(parentEl, selector, baseDelay, interval) {
    const gap = interval || 150;
    const els = Array.from(parentEl.querySelectorAll(selector));
    els.forEach((el, i) => {
      setTimeout(() => el.classList.add("is-visible"), baseDelay + i * gap);
    });
  }

  // ── Counter animation ──
  function animateCounter(el, target, prefix, suffix) {
    if (!el || !Number.isFinite(target) || target <= 0) return;
    const isInt = el.dataset.integer === "1";
    const duration = 1800;
    const startTime = performance.now();
    const tick = (now) => {
      const elapsed = now - startTime;
      const progress = Math.min(1, elapsed / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      const current = target * eased;
      const formatted = isInt ? Math.round(current).toLocaleString("en-US") : formatCompactVolume(current);
      el.textContent = (prefix || "") + formatted + (suffix || "");
      if (progress < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  // ── Stats state: handles race between fetch and observer ──
  let _statsReady = false;
  let _numbersRevealed = false;

  function triggerCounters() {
    if (!_statsReady || !_numbersRevealed) return;
    document.querySelectorAll(".number-value").forEach(el => {
      const target = parseFloat(el.dataset.target);
      if (!Number.isFinite(target) || target <= 0) return;
      if (reduceMotion) {
        const p = el.dataset.prefix || "";
        const s = el.dataset.suffix || "";
        const formatted = el.dataset.integer === "1" ? Math.round(target).toLocaleString("en-US") : formatCompactVolume(target);
        el.textContent = p + formatted + s;
      } else {
        animateCounter(el, target, el.dataset.prefix || "", el.dataset.suffix || "");
      }
    });
  }

  // ── Fetch landing stats (public endpoint, no auth) ──
  let _landingStatsFetched = false;
  function fetchLandingStats() {
    if (_landingStatsFetched) return;
    _landingStatsFetched = true;
    fetchWithTimeout("/api/admin/stats/overview", {}, 8000)
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data) return;
        const volEl = document.getElementById("landing_stat_volume");
        const feesEl = document.getElementById("landing_stat_fees");
        const usersEl = document.getElementById("landing_stat_users");
        const vol = Number(data.all_time_volume_usd) || 0;
        const backendFees = Number(data.all_time_fees_usd);
        const fees = Number.isFinite(backendFees) && backendFees >= 0 ? backendFees : vol * PLATFORM_FEE_RATE;
        const users = Number(data.unique_connected_addresses) || 0;
        if (volEl) { volEl.dataset.target = vol; volEl.dataset.prefix = "$"; volEl.dataset.suffix = ""; }
        if (feesEl) { feesEl.dataset.target = fees; feesEl.dataset.prefix = "$"; feesEl.dataset.suffix = ""; }
        if (usersEl) { usersEl.dataset.target = users; usersEl.dataset.prefix = ""; usersEl.dataset.suffix = ""; }
        _statsReady = true;
        triggerCounters();
      })
      .catch(() => {});
  }
  // Pre-fetch stats early
  fetchLandingStats();

  const taglineEl = manifesto.querySelector(".tagline");

  if (reduceMotion) {
    if (taglineEl) taglineEl.classList.add("is-visible");
    document.querySelectorAll(".feature-card, .number-card").forEach(el => el.classList.add("is-visible"));
    _numbersRevealed = true;
    triggerCounters();
    return;
  }

  // ── Tagline: simple fade-in on scroll ──
  if (taglineEl) {
    const taglineObserver = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting) taglineEl.classList.add("is-visible");
      else taglineEl.classList.remove("is-visible");
    }, { threshold: 0.3 });
    taglineObserver.observe(manifesto);
  }

  // ── Other sections: IntersectionObserver (bidirectional) ──
  const sectionSelectors = new Map([
    [features, ".feature-card"],
    [numbers, ".number-card"],
  ]);

  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const section = entry.target;
      const selector = sectionSelectors.get(section);
      if (!selector) continue;

      if (entry.isIntersecting) {
        const interval = section === features ? 200 : 180;
        staggerReveal(section, selector, 0, interval);
        if (section === numbers) {
          _numbersRevealed = true;
          setTimeout(triggerCounters, 300);
        }
      } else {
        Array.from(section.querySelectorAll(selector)).forEach(el => el.classList.remove("is-visible"));
      }
    }
  }, { threshold: 0.08, rootMargin: "0px 0px -10% 0px" });

  [features, numbers].forEach(s => { if (s) observer.observe(s); });

  // ── Scroll hint ──
  if (hintEl) {
    const updateHint = () => hintEl.classList.toggle("is-hidden", (window.scrollY || 0) > 24);
    window.addEventListener("scroll", updateHint, { passive: true });
    updateHint();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// HIP-4 Volume Calculator — parse un export Hyperliquid trades CSV, ne garde que
// les coins HIP-4 (#<digits>), filtre par plage de dates (mois/jour) et somme le
// notional. 100% client-side. Accent orange rgb(255,140,60). Helpers locaux
// (autonomes) pour ne pas dépendre du closure Kpoints.
// ═══════════════════════════════════════════════════════════════════════════
function initHipVolDrawer() {
  const fileEl   = document.getElementById("hipvol_csv");
  const nameEl   = document.getElementById("hipvol_csvName");
  const calcBtn  = document.getElementById("hipvol_calcBtn");
  const resultEl = document.getElementById("hipvol_result");
  const breakEl  = document.getElementById("hipvol_breakdown");
  if (!fileEl || !calcBtn || !resultEl) return;

  function pickCol(row, keys) { for (const k of keys) { if (k in row) return row[k]; } return ""; }

  function parseCsv(text) {
    const rows  = [];
    const lines = String(text || "").replace(/\r/g, "").split("\n").filter((l) => l.trim().length);
    if (!lines.length) return rows;
    const delim   = lines[0].includes(";") && !lines[0].includes(",") ? ";" : ",";
    const headers = lines[0].split(delim).map((h) => h.trim().toLowerCase());
    for (let i = 1; i < lines.length; i++) {
      const cols = lines[i].split(delim);
      const row  = {};
      headers.forEach((h, idx) => { row[h] = (cols[idx] || "").trim(); });
      rows.push(row);
    }
    return rows;
  }

  function parseNum(v) {
    const raw  = String(v == null ? "" : v).trim();
    // (1,234.56) → -1234.56 ; strip currency/thousands separators.
    const norm = raw.replace(/\(([^)]+)\)/g, "-$1").replace(/[^0-9.\-]/g, "");
    const n = parseFloat(norm);
    return Number.isFinite(n) ? n : 0;
  }

  function parseRowDate(row) {
    const raw = pickCol(row, ["time", "timestamp", "date", "datetime", "created_at"]);
    if (!raw) return null;
    const val = String(raw).trim();
    if (/^\d+(\.\d+)?$/.test(val)) {
      const num = parseFloat(val);
      const ms  = num > 1e12 ? num : num * 1000; // sec vs ms epoch
      const d   = new Date(ms);
      return isNaN(d.getTime()) ? null : d;
    }
    const m = val.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/);
    if (m) {
      const d = new Date(+m[3], +m[2] - 1, +m[1], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0));
      return isNaN(d.getTime()) ? null : d;
    }
    if (/^\d{4}-\d{2}-\d{2}/.test(val)) {
      const d = new Date(val.length > 10 ? val : val + "T00:00:00");
      return isNaN(d.getTime()) ? null : d;
    }
    const p = Date.parse(val);
    return isNaN(p) ? null : new Date(p);
  }

  function getMonthDay(monthEl, dayEl) {
    const month = parseInt((monthEl && monthEl.value) || "", 10);
    const day   = parseInt((dayEl   && dayEl.value)   || "", 10);
    if (!month || !day || month < 1 || month > 12 || day < 1 || day > 31) return null;
    return { month, day };
  }

  function inRange(d, startMd, endMd) {
    if (!startMd && !endMd) return true;
    if (!d) return false;
    const key      = (d.getMonth() + 1) * 100 + d.getDate();
    const startKey = startMd ? startMd.month * 100 + startMd.day : null;
    const endKey   = endMd   ? endMd.month   * 100 + endMd.day   : null;
    if (startKey === null) return key <= endKey;
    if (endKey   === null) return key >= startKey;
    if (endKey < startKey) return key >= startKey || key <= endKey; // wrap year boundary
    return key >= startKey && key <= endKey;
  }

  // L'export trades Hyperliquid nomme les marchés HIP-4 (outcomes/predictions)
  // par leur libellé lisible, qui contient toujours " - " séparant la question
  // du côté (Yes/No) ou du candidat — ex "BTC above 67297 …? - Yes",
  // "Champions League Winner - Arsenal". Les perps/spot n'ont jamais " - "
  // (ils sont "NAME", "NAME (xyz)", "NAME (km)" ou "BASE/QUOTE"). On garde aussi
  // le format interne "#NNNN" au cas où une autre source l'utilise.
  function isHip4Coin(coin) {
    const s = String(coin || "").trim();
    return /^#\d+$/.test(s) || / - /.test(s);
  }

  function fmtUsd(v) {
    return "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function setResult(html, isError) {
    resultEl.innerHTML = html;
    resultEl.classList.add("visible");
    resultEl.classList.toggle("kq-pnl-negative", !!isError);
  }

  fileEl.addEventListener("change", () => {
    if (fileEl.files && fileEl.files[0]) {
      if (nameEl) nameEl.textContent = fileEl.files[0].name;
      setResult("File loaded. Click Calculate HIP-4 Volume.", false);
    } else if (nameEl) {
      nameEl.textContent = "No file chosen";
    }
  });

  calcBtn.addEventListener("click", () => {
    if (breakEl) { breakEl.hidden = true; breakEl.innerHTML = ""; }
    if (!fileEl.files || !fileEl.files[0]) { setResult("Please select a .csv file first.", true); return; }
    setResult("Calculating…", false);

    const reader = new FileReader();
    reader.onload = () => {
      const rows = parseCsv(reader.result);
      if (!rows.length) { setResult("No rows found in this CSV.", true); return; }
      const startMd = getMonthDay(document.getElementById("hipvol_startMonth"), document.getElementById("hipvol_startDay"));
      const endMd   = getMonthDay(document.getElementById("hipvol_endMonth"),   document.getElementById("hipvol_endDay"));

      let total = 0, count = 0, pnl = 0;
      const byCoin = {};
      const allCoins = {}; // tous les coins vus (pour diagnostic si 0 match)
      for (const row of rows) {
        const coin = pickCol(row, ["coin", "symbol", "market", "asset"]);
        const coinKey = String(coin).trim();
        if (coinKey) allCoins[coinKey] = (allCoins[coinKey] || 0) + 1;
        if (!isHip4Coin(coin)) continue;
        const d = parseRowDate(row);
        if (!inRange(d, startMd, endMd)) continue;
        let ntl = parseNum(pickCol(row, ["ntl", "notional", "value"]));
        if (!ntl) {
          const px = parseNum(pickCol(row, ["px", "price", "avg_price"]));
          const sz = parseNum(pickCol(row, ["sz", "size", "qty", "quantity"]));
          ntl = Math.abs(px * sz);
        }
        ntl = Math.abs(ntl);
        if (!ntl) continue;
        total += ntl; count++;
        // Realized PnL = sum of closedPnl, net of fees (HL export columns).
        pnl += parseNum(pickCol(row, ["closedpnl", "closed_pnl", "pnl"])) - Math.abs(parseNum(pickCol(row, ["fee"])));
        byCoin[coinKey] = (byCoin[coinKey] || 0) + ntl;
      }

      if (!count) {
        const distinct = Object.keys(allCoins);
        const sample = distinct.slice(0, 15).join(", ");
        const more = distinct.length > 15 ? `, … (+${distinct.length - 15})` : "";
        setResult(
          `No HIP-4 (outcomes) trades found. ${distinct.length} distinct coin(s) in the file: <code>${escapeHtml(sample)}${more}</code>`,
          true
        );
        return;
      }

      const rangeTxt = (startMd || endMd)
        ? ` (${startMd ? startMd.month + "/" + startMd.day : "…"} → ${endMd ? endMd.month + "/" + endMd.day : "…"})`
        : "";
      const pnlColor = pnl >= 0 ? "#34d399" : "#f87171";
      const pnlStr = (pnl >= 0 ? "+" : "-") + fmtUsd(Math.abs(pnl)).slice(1);
      let shareLine = "";
      if (typeof _hip4PlatformVol24hUsd === "number" && _hip4PlatformVol24hUsd > 0) {
        const pct = (total / _hip4PlatformVol24hUsd) * 100;
        const pctStr = pct >= 10 ? pct.toFixed(1) : pct.toFixed(2);
        shareLine = `<br><span style="opacity:.75;">= <strong style="color:rgb(255,140,60);">${pctStr}%</strong> of HL outcomes 24h volume (${fmtUsd(_hip4PlatformVol24hUsd).replace(/\.00$/, "")})</span>`;
      }
      setResult(
        `HIP-4 volume${rangeTxt}: <strong style="color:rgb(255,140,60);">${fmtUsd(total)}</strong> · ${count} trades`
        + `<br>Realized PnL (net fees): <strong style="color:${pnlColor};">${pnlStr}</strong>`
        + shareLine,
        false
      );

      if (breakEl) {
        const items = Object.entries(byCoin).sort((a, b) => b[1] - a[1]);
        breakEl.innerHTML = items.map(([c, v]) =>
          `<li><span class="hipvol-coin">${escapeHtml(c)}</span><span class="hipvol-coin-vol">${fmtUsd(v)}</span></li>`
        ).join("");
        breakEl.hidden = false;
      }
    };
    reader.onerror = () => setResult("Could not read this file.", true);
    reader.readAsText(fileEl.files[0]);
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// HIP-4 Prediction Markets - UI controller
// Section dédiée. Backend déjà testé. Branchée sur :
//   GET  /api/markets?mode=hip4&exchange=hyperliquid_mainnet  (poll 5s)
//   GET  /api/status                                          (when session active)
//   POST /api/config?slot=N + POST /api/start?slot=N          (launch)
//   POST /api/stop?slot=N + POST /api/pause?slot=N            (controls)
// ═══════════════════════════════════════════════════════════════════════════
(function () {
  "use strict";

  // ── Constants ─────────────────────────────────────────────────────────
  const STATUS_POLL_MS = 5000;
  // Slots dédiés HIP-4 (jamais partagés avec le trading form) : 2 sessions
  // outcomes simultanées max, une par slot, jamais deux fois le même outcome.
  const HIP4_FIRST_SLOT = MM_SLOTS;
  const HIP4_SLOTS = [];
  for (let s = HIP4_FIRST_SLOT; s < MAX_SLOTS; s++) HIP4_SLOTS.push(s);
  const HIP4_INV_CAP_HARD = Infinity; // plus de cap dur - limite = balance USDH disponible

  // ── State ─────────────────────────────────────────────────────────────
  const state = {
    markets: [],                  // [{outcome, expiry, expiryStr, underlying, raw, yesCoin, noCoin, label, yesPx, noPx}]
    marketsTs: 0,
    statusTimer: null,
    usdcTimer: null,
    usdcInFlight: null,
    usdcBalance: null,
    usdcBalanceFetchedAt: 0,
    countdownTimer: null,
    selectedOutcome: null,        // outcome currently configured / running
    sessionActive: false,
    sessionPaused: false,
    lastFills: [],
    seenFillSig: new Set(),
    capManualOverride: false,     // true once user touches cap field manually
    stopLossManualOverride: false, // true once user touches SL field manually
    sessionStartedAt: 0,          // ms timestamp when launchSession resolved
    firstQuoteSeen: false,        // becomes true once backend reports quote_placed>0 OR quote_skipped>0
    flattening: false,            // true between stop click and confirmed position=0
    flatteningStartedAt: 0,       // ms timestamp when flatten started
    suppressAutoResume: false,    // true quand l'utilisateur revient volontairement au browse pendant un run (evite la reouverture auto forcee)
    runningBySlot: {},            // slot HIP-4 → outcome running (server truth)
    runningSig: "",               // signature JSON de runningBySlot (détection de changement)
    hip4SlotPayloads: {},         // slot HIP-4 → dernier payload /api/status
    sessionSlot: null,            // slot HIP-4 de la session affichée dans la vue session
    authPollFailed: false,        // dernier poll /api/status en 401 → bandeau re-sign
    marketsLoaded: false,         // becomes true once /api/markets resolves once
    outcomesMode: false,          // true tant que la route /outcomes est active
  };

  // Callback (outcomes.js) notifie quand le running outcome change.
  let _onRunningChange = null;

  // ── Coin helpers (HIP-4 encoding spec) ────────────────────────────────
  function parseHip4Coin(coin) {
    if (typeof coin !== "string" || !coin.startsWith("#")) return null;
    const raw = coin.slice(1);
    if (!/^\d+$/.test(raw)) return null;
    const enc = Number(raw);
    const outcome = Math.floor(enc / 10);
    const side = enc % 10;
    if (outcome < 1 || (side !== 0 && side !== 1)) return null;
    return { outcome, side, isYes: side === 0 };
  }
  function hip4SideName(coin) {
    const p = parseHip4Coin(coin);
    if (!p) return "-";
    return p.isYes ? "YES" : "NO";
  }
  function coinFor(outcome, side) {
    return "#" + (10 * outcome + side);
  }

  // ── Time helpers ──────────────────────────────────────────────────────
  // Parse "20260504-0600" (UTC) → epoch ms
  function parseExpiry(s) {
    if (!s || typeof s !== "string") return null;
    const m = s.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})$/);
    if (!m) return null;
    const [, y, mo, d, h, mi] = m;
    return Date.UTC(+y, +mo - 1, +d, +h, +mi, 0);
  }
  function formatExpiry(epochMs) {
    if (!epochMs) return "-";
    const d = new Date(epochMs);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
  }
  function formatCountdown(ms) {
    if (ms == null || ms <= 0) return "00:00:00";
    const totalSec = Math.floor(ms / 1000);
    const d = Math.floor(totalSec / 86400);
    const h = Math.floor((totalSec % 86400) / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    const pad = (n) => String(n).padStart(2, "0");
    if (d > 0) return `${d}d ${pad(h)}:${pad(m)}:${pad(s)}`;
    return `${pad(h)}:${pad(m)}:${pad(s)}`;
  }

  // ── Label parsing ─────────────────────────────────────────────────────
  // Backend label format: "BTC > $78,213 by 20260504-0600 Yes"
  // → underlying="BTC", question="BTC > $78,213", expiryStr="20260504-0600"
  function parseLabel(label) {
    if (!label || typeof label !== "string") return { question: "-", underlying: "?", expiryStr: null };
    // Strip trailing " Yes" / " No"
    const stripped = label.replace(/\s+(Yes|No)\s*$/i, "").trim();
    // Split on " by " - keep both halves so we can build a full readable name
    const idx = stripped.lastIndexOf(" by ");
    let predicate = stripped, expiryStr = null;
    if (idx > 0) {
      predicate = stripped.slice(0, idx).trim();
      expiryStr = stripped.slice(idx + 4).trim();
    }
    // Format expiry "20260505-0600" -> "5 May 2026 · 06:00 UTC"
    let expiryHuman = "";
    if (expiryStr && /^\d{8}-\d{4}$/.test(expiryStr)) {
      const y = expiryStr.slice(0, 4), mo = +expiryStr.slice(4, 6), d = +expiryStr.slice(6, 8);
      const hh = expiryStr.slice(9, 11), mm = expiryStr.slice(11, 13);
      const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
      expiryHuman = `${d} ${months[mo-1] || mo} ${y} · ${hh}:${mm} UTC`;
    }
    const question = expiryHuman ? `${predicate} by ${expiryHuman}` : predicate;
    // Underlying = first token before space/comparator
    const ulMatch = predicate.match(/^([A-Za-z0-9]+)/);
    const underlying = ulMatch ? ulMatch[1].toUpperCase() : "?";
    return { question, underlying, expiryStr };
  }

  function tokenIconUrl(underlying) {
    if (!underlying) return "";
    return `https://financialmodelingprep.com/image-stock/${underlying.toUpperCase()}.png`;
  }

  // ── DOM helpers ───────────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }
  function setText(id, txt) { const el = $(id); if (el) el.textContent = txt; }

  // ── Markets fetch + render ────────────────────────────────────────────
  async function fetchMarkets() {
    try {
      // Use hyperliquid_mainnet exchange (HIP-4 lives on HL)
      const data = await apiGet("/api/markets?mode=hip4&exchange=hyperliquid_mainnet");
      const groups = Array.isArray(data?.groups) ? data.groups : [];
      const labels = (data && typeof data.labels === "object") ? data.labels : {};
      const mids = (data && typeof data.mids === "object" && data.mids) ? data.mids : {};
      // Each group ≈ one outcome with [#yes, #no]
      const markets = [];
      for (const g of groups) {
        const syms = Array.isArray(g.symbols) ? g.symbols : [];
        let yesCoin = null, noCoin = null;
        for (const s of syms) {
          const p = parseHip4Coin(s);
          if (!p) continue;
          if (p.isYes) yesCoin = s; else noCoin = s;
        }
        if (!yesCoin || !noCoin) continue;
        const yp = parseHip4Coin(yesCoin);
        const yLabel = labels[yesCoin] || g.label || "";
        const meta = parseLabel(yLabel);
        const yesMid = (mids[yesCoin] != null) ? Number(mids[yesCoin]) : null;
        const noMid  = (mids[noCoin]  != null) ? Number(mids[noCoin])  : null;
        markets.push({
          outcome: yp.outcome,
          yesCoin,
          noCoin,
          label: g.label || meta.question,
          question: meta.question,
          underlying: meta.underlying,
          expiryStr: meta.expiryStr,
          expiry: parseExpiry(meta.expiryStr),
          yesPx: Number.isFinite(yesMid) ? yesMid : null,
          noPx: Number.isFinite(noMid) ? noMid : null,
        });
      }
      state.markets = markets;
      state.marketsTs = Date.now();
      state.marketsLoaded = true;
    } catch (err) {
      console.warn("[hip4] fetch markets failed:", err);
      // On 401, the apiGet helper already surfaced a wallet error.
    }
  }

  // La liste des marchés HIP-4 n'est plus rafraîchie en continu (la grille
  // browse a disparu) : on la (re)charge à la demande avant d'ouvrir le drawer
  // ou la vue session, pour que question/expiry/coins soient à jour.
  async function ensureMarkets() {
    if (Date.now() - state.marketsTs > 15000) await fetchMarkets();
  }

  function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }

  function updateCountdowns() {
    const now = Date.now();
    // Session countdown card
    const sCard = $("hip4_view_session");
    if (sCard && !sCard.classList.contains("hidden") && state.selectedOutcome != null) {
      const m = state.markets.find((x) => x.outcome === state.selectedOutcome);
      if (m && m.expiry) {
        const ms = m.expiry - now;
        setText("hip4_countdown_clock", formatCountdown(ms));
        setText("hip4_countdown_meta", formatExpiry(m.expiry));
        const card = document.querySelector(".hip4-countdown-card");
        if (card) card.classList.toggle("is-imminent", ms > 0 && ms < 15 * 60 * 1000);
      }
    }
  }

  async function fetchUsdcBalance() {
    if (state.usdcInFlight) return state.usdcInFlight;
    state.usdcInFlight = (async () => {
      const setAvail = (txt) => document.querySelectorAll(".hip4-field-avail .hip4-avail-value").forEach(el => el.textContent = txt);
      // Only blank to "…" on the very first fetch so refreshes don't flash
      // the cached number out of the drawer when the user reopens it.
      if (state.usdcBalance == null) setAvail("…");
      try {
        let data;
        try {
          // Le solde USDC est au niveau du wallet : n'importe quel slot HIP-4 convient.
          data = await apiGet(`/api/hip4/usdc_balance?slot=${HIP4_FIRST_SLOT}`, { dex: "hyperliquid" });
        } catch (fastErr) {
          // Backward-compatible fallback for older backends during deploy windows.
          if (!fastErr || fastErr.status !== 404) throw fastErr;
          data = await apiGet("/api/metrics?market_source=hip4&mode=hyperliquid_mainnet&slot=0", { dex: "hyperliquid" });
        }
        const bal = Number(data?.available ?? data?.availableNotional ?? data?.summary?.availableNotional);
        if (Number.isFinite(bal) && bal >= 0) {
          state.usdcBalance = bal;
          state.usdcBalanceFetchedAt = Date.now();
          setAvail(bal.toFixed(2) + " USDC");
        } else {
          state.usdcBalance = 0;
          setAvail("0.00 USDC");
        }
      } catch (err) {
        console.warn("[hip4] usdc balance fetch failed:", err);
        if (state.usdcBalance == null) setAvail("-");
      }
    })();
    try {
      return await state.usdcInFlight;
    } finally {
      state.usdcInFlight = null;
    }
  }

  // Bandeau « session expirée » sur /outcomes : si le poll status 401, le bot
  // peut tourner sans être ni visible ni stoppable. Échec silencieux interdit.
  function updateHip4AuthBanner() {
    let el = document.getElementById("hip4_auth_banner");
    const show = state.outcomesMode && state.authPollFailed;
    if (!show) {
      if (el) el.classList.add("hidden");
      return;
    }
    if (!el) {
      el = document.createElement("div");
      el.id = "hip4_auth_banner";
      el.className = "hip4-auth-banner";
      el.innerHTML =
        '<span class="hip4-auth-banner-text">Wallet session expired - re-sign to see and stop your running bot.</span>' +
        '<button type="button" class="hip4-auth-banner-btn">Re-sign</button>';
      el.querySelector(".hip4-auth-banner-btn")?.addEventListener("click", () => {
        // The /api/status poll got a 401: the server rejected our token, but the
        // client may still treat the cached token as valid and auto-skip the
        // wallet signature step (no Rabby prompt). Clear the stale token first so
        // the connect flow actually re-challenges the wallet.
        try {
          const acct = (document.getElementById("account_address")?.value || walletAuthAccount || "").trim();
          if (acct) { clearWalletAuthForAccount(acct); } else { clearWalletAuthSession(); }
        } catch (_) {}
        try { openConnectModal("hyperliquid"); } catch (_) {}
      });
      document.body.appendChild(el);
    }
    el.classList.remove("hidden");
  }

  async function pollStatus() {
    try {
      const data = await apiGet("/api/status", { dex: "hyperliquid" });
      if (state.authPollFailed) {
        state.authPollFailed = false;
        updateHip4AuthBanner();
      }
      const slots = Array.isArray(data?.slots) ? data.slots : [];
      // Detect running HIP-4 sessions and extract their outcomes (browse cards).
      // Le symbole "#NN" est non ambigu : seules les sessions HIP-4 l'utilisent.
      const runningBySlot = {};
      state.hip4SlotPayloads = {};
      for (const slotId of HIP4_SLOTS) {
        const sp = slots.find((s) => s.slot === slotId);
        if (!sp) continue;
        state.hip4SlotPayloads[slotId] = sp;
        if (!(sp.running || sp.process_running)) continue;
        const rawSym = String(sp.symbol || sp.session?.symbol || "");
        const parsed = parseHip4Coin(rawSym);
        if (parsed) runningBySlot[slotId] = parsed.outcome;
        else console.debug(`[hip4] pollStatus: running slot ${slotId} has unparseable symbol ${JSON.stringify(rawSym)} — RUNNING card may not show`);
      }
      state.runningBySlot = runningBySlot;
      const sig = JSON.stringify(runningBySlot);
      if (state.runningSig !== sig) {
        state.runningSig = sig;
        // Notifie outcomes.js pour que les cartes basculent start ↔ RUNNING.
        try { if (_onRunningChange) _onRunningChange(Object.values(runningBySlot)); } catch (_) {}
      }
    } catch (err) {
      console.warn("[hip4] status poll failed:", err);
      if (err && err.status === 401 && !state.authPollFailed) {
        state.authPollFailed = true;
        updateHip4AuthBanner();
      }
    }
  }

  // ── Polling lifecycle ─────────────────────────────────────────────────
  function startStatusPolling() {
    if (state.statusTimer) return;
    pollStatus();
    state.statusTimer = setInterval(pollStatus, STATUS_POLL_MS);
  }
  function stopStatusPolling() {
    if (state.statusTimer) { clearInterval(state.statusTimer); state.statusTimer = null; }
  }
  // USDH balance refreshes every 20s while on /predictions so the drawer always
  // reflects the current spendable balance after fills, manual deposits or
  // withdrawals - no need for the user to reopen the drawer.
  function startUsdcPolling() {
    if (state.usdcTimer) return;
    state.usdcTimer = setInterval(() => fetchUsdcBalance().catch(() => {}), 20000);
  }
  function stopUsdcPolling() {
    if (state.usdcTimer) { clearInterval(state.usdcTimer); state.usdcTimer = null; }
  }

  // ── Wire up ───────────────────────────────────────────────────────────
  function bindOnce() {
    // No MM-specific bindings remain.
  }

  // ── Cycle de vie route /outcomes (appelé par applyRoute) ─────────────
  function onOutcomesEnter() {
    state.outcomesMode = true;
    // Status poll pour flaguer l'outcome RUNNING sur les cartes.
    startStatusPolling();
    // Pre-warm USDH balance so the config drawer opens with the value already
    // populated instead of showing a long "…" while the metrics call resolves.
    fetchUsdcBalance().catch(() => {});
    startUsdcPolling();
    // Countdown 1 s de la vue session (l'élément est patché seulement si visible).
    if (!state.countdownTimer) state.countdownTimer = setInterval(updateCountdowns, 1000);
  }
  function onOutcomesLeave() {
    state.outcomesMode = false;
    updateHip4AuthBanner();
    stopStatusPolling();
    stopUsdcPolling();
    if (state.countdownTimer) { clearInterval(state.countdownTimer); state.countdownTimer = null; }
  }

  // Pont utilisé par outcomes.js : MM bot sur les cartes outcomes.
  window.HIP4MM = {
    onOutcomesEnter,
    onOutcomesLeave,
    // Liste des outcomes en cours de MM (un par slot HIP-4 running).
    runningOutcomes: () => Object.values(state.runningBySlot || {}),
    // Compat : premier outcome running (anciens appelants mono-session).
    runningOutcome: () => Object.values(state.runningBySlot || {})[0] ?? null,
    setOnRunningChange: (cb) => { _onRunningChange = (typeof cb === "function") ? cb : null; },
    openConfig(_outcome) {
      // MM config drawer removed.
    },
    viewSession(_outcome) {
      // MM session view removed.
    },
  };

  // Expose
  window.HIP4 = {
    parseHip4Coin,
    hip4SideName,
    formatExpiry,
    formatCountdown,
    refreshUsdcBalance: () => fetchUsdcBalance().catch(() => {}),
    // Used by auth-success hooks to immediately reconcile the running session
    // badge after a re-sign - otherwise the user waits up to STATUS_POLL_MS
    // staring at "START MM SESSION" while the bot is actually running.
    pollNow: () => pollStatus().catch(() => {}),
  };

  // Bind ASAP (DOM is parsed by the time app.js executes since it's at end of body? - be safe)
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindOnce, { once: true });
  } else {
    bindOnce();
  }
})();

// app.js is loaded synchronously in the <head>, well before the modal markup
// further down the body is parsed. init() wires up all the modal close buttons
// (telegram, referral, my_stats, feedback, pnl_share, ...) via getElementById,
// so it must wait for the DOM to be fully parsed, otherwise those elements are
// still null and the close (✕) buttons never get a click handler.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => { init(); }, { once: true });
} else {
  init();
}

// ── HIP-4 Volume Bot Tab ─────────────────────────────────────────────────────
// Slot 7 is the dedicated hip4vol slot (HIP4_SLOTS = [6, 7]; slot 7 reserved).
let HIP4VOL_SLOT = 7; // slot de l'onglet actif (7 a 9)
const HIP4VOL_TAB_SLOTS = [7, 8, 9, 10, 11]; // 5 onglets = MAX_SLOTS - 7
const HIP4VOL_OPEN_SLOTS_KEY = "hip4vol_open_slots";
const HIP4VOL_SLOT_NAMES_KEY = "hip4vol_slot_names";
let _hip4volOpenSlots = [7];
let _hip4volSlotMeta = {}; // slot -> {label, running, mode}
// Noms d'onglets choisis par l'utilisateur (double-clic sur l'onglet) : slot -> nom.
let _hip4volSlotNames = (() => {
  try {
    const raw = JSON.parse(localStorage.getItem(HIP4VOL_SLOT_NAMES_KEY) || "{}");
    return raw && typeof raw === "object" ? raw : {};
  } catch (_) { return {}; }
})();
function _hip4volSaveSlotNames() {
  try { localStorage.setItem(HIP4VOL_SLOT_NAMES_KEY, JSON.stringify(_hip4volSlotNames)); } catch (_) {}
}
// Array.prototype.sort() trie en lexicographique : [7,8,9,10,11] deviendrait
// [10,11,7,8,9] et les onglets Market 4/5 passeraient en tete.
function _hip4volSortSlots(arr) { return [...arr].sort((a, b) => a - b); }
// Onglet en cours de renommage : le rendu des onglets est gele tant que le
// champ inline est ouvert, sinon un poll ou un changement d'onglet l'efface.
let _hip4volRenaming = null;
let _hip4volLastTabClick = { slot: null, t: 0 };

function _hip4volLoadOpenSlots() {
  try {
    const raw = JSON.parse(localStorage.getItem(HIP4VOL_OPEN_SLOTS_KEY) || "[7]");
    if (Array.isArray(raw)) _hip4volOpenSlots = _hip4volSortSlots([...new Set([7, ...raw.map(Number).filter((s) => HIP4VOL_TAB_SLOTS.includes(s))])]);
  } catch (_) {}
}
function _hip4volSaveOpenSlots() {
  try { localStorage.setItem(HIP4VOL_OPEN_SLOTS_KEY, JSON.stringify(_hip4volOpenSlots)); } catch (_) {}
}
function _hip4volTabLabel(slot) {
  const custom = _hip4volSlotNames[String(slot)];
  if (custom) return custom;
  const m = _hip4volSlotMeta[slot] || {};
  if (m.label) return m.label;
  return `Market ${HIP4VOL_TAB_SLOTS.indexOf(slot) + 1}`;
}
// Double-clic sur un onglet : renommage inline. Entree valide, Echap annule,
// un champ vide efface le nom personnalise et revient au libelle automatique.
function _hip4volStartRename(slot) {
  const bar = document.getElementById("hip4vol_tabs");
  const btn = bar && bar.querySelector(`.hv-tab[data-slot="${slot}"]`);
  const labelEl = btn && btn.querySelector(".ta-label");
  if (!labelEl || _hip4volRenaming !== null) return;
  _hip4volRenaming = slot;
  const current = _hip4volTabLabel(slot);
  const inp = document.createElement("input");
  inp.type = "text";
  inp.className = "hv-tab-rename";
  inp.value = current;
  inp.maxLength = 24;
  labelEl.textContent = "";
  labelEl.appendChild(inp);
  inp.focus();
  inp.select();
  let done = false;
  const finish = (commit) => {
    if (done) return;
    done = true;
    _hip4volRenaming = null;
    if (commit) {
      const v = inp.value.trim();
      if (v) _hip4volSlotNames[String(slot)] = v;
      else delete _hip4volSlotNames[String(slot)];
      _hip4volSaveSlotNames();
    }
    _hip4volRenderTabs();
  };
  inp.addEventListener("click", (e) => e.stopPropagation());
  inp.addEventListener("dblclick", (e) => e.stopPropagation());
  inp.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  inp.addEventListener("blur", () => finish(true));
}
function _hip4volRenderTabs() {
  const bar = document.getElementById("hip4vol_tabs");
  if (!bar || _hip4volRenaming !== null) return;
  bar.innerHTML = "";
  for (const slot of _hip4volOpenSlots) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ta-option hv-tab" + (slot === HIP4VOL_SLOT ? " active" : "");
    const m = _hip4volSlotMeta[slot] || {};
    b.innerHTML = `<span class="hv-tab-dot${m.running ? " is-on" : ""}"></span><span class="ta-label">${escapeHtml(_hip4volTabLabel(slot))}</span>`;
    b.dataset.slot = String(slot);
    b.title = "Double-click to rename";
    // Double-clic detecte a la main : chaque clic simple redessine la barre,
    // donc l'evenement dblclick natif ne retomberait pas sur le meme bouton.
    b.addEventListener("click", () => {
      const now = Date.now();
      if (_hip4volLastTabClick.slot === slot && now - _hip4volLastTabClick.t < 400) {
        _hip4volLastTabClick = { slot: null, t: 0 };
        _hip4volStartRename(slot);
        return;
      }
      _hip4volLastTabClick = { slot, t: now };
      _hip4volSelectSlot(slot);
    });
    bar.appendChild(b);
  }
  const free = HIP4VOL_TAB_SLOTS.find((s) => !_hip4volOpenSlots.includes(s));
  if (free !== undefined) {
    const add = document.createElement("button");
    add.type = "button";
    add.className = "ta-option ta-empty hv-tab-add";
    add.textContent = "+ Market";
    add.addEventListener("click", () => { _hip4volOpenSlots = _hip4volSortSlots([..._hip4volOpenSlots, free]); _hip4volSaveOpenSlots(); _hip4volSelectSlot(free); });
    bar.appendChild(add);
  }
}
// Parse minimal du TOML des configs HIP-4 (sections [hip4vol]/[hip4mm], scalaires et listes simples).
function _hip4volParseToml(text) {
  const out = { mode: "", hip4vol: {}, hip4mm: {} };
  let section = "";
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const sec = line.match(/^\[(\w+)\]$/);
    if (sec) { section = sec[1]; continue; }
    const kv = line.match(/^([A-Za-z_][\w]*)\s*=\s*(.+)$/);
    if (!kv) continue;
    let v = kv[2].trim();
    if (v.startsWith("[")) v = v.replace(/^\[|\]$/g, "").split(",").map((x) => x.trim().replace(/^"|"$/g, "")).filter(Boolean).map((x) => (isNaN(Number(x)) ? x : Number(x)));
    else if (v.startsWith('"')) v = v.replace(/^"|"$/g, "");
    else if (v === "true" || v === "false") v = v === "true";
    else if (!isNaN(Number(v))) v = Number(v);
    if (!section) out[kv[1]] = v; else if (out[section]) out[section][kv[1]] = v;
  }
  return out;
}
async function _hip4volApplySlotConfig(slot) {
  let toml = "";
  try { toml = (await apiGet(`/api/config?slot=${slot}`)).toml || ""; } catch (_) { toml = ""; }
  // Garde de source : si l'utilisateur a change d'onglet pendant la requete,
  // cette reponse ne doit pas ecraser le formulaire de l'onglet actif.
  if (slot !== HIP4VOL_SLOT) return;
  const cfg = _hip4volParseToml(toml);
  const maker = cfg.mode === "hip4mm";
  const sec = maker ? cfg.hip4mm : cfg.hip4vol;
  // Nouvel onglet ou onglet sans config maker : aucun marche preselectionne.
  // Une selection faite sur cet onglet mais pas encore sauvegardee est
  // conservee le temps de la page (memo par slot).
  if (!maker) {
    const remembered = (_hip4volSlotMeta[slot] && _hip4volSlotMeta[slot].selected) || [];
    _hip4volSelectedOutcomes = remembered.slice(0, 1);
    _hip4volUpdateOutcomeCount(); _hip4volRenderOutcomeList(); _hip4volSyncSearchField();
  }
  _hip4volSlotMeta[slot] = { ..._hip4volSlotMeta[slot], mode: cfg.mode || "" };
  if (cfg.mode === "hip4mm" || cfg.mode === "hip4vol") _hip4volSetMode(cfg.mode);
  if (cfg.mode === "hip4vol") _hip4volSlotMeta[slot] = { ..._hip4volSlotMeta[slot], label: "Taker" };
  const size = Number(maker ? sec.inv_cap_usd : sec.max_size_usd);
  if (Number.isFinite(size) && size > 0) {
    const inp = document.getElementById("hip4vol_max_size"); const sl = document.getElementById("hip4vol_size_slider");
    if (inp) inp.value = String(Math.round(size)); if (sl) sl.value = String(Math.round(size));
  }
  const vol = Number(sec.max_volume || 0);
  const vi = document.getElementById("hip4vol_max_volume"); const vs = document.getElementById("hip4vol_volume_slider");
  if (vi) vi.value = String(Math.round(vol)); if (vs) vs.value = String(Math.round(vol));
  if (!maker) {
    _hip4volSetTakerVenues(Array.isArray(sec.only_venues) ? sec.only_venues : []);
    if (Number.isFinite(Number(sec.max_spread_pct))) _hip4volSetSpreadMode(Number(sec.max_spread_pct) > 0.0003 ? "aggressive" : "conservative");
  } else {
    const ids = Array.isArray(sec.outcome_ids) ? sec.outcome_ids.map(Number).filter((n) => n > 0) : [];
    _hip4volSelectedOutcomes = ids.slice(0, 1);
    _hip4volSlotMeta[slot] = { ..._hip4volSlotMeta[slot], selected: ids.slice(0, 1) };
    if (ids.length) {
      const o = _hip4volOutcomes.find((x) => x.outcome_id === ids[0]);
      _hip4volSlotMeta[slot] = { ..._hip4volSlotMeta[slot], label: o ? o.label : `Outcome #${ids[0]}` };
    }
    if (Number.isFinite(Number(sec.spread_pct)) && Number(sec.spread_pct) > 0) {
      const el = document.getElementById("hip4mm_spread");
      if (el) {
        el.value = String(Number(sec.spread_pct) * 100);
        // Une écriture programmatique n'émet pas d'`input` : sans ça le curseur
        // et la pastille « défaut » restaient sur l'ancienne valeur, et affichaient
        // 0,04% pendant que le champ lisait 1,5%.
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
    _hip4volUpdateOutcomeCount(); _hip4volRenderOutcomeList(); _hip4volSyncSearchField();
  }
}
function _hip4volAnimateSlotSwitch(direction) {
  // Glissement des cartes de configuration (pas la barre d'onglets) pour
  // montrer que l'on change de config. direction : 1 = onglet suivant, -1 = precedent.
  const col = document.querySelector("#hip4vol_page .hv-col-config");
  if (!col) return;
  const cards = [...col.querySelectorAll(":scope > .hv-card:not(.hv-tabs-card), :scope > .trading-account-wrap")]
    .filter((el) => !el.classList.contains("hidden"));
  cards.forEach((el, i) => {
    el.classList.remove("hv-slot-in-next", "hv-slot-in-prev");
    void el.offsetWidth; // relance l'animation si la classe etait deja posee
    el.style.animationDelay = `${i * 35}ms`;
    el.classList.add(direction < 0 ? "hv-slot-in-prev" : "hv-slot-in-next");
    el.addEventListener("animationend", () => {
      el.classList.remove("hv-slot-in-next", "hv-slot-in-prev");
      el.style.animationDelay = "";
    }, { once: true });
  });
}

async function _hip4volSelectSlot(slot) {
  const prev = HIP4VOL_SLOT;
  HIP4VOL_SLOT = Number(slot);
  _hip4volRenderTabs();
  if (prev !== HIP4VOL_SLOT) {
    _hip4volAnimateSlotSwitch(HIP4VOL_SLOT > prev ? 1 : -1);
    // L'etat running affiche est celui de l'onglet actif uniquement : on part
    // d'idle et le poll immediat ci-dessous remet le vrai etat du slot.
    _hip4volWasRunning = false;
    if (_hip4volRunning) _hip4volSetRunningState(false);
  }
  await _hip4volApplySlotConfig(HIP4VOL_SLOT);
  try { _hip4volPollStatus(); } catch (_) {}
  try { _hip4volFetchAvail(); } catch (_) {}
  _hip4volRenderTabs();
}
function _hip4volSyncTabsFromStatus(slots) {
  let changed = false;
  for (const sp of slots || []) {
    const s = Number(sp.slot);
    if (!HIP4VOL_TAB_SLOTS.includes(s)) continue;
    const running = !!(sp.running || sp.process_running);
    if (running && !_hip4volOpenSlots.includes(s)) { _hip4volOpenSlots = _hip4volSortSlots([..._hip4volOpenSlots, s]); _hip4volSaveOpenSlots(); changed = true; }
    const prev = _hip4volSlotMeta[s] || {};
    const mode = sp.mode || prev.mode || "";
    let label = prev.label;
    if (mode === "hip4vol") label = "Taker";
    const oid = Number(sp.outcome_id || (sp.runtime_risk && sp.runtime_risk.hip4mm_outcomes && Object.keys(sp.runtime_risk.hip4mm_outcomes)[0]));
    if (mode === "hip4mm" && oid) {
      const o = _hip4volOutcomes.find((x) => x.outcome_id === oid);
      label = o ? o.label : `Outcome #${oid}`;
    }
    if (prev.running !== running || prev.label !== label || prev.mode !== mode) changed = true;
    _hip4volSlotMeta[s] = { ...prev, label, running, mode };
  }
  if (changed) _hip4volRenderTabs();
}
const HIP4VOL_POLL_MS = 5000;

let _hip4volMode = "hip4vol"; // "hip4vol" (taker) | "hip4mm" (maker)
let _hip4volOutcomes = [];    // [{outcome_id, label, volume}] — tous les outcomes live, triés par volume desc
let _hip4volSelectedOutcomes = []; // outcome_id[] : un marche par session (onglet)
function _hip4volIsSelected(id) { return _hip4volSelectedOutcomes.includes(id); }
function _hip4volUpdateOutcomeCount() {
  const hint = document.getElementById("hip4vol_outcome_selected");
  if (hint) hint.textContent = "";
}
// Le champ de recherche sert aussi d'affichage du marché choisi : tant que
// l'utilisateur ne tape pas, sa valeur est le label sélectionné (et ne filtre
// donc pas la liste). Dès qu'il tape, il redevient un filtre.
let _hip4volSearchDirty = false;

const _HIP4VOL_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function _hip4volHumanizeLabel(s) {
  // "… by 20260613-0600" -> "… by 13 Jun 2026 · 06:00 UTC" (même style que parseLabel / outcomes.js)
  // Les tirets cadratins du display_label serveur ("parent — nom") sont retirés.
  return String(s || "")
    .replace(/\s*—\s*/g, " ")
    .replace(/\b(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\b/g, (raw, y, mo, d, hh, mm) => {
      const m = _HIP4VOL_MONTHS[Number(mo) - 1];
      return m ? `${Number(d)} ${m} ${y} · ${hh}:${mm} UTC` : raw;
    });
}

function _hip4volFmtVol(v) {
  const n = Number(v) || 0;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}K`;
  if (n > 0) return `$${n.toFixed(0)}`;
  return "";
}

function _hip4volSelectedLabel() {
  return _hip4volOutcomes.filter((o) => _hip4volIsSelected(o.outcome_id)).map((o) => o.label).join(" · ");
}

// Remet dans le champ le nom du marché choisi (+ coche via .is-selected).
function _hip4volSyncSearchField() {
  const el = document.getElementById("hip4vol_outcome_search");
  if (!el) return;
  const label = _hip4volSelectedLabel();
  _hip4volSearchDirty = false;
  el.value = label;
  el.classList.toggle("is-selected", !!label);
}

function _hip4volSetListOpen(open) {
  document.getElementById("hip4vol_outcome_list")?.classList.toggle("is-open", !!open);
  // À la fermeture, une recherche non validée est abandonnée : le champ
  // réaffiche le marché sélectionné.
  if (!open) _hip4volSyncSearchField();
}

function _hip4volRenderOutcomeList() {
  const list = document.getElementById("hip4vol_outcome_list");
  if (!list) return;
  const q = _hip4volSearchDirty
    ? (document.getElementById("hip4vol_outcome_search")?.value || "").trim().toLowerCase()
    : "";
  const items = _hip4volOutcomes.filter((o) =>
    _hip4volVenueMatches(o, _hip4volVenueFilter) && (!q || o.label.toLowerCase().includes(q)));
  list.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "hv-outcome-empty";
    empty.textContent = !_hip4volOutcomes.length ? "No live markets"
      : (q ? "No match" : "No live market from this deployer");
    list.appendChild(empty);
    return;
  }
  for (const o of items) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "hv-outcome-item" + (_hip4volIsSelected(o.outcome_id) ? " is-active" : "");
    const name = document.createElement("span");
    name.className = "hv-outcome-name";
    name.textContent = o.label;
    row.appendChild(name);
    // Provenance du marché (outcomeMeta.venue) : nom du déployeur permissionless.
    const venueInfo = _hip4VenueBadge(o.venue);
    if (venueInfo) {
      const badge = document.createElement("span");
      badge.className = "hv-outcome-venue hv-outcome-venue--" + venueInfo.key;
      badge.textContent = venueInfo.text;
      badge.title = venueInfo.title;
      row.appendChild(badge);
    }
    const vol = document.createElement("span");
    vol.className = "hv-outcome-vol";
    vol.textContent = _hip4volFmtVol(o.volume);
    row.appendChild(vol);
    row.addEventListener("click", () => {
      // Un marche par session (onglet) : le clic remplace la selection.
      _hip4volSelectedOutcomes = [o.outcome_id];
      _hip4volSlotMeta[HIP4VOL_SLOT] = { ..._hip4volSlotMeta[HIP4VOL_SLOT], label: o.label, selected: [o.outcome_id] };
      _hip4volUpdateOutcomeCount();
      _hip4volRenderOutcomeList();
      _hip4volSetListOpen(false);
      try { _hip4volRenderTabs(); } catch (_) {}
    });
    list.appendChild(row);
  }
}

// Badge de provenance d'un marché HIP-4 (outcomeMeta.venue = déployeur
// permissionless). Vide pour les marchés natifs Hyperliquid (venue "").
const HIP4_VENUE_BADGES = {
  out:  { key: "out",  text: "out",  title: "Deployed by outcome.xyz" },
  txyz: { key: "txyz", text: "TXYZ", title: "Deployed by trade.xyz" },
  skew: { key: "skew", text: "skew", title: "Deployed by Skew" },
};
function _hip4VenueBadge(venue) {
  return HIP4_VENUE_BADGES[String(venue || "").toLowerCase()] || null;
}
// Filtre du picker par deployeur ("hl" = marches natifs Hyperliquid, venue vide).
const HIP4VOL_VENUE_FILTERS = [
  { key: "all",  label: "All" },
  { key: "hl",   label: "Hyperliquid" },
  { key: "out",  label: "out" },
  { key: "txyz", label: "TXYZ" },
  { key: "skew", label: "skew" },
];
const HIP4VOL_VENUE_FILTER_KEY = "hip4vol_venue_filter";
let _hip4volVenueFilter = (() => { try { return localStorage.getItem(HIP4VOL_VENUE_FILTER_KEY) || "all"; } catch (_) { return "all"; } })();
function _hip4volVenueMatches(o, key) {
  if (!key || key === "all") return true;
  const v = String(o.venue || "").toLowerCase();
  return key === "hl" ? v === "" : v === key;
}
function _hip4volSetVenueFilter(key) {
  _hip4volVenueFilter = HIP4VOL_VENUE_FILTERS.some((f) => f.key === key) ? key : "all";
  try { localStorage.setItem(HIP4VOL_VENUE_FILTER_KEY, _hip4volVenueFilter); } catch (_) {}
  // Ne PAS reconstruire les chips ici : le bouton clique serait retire du DOM
  // avant le gestionnaire global "clic en dehors du picker", qui verrait alors
  // un noeud detache et refermerait la liste. On ne bascule que les classes.
  document.querySelectorAll("#hip4vol_venue_chips .hv-venue-chip").forEach((b) => {
    b.classList.toggle("is-active", b.dataset.venue === _hip4volVenueFilter);
  });
  _hip4volRenderOutcomeList();
  _hip4volSetListOpen(true);
}
function _hip4volRenderVenueChips() {
  const bar = document.getElementById("hip4vol_venue_chips");
  if (!bar) return;
  if (bar.childElementCount) {
    // Deja construites : juste l'etat actif (voir _hip4volSetVenueFilter).
    bar.querySelectorAll(".hv-venue-chip").forEach((b) => b.classList.toggle("is-active", b.dataset.venue === _hip4volVenueFilter));
    return;
  }
  for (const f of HIP4VOL_VENUE_FILTERS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "hv-venue-chip hv-venue-chip--" + f.key + (f.key === _hip4volVenueFilter ? " is-active" : "");
    b.textContent = f.label;
    b.dataset.venue = f.key;
    b.addEventListener("click", () => _hip4volSetVenueFilter(f.key));
    bar.appendChild(b);
  }
}

async function _hip4volLoadOutcomes() {
  try {
    const raw = await apiGet("/api/markets?mode=hip4&exchange=hyperliquid_mainnet");
    const groups = Array.isArray(raw?.groups) ? raw.groups : [];
    const seen = new Set();
    const out = [];
    for (const g of groups) {
      // outcome_id fourni par le serveur ; fallback : coins "#<outcome*10+side>"
      // -> outcome id = floor(n/10) (même dérivation que parseMarkets).
      let oid = Number(g.outcome_id);
      if (!Number.isFinite(oid) || oid <= 0) {
        const sym = String((g.symbols || [])[0] || "");
        const n = parseInt(sym.replace(/^#/, ""), 10);
        if (!Number.isFinite(n)) continue;
        oid = Math.floor(n / 10);
      }
      if (seen.has(oid)) continue;
      seen.add(oid);
      // Tri/affichage : volume Hyperliquid 24h (candleSnapshot on-chain, tous
      // traders) — PAS le volume Tradoor. Fallback volume plateforme tant que
      // le premier refresh de fond n'a pas rempli hl_volume.
      const hlVol = Number(g.hl_volume) || 0;
      out.push({
        outcome_id: oid,
        label: _hip4volHumanizeLabel(g.label || g.parent_title || `Outcome #${oid}`),
        volume: hlVol > 0 ? hlVol : (Number(g.volume) || 0),
        venue: String(g.venue || ""),
      });
    }
    out.sort((a, b) => b.volume - a.volume);
    _hip4volOutcomes = out;
    // Purge des marches selectionnes qui ne sont plus live.
    _hip4volSelectedOutcomes = _hip4volSelectedOutcomes.filter((id) => out.some((o) => o.outcome_id === id));
    _hip4volUpdateOutcomeCount();
    try { _hip4volRenderTabs(); } catch (_) {}
    if (!_hip4volSearchDirty) _hip4volSyncSearchField();
    _hip4volRenderVenueChips();
    _hip4volRenderOutcomeList();
  } catch (e) {
    const list = document.getElementById("hip4vol_outcome_list");
    if (list) list.innerHTML = '<div class="hv-outcome-empty">Failed to load — switch modes to retry</div>';
  }
}

const HIP4VOL_MODE_STORAGE_KEY = "hip4vol_mode";

function _hip4volSetMode(mode) {
  _hip4volMode = mode === "hip4mm" ? "hip4mm" : "hip4vol";
  try { localStorage.setItem(HIP4VOL_MODE_STORAGE_KEY, _hip4volMode); } catch (_) {}
  const maker = _hip4volMode === "hip4mm";
  document.getElementById("hip4vol_mode_taker")?.classList.toggle("is-active", !maker);
  document.getElementById("hip4vol_mode_maker")?.classList.toggle("is-active", maker);
  document.getElementById("hip4vol_outcome_card")?.classList.toggle("hidden", !maker);
  document.getElementById("hip4vol_only_out_card")?.classList.toggle("hidden", maker);
  // Volume target card stays visible in maker mode: max_volume is enforced
  // end-to-end for hip4mm too (TOML -> HIP4MMCfg -> status-loop cap check).
  document.getElementById("hip4vol_spread_field")?.classList.toggle("hidden", maker);
  document.getElementById("hip4mm_spread_card")?.classList.toggle("hidden", !maker);
  const sizeLabel = document.getElementById("hip4vol_size_label");
  if (sizeLabel) {
    sizeLabel.innerHTML = maker ? "Inventory cap <em>max exposure</em>" : "Max size <em>/ order</em>";
  }
  const sizeInput = document.getElementById("hip4vol_max_size");
  if (sizeInput) sizeInput.min = maker ? 50 : 20;
  if (maker && !_hip4volOutcomes.length) _hip4volLoadOutcomes();
}

// ── Trading style (spread limit) : conservative (0.02%) / aggressive (0.05%).
// Le client n'envoie que le MODE ; le % est mappé et clampé côté serveur.
const HIP4VOL_SPREAD_MODE_STORAGE_KEY = "hip4vol_spread_mode";
// Deployeurs cibles par le taker. Liste vide = tous les marches (defaut, y
// compris les marches natifs Hyperliquid). Sinon uniquement les venues cochees
// parmi HIP4_TAKER_VENUES (allowlist serveur identique).
const HIP4_TAKER_VENUES = ["out", "txyz", "skew"];
const HIP4VOL_TAKER_VENUES_KEY = "hip4vol_taker_venues";
const HIP4VOL_ONLY_OUT_STORAGE_KEY = "hip4vol_only_out"; // ancien booleen, migre au premier chargement
let _hip4volTakerVenues = [];
function _hip4volTakerVenuesList() { return _hip4volTakerVenues.slice(); }
function _hip4volOnlyOut() { return _hip4volTakerVenues.length === 1 && _hip4volTakerVenues[0] === "out"; }
function _hip4volSetTakerVenues(list) {
  const clean = [];
  for (const v of (Array.isArray(list) ? list : [])) {
    const k = String(v || "").trim().toLowerCase();
    if (HIP4_TAKER_VENUES.includes(k) && !clean.includes(k)) clean.push(k);
  }
  _hip4volTakerVenues = clean;
  try { localStorage.setItem(HIP4VOL_TAKER_VENUES_KEY, JSON.stringify(clean)); } catch (_) {}
  const all = clean.length === 0;
  document.getElementById("hip4vol_universe_all")?.classList.toggle("is-active", all);
  document.querySelectorAll("#hip4vol_taker_venue_chips .hv-venue-chip[data-venue]").forEach((b) => {
    b.classList.toggle("is-active", clean.includes(b.dataset.venue));
  });
}
function _hip4volToggleTakerVenue(v) {
  const cur = _hip4volTakerVenuesList();
  const i = cur.indexOf(v);
  if (i >= 0) cur.splice(i, 1); else cur.push(v);
  _hip4volSetTakerVenues(cur);
}
// Compatibilite : _hip4volSetUniverse(onlyOut) etait l'ancien reglage binaire.
function _hip4volSetUniverse(onlyOut) { _hip4volSetTakerVenues(onlyOut ? ["out"] : []); }
function _hip4volInitOnlyOut() {
  document.getElementById("hip4vol_universe_all")?.addEventListener("click", () => _hip4volSetTakerVenues([]));
  document.querySelectorAll("#hip4vol_taker_venue_chips .hv-venue-chip[data-venue]").forEach((b) => {
    b.addEventListener("click", () => _hip4volToggleTakerVenue(b.dataset.venue));
  });
  let saved = [];
  try {
    const raw = localStorage.getItem(HIP4VOL_TAKER_VENUES_KEY);
    if (raw != null) saved = JSON.parse(raw);
    else if (localStorage.getItem(HIP4VOL_ONLY_OUT_STORAGE_KEY) === "1") saved = ["out"];
  } catch (_) { saved = []; }
  _hip4volSetTakerVenues(saved);
}
let _hip4volSpreadMode = (() => {
  try {
    const v = localStorage.getItem(HIP4VOL_SPREAD_MODE_STORAGE_KEY);
    return v === "aggressive" ? "aggressive" : "conservative";
  } catch (_) { return "conservative"; }
})();

function _hip4volSetSpreadMode(mode) {
  _hip4volSpreadMode = mode === "aggressive" ? "aggressive" : "conservative";
  try { localStorage.setItem(HIP4VOL_SPREAD_MODE_STORAGE_KEY, _hip4volSpreadMode); } catch (_) {}
  const agg = _hip4volSpreadMode === "aggressive";
  document.getElementById("hip4vol_spread_conservative")?.classList.toggle("is-active", !agg);
  document.getElementById("hip4vol_spread_aggressive")?.classList.toggle("is-active", agg);
}

let _hip4volPollTimer = null;
let _hip4volTierTimer = null;
let _hip4volRunning = false;
let _hip4volTier = null; // last fetched tier object

// ── Session history (page /hip4vol) — helpers purs ──────────────────────────
function _hip4volIsHip4Mode(mode) {
  const m = String(mode || "").toLowerCase();
  return m.startsWith("hip4vol") || m.startsWith("hip4mm");
}

function _hip4volSessionCost10k(volume, pnl, fees) {
  const v = Number(volume) || 0;
  if (v <= 0) return null;
  return (((Number(fees) || 0) - (Number(pnl) || 0)) / v) * 10000;
}

function _hip4volFmtSessionDate(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "-";
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

function _hip4volFmtDuration(minutes) {
  if (minutes == null) return "-";
  const m = Number(minutes);
  if (!Number.isFinite(m) || m < 0) return "-";
  const h = Math.floor(m / 60);
  const mm = Math.round(m % 60);
  return h > 0 ? `${h}h ${mm}m` : `${mm}m`;
}

function _hip4volPnlFromRecord(s) {
  const v = s.pnl_total_usd_net ?? s.display_pnl_usd ?? s.pnl_total_usd;
  return Number(v) || 0;
}

function _hip4volVolFromRecord(s) {
  const primary = Number(s.session_volume_usd);
  if (Number.isFinite(primary) && primary > 0) return primary;
  return Number(s.engine_volume_usd) || 0;
}

function _hip4volFeesFromRecord(s) {
  const v = s.project_fees_usd ?? s.platform_fees_usd ?? 0;
  return Number(v) || 0;
}

function _hip4volSessionTs(s) {
  const t = Date.parse(s.ended_at || s.started_at || "");
  return Number.isFinite(t) ? t : 0;
}

// ── Session history — état + rendu ──────────────────────────────────────────
let _hip4volHistory = [];       // sessions HIP-4 filtrées, triées (récent -> ancien)
let _hip4volHistoryShown = 20;  // nb de lignes affichées
let _hip4volWasRunning = false; // pour détecter la transition running -> stopped
const _HIP4VOL_HISTORY_PAGE = 20;

function _hip4volRenderHistory() {
  const body = document.getElementById("hip4vol_history_body");
  const summary = document.getElementById("hip4vol_history_summary");
  const moreBtn = document.getElementById("hip4vol_history_more");
  if (!body) return;

  const all = _hip4volHistory;
  // Résumé sur TOUTES les sessions filtrées.
  if (summary) {
    if (!all.length) {
      summary.textContent = "";
    } else {
      let totVol = 0, totPnl = 0;
      all.forEach((s) => { totVol += _hip4volVolFromRecord(s); totPnl += _hip4volPnlFromRecord(s); });
      const pnlStr = `${totPnl >= 0 ? "+" : ""}$${totPnl.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
      summary.textContent = `${all.length} sessions · $${formatCompactVolume(totVol)} volume · PnL ${pnlStr}`;
    }
  }

  if (!all.length) {
    body.innerHTML = '<div class="hv-history-empty">No sessions yet.</div>';
    if (moreBtn) moreBtn.style.display = "none";
    return;
  }

  body.innerHTML = "";
  const shown = all.slice(0, _hip4volHistoryShown);
  shown.forEach((s) => {
    const vol = _hip4volVolFromRecord(s);
    const pnl = _hip4volPnlFromRecord(s);
    const cost = _hip4volSessionCost10k(vol, pnl, _hip4volFeesFromRecord(s));
    const row = document.createElement("div");
    row.className = "hv-history-row";
    const cells = [
      { text: _hip4volFmtSessionDate(s.ended_at || s.started_at) },
      { text: _hip4volFmtDuration(s.duration_min) },
      { text: `$${formatCompactVolume(vol)}`, cls: "hv-h-vol" },
      { text: `${pnl >= 0 ? "+" : ""}$${pnl.toLocaleString("en-US", { maximumFractionDigits: 2 })}`,
        cls: pnl > 0 ? "hv-h-pos" : (pnl < 0 ? "hv-h-neg" : "") },
      { text: cost == null ? "-" : `$${cost.toFixed(2)}` },
    ];
    cells.forEach((c) => {
      const div = document.createElement("div");
      if (c.cls) div.className = c.cls;
      div.textContent = c.text;
      row.appendChild(div);
    });
    body.appendChild(row);
  });

  if (moreBtn) {
    moreBtn.style.display = all.length > _hip4volHistoryShown ? "" : "none";
    moreBtn.onclick = () => { _hip4volHistoryShown += _HIP4VOL_HISTORY_PAGE; _hip4volRenderHistory(); };
  }
}

async function _hip4volLoadHistory() {
  const body = document.getElementById("hip4vol_history_body");
  if (!body) return;
  try {
    const data = await apiGet("/api/pnl_history");
    const all = Array.isArray(data && data.sessions) ? data.sessions : [];
    _hip4volHistory = all
      .filter((s) => s && _hip4volIsHip4Mode(s.mode))
      .sort((a, b) => _hip4volSessionTs(b) - _hip4volSessionTs(a));
    _hip4volHistoryShown = _HIP4VOL_HISTORY_PAGE;
    _hip4volRenderHistory();
  } catch (e) {
    body.innerHTML = "";
    const err = document.createElement("div");
    err.className = "hv-history-empty";
    err.style.color = "#f87171";
    err.textContent = (e && e.message) || "error";
    body.appendChild(err);
  }
}

let _hip4volAvailUsd = null; // last fetched available collateral (USDC), null = unknown

// Plafonne le slider/input de taille au min(available collateral, tier size_cap).
// Appelé par _hip4volApplyTier (tier connu) ET _hip4volFetchAvail (balance màj).
function _hip4volApplySizeCap() {
  const sizeInput = document.getElementById("hip4vol_max_size");
  const sizeSlider = document.getElementById("hip4vol_size_slider");
  const sizeHint = document.getElementById("hip4vol_size_cap_hint");
  if (!sizeInput) return;
  const tierCap = (_hip4volTier && _hip4volTier.size_cap != null) ? Number(_hip4volTier.size_cap) : Infinity;
  const avail = (Number.isFinite(_hip4volAvailUsd) && _hip4volAvailUsd > 0) ? Math.floor(_hip4volAvailUsd) : Infinity;
  let cap = Math.min(tierCap, avail);
  if (!Number.isFinite(cap)) {
    // ni cap tier ni balance connue → pas de plafond dur
    sizeInput.removeAttribute("max");
    if (sizeSlider) sizeSlider.max = String(5000);
    if (sizeHint) { sizeHint.textContent = "No cap"; sizeHint.classList.remove("hip4vol-capped"); }
    return;
  }
  cap = Math.max(1, Math.floor(cap));
  sizeInput.max = String(cap);
  if (Number(sizeInput.value) > cap) sizeInput.value = String(cap);
  if (sizeSlider) {
    // garde le slider valide si le cap descend sous son min par défaut
    if (cap < Number(sizeSlider.min)) sizeSlider.min = String(Math.max(1, cap));
    sizeSlider.max = String(cap);
    let sv = Number(sizeInput.value);
    if (Number.isFinite(sv)) { if (sv > cap) sv = cap; sizeSlider.value = String(sv); }
    _hip4volUpdateFill(sizeSlider);
  }
  if (sizeHint) {
    const limitedByBalance = (avail < tierCap);
    sizeHint.textContent = limitedByBalance ? `Max: $${cap} (balance)` : `Cap: $${cap}`;
    sizeHint.classList.add("hip4vol-capped");
  }
}

// ─ Helpers ──────────────────────────────────────────────────────────────────
function _hip4volFmt(v, prefix = "", suffix = "") {
  if (v == null || v === "" || v === undefined) return "-";
  return prefix + v + suffix;
}

function _hip4volFmtUsd(v) {
  if (v == null || v === undefined) return "-";
  const n = Number(v);
  if (isNaN(n)) return "-";
  return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function _hip4volSetMsg(elId, text, isError = false) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = text;
  el.style.color = isError ? "#f87171" : "rgb(255,140,60)";
}

function _hip4volClearMsg(elId) {
  const el = document.getElementById(elId);
  if (el) { el.textContent = ""; }
}

// ─ Tier bounding ────────────────────────────────────────────────────────────
function _hip4volApplyTier(tier) {
  _hip4volTier = tier;
  const sizeInput = document.getElementById("hip4vol_max_size");
  const tierBadge = document.getElementById("hip4vol_tier_badge");
  const costValue = document.getElementById("hip4vol_cost_value");

  if (tierBadge) tierBadge.textContent = tier.tier ? `Tier ${tier.tier}` : "-";
  if (costValue) costValue.textContent = "-";

  if (sizeInput) {
    sizeInput.disabled = false;
    // Plafond = min(available collateral, tier size_cap) — voir _hip4volApplySizeCap.
    _hip4volApplySizeCap();
  }

  // Trading style (Conservative/Aggressive) : visible pour tous les tiers.
  // Le spread effectif est mappé côté serveur (spread_mode -> %), jamais
  // envoyé en valeur libre depuis le client.

  _hip4volRefreshApproveState();
  // Plus de gate invite-only : le bot HIP-4 est ouvert à tous (le serveur a
  // HIP4VOL_REQUIRE_CODE=False ; les codes spéciaux ne servent plus qu'aux
  // fees réduits). La vue locked reste dans le DOM mais n'est jamais montrée.
  _hip4volShowMain(true);
}

// Reflect the on-chain builder-fee approval so the button persists across refresh.
function _hip4volMarkApproved(btn, label, acct) {
  btn.classList.add("is-approved");
  btn.disabled = true;
  btn._hvApprovedAcct = acct || "";
  btn.innerHTML = `<span class="hv-check"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12.5l5 5L20 6.5"/></svg></span><span class="hv-check-label">${label}</span>`;
}
function _hip4volResetApprove(btn) {
  btn.classList.remove("is-approved");
  btn.disabled = false;
  btn._hvApprovedAcct = "";
  btn.textContent = "Approve builder fees";
}
async function _hip4volRefreshApproveState() {
  const btn = document.getElementById("hip4vol_approve_btn");
  if (!btn) return;
  // Privileged / fee-exempt wallets never need an on-chain approval.
  if (_hip4volTier && Number(_hip4volTier.fee_per_10k) === 0) {
    _hip4volMarkApproved(btn, "Fee-exempt", "");
    return;
  }
  const acct = (document.getElementById("account_address")?.value || "").trim();
  if (!acct) return; // wallet not connected yet
  // Already confirmed for this account — no need to re-query every tick.
  if (btn.classList.contains("is-approved") && btn._hvApprovedAcct === acct) return;
  try {
    const st = await apiGet(`/api/builder/status?mode=hip4vol&account_address=${encodeURIComponent(acct)}`);
    if (st && st.approved) _hip4volMarkApproved(btn, "Approved", acct);
    else _hip4volResetApprove(btn);
  } catch (e) { /* leave button as-is */ }
}

// ─ Tier fetch ────────────────────────────────────────────────────────────────
function _hip4volShowMain(showMain) {
  const mainView = document.getElementById("hip4vol_main");
  const lockedView = document.getElementById("hip4vol_locked");
  if (mainView) mainView.classList.toggle("hidden", !showMain);
  if (lockedView) lockedView.classList.toggle("hidden", showMain);
}

async function _hip4volFetchTier() {
  try {
    const data = await apiGet("/api/hip4vol/tier");
    _hip4volApplyTier(data);
  } catch (err) {
    // Pas de gate invite-only : page normale même sans wallet / sur erreur
    // (401 attendu quand non connecté) ; le poll 5s re-tentera le tier.
    _hip4volShowMain(true);
  }
}

// ─ Available balance (USDC) ───────────────────────────────────────────────────
async function _hip4volFetchAvail() {
  const el = document.getElementById("hip4vol_avail");
  if (!el) return;
  try {
    const d = await apiGet(`/api/hip4/usdc_balance?slot=${HIP4VOL_SLOT}&subaccount=${encodeURIComponent(getSelectedTradingAccount("hip4"))}`);
    const avail = Number(d?.available ?? d?.total ?? 0);
    el.textContent = Number.isFinite(avail) ? `$${avail.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : "-";
    // Plafonne le slider de taille à min(available, size_cap) dès que la balance est connue.
    _hip4volAvailUsd = Number.isFinite(avail) ? avail : null;
    _hip4volApplySizeCap();
  } catch (e) {
    el.textContent = "-";
  }
}

// ─ Status poll ───────────────────────────────────────────────────────────────
// Stats normalisees d'un slot renvoye par /api/status (memes champs que le
// panneau live, qui accepte les variantes prefixees et plates).
function _hip4volSlotStats(sp) {
  const sess = (sp && (sp.session || sp)) || {};
  const volume = Number(sess.hip4vol_volume ?? sess.volume) || 0;
  const pnl = Number(sess.hip4vol_pnl ?? sess.pnl) || 0;
  const fees = Number(sess.hip4vol_fees_due ?? sess.fees_due) || 0;
  const trades = sess.hip4vol_trades ?? sess.trades;
  return {
    running: !!(sp && (sp.running || sp.process_running)),
    started: !!sp,
    volume,
    pnl,
    trades: trades != null ? Number(trades) : null,
    cost10k: volume > 0 ? ((fees - pnl) / volume) * 10000 : null,
  };
}

// Recapitulatif : une ligne par onglet ouvert, pour lire les stats de TOUTES
// les sessions sans changer d'onglet. Masque quand un seul onglet est ouvert
// (le panneau live dit deja tout) ou qu'aucune session ne tourne. Cliquer une
// ligne bascule sur l'onglet correspondant.
let _hip4volSessionsHtml = "";
function _hip4volRenderSessionsPanel(slots) {
  const panel = document.getElementById("hip4vol_sessions_panel");
  const body = document.getElementById("hip4vol_sessions_rows");
  const totalEl = document.getElementById("hip4vol_sessions_total");
  if (!panel || !body) return;
  const rows = _hip4volOpenSlots.map((slot) => ({
    slot,
    label: _hip4volTabLabel(slot),
    stats: _hip4volSlotStats((slots || []).find((sp) => Number(sp.slot) === slot)),
  }));
  const running = rows.filter((r) => r.stats.running);
  if (rows.length < 2 || !running.length) {
    panel.classList.add("hidden");
    _hip4volSessionsHtml = "";
    body.innerHTML = "";
    return;
  }
  panel.classList.remove("hidden");

  const html = rows.map((r) => {
    const st = r.stats;
    const cls = "hv-sessions-row" + (r.slot === HIP4VOL_SLOT ? " is-active" : "") + (st.running ? "" : " is-idle");
    const pnlColor = st.pnl < 0 ? "#f87171" : (st.pnl > 0 ? "#6ee7a8" : "#fff");
    const hasData = st.running || st.volume > 0;
    return `<button type="button" class="${cls}" data-slot="${r.slot}">` +
      `<div class="hv-sessions-name"><span class="hv-tab-dot${st.running ? " is-on" : ""}"></span>${escapeHtml(r.label)}</div>` +
      `<div>${hasData ? _hip4volFmtUsd(st.volume) : "-"}</div>` +
      `<div style="color:${hasData ? pnlColor : "inherit"}">${hasData ? _hip4volFmtUsd(st.pnl) : "-"}</div>` +
      `<div>${st.trades != null ? String(st.trades) : "-"}</div>` +
      `<div>${st.cost10k != null ? "$" + st.cost10k.toFixed(2) : "-"}</div>` +
      `</button>`;
  }).join("");
  // Le poll repasse toutes les 5 s : ne reconstruire le DOM que si le contenu a
  // change, sinon un utilisateur au clavier perd le focus de sa ligne a chaque
  // tour (et le survol souris clignote).
  if (html !== _hip4volSessionsHtml) {
    _hip4volSessionsHtml = html;
    body.innerHTML = html;
    body.querySelectorAll("button[data-slot]").forEach((el) => {
      el.addEventListener("click", () => _hip4volSelectSlot(Number(el.dataset.slot)));
    });
  }

  if (totalEl) {
    const totVol = rows.reduce((a, r) => a + r.stats.volume, 0);
    const totPnl = rows.reduce((a, r) => a + r.stats.pnl, 0);
    totalEl.textContent = `${running.length} running \u00b7 ${_hip4volFmtUsd(totVol)} volume \u00b7 ${_hip4volFmtUsd(totPnl)} PnL`;
  }
}

async function _hip4volPollStatus() {
  try {
    const data = await apiGet("/api/status");
    const slots = Array.isArray(data?.slots) ? data.slots : [];
    try { _hip4volSyncTabsFromStatus(slots); } catch (_) {}
    // Avant tout retour anticipe : le recapitulatif couvre TOUS les onglets, il
    // doit s'afficher meme quand l'onglet actif est a l'arret.
    try { _hip4volRenderSessionsPanel(slots); } catch (_) {}
    const sp = slots.find((s) => s.slot === HIP4VOL_SLOT);
    if (!sp) {
      // Aucune session connue sur le slot de l'onglet actif : etat idle, on ne
      // garde jamais le "running" d'un autre onglet.
      if (_hip4volRunning) _hip4volSetRunningState(false);
      _hip4volWasRunning = false;
      return;
    }

    const isRunning = !!(sp.running || sp.process_running);
    if (_hip4volWasRunning && !isRunning) _hip4volLoadHistory(); // session vient de s'arrêter -> nouveau record
    _hip4volWasRunning = isRunning;
    _hip4volSetRunningState(isRunning);

    if (!isRunning) return;

    // Show panel
    const panel = document.getElementById("hip4vol_status_panel");
    if (panel) panel.classList.remove("hidden");

    // Sub-account badge (Task 12) : reflète le trading_account de la session live.
    const subBadgeEl = document.getElementById("hip4vol_sub_badge");
    if (subBadgeEl) subBadgeEl.innerHTML = _sessionSubBadgeHtml(sp.trading_account);

    // Extract fields — support both hip4vol_* prefixed and flat variants
    const sess = sp.session || sp || {};
    const volume  = sess.hip4vol_volume  ?? sess.volume;
    const pnl     = sess.hip4vol_pnl     ?? sess.pnl;
    const trades  = sess.hip4vol_trades  ?? sess.trades;
    const feesDue = sess.hip4vol_fees_due ?? sess.fees_due;
    const resids  = sess.hip4vol_residuals ?? sess.residuals;
    const scanned = sess.hip4vol_scanned ?? sess.scanned;
    const qualify = sess.hip4vol_qualifying ?? sess.qualifying;

    // Compute live cost per $10k of volume
    const _vol  = Number(volume)  || 0;
    const _pnl  = Number(pnl)     || 0;
    const _fees = Number(feesDue) || 0;
    const _cost10k = _vol > 0 ? ((_fees - _pnl) / _vol) * 10000 : null;
    const _costEl = document.getElementById("hip4vol_cost_value");
    if (_costEl) _costEl.textContent = _cost10k != null ? `$${_cost10k.toFixed(2)} / $10k` : "-";

    const $ = (id) => document.getElementById(id);
    const setV = (id, val) => { const el = $(id); if (el) el.textContent = val; };

    setV("hip4vol_stat_volume",   _hip4volFmtUsd(volume));
    setV("hip4vol_stat_pnl",      _hip4volFmtUsd(pnl));
    setV("hip4vol_stat_trades",   trades != null ? String(trades) : "-");
    setV("hip4vol_stat_residuals", Array.isArray(resids) ? String(resids.length) : (resids != null ? String(resids) : "-"));
    setV("hip4vol_stat_cost",     _cost10k != null ? `$${_cost10k.toFixed(2)}` : "-");
    // Maker multi-marches : une ligne par outcome (phase), relayee par runtime_risk.
    const mmBox = document.getElementById("hip4vol_mm_markets");
    const mmOutcomes = sp.runtime_risk && sp.runtime_risk.hip4mm_outcomes;
    if (mmBox) {
      if (mmOutcomes && typeof mmOutcomes === "object" && Object.keys(mmOutcomes).length) {
        mmBox.classList.remove("hidden");
        mmBox.innerHTML = Object.entries(mmOutcomes).map(([id, o]) => {
          const label = (_hip4volOutcomes.find((x) => String(x.outcome_id) === String(id)) || {}).label || `#${id}`;
          const state = o && o.running ? (o.phase || "live") : ((o && o.phase) || "stopped");
          return `<div class="hv-mm-market"><span>${escapeHtml(label)}</span><span class="hv-mm-phase">${escapeHtml(String(state))}</span></div>`;
        }).join("");
      } else {
        mmBox.classList.add("hidden");
      }
    }
    // Color: volume always green; PnL red when negative, green when positive.
    const _volEl = $("hip4vol_stat_volume");
    if (_volEl) _volEl.style.color = "#6ee7a8";
    const _pnlEl = $("hip4vol_stat_pnl");
    if (_pnlEl) { const _p = Number(pnl) || 0; _pnlEl.style.color = _p < 0 ? "#f87171" : (_p > 0 ? "#6ee7a8" : "#fff"); }
    setV("hip4vol_stat_scanned",
      (scanned != null && qualify != null) ? `${scanned} / ${qualify}` :
      scanned != null ? String(scanned) : "-");
  } catch (err) {
    console.warn("[hip4vol] status poll failed:", err);
  }
}

// Paint the orange fill of a range slider, aligned to the THUMB CENTER.
// A native range thumb's center travels from thumbW/2 to width-thumbW/2, so a
// plain 0→100% gradient drifts off the thumb at the extremes. Compensate.
function _hip4volUpdateFill(slider) {
  if (!slider) return;
  const min = Number(slider.min) || 0, max = Number(slider.max) || 100, val = Number(slider.value) || 0;
  const ratio = max > min ? (val - min) / (max - min) : 0;
  const THUMB = 20; // matches .hv-slider thumb width
  const w = slider.offsetWidth || 0;
  let pct;
  if (w > THUMB) {
    const centerPx = (THUMB / 2) + ratio * (w - THUMB);
    pct = (centerPx / w) * 100;
  } else {
    pct = ratio * 100;
  }
  slider.style.setProperty("--fill", pct.toFixed(2) + "%");
}

// Two-way link a range slider with its numeric input.
function _hip4volLinkSlider(sliderId, inputId) {
  const slider = document.getElementById(sliderId);
  const input = document.getElementById(inputId);
  if (!slider || !input) return;
  slider.addEventListener("input", () => { input.value = slider.value; _hip4volUpdateFill(slider); });
  input.addEventListener("input", () => {
    let v = Number(input.value);
    if (!Number.isFinite(v)) v = Number(slider.min) || 0;
    if (v > Number(slider.max)) slider.max = String(v); // allow large (privileged) values
    slider.value = String(v);
    _hip4volUpdateFill(slider);
  });
  _hip4volUpdateFill(slider);
}

function _hip4volSetRunningState(running) {
  _hip4volRunning = running;
  // Le poller MM (refreshStatus) ne tourne pas forcément sur cette page :
  // synchronise directement l'entrée runningSlots du slot HIP-4, sinon le
  // verrou des pills Trading Account survit à la session (refresh obligatoire
  // pour changer de sub-account après un stop).
  const _rsIdx = runningSlots.findIndex((s) => s.slot === HIP4VOL_SLOT);
  if (!running && _rsIdx >= 0) runningSlots.splice(_rsIdx, 1);
  if (running && _rsIdx < 0) {
    runningSlots.push({ slot: HIP4VOL_SLOT, running: true, degraded: false,
                        tradingAccount: getSelectedTradingAccount("hip4") });
  }
  const startBtn = document.getElementById("hip4vol_start_btn");
  const stopBtn  = document.getElementById("hip4vol_stop_btn");
  const approveBtn = document.getElementById("hip4vol_approve_btn");
  const panel    = document.getElementById("hip4vol_status_panel");
  const consoleEl = document.getElementById("hip4vol_console");
  const stateEl  = document.getElementById("hip4vol_console_state");
  const modeTakerBtn = document.getElementById("hip4vol_mode_taker");
  const modeMakerBtn = document.getElementById("hip4vol_mode_maker");
  const cfgInputs = document.querySelectorAll("#hip4vol_page .hv-value-input, #hip4vol_page .hv-slider, #hip4vol_page .hv-code-input");
  if (startBtn) {
    startBtn.classList.toggle("hidden", running);
    // Le start l'a disabled au clic et il reste caché-disabled pendant la
    // session : ré-armer au retour idle, sinon Start est mort après un stop.
    if (!running) { startBtn.disabled = false; startBtn.textContent = "Start"; }
  }
  if (approveBtn) approveBtn.classList.toggle("hidden", running);
  if (stopBtn)  stopBtn.classList.toggle("hidden", !running);
  document.getElementById("hip4vol_stop_limit_btn")?.classList.toggle("hidden", !running);
  if (panel) panel.classList.toggle("hidden", !running);
  if (!running) { const b = document.getElementById("hip4vol_sub_badge"); if (b) b.innerHTML = ""; }
  if (consoleEl) consoleEl.classList.toggle("is-live", running);
  if (stateEl) stateEl.textContent = running ? "LIVE" : "";
  if (modeTakerBtn) modeTakerBtn.disabled = running;
  if (modeMakerBtn) modeMakerBtn.disabled = running;
  cfgInputs.forEach((el) => { el.disabled = running; });
  // Restore tier-driven locks (e.g. fixed spread for non-privileged) after stop.
  if (!running && _hip4volTier) _hip4volApplyTier(_hip4volTier);
  // Trading account pills follow the same running-session lock (Task 9 pattern).
  if (typeof renderTradingAccountSelector === "function") renderTradingAccountSelector();
}

function _hip4volStartPoll() {
  if (_hip4volPollTimer) return;
  _hip4volPollStatus();
  _hip4volPollTimer = setInterval(_hip4volPollStatus, HIP4VOL_POLL_MS);
}

function _hip4volStopPoll() {
  if (_hip4volPollTimer) { clearInterval(_hip4volPollTimer); _hip4volPollTimer = null; }
}

// ─ Lifecycle ─────────────────────────────────────────────────────────────────
function _hip4volOnEnter() {
  _hip4volShowMain(true);  // plus de gate invite-only — page toujours visible
  _hip4volFetchTier();
  _hip4volFetchAvail();
  _hip4volLoadHistory();
  _hip4volStartPoll();
  // Re-fetch tier + balance periodically so they update once the wallet connects
  // / changes (e.g. a privileged wallet) without needing to leave + re-enter.
  if (_hip4volTierTimer) clearInterval(_hip4volTierTimer);
  _hip4volTierTimer = setInterval(() => { _hip4volFetchTier(); _hip4volFetchAvail(); }, 5000);
}

function _hip4volOnLeave() {
  _hip4volStopPoll();
  if (_hip4volTierTimer) { clearInterval(_hip4volTierTimer); _hip4volTierTimer = null; }
}

// ─ Claim code ────────────────────────────────────────────────────────────────
async function _hip4volSubmitCode(inputId, msgId, btnId) {
  const codeInput = document.getElementById(inputId);
  const code = (codeInput?.value || "").trim();
  if (!code) { _hip4volSetMsg(msgId, "Enter a code first.", true); return; }
  const btn = btnId ? document.getElementById(btnId) : null;
  if (btn) btn.disabled = true;
  _hip4volClearMsg(msgId);
  try {
    const data = await apiPost("/api/hip4vol/claim_code", { code });
    if (data.ok) {
      _hip4volSetMsg(msgId, `Code accepted! Tier: ${data.tier ?? "-"}`, false);
      if (codeInput) codeInput.value = "";
      await _hip4volFetchTier(); // re-resolves access → unlocks the page
    } else {
      _hip4volSetMsg(msgId, data.detail || "Invalid code.", true);
    }
  } catch (err) {
    _hip4volSetMsg(msgId, err.message || "Claim failed.", true);
  } finally {
    if (btn) btn.disabled = false;
  }
}
function _hip4volUnlock() { return _hip4volSubmitCode("hip4vol_lock_code_input", "hip4vol_lock_msg", "hip4vol_lock_claim_btn"); }

// ─ Approve builder fees ───────────────────────────────────────────────────────
async function _hip4volApprove() {
  const btn = document.getElementById("hip4vol_approve_btn");
  _hip4volClearMsg("hip4vol_action_msg");
  // Wallet à tarif nul : c'est le SERVEUR qui dit s'il reste une approbation à
  // poser. Un compte fee-exempt garde le builder code attaché avec f=0, ce qui
  // exige quand même une approbation on-chain > 0 (HIP4_ZERO_FEE_ACCOUNTS).
  // Se fier au seul `fee_per_10k` du tier rendait ce bouton inerte pour ces
  // comptes : il annonçait « rien à approuver » pendant que /api/start exigeait
  // 16 bps on-chain, sans aucun chemin dans l'UI pour les poser (prod 2026-09-06).
  if (_hip4volTier && Number(_hip4volTier.fee_per_10k) === 0) {
    const _acct = (document.getElementById("account_address")?.value || "").trim();
    let _requiredRate = null;
    try {
      const _st = await apiGet(
        `/api/builder/status?account_address=${encodeURIComponent(_acct)}&mode=hip4vol&force=1`,
      );
      _requiredRate = Number(_st?.required_fee_rate);
    } catch (_) {
      _requiredRate = null; // statut illisible : on signe, le serveur tranchera
    }
    if (Number.isFinite(_requiredRate) && _requiredRate === 0) {
      _hip4volSetMsg("hip4vol_action_msg", "Your wallet is fee-exempt — no builder-fee approval needed.", false);
      return;
    }
  }
  if (btn) btn.disabled = true;
  try {
    await approveBuilderFeeWithWallet("hip4vol");
    // Success: morph the button into an animated checkmark (persisted across refresh).
    if (btn) {
      const acct = (document.getElementById("account_address")?.value || "").trim();
      _hip4volMarkApproved(btn, "Approved", acct);
    }
  } catch (err) {
    _hip4volSetMsg("hip4vol_action_msg", err.message || "Approval failed.", true);
    if (btn) btn.disabled = false;
  }
}

// ─ Start ─────────────────────────────────────────────────────────────────────
async function _hip4volStart() {
  // Pas connecté (aucun token d'auth) → déclencher le module de connexion
  // (modal 3 étapes) au lieu de laisser /api/start échouer en 401 silencieux.
  // L'utilisateur relance Start après la connexion — pas d'auto-start d'une
  // action qui engage des fonds.
  const _hasAuth = (typeof walletAuthToken === "string" && walletAuthToken) ||
                   (typeof walletAuthTokens === "object" && walletAuthTokens && Object.keys(walletAuthTokens).length > 0);
  if (!_hasAuth) {
    try { await _connectSingleDex(_activeDex); } catch (_) {}
    return;
  }
  const startBtn = document.getElementById("hip4vol_start_btn");
  if (startBtn) { startBtn.disabled = true; startBtn.textContent = "Starting…"; }
  _hip4volClearMsg("hip4vol_action_msg");

  const maxSize    = Number(document.getElementById("hip4vol_max_size")?.value  || 100);
  const maxVolume  = Number(document.getElementById("hip4vol_max_volume")?.value || 0);
  // Clamp to tier caps if present (le spread n'est plus une valeur libre : on
  // envoie le MODE, mappé et clampé côté serveur)
  const sizeCap   = _hip4volTier?.size_cap;
  const safeSizeUsd   = (sizeCap != null) ? Math.min(maxSize, sizeCap) : maxSize;

  // Un seul taker par wallet (il scanne tout l'univers) : message avant tout appel.
  if (_hip4volMode === "hip4vol") {
    const takerRunning = Object.entries(_hip4volSlotMeta).find(([s, m]) => Number(s) !== HIP4VOL_SLOT && m && m.running && m.mode === "hip4vol");
    if (takerRunning) {
      _hip4volSetMsg("hip4vol_action_msg",
        "The taker is already running on another tab. A second one scans the same markets: both race for the " +
        "same book depth, and stopping one can close the other's positions. One taker session at a time.", true);
      if (startBtn) { startBtn.disabled = false; startBtn.textContent = "Start"; }
      return;
    }
  }
  // Taker exclusif des makers (self-trade) : message explicatif avant tout appel.
  if (_hip4volMode === "hip4vol") {
    const makerTab = Object.entries(_hip4volSlotMeta).find(([s, m]) => Number(s) !== HIP4VOL_SLOT && m && m.running && m.mode === "hip4mm");
    if (makerTab) {
      const [mslot, m] = makerTab;
      _hip4volSetMsg("hip4vol_action_msg",
        `A maker session is running (${m.label || "Market " + (HIP4VOL_TAB_SLOTS.indexOf(Number(mslot)) + 1)}). ` +
        "The taker scans every live market and would trade against your own maker quotes (self-trade). " +
        "Stop your maker sessions first, or start another maker instead.", true);
      if (startBtn) { startBtn.disabled = false; startBtn.textContent = "Start"; }
      return;
    }
  }
  if (_hip4volMode === "hip4mm") {
    const takerTab = Object.entries(_hip4volSlotMeta).find(([s, m]) => Number(s) !== HIP4VOL_SLOT && m && m.running && m.mode === "hip4vol");
    if (takerTab) {
      _hip4volSetMsg("hip4vol_action_msg",
        "The taker is running on another tab. It scans every live market and would trade against this maker's quotes (self-trade). Stop the taker first.", true);
      if (startBtn) { startBtn.disabled = false; startBtn.textContent = "Start"; }
      return;
    }
  }
  // Maker mode: validate outcome + $50 floor before any API call.
  if (_hip4volMode === "hip4mm") {
    if (!_hip4volSelectedOutcomes.length) {
      _hip4volSetMsg("hip4vol_action_msg", "Choose an outcome market for maker mode.", true);
      if (startBtn) { startBtn.disabled = false; startBtn.textContent = "Start"; }
      return;
    }
    if (safeSizeUsd < 50) {
      _hip4volSetMsg("hip4vol_action_msg", "Maker mode requires at least $50.", true);
      if (startBtn) { startBtn.disabled = false; startBtn.textContent = "Start"; }
      return;
    }
  }

  try {
    // Step 1: save config
    const body = {
      max_size_usd:  safeSizeUsd,
      spread_mode:   _hip4volSpreadMode,
      max_volume:    maxVolume || null,
      mode:          _hip4volMode,
      subaccount:    getSelectedTradingAccount("hip4"),
      only_venues: _hip4volMode === "hip4vol" ? _hip4volTakerVenuesList() : [],
    };
    if (_hip4volMode === "hip4mm") {
      body.outcome_ids = _hip4volSelectedOutcomes.map(Number);
      body.seed_mode = "split"; // ouverture toujours par split (1 $ la paire, sans spread)
      // Quote spread réglable — le serveur clamp aussi (HIP4MM_SPREAD_MIN/MAX).
      // Bornes lues sur le champ lui-même : une troisième copie en dur ici avait
      // silencieusement ramené 1,5% à 0,2% après l'élargissement de la plage.
      const _spEl = document.getElementById("hip4mm_spread");
      const qsp = Number(_spEl?.value);
      if (Number.isFinite(qsp) && qsp > 0) {
        const _lo = Number(_spEl?.min) || 0.02;
        const _hi = Number(_spEl?.max) || 2;
        body.quote_spread_pct = Math.min(_hi, Math.max(_lo, qsp));
      }
    }
    await apiPost(`/api/hip4vol/save_config?slot=${HIP4VOL_SLOT}`, body);

    // Step 2: start session
    await apiPost(`/api/start?slot=${HIP4VOL_SLOT}`, {}, { timeoutMs: 30000 });

    // Step 3: confirm the bot is actually running BEFORE going LIVE.
    let running = false;
    for (let i = 0; i < 15; i++) {
      try {
        const st = await apiGet("/api/status");
        const sp = (st.slots || []).find((s) => s.slot === HIP4VOL_SLOT);
        if (sp && (sp.running || sp.process_running)) { running = true; break; }
      } catch (e) { /* keep polling */ }
      await new Promise((r) => setTimeout(r, 1000));
    }
    if (!running) {
      _hip4volSetMsg("hip4vol_action_msg", "Bot did not start — check approval / USDC and try again.", true);
      if (startBtn) { startBtn.disabled = false; startBtn.textContent = "Start"; }
      return;
    }

    // Confirmed running → ignition + LIVE.
    const consoleEl = document.getElementById("hip4vol_console");
    if (consoleEl) { consoleEl.classList.add("igniting"); setTimeout(() => consoleEl.classList.remove("igniting"), 900); }
    if (startBtn) startBtn.textContent = "Start";
    _hip4volSetRunningState(true);
    _hip4volStartPoll();
  } catch (err) {
    const msg = String(err.message || "Start failed.");
    const el = document.getElementById("hip4vol_action_msg");
    if (el) {
      const isBalanceErr = /insufficient|balance/i.test(msg);
      const cta = isBalanceErr ? _fundSubCtaHtml("hip4") : "";
      el.innerHTML = `${escapeHtml(msg)}${cta}`;
      el.style.color = "#f87171";
    }
    if (startBtn) { startBtn.disabled = false; startBtn.textContent = "Start"; }
  }
}

// ─ Stop ──────────────────────────────────────────────────────────────────────
// ─ Stop with limit orders ────────────────────────────────────────────────────
let _hip4volLimitPollTimer = null;

function _hip4volAskLimitDeadline() {
  return new Promise((resolve) => {
    const overlay = document.getElementById("hip4vol_limit_modal_overlay");
    const modal = document.getElementById("hip4vol_limit_modal");
    const choices = document.getElementById("hip4vol_limit_choices");
    const confirmBtn = document.getElementById("hip4vol_limit_modal_confirm");
    const closeBtn = document.getElementById("hip4vol_limit_modal_close");
    if (!overlay || !modal || !choices || !confirmBtn) { resolve(null); return; }

    let minutes = 30;
    function onChoice(e) {
      const btn = e.target.closest(".hv-limit-choice");
      if (!btn) return;
      minutes = parseInt(btn.dataset.min, 10);
      choices.querySelectorAll(".hv-limit-choice").forEach((b) => b.classList.toggle("is-active", b === btn));
    }
    function close(value) {
      overlay.classList.add("hidden"); overlay.setAttribute("aria-hidden", "true");
      modal.classList.add("hidden"); modal.setAttribute("aria-hidden", "true");
      choices.removeEventListener("click", onChoice);
      confirmBtn.removeEventListener("click", onConfirm);
      closeBtn?.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onCancel);
      document.removeEventListener("keydown", onKey);
      resolve(value);
    }
    function onConfirm() { close(minutes); }
    function onCancel() { close(null); }
    function onKey(e) { if (e.key === "Escape") close(null); }

    choices.addEventListener("click", onChoice);
    confirmBtn.addEventListener("click", onConfirm);
    closeBtn?.addEventListener("click", onCancel);
    overlay.addEventListener("click", onCancel);
    document.addEventListener("keydown", onKey);
    overlay.classList.remove("hidden"); overlay.setAttribute("aria-hidden", "false");
    modal.classList.remove("hidden"); modal.setAttribute("aria-hidden", "false");
    confirmBtn.focus();
  });
}

async function _hip4volStopLimit() {
  const minutes = await _hip4volAskLimitDeadline();
  if (!minutes) return;
  const btn = document.getElementById("hip4vol_stop_limit_btn");
  const stopBtn = document.getElementById("hip4vol_stop_btn");
  if (btn) { btn.disabled = true; btn.textContent = "Stopping…"; }
  if (stopBtn) stopBtn.disabled = true;
  _hip4volClearMsg("hip4vol_action_msg");
  try {
    await apiPost(`/api/stop?slot=${HIP4VOL_SLOT}&close_mode=limit&deadline_min=${minutes}`);
    // Le closer serveur n'existe qu'une fois le process bot sorti (jusqu'a 60 s
    // pour les modes HIP-4) : on attend l'arret reel avant de basculer la
    // console en idle, comme le fait le Stop classique.
    let stopped = false;
    for (let i = 0; i < 90; i++) {
      try {
        const st = await apiGet("/api/status");
        const sp = (st.slots || []).find((s) => s.slot === HIP4VOL_SLOT);
        if (!sp || !(sp.running || sp.process_running)) { stopped = true; break; }
      } catch (e) { /* keep polling */ }
      await new Promise((r) => setTimeout(r, 1000));
    }
    if (!stopped) _hip4volSetMsg("hip4vol_action_msg", "Stop requested (still settling…).", true);
    _hip4volStopPoll();
    _hip4volSetRunningState(false);
    _hip4volStartLimitPoll(true);
  } catch (err) {
    _hip4volSetMsg("hip4vol_action_msg", String(err.message || "Stop failed."), true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Stop with limit orders"; }
    if (stopBtn) stopBtn.disabled = false;
  }
}

let _hip4volLimitPollGraceUntil = 0;

function _hip4volStartLimitPoll(withGrace = false) {
  // Periode de grace : juste apres un stop, le closer peut mettre quelques
  // secondes a ecrire son etat ; un premier "inactive" ne doit pas couper le poll.
  _hip4volLimitPollGraceUntil = withGrace ? Date.now() + 30000 : 0;
  if (_hip4volLimitPollTimer) { _hip4volPollLimitClose(); return; }
  _hip4volPollLimitClose();
  _hip4volLimitPollTimer = setInterval(_hip4volPollLimitClose, 4000);
}

function _hip4volStopLimitPoll() {
  if (_hip4volLimitPollTimer) { clearInterval(_hip4volLimitPollTimer); _hip4volLimitPollTimer = null; }
}

const _HIP4VOL_LIMIT_NOTES = {
  resting: "resting at best price",
  dust_below_min: "too small for a limit order, will close at market",
  empty_book: "no book, waiting",
  flat: "closed",
  market_closed: "closed at market",
  aborted: "cancelled, closing at market",
};

async function _hip4volPollLimitClose() {
  let st;
  try {
    st = await apiGet(`/api/hip4/limit_close/status?slot=${HIP4VOL_SLOT}`);
  } catch (e) {
    // Sans wallet connecte (401) : inutile d'insister toutes les 4 s.
    if (e && e.status === 401) _hip4volStopLimitPoll();
    return;
  }
  const panel = document.getElementById("hip4vol_limit_close_panel");
  if (!panel) return;
  if (!st || !st.active) {
    panel.classList.add("hidden");
    if (Date.now() >= _hip4volLimitPollGraceUntil) _hip4volStopLimitPoll();
    return;
  }
  _hip4volLimitPollGraceUntil = 0;
  panel.classList.remove("hidden");
  const title = document.getElementById("hip4vol_limit_close_title");
  if (title) {
    title.textContent = st.phase === "market"
      ? "Closing at market"
      : "Closing with limit orders";
  }
  const timer = document.getElementById("hip4vol_limit_close_timer");
  if (timer) {
    const left = Math.max(0, Math.round((st.seconds_left || 0) / 60));
    timer.textContent = st.phase === "market" ? "" : `${left} min left`;
  }
  const rows = document.getElementById("hip4vol_limit_close_rows");
  if (rows) {
    rows.innerHTML = Object.entries(st.coins || {}).map(([coin, c]) => {
      const note = _HIP4VOL_LIMIT_NOTES[c.note] || String(c.note || "");
      const px = c.px ? ` @ ${c.px}` : "";
      return `<div class="hv-limit-close-row"><span>${escapeHtml(coin)}</span>`
        + `<span>${Number(c.qty || 0).toFixed(2)}${escapeHtml(px)}</span>`
        + `<span class="hv-limit-close-note">${escapeHtml(note)}</span></div>`;
    }).join("");
  }
}

async function _hip4volLimitCloseMarket() {
  const btn = document.getElementById("hip4vol_limit_close_market_btn");
  if (btn) { btn.disabled = true; btn.textContent = "Closing…"; }
  try {
    await apiPost(`/api/hip4/limit_close/market?slot=${HIP4VOL_SLOT}`);
    _hip4volPollLimitClose();
  } catch (err) {
    _hip4volSetMsg("hip4vol_action_msg", String(err.message || "Close failed."), true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Close now (market)"; }
  }
}

async function _hip4volStop() {
  const stopBtn = document.getElementById("hip4vol_stop_btn");
  if (stopBtn) stopBtn.disabled = true;
  _hip4volClearMsg("hip4vol_action_msg");

  if (stopBtn) stopBtn.textContent = "Stopping…";
  try {
    await apiPost(`/api/stop?slot=${HIP4VOL_SLOT}`);
    // Verify the bot actually stopped (poll status up to ~14s).
    let stopped = false;
    for (let i = 0; i < 14; i++) {
      try {
        const st = await apiGet("/api/status");
        const sp = (st.slots || []).find((s) => s.slot === HIP4VOL_SLOT);
        if (!sp || !(sp.running || sp.process_running)) { stopped = true; break; }
      } catch (e) { /* keep polling */ }
      await new Promise((r) => setTimeout(r, 1000));
    }
    // Shutdown burst + confirmation checkmark on the Stop button (like Approve).
    const consoleEl = document.getElementById("hip4vol_console");
    if (consoleEl) { consoleEl.classList.add("shutting"); setTimeout(() => consoleEl.classList.remove("shutting"), 750); }
    if (stopBtn) {
      stopBtn.classList.add("is-stopped");
      stopBtn.innerHTML = '<span class="hv-check"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12.5l5 5L20 6.5"/></svg></span><span class="hv-check-label">Stopped</span>';
    }
    if (!stopped) _hip4volSetMsg("hip4vol_action_msg", "Stop requested (still settling…).", true);
    else _hip4volClearMsg("hip4vol_action_msg");
    _hip4volStopPoll();
    // After the confirmation, revert the console to the idle Start state.
    setTimeout(() => {
      if (stopBtn) { stopBtn.classList.remove("is-stopped"); stopBtn.textContent = "Stop"; stopBtn.disabled = false; }
      _hip4volSetRunningState(false);
    }, 1200);
  } catch (err) {
    _hip4volSetMsg("hip4vol_action_msg", err.message || "Stop failed.", true);
    if (stopBtn) { stopBtn.disabled = false; stopBtn.textContent = "Stop"; }
  }
}

// ─ Init (bind events once DOM ready) ─────────────────────────────────────────
function _hip4volBind() {
  const lockBtn = document.getElementById("hip4vol_lock_claim_btn");
  if (lockBtn) lockBtn.addEventListener("click", _hip4volUnlock);
  const lockInput = document.getElementById("hip4vol_lock_code_input");
  if (lockInput) lockInput.addEventListener("keydown", (e) => { if (e.key === "Enter") _hip4volUnlock(); });

  const approveBtn = document.getElementById("hip4vol_approve_btn");
  if (approveBtn) approveBtn.addEventListener("click", _hip4volApprove);

  const startBtn = document.getElementById("hip4vol_start_btn");
  if (startBtn) startBtn.addEventListener("click", _hip4volStart);

  const stopBtn = document.getElementById("hip4vol_stop_btn");
  if (stopBtn) stopBtn.addEventListener("click", _hip4volStop);
  document.getElementById("hip4vol_stop_limit_btn")?.addEventListener("click", _hip4volStopLimit);
  document.getElementById("hip4vol_limit_close_market_btn")?.addEventListener("click", _hip4volLimitCloseMarket);

  document.getElementById("hip4vol_mode_taker")?.addEventListener("click", () => _hip4volSetMode("hip4vol"));
  document.getElementById("hip4vol_mode_maker")?.addEventListener("click", () => _hip4volSetMode("hip4mm"));
  // Mode et reglages : restaures depuis la config du slot actif (onglets),
  // le localStorage ne sert que de repli quand le slot n'a pas de config.
  try {
    const savedMode = localStorage.getItem(HIP4VOL_MODE_STORAGE_KEY);
    if (savedMode === "hip4mm" || savedMode === "hip4vol") _hip4volSetMode(savedMode);
  } catch (_) {}
  // Un rechargement de page pendant un close passif doit retrouver le bandeau.
  try { _hip4volStartLimitPoll(); } catch (_) {}
  // Trading style (spread) : bind + restaure la sélection persistée.
  _hip4volInitOnlyOut();
  _hip4volLoadOpenSlots();
  _hip4volRenderTabs();
  _hip4volSelectSlot(HIP4VOL_SLOT);
  document.getElementById("hip4vol_spread_conservative")?.addEventListener("click", () => _hip4volSetSpreadMode("conservative"));
  document.getElementById("hip4vol_spread_aggressive")?.addEventListener("click", () => _hip4volSetSpreadMode("aggressive"));
  _hip4volSetSpreadMode(_hip4volSpreadMode);
  const _hip4volSearchEl = document.getElementById("hip4vol_outcome_search");
  if (_hip4volSearchEl) {
    _hip4volSearchEl.addEventListener("input", () => {
      // L'utilisateur tape : le champ redevient un filtre (coche retirée).
      _hip4volSearchDirty = true;
      _hip4volSearchEl.classList.remove("is-selected");
      _hip4volRenderOutcomeList();
      _hip4volSetListOpen(true);
    });
    // Ouvre la liste au clic/focus, en présélectionnant le texte : taper
    // remplace le nom du marché choisi au lieu de s'y ajouter.
    const _hip4volOpenSearch = () => {
      _hip4volRenderOutcomeList();
      _hip4volSetListOpen(true);
      if (_hip4volSearchEl.value) _hip4volSearchEl.select();
    };
    _hip4volSearchEl.addEventListener("focus", _hip4volOpenSearch);
    _hip4volSearchEl.addEventListener("click", _hip4volOpenSearch);
    _hip4volSearchEl.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { _hip4volSetListOpen(false); _hip4volSearchEl.blur(); }
    });
  }
  // Referme la liste au clic en dehors du picker outcome.
  document.addEventListener("click", (e) => {
    const card = document.getElementById("hip4vol_outcome_card");
    // Cible detachee (element re-rendu par son propre gestionnaire de clic) :
    // on ne peut pas savoir d'ou venait le clic, on ne ferme pas.
    if (e.target && e.target.isConnected === false) return;
    if (card && !card.contains(e.target)) _hip4volSetListOpen(false);
  });

  _hip4volLinkSlider("hip4vol_size_slider", "hip4vol_max_size");
  _hip4volLinkSlider("hip4vol_volume_slider", "hip4vol_max_volume");
  _hip4volLinkSlider("hip4mm_spread_slider", "hip4mm_spread");
  // Point vert sur le slider spread quand on est à la valeur par défaut (0.04%)
  const _spreadDefaultMark = () => {
    const s = document.getElementById("hip4mm_spread_slider");
    if (!s) return;
    s.classList.toggle("is-default", Math.abs(Number(s.value) - 0.04) < 1e-9);
  };
  ["hip4mm_spread_slider", "hip4mm_spread"].forEach((id) => {
    document.getElementById(id)?.addEventListener("input", _spreadDefaultMark);
  });
  _spreadDefaultMark();

  // Re-align slider fills when the layout width changes.
  window.addEventListener("resize", () => {
    ["hip4vol_size_slider", "hip4vol_volume_slider", "hip4mm_spread_slider"].forEach((id) => {
      _hip4volUpdateFill(document.getElementById(id));
    });
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", _hip4volBind, { once: true });
} else {
  _hip4volBind();
}

