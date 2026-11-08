import { Icon, Empty, Pending } from "../ui";
import { api, usd, short, clock } from "../data";
import { strategies, phaseText, stateText } from "../config";
import type { PerformanceProps } from "../viewTypes";
export function PerformanceView({
  owner,
  period,
  setPeriod,
  history,
  volume,
  net,
  sum,
  setWalletModal,
}: PerformanceProps) {
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">VOS RÉSULTATS</span>
          <h1>Une vue claire.</h1>
        </div>
        <div className="segments period">
          {[1, 7, 30, 90].map((n) => (
            <button
              className={period === n ? "active" : ""}
              key={n}
              onClick={() => setPeriod(n)}
            >
              {n === 1 ? "24 h" : n + " j"}
            </button>
          ))}
        </div>
      </div>
      {owner ? (
        <>
          <div className="metrics-grid">
            {[
              ["Volume exécuté", usd(volume)],
              ["Net réalisé", usd(net)],
              ["Frais", usd(sum("fees"))],
              [
                "Coût / 1M",
                usd(volume && net != null ? (-net / volume) * 1e6 : null),
              ],
            ].map(([label, value]) => (
              <div className="panel metric" key={label}>
                <span className="metric-label">{label}</span>
                <strong>{value}</strong>
              </div>
            ))}
          </div>
          <section className="panel chart-panel">
            <div className="section-heading">
              <Icon name="chart" />
              <h2>Volume par session</h2>
              <span className="micro-label">
                {period === 1 ? "24 HEURES" : period + " JOURS"}
              </span>
            </div>
            {history.length ? (
              <div
                className="bar-chart"
                role="img"
                aria-label="Volume exécuté par session sur la période sélectionnée"
              >
                {history
                  .slice()
                  .reverse()
                  .map((s) => (
                    <div className="bar-column" key={s.id}>
                      <span>{usd(s.volume_usd)}</span>
                      <div className="bar-track">
                        <div
                          className="bar"
                          style={{
                            height:
                              Math.max(
                                2,
                                ((s.volume_usd || 0) /
                                  Math.max(
                                    1,
                                    ...history.map((s) => s.volume_usd || 0),
                                  )) *
                                  100,
                              ) + "%",
                          }}
                        />
                      </div>
                      <small>
                        {new Date(s.started * 1000).toLocaleDateString(
                          "fr-FR",
                          { day: "2-digit", month: "short" },
                        )}
                      </small>
                    </div>
                  ))}
              </div>
            ) : (
              <Empty
                icon="chart"
                title="Vos résultats prennent forme ici."
                text="Le graphique se remplit avec les données de vos sessions."
              />
            )}
            <p className="chart-caption">
              Sessions démarrées sur la période · PnL réalisé, frais inclus ·
              Les positions encore ouvertes sont affichées dans Sessions.
            </p>
          </section>
          <div className="panel table-panel">
            <table>
              <caption>Résultats par stratégie</caption>
              <thead>
                <tr>
                  <th>Stratégie</th>
                  <th>Volume</th>
                  <th>Net réalisé</th>
                  <th>Fills</th>
                </tr>
              </thead>
              <tbody>
                {strategies.map((st) => {
                  const rows = history.filter((s) => s.strategy === st.id);
                  return (
                    <tr key={st.id}>
                      <td>
                        <Icon name={st.icon} size={16} />
                        {st.name}
                      </td>
                      <td>
                        {rows.length
                          ? usd(
                              rows.reduce((a, s) => a + (s.volume_usd || 0), 0),
                            )
                          : "—"}
                      </td>
                      <td>
                        {rows.length
                          ? usd(rows.reduce((a, s) => a + (s.net_pnl || 0), 0))
                          : "—"}
                      </td>
                      <td>
                        {rows.length
                          ? rows.reduce((a, s) => a + (s.fills || 0), 0)
                          : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <Empty
          icon="chart"
          title="Vos performances, en perspective."
          text="Connectez votre wallet pour consulter vos résultats personnels."
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
