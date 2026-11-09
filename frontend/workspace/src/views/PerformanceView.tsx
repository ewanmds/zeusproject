import { lazy, Suspense } from "react";
import { Empty } from "../ui";
import { SegmentedControl } from "../components/SegmentedControl";
import { StatsGrid, Skeleton } from "../components/feedback";
import { DataTable } from "../components/DataTable";
import { RichPopover } from "../components/fields";
import { usd } from "../data";
import { strategies } from "../config";
import { aggregate } from "../presentation/session";
import type { PerformanceProps } from "../viewTypes";
const PerformanceCharts = lazy(
  () => import("../components/workspace/PerformanceCharts"),
);
export function PerformanceView({
  owner,
  loading,
  period,
  setPeriod,
  history,
  volume,
  net,
  sum,
  setWalletModal,
}: PerformanceProps) {
  const summary = strategies.map((st) => ({
    id: st.id,
    name: st.name,
    rows: history.filter((s) => s.strategy === st.id),
  }));
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">VOS RÉSULTATS</span>
          <h1>Une vue claire.</h1>
        </div>
        <SegmentedControl
          label="Période des performances"
          className="period"
          value={period}
          options={[1, 7, 30, 90].map((n) => ({
            value: n,
            label: n === 1 ? "24 h" : n + " j",
          }))}
          onValueChange={setPeriod}
        />
      </div>
      {owner ? (
        loading ? (
          <Skeleton />
        ) : (
          <>
            <StatsGrid
              items={[
                { label: "Volume exécuté", value: usd(volume), icon: "chart" },
                {
                  label: "Net réalisé",
                  value: usd(net),
                  icon: "target",
                  negative: net != null && net < 0,
                },
                { label: "Frais", value: usd(sum("fees")), icon: "wallet" },
                {
                  label: "Coût / 1M",
                  value: usd(
                    volume && net != null ? (-net / volume) * 1e6 : null,
                  ),
                  icon: "info",
                  help: (
                    <RichPopover label="Coût par million">
                      Le coût est calculé à partir du net réalisé, frais inclus,
                      et du volume exécuté sur la même période.
                    </RichPopover>
                  ),
                },
              ]}
            />
            <Suspense fallback={<Skeleton label="Chargement des graphiques" />}>
              <PerformanceCharts history={history} />
            </Suspense>
            <p className="chart-caption">
              Sessions démarrées sur la période · PnL réalisé, frais inclus ·
              Les positions ouvertes restent dans Sessions.
            </p>
            <div className="panel table-panel">
              <DataTable
                rows={summary}
                rowKey={(row) => row.id}
                caption="Résultats par stratégie"
                columns={[
                  { id: "name", label: "Stratégie", render: (row) => row.name },
                  {
                    id: "volume",
                    label: "Volume",
                    render: (row) =>
                      row.rows.length
                        ? usd(aggregate(row.rows, "volume_usd"))
                        : "—",
                  },
                  {
                    id: "net",
                    label: "Net réalisé",
                    render: (row) =>
                      row.rows.length
                        ? usd(aggregate(row.rows, "net_pnl"))
                        : "—",
                  },
                  {
                    id: "fills",
                    label: "Fills",
                    render: (row) =>
                      row.rows.length
                        ? (aggregate(row.rows, "fills") ?? "—")
                        : "—",
                  },
                ]}
              />
            </div>
            <details className="panel performance-data">
              <summary>Données par session</summary>
              <DataTable
                rows={history}
                rowKey={(s) => s.id}
                caption="Données exactes des graphiques"
                columns={[
                  {
                    id: "name",
                    label: "Session",
                    render: (s) => s.name,
                    sort: (s) => s.name,
                  },
                  {
                    id: "started",
                    label: "Début",
                    render: (s) =>
                      new Date(s.started * 1000).toLocaleString("fr-FR"),
                    sort: (s) => s.started,
                  },
                  {
                    id: "volume",
                    label: "Volume",
                    render: (s) => usd(s.volume_usd),
                    sort: (s) => s.volume_usd,
                  },
                  {
                    id: "fills",
                    label: "Fills",
                    render: (s) => s.fills ?? "—",
                    sort: (s) => s.fills,
                  },
                ]}
              />
            </details>
          </>
        )
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
