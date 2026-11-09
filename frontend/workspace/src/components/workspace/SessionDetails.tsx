import { usd } from "../../data";
import type { Session } from "../../types";
import { DetailTabs } from "../DetailTabs";
import { SessionProgress } from "../SessionProgress";
import { InlineAlert } from "../feedback";
export function SessionDetails({ session }: { session: Session }) {
  const s = session;
  const values = (items: [string, string | number][]) => (
    <dl className="summary-list">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
  return (
    <>
      <h3>{s.name}</h3>
      <DetailTabs
        items={[
          {
            id: "execution",
            label: "Exécution",
            content: (
              <>
                {values([
                  ["Volume exécuté", usd(s.volume_usd)],
                  ["Fills", s.fills ?? "—"],
                  ["Coût / 1M", usd(s.cost_per_1m)],
                ])}
                <div className="quote-box">
                  {s.quotes?.length ? (
                    s.quotes.map((q: any, index: number) => (
                      <div key={index}>
                        <span>
                          BID{" "}
                          <b>
                            {q.bid_size > 0
                              ? q.bid_size + " × " + q.bid_price
                              : "—"}
                          </b>
                        </span>
                        <span>
                          ASK{" "}
                          <b>
                            {q.ask_size > 0
                              ? q.ask_size + " × " + q.ask_price
                              : "—"}
                          </b>
                        </span>
                      </div>
                    ))
                  ) : (
                    <p>Aucun ordre coté dans le dernier état reçu.</p>
                  )}
                </div>
                <SessionProgress session={s} />
              </>
            ),
          },
          {
            id: "position",
            label: "Position",
            content: values([
              ["Position valorisée", usd(s.position_usd)],
              ["PnL brut avant frais", usd(s.net_pnl !== null && s.fees !== null ? s.net_pnl + s.fees : null)],
              ["PnL réalisé net", usd(s.net_pnl)],
              ["Frais", usd(s.fees)],
            ]),
          },
          {
            id: "parameters",
            label: "Paramètres",
            content: values([
              [
                "Stratégie",
                s.strategy === "outcome"
                  ? "Auto Outcome"
                  : s.strategy.toUpperCase(),
              ],
              ["Marché", s.market],
              ["Côté", s.config.outcome_side || s.config.side || "—"],
              [
                "Montant par ordre",
                usd(s.config.order_size_usd ?? s.config.order_usd),
              ],
              [
                "Position max.",
                usd(s.config.max_position_usd ?? s.config.position_cap),
              ],
              [
                "Seuil de perte réalisée",
                usd(s.config.session_stop_loss_usd ?? s.config.stop_loss_usd),
              ],
            ]),
          },
        ]}
      />
      {s.reason && <InlineAlert tone="warning">{s.reason}</InlineAlert>}
      <p className="session-id">SESSION {s.id}</p>
    </>
  );
}
