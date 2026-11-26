import { strategies, nav, initial } from "./config";
import { RunView } from "./views/RunView";
import { LandingView } from "./views/LandingView";

import { lazy, Suspense, useEffect, useState, useRef } from "react";
import { api, usd, short } from "./data";
import { useWorkspace } from "./useWorkspace";
import { signAgentApproval } from "./authorization";
import { Icon, Modal, Pending } from "./ui";
import { FeedbackButton } from "./components/FeedbackButton";
import type { Draft, View, Session, Agent, Operation, Snapshot } from "./types";
import { resolveStopSnapshot } from "./presentation/stop";
import { activeSessionCount, sessionVisible } from "./presentation/session";
import { launchPayload, formBlockReason } from "./presentation/launch";
import { useI18n } from "./i18n";
import { NavDock } from "./components/layout";
import { Kbd } from "./components/fields";
import { useMobile } from "./components/Dialog";
import { InlineAlert, Notification, Skeleton } from "./components/feedback";

const SessionsView = lazy(() =>
  import("./views/SessionsView").then((m) => ({ default: m.SessionsView })),
);
const AccountView = lazy(() =>
  import("./views/AccountView").then((m) => ({ default: m.AccountView })),
);
const ProfileDropdown = lazy(() =>
  import("./components/workspace/WalletPanel").then((m) => ({
    default: m.ProfileDropdown,
  })),
);
const GlobalSearch = lazy(() =>
  import("./components/workspace/GlobalSearch").then((m) => ({
    default: m.GlobalSearch,
  })),
);
const SessionDetails = lazy(() =>
  import("./components/workspace/SessionDetails").then((m) => ({
    default: m.SessionDetails,
  })),
);
const PerformanceView = lazy(() =>
  import("./views/PerformanceView").then((module) => ({
    default: module.PerformanceView,
  })),
);
export default function App() {
  const w = useWorkspace();
  const [view, setView] = useState<View>(() => {
    const target = location.hash.slice(1);
    return nav.some((n) => n.id === target) ? (target as View) : "run";
  });
  const [walletModal, setWalletModal] = useState(false),
    [walletBusy, setWalletBusy] = useState("");
  const [walletError, setWalletError] = useState(""),
    [draft, setDraft] = useState<Draft>(initial),
    [hydrated, setHydrated] = useState(false);
  const [markets, setMarkets] = useState<any[]>([]),
    [scanner, setScanner] = useState<any>(null),
    [search, setSearch] = useState("");
  const [review, setReview] = useState<any>(null),
    [busy, setBusy] = useState(""),
    [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<Session | null>(null);
  const [pendingStops, setPendingStops] = useState<Record<string, number>>({});
  const stopRequests = useRef(new Set<string>());
  const [scrolled, setScrolled] = useState(false);
  const [filter, setFilter] = useState("active"),
    [period, setPeriod] = useState(7),
    [now, setNow] = useState(Date.now());
  const [searchOpen, setSearchOpen] = useState(false);
  const [actionErrors, setActionErrors] = useState({
    run: "",
    account: "",
    stop: "",
  });
  const mobile = useMobile();
  const owner = w.identity?.wallet;
  const currentOwner = useRef(owner);
  currentOwner.current = owner;
  const sessions = w.snapshot?.sessions || [];
  const { locale, setLocale, t } = useI18n();
  const metrics = w.snapshot?.metrics;
  // Compare only client-side clocks: published_at comes from the server, and
  // any client/server skew would otherwise mark a healthy stream as delayed
  // forever. A stalled stream already stops refreshing `received`.
  const stale = !!owner && (!w.received || now - w.received > 6000);
  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, []);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => {
    const change = () => {
      const target = location.hash.slice(1);
      setView(nav.some((n) => n.id === target) ? (target as View) : "run");
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  function navigate(v: View) {
    location.hash = v;
    setView(v);
    window.scrollTo({
      top: 0,
      behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "instant"
        : "smooth",
    });
  }
  useEffect(() => {
    setSearchOpen(false);
    setNotice("");
    setActionErrors({ run: "", account: "", stop: "" });
    setReview(null);
    setSelected(null);
    setPendingStops({});
    stopRequests.current.clear();
    setScanner(null);
    setMarkets([]);
    setHydrated(false);
    if (owner) {
      try {
        const saved = JSON.parse(
          localStorage.getItem("zeus-draft:" + owner) || "null",
        );
        setDraft(saved ? { ...initial, ...saved, side: null } : initial);
      } catch {
        setDraft(initial);
      }
    } else setDraft(initial);
    setHydrated(true);
  }, [owner]);
  useEffect(() => {
    if (owner && hydrated)
      try {
        localStorage.setItem("zeus-draft:" + owner, JSON.stringify(draft));
      } catch {}
  }, [draft, owner, hydrated]);
  useEffect(() => {
    if (!owner) return;
    let alive = true;
    setMarkets([]);
    api<any>("/markets?strategy=" + draft.strategy)
      .then((data) => {
        if (alive) setMarkets(data.tokens || data.outcomes || []);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [owner, draft.strategy]);
  useEffect(() => {
    if (!owner || draft.strategy !== "outcome") {
      setScanner(null);
      return;
    }
    if (!draft.side) {
      // No default side: do not observe the wrong Outcome coin before the
      // user explicitly chooses Yes or No.
      setScanner(null);
      return;
    }
    // Never poll limits the server will reject: a half-typed amount/spread
    // used to raise 422 and paint a false "Flux retardé" connection warning.
    const valid =
      Number.isFinite(draft.order_usd) &&
      draft.order_usd >= 10.5 &&
      Number.isFinite(draft.spread_bps) &&
      draft.spread_bps > 0 &&
      draft.spread_bps <= 100;
    if (!valid) return;
    let alive = true;
    const timers = { full: 0, observed: 0 };
    const query = `side=${draft.side}&order_usd=${draft.order_usd}&spread_bps=${draft.spread_bps}`;
    const refresh = async (observed: boolean) => {
      try {
        if (!document.hidden) {
          const value = await api<any>(`/scanner${observed ? "/observed" : ""}?${query}`);
          if (alive) setScanner((old: any) => observed
            ? { ...old, ...value, observation_error: false }
            : { ...old, ...value });
        }
      } catch (e: any) {
        // Only a transport/server failure is a feed-health signal. A 4xx is a
        // draft problem, and an aborted request belongs to an identity change.
        if (
          alive &&
          observed &&
          e?.name !== "AbortError" &&
          (typeof e?.status !== "number" || e.status >= 500)
        )
          setScanner((old: any) => ({ ...old, observation_error: true }));
      } finally {
        if (alive) timers[observed ? "observed" : "full"] = window.setTimeout(() => refresh(observed), observed ? 2000 : 5000);
      }
    };
    timers.full = window.setTimeout(() => refresh(false), 350);
    timers.observed = window.setTimeout(() => refresh(true), 450);
    return () => {
      alive = false;
      Object.values(timers).forEach(clearTimeout);
    };
  }, [owner, draft.strategy, draft.side, draft.order_usd, draft.spread_bps]);
  useEffect(() => {
    let prefix = 0;
    const keyboard = (event: KeyboardEvent) => {
      const element = event.target as HTMLElement;
      if (
        element.closest(
          "input, textarea, select, [contenteditable=true], [role=combobox]",
        ) ||
        event.metaKey ||
        event.ctrlKey ||
        event.altKey ||
        document.querySelector("dialog[open]")
      )
        return;
      if (event.key === "/") {
        event.preventDefault();
        setSearchOpen(true);
      }
      if (Date.now() - prefix < 900) {
        const target = (
          { l: "run", s: "sessions", p: "performance", c: "account" } as Record<
            string,
            View
          >
        )[event.key];
        if (target) {
          event.preventDefault();
          navigate(target);
        }
      }
      prefix = event.key === "g" ? Date.now() : 0;
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, []);
  const update = (key: keyof Draft, value: string | number | null) => {
    setActionErrors((old) => ({ ...old, run: "" }));
    setDraft((old) => ({ ...old, [key]: value }));
    setReview(null);
    setNotice("");
  };
  const connect = async (wallet: any) => {
    setWalletBusy(wallet?.info?.name || "WalletConnect");
    setWalletError("");
    if (!wallet) setWalletModal(false);
    try {
      const provider =
        wallet?.provider ||
        (await (
          await import("./walletconnect")
        ).connectWalletConnect(w.config?.walletconnect_project_id || ""));
      await w.connect(provider);
      setWalletModal(false);
      setNotice("Wallet connecté. Votre espace est prêt.");
    } catch (e: any) {
      setWalletModal(true);
      setWalletError(
        e.code === 4001
          ? "Signature refusée. Vous pouvez réessayer."
          : e.message || "La connexion a échoué.",
      );
    } finally {
      setWalletBusy("");
    }
  };
  const authorize = async () => {
    if (!w.identity) {
      setWalletModal(true);
      return;
    }
    setBusy("agent");
    setActionErrors((old) => ({ ...old, account: "" }));
    setNotice("");
    try {
      let provider = w.provider.current;
      if (!provider) {
        const wallet = w.wallets[0];
        if (!wallet) {
          setWalletModal(true);
          return;
        }
        await w.connect(wallet.provider);
        provider = wallet.provider;
      }
      const prepared = await api<Agent>("/agent/prepare", {});
      const signature = await signAgentApproval(
        provider,
        w.identity.wallet,
        prepared.typed_data,
      );
      const authorization = await api<Agent>("/agent/approve", { signature });
      await w.loadIdentity();
      w.refresh();
      setNotice(
        authorization.status === "ready"
          ? "Autorisation vérifiée. Vous pouvez lancer votre session."
          : "Autorisation envoyée, confirmation en attente. Actualisez le statut du compte.",
      );
    } catch (e: any) {
      setActionErrors((old) => ({
        ...old,
        account:
          e.code === 4001
            ? "Autorisation refusée. Aucun bot lancé."
            : e.message || "Autorisation non confirmée. Vérifiez le compte.",
      }));
    } finally {
      setBusy("");
    }
  };
  const preview = () => {
    if (!owner) {
      setWalletModal(true);
      return;
    }
    if ((w.balance?.agent?.status ?? w.identity?.agent?.status) !== "ready") {
      navigate("account");
      return;
    }
    if (formBlockReason(draft, w.balance?.usdc_available ?? null)) {
      setActionErrors((old) => ({ ...old, run: "Complétez les paramètres." }));
      return;
    }
    setActionErrors((old) => ({ ...old, run: "" }));
    setNotice("");
    setReview({
      config: launchPayload(draft),
      account: owner,
      usdc_available: w.balance?.usdc_available ?? null,
    });
  };
  const launch = async () => {
    if (!review || !owner || busy) return;
    setBusy("launch");
    const key = crypto.randomUUID();
    try {
      const result = await api<Operation>("/sessions", review.config, {
        "Idempotency-Key": key,
      });
      setReview(null);
      if (result.phase === "confirmed") {
        navigate("sessions");
        setNotice("Session lancée.");
        w.refresh();
      } else
        setNotice(
          result.result.message ||
            "Lancement non confirmé. Vérifiez la session avant de réessayer.",
        );
    } catch (e: any) {
      // A definitive 4xx rejection (validation, CSRF, conflict) means nothing
      // was launched: keep the review open and show the reason next to it.
      if (
        typeof e?.status === "number" &&
        e.status >= 400 &&
        e.status < 500 &&
        e.status !== 408
      ) {
        setActionErrors((old) => ({ ...old, run: e.message }));
      } else {
        setReview(null);
        setNotice("Résultat en attente. Vérifiez la session avant de réessayer.");
      }
    } finally {
      setBusy("");
    }
  };
  const stopSession = async (session: Session) => {
    if (stopRequests.current.has(session.id)) return;
    stopRequests.current.add(session.id);
    setPendingStops((old) => ({ ...old, [session.id]: Date.now() }));
    setSelected(null);
    setNotice("Arrêt demandé : annulation des ordres et fermeture au marché.");
    setActionErrors((old) => ({ ...old, stop: "" }));
    try {
      const snapshot = await api<Snapshot>(
        "/sessions/" + encodeURIComponent(session.id) + "/stop",
        { mode: "cancel_and_market" },
      );
      if (currentOwner.current !== owner) return;
      // Use the command's fresh snapshot immediately; the live stream may lag
      // behind the stop and otherwise re-show an already closed session.
      w.applySnapshot(snapshot);
      const resolution = resolveStopSnapshot(snapshot, session.id);
      // A 200 only records the request; never claim a verified stop until the
      // returned snapshot proves it.
      setNotice(resolution.confirmed ? "Arrêt et clôture confirmés." : "Arrêt demandé : clôture au marché en cours.");
      w.refresh();
    } catch (e: any) {
      if (currentOwner.current !== owner) return;
      stopRequests.current.delete(session.id);
      setPendingStops((old) => { const next = { ...old }; delete next[session.id]; return next; });
      setNotice("Arrêt non confirmé : " + e.message);
      setActionErrors((old) => ({ ...old, stop: e.message }));
      w.refresh();
    }
  };

  useEffect(() => {
    if (!Object.keys(pendingStops).length) return;
    const next = { ...pendingStops };
    let changed = false;
    for (const [id, started] of Object.entries(pendingStops)) {
      const resolution = resolveStopSnapshot(w.snapshot, id);
      if (resolution.confirmed || now-started >= 45000) {
        delete next[id];
        stopRequests.current.delete(id);
        changed = true;
        if (!resolution.confirmed) {
          setNotice("Arrêt non confirmé après 45 s : la session reste listée tant que la clôture n’est pas vérifiée.");
        }
      }
    }
    if (changed) setPendingStops(next);
  }, [w.snapshot, now, pendingStops]);

  const reconnect = () => {
    w.refresh();
    if (!owner) void w.loadIdentity();
  };
  const ready =
    (w.balance?.agent?.status ?? w.identity?.agent?.status) === "ready";
  const filtered = sessions.filter(
    (s) => !pendingStops[s.id] && sessionVisible(s, filter),
  );
  const activeCount = activeSessionCount(sessions, pendingStops);
  const history = sessions.filter(
    (s) => s.started >= now / 1000 - period * 86400,
  );
  const sum = (field: "volume_usd" | "fees" | "net_pnl" | "fills") =>
    w.snapshot &&
    history.every((s) => s.accounting_complete && s[field] != null)
      ? history.reduce((n, s) => n + (s[field] || 0), 0)
      : null;
  const volume = sum("volume_usd"),
    net = sum("net_pnl");
  const [showHelp, setShowHelp] = useState(false);
  const viewProps = {
    w,
    draft,
    update,
    setDraft,
    setReview,
    markets,
    search,
    setSearch,
    scanner,
    owner,
    ready,
    preview,
    busy,
    navigate,
    setShowHelp,
    actionError: review
      ? ""
      : view === "account"
        ? actionErrors.account
        : actionErrors.run,
    loading: !!owner && !w.snapshot,
    metrics,
    stale,
    filtered,
    filter,
    setFilter,
    now,
    setStop: (session: Session | null) => { if (session) void stopSession(session); },
    setSelected,
    setNotice,
    setWalletModal,
    stopError: actionErrors.stop,
    period,
    setPeriod,
    history,
    volume,
    net,
    sum,
    authorize,
  };
  const detail = selected
    ? sessions.find((s) => s.id === selected.id) || selected
    : null;
  const reviewOutcome = review?.config.strategy === "outcome";
  const reviewSide =
    review?.config.side === "yes"
      ? "Yes"
      : review?.config.side === "no"
        ? "No"
        : "Non sélectionné";

  return (
    <>
      <a className="skip" href="#main">
        {t("header.skipToContent")}
      </a>
      <header className={"header" + (scrolled ? " is-scrolled" : "")}>
        <a className="brand" href="#run" aria-label={t("header.brandHome")}>
          <span className="brand-mark">Z</span>
          <span>
            Zeus<span className="brand-sub">{t("header.brandSub")}</span>
          </span>
        </a>
        {owner && <NavDock view={view} active={activeCount} />}
        <div className="header-actions">
          <button
            type="button"
            className="locale-toggle"
            onClick={() => setLocale(locale === "fr" ? "en" : "fr")}
            aria-label={
              locale === "fr" ? "Switch to English" : "Passer en français"
            }
            title={locale === "fr" ? "English" : "Français"}
          >
            {locale.toUpperCase()}
          </button>
          {owner ? (
            <>
              <button
                className={"live " + (!stale ? "is-live" : "")}
                onClick={reconnect}
                title={t("header.refreshData")}
              >
                <i />
                {stale ? t("header.refreshing") : t("header.live")}
              </button>
              <button
                className="icon-btn search-launch"
                aria-label={t("header.openSearch")}
                onClick={() => setSearchOpen(true)}
              >
                <Icon name="search" size={19} />
                <span className="search-label">{t("header.search")}</span>
                <Kbd>/</Kbd>
              </button>
              <Suspense
                fallback={
                  <button
                    className="button wallet-connected"
                    onClick={() => navigate("account")}
                  >
                    {short(owner)}
                  </button>
                }
              >
                <ProfileDropdown
                  address={owner}
                  onAccount={() => navigate("account")}
                  onLogout={w.logout}
                  notify={setNotice}
                />
              </Suspense>
            </>
          ) : (
            <button
              className="button primary compact"
              onClick={() => setWalletModal(true)}
            >
              <Icon name="wallet" size={17} />
              Connecter
            </button>
          )}
        </div>
      </header>
      <main id="main" className={"main view-" + view}>
        {w.error && (
          <InlineAlert tone="warning" onClose={() => w.setError("")}>
            {w.error}
          </InlineAlert>
        )}
        {!owner ? (
          <LandingView
            onConnect={() => setWalletModal(true)}
            onHelp={() => setShowHelp(true)}
          />
        ) : (
          <>
            {view === "run" && <RunView {...viewProps} />}
            {view === "sessions" && (
              <Suspense fallback={<Skeleton />}>
                <SessionsView {...viewProps} />
              </Suspense>
            )}
            {view === "performance" && (
              <Suspense fallback={<Skeleton />}>
                <PerformanceView {...viewProps} />
              </Suspense>
            )}
            {view === "account" && (
              <Suspense fallback={<Skeleton />}>
                <AccountView {...viewProps} />
              </Suspense>
            )}
          </>
        )}
        <footer className="page-footer">
          <span>
            <span className="tiny-brand">Z</span>Zeus Workspace
          </span>
          <span>
            HIP-3 <b>·</b> HIP-4
          </span>
          <button onClick={() => setShowHelp(true)}>
            Aide
            <Icon name="info" size={13} />
          </button>
        </footer>
      </main>
      {notice && (
        <Notification message={notice} onClose={() => setNotice("")} />
      )}
      {searchOpen && (
        <Suspense
          fallback={
            <Modal
              title="Rechercher dans Zeus"
              onClose={() => setSearchOpen(false)}
            >
              <Pending label="Chargement de la recherche" />
            </Modal>
          }
        >
          <GlobalSearch
            key={owner || "public"}
            sessions={sessions}
            markets={draft.strategy === "outcome" ? [] : markets}
            onClose={() => setSearchOpen(false)}
            onSelect={(result) => {
              setSearchOpen(false);
              if (result.session) {
                navigate("sessions");
                setSelected(result.session);
              } else if (result.market) {
                update("market", result.market);
                navigate("run");
              } else if (result.view) navigate(result.view);
            }}
          />
        </Suspense>
      )}
      {walletModal && (
        <Modal
          title="Votre wallet. Votre workspace."
          onClose={() => {
            if (walletBusy) w.cancelConnection();
            setWalletBusy("");
            setWalletModal(false);
          }}
        >
          <p className="modal-description">
            Connectez votre wallet, puis signez le message de connexion.
          </p>
          <div className="wallet-list">
            {w.wallets.map((wallet) => (
              <button
                className="wallet-option"
                key={wallet.info.uuid}
                disabled={!!walletBusy}
                onClick={() => connect(wallet)}
              >
                <span className="provider-icon">
                  <Icon name="wallet" />
                </span>
                <strong>{wallet.info.name}</strong>
                <span className="provider-label">Installé</span>
                <Icon name="arrow" size={17} />
              </button>
            ))}
            <button
              className="wallet-option"
              disabled={!!walletBusy || !w.config?.walletconnect_project_id}
              title={
                !w.config?.walletconnect_project_id
                  ? "Le serveur doit configurer ZEUS_WALLETCONNECT_PROJECT_ID."
                  : undefined
              }
              onClick={() => connect(null)}
            >
              <span className="provider-icon wc">
                <Icon name="link" />
              </span>
              <strong>WalletConnect</strong>
              <span className="provider-label">Mobile & QR</span>
              <Icon name="arrow" size={17} />
            </button>
          </div>
          {!w.wallets.length && (
            <p className="hint">
              Installez un wallet EVM comme Rabby ou MetaMask, ou utilisez
              WalletConnect lorsqu’il est configuré.
            </p>
          )}
          {!w.config?.walletconnect_project_id && (
            <p className="hint">
              WalletConnect n’est pas encore configuré sur ce serveur.
            </p>
          )}
          {walletBusy && (
            <div className="wallet-progress">
              <Pending label={"Continuez dans " + walletBusy} />
            </div>
          )}
          {walletError && (
            <p className="error-text" role="alert">
              {walletError}
            </p>
          )}
          <div className="modal-note">
            <Icon name="shield" size={15} />
            <span>
              La connexion ne lance aucun ordre et n’autorise pas le trading.
            </span>
          </div>
        </Modal>
      )}
      {owner &&
        review &&
        review.account?.toLowerCase() === owner.toLowerCase() && (
          <Modal
            title="Confirmer le lancement."
            onClose={() => {
              if (busy === "launch") {
                setNotice(
                  "Lancement en cours. Vérifiez Sessions avant de relancer.",
                );
              }
              setReview(null);
            }}
          >
            <div className="review-strategy">
              <span className="strategy-icon">
                <Icon
                  name={
                    strategies.find((s) => s.id === review.config.strategy)
                      ?.icon || "auto"
                  }
                />
              </span>
              <div>
                <h3>
                  {
                    strategies.find((s) => s.id === review.config.strategy)
                      ?.name
                  }
                </h3>
                <span>Ordres réels · Hyperliquid</span>
              </div>
            </div>
            <dl className="summary-list">
              <div>
                <dt>Montant</dt>
                <dd>{usd(review.config.order_usd)}</dd>
              </div>
              <div>
                <dt>Marché</dt>
                <dd>
                  {reviewOutcome
                    ? "Sélection Auto · " + reviewSide
                    : review.config.market}
                </dd>
              </div>
              {reviewOutcome && (
                <div>
                  <dt>Plafond</dt>
                  <dd>{usd(review.config.position_cap)}</dd>
                </div>
              )}
              <div>
                <dt>Solde</dt>
                <dd>{usd(review.usdc_available)}</dd>
              </div>
            </dl>
            {actionErrors.run && <InlineAlert>{actionErrors.run}</InlineAlert>}
            <FeedbackButton
              className="button primary full"
              onClick={launch}
              disabled={!!busy}
              loading={busy === "launch"}
              loadingLabel="Lancement"
            >
              Lancer
              <Icon name="run" size={17} />
            </FeedbackButton>
          </Modal>
        )}
      {owner && detail && (
        <Modal
          title="Détail de la session"
          variant={mobile ? "sheet" : "dialog"}
          onClose={() => setSelected(null)}
          wide
        >
          {stale && (
            <InlineAlert tone="warning">
              Données retardées. Les valeurs affichées sont le dernier état
              reçu.
            </InlineAlert>
          )}
          <Suspense fallback={<Pending label="Chargement du détail" />}>
            <SessionDetails
              key={detail.id}
              session={stale ? { ...detail, stale: true } : detail}
            />
          </Suspense>
        </Modal>
      )}

      {showHelp && (
        <Modal
          title="Trois étapes pour commencer."
          onClose={() => setShowHelp(false)}
        >
          <p className="keyboard-help">
            <Kbd>/</Kbd> Recherche <Kbd>g</Kbd> puis <Kbd>s</Kbd> Sessions{" "}
            <Kbd>Échap</Kbd> Fermer
          </p>
          <div className="help-steps">
            {[
              [
                "01",
                "Connectez votre wallet",
                "Une signature vérifie votre identité.",
              ],
              [
                "02",
                "Autorisez votre agent",
                "Une permission Hyperliquid distincte permet le trading.",
              ],
              [
                "03",
                "Préparez votre session",
                "Vérifiez le compte, le montant et les paramètres avant le lancement.",
              ],
            ].map(([n, t, d]) => (
              <div key={n}>
                <span className="step">{n}</span>
                <div>
                  <h3>{t}</h3>
                  <p>{d}</p>
                </div>
              </div>
            ))}
          </div>
          <div className="modal-note">
            <Icon name="info" size={16} />
            <span>
              Le coût par million utilise le PnL réalisé net rapporté au volume
              exécuté. Les positions ouvertes restent visibles dans Sessions.
            </span>
          </div>
        </Modal>
      )}
    </>
  );
}
