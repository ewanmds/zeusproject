import { Icon } from "../ui";
import { HaloInput, HaloField, MarketSelect, RichPopover } from "../components/fields";
import { ResponsiveOptions } from "../components/Dialog";
import { SessionStepper } from "../components/layout";
import { DataTable } from "../components/DataTable";
import { InlineAlert } from "../components/feedback";
import { SegmentedControl } from "../components/SegmentedControl";
import { FeedbackButton } from "../components/FeedbackButton";
import { usd, short } from "../data";
import { strategies } from "../config";
import type { RunProps } from "../viewTypes";
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
  pendingOperation,
  busy,
  setShowHelp,
  actionError,
}: RunProps) {
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">VOTRE PROCHAINE SESSION</span>
          <h1>Lancer, simplement.</h1>
        </div>
        <span className="environment"><i />Hyperliquid Mainnet</span>
      </div>
      <div className="launch-grid">
        <section
          className="panel launch-form"
          aria-label="Préparer une session"
        >
          <div className="section-heading">
            <span className="step">01</span>
            <h2>Choisissez votre stratégie</h2>
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
            <h2>Configurez la session</h2>
          </div>
          <div className="amount-section">
            <label htmlFor="order">Montant par ordre</label>
            <div className="amount-input">
              <span>$</span>
              <HaloInput
                id="order"
                type="number"
                inputMode="decimal"
                min="10.5"
                step="0.01"
                value={draft.order_usd || ""}
                onChange={(e) => update("order_usd", Number(e.target.value))}
              />
              <span className="amount-unit">USD</span>
            </div>
            <div className="amount-bottom">
              <span>Minimum 10,50 $</span>
              <div className="quick-amounts">
                {[25, 50, 100].map((n) => (
                  <button
                    key={n}
                    onClick={() => update("order_usd", n)}
                    className={draft.order_usd === n ? "selected" : ""}
                  >
                    ${n}
                  </button>
                ))}
              </div>
            </div>
          </div>
          {draft.strategy === "outcome" ? (
            <div className="inline-fields">
              <div>
                <span className="field-label">Côté Outcome</span>
                <SegmentedControl label="Côté Outcome" value={draft.side}
                  options={[{ value: "yes", label: "Yes" }, { value: "no", label: "No" }]}
                  onValueChange={(side) => update("side", side)} />
              </div>
              <div className="selection-note">
                <Icon name="auto" />
                <div>
                  <strong>Marché automatique</strong>
                  <span>Flux et carnet observés en direct</span>
                </div>
              </div>
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
              <MarketSelect value={draft.market} search={search} onChange={(value) => update("market", value)}
                options={markets.map((m) => ({ value: m.key, label: m.symbol || m.name || m.key }))} />
              {!owner && (
                <span className="field-hint">
                  Connectez votre wallet pour charger les marchés.
                </span>
              )}
            </div>
          )}
          <ResponsiveOptions summary={<>
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
</>}>
            <div className="advanced-body">
              {draft.strategy === "outcome" ? (
                <div className="two-fields">
                  <HaloField label="Plafond de position · USD"><HaloInput
                      type="number"
                      min={draft.order_usd}
                      value={draft.position_cap}
                      onChange={(e) =>
                        update("position_cap", Number(e.target.value))
                      }
                    /></HaloField>
                  <HaloField label="Spread maximal · bps"><HaloInput
                      type="number"
                      min="0.01"
                      max="100"
                      step="0.1"
                      value={draft.spread_bps}
                      onChange={(e) =>
                        update("spread_bps", Number(e.target.value))
                      }
                    /></HaloField>
                </div>
              ) : draft.strategy === "mid" ? (
                <HaloField label="Distance au prix médian · bps"><HaloInput
                    type="number"
                    min="0"
                    max="9999"
                    step="0.1"
                    value={draft.offset_bps}
                    onChange={(e) =>
                      update("offset_bps", Number(e.target.value))
                    }
                  /></HaloField>
              ) : (
                <p>
                  Le moteur cote au meilleur bid et au meilleur ask. Chaque
                  ordre reste soumis aux contrôles de fonds et de risque.
                </p>
              )}
              {draft.strategy === "outcome" && (
                <div className="session-target">
                  <span className="field-label">Fin de session</span>
                  <SegmentedControl label="Fin de session"
                    value={draft.duration_sec !== null ? "duration" : draft.target_volume_usd !== null ? "volume" : "manual"}
                    options={[{ value: "manual", label: "Manuelle" }, { value: "duration", label: "Durée" }, { value: "volume", label: "Volume" }]}
                    onValueChange={(mode) => {
                      setDraft((d) => ({ ...d,
                        duration_sec: mode === "duration" ? 14400 : null,
                        target_volume_usd: mode === "volume" ? 1000 : null,
                      }));
                      setReview(null);
                    }} />
                  {draft.duration_sec !== null && (
                    <HaloField label="Durée · minutes"><HaloInput
                        type="number"
                        min="1"
                        max="43200"
                        value={draft.duration_sec / 60}
                        onChange={(e) =>
                          update("duration_sec", Number(e.target.value) * 60)
                        }
                      /></HaloField>
                  )}
                  {draft.target_volume_usd !== null && (
                    <HaloField label="Volume exécuté cible · USD"><HaloInput
                        type="number"
                        min="10"
                        value={draft.target_volume_usd}
                        onChange={(e) =>
                          update("target_volume_usd", Number(e.target.value))
                        }
                      /></HaloField>
                  )}
                  <HaloField label="Seuil de perte réalisée · USD"><HaloInput
                      type="number"
                      min="0.01"
                      step="0.01"
                      value={draft.stop_loss_usd}
                      onChange={(e) =>
                        update("stop_loss_usd", Number(e.target.value))
                      }
                    /></HaloField>
                  {draft.duration_sec || draft.target_volume_usd ? (
                    <p className="hint">
                      À l’objectif, les nouveaux achats cessent. La sortie reste
                      en cours jusqu’à son exécution en maker.
                    </p>
                  ) : null}
                </div>
              )}
              <div className="small-info">
                <Icon name="shield" size={15} />
                <span>
                  Les paramètres sont figés au lancement de la session.
                </span>
              </div>
            </div>
          </ResponsiveOptions>
          {draft.strategy === "outcome" && owner && (
            <div className="scanner">
              <div className="scanner-head">
                <span>
                  <i className={scanner?.ready ? "green-dot" : "gold-dot"} />
                  Scanner de marchés
                </span>
                <span className="mono">
                  {scanner?.ready
                    ? scanner.candidates.length + " éligibles"
                    : scanner?.warming_up
                      ? "Observation…"
                      : "En attente de flux"}
                </span>
              </div>
              {!!scanner?.candidates?.length && <DataTable<any> caption="Candidats observés · marché actif dans Sessions"
                rows={scanner.candidates.slice(0, 8)} rowKey={(m) => m.market} columns={[
                  { id: "name", label: "Marché", render: (m) => m.name, sort: (m) => m.name },
                  { id: "volume", label: "USD/min", render: (m) => usd(m.volume_usd_1m), sort: (m) => m.volume_usd_1m },
                  { id: "spread", label: "Spread", render: (m) => Number.isFinite(m.spread_bps) ? m.spread_bps.toFixed(2) + " bps" : "—", sort: (m) => m.spread_bps },
                  { id: "queue", label: "File estimée", render: (m) => Number.isFinite(m.queue_proxy_sec) ? Math.round(m.queue_proxy_sec) + " s" : "—", sort: (m) => m.queue_proxy_sec },
                ]} />}
              <RichPopover label="Comprendre le scanner">Le débit est le volume observé en une minute. La file est une estimation ; elle ne garantit pas l’exécution d’un ordre.</RichPopover>
            </div>
          )}
        </section>
        <aside className="panel summary">
          <div className="summary-top">
            <span className="eyebrow">RÉCAPITULATIF</span>
            <span className="summary-icon">
              <Icon name="spark" />
            </span>
          </div>
          <h2>Votre session.</h2>
          <div className="available">
            <span>Solde spot disponible</span>
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
              <dt>Stratégie</dt>
              <dd>{strategies.find((s) => s.id === draft.strategy)?.name}</dd>
            </div>
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
            <div>
              <dt>Exécution</dt>
              <dd>
                <span className="maker-badge">Maker · Post-only</span>
              </dd>
            </div>
            {draft.strategy === "outcome" && (
              <div>
                <dt>Plafond de position</dt>
                <dd>{usd(draft.position_cap)}</dd>
              </div>
            )}
            <div>
              <dt>Compte de trading</dt>
              <dd>{owner ? short(owner) : "Non connecté"}</dd>
            </div>
          </dl>
          <div className="summary-bottom">
            <SessionStepper connected={!!owner} ready={ready} />
            <FeedbackButton
              className="button primary launch-button"
              onClick={preview}
              loading={busy === "preview"}
              loadingLabel="Vérification"
              disabled={
                !!busy ||
                !!pendingOperation ||
                (!!owner && ready && (!Number.isFinite(draft.order_usd) || draft.order_usd < 10.5 ||
                (draft.strategy !== "outcome" && !draft.market)))
              }
            >
              <>
                  {!owner
                    ? "Connecter mon wallet"
                    : !ready
                      ? "Autoriser le trading"
                      : "Vérifier et lancer"}
                  <Icon name="arrow" size={18} />
              </>
            </FeedbackButton>
            {actionError && <InlineAlert>{actionError}</InlineAlert>}
            <p className="footnote">
              <Icon name="shield" size={13} />
              {owner
                ? "Vous vérifiez les paramètres avant le lancement."
                : "Vos fonds restent sur votre compte Hyperliquid."}
            </p>
          </div>
        </aside>
      </div>
      <div className="bottom-guide">
        <span>
          <Icon name="link" size={15} />
          Votre wallet
        </span>
        <i />
        <span>
          <Icon name="shield" size={15} />
          Votre compte
        </span>
        <i />
        <span>
          <Icon name="run" size={15} />
          Vos sessions
        </span>
        <button onClick={() => setShowHelp(true)}>
          Comment ça marche ?<Icon name="info" size={14} />
        </button>
      </div>
    </>
  );
}
