import { lazy, Suspense, useState } from "react";
import { Icon } from "../ui";
import { HaloInput, HaloField } from "../components/fields";
import { ResponsiveOptions } from "../components/Dialog";
import { SessionStepper } from "../components/layout";
import { DataTable } from "../components/DataTable";
import { LiveAmount } from "../components/LiveAmount";
import { InlineAlert } from "../components/feedback";
import { SegmentedControl } from "../components/SegmentedControl";
import { FeedbackButton } from "../components/FeedbackButton";
import { StickyLaunchBar } from "../components/StickyLaunchBar";
import { usd, short } from "../data";
import { strategies } from "../config";
import {
  amountError,
  amountWarning,
  capError,
  formBlockReason,
  orderMax,
  quickAmounts,
  sideLabel,
} from "../presentation/launch";
import type { RunProps } from "../viewTypes";
const MarketSelect = lazy(() =>
  import("../components/MarketSelect").then((m) => ({
    default: m.MarketSelect,
  })),
);

function scannerStatus(market: any) {
  if (market.auto_eligible) return "Prêt";
  const reason = String(market.eligibility_reason ?? "");
  if (reason.startsWith("Déjà en session")) return "Occupé";
  if (reason.startsWith("Arrêt non confirmé")) return "Arrêt non confirmé";
  if (reason.startsWith("Scan en attente") || reason.startsWith("Souscriptions"))
    return "En attente";
  if (reason.startsWith("Profondeur")) return "Profondeur";
  if (reason.startsWith("Spread")) return "Spread";
  if (reason.startsWith("Flux dans un seul sens")) return "Flux faible";
  if (reason.startsWith("Dernier trade")) return "Trade ancien";
  return reason || "Indisponible";
}

export function RunView({
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
  actionError,
}: RunProps) {
  const available = w.balance?.usdc_available ?? null;
  const amountIssue = amountError(draft, available);
  const warning = amountWarning(draft, available);
  const capIssue = capError(draft);
  const block = formBlockReason(draft, available);
  const quick = quickAmounts(draft, available);
  const [scannerOpen, setScannerOpen] = useState(false);
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Lancer, simplement.</h1>
        </div>
        <span className="environment">
          <i />
          Hyperliquid Mainnet
        </span>
      </div>
      <div className="launch-grid">
        <section
          className="panel launch-form"
          aria-label="Préparer une session"
        >
          <div className="section-heading">
            <span className="step">01</span>
            <h2>Stratégie</h2>
          </div>
          <div className="strategy-grid">
            {strategies.map((s) => (
              <button
                key={s.id}
                className={
                  "strategy-card " + (draft.strategy === s.id ? "selected" : "")
                }
                onClick={() => {
                  setDraft((d) => ({
                    ...d,
                    strategy: s.id,
                    market: "",
                    duration_sec: null,
                    target_volume_usd: null,
                  }));
                  setReview(null);
                }}
                aria-pressed={draft.strategy === s.id}
              >
                <div className="strategy-top">
                  <span className="strategy-icon">
                    <Icon name={s.icon} />
                  </span>
                  <span className="micro-label">{s.label}</span>
                </div>
                <h3>{s.name}</h3>
                <p>{s.description}</p>
                <div className="strategy-bottom">
                  <span>{s.detail}</span>
                  <span className="selection-check">
                    {draft.strategy === s.id && <Icon name="check" size={12} />}
                  </span>
                </div>
              </button>
            ))}
          </div>
          <div className="section-heading with-line">
            <span className="step">02</span>
            <h2>Paramètres</h2>
          </div>
          <div className="amount-section">
            <label htmlFor="order">Montant par ordre</label>
            <div className={"amount-input" + (amountIssue ? " has-error" : "")}>
              <span>$</span>
              <HaloInput
                id="order"
                type="number"
                inputMode="decimal"
                min="10.5"
                max={orderMax(draft, available) ?? undefined}
                step="0.01"
                value={draft.order_usd || ""}
                aria-invalid={!!amountIssue || undefined}
                aria-describedby="order-help order-error order-warning"
                onChange={(e) => update("order_usd", Number(e.target.value))}
              />
              <span className="amount-unit">USD</span>
            </div>
            <div className="amount-bottom">
              <span id="order-help">Minimum 10,50 $</span>
              <div
                className="quick-amounts"
                role="group"
                aria-label="Montants rapides"
              >
                {quick.length ? (
                  quick.map((item) => (
                    <button
                      key={item.label}
                      type="button"
                      onClick={() => update("order_usd", item.value)}
                      className={
                        Math.abs(draft.order_usd - item.value) < 0.005
                          ? "selected"
                          : ""
                      }
                      aria-label={
                        item.label === "MAX"
                          ? "Utiliser le montant maximal autorisé"
                          : `${item.label} du maximum autorisé`
                      }
                    >
                      {item.label}
                    </button>
                  ))
                ) : (
                  <span className="field-hint">Solde en cours de chargement…</span>
                )}
              </div>
            </div>
            {amountIssue && (
              <p id="order-error" className="error-text" role="alert">
                {amountIssue}
              </p>
            )}
            {warning && (
              <p id="order-warning" className="amount-warning" role="status">
                <Icon name="info" size={14} />
                {warning}
              </p>
            )}
          </div>
          {draft.strategy === "outcome" ? (
            <div className="field">
              <span className="field-label">Côté Outcome</span>
              <SegmentedControl
                label="Côté Outcome"
                value={draft.side ?? ""}
                options={[
                  { value: "yes", label: "Yes" },
                  { value: "no", label: "No" },
                ]}
                onValueChange={(side) =>
                  update("side", side as "yes" | "no")
                }
              />
            </div>
          ) : (
            <div className="field">
              <label htmlFor="market">Marché HIP-3</label>
              <HaloInput
                aria-label="Rechercher un marché"
                placeholder="Rechercher un marché…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <Suspense
                fallback={<p className="hint">Chargement de la sélection…</p>}
              >
                <MarketSelect
                  value={draft.market}
                  search={search}
                  onChange={(value) => update("market", value)}
                  options={markets.map((m) => ({
                    value: m.key,
                    label: m.symbol || m.name || m.key,
                  }))}
                />
              </Suspense>
              {!owner && (
                <span className="field-hint">
                  Connectez votre wallet pour charger les marchés.
                </span>
              )}
            </div>
          )}
          <ResponsiveOptions
            summary={
              <>
                <span>
                  <Icon name="target" size={17} />
                  Options de session
                </span>
                <span>
                  {draft.strategy === "outcome"
                    ? usd(draft.position_cap) + " max."
                    : draft.strategy === "mid"
                      ? draft.offset_bps + " bps"
                      : "Post-only"}
                  <Icon name="chevron" size={16} />
                </span>
              </>
            }
          >
            <div className="advanced-body">
              {draft.strategy === "outcome" ? (
                <div className="two-fields">
                  <HaloField
                    label="Plafond de position · USD"
                    error={capIssue || undefined}
                    description="Exposition maximale."
                  >
                    <HaloInput
                      type="number"
                      min="10.5"
                      value={draft.position_cap}
                      onChange={(e) =>
                        update("position_cap", Number(e.target.value))
                      }
                    />
                  </HaloField>
                  <HaloField label="Spread maximal · bps">
                    <HaloInput
                      type="number"
                      min="0.01"
                      max="100"
                      step="0.1"
                      value={draft.spread_bps}
                      onChange={(e) =>
                        update("spread_bps", Number(e.target.value))
                      }
                    />
                  </HaloField>
                </div>
              ) : draft.strategy === "mid" ? (
                <HaloField label="Distance au prix médian · bps">
                  <HaloInput
                    type="number"
                    min="0"
                    max="9999"
                    step="0.1"
                    value={draft.offset_bps}
                    onChange={(e) =>
                      update("offset_bps", Number(e.target.value))
                    }
                  />
                </HaloField>
              ) : (
                <p>
                  Le moteur cote au meilleur bid et au meilleur ask. Chaque
                  ordre reste soumis aux contrôles de fonds et de risque.
                </p>
              )}
              {draft.strategy === "outcome" && (
                <div className="session-target">
                  <span className="field-label">Fin de session</span>
                  <SegmentedControl
                    label="Fin de session"
                    value={
                      draft.duration_sec !== null
                        ? "duration"
                        : draft.target_volume_usd !== null
                          ? "volume"
                          : "manual"
                    }
                    options={[
                      { value: "manual", label: "Manuelle" },
                      { value: "duration", label: "Durée" },
                      { value: "volume", label: "Volume" },
                    ]}
                    onValueChange={(mode) => {
                      setDraft((d) => ({
                        ...d,
                        duration_sec: mode === "duration" ? 14400 : null,
                        target_volume_usd: mode === "volume" ? 1000 : null,
                      }));
                      setReview(null);
                    }}
                  />
                  {draft.duration_sec !== null && (
                    <HaloField label="Durée · minutes">
                      <HaloInput
                        type="number"
                        min="1"
                        max="43200"
                        value={draft.duration_sec / 60}
                        onChange={(e) =>
                          update("duration_sec", Number(e.target.value) * 60)
                        }
                      />
                    </HaloField>
                  )}
                  {draft.target_volume_usd !== null && (
                    <HaloField label="Volume exécuté cible · USD">
                      <HaloInput
                        type="number"
                        min="10"
                        value={draft.target_volume_usd}
                        onChange={(e) =>
                          update("target_volume_usd", Number(e.target.value))
                        }
                      />
                    </HaloField>
                  )}
                  <HaloField label="Seuil de perte réalisée · USD">
                    <HaloInput
                      type="number"
                      min="0.01"
                      step="0.01"
                      value={draft.stop_loss_usd}
                      onChange={(e) =>
                        update("stop_loss_usd", Number(e.target.value))
                      }
                    />
                  </HaloField>
                </div>
              )}
            </div>
          </ResponsiveOptions>
          {draft.strategy === "outcome" && owner && (
            <section className={"scanner" + (scannerOpen ? " open" : "")}>
              <button
                type="button"
                className="scanner-head"
                aria-expanded={scannerOpen}
                aria-controls="scanner-panel"
                onClick={() => setScannerOpen((open) => !open)}
              >
                <span>
                  <i className={scanner?.ready ? "green-dot" : "gold-dot"} />
                  Scanner
                </span>
                <span className="scanner-head-right">
                  <span className="mono">
                    {scanner?.observed_markets?.length != null
                      ? scanner.observed_markets.length +
                        " marchés · " +
                        scanner.observed_markets.filter((m: any) => m.auto_eligible).length +
                        " prêts"
                      : scanner?.ready
                        ? scanner.candidates.length + " prêts"
                        : scanner?.warming_up
                          ? "Observation…"
                          : "En attente"}
                  </span>
                  <Icon name="chevron" size={16} />
                </span>
              </button>
              {scannerOpen && (
                <div id="scanner-panel" className="scanner-body">
                  {!scanner?.ready && scanner?.reason && (
                    <InlineAlert tone="warning">{scanner.reason}</InlineAlert>
                  )}
                  <p className="scanner-freshness" aria-live="off">
                    {scanner?.observation_error || scanner?.observation_connected === false
                      ? "Flux retardé · dernières valeurs"
                      : scanner?.observed_at
                        ? `Actualisé à ${new Date(scanner.observed_at * 1000).toLocaleTimeString("fr-FR")}`
                        : "Connexion…"}
                  </p>
                  {!!(scanner?.observed_markets ?? scanner?.candidates)?.length && (
                    <DataTable<any>
                      caption="Marchés observés"
                      rows={(scanner.observed_markets ?? scanner.candidates)}
                      defaultSort={{ id: "rank", direction: "asc" }}
                      animateChanges
                      rowKey={(m) => m.market}
                      columns={[
                        {
                          id: "rank",
                          label: "#",
                          render: (m) =>
                            Number.isFinite(m.rank) ? (
                              <span className="scanner-rank">#{m.rank}</span>
                            ) : (
                              "—"
                            ),
                          sort: (m) => m.rank ?? 9999,
                        },
                        {
                          id: "name",
                          label: "Marché",
                          render: (m) => (
                            <>
                              <span>{m.name}</span>
                              <small
                                className={
                                  m.auto_eligible
                                    ? "scanner-row-status scanner-admitted"
                                    : "scanner-row-status scanner-observed"
                                }
                                title={m.eligibility_reason ?? ""}
                              >
                                {scannerStatus(m)}
                              </small>
                            </>
                          ),
                          sort: (m) => m.name,
                        },
                        {
                          id: "volume",
                          label: "USD/min",
                          render: (m) => <LiveAmount value={m.volume_usd_1m} />,
                          sort: (m) => m.volume_usd_1m,
                        },
                        {
                          id: "spread",
                          label: "Spread",
                          render: (m) =>
                            Number.isFinite(m.spread_bps)
                              ? m.spread_bps.toFixed(2) + " bps"
                              : "—",
                          sort: (m) => m.spread_bps,
                        },
                      ]}
                    />
                  )}
                </div>
              )}
            </section>
          )}
        </section>
        <aside className="panel summary">
          <h2>Résumé.</h2>
          <div className="available">
            <span>Solde disponible</span>
            <strong>
              {owner ? usd(w.balance?.usdc_available) : "—"}
              <small>USDC</small>
            </strong>
            {owner && (
              <span className="account-label">
                {short(w.identity!.account)}
                <Icon name="shield" size={12} />
              </span>
            )}
          </div>
          <dl className="summary-list">
            <div>
              <dt>Montant par ordre</dt>
              <dd>{usd(draft.order_usd)}</dd>
            </div>
            <div>
              <dt>Marché</dt>
              <dd>
                {draft.strategy === "outcome"
                  ? "Sélection Auto"
                  : draft.market || "À sélectionner"}
              </dd>
            </div>
            {draft.strategy === "outcome" && (
              <div>
                <dt>Côté Outcome</dt>
                <dd>{sideLabel(draft.side)}</dd>
              </div>
            )}
            {draft.strategy === "outcome" && (
              <div>
                <dt>Plafond</dt>
                <dd>{usd(draft.position_cap)}</dd>
              </div>
            )}
            {(draft.duration_sec || draft.target_volume_usd) && (
              <div>
                <dt>Fin de session</dt>
                <dd>
                  {draft.duration_sec
                    ? `${Math.round(draft.duration_sec / 60)} min`
                    : `${usd(draft.target_volume_usd)} de volume`}
                </dd>
              </div>
            )}
          </dl>
          <div className="summary-bottom">
            <SessionStepper connected={!!owner} ready={ready} />
            {(!owner || !ready) && (
              <p className="stepper-note">
                {!owner ? "Wallet requis." : "Autorisation requise."}
              </p>
            )}
            <FeedbackButton
              className="button primary launch-button summary-launch"
              onClick={preview}
              loading={busy === "launch"}
              loadingLabel="Lancement"
              disabled={!!busy || (!!owner && ready && !!block)}
            >
              <>
                {!owner
                  ? "Connecter"
                  : !ready
                    ? "Autoriser"
                    : "Lancer"}
                <Icon name="arrow" size={18} />
              </>
            </FeedbackButton>
            {actionError && <InlineAlert>{actionError}</InlineAlert>}
          </div>
        </aside>
      </div>
      <StickyLaunchBar
        mobileOnly
        amount={usd(draft.order_usd)}
        status={
          !owner
            ? "Wallet non connecté"
            : !ready
              ? "Autorisation requise"
              : block
                ? "Complétez les paramètres"
                : "Prêt"
        }
        label={
          !owner
            ? "Connecter"
            : !ready
              ? "Autoriser"
              : "Lancer"
        }
        loading={busy === "launch"}
        loadingLabel="Lancement"
        disabled={!!busy || (!!owner && ready && !!block)}
        onAction={preview}
      />
    </>
  );
}
