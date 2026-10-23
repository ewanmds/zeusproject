/* ============================================================================
   delta.js - Section Delta (/delta) pour Tradoor
   Jambe long sur une plateforme, jambe short sur l'autre, reverse sur minuterie.
   Module exposé en window.DELTA { onEnter(), onLeave() }, appelé par applyRoute.
   Données : GET /api/delta/markets, /api/delta/account, /api/delta/status,
   /api/delta/history ; POST /api/delta/start, /api/delta/stop,
   /api/delta/reverse_now. Helpers globaux d'app.js : apiGet, apiPost,
   _fmtPrice, _tokenIconUrl, openWalletsDrawer.
   Écran Setup (#delta_setup) et écran de suivi (#delta_running), historique
   des sessions (#delta_history) sous les deux.
   ============================================================================ */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const MINUS = "−";

  const VENUES = [
    { id: "hyperliquid", name: "Hyperliquid", logo: "/web/hlgreen.png", color: "#35f0c2", tag: "Connected" },
    { id: "extended", name: "Extended", logo: "/web/ext.png", color: "#34eda4", tag: "Beta · connected" },
    { id: "risex", name: "RISEx", logo: "/web/risex.svg", color: "#03de82", tag: "Beta · connected" },
    // QFEX : marchés TradFi uniquement (actions, indices, matières premières) ; une seule
    // connexion de trading par compte, donc pas en même temps qu'un market maker QFEX.
    { id: "qfex", name: "QFEX", logo: "/web/qfex.svg", color: "#38a5a0", tag: "Beta · connected" },
    { id: "variational", name: "Variational", ini: "VA", color: "#a78bfa", soon: true },
    { id: "more", name: "More DEXs", ini: "+", color: "#8a7d72", soon: true },
  ];
  const VENUE_NAME = { hyperliquid: "Hyperliquid", extended: "Extended", risex: "RISEx", qfex: "QFEX" };
  // Sources HL de /api/delta/markets : perps principaux + dex HIP-3 de HIP3_SOURCES (app.js).
  const HIP3 = Object.values(typeof HIP3_SOURCES === "object" && HIP3_SOURCES ? HIP3_SOURCES : {});
  const HL_SOURCES = ["perps", ...HIP3.map((e) => e.delta)];
  const SOURCE_LABEL = { perps: "Perps", ...Object.fromEntries(HIP3.map((e) => [e.delta, e.label])) };
  // Dex on-chain d'un symbole HL ("io:SNDK" -> "io", "BTC" -> "").
  const hlDex = (sym) => (typeof hip3DexOfSymbol === "function" ? hip3DexOfSymbol(sym) : "");
  const MAX_LEV = 40; // borne serveur (delta_config)
  const STATUS_POLL_MS = 5000;
  const ACCOUNT_POLL_MS = 15000;

  // ── État du formulaire (défauts de la maquette v3) ─────────────────────────
  const S = {
    long: { venue: "hyperliquid", symbol: "BTC", leverage: 5 },
    short: { venue: "extended", symbol: "BTC-USD", leverage: 5 },
    notional: 5000,
    sameToken: true,
    mode: "limit_then_market",
    limitLeg: "auto",
    chase: 3,
    chaseTimeout: 45,
    interval: 30,
    // Options supplémentaires désactivées par défaut : l'utilisateur les active, et ses
    // réglages sont ensuite mémorisés (par wallet, voir formKey).
    jitterOn: false,
    jitter: 20,
    stops: {
      max_reverses: { on: false, v: 40 },
      volume_usd: { on: false, v: 1000000 },
      duration_h: { on: false, v: 24 },
      max_loss_usd: { on: false, v: 150 },
    },
    loopOn: false,
    guardOn: false,
    guardClose: 25,
    guardAlert: 40,
    telegramOn: false,
  };

  let _account = null;        // { hyperliquid: {...}, extended: {...} }
  let _prefilled = false;     // config du serveur appliquée une fois
  let _touched = false;       // l'utilisateur a modifié le formulaire
  let _running = false;
  let _starting = false;
  let _active = false;
  let _statusTimer = null;
  let _accountTimer = null;
  let _bound = false;
  let _lockMsg = "";         // explication quand le verrou même token a dû être retiré
  const _markets = {};        // "venue|source" -> { rows, promise }

  // ── Mémorisation du formulaire (navigateur) ────────────────────────────────
  // Chaque réglage modifié est gardé, par wallet : à la visite suivante, le formulaire
  // reprend tel quel (prioritaire sur la config de la dernière session lue sur le serveur).
  // Un autre wallet sur le même navigateur a son propre formulaire.
  const LEGACY_FORM_KEY = "delta_form_v1";   // ancienne clé commune à tous les wallets
  const FORM_DEFAULTS = JSON.stringify(S);
  const formKey = (acct) => LEGACY_FORM_KEY + ":" + acct;
  let _formAccount = null;   // wallet dont le formulaire est chargé ("" = aucun wallet)
  let _formRestored = false;
  function mergeSaved(dst, src) {
    for (const k of Object.keys(dst)) {
      if (!(k in src)) continue;
      const d = dst[k], v = src[k];
      if (d && typeof d === "object" && !Array.isArray(d)) {
        if (v && typeof v === "object") mergeSaved(d, v);
      } else if (typeof v === typeof d) {
        dst[k] = v;
      }
    }
  }
  function viewerAccount() {
    try {
      if (typeof walletAuthAccount !== "undefined" && walletAuthAccount) return String(walletAuthAccount).toLowerCase();
    } catch (_) {}
    return "";
  }
  function restoreForm(acct) {
    try {
      let raw = localStorage.getItem(formKey(acct));
      if (!raw) {
        // Migration : l'ancienne clé commune revient au premier wallet connecté, puis disparaît.
        raw = localStorage.getItem(LEGACY_FORM_KEY);
        if (raw) {
          localStorage.setItem(formKey(acct), raw);
          localStorage.removeItem(LEGACY_FORM_KEY);
        }
      }
      if (!raw) return false;
      mergeSaved(S, JSON.parse(raw));
      return true;
    } catch (_) {
      return false;
    }
  }
  function saveForm() {
    if (!_formAccount) return;               // pas de wallet : rien n'est mémorisé
    try { localStorage.setItem(formKey(_formAccount), JSON.stringify(S)); } catch (_) {}
  }
  // Charge le formulaire du wallet connecté quand il change. Réglages modifiés avant la
  // première connexion : gardés et mémorisés pour ce wallet.
  function syncFormAccount() {
    const acct = viewerAccount();
    if (acct === _formAccount) return;
    const prev = _formAccount;
    _formAccount = acct;
    if (!acct) return;                       // déconnexion : l'affichage reste tel quel
    if (!prev && _touched) { saveForm(); return; }
    if (prev) {
      // Changement de wallet : défauts, puis config serveur ou réglages de ce wallet.
      mergeSaved(S, JSON.parse(FORM_DEFAULTS));
      _touched = false;
      _prefilled = false;
      _lockMsg = "";
    }
    _formRestored = restoreForm(acct);
    if (_formRestored) ensureLegMarkets();      // venues / tokens de ce wallet
  }

  // ── Formatage (en-US) ──────────────────────────────────────────────────────
  const nf = (n, d = 0) => Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  // Montant exact, même règle que le serveur (_delta_usd) : $1,000 ou $820.50.
  function usd(v) {
    if (v == null || !Number.isFinite(Number(v))) return "-";
    const x = Number(v);
    const whole = Math.abs(x - Math.round(x)) < 0.005;
    return (x < 0 ? MINUS : "") + "$" + nf(Math.abs(x), whole ? 0 : 2);
  }
  // Compact : $5,000 / $243k / $1.2M / $2.14B
  function usdc(v) {
    if (v == null || !Number.isFinite(Number(v))) return "-";
    const a = Math.abs(Number(v));
    const sign = Number(v) < 0 ? MINUS : "";
    const trim = (n, d) => nf(n, d).replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1");
    if (a >= 1e9) return sign + "$" + trim(a / 1e9, 2) + "B";
    if (a >= 1e6) return sign + "$" + trim(a / 1e6, 2) + "M";
    if (a >= 1e4) return sign + "$" + nf(a / 1e3, 0) + "k";
    return sign + "$" + nf(a, 0);
  }
  function dur(min) {
    const m = Math.round(Number(min) || 0);
    if (m < 60) return m + " min";
    const h = Math.floor(m / 60);
    const r = m % 60;
    if (h >= 48) return nf(m / 1440, 1).replace(/\.0$/, "") + " d";
    return h + " h" + (r ? " " + String(r).padStart(2, "0") : "");
  }
  const fmtPrice = (v) => (typeof _fmtPrice === "function" ? _fmtPrice(v) : usd(v));
  const underlying = (sym) => {
    let s = String(sym || "").trim();
    if (s.includes(":")) s = s.split(":")[1];
    return s.toUpperCase().replace(/(-(USDC|USD|PERP)|\/USDC)$/, "");
  };
  const hlSource = (sym) => {
    const dex = hlDex(sym);
    return (dex && HIP3.find((e) => e.dex === dex)?.delta) || "perps";
  };
  const sourceOf = (leg) => (leg.venue === "hyperliquid" ? hlSource(leg.symbol) : "perps");
  const other = (side) => (side === "long" ? "short" : "long");
  const liqPct = (lev) => Math.max(0, (1 / lev - 0.005) * 100);

  // ── Auth / réseau (helpers d'app.js) ───────────────────────────────────────
  function isWalletConnected() {
    try {
      if (typeof walletAuthToken !== "undefined" && walletAuthToken) return true;
      if (typeof walletAuthTokens !== "undefined" && walletAuthTokens && Object.keys(walletAuthTokens).length) return true;
    } catch (_) {}
    return false;
  }

  function marketKey(venue, source) { return venue + "|" + (venue === "hyperliquid" ? source : ""); }

  function loadMarkets(venue, source) {
    const key = marketKey(venue, source);
    const cur = _markets[key];
    if (cur && (cur.rows || cur.promise)) return cur.promise || Promise.resolve(cur.rows);
    const url = venue === "hyperliquid"
      ? `/api/delta/markets?venue=hyperliquid&source=${encodeURIComponent(source)}`
      : `/api/delta/markets?venue=${encodeURIComponent(venue)}`;
    const entry = { rows: null, promise: null };
    entry.promise = apiGet(url)
      .then((d) => { entry.rows = Array.isArray(d?.markets) ? d.markets : []; entry.promise = null; return entry.rows; })
      .catch(() => { delete _markets[key]; return []; });
    _markets[key] = entry;
    return entry.promise;
  }
  function marketsCached(venue, source) { return _markets[marketKey(venue, source)]?.rows || null; }
  function marketOf(leg) {
    const rows = marketsCached(leg.venue, sourceOf(leg));
    return rows ? rows.find((r) => r.symbol === leg.symbol) || null : null;
  }
  function sourcesOf(venue) { return venue === "hyperliquid" ? HL_SOURCES : ["perps"]; }
  async function allRows(venue) {
    const lists = await Promise.all(sourcesOf(venue).map((s) => loadMarkets(venue, s)));
    return [].concat(...lists);
  }
  function underlyingsCached(venue) {
    const set = new Set();
    let complete = true;
    for (const s of sourcesOf(venue)) {
      const rows = marketsCached(venue, s);
      if (!rows) { complete = false; continue; }
      rows.forEach((r) => set.add(r.underlying || underlying(r.symbol)));
    }
    return complete ? set : null;
  }
  async function findByUnderlying(venue, u) {
    for (const s of sourcesOf(venue)) {
      const rows = await loadMarkets(venue, s);
      const hit = rows.find((r) => (r.underlying || underlying(r.symbol)) === u);
      if (hit) return hit;
    }
    return null;
  }

  // ── Leg cards ──────────────────────────────────────────────────────────────
  function legHtml(side) {
    const items = VENUES.map((v) => {
      const logo = v.logo ? `<img src="${v.logo}" alt="">` : esc(v.ini);
      return `<button type="button" class="dl-venue-chip" role="option" style="--vc:${v.color}" data-venue="${v.id}"${v.soon ? " disabled" : ""}>`
        + `<span class="dl-venue-logo">${logo}</span>`
        + `<span class="dl-venue-txt"><span class="dl-venue-name">${esc(v.name)}</span><span class="dl-venue-tag">${v.soon ? "Soon" : ""}</span></span></button>`;
    }).join("");
    const label = side === "long" ? "Long" : "Short";
    return `<div class="hv-card-head">
        <span class="dl-pill dl-pill--${side}">${label}</span>
      </div>
      <div class="dl-field"><span class="dl-lbl">Exchange</span>
        <div class="dl-dd">
          <button type="button" class="dl-venue-sel" data-pop-btn="venue" aria-haspopup="listbox" aria-expanded="false" aria-label="${label} exchange">
            <span class="dl-venue-logo" data-venue-logo></span>
            <span class="dl-venue-txt"><span class="dl-venue-name" data-o="selName"></span><span class="dl-venue-tag" data-o="selTag"></span></span>
            <span class="dl-chev" aria-hidden="true">&#9662;</span>
          </button>
          <div class="dl-menu" data-pop="venue" role="listbox" hidden>${items}</div>
        </div>
      </div>
      <div class="dl-field"><span class="dl-lbl">Token</span>
        <div class="dl-token-row">
          <button type="button" class="dl-token-btn" data-token-btn>
            <span class="dl-token-name" data-o="tokName">-</span><span class="dl-token-meta" data-o="tokMeta"></span><span class="dl-chev" aria-hidden="true">&#9662;</span>
          </button>
          <div class="dl-dd">
            <button type="button" class="dl-lev-btn" data-pop-btn="lev" aria-haspopup="dialog" aria-expanded="false" aria-label="${label} leverage" title="Leverage"><span data-o="levTxt"></span></button>
            <div class="dl-pop" data-pop="lev" hidden>
              <div class="dl-row"><span class="dl-lbl">Leverage</span><span class="dl-val"><span data-o="levPop"></span> <span class="dl-lev-max" data-o="levMax"></span></span></div>
              <input type="range" class="hv-slider" data-lev min="1" max="${MAX_LEV}" step="1" value="5" aria-label="${label} leverage" />
            </div>
          </div>
        </div>
      </div>
      <div class="dl-leg-tiles">
        <div><span class="dl-tile-lbl">Mark</span><span class="dl-tile-val" data-o="mark">-</span></div>
        <div><span class="dl-tile-lbl">Margin</span><span class="dl-tile-val" data-o="margin">-</span></div>
        <div><span class="dl-tile-lbl">Liq. distance</span><span class="dl-tile-val" data-o="liq">-</span></div>
        <div data-avail><span class="dl-tile-lbl">Available</span><span class="dl-tile-val" data-o="avail">-</span></div>
      </div>`;
  }

  // Menus des cartes jambe (exchange, levier) : un seul ouvert à la fois.
  function closePops(except) {
    document.querySelectorAll(".dl-leg [data-pop]").forEach((p) => {
      if (p === except) return;
      p.hidden = true;
      const btn = p.parentElement?.querySelector("[data-pop-btn]");
      if (btn) btn.setAttribute("aria-expanded", "false");
      p.closest(".dl-leg")?.classList.remove("has-pop");
    });
  }
  function togglePop(root, name) {
    const pop = root.querySelector(`[data-pop="${name}"]`);
    if (!pop) return;
    const open = pop.hidden;
    closePops(open ? pop : null);
    pop.hidden = !open;
    root.querySelector(`[data-pop-btn="${name}"]`)?.setAttribute("aria-expanded", open ? "true" : "false");
    root.classList.toggle("has-pop", open);
  }

  const sameVenue = () => S.long.venue === S.short.venue;
  // Réserve de marge d'une jambe : même plateforme et, sur HL, même dex (xyz:, mkts:, io:).
  const poolOf = (leg) => leg.venue + "|" + (leg.venue === "hyperliquid" ? hlDex(leg.symbol) : "");
  const sharedPool = () => poolOf(S.long) === poolOf(S.short);
  // Marge demandée sur la réserve de `side` (les deux jambes si elles la partagent).
  function poolNeed(side) {
    const N = Number(S.notional) || 0;
    return sharedPool() ? N / S.long.leverage + N / S.short.leverage : N / S[side].leverage;
  }

  function legEl(side) { return $(side === "long" ? "delta_leg_long" : "delta_leg_short"); }
  function setOut(root, key, text) { const el = root.querySelector(`[data-o="${key}"]`); if (el && el.textContent !== text) el.textContent = text; }

  function venueStatus(id) {
    const v = VENUES.find((x) => x.id === id);
    if (!v || v.soon) return "Soon";
    const a = _account && _account[id];
    return a && a.connected ? v.tag : "Not connected";
  }

  function maxLevOf(leg) {
    const m = marketOf(leg);
    const ml = Number(m?.max_leverage) || MAX_LEV;
    return Math.max(1, Math.min(MAX_LEV, ml));
  }

  function renderLeg(side) {
    const root = legEl(side);
    if (!root) return;
    const leg = S[side];
    const N = Number(S.notional) || 0;
    const m = marketOf(leg);
    root.querySelectorAll(".dl-venue-chip").forEach((chip) => {
      const id = chip.dataset.venue;
      const on = id === leg.venue;
      chip.classList.toggle("is-on", on);
      const tag = chip.querySelector(".dl-venue-tag");
      const text = venueStatus(id);
      if (tag && tag.textContent !== text) tag.textContent = text;
      chip.classList.toggle("is-off", text === "Not connected");
    });
    const cur = VENUES.find((v) => v.id === leg.venue);
    const sel = root.querySelector("[data-pop-btn=venue]");
    if (sel && cur) {
      sel.style.setProperty("--vc", cur.color);
      const st = venueStatus(cur.id);
      sel.classList.toggle("is-off", st === "Not connected");
      const logo = root.querySelector("[data-venue-logo]");
      if (logo && logo.dataset.v !== cur.id) {
        logo.dataset.v = cur.id;
        logo.innerHTML = cur.logo ? `<img src="${cur.logo}" alt="">` : esc(cur.ini);
      }
      setOut(root, "selName", cur.name);
      setOut(root, "selTag", st);
    }
    setOut(root, "tokName", m ? m.display || m.symbol : underlying(leg.symbol) || "-");
    const mx = maxLevOf(leg);
    // Marché HIP-3 en marge isolée uniquement (only_isolated de /api/delta/markets).
    setOut(root, "tokMeta", SOURCE_LABEL[sourceOf(leg)] + (m && m.only_isolated ? " · isolated" : ""));
    if (leg.leverage > mx) leg.leverage = mx;
    const slider = root.querySelector("[data-lev]");
    if (slider) {
      slider.max = String(mx);
      if (document.activeElement !== slider) slider.value = String(leg.leverage);
      const pct = mx > 1 ? ((leg.leverage - 1) / (mx - 1)) * 100 : 100;
      slider.style.setProperty("--fill", pct.toFixed(2) + "%");
    }
    setOut(root, "levTxt", leg.leverage + "x");
    setOut(root, "levPop", leg.leverage + "x");
    setOut(root, "levMax", `/ max ${mx}x`);
    setOut(root, "mark", m && m.price ? fmtPrice(Number(m.price)) : "-");
    const need = N / leg.leverage;
    setOut(root, "margin", N > 0 ? usd(need) : "-");
    const lq = liqPct(leg.leverage);
    setOut(root, "liq", (side === "long" ? MINUS : "+") + nf(lq, 1) + "%");
    const acc = _account && _account[leg.venue];
    const have = acc && acc.connected && acc.available_margin != null ? Number(acc.available_margin) : null;
    setOut(root, "avail", have == null ? "-" : usd(have));
    root.querySelector("[data-avail]")?.classList.toggle("is-short", have != null && poolNeed(side) > have);
  }

  // ── Projection, notes et validation ────────────────────────────────────────
  function projection() {
    const N = Number(S.notional) || 0;
    const per = 4 * N;
    if (!S.loopOn) return { per, perDay: 0, rev: 0, total: 2 * N };   // ouverture seule, pas de reverse
    const perDay = S.interval > 0 ? (per * 1440) / S.interval : 0;
    const cands = [];
    const st = S.stops;
    if (st.max_reverses.on && st.max_reverses.v > 0) cands.push(Math.floor(st.max_reverses.v));
    if (st.volume_usd.on && st.volume_usd.v > 0 && per > 0) cands.push(Math.max(0, Math.ceil((st.volume_usd.v - 2 * N) / per)));
    if (st.duration_h.on && st.duration_h.v > 0) cands.push(Math.max(0, Math.floor((st.duration_h.v * 60) / S.interval)));
    const rev = cands.length ? Math.min(...cands) : null;
    return { per, perDay, rev, total: rev == null ? null : 2 * N + per * rev };
  }

  function blockingMessage() {
    if (sameVenue() && underlying(S.long.symbol) === underlying(S.short.symbol)) return "On the same exchange, pick two different tokens.";
    for (const side of ["long", "short"]) {
      const v = S[side].venue;
      if (!(_account && _account[v] && _account[v].connected)) return `Connect ${VENUE_NAME[v]} first.`;
    }
    const N = Number(S.notional) || 0;
    const minN = minNotional();
    if (!(N >= minN)) return `Minimum size is ${usd(minN)} per leg.`;
    for (const side of sharedPool() ? ["long"] : ["long", "short"]) {
      const leg = S[side];
      const acc = _account[leg.venue];
      if (acc.available_margin == null) continue;
      const need = poolNeed(side);
      const have = Number(acc.available_margin);
      const both = sharedPool() ? " for both legs" : "";
      if (need > have) return `Not enough margin on ${VENUE_NAME[leg.venue]}${both}: need ${usd(need)}, have ${usd(have)}.`;
    }
    return "";
  }

  function minNotional() {
    let mn = 20;
    for (const side of ["long", "short"]) {
      const m = marketOf(S[side]);
      if (m && Number(m.min_notional_usd) > mn) mn = Number(m.min_notional_usd);
    }
    return mn;
  }

  // Frais maker du marché choisi s'il les porte (QFEX : barème par catégorie, comme le
  // bot), sinon ceux du compte.
  function makerFee(side) {
    const m = marketOf(S[side]);
    return m && m.maker_fee != null ? m.maker_fee : _account?.[S[side].venue]?.maker_fee;
  }

  function autoLegNote() {
    const fl = makerFee("long");
    const fs = makerFee("short");
    if (fl == null || fs == null) return "Auto rests the limit on the leg with the lowest maker fee.";
    const side = Number(fs) < Number(fl) ? "short" : "long";
    const fee = side === "short" ? fs : fl;
    return `Auto rests the limit on ${VENUE_NAME[S[side].venue]} (maker ${nf(Number(fee) * 100, 3)}%).`;
  }

  function setToggle(btn, on) {
    if (!btn) return;
    btn.classList.toggle("is-on", !!on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }
  function setVal(id, v) {
    const el = $(id);
    if (el && document.activeElement !== el && String(el.value) !== String(v)) el.value = v;
  }
  function setFill(el) {
    if (!el) return;
    const min = Number(el.min), max = Number(el.max), v = Number(el.value);
    el.style.setProperty("--fill", (max > min ? ((v - min) / (max - min)) * 100 : 0).toFixed(2) + "%");
  }
  function setText(id, t) { const el = $(id); if (el && el.textContent !== t) el.textContent = t; }

  function render() {
    syncFormAccount();                 // la sauvegarde se fait dans touch(), pas à chaque rendu
    renderLeg("long");
    renderLeg("short");
    const sameU = underlying(S.long.symbol) === underlying(S.short.symbol);
    setText("delta_spine_note", sameU ? "Same token" : "Different tokens");
    $("delta_setup")?.querySelector(".dl-spine")?.classList.toggle("is-warn", !sameU);

    // Size
    setVal("delta_notional", S.notional);
    setText("delta_size_hint", "min " + usd(minNotional()));
    setToggle($("delta_same_token"), S.sameToken);
    $("delta_same_token")?.classList.toggle("is-disabled", sameVenue());
    setText("delta_same_token_note", _lockMsg || (sameVenue()
      ? "Both legs on one exchange: pick a different token on each leg."
      : !sameU
      ? "Different tokens: the position is no longer strictly delta-neutral."
      : S.sameToken ? "Picking a token on one leg sets it on the other." : ""));
    $("delta_same_token_note")?.classList.toggle("is-warn", !!_lockMsg || (!sameU && !sameVenue()));

    // Reverse loop (contenu visible seulement si activé)
    setToggle($("delta_loop_on"), S.loopOn);
    $("delta_loop_card")?.classList.toggle("is-off", !S.loopOn);
    setVal("delta_interval", S.interval); setFill($("delta_interval"));
    setText("delta_interval_txt", dur(S.interval));
    setToggle($("delta_jitter_on"), S.jitterOn);
    const js = $("delta_jitter"); if (js) js.disabled = !S.jitterOn;
    setVal("delta_jitter", S.jitter); setFill($("delta_jitter"));
    setText("delta_jitter_txt", S.jitterOn ? `${dur(S.interval * (1 - S.jitter / 100))} to ${dur(S.interval * (1 + S.jitter / 100))}` : "Off");

    // Execution
    document.querySelectorAll("#delta_page [data-exec]").forEach((b) => b.classList.toggle("is-active", b.dataset.exec === S.mode));
    const lo = $("delta_limit_opts"); if (lo) lo.hidden = S.mode !== "limit_then_market";
    document.querySelectorAll("#delta_page [data-limit-leg]").forEach((b) => {
      const on = b.dataset.limitLeg === S.limitLeg;
      b.classList.toggle("is-on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    setText("delta_auto_note", S.limitLeg === "auto" ? autoLegNote() : "");
    setVal("delta_chase", S.chase);
    setVal("delta_chase_timeout", S.chaseTimeout);

    // Notifications Telegram
    setToggle($("delta_telegram_on"), S.telegramOn);
    $("delta_telegram_card")?.classList.toggle("is-off", !S.telegramOn);

    // Guard (contenu visible seulement si activé)
    setToggle($("delta_guard_on"), S.guardOn);
    $("delta_guard_card")?.classList.toggle("is-off", !S.guardOn);
    setVal("delta_guard_close", S.guardClose); setFill($("delta_guard_close"));
    setVal("delta_guard_alert", S.guardAlert); setFill($("delta_guard_alert"));
    setText("delta_guard_close_txt", S.guardClose + "%");
    setText("delta_guard_alert_txt", S.guardAlert + "% left");
    const lmin = Math.min(liqPct(S.long.leverage), liqPct(S.short.leverage));
    setText("delta_guard_note", `Measured against the starting distance: here, a price move of about ±${nf(lmin * (1 - S.guardClose / 100), 1)}% from entry.`);

    // Stops
    for (const [k, st] of Object.entries(S.stops)) {
      setToggle(document.querySelector(`#delta_page [data-stop-toggle="${k}"]`), st.on);
      document.querySelector(`#delta_page .dl-stop[data-stop="${k}"]`)?.classList.toggle("is-off", !st.on);
      const inp = document.querySelector(`#delta_page [data-stop-input="${k}"]`);
      if (inp) {
        inp.disabled = !st.on;
        if (document.activeElement !== inp && String(inp.value) !== String(st.v)) inp.value = st.v;
      }
    }

    // Projection
    const N = Number(S.notional) || 0;
    const p = projection();
    setText("delta_proj_rev", S.loopOn ? usdc(p.per) : "-");
    setText("delta_proj_day", usdc(p.perDay));
    const dh = S.stops.duration_h;
    setText("delta_proj_end", !S.loopOn
      ? (dh.on && dh.v > 0 ? `Held ${dur(dh.v * 60)}` : "Until you stop")
      : p.rev != null
        ? `${nf(p.rev)} ${p.rev === 1 ? "reverse" : "reverses"} · ${dur(p.rev * S.interval)}`
        : S.stops.max_loss_usd.on ? "Max loss only" : "Never");
    setText("delta_proj_total", p.total != null ? usdc(p.total) : "-");
    setText("delta_proj_margin", N > 0 ? `${usd(N / S.long.leverage)} + ${usd(N / S.short.leverage)}` : "-");

    // Start
    const block = blockingMessage();
    const btn = $("delta_start_btn");
    if (btn) {
      btn.disabled = !!block || _starting;
      btn.textContent = _starting ? "Starting…" : "Start";
    }
    const msg = $("delta_start_msg");
    if (msg && !_starting && msg.dataset.sticky !== "1") msg.textContent = block;
  }

  function clearStartError() {
    const msg = $("delta_start_msg");
    if (msg) msg.dataset.sticky = "";
  }
  function touch() { _touched = true; _lockMsg = ""; clearStartError(); render(); saveForm(); }

  // ── Actions ────────────────────────────────────────────────────────────────
  function swapLegs() {
    const l = S.long;
    S.long = S.short;
    S.short = l;
    touch();
  }

  function openConnect(venue) {
    if (!isWalletConnected()) {
      $("header_connect_btn")?.click();
      return;
    }
    if (typeof openWalletsDrawer === "function") openWalletsDrawer();
  }

  async function onVenueClick(side, id) {
    const leg = S[side];
    if (id === leg.venue) {
      const a = _account && _account[id];
      if (!(a && a.connected)) openConnect(id);
      return;
    }
    leg.venue = id;
    const o = S[other(side)];
    if (sameVenue()) S.sameToken = false;       // un compte ne peut pas être long et short du même token
    touch();                                    // affichage immédiat, le token suit
    // Même token sur la nouvelle plateforme si possible (et s'il n'est pas déjà pris par l'autre jambe).
    const u = underlying(leg.symbol);
    const hit = await findByUnderlying(id, u);
    const taken = (r) => sameVenue() && underlying(r.symbol) === underlying(o.symbol);
    let row = hit && !taken(hit) ? hit : null;
    if (!row) {
      const rows = (await allRows(id)).slice().sort((a, b) => (Number(b.volume_usd) || 0) - (Number(a.volume_usd) || 0));
      row = rows.find((r) => !taken(r)) || null;
    }
    if (row) {
      leg.symbol = row.symbol;
      const mx = Math.max(1, Math.min(MAX_LEV, Number(row.max_leverage) || MAX_LEV));
      if (leg.leverage > mx) leg.leverage = mx;
    }
    if (S.sameToken && underlying(leg.symbol) !== underlying(o.symbol)) {
      S.sameToken = false;
      _lockMsg = `${u} is not on ${VENUE_NAME[id]}: pick a token on each leg.`;
      _touched = true; clearStartError(); render(); saveForm();
    } else {
      touch();
    }
    await loadMarkets(id, sourceOf(leg));
    render();
  }

  async function setLegMarket(side, row) {
    const leg = S[side];
    leg.symbol = row.symbol;
    const mx = Math.max(1, Math.min(MAX_LEV, Number(row.max_leverage) || MAX_LEV));
    if (leg.leverage > mx) leg.leverage = mx;
    touch();
    if (S.sameToken) await alignOtherLeg(side);
  }

  // Aligne la jambe opposée sur le sous-jacent de `side`. Si ce sous-jacent
  // n'existe pas sur l'autre plateforme, rien n'est changé en silence : le
  // verrou est retiré et un message l'explique.
  async function alignOtherLeg(side) {
    const o = other(side);
    const u = underlying(S[side].symbol);
    if (underlying(S[o].symbol) === u) return;
    const hit = await findByUnderlying(S[o].venue, u);
    if (hit) {
      S[o].symbol = hit.symbol;
      const mx = Math.max(1, Math.min(MAX_LEV, Number(hit.max_leverage) || MAX_LEV));
      if (S[o].leverage > mx) S[o].leverage = mx;
    } else {
      S.sameToken = false;
      _lockMsg = `${u} is not on ${VENUE_NAME[S[o].venue]}: pick a token on each leg.`;
    }
    render();
    saveForm();                                 // jambe alignée après un choix de l'utilisateur
  }

  function payload() {
    const st = S.stops;
    const stop = {};
    for (const k of Object.keys(st)) stop[k] = st[k].on ? Number(st[k].v) : null;
    return {
      long: { venue: S.long.venue, symbol: S.long.symbol, leverage: S.long.leverage },
      short: { venue: S.short.venue, symbol: S.short.symbol, leverage: S.short.leverage },
      notional_usd: Number(S.notional),
      same_token: !!S.sameToken,
      execution: { mode: S.mode, limit_leg: S.limitLeg, chase_sec: Number(S.chase), chase_timeout_sec: Number(S.chaseTimeout) },
      loop: { enabled: !!S.loopOn, interval_min: Number(S.interval), jitter_pct: S.jitterOn ? Number(S.jitter) : 0 },
      stop,
      guard: { enabled: !!S.guardOn, close_at_pct: Number(S.guardClose), alert_at_pct: Number(S.guardAlert) },
      notifications: { telegram: !!S.telegramOn },
    };
  }

  async function start() {
    if (_starting || blockingMessage()) return;
    _starting = true;
    clearStartError();
    render();
    const msg = $("delta_start_msg");
    try {
      await apiPost("/api/delta/start", payload(), { timeoutMs: 30000 });
      _starting = false;
      if (msg) msg.textContent = "";
      await showRunning();
    } catch (e) {
      _starting = false;
      render();
      if (msg) {
        msg.textContent = (e && e.message) || "Could not start Delta. Try again.";
        msg.dataset.sticky = "1";
      }
      return;
    }
    render();
  }

  // ── Running ────────────────────────────────────────────────────────────────
  const LIVE_STATES = new Set(["STARTING", "OPENING", "HOLDING", "REVERSING", "CLOSING"]);
  const TERMINAL = new Set(["STOPPED", "ERROR"]);
  const HISTORY_SHOWN = 20;
  let _status = null;          // dernière réponse de /api/delta/status
  let _startedAt = 0;          // horodatage local (s) du dernier Start réussi
  let _tickTimer = null;
  let _cyclesHtml = "";        // rendu mémoïsé du tableau des cycles
  let _historyHtml = "";
  let _historyAll = false;
  let _sessions = null;
  let _stopping = false;
  let _runMsg = { text: "", err: false };

  // « Ended by » de l'historique (spec) ; toute autre raison = Error.
  const ENDED_BY = {
    max_reverses: "Reverse count", volume_target: "Volume target", duration: "Duration",
    max_loss: "Max loss", liquidation_guard: "Liquidation guard", user_stop: "You",
    server_lost: "Server lost", signer_expiring: "Signer expiry",
  };
  const STOP_SENTENCE = {
    max_reverses: "Reverse count reached.",
    volume_target: "Volume target reached.",
    duration: "Duration reached.",
    max_loss: "Max loss reached.",
    liquidation_guard: "Liquidation guard triggered: a leg came too close to liquidation.",
    user_stop: "Stopped by you.",
    server_lost: "Lost contact with the server.",
    signer_expiring: "The RISEx signer was about to expire: both legs were closed. Renew it before the next session.",
    imbalance: "The legs drifted apart and could not be rebalanced.",
    move_incomplete: "A move could not be completed after 3 tries.",
  };
  const endedBy = (reason) => ENDED_BY[reason] || "Error";

  function venueLabel(v) { return VENUE_NAME[v] || (v ? String(v) : "-"); }

  // Phrase d'erreur (bandeau rouge) : cause, puis consigne.
  function errorText(reason, cfg) {
    const r = String(reason || "");
    if (r === "hedge_failed") return "A leg could not be hedged. Both legs were closed. Check your positions.";
    const cf = /^close_failed_(long|short)$/.exec(r);
    if (cf) {
      const venue = cfg && cfg[cf[1]] ? venueLabel(cfg[cf[1]].venue) : "both exchanges";
      return `Check your positions on ${venue}: one leg may still be open.`;
    }
    const om = /^order_may_be_open(?:_(long|short))?$/.exec(r);
    if (om) {
      const venue = om[1] && cfg && cfg[om[1]] ? venueLabel(cfg[om[1]].venue) : "one of the exchanges";
      return `An order may still be open on ${venue}: check it on the exchange.`;
    }
    if (r === "position_mismatch") return "A position no longer matches what Delta opened. Both legs were closed. Check your positions.";
    if (r === "move_incomplete") return "A move could not be completed after 3 tries. Both legs were closed: check your positions.";
    if (r === "imbalance") return "The legs drifted apart and could not be rebalanced. Both legs were closed. Check your positions.";
    if (r === "existing_position") return "Delta did not start: a position is already open on one of these markets. Close it first.";
    if (r === "insufficient_margin") return "Delta did not start: not enough margin. Add funds and try again.";
    if (r === "size_below_minimum") return "Delta did not start: the size is below the market minimum. Raise the size.";
    if (r.startsWith("startup_failed")) return "Delta could not start. Nothing was opened. Try again.";
    if (r.startsWith("engine_error")) return "Delta hit an unexpected error. Both legs were closed. Check your positions.";
    return "Delta stopped with an error. Check your positions on both exchanges.";
  }

  function metricsOf(st) { return st && st.metrics && typeof st.metrics === "object" ? st.metrics : null; }
  function stateOf(m) { return m ? String(m.status || m.state || "") : ""; }

  // Vue Running si le process tourne, si une session est en cours (relance) ou si
  // le serveur demande une intervention. Un état terminal avec un process encore
  // vivant = fin de session (Setup), sauf juste après un Start (anciennes métriques).
  function wantsRunning(st) {
    if (!st) return _running;
    if (st.needs_attention) return true;
    const m = metricsOf(st);
    const s = stateOf(m);
    if (st.running) {
      if (!TERMINAL.has(s)) return true;
      return !!_startedAt && !(Number(m && m.updated_at) >= _startedAt);
    }
    return LIVE_STATES.has(s) && s !== "STARTING";
  }

  function signed(v, d = 2) {
    if (v == null || !Number.isFinite(Number(v))) return "-";
    const x = Number(v);
    return (x > 0 ? "+" : x < 0 ? MINUS : "") + "$" + nf(Math.abs(x), d);
  }
  const tone = (v) => (v == null || !Number.isFinite(Number(v)) || Number(v) === 0 ? "" : Number(v) > 0 ? "dl-pos" : "dl-neg");
  function setStat(id, text, cls, title) {
    const el = $(id);
    if (!el) return;
    if (el.textContent !== text) el.textContent = text;
    el.classList.remove("dl-pos", "dl-neg");
    if (cls) el.classList.add(cls);
    el.classList.toggle("dl-partial", !!title);
    if (title) el.title = title; else el.removeAttribute("title");
  }
  function clock(ts) {
    const d = new Date(Number(ts) * 1000);
    return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }
  function mmss(sec) {
    const s = Math.max(0, Math.floor(sec));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${r}` : `${String(m).padStart(2, "0")}:${r}`;
  }
  const baseSym = (sym) => underlying(sym) || String(sym || "");
  function sizeTxt(size, sym) {
    const a = Math.abs(Number(size));
    if (!Number.isFinite(a)) return "-";
    const d = a >= 100 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 4 : 6;
    return nf(a, d).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "") + " " + baseSym(sym);
  }
  function costTxt(v) {
    if (v == null || !Number.isFinite(Number(v))) return "-";
    const x = Number(v);
    return (x < 0 ? MINUS : "") + "$" + nf(Math.abs(x), Math.abs(x) >= 100 ? 0 : 2);
  }
  function flipHtml(f) {
    if (f === "open") return '<span class="dl-dir">Open</span>';
    const m = /^([LS])→([LS])$/.exec(String(f || ""));
    if (!m) return esc(f || "-");
    const c = (x) => `<span class="${x === "L" ? "dl-l" : "dl-s"}">${x}</span>`;
    return `<span class="dl-dir">${c(m[1])}→${c(m[2])}</span>`;
  }
  const secTxt = (v) => (v == null || !Number.isFinite(Number(v)) ? "-" : nf(Number(v), 1) + "s");

  function cfgOf(m) {
    const c = _status && _status.config;
    return {
      long: (m && m.long) || (c && c.long) || null,
      short: (m && m.short) || (c && c.short) || null,
      execution: (c && c.execution) || null,
    };
  }

  function renderBanner(el, kind, html, dismissKey) {
    if (!el) return;
    if (!html) { el.hidden = true; el.innerHTML = ""; el.className = "dl-banner"; return; }
    const close = dismissKey ? `<button type="button" class="dl-banner-close" data-dismiss="${esc(dismissKey)}" aria-label="Dismiss">&#10005;</button>` : "";
    const out = `<div class="dl-banner-txt">${html}</div>${close}`;
    if (el.innerHTML !== out) el.innerHTML = out;
    el.className = "dl-banner dl-banner--" + kind;
    el.hidden = false;
  }

  function dismissed(key) { try { return localStorage.getItem("delta_banner_dismissed") === key; } catch (_) { return false; } }

  // Bandeau de fin (écran Setup) : raison de la dernière session terminée.
  function renderEndBanner() {
    const el = $("delta_end_banner");
    const m = metricsOf(_status);
    const s = stateOf(m);
    if (!m || !TERMINAL.has(s) || _running) { renderBanner(el, "", ""); return; }
    const key = String(m.updated_at || "");
    if (dismissed(key)) { renderBanner(el, "", ""); return; }
    if (s === "ERROR") {
      renderBanner(el, "err", `<b>Delta stopped.</b> ${esc(errorText(m.reason, cfgOf(m)))}`, key);
    } else {
      const why = STOP_SENTENCE[m.reason] || "";
      renderBanner(el, "info", `<b>Delta stopped.</b> ${esc(why)} Both legs are closed.`, key);
    }
  }

  function renderRunBanner(m, s) {
    const el = $("delta_run_banner");
    if (_status && _status.needs_attention) {
      renderBanner(el, "err", "Your Delta loop stopped unexpectedly and could not restart. Check your positions on both exchanges.");
    } else if (s === "CLOSING") {
      const why = STOP_SENTENCE[m && m.reason] || "";
      renderBanner(el, "warn", `<b>Delta is stopping.</b> ${esc(why)}`);
    } else if (m && m.startup_error) {
      renderBanner(el, "warn", "Delta cannot reach an exchange right now and keeps retrying. Your positions stay open.");
    } else {
      renderBanner(el, "", "");
    }
  }

  function renderPosition(id, card, m) {
    const el = $(id);
    if (!el) return;
    const cfg = cfgOf(m)[card] || {};
    const p = m && m.positions ? m.positions[card] : null;
    const size = p ? Number(p.size) : 0;
    const side = p && size !== 0 ? (size > 0 ? "long" : "short") : null;
    const venue = venueLabel((p && p.venue) || cfg.venue);
    const sym = (p && p.symbol) || cfg.symbol || "";
    let html;
    if (!side) {
      html = `<div class="dl-row"><span class="dl-pill dl-pill--flat">Flat</span><span class="dl-lbl">${esc(venue)}</span></div>
        <div class="dl-row"><span class="dl-val">${esc(baseSym(sym) || "-")}</span><span class="dl-val dl-muted">-</span></div>`;
    } else {
      const liq = p.liq_dist_pct == null ? "liq. -" : `liq. ${nf(p.liq_dist_pct, 1)}% away${p.liq_estimated ? " <em>est.</em>" : ""}`;
      // Taille réellement tradée (quantité x mark) : peut différer du notionnel saisi (pas commun).
      const notional = Number.isFinite(Number(p.mark)) && Number(p.mark) > 0 ? Math.abs(size) * Number(p.mark) : null;
      const sizeHtml = esc(sizeTxt(size, sym)) + (notional == null ? "" : ` <span class="dl-muted" title="Size traded: quantity × mark price.">≈ ${esc(usd(Math.round(notional)))}</span>`);
      html = `<div class="dl-row"><span class="dl-pill dl-pill--${side}">${side === "long" ? "Long" : "Short"}</span><span class="dl-lbl">${esc(venue)}</span></div>
        <div class="dl-row"><span class="dl-val">${sizeHtml}</span><span class="dl-val ${tone(p.upnl)}">${signed(p.upnl)}</span></div>
        <div class="dl-row dl-livepos-sub"><span>entry ${p.entry == null ? "-" : esc(fmtPrice(Number(p.entry)))}</span><span${p.liq_estimated ? ' title="Estimated from leverage: the exchange does not report it."' : ""}>${liq}</span></div>`;
    }
    if (el.innerHTML !== html) el.innerHTML = html;
  }

  function renderStops(m) {
    const el = $("delta_stop_list");
    if (!el) return;
    const sp = (m && m.stop_progress) || {};
    const row = (label, value, frac) => `<div class="dl-stopprog"><div class="dl-row"><span>${label}</span><span class="dl-val">${value}</span></div>`
      + `<div class="dl-progress dl-progress--thin"><i style="width:${(Math.max(0, Math.min(1, frac || 0)) * 100).toFixed(1)}%"></i></div></div>`;
    let html = "";
    const f = (c, t) => (Number(t) > 0 && c != null ? Number(c) / Number(t) : 0);
    if (sp.max_reverses) html += row("Reverses", `${nf(sp.max_reverses.current)} / ${nf(sp.max_reverses.target)}`, f(sp.max_reverses.current, sp.max_reverses.target));
    if (sp.volume_usd) html += row("Volume", `${usdc(sp.volume_usd.current)} / ${usdc(sp.volume_usd.target)}`, f(sp.volume_usd.current, sp.volume_usd.target));
    if (sp.duration_h) html += row("Time", `${dur(Number(sp.duration_h.current) * 60)} / ${dur(Number(sp.duration_h.target) * 60)}`, f(sp.duration_h.current, sp.duration_h.target));
    if (sp.max_loss_usd) html += row("Loss", `${sp.max_loss_usd.current == null ? "-" : usd(Math.round(sp.max_loss_usd.current))} / ${usd(sp.max_loss_usd.target)}`, f(sp.max_loss_usd.current, sp.max_loss_usd.target));
    if (!html) html = '<span class="dl-note">No stop condition set: the loop runs until you stop it.</span>';
    if (el.innerHTML !== html) el.innerHTML = html;
  }

  function renderCycles(m, s) {
    const body = $("delta_cycles_body");
    if (!body) return;
    const cycles = Array.isArray(m && m.cycles) ? m.cycles.slice().reverse() : [];
    let html = "";
    if (s === "HOLDING" || s === "REVERSING") {
      const dir = Number(m.direction) > 0 ? "L→S" : "S→L";
      const when = s === "REVERSING" ? "now" : 'in <span data-dl-countdown></span>';
      html += `<tr class="is-next"><td>${nf(Number(m.reverses || 0) + 1)}</td><td>${when}</td><td>${flipHtml(dir)}</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td></tr>`;
    }
    html += cycles.map((c) => {
      const fund = c.funding == null ? "-" : signed(c.funding);
      return `<tr><td>${c.flip === "open" ? "0" : esc(nf(Number(c.n) || 0))}</td><td>${c.t ? esc(clock(c.t)) : "-"}</td><td>${flipHtml(c.flip)}</td>`
        + `<td>${secTxt(c.limit_fill_sec)}</td><td>${c.slippage_bp == null ? "-" : nf(c.slippage_bp, 1) + " bp"}</td>`
        + `<td>${c.fees == null ? "-" : "$" + nf(c.fees, 2)}</td><td class="${tone(c.funding)}">${fund}</td><td>${secTxt(c.unhedged_sec)}</td></tr>`;
    }).join("");
    if (!html) html = '<tr class="dl-cycles-empty"><td colspan="8">The first cycle appears once both legs are open.</td></tr>';
    if (html !== _cyclesHtml) {
      _cyclesHtml = html;
      body.innerHTML = html;
      tick();
    }
  }

  function execHint(cfg) {
    const ex = cfg.execution;
    if (!ex) return "";
    if (ex.mode === "market") return "Market both legs";
    const leg = ex.limit_leg === "auto" ? "auto" : ex.limit_leg === "long" ? "long leg" : "short leg";
    return `Limit (${leg}), then market`;
  }

  function renderRunning() {
    const m = metricsOf(_status);
    const s = stateOf(m);
    const cfg = cfgOf(m);
    renderRunBanner(m, s);

    // Carte principale
    const pill = $("delta_run_pill");
    const pillTxt = _status && _status.needs_attention ? "Attention" : s === "CLOSING" ? "Stopping" : "Running";
    if (pill) {
      if (pill.textContent !== pillTxt) pill.textContent = pillTxt;
      pill.classList.toggle("is-alert", pillTxt !== "Running");
    }
    const L = cfg.long || {}, Sh = cfg.short || {};
    const market = baseSym(L.symbol) === baseSym(Sh.symbol)
      ? `${baseSym(L.symbol) || "-"} · ${L.leverage || "-"}x / ${Sh.leverage || "-"}x`
      : `${baseSym(L.symbol)} / ${baseSym(Sh.symbol)}`;
    setText("delta_run_market", market);
    const dirPos = !m || Number(m.direction) >= 0;
    const curLong = venueLabel((dirPos ? L : Sh).venue);
    const curShort = venueLabel((dirPos ? Sh : L).venue);
    const done = Number(m && m.reverses) || 0;
    const max = m && m.max_reverses != null ? Number(m.max_reverses) : null;
    const revTxt = max ? `${nf(done)} / ${nf(max)}` : nf(done);
    const sentence = $("delta_run_sentence");
    let sHtml;
    if (s === "CLOSING") sHtml = `Both legs are being closed at market. Reverse <span class="dl-val">${revTxt}</span>.`;
    else if (s === "STARTING" || s === "OPENING" || !s) sHtml = `Opening long on <b>${esc(curLong)}</b> and short on <b>${esc(curShort)}</b>.`;
    else sHtml = `Long on <b>${esc(curLong)}</b> becomes short, short on <b>${esc(curShort)}</b> becomes long. Reverse <span class="dl-val">${revTxt}</span>.`;
    if (sentence && sentence.innerHTML !== sHtml) sentence.innerHTML = sHtml;
    setText("delta_run_exec", execHint(cfg));

    // Tuiles
    setStat("delta_stat_rev", m ? revTxt : "-");
    setStat("delta_stat_vol", m && m.volume_usd != null ? usdc(m.volume_usd) : "-");
    const fees = m && m.fees_usd != null ? -Number(m.fees_usd) : null;
    setStat("delta_stat_fees", signed(fees), tone(fees));
    const extLeg = [L.venue, Sh.venue].includes("extended");
    setStat("delta_stat_funding", m ? signed(m.funding_usd) : "-", m ? tone(m.funding_usd) : "",
      m && m.funding_partial ? (extLeg ? "Extended funding is not available yet." : "Part of the funding is not available yet.") : "");
    setStat("delta_stat_pnl", m ? signed(m.pnl_usd) : "-", m ? tone(m.pnl_usd) : "",
      m && m.pnl_partial ? "Estimated: some fills could not be priced." : "");
    setStat("delta_stat_cost", m ? costTxt(m.cost_per_m) : "-");

    renderCycles(m, s);
    renderPosition("delta_pos_long", "long", m);
    renderPosition("delta_pos_short", "short", m);
    renderStops(m);

    // Boutons
    const closing = s === "CLOSING" || _stopping;
    const rev = $("delta_reverse_btn");
    if (rev) rev.disabled = s !== "HOLDING" || closing;
    const stop = $("delta_stop_btn");
    if (stop) {
      stop.disabled = closing;
      const t = closing ? "Stopping…" : "Stop";
      if (stop.textContent !== t) stop.textContent = t;
    }
    const msg = $("delta_run_msg");
    if (msg) {
      if (msg.textContent !== _runMsg.text) msg.textContent = _runMsg.text;
      msg.classList.toggle("hv-err", _runMsg.err);
    }
    tick();
  }

  // Compte à rebours local, à la seconde, depuis next_reverse_at.
  function tick() {
    if (!_running) return;
    const m = metricsOf(_status);
    const s = stateOf(m);
    const now = Date.now() / 1000;
    const next = Number(m && m.next_reverse_at);
    let title, num = "", frac = 0;
    if (_status && _status.needs_attention && s !== "CLOSING") title = "Delta needs attention";
    else if (s === "CLOSING") title = "Closing both legs…";
    else if (s === "REVERSING") { title = "Reversing…"; frac = 1; }
    else if (s === "HOLDING" && !(Number.isFinite(next) && next > 0)) title = "Holding both legs";
    else if (s === "HOLDING") {
      const left = next - now;
      if (left <= 0) { title = "Reversing…"; frac = 1; }
      else {
        title = "Next reverse in";
        num = mmss(left);
        const cycles = Array.isArray(m.cycles) ? m.cycles : [];
        const last = cycles.length ? Number(cycles[cycles.length - 1].t) : Number(m.started_at);
        const total = Number.isFinite(last) && next > last ? next - last : 0;
        frac = total > 0 ? 1 - left / total : 0;
      }
    } else title = "Opening both legs…";
    setText("delta_run_title_txt", title);
    document.querySelectorAll("#delta_running [data-dl-countdown]").forEach((el) => { if (el.textContent !== num) el.textContent = num; });
    const bar = $("delta_run_progress");
    if (bar) bar.style.width = (Math.max(0, Math.min(1, frac)) * 100).toFixed(2) + "%";
  }

  function startTick() { if (!_tickTimer) _tickTimer = setInterval(tick, 1000); }
  function stopTick() { if (_tickTimer) { clearInterval(_tickTimer); _tickTimer = null; } }

  // Modal Promise (modèle _hip4volAskLimitDeadline) : true = confirmé.
  function askStop() {
    return new Promise((resolve) => {
      const overlay = $("delta_stop_modal_overlay");
      const modal = $("delta_stop_modal");
      const confirmBtn = $("delta_stop_modal_confirm");
      const cancelBtn = $("delta_stop_modal_cancel");
      const closeBtn = $("delta_stop_modal_close");
      if (!overlay || !modal || !confirmBtn) { resolve(false); return; }
      function close(value) {
        overlay.classList.add("hidden"); overlay.setAttribute("aria-hidden", "true");
        modal.classList.add("hidden"); modal.setAttribute("aria-hidden", "true");
        confirmBtn.removeEventListener("click", onConfirm);
        cancelBtn?.removeEventListener("click", onCancel);
        closeBtn?.removeEventListener("click", onCancel);
        overlay.removeEventListener("click", onCancel);
        document.removeEventListener("keydown", onKey);
        _closeStopModal = null;
        resolve(value);
      }
      function onConfirm() { close(true); }
      function onCancel() { close(false); }
      function onKey(e) { if (e.key === "Escape") close(false); }
      confirmBtn.addEventListener("click", onConfirm);
      cancelBtn?.addEventListener("click", onCancel);
      closeBtn?.addEventListener("click", onCancel);
      overlay.addEventListener("click", onCancel);
      document.addEventListener("keydown", onKey);
      _closeStopModal = () => close(false);
      overlay.classList.remove("hidden"); overlay.setAttribute("aria-hidden", "false");
      modal.classList.remove("hidden"); modal.setAttribute("aria-hidden", "false");
      confirmBtn.focus();
    });
  }
  let _closeStopModal = null;

  async function stopLoop() {
    if (_stopping) return;
    if (!(await askStop())) return;
    _stopping = true;
    _runMsg = { text: "", err: false };
    renderRunning();
    try {
      await apiPost("/api/delta/stop", {}, { timeoutMs: 30000 });
    } catch (e) {
      _stopping = false;
      if (e && e.status !== 404) _runMsg = { text: (e && e.message) || "Could not stop Delta. Try again.", err: true };
    }
    await pollStatus();
  }

  async function reverseNow() {
    const btn = $("delta_reverse_btn");
    if (btn) btn.disabled = true;
    try {
      await apiPost("/api/delta/reverse_now", {}, { timeoutMs: 15000 });
      _runMsg = { text: "Reverse requested: it starts within a few seconds.", err: false };
    } catch (e) {
      _runMsg = { text: (e && e.message) || "Could not reverse now. Try again.", err: true };
    }
    renderRunning();
    setTimeout(() => { if (_active) pollStatus(); }, 1500);
  }

  // ── Historique des sessions ────────────────────────────────────────────────
  async function loadHistory() {
    if (!isWalletConnected()) { _sessions = null; renderHistory(); return; }
    try {
      const d = await apiGet("/api/delta/history");
      _sessions = Array.isArray(d && d.sessions) ? d.sessions : [];
    } catch (_) {
      if (_sessions == null) return;
    }
    if (_active) renderHistory();
  }

  function renderHistory() {
    const box = $("delta_history");
    const table = $("delta_history_table");
    if (!box || !table) return;
    if (_sessions == null) { box.hidden = true; return; }
    box.hidden = false;
    const rows = _historyAll ? _sessions : _sessions.slice(0, HISTORY_SHOWN);
    const date = (ts) => {
      if (!ts) return "-";
      const d = new Date(Number(ts) * 1000);
      return d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) + ", "
        + d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
    };
    let html = `<div class="hv-history-row hv-history-colhead"><div>Date</div><div>Duration</div><div>Volume</div><div>Net PnL</div><div>Cost / $1M</div><div>Ended by</div></div>`;
    html += rows.map((r) => {
      const d = r.started_at && r.ended_at ? dur((Number(r.ended_at) - Number(r.started_at)) / 60) : "-";
      const by = endedBy(r.reason);
      const tip = by === "Error" ? errorText(r.reason, r) : STOP_SENTENCE[r.reason] || "";
      const pnlCls = Number(r.pnl_usd) > 0 ? "hv-h-pos" : Number(r.pnl_usd) < 0 ? "hv-h-neg" : "";
      return `<div class="hv-history-row"><div>${esc(date(r.started_at || r.ended_at))}</div><div>${esc(d)}</div>`
        + `<div class="${Number(r.volume_usd) > 0 ? "hv-h-vol" : ""}">${usdc(r.volume_usd)}</div><div class="${pnlCls}"${r.pnl_partial ? ' title="Estimated: some fills could not be priced."' : ""}>${signed(r.pnl_usd)}</div>`
        + `<div>${costTxt(r.cost_per_m)}</div><div class="${by === "Error" ? "hv-h-neg" : ""}" title="${esc(tip)}">${esc(by)}</div></div>`;
    }).join("");
    if (!_sessions.length) html += '<div class="hv-history-empty">No session yet.</div>';
    if (_sessions.length > HISTORY_SHOWN) html += `<button type="button" class="hv-history-more" data-history-more>${_historyAll ? "Show less" : `Show all ${nf(_sessions.length)}`}</button>`;
    if (html !== _historyHtml) { _historyHtml = html; table.innerHTML = html; }
    const vol = _sessions.reduce((a, r) => a + (Number(r.volume_usd) || 0), 0);
    setText("delta_history_summary", _sessions.length ? `${nf(_sessions.length)} ${_sessions.length === 1 ? "session" : "sessions"} · ${usdc(vol)}` : "");
  }

  function applyView(running) {
    const was = _running;
    _running = !!running;
    const setup = $("delta_setup");
    const run = $("delta_running");
    if (setup) setup.classList.toggle("hidden", _running);
    if (run) { run.classList.toggle("hidden", !_running); run.setAttribute("aria-hidden", _running ? "false" : "true"); }
    if (_running) { stopAccountPoll(); startTick(); } else { stopTick(); if (_active) { startAccountPoll(); pollAccount(); } }
    if (was && !_running) { _stopping = false; _runMsg = { text: "", err: false }; loadHistory(); }
    if (!was && _running) { _runMsg = { text: "", err: false }; if (_closeStopModal) _closeStopModal(); }
  }

  async function showRunning() {
    _startedAt = Date.now() / 1000;
    _running = false;
    applyView(true);
    await pollStatus();
  }

  // ── Données serveur ────────────────────────────────────────────────────────
  function applyConfig(cfg) {
    if (!cfg || typeof cfg !== "object") return;
    const leg = (src, dst) => {
      if (!src || !VENUE_NAME[src.venue] || !src.symbol) return;
      dst.venue = src.venue;
      dst.symbol = String(src.symbol);
      const lev = Math.round(Number(src.leverage));
      if (lev >= 1) dst.leverage = Math.min(MAX_LEV, lev);
    };
    leg(cfg.long, S.long);
    leg(cfg.short, S.short);
    const num = (v, d) => (v == null || !Number.isFinite(Number(v)) ? d : Number(v));
    S.notional = num(cfg.notional_usd, S.notional);
    if (typeof cfg.same_token === "boolean") S.sameToken = cfg.same_token;
    const ex = cfg.execution || {};
    if (ex.mode === "market" || ex.mode === "limit_then_market") S.mode = ex.mode;
    if (["long", "short", "auto"].includes(ex.limit_leg)) S.limitLeg = ex.limit_leg;
    S.chase = num(ex.chase_sec, S.chase);
    S.chaseTimeout = num(ex.chase_timeout_sec, S.chaseTimeout);
    const lp = cfg.loop || {};
    if (typeof lp.enabled === "boolean") S.loopOn = lp.enabled;
    S.interval = num(lp.interval_min, S.interval);
    const j = num(lp.jitter_pct, S.jitter);
    S.jitterOn = j > 0;
    if (j > 0) S.jitter = j;
    const st = cfg.stop || {};
    for (const k of Object.keys(S.stops)) {
      if (!(k in st)) continue;
      if (st[k] == null) S.stops[k].on = false;
      else { S.stops[k].on = true; S.stops[k].v = num(st[k], S.stops[k].v); }
    }
    const nt = cfg.notifications || {};
    if (typeof nt.telegram === "boolean") S.telegramOn = nt.telegram;
    const g = cfg.guard || {};
    if (typeof g.enabled === "boolean") S.guardOn = g.enabled;
    S.guardClose = num(g.close_at_pct, S.guardClose);
    S.guardAlert = num(g.alert_at_pct, S.guardAlert);
  }

  async function fetchStatus() {
    if (!isWalletConnected()) return null;
    try {
      return await apiGet("/api/delta/status");
    } catch (_) {
      return null;
    }
  }

  async function pollStatus() {
    const st = await fetchStatus();
    if (!_active || !st) return;
    if (!_prefilled && !_touched && !_formRestored && st.config) {
      applyConfig(st.config);
      ensureLegMarkets();
    }
    _prefilled = true;
    const prevState = stateOf(metricsOf(_status));
    const live = wantsRunning(st);
    // Juste après un Start, les métriques sont encore celles de la session
    // précédente (terminale) : on ne les affiche pas dans l'écran de suivi.
    _status = live && TERMINAL.has(stateOf(metricsOf(st))) ? { ...st, metrics: null } : st;
    if (live !== _running) applyView(live);
    const s = stateOf(metricsOf(st));
    if (TERMINAL.has(s) && prevState && !TERMINAL.has(prevState)) loadHistory();
    if (_running) renderRunning();
    renderEndBanner();
    render();
  }

  async function pollAccount() {
    if (!isWalletConnected()) {
      _account = null;
      render();
      return;
    }
    try {
      const d = await apiGet("/api/delta/account");
      _account = d && d.venues ? d.venues : null;
    } catch (_) {
      // garde le dernier solde connu
    }
    if (_active) render();
  }

  function ensureLegMarkets() {
    const jobs = [];
    for (const side of ["long", "short"]) jobs.push(loadMarkets(S[side].venue, sourceOf(S[side])));
    Promise.all(jobs).then(() => { if (_active) render(); });
  }

  function startAccountPoll() {
    if (_accountTimer) return;
    _accountTimer = setInterval(pollAccount, ACCOUNT_POLL_MS);
  }
  function stopAccountPoll() {
    if (_accountTimer) { clearInterval(_accountTimer); _accountTimer = null; }
  }

  // ── Modal token ────────────────────────────────────────────────────────────
  const M = { side: "long", source: "perps", sort: "volume", asc: false };

  function openTokenModal(side) {
    M.side = side;
    M.source = sourceOf(S[side]);
    const search = $("delta_token_modal_search");
    if (search) search.value = "";
    $("delta_token_modal_overlay")?.classList.remove("hidden");
    $("delta_token_modal_overlay")?.setAttribute("aria-hidden", "false");
    $("delta_token_modal")?.classList.remove("hidden");
    setText("delta_token_modal_title", `${side === "long" ? "Long" : "Short"} leg · ${VENUE_NAME[S[side].venue]}`);
    renderTokenTabs();
    renderTokenRows();
    loadMarkets(S[side].venue, M.source).then(renderTokenRows);
    if (S.sameToken) allRows(S[other(side)].venue).then(renderTokenRows);
    if (search) search.focus();
  }
  function closeTokenModal() {
    $("delta_token_modal_overlay")?.classList.add("hidden");
    $("delta_token_modal_overlay")?.setAttribute("aria-hidden", "true");
    $("delta_token_modal")?.classList.add("hidden");
  }
  function tokenModalOpen() { const m = $("delta_token_modal"); return !!m && !m.classList.contains("hidden"); }

  function renderTokenTabs() {
    const tabs = $("delta_token_modal_tabs");
    if (!tabs) return;
    const srcs = sourcesOf(S[M.side].venue);
    tabs.classList.toggle("hidden", srcs.length < 2);
    tabs.innerHTML = srcs.length < 2 ? "" : srcs.map((s) =>
      `<button type="button" class="mm-token-modal-tab${s === M.source ? " active" : ""}" data-src="${s}" role="tab" aria-selected="${s === M.source}">${SOURCE_LABEL[s]}</button>`).join("");
  }

  function renderTokenRows() {
    const tbody = $("delta_token_modal_body");
    if (!tbody || !tokenModalOpen()) return;
    const leg = S[M.side];
    const rows = marketsCached(leg.venue, M.source);
    document.querySelectorAll("#delta_token_modal th[data-sort]").forEach((th) => th.classList.toggle("sort-active", th.dataset.sort === M.sort));
    if (!rows) { tbody.innerHTML = '<tr class="dl-tm-empty"><td colspan="3">Loading…</td></tr>'; return; }
    const q = ($("delta_token_modal_search")?.value || "").trim().toLowerCase();
    const otherVenue = S[other(M.side)].venue;
    // Verrou même token : tant que les marchés de l'autre plateforme ne sont pas
    // tous chargés, aucune ligne n'est sélectionnable (jamais de repli silencieux).
    const otherSet = S.sameToken ? underlyingsCached(otherVenue) : null;
    const pending = S.sameToken && !otherSet;
    const list = rows.filter((r) => !q || String(r.display || "").toLowerCase().includes(q) || String(r.symbol).toLowerCase().includes(q));
    const dir = M.asc ? 1 : -1;
    list.sort((a, b) => {
      if (M.sort === "symbol") return String(a.display || a.symbol).localeCompare(String(b.display || b.symbol)) * dir;
      const k = M.sort === "lev" ? "max_leverage" : "volume_usd";
      return ((Number(a[k]) || 0) - (Number(b[k]) || 0)) * dir;
    });
    const html = list.map((r) => {
      const u = r.underlying || underlying(r.symbol);
      const usedByOther = otherVenue === leg.venue && u === underlying(S[other(M.side)].symbol);
      const blocked = pending || usedByOther || (otherSet ? !otherSet.has(u) : false);
      const sel = r.symbol === leg.symbol ? " selected" : "";
      const icon = typeof _tokenIconUrl === "function"
        ? `<img class="token-icon" src="${esc(_tokenIconUrl(r.symbol))}" data-fallback="${esc(typeof _tokenIconFallbackUrl === "function" ? _tokenIconFallbackUrl(r.symbol) : "")}" width="20" height="20" alt="" loading="lazy" onerror="_tokenIconError(this)">`
        : "";
      const note = pending
        ? `<span class="dl-tm-note">Checking ${esc(VENUE_NAME[otherVenue])}…</span>`
        : usedByOther ? `<span class="dl-tm-note">${M.side === "long" ? "Short" : "Long"} leg</span>`
        : blocked ? `<span class="dl-tm-note">Not on ${esc(VENUE_NAME[otherVenue])}</span>` : "";
      return `<tr class="${sel}${blocked ? " disconnected" : ""}" data-symbol="${esc(r.symbol)}"${blocked ? ' aria-disabled="true"' : ""}>
        <td><div class="mm-tm-token-cell">${icon}${esc(r.display || r.symbol)}${note}</div></td>
        <td class="mm-col-right">${Number(r.max_leverage) ? esc(r.max_leverage) + "x" : "-"}</td>
        <td class="mm-col-right">${Number(r.volume_usd) ? usdc(r.volume_usd) : "-"}</td>
      </tr>`;
    }).join("");
    tbody.innerHTML = html || '<tr class="dl-tm-empty"><td colspan="3">No market found.</td></tr>';
  }

  // ── Liaisons DOM (une fois) ────────────────────────────────────────────────
  function numFrom(el, fallback) {
    const v = Number(el.value);
    return el.value !== "" && Number.isFinite(v) ? v : fallback;
  }

  function bind() {
    if (_bound) return;
    const page = $("delta_page");
    if (!page) return;
    _bound = true;
    for (const side of ["long", "short"]) {
      const root = legEl(side);
      root.innerHTML = legHtml(side);
      root.addEventListener("click", (e) => {
        const pb = e.target.closest("[data-pop-btn]");
        if (pb) { togglePop(root, pb.dataset.popBtn); return; }
        const chip = e.target.closest(".dl-venue-chip");
        if (chip && !chip.disabled) { closePops(); onVenueClick(side, chip.dataset.venue); return; }
        if (e.target.closest("[data-token-btn]")) { closePops(); openTokenModal(side); }
      });
      root.querySelector("[data-lev]").addEventListener("input", (e) => {
        S[side].leverage = Math.max(1, Math.round(Number(e.target.value) || 1));
        touch();
      });
    }
    document.addEventListener("click", (e) => { if (!e.target.closest(".dl-dd")) closePops(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePops(); });
    $("delta_swap_btn")?.addEventListener("click", swapLegs);

    $("delta_notional")?.addEventListener("input", (e) => { S.notional = numFrom(e.target, 0); touch(); });
    $("delta_same_token")?.addEventListener("click", () => {
      if (sameVenue()) {
        S.sameToken = false;
        touch();
        _lockMsg = "Same token is not possible with both legs on one exchange.";
        render();
        return;
      }
      S.sameToken = !S.sameToken;
      touch();
      if (S.sameToken) alignOtherLeg("long");
    });
    $("delta_interval")?.addEventListener("input", (e) => { S.interval = Number(e.target.value); touch(); });
    $("delta_jitter_on")?.addEventListener("click", () => { S.jitterOn = !S.jitterOn; touch(); });
    $("delta_loop_on")?.addEventListener("click", () => { S.loopOn = !S.loopOn; touch(); });
    $("delta_guard_on")?.addEventListener("click", () => { S.guardOn = !S.guardOn; touch(); });
    $("delta_telegram_on")?.addEventListener("click", () => { S.telegramOn = !S.telegramOn; touch(); });
    $("delta_jitter")?.addEventListener("input", (e) => { S.jitter = Number(e.target.value); touch(); });
    page.querySelectorAll("[data-exec]").forEach((b) => b.addEventListener("click", () => { S.mode = b.dataset.exec; touch(); }));
    page.querySelectorAll("[data-limit-leg]").forEach((b) => b.addEventListener("click", () => { S.limitLeg = b.dataset.limitLeg; touch(); }));
    $("delta_chase")?.addEventListener("input", (e) => { S.chase = numFrom(e.target, S.chase); touch(); });
    $("delta_chase_timeout")?.addEventListener("input", (e) => { S.chaseTimeout = numFrom(e.target, S.chaseTimeout); touch(); });
    $("delta_guard_close")?.addEventListener("input", (e) => {
      S.guardClose = Number(e.target.value);
      if (S.guardAlert <= S.guardClose) S.guardAlert = Math.min(80, S.guardClose + 5);
      touch();
    });
    $("delta_guard_alert")?.addEventListener("input", (e) => {
      S.guardAlert = Number(e.target.value);
      if (S.guardAlert <= S.guardClose) S.guardClose = Math.max(10, S.guardAlert - 5);
      touch();
    });
    page.querySelectorAll("[data-stop-toggle]").forEach((b) => b.addEventListener("click", () => {
      const st = S.stops[b.dataset.stopToggle];
      st.on = !st.on;
      touch();
    }));
    page.querySelectorAll("[data-stop-input]").forEach((inp) => inp.addEventListener("input", () => {
      const st = S.stops[inp.dataset.stopInput];
      st.v = numFrom(inp, st.v);
      touch();
    }));
    $("delta_start_btn")?.addEventListener("click", start);

    // Running
    $("delta_stop_btn")?.addEventListener("click", stopLoop);
    $("delta_reverse_btn")?.addEventListener("click", reverseNow);
    page.addEventListener("click", (e) => {
      const d = e.target.closest("[data-dismiss]");
      if (d) {
        try { localStorage.setItem("delta_banner_dismissed", d.dataset.dismiss); } catch (_) {}
        renderEndBanner();
        return;
      }
      if (e.target.closest("[data-history-more]")) { _historyAll = !_historyAll; renderHistory(); }
    });

    // Modal token
    $("delta_token_modal_close")?.addEventListener("click", closeTokenModal);
    $("delta_token_modal_overlay")?.addEventListener("click", closeTokenModal);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && tokenModalOpen()) closeTokenModal(); });
    $("delta_token_modal_search")?.addEventListener("input", renderTokenRows);
    const tabs = $("delta_token_modal_tabs");
    tabs?.addEventListener("mousedown", (e) => { if (e.target.closest("[data-src]")) e.preventDefault(); });
    tabs?.addEventListener("click", (e) => {
      const t = e.target.closest("[data-src]");
      if (!t) return;
      e.stopPropagation();
      M.source = t.dataset.src;
      renderTokenTabs();
      renderTokenRows();
      loadMarkets(S[M.side].venue, M.source).then(renderTokenRows);
    });
    document.querySelector("#delta_token_modal thead")?.addEventListener("click", (e) => {
      const th = e.target.closest("th[data-sort]");
      if (!th) return;
      if (M.sort === th.dataset.sort) M.asc = !M.asc;
      else { M.sort = th.dataset.sort; M.asc = M.sort === "symbol"; }
      renderTokenRows();
    });
    $("delta_token_modal_body")?.addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-symbol]");
      if (!tr || tr.classList.contains("disconnected")) return;
      const rows = marketsCached(S[M.side].venue, M.source) || [];
      const row = rows.find((r) => r.symbol === tr.dataset.symbol);
      if (!row) return;
      closeTokenModal();
      setLegMarket(M.side, row);
    });
  }

  // ── Cycle de vie ───────────────────────────────────────────────────────────
  function onEnter() {
    bind();
    _active = true;
    render();
    ensureLegMarkets();
    pollAccount();
    pollStatus();
    loadHistory();
    if (!_statusTimer) _statusTimer = setInterval(pollStatus, STATUS_POLL_MS);
    if (_running) startTick(); else startAccountPoll();
  }

  function onLeave() {
    if (!_active) return;
    _active = false;
    if (_statusTimer) { clearInterval(_statusTimer); _statusTimer = null; }
    stopAccountPoll();
    stopTick();
    closeTokenModal();
    if (_closeStopModal) _closeStopModal();
  }

  window.DELTA = { onEnter, onLeave };
})();
