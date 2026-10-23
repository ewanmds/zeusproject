/* ============================================================================
   outcomes.js - Section "Outcome" (HIP-4) pour Tradoor
   Portage vanilla du design Claude Design (app/list/market/chrome.jsx), branché
   sur les VRAIS marchés HIP-4. Module exposé en window.OUTCOMES { onEnter(id), onLeave() }.
   Données : GET /api/markets?mode=hip4 (liste+mids), /api/hip4/outcome/<id> (meta),
   /api/hip4/book?coin=#N (order book live), /api/hip4/usdc_balance (balance, auth).
   Le placement d'ordre (CTA Buy) est livré en Phase 2.
   ============================================================================ */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  // Neutralise les tirets cadratin (- / -) y compris ceux venant des libellés
  // de l'API Hyperliquid, puis échappe le HTML.
  const esc = (s) => String(s == null ? "" : s).replace(/[-―]/g, "-").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const go = (p) => (window.navigate ? window.navigate(p) : (location.pathname = p));
  // Largeur de l'input montant ajustée au pixel sur son contenu (l'attribut
  // `size` surestime en police 40px, ce qui décale le cadre de focus).
  const _measCtx = document.createElement("canvas").getContext("2d");
  function sizeAmtInput(inp) {
    const cs = getComputedStyle(inp);
    _measCtx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    try { _measCtx.letterSpacing = cs.letterSpacing === "normal" ? "0px" : cs.letterSpacing; } catch (_) {}
    inp.style.width = Math.ceil(_measCtx.measureText(inp.value || "0").width + 4) + "px";
  }
  const SUBNAV = ["Trending", "All", "Crypto", "Economic", "Sports", "Finance", "Politics"];

  // ── Bridges wallet/auth Tradoor (globales de app.js) ──────────────────────
  // Outcomes = Hyperliquid : toujours le token du wallet HL, jamais celui de la venue active.
  function ocAuthHeaders(extra) { try { return (typeof authHeaders === "function") ? authHeaders(extra || {}, "hyperliquid") : (extra || {}); } catch (_) { return extra || {}; } }
  function ocIsConnected() {
    try {
      if (typeof walletAuthToken !== "undefined" && walletAuthToken) return true;
      if (typeof walletAuthTokens !== "undefined" && walletAuthTokens && Object.keys(walletAuthTokens).length) return true;
    } catch (_) {}
    return false;
  }
  function ocConnect() { try { if (typeof connectAndSignWallet === "function") return connectAndSignWallet(); } catch (_) {} }
  // Trading account (HL subaccounts, scope "hip4") — état partagé avec app.js.
  // `getSelectedTradingAccount`/`setSelectedTradingAccount` sont des déclarations
  // `function` top-level dans app.js → attachées à `window`. `_hlSubaccounts` est
  // un `let` top-level → PAS attaché à `window`, on le lit par son nom nu (les
  // deux <script> classiques partagent le même scope lexical global).
  function ocSelectedTA(scope) {
    try { return (typeof window.getSelectedTradingAccount === "function") ? window.getSelectedTradingAccount(scope) : ""; } catch (_) { return ""; }
  }
  function ocSetSelectedTA(addr, scope) {
    try { if (typeof window.setSelectedTradingAccount === "function") window.setSelectedTradingAccount(addr, scope); } catch (_) {}
  }
  function ocSubaccounts() {
    try { return (typeof _hlSubaccounts !== "undefined" && Array.isArray(_hlSubaccounts)) ? _hlSubaccounts : []; } catch (_) { return []; }
  }

  async function apiGet(path, auth) {
    const res = await fetch(path, { headers: auth ? ocAuthHeaders({}) : {} });
    if (!res.ok) throw new Error("HTTP " + res.status + " " + path);
    return res.json();
  }

  // ── State ─────────────────────────────────────────────────────────────────
  const S = {
    venue: (() => { try { return localStorage.getItem("oc_venue_filter") || "all"; } catch (_) { return "all"; } })(),
    markets: [], loaded: false, cat: "All", detail: null, meta: null, book: null, balance: null, positions: [],
    chartSamples: [], chartDisp: null, chartView: null, chartMode: null, curUnderlying: null,
    side: "yes", buySide: "Buy", amt: 0, orderType: "market", limitC: null,
    obTick: 0.0001, obMenuOpen: false,
    tMarkets: null, tBook: null, tCd: null, chartRAF: null,
  };

  // ── Icônes ────────────────────────────────────────────────────────────────
  function iconFor(label) {
    const s = String(label || "").toLowerCase();
    if (s.includes("btc") || s.includes("bitcoin")) return { key: "btc", emoji: "₿", cls: "oc-tok--btc" };
    if (s.includes("eth") || s.includes("ether")) return { key: "eth", emoji: "Ξ", bg: "#454a75" };
    if (s.includes("world cup") || s.includes("fifa") || s.includes("soccer") || s.includes("football") ||
        s.includes("premier league") || s.includes("champions league") || s.includes("la liga") || s.includes("serie a")) return { key: "soccer", emoji: "⚽", bg: "#16331f" };
    if (s.includes("tennis") || s.includes("us open")) return { key: "tennis", emoji: "🎾", bg: "#1f2d1a" };
    if (s.includes("nfl")) return { key: "nfl", emoji: "🏈", bg: "#2d1f1a" };
    if (s.includes("nba") || s.includes("finals") || s.includes("champion") || s.includes("game") || s.includes("cup") ||
        s.includes(" vs ") || s.includes("winner")) return { key: "trophy", emoji: "🏆", bg: "#2a2722" };
    if (s.includes("cpi") || s.includes("inflation")) return { key: "cpi", emoji: "📈", bg: "#1c2230" };
    if (s.includes("fed") || s.includes("rate") || s.includes("fomc")) return { key: "fed", emoji: "🏛️", bg: "#f3f3f3", color: "#111" };
    if (s.includes("sol")) return { key: "sol", emoji: "◎", bg: "#2a2444" };
    return { key: "generic", emoji: "◇", bg: "#222" };
  }
  function catFor(label) {
    const s = String(label || "").toLowerCase();
    if (s.includes("btc") || s.includes("bitcoin") || s.includes("eth") || s.includes("sol") || s.includes("crypto")) return "Crypto";
    if (s.includes("cpi") || s.includes("fed") || s.includes("rate") || s.includes("inflation") || s.includes("gdp")) return "Economic";
    if (s.includes("nba") || s.includes("finals") || s.includes("game") || s.includes("champion") || s.includes("cup") ||
        s.includes(" vs ") || s.includes("winner") || s.includes("league") || s.includes("nfl") || s.includes("tennis") ||
        s.includes("us open") || s.includes("serie a") || s.includes("la liga")) return "Sports";
    return "Other";
  }
  // Drapeaux pays (marchés type "champion" : World Cup, etc.)
  const FLAGS = {
    "algeria": "🇩🇿", "argentina": "🇦🇷", "australia": "🇦🇺", "austria": "🇦🇹",
    "belgium": "🇧🇪", "bosnia and herzegovina": "🇧🇦", "brazil": "🇧🇷", "canada": "🇨🇦",
    "cape verde": "🇨🇻", "colombia": "🇨🇴", "congo dr": "🇨🇩", "croatia": "🇭🇷",
    "curacao": "🇨🇼", "czechia": "🇨🇿", "ecuador": "🇪🇨", "egypt": "🇪🇬",
    "england": "🏴\u{E0067}\u{E0062}\u{E0065}\u{E006E}\u{E0067}\u{E007F}", "france": "🇫🇷",
    "germany": "🇩🇪", "ghana": "🇬🇭", "haiti": "🇭🇹", "iran": "🇮🇷", "iraq": "🇮🇶",
    "ivory coast": "🇨🇮", "japan": "🇯🇵", "jordan": "🇯🇴", "mexico": "🇲🇽",
    "morocco": "🇲🇦", "netherlands": "🇳🇱", "new zealand": "🇳🇿", "norway": "🇳🇴",
    "panama": "🇵🇦", "paraguay": "🇵🇾", "portugal": "🇵🇹", "qatar": "🇶🇦",
    "saudi arabia": "🇸🇦", "scotland": "🏴\u{E0067}\u{E0062}\u{E0073}\u{E0063}\u{E0074}\u{E007F}",
    "senegal": "🇸🇳", "south africa": "🇿🇦", "south korea": "🇰🇷", "korea republic": "🇰🇷", "spain": "🇪🇸",
    "sweden": "🇸🇪", "switzerland": "🇨🇭", "tunisia": "🇹🇳", "turkey": "🇹🇷",
    "usa": "🇺🇸", "uruguay": "🇺🇾", "uzbekistan": "🇺🇿",
  };
  function flagFor(name) { return FLAGS[String(name || "").trim().toLowerCase()] || ""; }

  function tokHTML(ic, size) {
    const st = `width:${size}px;height:${size}px;font-size:${Math.round(size * 0.52)}px;`
      + (ic.bg ? `background:${ic.bg};` : "") + (ic.color ? `color:${ic.color};` : "");
    return `<div class="oc-tok ${ic.cls || ""}" style="${st}">${esc(ic.emoji)}</div>`;
  }
  function logoSVG(size) {
    return `<svg class="oc-brand-mark" width="${size}" height="${size}" viewBox="0 0 32 32" fill="none">
      <defs><linearGradient id="ocg" x1="4" y1="2" x2="28" y2="30" gradientUnits="userSpaceOnUse">
        <stop stop-color="#f7931a"/><stop offset="0.55" stop-color="#f7b733"/><stop offset="1" stop-color="#2ec27e"/></linearGradient></defs>
      <path d="M18.5 2.2 6.2 17.4c-.5.6-.1 1.5.7 1.5h6.1l-2.6 10.6c-.2.9.9 1.5 1.5.8l12.4-15.2c.5-.6.1-1.5-.7-1.5h-6.2l2.6-10.5c.2-.9-.9-1.5-1.5-.7Z" fill="url(#ocg)"/></svg>`;
  }

  // ── Illustrations SVG multicolores (fond de card, remplacent l'emoji) ──────
  function cardArt(key) {
    switch (key) {
      case "btc":
        return `<svg viewBox="0 0 140 140" fill="none" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
          <defs>
            <linearGradient id="oaBtc" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffd97a"/><stop offset=".5" stop-color="#f7931a"/><stop offset="1" stop-color="#b45c06"/></linearGradient>
            <linearGradient id="oaBtcUp" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#2ec27e"/><stop offset="1" stop-color="#7af0b6"/></linearGradient>
          </defs>
          <path d="M6 112 L40 84 L60 96 L98 50 L134 24" stroke="url(#oaBtcUp)" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
          <circle cx="86" cy="66" r="44" fill="url(#oaBtc)"/>
          <circle cx="86" cy="66" r="44" stroke="#ffe7b3" stroke-width="2.5" opacity=".55"/>
          <circle cx="72" cy="50" r="10" fill="#fff" opacity=".18"/>
          <text x="86" y="84" font-size="52" font-weight="800" text-anchor="middle" fill="#fff" font-family="Inter, sans-serif">₿</text>
        </svg>`;
      case "trophy":
        return `<svg viewBox="0 0 140 140" fill="none" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
          <defs>
            <linearGradient id="oaCup" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe9a3"/><stop offset="1" stop-color="#c8951f"/></linearGradient>
          </defs>
          <circle cx="26" cy="30" r="4" fill="#5be7a9"/><circle cx="120" cy="26" r="3.5" fill="#60a5fa"/>
          <rect x="44" y="22" width="6" height="6" rx="1.5" fill="#f0506e" transform="rotate(20 47 25)"/>
          <rect x="104" y="58" width="6" height="6" rx="1.5" fill="#a855f7" transform="rotate(35 107 61)"/>
          <circle cx="34" cy="72" r="3" fill="#f7931a"/>
          <path d="M44 30 h52 v16 a26 26 0 0 1 -52 0 z" fill="url(#oaCup)"/>
          <path d="M44 34 h-12 a12 12 0 0 0 12 12 z M96 34 h12 a12 12 0 0 1 -12 12 z" fill="url(#oaCup)"/>
          <rect x="64" y="70" width="12" height="16" fill="url(#oaCup)"/>
          <path d="M50 86 h40 l-6 14 h-28 z" fill="url(#oaCup)"/>
          <rect x="48" y="100" width="44" height="8" rx="3" fill="#9c7016"/>
          <path d="M70 34 l3.2 6.6 7.2 1 -5.2 5.1 1.2 7.2 -6.4 -3.4 -6.4 3.4 1.2 -7.2 -5.2 -5.1 7.2 -1 z" fill="#fff" opacity=".85"/>
        </svg>`;
      case "soccer":
        return `<svg viewBox="0 0 140 140" fill="none" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
          <defs>
            <radialGradient id="oaBall" cx=".38" cy=".34" r=".75"><stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#cfd6dd"/></radialGradient>
            <linearGradient id="oaPitch" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2ec27e"/><stop offset="1" stop-color="#15803d"/></linearGradient>
          </defs>
          <path d="M6 116 q64 -26 128 0 v18 h-128 z" fill="url(#oaPitch)" opacity=".55"/>
          <path d="M40 70 h60 M46 60 v52 M94 60 v52 M52 52 v68 M88 52 v68" stroke="#dfeee6" stroke-width="2" opacity=".35"/>
          <circle cx="86" cy="58" r="38" fill="url(#oaBall)"/>
          <circle cx="86" cy="58" r="38" stroke="#9aa4ad" stroke-width="2" opacity=".5"/>
          <path d="M86 40 l13 9.5 -5 15.3 -16 0 -5 -15.3 z" fill="#15212b"/>
          <path d="M86 40 l-1 -16 M99 49.5 l15 -5 M94 64.8 l9 13 M78 64.8 l-9 13 M73 49.5 l-15 -5" stroke="#1c2a35" stroke-width="3.4" stroke-linecap="round"/>
          <path d="M62 31 l5 9 M110 31 l-5 9 M120 70 l-10 -1 M52 70 l10 -1 M86 96 l0 -10" stroke="#1c2a35" stroke-width="3" stroke-linecap="round" opacity=".75"/>
          <circle cx="28" cy="34" r="3.5" fill="#f7931a"/><circle cx="120" cy="100" r="3" fill="#60a5fa"/>
        </svg>`;
      case "fed":
        return `<svg viewBox="0 0 140 140" fill="none" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
          <defs>
            <linearGradient id="oaFed" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#dbe7ff"/><stop offset="1" stop-color="#5b82c9"/></linearGradient>
          </defs>
          <polygon points="30,52 78,26 126,52" fill="url(#oaFed)"/>
          <rect x="30" y="52" width="96" height="7" rx="2" fill="url(#oaFed)"/>
          <rect x="36" y="62" width="9" height="44" fill="url(#oaFed)"/>
          <rect x="56" y="62" width="9" height="44" fill="url(#oaFed)"/>
          <rect x="76" y="62" width="9" height="44" fill="url(#oaFed)"/>
          <rect x="96" y="62" width="9" height="44" fill="url(#oaFed)"/>
          <rect x="116" y="62" width="9" height="44" fill="url(#oaFed)"/>
          <rect x="28" y="108" width="100" height="8" rx="2" fill="#3f5e96"/>
          <path d="M10 92 l14 -10 12 6" stroke="#2ec27e" stroke-width="4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M10 118 l14 8 12 -6" stroke="#f0506e" stroke-width="4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>`;
      case "cpi":
        return `<svg viewBox="0 0 140 140" fill="none" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
          <defs>
            <linearGradient id="oaCpi" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#2ec27e"/></linearGradient>
          </defs>
          <rect x="20" y="86" width="16" height="26" rx="3" fill="#60a5fa" opacity=".85"/>
          <rect x="44" y="70" width="16" height="42" rx="3" fill="#22d3ee" opacity=".85"/>
          <rect x="68" y="52" width="16" height="60" rx="3" fill="#2ec27e" opacity=".9"/>
          <rect x="92" y="36" width="16" height="76" rx="3" fill="#a3e635" opacity=".9"/>
          <path d="M22 92 L52 74 L76 58 L116 26" stroke="url(#oaCpi)" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M104 26 h12 v12" stroke="url(#oaCpi)" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>`;
      case "eth":
        return `<svg viewBox="0 0 140 140" fill="none" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
          <defs><linearGradient id="oaEth" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#a6b6ff"/><stop offset="1" stop-color="#5b6bd6"/></linearGradient></defs>
          <polygon points="86,18 116,68 86,86 56,68" fill="url(#oaEth)"/>
          <polygon points="86,18 116,68 86,52" fill="#fff" opacity=".25"/>
          <polygon points="86,92 116,74 86,118 56,74" fill="url(#oaEth)" opacity=".9"/>
        </svg>`;
      default:
        return `<svg viewBox="0 0 140 140" fill="none" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">
          <defs><linearGradient id="oaGen" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f7b733"/><stop offset=".5" stop-color="#f7931a"/><stop offset="1" stop-color="#2ec27e"/></linearGradient></defs>
          <circle cx="88" cy="60" r="40" fill="url(#oaGen)" opacity=".9"/>
          <circle cx="60" cy="92" r="20" fill="#60a5fa" opacity=".55"/>
          <path d="M20 110 L48 86 L70 98 L110 56" stroke="#fff" stroke-width="4" fill="none" opacity=".4" stroke-linecap="round"/>
        </svg>`;
    }
  }

  // ── Mapping marchés réels HIP-4 → cards ───────────────────────────────────
  // ── Dates lisibles ────────────────────────────────────────────────────────
  // Le backend émet l'expiry au format brut "20260613-0600" (dans expiry_str et
  // incrusté dans les labels "… by 20260613-0600"). On l'humanise partout :
  // "13 Jun 2026 · 06:00 UTC" (même style que parseLabel d'app.js).
  const OC_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function humanExpiry(raw) {
    const m = String(raw || "").match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})$/);
    if (!m) return "";
    const mo = OC_MONTHS[Number(m[2]) - 1] || m[2];
    return `${Number(m[3])} ${mo} ${m[1]} · ${m[4]}:${m[5]} UTC`;
  }
  // Badge du deployeur permissionless (outcomeMeta.venue) ; rien pour les
  // marches natifs Hyperliquid.
  const VENUE_BADGES = {
    out:  { text: "out",  title: "Deployed by outcome.xyz" },
    txyz: { text: "TXYZ", title: "Deployed by trade.xyz" },
    skew: { text: "skew", title: "Deployed by Skew" },
  };
  // Filtre par deployeur (chips de la liste). "hl" = marches natifs Hyperliquid
  // (venue vide). Ordre = ordre d'affichage.
  const VENUE_FILTERS = [
    { key: "all",  label: "All deployers" },
    { key: "hl",   label: "Hyperliquid" },
    { key: "out",  label: "outcome.xyz" },
    { key: "txyz", label: "TXYZ" },
    { key: "skew", label: "Skew" },
  ];
  function venueMatches(m, key) {
    if (!key || key === "all") return true;
    const v = String(m.venue || "").toLowerCase();
    return key === "hl" ? v === "" : v === key;
  }
  function venueChipsHTML(active) {
    return VENUE_FILTERS.map((f) =>
      `<button class="oc-chip oc-chip--venue${f.key === active ? " active" : ""}" data-venue="${esc(f.key)}" type="button">${esc(f.label)}</button>`
    ).join("");
  }
  function venueBadgeHTML(venue) {
    const v = VENUE_BADGES[String(venue || "").toLowerCase()];
    if (!v) return "";
    return `<span class="oc-venue oc-venue--${esc(String(venue).toLowerCase())}" title="${esc(v.title)}">${esc(v.text)}</span>`;
  }

  function humanizeLabel(s) {
    return String(s || "").replace(/\b(\d{8}-\d{4})\b/g, (raw) => humanExpiry(raw) || raw);
  }

  function mapMarkets(raw) {
    const groups = (raw && raw.groups) || [];
    const labels = (raw && raw.labels) || {};
    const mids = (raw && raw.mids) || {};
    const out = [];
    for (const g of groups) {
      const syms = g.symbols || [];
      if (syms.length < 2) continue;
      const yes = syms[0], no = syms[1];
      const base = g.label || "";
      const yesName = (labels[yes] || "").startsWith(base) ? ((labels[yes].slice(base.length).trim()) || "YES") : "YES";
      const noName = (labels[no] || "").startsWith(base) ? ((labels[no].slice(base.length).trim()) || "NO") : "NO";
      const mk = (p) => (typeof p === "number" && p > 0 && p < 1) ? p : null;
      const yPx = mk(mids[yes]), nPx = mk(mids[no]);
      let outcome = null;
      const n = parseInt(String(yes).replace("#", ""), 10);
      if (Number.isFinite(n)) outcome = Math.floor(n / 10);
      out.push({
        id: String(outcome), outcome, yesCoin: yes, noCoin: no, base, title: humanizeLabel(base),
        icon: iconFor(base), category: catFor(base), kind: "binary",
        parentKey: g.parent_key || "", parentTitle: humanizeLabel(g.parent_title || ""), subLabel: humanizeLabel(g.sub_label || base),
        dir: g.dir || "", idx: (g.idx == null ? null : Number(g.idx)),
        venue: String(g.venue || "").toLowerCase(),
        yesName, noName, yesPx: yPx, noPx: nPx,
        yes: { label: yesName.toUpperCase(), x: yPx ? "x" + (1 / yPx).toFixed(2) : "-", to: yPx ? "$" + Math.round(100 / yPx).toLocaleString("en-US") : "-" },
        no: { label: noName.toUpperCase(), x: nPx ? "x" + (1 / nPx).toFixed(2) : "-", to: nPx ? "$" + Math.round(100 / nPx).toLocaleString("en-US") : "-" },
      });
    }
    return out;
  }

  // ── Chrome (subnav catégories - le header Tradoor global reste au-dessus) ──
  function chromeHTML(activeCat) {
    const tabs = SUBNAV.map((s) => `<button class="${s === activeCat ? "active" : ""}" data-cat="${esc(s)}" type="button">${esc(s)}</button>`).join("");
    return `<div class="oc-wrap"><div class="oc-subnav">${tabs}</div></div>`;
  }
  function footerHTML() {
    return `<div class="oc-wrap"><div class="oc-footer">
      <div class="oc-footer-left">${logoSVG(22)}<span class="oc-online-dot"></span></div>
      <div class="oc-footer-links"><span class="oc-fl-lang">English</span><a>Docs</a><a>Support</a><a>Terms</a><a>Privacy Policy</a></div>
    </div></div>`;
  }

  // ── Liste ─────────────────────────────────────────────────────────────────
  function binaryCardHTML(m) {
    return `<div class="oc-card" data-goto="${esc(m.id)}">
      <div class="oc-card-deco" aria-hidden="true"></div>
      <div class="oc-card-art2" aria-hidden="true">${cardArt(m.icon.key)}</div>
      <div class="oc-card-art" aria-hidden="true">${cardArt(m.icon.key)}</div>
      <div class="oc-card-head"><div class="oc-card-title">${esc(m.title)}</div></div>
      <div class="oc-card-body">
        <div class="oc-bin-row">
          <button class="oc-bin-btn oc-bin-btn--yes" type="button">${esc(m.yes.label)} <span class="oc-mult">${esc(m.yes.x)}</span></button>
          <button class="oc-bin-btn oc-bin-btn--no" type="button">${esc(m.no.label)} <span class="oc-mult">${esc(m.no.x)}</span></button>
        </div>
        <div class="oc-bin-to"><span>$100 → <b>${esc(m.yes.to)}</b></span><span>$100 → <b>${esc(m.no.to)}</b></span></div>
      </div>
      <div class="oc-card-foot"><span class="oc-live"><span class="oc-live-dot"></span>Live</span>
        <span class="oc-card-meta">${venueBadgeHTML(m.venue)}</span></div>
    </div>`;
  }

  // Tri des membres d'un marché multi : tranche la plus haute en premier
  function sortMembers(arr) {
    return arr.slice().sort((a, b) => {
      const ai = (a.idx == null ? (parseInt(a.yesCoin.replace("#", ""), 10) || 0) : a.idx);
      const bi = (b.idx == null ? (parseInt(b.yesCoin.replace("#", ""), 10) || 0) : b.idx);
      return bi - ai;
    });
  }

  function multiCardHTML(group) {
    // Marché "champion/question" (parent_key "q|...") : pas d'ordre naturel →
    // trier par probabilité décroissante (favoris en tête) et ne montrer que le
    // haut du classement. Buckets de prix ("b|...") : garder l'ordre par tranche.
    const question = !!(group[0] && String(group[0].parentKey || "").startsWith("q|"));
    const members = question
      ? group.slice().sort((a, b) => (b.yesPx || 0) - (a.yesPx || 0))
      : sortMembers(group);
    const m0 = members[0];
    // carte ouvre l'outcome le plus probable
    const rep = members.reduce((best, x) => ((x.yesPx || 0) > (best.yesPx || 0) ? x : best), members[0]);
    // Carte "champion" avec beaucoup de choix (World Cup…) : n'afficher que les
    // 2 favoris avant le clic. Petits marchés (CPI 3 choix, buckets) : tout garder.
    const MAX = (question && members.length > 4) ? 2 : 6;
    const shown = members.length > MAX ? members.slice(0, MAX) : members;
    const rows = shown.map((s) => {
      const arrow = s.dir === "up" ? "↑ " : s.dir === "down" ? "↓ " : "";
      const name = String(s.subLabel || "").replace(/^[<>]\s*/, "");
      const flag = flagFor(name);
      const pct = s.yesPx ? Math.round(s.yesPx * 100) + "%" : "-";
      return `<div class="oc-multi-row" data-goto="${esc(s.id)}">
          <span class="oc-multi-label">${flag ? `<span class="oc-flag">${flag}</span>` : ""}${esc(arrow + name)}</span>
          <span class="oc-multi-pct">${pct}</span>
        </div>`;
    }).join("");
    const moreN = members.length - shown.length;
    const moreRow = moreN > 0
      ? `<div class="oc-multi-more" data-goto="${esc(rep.id)}">+${moreN} more outcomes</div>`
      : "";
    return `<div class="oc-card oc-card--multi" data-goto="${esc(rep.id)}">
        <div class="oc-card-deco" aria-hidden="true"></div>
        <div class="oc-card-art2" aria-hidden="true">${cardArt(m0.icon.key)}</div>
        <div class="oc-card-art" aria-hidden="true">${cardArt(m0.icon.key)}</div>
        <div class="oc-card-head"><div class="oc-card-title">${esc(m0.parentTitle || m0.base)}</div></div>
        <div class="oc-card-body">${rows}${moreRow}</div>
        <div class="oc-card-foot"><span class="oc-live"><span class="oc-live-dot"></span>Live</span><span class="oc-card-meta">${venueBadgeHTML(m0.venue)}</span></div>
      </div>`;
  }

  function renderList() {
    const root = $("outcomes_list_view");
    if (!root) return;
    const items = S.markets.filter((m) => (S.cat === "All" || S.cat === "Trending" || m.category === S.cat) && venueMatches(m, S.venue));
    // Regrouper les outcomes multi (même parent_key) en une seule card ; les
    // binaires (parent_key vide) restent une card chacun. Ordre de 1re apparition.
    const cells = [];
    const groupMap = new Map();
    for (const m of items) {
      if (m.parentKey) {
        if (!groupMap.has(m.parentKey)) { const arr = []; groupMap.set(m.parentKey, arr); cells.push({ type: "group", arr }); }
        groupMap.get(m.parentKey).push(m);
      } else {
        cells.push({ type: "single", m });
      }
    }
    // Avant la fin du premier fetch, S.markets est vide : afficher des cartes
    // skeleton plutôt que le message "no market" (qui flashait à l'arrivée).
    const skeleton = `<div class="oc-card oc-card--skel" aria-hidden="true">
        <div class="oc-skel-line oc-skel-line--title"></div>
        <div class="oc-skel-line"></div>
        <div class="oc-skel-line oc-skel-line--short"></div>
      </div>`.repeat(3);
    const cards = cells.length
      ? cells.map((c) => c.type === "group" ? (c.arr.length > 1 ? multiCardHTML(c.arr) : binaryCardHTML(c.arr[0])) : binaryCardHTML(c.m)).join("")
      : (S.loaded
          ? `<div class="oc-pan" style="grid-column:1/-1;padding:48px;text-align:center;color:var(--text-3)">No active HIP-4 market for "${esc(S.cat)}"${S.venue && S.venue !== "all" ? " on this deployer" : ""} right now.</div>`
          : skeleton);
    root.innerHTML = `${chromeHTML(S.cat)}
      <div class="oc-wrap"><div class="oc-list-layout">
        <div class="oc-chips">
          <button class="oc-chip oc-chip--live" type="button"><span class="oc-live-dot"></span>Live</button>
          <span class="oc-chip-divider"></span>
          ${venueChipsHTML(S.venue)}
        </div>
        <div class="oc-grid">${cards}</div>
      </div></div>`;
    wireChrome(root);
    const grid = root.querySelector(".oc-grid");
    if (grid) grid.addEventListener("click", (e) => {
      const el = e.target.closest("[data-goto]");
      if (el && grid.contains(el)) go("/outcomes/" + el.dataset.goto);
    });
  }

  function wireChrome(root) {
    root.querySelectorAll("[data-oc-home]").forEach((b) => b.addEventListener("click", () => go("/outcomes")));
    root.querySelectorAll(".oc-subnav button[data-cat]").forEach((b) =>
      b.addEventListener("click", () => { S.cat = b.dataset.cat; renderList(); }));
    root.querySelectorAll(".oc-chip[data-venue]").forEach((b) =>
      b.addEventListener("click", () => {
        S.venue = b.dataset.venue || "all";
        try { localStorage.setItem("oc_venue_filter", S.venue); } catch (_) {}
        renderList();
      }));
  }

  // ── Fiche marché ──────────────────────────────────────────────────────────
  function renderDetail() {
    const root = $("outcomes_detail_view");
    if (!root) return;
    const m = S.detail;
    if (!m) { root.innerHTML = `<div class="oc-wrap" style="padding:60px 34px;color:var(--text-3)">Loading…</div>`; return; }
    const meta = S.meta || {};
    const yesPx = m.yesPx || (meta.mid_yes || 0);
    const noPx = m.noPx || (meta.mid_no || (yesPx ? 1 - yesPx : 0));
    const yesC = yesPx ? Math.round(yesPx * 100) : null;
    const noC = noPx ? Math.round(noPx * 100) : null;
    const isPrice = !!(meta && meta.underlying);
    const target = (meta.target != null) ? fmtMoney(meta.target) : (isPrice ? "-" : "-");
    const curLabel = isPrice ? "Current" : "YES chance";
    const curInit = isPrice
      ? (S.curUnderlying ? fmtMoney(S.curUnderlying) : "…")
      : (yesPx ? Math.round(yesPx * 100) + "%" : "-");

    root.innerHTML = `${chromeHTML("Crypto")}
      <div class="oc-wrap">
        <button class="oc-mkt-back" data-oc-home="1" type="button">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 18l-6-6 6-6"/></svg> Markets
        </button>
        <div class="oc-market-grid">
          <div class="oc-market-main">
            <div class="oc-pan oc-mkt-header">
              <div class="oc-mkt-header-l">${tokHTML(m.icon, 48)}
                <div><div class="oc-mkt-title">${esc(m.title)}</div>
                  <div class="oc-mkt-sub">${venueBadgeHTML(m.venue || meta.venue)} ${esc(humanExpiry(meta.expiry_str) || meta.expiry_str || "")}
                    <span class="oc-mkt-rules"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h0"/></svg>Rules</span>
                  </div></div>
              </div>
              <div class="oc-mkt-stats">
                <div class="oc-mkt-stat"><div class="l">Volume</div><div class="v">${meta.volume != null ? esc(meta.volume) : "-"}</div></div>
                <div class="oc-mkt-stat"><div class="l">Open interest</div><div class="v">${meta.open_interest != null ? esc(meta.open_interest) : "-"}</div></div>
              </div>
            </div>
            <div class="oc-mkt-row" id="oc_mkt_row">
              <div class="oc-pan oc-chart-pan">
                <div class="oc-chart-top">
                  <div class="oc-chart-tc">
                    <div><div class="oc-chart-lbl"><span class="d" style="background:var(--text-3)"></span>Target</div><div class="oc-chart-val">${esc(target)}</div></div>
                    <div><div class="oc-chart-lbl"><span class="d" style="background:var(--accent)"></span>${esc(curLabel)}</div><div class="oc-chart-val oc-tnum" id="oc_current" style="color:var(--accent)">${esc(curInit)}</div></div>
                  </div>
                  <div class="oc-countdown" id="oc_cd"></div>
                </div>
                <div class="oc-chart-area" id="oc_chart"></div>
                <div class="oc-chart-tools">
                  <button class="oc-chart-tool active" type="button"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 17l5-6 4 3 6-8"/></svg></button>
                  <button class="oc-chart-tool" type="button"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M7 4v4M7 16v4M7 8a2 2 0 0 1 0 8M17 6v3M17 15v3M17 9a2 2 0 0 1 0 6"/></svg></button>
                </div>
              </div>
              <div class="oc-pan oc-ob" id="oc_ob"></div>
              <div class="oc-pan oc-oc" id="oc_outcomes"></div>
            </div>
            <div class="oc-pan oc-bt" id="oc_bt"></div>
          </div>
          <div style="display:flex;flex-direction:column;gap:18px">
            <div class="oc-pan oc-buy" id="oc_buy"></div>
            <div class="oc-pan" id="oc_collapse"></div>
          </div>
        </div>
      </div>`;
    wireChrome(root);
    renderOutcomesPanel();
    renderBuyPanel();
    renderBottomTabs();
    renderCollapse();
    renderBook();
    renderChart();
    startCountdown();
    document.querySelectorAll("#oc_chart").length && null;
    root.querySelectorAll(".oc-chart-tool").forEach((t) => t.addEventListener("click", () => {
      root.querySelectorAll(".oc-chart-tool").forEach((x) => x.classList.remove("active")); t.classList.add("active");
    }));
  }

  // Frères d'un même marché multi-choix (CPI, tranches de prix BTC…)
  function getSiblings() {
    const m = S.detail;
    const pk = (m && m.parentKey) || (S.meta && S.meta.parent_key) || "";
    if (!pk) return [];
    const sibs = S.markets.filter((x) => x.parentKey && x.parentKey === pk);
    return sortMembers(sibs);  // tranche la plus haute en premier (cohérent avec la liste)
  }

  // Le panneau "Outcomes" n'a de sens que pour les marchés à plusieurs choix.
  // Pour un binaire pur (Yes/No), on le masque et la fiche passe en 2 colonnes.
  function renderOutcomesPanel() {
    const box = $("oc_outcomes"); if (!box) return;
    const row = $("oc_mkt_row");
    const m = S.detail;
    const sibs = getSiblings();
    if (sibs.length <= 1) {
      box.style.display = "none";
      if (row) row.classList.add("oc-no-outcomes");
      return;
    }
    box.style.display = "";
    if (row) row.classList.remove("oc-no-outcomes");
    const title = (m && m.parentTitle) || (S.meta && S.meta.parent_title) || "Outcomes";
    // Tri par probabilité décroissante (favoris en tête)
    const ordered = sibs.slice().sort((a, b) => (b.yesPx || 0) - (a.yesPx || 0));
    const rows = ordered.map((s) => {
      const px = s.yesPx || 0;
      const pct = px ? Math.round(px * 100) + "%" : "-";
      const name = String(s.subLabel || "");
      const flag = flagFor(name);
      const active = String(s.id) === String(m.id) ? " active" : "";
      return `<div class="oc-oc-item${active}" data-go="${esc(s.id)}" data-name="${esc(name.toLowerCase())}">
        <div class="oc-oc-item-top"><span class="oc-oc-label">${flag ? `<span class="oc-flag">${flag}</span>` : ""}${esc(name)}</span><span class="oc-oc-pct" style="color:var(--accent)">${pct}</span></div>
        <div class="oc-oc-sub"><span class="oc-oc-vol">${px ? px.toFixed(3) + " / share" : ""}</span></div>
      </div>`;
    }).join("");
    box.innerHTML = `<div class="oc-oc-title">${esc(title)}</div>
      <div class="oc-oc-searchwrap">
        <svg class="oc-oc-search-ico" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
        <input class="oc-oc-search" id="oc_oc_search" type="text" placeholder="Search outcome…" autocomplete="off" spellcheck="false" />
      </div>
      <div class="oc-oc-list" id="oc_oc_list">${rows}</div>`;
    box.querySelectorAll(".oc-oc-item[data-go]").forEach((it) =>
      it.addEventListener("click", () => { if (it.dataset.go !== String(m.id)) go("/outcomes/" + it.dataset.go); }));
    const search = $("oc_oc_search"), list = $("oc_oc_list");
    if (search && list) {
      search.addEventListener("input", () => {
        const q = search.value.trim().toLowerCase();
        list.querySelectorAll(".oc-oc-item").forEach((it) => {
          it.style.display = (!q || (it.dataset.name || "").includes(q)) ? "" : "none";
        });
      });
    }
  }

  // Mise à jour "en place" des % du panneau outcomes (sans reconstruire le DOM)
  // → préserve le survol, le zoom et le texte de recherche lors du refresh 5 s.
  function refreshOutcomesPanelValues() {
    const list = $("oc_oc_list"); if (!list) return false;
    list.querySelectorAll(".oc-oc-item").forEach((it) => {
      const m = S.markets.find((x) => String(x.id) === it.dataset.go);
      if (!m) return;
      const px = m.yesPx || 0;
      const pctEl = it.querySelector(".oc-oc-pct"); if (pctEl) pctEl.textContent = px ? Math.round(px * 100) + "%" : "-";
      const volEl = it.querySelector(".oc-oc-vol"); if (volEl) volEl.textContent = px ? px.toFixed(3) + " / share" : "";
    });
    return true;
  }

  // Décimales / regroupement de l'order book (bouton en-tête).
  const OB_TICKS = [0.0001, 0.001, 0.01];
  function obDecimals(tick) { return tick >= 0.01 ? 2 : tick >= 0.001 ? 3 : 4; }
  function fmtTick(tick) { return tick.toFixed(obDecimals(tick)); }
  // Regroupe les niveaux selon le pas choisi : asks arrondis vers le haut,
  // bids vers le bas, en cumulant taille et notional par bucket.
  function groupLevels(levels, tick, side) {
    if (!(tick > 0.0001)) return levels || [];
    const map = new Map();
    for (const r of (levels || [])) {
      const bucket = side === "ask"
        ? Math.ceil((r.px - 1e-9) / tick) * tick
        : Math.floor((r.px + 1e-9) / tick) * tick;
      const key = bucket.toFixed(6);
      const c = map.get(key) || { px: bucket, sz: 0, total: 0 };
      c.sz += r.sz || 0; c.total += r.total || 0;
      map.set(key, c);
    }
    const arr = [...map.values()];
    arr.sort((a, b) => side === "ask" ? a.px - b.px : b.px - a.px);
    return arr;
  }

  function renderBook() {
    const box = $("oc_ob"); if (!box) return;
    if (!S._obDocBound) {
      S._obDocBound = true;
      document.addEventListener("click", (e) => {
        if (S.obMenuOpen && !(e.target.closest && e.target.closest("#oc_ob_tick"))) { S.obMenuOpen = false; renderBook(); }
      });
    }
    const b = S.book;
    const tick = S.obTick, dec = obDecimals(tick);
    let rows = "";
    if (!b || ((!b.asks || !b.asks.length) && (!b.bids || !b.bids.length))) {
      rows = `<div class="oc-ob-empty">Order book empty / loading…</div>`;
    } else {
      const asks = groupLevels(b.asks || [], tick, "ask");
      const bids = groupLevels(b.bids || [], tick, "bid");
      const all = [].concat(asks, bids);
      const maxT = Math.max(1, ...all.map((r) => r.total || 0));
      const askRows = asks.slice().reverse().map((r) => obRow(r, "ask", maxT, dec)).join("");
      const bidRows = bids.map((r) => obRow(r, "bid", maxT, dec)).join("");
      const spread = b.spread ? b.spread.toFixed(4) : "-";
      rows = `${askRows}<div class="oc-ob-spread"><span class="p oc-tnum">${spread}</span><span class="s">Spread</span></div>${bidRows}`;
    }
    const menu = S.obMenuOpen
      ? `<div class="oc-ob-tickmenu">${OB_TICKS.map((t) => `<button type="button" class="oc-ob-tickopt ${t === S.obTick ? "sel" : ""}" data-tick="${t}">${fmtTick(t)}</button>`).join("")}</div>`
      : "";
    box.innerHTML = `<div class="oc-ob-head"><span class="oc-ob-title">Order Book</span>
        <div class="oc-ob-depth" id="oc_ob_tick" role="button" tabindex="0" aria-haspopup="true" aria-expanded="${S.obMenuOpen}">${fmtTick(tick)} <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>${menu}</div></div>
      <div class="oc-ob-cols"><span>Price</span><span>Shares</span><span>Total</span></div>
      <div>${rows}</div>`;
    const tickBtn = $("oc_ob_tick");
    if (tickBtn) tickBtn.addEventListener("click", (e) => {
      const opt = e.target.closest(".oc-ob-tickopt");
      if (opt) { e.stopPropagation(); S.obTick = parseFloat(opt.dataset.tick); S.obMenuOpen = false; renderBook(); return; }
      e.stopPropagation(); S.obMenuOpen = !S.obMenuOpen; renderBook();
    });
  }
  function obRow(r, side, maxT, dec) {
    const px = r.px.toFixed(dec == null ? 4 : dec);
    const w = Math.min(100, (r.total || 0) / maxT * 100);
    return `<div class="oc-ob-row">
      <span class="oc-ob-p oc-ob-p--${side} oc-tnum">${px}</span>
      <span class="oc-ob-sh oc-tnum">${Math.round(r.sz)}</span>
      <span class="oc-ob-tt oc-tnum">$${(r.total || 0).toFixed(2)}</span>
      <span class="oc-ob-depthbar oc-ob-depthbar--${side}" style="width:${w}%"></span></div>`;
  }

  function fmtMoney(v) {
    if (v == null || isNaN(v)) return "-";
    const n = Number(v);
    return "$" + (Math.abs(n) >= 1000 ? Math.round(n).toLocaleString("en-US") : n.toFixed(2));
  }

  // ── Courbe animée unifiée (prix OU probabilité) ────────────────────────────
  // Mode "price" = prix du sous-jacent (BTC up/down) + ligne cible. Mode "prob" =
  // probabilité YES (marchés événementiels). Même moteur : rAF, défilement temporel
  // continu, easing/point de tête qui mène, warm-up à l'arrivée, axe Y lissé.
  const PRICE_WINDOW_MS = 60000; // fenêtre visible (60 s)
  const CW = 600, CHt = 340;

  function renderChart() {
    const area = $("oc_chart"); if (!area) return;
    if (S.chartSamples.length > 0) ensureChart(area);
    else if (area.dataset.kind !== "empty") {
      area.dataset.kind = "empty";
      area.innerHTML = `<div style="height:100%;display:grid;place-items:center;color:var(--text-4);font-size:13px">Loading chart…</div>`;
    }
  }

  // Warm-up : sème ~55 s d'historique (marche aléatoire de faible amplitude, à
  // réversion vers la valeur réelle courante) pour éviter la ligne plate à l'arrivée.
  function seedChartHistory(v, mode) {
    const n = 28, now = Date.now(), span = PRICE_WINDOW_MS * 0.92;
    const amp = mode === "prob" ? Math.max(v * 0.05, 0.004) : Math.max(v * 0.0006, 1);
    const out = new Array(n);
    let cur = v;
    for (let i = n - 1; i >= 0; i--) {
      out[i] = { t: now - ((n - 1 - i) / (n - 1)) * span, v: cur };
      cur += (Math.random() - 0.5) * amp * 0.45 + (v - cur) * 0.08;
      if (mode === "prob") cur = Math.max(0.001, Math.min(0.999, cur));
    }
    return out;
  }

  function ensureChart(area) {
    if (area.dataset.kind === "chart") { startChartRAF(); return; }
    area.dataset.kind = "chart";
    area.innerHTML = `<svg class="oc-chart-svg" viewBox="0 0 ${CW} ${CHt}" preserveAspectRatio="none">
        <defs><linearGradient id="ocfill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--accent)" stop-opacity="0.18"/><stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>
        ${[0.2, 0.4, 0.6, 0.8].map((f) => `<line x1="0" y1="${(CHt * f).toFixed(0)}" x2="${CW}" y2="${(CHt * f).toFixed(0)}" stroke="var(--border-soft)" stroke-width="1"/>`).join("")}
        <line class="oc-tgt" x1="0" x2="${CW}" stroke="var(--text-3)" stroke-width="1" stroke-dasharray="5 5" opacity="0.75" style="display:none"/>
        <path class="oc-fill" fill="url(#ocfill)"/>
        <path class="oc-line" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
        <circle class="oc-dot" r="5" fill="var(--accent)" stroke="var(--bg)" stroke-width="2"/>
      </svg>
      <div class="oc-chart-marker oc-tgt-tag" style="display:none"></div>
      <div class="oc-chart-yaxis"></div>`;
    startChartRAF();
  }

  function startChartRAF() {
    if (S.chartRAF) return;
    const loop = () => { S.chartRAF = requestAnimationFrame(loop); chartTick(); };
    S.chartRAF = requestAnimationFrame(loop);
  }
  function stopChartRAF() { if (S.chartRAF) { cancelAnimationFrame(S.chartRAF); S.chartRAF = null; } }

  function chartTick() {
    const area = $("oc_chart");
    if (!area || area.dataset.kind !== "chart" || S.view !== "detail") { stopChartRAF(); return; }
    const svg = area.querySelector("svg"); if (!svg) return;
    const samples = S.chartSamples;
    if (!samples.length) return;
    const mode = S.chartMode || "price";

    const now = Date.now();
    const winStart = now - PRICE_WINDOW_MS;
    // Valeur affichée : easing fluide vers le dernier échantillon (pas de saut).
    const targetV = samples[samples.length - 1].v;
    if (S.chartDisp == null) S.chartDisp = targetV;
    S.chartDisp += (targetV - S.chartDisp) * 0.13;

    // Le point de tête (now, chartDisp) MÈNE ; on exclut le tout dernier échantillon
    // brut de l'historique (sinon pic à gauche du point), représenté par le point eased.
    const inWin = samples.filter((s) => s.t >= winStart);
    const hist = inWin.slice(0, Math.max(0, inWin.length - 1));
    const pts = hist.map((s) => ({ t: s.t, v: s.v }));
    if (!pts.length || pts[0].t > winStart) {
      const prev = samples.filter((s) => s.t < winStart).slice(-1)[0] || samples[0];
      pts.unshift({ t: winStart, v: prev.v });
    }
    pts.push({ t: now, v: S.chartDisp });

    const target = (mode === "price" && S.meta && typeof S.meta.target === "number") ? S.meta.target : null;
    const vals = pts.map((p) => p.v);
    const mn = Math.min(...vals), mx = Math.max(...vals);
    const sp = (mx - mn) || (mode === "prob" ? 0.01 : Math.max(1, mx * 0.0006));
    const tLo = mn - sp * 0.35, tHi = mx + sp * 0.35;
    if (!S.chartView) S.chartView = { lo: tLo, hi: tHi };
    // L'axe contient TOUJOURS les données (expansion instantanée) et se resserre lentement.
    S.chartView.lo = Math.min(tLo, S.chartView.lo + (tLo - S.chartView.lo) * 0.08);
    S.chartView.hi = Math.max(tHi, S.chartView.hi + (tHi - S.chartView.hi) * 0.08);
    const lo = S.chartView.lo, hi = S.chartView.hi;

    const x = (t) => ((t - winStart) / PRICE_WINDOW_MS) * CW;
    const y = (v) => CHt - ((v - lo) / ((hi - lo) || 1)) * CHt;
    const dPath = pts.map((p, i) => (i ? "L" : "M") + x(p.t).toFixed(1) + "," + y(p.v).toFixed(1)).join(" ");
    const lastX = x(now), lastY = y(S.chartDisp), x0 = x(pts[0].t);
    svg.querySelector(".oc-line").setAttribute("d", dPath);
    svg.querySelector(".oc-fill").setAttribute("d", `${dPath} L${lastX.toFixed(1)},${CHt} L${x0.toFixed(1)},${CHt} Z`);
    const dot = svg.querySelector(".oc-dot");
    dot.setAttribute("cx", lastX.toFixed(1)); dot.setAttribute("cy", lastY.toFixed(1));

    const tgtLine = svg.querySelector(".oc-tgt");
    const tgtTag = area.querySelector(".oc-tgt-tag");
    if (target != null) {
      const ty = Math.max(2, Math.min(CHt - 2, y(target)));
      tgtLine.setAttribute("y1", ty.toFixed(1)); tgtLine.setAttribute("y2", ty.toFixed(1)); tgtLine.style.display = "";
      tgtTag.style.display = ""; tgtTag.style.top = `calc(${(ty / CHt * 100).toFixed(1)}% - 12px)`;
    } else { tgtLine.style.display = "none"; tgtTag.style.display = "none"; }

    if (!S._chartTextTs || now - S._chartTextTs > 200) {
      S._chartTextTs = now;
      const fmt = (v) => mode === "prob" ? Math.round(v * 100) + "%" : fmtMoney(v);
      const yax = area.querySelector(".oc-chart-yaxis");
      if (yax) yax.innerHTML = [0, 0.25, 0.5, 0.75, 1].map((f) => `<span>${fmt(hi - f * (hi - lo))}</span>`).join("");
      if (target != null && tgtTag) tgtTag.innerHTML = `${fmtMoney(target)} <span style="color:var(--accent)">${target > S.chartDisp ? "▲" : "▼"}</span>`;
      const cur = $("oc_current");
      if (cur) {
        if (mode === "prob") {
          cur.textContent = Math.round(S.chartDisp * 100) + "%";
        } else {
          const delta = S.chartDisp - pts[0].v;
          const col = delta >= 0 ? "var(--yes)" : "var(--no)";
          cur.innerHTML = `${fmtMoney(S.chartDisp)} <span style="color:${col};font-size:13px;font-weight:600">${delta >= 0 ? "↑" : "↓"}${fmtMoney(Math.abs(delta))}</span>`;
        }
      }
    }
  }

  // Prix effectif servant au calcul du "Potential Return" : prix limite si on est
  // en mode Limit, sinon le mid courant du côté choisi.
  function currentEffPx() {
    const m = S.detail; if (!m) return 0;
    const yesPx = m.yesPx || 0, noPx = m.noPx || (yesPx ? 1 - yesPx : 0);
    const px = S.side === "yes" ? yesPx : noPx;
    return S.orderType === "limit" ? ((S.limitC || 0) / 100) : px;
  }
  function refreshPotential() {
    const pot = $("oc_potential"); if (!pot) return;
    const ep = currentEffPx();
    // Achat : $ misé / prix = retour potentiel. Vente : parts × prix = $ reçu.
    const v = (S.amt > 0 && ep > 0) ? (S.buySide === "Sell" ? S.amt * ep : S.amt / ep) : 0;
    pot.textContent = "$" + (v || 0).toFixed(2);
  }

  function renderBuyPanel() {
    const box = $("oc_buy"); if (!box) return;
    const m = S.detail;
    const yesPx = m.yesPx || 0, noPx = m.noPx || (yesPx ? 1 - yesPx : 0);
    const yesC = yesPx ? Math.round(yesPx * 100) : 0, noC = noPx ? Math.round(noPx * 100) : 0;
    const px = S.side === "yes" ? yesPx : noPx;             // mid courant du côté
    const isLimit = S.orderType === "limit";
    const isSell = S.buySide === "Sell";
    if (isLimit && (S.limitC == null)) S.limitC = Math.max(1, Math.min(99, Math.round(px * 100) || 50));
    const effPx = isLimit ? (S.limitC / 100) : px;          // prix utilisé pour le calcul
    const connected = ocIsConnected();
    const heldQty = isSell ? ocHeldQty(m.outcome, S.side) : 0;
    // En achat : S.amt = montant $. En vente : S.amt = nombre de parts.
    const potential = isSell
      ? ((S.amt > 0 && effPx > 0) ? "$" + (S.amt * effPx).toFixed(2) : "$0.00")
      : ((S.amt > 0 && effPx > 0) ? "$" + (S.amt / effPx).toFixed(2) : "$0.00");
    const sideName = S.side === "yes" ? esc(m.yesName) : esc(m.noName);
    const balLabel = isSell ? "Avl. position" : "Avl. Balance";
    let bal;
    if (isSell) {
      bal = connected ? (heldQty > 0 ? `${ocFmtShares(heldQty)} ${sideName} (~$${(heldQty * px).toFixed(2)})` : "0") : "N/A";
    } else {
      bal = S.balance && typeof S.balance.available === "number" ? "$" + S.balance.available.toFixed(2) : (connected ? "…" : "N/A");
    }
    const cta = connected ? `${esc(S.buySide)} ${sideName}` : "Sign In";
    box.innerHTML = `
      <div class="oc-buy-head">${tokHTML(m.icon, 28)}<span class="oc-buy-price">${esc(m.title)}</span></div>
      <div class="oc-buy-tabs"><div class="oc-buy-tabs-l">
        <button class="oc-buy-tab ${S.buySide === "Buy" ? "active" : ""}" data-bs="Buy" type="button">Buy</button>
        <button class="oc-buy-tab ${S.buySide === "Sell" ? "active" : ""}" data-bs="Sell" type="button">Sell</button>
      </div>
      <div class="oc-otype">
        <button class="oc-otype-btn ${!isLimit ? "active" : ""}" data-ot="market" type="button">Market</button>
        <button class="oc-otype-btn ${isLimit ? "active" : ""}" data-ot="limit" type="button">Limit</button>
      </div></div>
      <div class="oc-yn-row">
        <button class="oc-yn-btn oc-yn-btn--yes ${S.side === "yes" ? "sel" : ""}" data-side="yes" type="button">${esc(m.yesName)} ${yesC}¢</button>
        <button class="oc-yn-btn oc-yn-btn--no ${S.side === "no" ? "sel" : ""}" data-side="no" type="button">${esc(m.noName)} ${noC}¢</button>
      </div>
      ${isLimit ? `<div class="oc-limit-row">
        <span class="oc-limit-lbl">Limit price</span>
        <div class="oc-limit-field"><input id="oc_limit_input" class="oc-limit-input oc-tnum" type="number" min="1" max="99" step="1" value="${S.limitC}" /><span class="oc-limit-unit">¢</span></div>
      </div>` : ""}
      ${(() => {
        const taAddr = ocSelectedTA("hip4");
        const taSubs = ocSubaccounts();
        const btns = [`<button type="button" class="ta-option ${!taAddr ? "active" : ""}" data-ta="" data-scope="hip4">Main</button>`];
        for (let i = 1; i <= 2; i++) {
          const sub = taSubs[i - 1];
          if (sub) {
            const active = !!taAddr && taAddr.toLowerCase() === String(sub.address || "").toLowerCase();
            btns.push(`<button type="button" class="ta-option ${active ? "active" : ""}" data-ta="${esc(sub.address)}">${esc(sub.name || ("Sub " + i))}</button>`);
          } else {
            btns.push(`<button type="button" class="ta-option ta-empty" data-ta-slot="${i}">+ Sub ${i}</button>`);
          }
        }
        return `<div class="trading-account-wrap oc-ta-wrap"><div class="trading-account-selector" id="oc_ta_selector">${btns.join("")}</div></div>`;
      })()}
      <div class="oc-balance-box">
        <div class="oc-balance-top"><span class="oc-balance-l">${balLabel}</span><span class="oc-balance-na">${esc(bal)}</span></div>
        <div class="oc-balance-amt-wrap">${isSell ? "" : `<span class="oc-balance-cur">$</span>`}<input id="oc_amt_input" class="oc-balance-amt oc-balance-amt-input oc-tnum" type="text" inputmode="decimal" autocomplete="off" value="${S.amt}" />${isSell ? `<span class="oc-balance-unit">shares</span>` : ""}</div>
        <div class="oc-quick-row">
          ${isSell
            ? [25, 50, 100].map((p) => `<button class="oc-quick-btn" data-pct="${p}" type="button">${p === 100 ? "Max" : p + "%"}</button>`).join("")
            : [10, 50, 100, 500].map((q) => `<button class="oc-quick-btn" data-add="${q}" type="button">+${q}</button>`).join("") + `<button class="oc-quick-btn" data-max="1" type="button">Max</button>`}
        </div>
        <div class="oc-potential"><span class="oc-potential-l">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v10"/></svg>${S.buySide === "Sell" ? "You receive" : "Potential Return"}</span>
          <span class="oc-potential-v oc-tnum" id="oc_potential">${potential}</span></div>
      </div>
      <button class="oc-buy-cta" id="oc_cta" type="button">${cta}</button>`;

    const taSel = box.querySelector("#oc_ta_selector");
    if (taSel) taSel.addEventListener("click", (e) => {
      const btn = e.target.closest(".ta-option");
      if (!btn) return;
      if (btn.classList.contains("ta-empty")) { try { window.openWalletsDrawer && window.openWalletsDrawer(); } catch (_) {} return; }
      ocSetSelectedTA(btn.dataset.ta || "", "hip4");
      loadBalance();
      loadPositions();
      if (S.btTab === "Open Orders") loadOpenOrders();
      renderBuyPanel();
    });
    box.querySelectorAll(".oc-yn-btn[data-side]").forEach((b) => b.addEventListener("click", () => { S.side = b.dataset.side; S.limitC = null; S.amt = 0; renderOutcomesPanel(); renderBuyPanel(); }));
    box.querySelectorAll(".oc-buy-tab[data-bs]").forEach((b) => b.addEventListener("click", () => { if (S.buySide !== b.dataset.bs) S.amt = 0; S.buySide = b.dataset.bs; renderBuyPanel(); }));
    box.querySelectorAll(".oc-otype-btn[data-ot]").forEach((b) => b.addEventListener("click", () => { S.orderType = b.dataset.ot; if (S.orderType === "market") S.limitC = null; renderBuyPanel(); }));
    box.querySelectorAll(".oc-quick-btn[data-add]").forEach((b) => b.addEventListener("click", () => { S.amt += parseInt(b.dataset.add, 10); renderBuyPanel(); }));
    box.querySelectorAll(".oc-quick-btn[data-pct]").forEach((b) => b.addEventListener("click", () => {
      const pct = parseInt(b.dataset.pct, 10);
      const held = ocHeldQty(m.outcome, S.side);
      S.amt = pct >= 100 ? held : Math.round(held * pct) / 100;
      renderBuyPanel();
    }));
    const maxBtn = box.querySelector(".oc-quick-btn[data-max]");
    if (maxBtn) maxBtn.addEventListener("click", () => { S.amt = Math.max(0, Math.floor((S.balance && S.balance.available) || 0)); renderBuyPanel(); });
    // Saisie libre du montant (sans re-render pour ne pas perdre le focus) : on
    // met à jour le Potential Return + l'encart Order Information en direct.
    const amtInput = $("oc_amt_input");
    if (amtInput) {
      sizeAmtInput(amtInput);
      amtInput.addEventListener("input", () => {
        let v = parseFloat(String(amtInput.value).replace(/[^0-9.]/g, ""));
        if (!Number.isFinite(v) || v < 0) v = 0;
        S.amt = v;
        sizeAmtInput(amtInput);
        refreshPotential();
        renderCollapse();
      });
      // À la sortie du champ, on normalise l'affichage sur la valeur clampée.
      amtInput.addEventListener("blur", () => { if (String(S.amt) !== amtInput.value) { amtInput.value = S.amt; sizeAmtInput(amtInput); } });
      // Un clic sélectionne tout : taper remplace directement le montant.
      amtInput.addEventListener("focus", () => amtInput.select());
      const amtWrap = box.querySelector(".oc-balance-amt-wrap");
      if (amtWrap) amtWrap.addEventListener("click", (e) => { if (e.target !== amtInput) { amtInput.focus(); amtInput.select(); } });
    }
    const limitInput = $("oc_limit_input");
    if (limitInput) {
      limitInput.addEventListener("input", () => {
        let v = parseFloat(limitInput.value);
        if (!Number.isFinite(v)) return;
        // Le champ est en cents (1-99). Un prix tapé en dollars (0.45) est
        // converti, au lieu d'être clampé à 1¢ comme avant ($0.24 de payout).
        if (v > 0 && v < 1) v = v * 100;
        v = Math.round(v);
        if (v < 1) return;            // saisie en cours ("0", "0.") : on attend
        S.limitC = Math.min(99, v);
        refreshPotential();
      });
      // À la sortie du champ, réaffiche la valeur effective en cents.
      limitInput.addEventListener("blur", () => { if (String(S.limitC) !== limitInput.value) limitInput.value = S.limitC; });
    }
    const ctaBtn = $("oc_cta");
    if (ctaBtn) ctaBtn.addEventListener("click", onCta);
  }
  async function onCta() {
    if (!ocIsConnected()) { ocConnect(); return; }
    const m = S.detail;
    if (!m || !m.outcome) { ocToast("Invalid market."); return; }
    const side = S.side.toUpperCase();          // YES | NO
    const action = S.buySide.toLowerCase();     // buy | sell
    const isSell = S.buySide === "Sell";
    const isLimit = S.orderType === "limit";
    if (isLimit && !(S.limitC > 0 && S.limitC < 100)) { ocToast("Enter a limit price (1–99¢)."); return; }
    // Le trade part sur le même compte que le solde affiché (mini-selector hip4).
    const body = { outcome: Number(m.outcome), side, action, order_type: S.orderType, subaccount: ocSelectedTA("hip4") };
    if (isLimit) body.limit_price = S.limitC / 100;
    if (isSell) {
      // En vente, on cède des PARTS détenues (pas du cash). On envoie la taille
      // exacte en shares pour ne jamais tenter de vendre plus que détenu.
      const held = ocHeldQty(m.outcome, S.side);
      if (!(held > 0)) { ocResultPopup({ kind: "error", title: "No position", detail: `You don't hold any ${side} shares on this market.` }); return; }
      let shares = S.amt;
      if (!(shares > 0)) { ocToast("Enter a number of shares."); return; }
      if (shares >= held) shares = held;        // vendre toute la position
      body.size = shares;
    } else {
      if (!(S.amt > 0)) { ocToast("Enter an amount."); return; }
      // Le solde n'est plus plafonné à la saisie : on vérifie ici, à l'envoi.
      const avail = (S.balance && typeof S.balance.available === "number") ? S.balance.available : null;
      if (avail != null && S.amt > avail) { ocToast("Insufficient balance."); return; }
      body.usd_amount = S.amt;
    }
    const cta = $("oc_cta");
    if (cta) { cta.disabled = true; cta.textContent = "Sending…"; }
    try {
      const res = await fetch("/api/hip4/taker_order", {
        method: "POST",
        headers: ocAuthHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        ocResultPopup({ kind: "error", title: "Order failed", detail: data.detail || ("HTTP " + res.status) });
      } else {
        const r = data.result || {};
        if (r.ok) {
          if (r.filled_sz > 0) {
            const px = (r.avg_px != null) ? (r.avg_px * 100).toFixed(1) + "¢" : "?";
            ocResultPopup({
              kind: "success",
              title: "Position opened",
              detail: `${action === "sell" ? "Sold" : "Bought"} ${r.filled_sz} ${side} @ ${px}`,
              sub: `~$${(r.notional || 0).toFixed(2)}`,
            });
          } else if (r.resting_oid) {
            ocResultPopup({ kind: "pending", title: "Limit order placed", detail: `@ ${S.limitC}¢ (resting)`, sub: "See Open Orders" });
          } else {
            ocResultPopup({ kind: "success", title: "Order placed" });
          }
          S.amt = 0;
          loadBalance(); loadBook(); loadPositions();
        } else {
          ocResultPopup({ kind: "error", title: "Order not filled", detail: ocOrderErrMsg(r.error) });
        }
      }
    } catch (e) {
      ocResultPopup({ kind: "error", title: "Network error", detail: e.message });
    } finally {
      renderBuyPanel();
    }
  }
  // Traduit les codes d'erreur de l'adapter en message lisible.
  function ocOrderErrMsg(err) {
    const s = String(err || "unknown reason");
    const m = /^below_min_notional:([\d.]+)<([\d.]+)/.exec(s);
    if (m) return `Order value $${m[1]} is below Hyperliquid's $${Math.round(Number(m[2]))} minimum. Increase the amount${S.buySide === "Sell" ? " or raise your limit price" : ""}.`;
    if (s === "empty_book") return "No liquidity on this side right now.";
    if (s === "no_fill_at_price") return "No fill at the current price. Try again or use a limit order.";
    return s;
  }

  function renderCollapse() {
    const box = $("oc_collapse"); if (!box) return;
    const m = S.detail; const px = S.side === "yes" ? m.yesPx : m.noPx;
    box.innerHTML = `<div class="oc-collapse" id="oc_collapse_head"><span class="oc-collapse-t">Order Information</span>
        <span class="oc-collapse-ico"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg></span></div>
      <div class="oc-collapse-body hidden" id="oc_collapse_body">
        <div class="oc-ci-row"><span class="l">Avg. Price</span><span class="v oc-tnum">${px ? Math.round(px * 100) + "¢" : "-"}</span></div>
        <div class="oc-ci-row"><span class="l">Shares</span><span class="v oc-tnum">${px && S.amt ? Math.floor(S.amt / px) : 0}</span></div>
        <div class="oc-ci-row"><span class="l">Est. Fees</span><span class="v oc-tnum">$0.00</span></div>
        <div class="oc-ci-row"><span class="l">Total</span><span class="v oc-tnum">$${S.amt.toFixed ? S.amt.toFixed(2) : S.amt}</span></div>
      </div>`;
    const head = $("oc_collapse_head"), body = $("oc_collapse_body");
    if (head) head.addEventListener("click", () => { head.parentElement.querySelector(".oc-collapse").classList.toggle("open"); body.classList.toggle("hidden"); });
  }

  function renderBottomTabs() {
    const box = $("oc_bt"); if (!box) return;
    const tabs = ["Positions", "Open Orders", "Trade History", "Outcomes"];
    if (!S.btTab) S.btTab = "Positions";
    box.innerHTML = `<div class="oc-bt-row">${tabs.map((t) => `<button class="oc-bt-tab ${t === S.btTab ? "active" : ""}" data-bt="${esc(t)}" type="button">${esc(t)}</button>`).join("")}</div>
      <div id="oc_bt_body"></div>`;
    box.querySelectorAll(".oc-bt-tab[data-bt]").forEach((t) => t.addEventListener("click", () => { S.btTab = t.dataset.bt; renderBottomTabs(); }));
    renderBtBody();
  }
  function btEmpty(msg) {
    return `<div class="oc-bt-empty"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" opacity="0.5"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M8 14h8"/></svg>${esc(msg)}</div>`;
  }
  function renderBtBody() {
    const body = $("oc_bt_body"); if (!body) return;
    if (S.btTab === "Open Orders") {
      if (!ocIsConnected()) { body.innerHTML = btEmpty("Connect your wallet to see your orders"); return; }
      body.innerHTML = btEmpty("Loading…");
      loadOpenOrders();
      return;
    }
    if (S.btTab === "Positions") {
      if (!ocIsConnected()) { body.innerHTML = btEmpty("Connect your wallet to see your positions"); return; }
      if (S.positions && S.positions.length) renderPositions();
      else body.innerHTML = btEmpty("Loading…");
      loadPositions();
      return;
    }
    body.innerHTML = btEmpty("No " + S.btTab.toLowerCase() + " yet");
  }
  // Retrouve le marché chargé (titre + prix yesPx/noPx) à partir d'un outcome.
  function ocMarketFor(outcome) {
    return (S.markets || []).find((mk) => Number(mk.outcome) === Number(outcome));
  }
  function renderPositions() {
    const body = $("oc_bt_body"); if (!body) return;
    if (S.btTab !== "Positions") return;
    const list = (S.positions || []).filter((p) => Number(p.qty) > 0);
    if (!list.length) { body.innerHTML = btEmpty("No positions yet"); return; }
    const rows = list.map((p) => {
      const mk = ocMarketFor(p.outcome);
      const px = mk ? (p.side === "NO" ? (mk.noPx || 0) : (mk.yesPx || 0)) : 0;
      const title = mk ? esc(mk.title) : ("Outcome #" + p.outcome);
      const val = px ? "$" + (Number(p.qty) * px).toFixed(2) : "-";
      const cents = px ? Math.round(px * 100) + "¢" : "-";
      return `<div class="oc-pos-row">
          <span class="oc-pos-title">${title}</span>
          <span class="oc-pos-side oc-pos-side--${p.side === "NO" ? "no" : "yes"}">${esc(p.side)}</span>
          <span class="oc-pos-sh oc-tnum">${ocFmtShares(p.qty)} <small>@ ${cents}</small></span>
          <span class="oc-pos-val oc-tnum">${val}</span>
          <button class="oc-quick-btn" data-sell-outcome="${p.outcome}" data-sell-side="${p.side === "NO" ? "no" : "yes"}" type="button">Sell</button>
        </div>`;
    }).join("");
    body.innerHTML = `<div class="oc-pos-wrap">${rows}</div>`;
    body.querySelectorAll("[data-sell-outcome]").forEach((b) => b.addEventListener("click", () => {
      ocStartSell(Number(b.dataset.sellOutcome), b.dataset.sellSide);
    }));
  }
  // Ouvre le panneau de vente pré-rempli pour une position donnée.
  function ocStartSell(outcome, side) {
    const sd = side === "no" ? "no" : "yes";
    if (S.detail && Number(S.detail.outcome) === Number(outcome)) {
      S.buySide = "Sell"; S.side = sd; S.orderType = "market"; S.limitC = null;
      S.amt = ocHeldQty(outcome, sd);
      renderBuyPanel(); renderOutcomesPanel();
      const buyBox = $("oc_buy"); if (buyBox && buyBox.scrollIntoView) buyBox.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    // Marché différent : on navigue, et onEnter appliquera l'intent de vente.
    const mk = ocMarketFor(outcome);
    if (!mk) { ocToast("Market not found."); return; }
    S.pendingSell = { outcome: Number(outcome), side: sd };
    go("/outcomes/" + mk.id);
  }
  async function loadOpenOrders() {
    const body = $("oc_bt_body"); if (!body) return;
    let orders = [];
    try { const d = await apiGet("/api/hip4/open_orders?subaccount=" + encodeURIComponent(ocSelectedTA("hip4")), true); orders = (d && d.orders) || []; }
    catch (_) { body.innerHTML = btEmpty("Couldn't load orders"); return; }
    if (S.btTab !== "Open Orders" || !$("oc_bt_body")) return;
    if (!orders.length) { body.innerHTML = btEmpty("No open orders yet"); return; }
    const rows = orders.map((o) => `<div class="oc-ob-row" style="grid-template-columns:1.2fr 1fr 1fr auto;align-items:center;cursor:default">
        <span class="oc-ob-p oc-ob-p--${o.side === "buy" ? "bid" : "ask"} oc-tnum">${o.coin} ${o.side}</span>
        <span class="oc-ob-sh oc-tnum">${Math.round(o.sz)}</span>
        <span class="oc-ob-tt oc-tnum">${(o.limit_px * 100).toFixed(1)}¢</span>
        <button class="oc-quick-btn" data-cancel-coin="${esc(o.coin)}" data-cancel-oid="${o.oid}" type="button" style="margin-left:10px">Cancel</button>
      </div>`).join("");
    body.innerHTML = `<div style="padding:8px 0">${rows}</div>`;
    body.querySelectorAll("[data-cancel-oid]").forEach((b) => b.addEventListener("click", () => cancelOrder(b.dataset.cancelCoin, b.dataset.cancelOid, b)));
  }
  async function cancelOrder(coin, oid, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    try {
      // subaccount : le cancel doit être signé avec le même vault que l'ordre affiché.
      const res = await fetch("/api/hip4/cancel_order", { method: "POST", headers: ocAuthHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ coin, oid: Number(oid), subaccount: ocSelectedTA("hip4") }) });
      const d = await res.json().catch(() => ({}));
      if (res.ok && d.result && d.result.ok) { ocToast("Order cancelled."); loadOpenOrders(); }
      else { ocToast("Cancel failed: " + (d.detail || (d.result && d.result.error) || res.status)); if (btn) { btn.disabled = false; btn.textContent = "Cancel"; } }
    } catch (e) { ocToast("Network error: " + e.message); if (btn) { btn.disabled = false; btn.textContent = "Cancel"; } }
  }

  // ── Countdown ─────────────────────────────────────────────────────────────
  function startCountdown() {
    stopCountdown();
    tickCountdown();
    S.tCd = setInterval(tickCountdown, 1000);
  }
  function stopCountdown() { if (S.tCd) { clearInterval(S.tCd); S.tCd = null; } }
  function tickCountdown() {
    const box = $("oc_cd"); if (!box) { stopCountdown(); return; }
    const ts = S.meta && S.meta.expiry_ts ? Number(S.meta.expiry_ts) * (S.meta.expiry_ts > 1e12 ? 1 : 1000) : 0;
    if (!ts) { box.innerHTML = `<div class="oc-cd-cell"><div class="oc-cd-num">-</div><div class="oc-cd-unit">EXPIRY</div></div>`; return; }
    let s = Math.max(0, Math.floor((ts - Date.now()) / 1000));
    const h = Math.floor(s / 3600); s -= h * 3600;
    const mn = Math.floor(s / 60); s -= mn * 60;
    const cell = (n, u) => `<div class="oc-cd-cell"><div class="oc-cd-num oc-tnum">${String(n).padStart(2, "0")}</div><div class="oc-cd-unit">${u}</div></div>`;
    box.innerHTML = cell(h, "HOURS") + cell(mn, "MINS") + cell(s, "SECS");
  }

  // ── Toast minimal ─────────────────────────────────────────────────────────
  function ocToast(msg) {
    let t = $("oc_toast");
    if (!t) { t = document.createElement("div"); t.id = "oc_toast"; t.style.cssText = "position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#17181b;border:1px solid #2a2b2f;color:#ededed;padding:12px 18px;border-radius:10px;font:14px Inter,sans-serif;z-index:9999;box-shadow:0 8px 30px rgba(0,0,0,.5)"; document.body.appendChild(t); }
    t.textContent = msg; t.style.opacity = "1";
    clearTimeout(t._h); t._h = setTimeout(() => { t.style.opacity = "0"; }, 2600);
  }

  // ── Pop-up de résultat d'ordre (succès / en attente / erreur) ─────────────
  let ocRpRaf = null;
  let ocRpTimer = null;
  const OC_RP_ICONS = {
    success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
    pending: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/></svg>',
    error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  };
  function ocReducedMotion() {
    try { return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (_) { return false; }
  }
  function ocBuildResultPopup() {
    let overlay = $("oc_result_overlay");
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.id = "oc_result_overlay";
    overlay.className = "oc-rp-overlay hidden";
    const pop = document.createElement("div");
    pop.id = "oc_result_popup";
    pop.className = "oc-rp hidden";
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-live", "assertive");
    pop.innerHTML =
      '<canvas class="oc-rp-confetti" id="oc_result_confetti"></canvas>' +
      '<button type="button" class="oc-rp-close" aria-label="Close">&#10005;</button>' +
      '<div class="oc-rp-icon" id="oc_result_icon"></div>' +
      '<div class="oc-rp-title" id="oc_result_title"></div>' +
      '<div class="oc-rp-detail" id="oc_result_detail"></div>' +
      '<div class="oc-rp-sub" id="oc_result_sub"></div>';
    document.body.appendChild(overlay);
    document.body.appendChild(pop);
    overlay.addEventListener("click", ocCloseResultPopup);
    pop.querySelector(".oc-rp-close").addEventListener("click", ocCloseResultPopup);
    return overlay;
  }
  function ocOnKeyResultPopup(e) { if (e.key === "Escape") ocCloseResultPopup(); }
  function ocCloseResultPopup() {
    if (ocRpRaf) { cancelAnimationFrame(ocRpRaf); ocRpRaf = null; }
    if (ocRpTimer) { clearTimeout(ocRpTimer); ocRpTimer = null; }
    const overlay = $("oc_result_overlay"); const pop = $("oc_result_popup");
    if (overlay) overlay.classList.add("hidden");
    if (pop) pop.classList.add("hidden");
    document.removeEventListener("keydown", ocOnKeyResultPopup);
  }
  function ocResultPopup(opts) {
    const o = opts || {};
    const kind = (o.kind === "success" || o.kind === "pending" || o.kind === "error") ? o.kind : "success";
    ocBuildResultPopup();
    const overlay = $("oc_result_overlay"); const pop = $("oc_result_popup");
    if (!pop) return;
    pop.classList.remove("oc-rp--success", "oc-rp--pending", "oc-rp--error");
    pop.classList.add("oc-rp--" + kind);
    $("oc_result_icon").innerHTML = OC_RP_ICONS[kind] || OC_RP_ICONS.success;
    $("oc_result_title").textContent = o.title || "";
    const detail = $("oc_result_detail"); detail.textContent = o.detail || ""; detail.style.display = o.detail ? "" : "none";
    const sub = $("oc_result_sub"); sub.textContent = o.sub || ""; sub.style.display = o.sub ? "" : "none";
    overlay.classList.remove("hidden");
    pop.classList.remove("hidden");
    // Force un reflow pour (re)jouer l'animation d'entrée CSS à chaque ouverture.
    void pop.offsetWidth;
    document.addEventListener("keydown", ocOnKeyResultPopup);
    if (ocRpRaf) { cancelAnimationFrame(ocRpRaf); ocRpRaf = null; }
    if (kind === "success" && !ocReducedMotion()) ocResultConfetti();
    if (ocRpTimer) clearTimeout(ocRpTimer);
    ocRpTimer = setTimeout(ocCloseResultPopup, kind === "error" ? 5000 : 4200);
  }
  function ocResultConfetti() {
    const canvas = $("oc_result_confetti"); if (!canvas) return;
    const ctx = canvas.getContext("2d"); if (!ctx) return;
    const r = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, r.width); canvas.height = Math.max(1, r.height);
    const colors = ["#35f0c2", "#7cffd4", "#ffffff", "#ff8c3c"];
    const parts = [];
    for (let i = 0; i < 90; i++) {
      parts.push({
        x: canvas.width / 2, y: canvas.height * 0.30,
        vx: (Math.random() - 0.5) * 9, vy: Math.random() * -8 - 2,
        s: Math.random() * 3 + 2, color: colors[(Math.random() * colors.length) | 0], life: 1,
      });
    }
    function draw() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      let alive = false;
      for (const p of parts) {
        p.vy += 0.22; p.x += p.vx; p.y += p.vy; p.life -= 0.008;
        if (p.life > 0 && p.y < canvas.height + 12) {
          alive = true; ctx.globalAlpha = Math.max(0, p.life); ctx.fillStyle = p.color;
          ctx.fillRect(p.x, p.y, p.s, p.s);
        }
      }
      ctx.globalAlpha = 1;
      ocRpRaf = alive ? requestAnimationFrame(draw) : null;
    }
    ocRpRaf = requestAnimationFrame(draw);
  }

  // ── Data loops ────────────────────────────────────────────────────────────
  async function loadMarkets() {
    try {
      const raw = await apiGet("/api/markets?mode=hip4&exchange=hyperliquid_mainnet", false);
      S.markets = mapMarkets(raw);
      S.loaded = true;
      if (S.view === "list") {
        // Ne pas reconstruire la grille pendant que l'utilisateur survole une
        // carte : sinon le DOM est recréé toutes les 5 s et le faisceau de
        // bordure "clignote" (disparaît puis revient). On rafraîchit dès que le
        // curseur quitte la grille.
        const grid = $("outcomes_list_view") && $("outcomes_list_view").querySelector(".oc-grid");
        if (!grid || !grid.matches(":hover")) renderList();
      }
      if (S.view === "detail" && S.detail) {
        const fresh = S.markets.find((x) => x.id === S.detail.id);
        if (fresh) {
          S.detail.yesPx = fresh.yesPx; S.detail.noPx = fresh.noPx;
          S.detail.parentKey = fresh.parentKey; S.detail.parentTitle = fresh.parentTitle; S.detail.subLabel = fresh.subLabel;
          if (!S.detail.base) { S.detail.base = fresh.base; S.detail.title = fresh.title; S.detail.icon = fresh.icon; }
          // Ne pas reconstruire le buy panel pendant que l'utilisateur saisit
          // (montant / prix limite) : sinon le champ perd le focus et le texte.
          const buyBox = $("oc_buy");
          const typingInBuy = buyBox && buyBox.contains(document.activeElement) && document.activeElement.tagName === "INPUT";
          if (!typingInBuy) renderBuyPanel();
          // MAJ en place du panneau outcomes (préserve survol/zoom/recherche) ;
          // reconstruction complète seulement si la structure n'existe pas encore.
          if (!refreshOutcomesPanelValues()) renderOutcomesPanel();
        }
      }
    } catch (e) { /* garde l'état précédent */ }
  }
  async function loadBalance() {
    if (!ocIsConnected()) { S.balance = null; return; }
    try {
      S.balance = await apiGet("/api/hip4/usdc_balance?subaccount=" + encodeURIComponent(ocSelectedTA("hip4")), true);
      if (S.view === "detail") renderBuyPanel();
    } catch (_) {}
  }
  // Parts détenues (shares) pour un (outcome, side) donné. side = "yes"/"no" ou "YES"/"NO".
  function ocHeldQty(outcome, side) {
    if (!Array.isArray(S.positions) || outcome == null) return 0;
    const want = String(side).toUpperCase() === "NO" ? "NO" : "YES";
    const p = S.positions.find((x) => Number(x.outcome) === Number(outcome) && x.side === want);
    return p ? Number(p.qty) || 0 : 0;
  }
  function ocFmtShares(q) {
    const n = Number(q) || 0;
    return Number.isInteger(n) ? String(n) : n.toFixed(2);
  }
  async function loadPositions() {
    if (!ocIsConnected()) { S.positions = []; return; }
    try { const d = await apiGet("/api/hip4/positions?subaccount=" + encodeURIComponent(ocSelectedTA("hip4")), true); S.positions = (d && d.positions) || []; }
    catch (_) { return; }
    if (S.view === "detail" && S.buySide === "Sell") {
      // Rafraîchit le plafond/Max du panneau de vente sans casser la saisie en cours.
      const buyBox = $("oc_buy");
      const typing = buyBox && buyBox.contains(document.activeElement) && document.activeElement.tagName === "INPUT";
      if (!typing) renderBuyPanel();
    }
    if (S.btTab === "Positions" && $("oc_bt_body")) renderPositions();
  }
  async function loadBook() {
    if (S.view !== "detail" || !S.detail) return;
    try {
      const um = (S.meta && S.meta.underlying) ? S.meta.underlying : "";
      const u = um ? "&underlying=" + encodeURIComponent(um) : "";
      const b = await apiGet("/api/hip4/book?coin=" + encodeURIComponent(S.detail.yesCoin) + u, false);
      S.book = b;
      // Échantillon de courbe : prix sous-jacent si dispo (mode price), sinon
      // probabilité YES (mode prob). On attend que la meta soit chargée pour fixer le mode.
      if (S.meta) {
        const mode = um ? "price" : "prob";
        let v = null;
        if (mode === "price" && typeof b.underlying_px === "number" && b.underlying_px > 0) v = b.underlying_px;
        else if (mode === "prob" && typeof b.mid === "number" && b.mid > 0) v = b.mid;
        if (v != null) {
          // Si le mode change (ex. un échantillon proba pris avant que la meta
          // fixe le sous-jacent), on repart de zéro pour ne pas mélanger les échelles.
          if (S.chartMode && S.chartMode !== mode) { S.chartSamples = []; S.chartDisp = null; S.chartView = null; }
          S.chartMode = mode;
          if (mode === "price") S.curUnderlying = v;
          if (S.chartSamples.length === 0) { S.chartSamples = seedChartHistory(v, mode); S.chartDisp = v; S.chartView = null; }
          else { S.chartSamples.push({ t: Date.now(), v }); if (S.chartSamples.length > 600) S.chartSamples.shift(); }
        }
      }
      renderBook(); renderChart();
    } catch (_) {}
  }
  async function loadMeta(id) {
    try { S.meta = await apiGet("/api/hip4/outcome/" + encodeURIComponent(id), false); }
    catch (_) { S.meta = null; }
    if (S.detail && S.meta) {
      if (S.meta.label) { S.detail.title = humanizeLabel(S.meta.label); S.detail.icon = iconFor(S.meta.label); }
      if (typeof S.meta.mid_yes === "number") S.detail.yesPx = S.detail.yesPx || S.meta.mid_yes;
      if (typeof S.meta.mid_no === "number") S.detail.noPx = S.detail.noPx || S.meta.mid_no;
      if (S.meta.side_yes) S.detail.yesName = S.meta.side_yes;
      if (S.meta.side_no) S.detail.noName = S.meta.side_no;
    }
    if (S.view === "detail") { renderDetail(); loadBook(); }
  }

  // ── Lightfall background (React Bits, porté en WebGL natif, sans dépendance) ──
  const LF_MAX_COLORS = 8;
  function lfHexToRGB(hex) {
    const c = String(hex).replace("#", "").padEnd(6, "0");
    return [parseInt(c.slice(0, 2), 16) / 255, parseInt(c.slice(2, 4), 16) / 255, parseInt(c.slice(4, 6), 16) / 255];
  }
  function lfPrepColors(input) {
    const base = (input && input.length ? input : ["#A6C8FF", "#5227FF", "#FF9FFC"]).slice(0, LF_MAX_COLORS);
    const count = base.length;
    const arr = [];
    for (let i = 0; i < LF_MAX_COLORS; i++) arr.push(lfHexToRGB(base[Math.min(i, base.length - 1)]));
    const avg = [0, 0, 0];
    for (let i = 0; i < count; i++) { avg[0] += arr[i][0]; avg[1] += arr[i][1]; avg[2] += arr[i][2]; }
    avg[0] /= count; avg[1] /= count; avg[2] /= count;
    return { arr, count, avg };
  }
  const LF_VERT = "attribute vec2 position;attribute vec2 uv;varying vec2 vUv;void main(){vUv=uv;gl_Position=vec4(position,0.0,1.0);}";
  const LF_FRAG = `precision highp float;
uniform vec3 iResolution;uniform vec2 iMouse;uniform float iTime;
uniform vec3 uColor0;uniform vec3 uColor1;uniform vec3 uColor2;uniform vec3 uColor3;uniform vec3 uColor4;uniform vec3 uColor5;uniform vec3 uColor6;uniform vec3 uColor7;uniform int uColorCount;
uniform vec3 uBgColor;uniform vec3 uMouseColor;uniform float uSpeed;uniform int uStreakCount;uniform float uStreakWidth;uniform float uStreakLength;uniform float uGlow;uniform float uDensity;uniform float uTwinkle;uniform float uZoom;uniform float uBgGlow;uniform float uOpacity;uniform float uMouseEnabled;uniform float uMouseStrength;uniform float uMouseRadius;
varying vec2 vUv;
vec3 palette(float h){int count=uColorCount;if(count<1)count=1;int idx=int(floor(clamp(h,0.0,0.999999)*float(count)));if(idx<=0)return uColor0;if(idx==1)return uColor1;if(idx==2)return uColor2;if(idx==3)return uColor3;if(idx==4)return uColor4;if(idx==5)return uColor5;if(idx==6)return uColor6;return uColor7;}
vec3 tanhv(vec3 x){vec3 e=exp(-2.0*x);return (1.0-e)/(1.0+e);}
vec2 sceneC(vec2 frag,vec2 r){vec2 P=(frag+frag-r)/r.x;float z=0.0;float d=1e3;vec4 O=vec4(0.0);for(int k=0;k<39;k++){if(d<=1e-4)break;O=z*normalize(vec4(P,uZoom,0.0))-vec4(0.0,4.0,1.0,0.0)/4.5;d=1.0-sqrt(length(O*O));z+=d;}return vec2(O.x,atan(O.z,O.y));}
void mainImage(out vec4 o,vec2 C){vec2 r=iResolution.xy;vec2 uv0=(C+C-r)/r.x;float T=0.1*iTime*uSpeed+9.0;float angRings=max(1.0,floor(6.28318530718*max(uDensity,0.05)+0.5));vec2 Y=vec2(5e-3,6.28318530718/angRings);
vec2 c0=sceneC(C,r);vec2 cdx=sceneC(C+vec2(1.0,0.0),r);vec2 cdy=sceneC(C+vec2(0.0,1.0),r);vec2 dCx=cdx-c0;vec2 dCy=cdy-c0;dCx.y-=6.28318530718*floor(dCx.y/6.28318530718+0.5);dCy.y-=6.28318530718*floor(dCy.y/6.28318530718+0.5);vec2 fw=abs(dCx)+abs(dCy);C=c0;
vec2 P=vec2(2.0,1.0)*uv0-(r/r.x)*vec2(0.0,1.0);vec4 O=vec4(uBgColor*90.0*uBgGlow/(1e3*dot(P,P)+6.0),0.0);
float mGlow=0.0;if(uMouseEnabled>0.5){vec2 mN=(iMouse+iMouse-r)/r.x;float md=length(uv0-mN);mGlow=exp(-md*md/max(uMouseRadius*uMouseRadius,1e-4))*uMouseStrength;O.rgb+=uMouseColor*mGlow*0.25;}
float zr=5e-4*uStreakWidth;vec2 rr=vec2(max(length(fw),1e-5));float tail=19.0/max(uStreakLength,0.05);
for(int m=0;m<16;m++){if(m>=uStreakCount)break;float jf=float(m)+1.0;float ic=fract(sin(dot(vec2(jf,floor(C.x/Y.x+0.5)),vec2(7.0,11.0))*73.0));vec2 Pp=C-(T+T*ic)*vec2(0.0,1.0);Pp-=floor(Pp/Y+0.5)*Y;float h=fract(8663.0*ic);vec3 col=palette(h);float weight=mix(1.5,1.0+sin(T+7.0*h+4.0),uTwinkle);weight*=(1.0+mGlow*2.0);vec2 inner=vec2(length(max(Pp,vec2(-1.0,0.0))),length(Pp)-zr)-zr;vec2 sm=vec2(1.0)-smoothstep(-rr,rr,inner);O.rgb+=dot(sm,vec2(exp(tail*Pp.y),3.0))*col*weight;C.x+=Y.x/8.0;}
vec3 colr=sqrt(tanhv(max(O.rgb*uGlow-vec3(0.04,0.08,0.02),0.0)));o=vec4(colr,uOpacity);}
void main(){vec4 color;mainImage(color,vUv*iResolution.xy);gl_FragColor=color;}`;

  function ocLightfall(container, opts) {
    const o = Object.assign({
      colors: ["#d36112", "#0bd116", "#F97316"], backgroundColor: "#000000",
      speed: 0.3, streakCount: 3, streakWidth: 1, streakLength: 1, glow: 1,
      density: 1, twinkle: 1, zoom: 2, backgroundGlow: 1, opacity: 1,
      mouseInteraction: true, mouseStrength: 1, mouseRadius: 0.6, mouseDampening: 0.15,
      dpr: Math.min(2, window.devicePixelRatio || 1),
    }, opts || {});
    const canvas = document.createElement("canvas");
    canvas.style.cssText = "width:100%;height:100%;display:block";
    container.appendChild(canvas);
    const gl = canvas.getContext("webgl", { alpha: true, antialias: true, premultipliedAlpha: false });
    if (!gl) { return { pause() {}, resume() {}, destroy() { if (canvas.parentNode) canvas.parentNode.removeChild(canvas); } }; }

    function compile(type, src) {
      const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) console.error("[Lightfall] shader", gl.getShaderInfoLog(s));
      return s;
    }
    const prog = gl.createProgram();
    gl.attachShader(prog, compile(gl.VERTEX_SHADER, LF_VERT));
    gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, LF_FRAG));
    gl.linkProgram(prog); gl.useProgram(prog);

    const posLoc = gl.getAttribLocation(prog, "position");
    const uvLoc = gl.getAttribLocation(prog, "uv");
    const posBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, posBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(posLoc); gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);
    const uvBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, uvBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 2, 0, 0, 2]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(uvLoc); gl.vertexAttribPointer(uvLoc, 2, gl.FLOAT, false, 0, 0);

    const U = (n) => gl.getUniformLocation(prog, n);
    const { arr, count, avg } = lfPrepColors(o.colors);
    for (let i = 0; i < 8; i++) gl.uniform3f(U("uColor" + i), arr[i][0], arr[i][1], arr[i][2]);
    gl.uniform1i(U("uColorCount"), count);
    const bg = lfHexToRGB(o.backgroundColor); gl.uniform3f(U("uBgColor"), bg[0], bg[1], bg[2]);
    gl.uniform3f(U("uMouseColor"), avg[0], avg[1], avg[2]);
    gl.uniform1f(U("uSpeed"), o.speed);
    gl.uniform1i(U("uStreakCount"), Math.max(1, Math.min(16, Math.round(o.streakCount))));
    gl.uniform1f(U("uStreakWidth"), o.streakWidth);
    gl.uniform1f(U("uStreakLength"), o.streakLength);
    gl.uniform1f(U("uGlow"), o.glow);
    gl.uniform1f(U("uDensity"), o.density);
    gl.uniform1f(U("uTwinkle"), o.twinkle);
    gl.uniform1f(U("uZoom"), o.zoom);
    gl.uniform1f(U("uBgGlow"), o.backgroundGlow);
    gl.uniform1f(U("uOpacity"), o.opacity);
    gl.uniform1f(U("uMouseEnabled"), o.mouseInteraction ? 1 : 0);
    gl.uniform1f(U("uMouseStrength"), o.mouseStrength);
    gl.uniform1f(U("uMouseRadius"), o.mouseRadius);
    const uRes = U("iResolution"), uMouse = U("iMouse"), uTime = U("iTime");

    function resize() {
      const r = container.getBoundingClientRect();
      const W = Math.max(1, Math.floor(r.width * o.dpr)), H = Math.max(1, Math.floor(r.height * o.dpr));
      canvas.width = W; canvas.height = H;
      gl.viewport(0, 0, W, H);
      gl.uniform3f(uRes, W, H, 1);
    }
    resize();
    const ro = new ResizeObserver(resize); ro.observe(container);

    const target = [0, 0], cur = [0, 0];
    function onMove(e) {
      const r = canvas.getBoundingClientRect();
      target[0] = (e.clientX - r.left) * o.dpr;
      target[1] = (r.height - (e.clientY - r.top)) * o.dpr;
      if (o.mouseDampening <= 0) { cur[0] = target[0]; cur[1] = target[1]; }
    }
    if (o.mouseInteraction) window.addEventListener("pointermove", onMove, { passive: true });

    let raf = null, paused = false, last = 0;
    function loop(t) {
      raf = requestAnimationFrame(loop);
      if (paused) return;
      gl.uniform1f(uTime, t * 0.001);
      if (o.mouseDampening > 0) {
        if (!last) last = t;
        const dt = (t - last) / 1000; last = t;
        const tau = Math.max(1e-4, o.mouseDampening);
        let f = 1 - Math.exp(-dt / tau); if (f > 1) f = 1;
        cur[0] += (target[0] - cur[0]) * f; cur[1] += (target[1] - cur[1]) * f;
      } else { last = t; }
      gl.uniform2f(uMouse, cur[0], cur[1]);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    raf = requestAnimationFrame(loop);

    return {
      pause() { paused = true; },
      resume() { paused = false; last = 0; },
      destroy() {
        if (raf) cancelAnimationFrame(raf);
        ro.disconnect();
        if (o.mouseInteraction) window.removeEventListener("pointermove", onMove);
        const ext = gl.getExtension("WEBGL_lose_context"); if (ext) ext.loseContext();
        if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
      },
    };
  }

  function showLightfall() {
    const host = $("oc_bg"); if (!host) return;
    host.style.display = "";
    if (!S.lf) { try { S.lf = ocLightfall(host, {}); } catch (e) { S.lf = null; } }
    else S.lf.resume();
    window.__ocLF = S.lf;
  }
  function hideLightfall() { const host = $("oc_bg"); if (host) host.style.display = "none"; if (S.lf) S.lf.pause(); }
  function destroyLightfall() { if (S.lf) { try { S.lf.destroy(); } catch (_) {} S.lf = null; } const host = $("oc_bg"); if (host) host.style.display = "none"; }

  // ── API publique (appelée par applyRoute) ─────────────────────────────────
  function onEnter(id) {
    const listV = $("outcomes_list_view"), detailV = $("outcomes_detail_view");
    stopTimers();
    if (id) {
      S.view = "detail";
      if (listV) listV.classList.add("hidden");
      if (detailV) detailV.classList.remove("hidden");
      S.detail = S.markets.find((x) => x.id === String(id)) || { id: String(id), title: "Outcome #" + id, icon: iconFor(""), kind: "binary", yesName: "YES", noName: "NO", yesPx: 0, noPx: 0, yesCoin: "#" + (Number(id) * 10), noCoin: "#" + (Number(id) * 10 + 1) };
      S.book = null; S.chartSamples = []; S.chartDisp = null; S.chartView = null; S.chartMode = null; S.curUnderlying = null; S.side = "yes"; S.amt = 0; S.buySide = "Buy"; S.orderType = "market"; S.limitC = null; S.meta = null;
      // Intent de vente différé (clic « Sell » depuis l'onglet Positions sur un autre marché).
      if (S.pendingSell && S.detail && Number(S.detail.outcome) === Number(S.pendingSell.outcome)) {
        S.buySide = "Sell"; S.side = S.pendingSell.side === "no" ? "no" : "yes";
        S.amt = ocHeldQty(S.detail.outcome, S.pendingSell.side);
      }
      S.pendingSell = null;
      window.scrollTo(0, 0);
      hideLightfall();
      renderDetail();
      loadMeta(id);
      if (!S.markets.length) loadMarkets();
      loadBalance();
      loadPositions();
      loadBook();
      S.tBook = setInterval(loadBook, 1800);
      S.tMarkets = setInterval(loadMarkets, 5000);
    } else {
      S.view = "list";
      if (detailV) detailV.classList.add("hidden");
      if (listV) listV.classList.remove("hidden");
      showLightfall();
      renderList();
      loadMarkets();
      S.tMarkets = setInterval(loadMarkets, 5000);
    }
  }
  function onLeave() { stopTimers(); destroyLightfall(); }
  function stopTimers() {
    stopCountdown();
    stopChartRAF();
    if (S.tBook) { clearInterval(S.tBook); S.tBook = null; }
    if (S.tMarkets) { clearInterval(S.tMarkets); S.tMarkets = null; }
  }

  // Cohérence : monter la section dans .app pour que le header global Tradoor
  // (sticky, pleine largeur, z-index 20) reste au-dessus et que le contenu
  // s'aligne sur la largeur du site. #oc_bg (fixed) reste plein écran.
  (function relocateIntoApp() {
    const app = document.querySelector(".app");
    const sec = $("outcomes_page");
    if (app && sec && sec.parentElement !== app) app.appendChild(sec);
  })();

  window.OUTCOMES = { onEnter, onLeave };
})();
