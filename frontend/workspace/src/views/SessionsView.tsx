import { Icon, Empty, Pending } from "../ui";
import { api, usd, short, clock } from "../data";
import { strategies, phaseText, stateText } from "../config";
import type { SessionsProps } from "../viewTypes";
export function SessionsView({
  owner,
  metrics,
  stale,
  filtered,
  filter,
  setFilter,
  now,
  w,
  navigate,
  setStop,
  setSelected,
  setNotice,
  setWalletModal,
}: SessionsProps) {
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">ACTIVITÉ DU COMPTE</span>
          <h1>Vos sessions.</h1>
        </div>
        <button
          className="button primary compact"
          onClick={() => navigate("run")}
        >
          <Icon name="run" size={16} />
          Nouvelle session
        </button>
      </div>
      {owner ? (
        <>
          <div className="metrics-grid">
            {[
              ["Volume exécuté", usd(metrics?.volume_usd), "chart"],
              ["PnL réalisé net", usd(metrics?.net_pnl), "target"],
              ["Fills", metrics?.fills ?? "—", "auto"],
              ["Coût / 1M", usd(metrics?.cost_per_1m), "info"],
            ].map(([label, value, icon]) => (
              <div className="metric panel" key={label}>
                <span className="metric-label">
                  <Icon name={String(icon)} size={17} />
                  {label}
                </span>
                <strong
                  className={
                    label === "PnL réalisé net" && (metrics?.net_pnl || 0) < 0
                      ? "negative"
                      : ""
                  }
                >
                  {stale ? "—" : value}
                </strong>
              </div>
            ))}
          </div>
          <div className="list-toolbar">
            <div className="segments">
              {[
                ["active", "En cours"],
                ["closed", "Terminées"],
                ["all", "Toutes"],
              ].map(([id, title]) => (
                <button
                  key={id}
                  onClick={() => setFilter(id)}
                  className={filter === id ? "active" : ""}
                >
                  {title}
                </button>
              ))}
            </div>
            <span>
              {filtered.length} session{filtered.length > 1 ? "s" : ""}
            </span>
          </div>
          {filtered.length ? (
            <div className="session-list">
              {filtered.map((s) => (
                <article
                  className={
                    "panel session-card " + (s.running ? "is-running" : "")
                  }
                  key={s.id}
                >
                  <div className="session-card-head">
                    <div className="session-title">
                      <span className="session-symbol">
                        <Icon
                          name={
                            s.strategy === "outcome"
                              ? "auto"
                              : s.strategy === "mid"
                                ? "target"
                                : "book"
                          }
                          size={22}
                        />
                      </span>
                      <div>
                        <div className="session-tags">
                          <span>
                            {s.strategy === "outcome"
                              ? "AUTO OUTCOME"
                              : s.strategy.toUpperCase()}
                          </span>
                          <span>Maker</span>
                        </div>
                        <h2>
                          {s.name}
                          {s.config.outcome_side && (
                            <small>
                              {" "}
                              · {s.config.outcome_side === "yes" ? "Yes" : "No"}
                            </small>
                          )}
                        </h2>
                      </div>
                    </div>
                    <span className={"state-badge " + (s.running ? "on" : "")}>
                      <i />
                      {s.stale || stale
                        ? "Données retardées"
                        : stateText[s.state] || phaseText[s.phase] || s.phase}
                    </span>
                  </div>
                  <div className="session-metrics">
                    <div>
                      <span>Volume</span>
                      <strong>{usd(s.volume_usd)}</strong>
                    </div>
                    <div>
                      <span>Fills</span>
                      <strong>{s.fills ?? "—"}</strong>
                    </div>
                    <div>
                      <span>Net réalisé</span>
                      <strong
                        className={
                          (s.net_pnl || 0) < 0 ? "negative" : "positive"
                        }
                      >
                        {usd(s.net_pnl)}
                      </strong>
                    </div>
                    <div>
                      <span>Durée</span>
                      <strong>
                        {clock(
                          s.duration_sec +
                            (s.running && !stale
                              ? (now - w.received) / 1000
                              : 0),
                        )}
                      </strong>
                    </div>
                  </div>
                  <div className="session-footer">
                    <span>
                      {usd(s.config.order_size_usd || s.config.order_usd)} /
                      ordre
                      {s.rotations > 0 && " · " + s.rotations + " rotations"}
                    </span>
                    <div>
                      <button
                        className="button subtle"
                        onClick={() => setSelected(s)}
                      >
                        Détails
                        <Icon name="arrow" size={14} />
                      </button>
                      {s.running && (
                        <button
                          className="button danger small"
                          onClick={() => setStop(s)}
                        >
                          <Icon name="stop" size={13} />
                          Arrêter
                        </button>
                      )}
                      {!s.running && !s.cleanup_confirmed && (
                        <button
                          className="button secondary small"
                          onClick={async () => {
                            try {
                              await api("/reconcile", {});
                              w.refresh();
                            } catch (e: any) {
                              setNotice(e.message);
                            }
                          }}
                        >
                          Vérifier l’arrêt
                        </button>
                      )}
                    </div>
                  </div>
                  {s.reason && (
                    <p className="session-reason">
                      <Icon name="info" size={14} />
                      {s.reason}
                    </p>
                  )}
                </article>
              ))}
            </div>
          ) : (
            <Empty
              title={
                filter === "active"
                  ? "Tout est prêt pour commencer."
                  : "Aucune session ici."
              }
              text={
                filter === "active"
                  ? "Vos sessions apparaîtront ici dès leur lancement."
                  : "Les sessions vérifiées restent accessibles dans cet espace."
              }
              action={
                <button
                  className="button secondary"
                  onClick={() => navigate("run")}
                >
                  Préparer une session
                  <Icon name="arrow" size={16} />
                </button>
              }
            />
          )}
        </>
      ) : (
        <Empty
          icon="wallet"
          title="Votre activité, au même endroit."
          text="Connectez votre wallet pour retrouver vos sessions et leurs résultats."
          action={
            <button
              className="button primary"
              onClick={() => setWalletModal(true)}
            >
              Connecter mon wallet
            </button>
          }
        />
      )}
    </>
  );
}
