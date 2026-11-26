import { lazy, Suspense, useMemo, useState } from "react";
import { Empty } from "../ui";
import { SegmentedControl } from "../components/SegmentedControl";
import { StatsGrid, Skeleton } from "../components/feedback";
import { DataTable } from "../components/DataTable";
import { TradeJournalTable } from "../components/TradeJournalTable";
import { RichPopover } from "../components/fields";
import { usd } from "../data";
import { strategies } from "../config";
import { aggregate } from "../presentation/session";
import {
  filterByVenue,
  venueOf,
  type Venue,
} from "../presentation/performance";
import type { PerformanceProps } from "../viewTypes";

const PerformanceCharts = lazy(
  () => import("../components/workspace/PerformanceCharts"),
);

const periods = [1, 7, 30, 90].map((n) => ({
  value: n,
  label: n === 1 ? "24 h" : n + " j",
}));
const venues: { value: Venue; label: string }[] = [
  { value: "all", label: "Tous" },
  { value: "hip4", label: "HIP-4" },
  { value: "hip3", label: "HIP-3" },
];

export function PerformanceView({
  owner,
  loading,
  period,
  setPeriod,
  history,
  setWalletModal,
}: PerformanceProps) {
  const [venue, setVenue] = useState<Venue>("all");
  const filtered = useMemo(() => filterByVenue(history, venue), [history, venue]);
  const volume = aggregate(filtered, "volume_usd");
  const net = aggregate(filtered, "net_pnl");
  const fees = aggregate(filtered, "fees");
  const summary = strategies
    .filter(
      (strategy) =>
        venue === "all" || venueOf({ strategy: strategy.id }) === venue,
    )
    .map((strategy) => ({
      id: strategy.id,
      name: strategy.name,
      rows: filtered.filter((session) => session.strategy === strategy.id),
    }))
    .filter((row) => row.rows.length > 0);
  const journalRows = filtered.map((session) => {
    const pnl = session.net_pnl ?? 0;
    const status = session.running
      ? ("open" as const)
      : pnl > 0
        ? ("win" as const)
        : pnl < 0
          ? ("loss" as const)
          : ("stopped" as const);
    return {
      id: session.id,
      date: session.started,
      market: session.name,
      side:
        session.config.outcome_side === "yes"
          ? "Yes"
          : session.config.outcome_side === "no"
            ? "No"
            : session.strategy.toUpperCase(),
      size: session.volume_usd,
      net_pnl: session.net_pnl,
      duration_sec: session.duration_sec,
      status,
    };
  });

  return (
    <>
      <div className="page-heading performance-heading">
        <div>
          <h1>Une vue claire.</h1>
        </div>
        <SegmentedControl
          label="Période des performances"
          className="period"
          value={period}
          options={periods}
          onValueChange={setPeriod}
        />
      </div>
      {owner ? (
        loading ? (
          <Skeleton />
        ) : (
          <>
            <div className="performance-filters">
              <span className="micro-label">Marché</span>
              <SegmentedControl
                label="Filtrer les performances par famille de marchés"
                value={venue}
                options={venues}
                onValueChange={setVenue}
              />
              <span className="live-indicator" title="Mise à jour continue">
                <i />
                Flux continu
              </span>
            </div>
            <StatsGrid
              items={[
                { label: "Volume exécuté", value: usd(volume), icon: "chart" },
                {
                  label: "Net réalisé",
                  value: usd(net),
                  icon: "target",
                  negative: net != null && net < 0,
                },
                { label: "Frais", value: usd(fees), icon: "wallet" },
                {
                  label: "Coût / 1M",
                  value: usd(
                    volume && net != null ? (-net / volume) * 1e6 : null,
                  ),
                  icon: "info",
                  help: (
                    <RichPopover label="Coût par million">
                      Le coût est calculé à partir du net réalisé, frais inclus,
                      et du volume exécuté sur la même période. Un coût négatif
                      signifie un gain net.
                    </RichPopover>
                  ),
                },
              ]}
            />
            <Suspense fallback={<Skeleton label="Chargement des graphiques" />}>
              <PerformanceCharts
                history={filtered}
                period={period}
                venue={venue}
              />
            </Suspense>
            <p className="chart-caption">
              Sessions démarrées sur la période · PnL réalisé, frais inclus ·
              Les positions ouvertes restent dans Sessions.
            </p>
            <TradeJournalTable
              rows={journalRows}
              title="Journal des sessions"
              subtitle={`${journalRows.length} session${journalRows.length > 1 ? "s" : ""} sur la période`}
              emptyTitle="Aucune session sur la période."
              emptyText="Lancez une session pour alimenter votre journal."
            />
            {summary.length > 0 && (
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
            )}
            <details className="panel performance-data">
              <summary>Données par session</summary>
              <DataTable
                rows={filtered}
                rowKey={(session) => session.id}
                caption="Données exactes des graphiques"
                columns={[
                  {
                    id: "name",
                    label: "Session",
                    render: (session) => session.name,
                    sort: (session) => session.name,
                  },
                  {
                    id: "started",
                    label: "Début",
                    render: (session) =>
                      new Date(session.started * 1000).toLocaleString("fr-FR"),
                    sort: (session) => session.started,
                  },
                  {
                    id: "volume",
                    label: "Volume",
                    render: (session) => usd(session.volume_usd),
                    sort: (session) => session.volume_usd,
                  },
                  {
                    id: "fills",
                    label: "Fills",
                    render: (session) => session.fills ?? "—",
                    sort: (session) => session.fills,
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
