import { strategies, nav, initial } from "./config";
import { RunView } from "./views/RunView";

import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { api, usd, short, clock } from "./data";
import { useWorkspace } from "./useWorkspace";
import { signAgentApproval } from "./authorization";
import { Icon, Modal, Pending } from "./ui";
import { FeedbackButton } from "./components/FeedbackButton";
import type { Draft, View, Session, Agent, Operation } from "./types";
import { NavDock } from "./components/layout";
import { Kbd } from "./components/fields";
import { AlertDialog, useMobile } from "./components/Dialog";
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
  const [view, setView] = useState<View>(
    (location.hash.slice(1) as View) || "run",
  );
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
  const [selected, setSelected] = useState<Session | null>(null),
    [stop, setStop] = useState<Session | null>(null);
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
  const operation = useRef<string | null>(null);
  const [pendingOperation, setPendingOperation] = useState<Operation | null>(
    null,
  );
  const owner = w.identity?.wallet;
  const sessions = w.snapshot?.sessions || [];
  const metrics = w.snapshot?.metrics;
  const stale =
    !!owner &&
    (!w.received ||
      now - w.received > 6000 ||
      now - (w.snapshot?.published_at || 0) * 1000 > 6000);
  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
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
    setStop(null);
    setScanner(null);
    setMarkets([]);
    setPendingOperation(null);
    operation.current = null;
    setHydrated(false);
    if (owner) {
      try {
        const saved = JSON.parse(
          localStorage.getItem("zeus-draft:" + owner) || "null",
        );
        setDraft(saved ? { ...initial, ...saved } : initial);
        const pending = sessionStorage.getItem("zeus-command:" + owner);
        if (pending) {
          operation.current = pending;
          api<Operation>("/commands/" + pending)
            .then(setPendingOperation)
            .catch(() => {});
        }
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
    setScanner(null);
    if (!owner || draft.strategy !== "outcome") return;
    let alive = true;
    const scan = () =>
      api<any>(
        `/scanner?side=${draft.side}&order_usd=${draft.order_usd}&spread_bps=${draft.spread_bps}`,
      )
        .then((d) => {
          if (alive) setScanner(d);
        })
        .catch(() => {});
    const wait = setTimeout(scan, 350),
      timer = window.setInterval(scan, 10000);
    return () => {
      alive = false;
      clearTimeout(wait);
      clearInterval(timer);
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
  const preview = async () => {
    if (!owner) {
      setWalletModal(true);
      return;
    }
    if ((w.balance?.agent?.status ?? w.identity?.agent?.status) !== "ready") {
      navigate("account");
      return;
    }
    setBusy("preview");
    setActionErrors((old) => ({ ...old, run: "" }));
    setNotice("");
    try {
      setReview(await api("/sessions/preview", draft));
    } catch (e: any) {
      setActionErrors((old) => ({ ...old, run: e.message }));
    } finally {
      setBusy("");
    }
  };
  const launch = async () => {
    if (!review || !owner || busy) return;
    setBusy("launch");
    const key = crypto.randomUUID();
    operation.current = key;
    sessionStorage.setItem("zeus-command:" + owner, key);
    try {
      const result = await api<Operation>("/sessions", review.config, {
        "Idempotency-Key": key,
      });
      setPendingOperation(result);
      setReview(null);
      if (result.phase === "confirmed") {
        sessionStorage.removeItem("zeus-command:" + owner);
        operation.current = null;
        setPendingOperation(null);
        navigate("sessions");
        setNotice("Session lancée.");
        w.refresh();
      } else
        setNotice(
          result.result.message ||
            "Lancement non confirmé. Vérifiez la commande avant de réessayer.",
        );
    } catch {
      setReview(null);
      setPendingOperation({ command: key, phase: "starting", result: {} });
      setNotice("Résultat en attente. La commande ne sera pas renvoyée.");
    } finally {
      setBusy("");
    }
  };
  const verifyCommand = async () => {
    if (!operation.current) return;
    setBusy("verify");
    try {
      const result = await api<Operation>("/commands/" + operation.current);
      setPendingOperation(result);
      w.refresh();
      if (result.phase === "confirmed") {
        sessionStorage.removeItem("zeus-command:" + owner);
        operation.current = null;
        setPendingOperation(null);
        navigate("sessions");
        setNotice("Session retrouvée.");
      }
    } catch (e: any) {
      setNotice(e.message);
    } finally {
      setBusy("");
    }
  };
  const clearCommand = () => {
    if (!owner || pendingOperation?.phase !== "rejected") return;
    sessionStorage.removeItem("zeus-command:" + owner);
    operation.current = null;
    setPendingOperation(null);
  };
  const stopSession = async () => {
    if (!stop) return;
    setBusy("stop");
    setActionErrors((old) => ({ ...old, stop: "" }));
    try {
      await api("/sessions/" + encodeURIComponent(stop.id) + "/stop", {});
      setStop(null);
      setSelected(null);
      setNotice("Arrêt vérifié.");
      w.refresh();
    } catch (e: any) {
      setActionErrors((old) => ({ ...old, stop: e.message }));
      w.refresh();
    } finally {
      setBusy("");
    }
  };
  const reconnect = () => {
    w.refresh();
    if (!owner) void w.loadIdentity();
  };
  const ready =
    (w.balance?.agent?.status ?? w.identity?.agent?.status) === "ready";
  const filtered = sessions.filter(
    (s) =>
      filter === "all" ||
      (filter === "active"
        ? s.running || !s.cleanup_confirmed
        : !s.running && s.cleanup_confirmed),
  );
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
    pendingOperation,
    busy,
    navigate,
    setShowHelp,
    actionError: view === "account" ? actionErrors.account : actionErrors.run,
    loading: !!owner && !w.snapshot,
    metrics,
    stale,
    filtered,
    filter,
    setFilter,
    now,
    setStop,
    setSelected,
    setNotice,
    setWalletModal,
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

  return (
    <>
      <a className="skip" href="#main">
        Aller au contenu
      </a>
      <header className="header">
        <a className="brand" href="#run" aria-label="Zeus, accueil">
          <span className="brand-mark">Z</span>
          <span>
            Zeus<span className="brand-sub">TRADING WORKSPACE</span>
          </span>
        </a>
        <NavDock view={view} active={metrics?.active || 0} />
        <div className="header-actions">
          <button
            className={"live " + (owner && !stale ? "is-live" : "")}
            onClick={reconnect}
            title="Actualiser les données"
          >
            <i />
            {owner
              ? stale
                ? "Flux retardé"
                : w.connection === "live"
                  ? "Live"
                  : "Actualisation"
              : "Mainnet"}
          </button>
          <button
            className="icon-btn search-launch"
            aria-label="Ouvrir la recherche"
            onClick={() => setSearchOpen(true)}
          >
            <Icon name="search" size={19} />
            <Kbd>/</Kbd>
          </button>
          {owner ? (
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
        {pendingOperation && (
          <div className="command-alert">
            <div>
              <strong>Lancement à vérifier</strong>
              <p>
                {pendingOperation.result.message ||
                  "Le résultat de cette commande est en attente."}
              </p>
            </div>
            <button
              className="button secondary"
              onClick={verifyCommand}
              disabled={!!busy}
            >
              Vérifier
            </button>
            {pendingOperation.phase === "rejected" && (
              <button className="button subtle" onClick={clearCommand}>
                Terminer la vérification
              </button>
            )}
          </div>
        )}
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
            title="Vérifiez votre session."
            locked={!!busy}
            onClose={() => !busy && setReview(null)}
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
                <span>Hyperliquid Mainnet · Ordres réels</span>
              </div>
            </div>
            <dl className="summary-list">
              <div>
                <dt>Compte</dt>
                <dd>{short(review.account)}</dd>
              </div>
              <div>
                <dt>Montant par ordre</dt>
                <dd>{usd(review.config.order_usd)}</dd>
              </div>
              <div>
                <dt>Plafond de position</dt>
                <dd>
                  {review.config.strategy === "outcome"
                    ? usd(review.config.position_cap)
                    : "Limites du moteur"}
                </dd>
              </div>
              <div>
                <dt>Marché</dt>
                <dd>
                  {review.config.strategy === "outcome"
                    ? "Scanner Auto · " + review.config.side
                    : review.config.market}
                </dd>
              </div>
              <div>
                <dt>Solde spot disponible</dt>
                <dd>{usd(review.usdc_available)}</dd>
              </div>
              {review.config.strategy === "outcome" && (
                <>
                  <div>
                    <dt>Arrêt des achats</dt>
                    <dd>
                      {review.config.duration_sec
                        ? clock(review.config.duration_sec)
                        : review.config.target_volume_usd
                          ? usd(review.config.target_volume_usd) + " de volume"
                          : "Manuel"}
                    </dd>
                  </div>
                  <div>
                    <dt>Seuil de perte réalisée</dt>
                    <dd>{usd(review.config.stop_loss_usd)}</dd>
                  </div>
                </>
              )}
            </dl>
            <p className="hint">
              Les quantités Outcome sont arrondies à des tokens entiers. Le
              montant réel peut légèrement dépasser la taille demandée.
            </p>
            <FeedbackButton
              className="button primary full"
              onClick={launch}
              disabled={!!busy}
              loading={busy === "launch"}
              loadingLabel="Lancement en cours"
            >
              Lancer la session
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

      {owner && stop && (
        <AlertDialog
          title="Arrêter cette session ?"
          onClose={() => !busy && setStop(null)}
          locked={!!busy}
        >
          <p className="modal-description">{stop.name}</p>
          <p>
            Le moteur annule ses ordres et applique sa procédure de sortie.
            L’arrêt reste en cours jusqu’à sa vérification.
          </p>
          {actionErrors.stop && <InlineAlert>{actionErrors.stop}</InlineAlert>}
          <div className="dialog-actions">
            <button
              className="button secondary"
              onClick={() => setStop(null)}
              disabled={!!busy}
            >
              Continuer la session
            </button>
            <FeedbackButton
              className="button danger"
              onClick={stopSession}
              disabled={!!busy}
              loading={busy === "stop"}
              loadingLabel="Arrêt en cours"
            >
              Confirmer l’arrêt
            </FeedbackButton>
          </div>
        </AlertDialog>
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
