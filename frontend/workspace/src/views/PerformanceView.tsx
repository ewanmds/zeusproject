import { lazy, Suspense, useMemo, useState } from "react";
import { Empty } from "../ui";
import { SegmentedControl } from "../components/SegmentedControl";
import { Skeleton, StatsGrid } from "../components/feedback";
import { DataTable } from "../components/DataTable";
import { TradeJournalTable } from "../components/TradeJournalTable";
import { RichPopover } from "../components/fields";
import { clock, usd } from "../data";
import { strategies } from "../config";
import {
  filterByVenue,
  formatFactor,
  formatPercent,
  performanceFacts,
  venueLabel,
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
  const facts = useMemo(() => performanceFacts(filtered), [filtered]);
  const counts = useMemo(
    () => ({
      all: history.length,
      hip4: history.filter((session) => venueOf(session) === "hip4").length,
      hip3: history.filter((session) => venueOf(session) === "hip3").length,
    }),
    [history],
  );
  const venueOptions: { value: Venue; label: string }[] = [
    { value: "all", label: "Tous" },
    { value: "hip4", label: `HIP-4 · ${counts.hip4}` },
    { value: "hip3", label: `HIP-3 · ${counts.hip3}` },
  ];
  const summary = strategies
    .map((strategy) => {
      const rows = filtered.filter((session) => session.strategy === strategy.id);
      return {
        id: strategy.id,
        name: strategy.name,
        label: strategy.label,
        rows,
        facts: performanceFacts(rows),
      };
    })
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

  const factsStrip: { label: string; value: string; hint?: string }[] = [
    { label: "Frais", value: usd(facts.fees) },
    {
      label: "Fills",
      value: facts.fills == null ? "—" : String(facts.fills),
    },
    {
      label: "Durée moyenne",
      value: facts.avgDuration == null ? "—" : clock(facts.avgDuration),
    },
    { label: "Profit factor", value: formatFactor(facts.profitFactor) },
    {
      label: "Meilleur marché",
      value: facts.bestMarket?.name ?? "—",
      hint: facts.bestMarket ? usd(facts.bestMarket.net) : undefined,
    },
    {
      label: "Sessions",
      value: String(facts.sessions),
      hint: `${facts.running} en cours`,
    },
  ];

  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">Performances</span>
          <h1>Vos résultats, en continu.</h1>
          <p className="page-subtitle">
            HIP-3 et HIP-4 · net réalisé, frais inclus.
          </p>
        </div>
      </div>
      {owner ? (
        loading ? (
          <Skeleton />
        ) : (
          <>
            <div className="performance-toolbar panel">
              <div className="toolbar-group">
                <span className="micro-label">Marché</span>
                <SegmentedControl
                  label="Filtrer les performances par famille de marchés"
                  value={venue}
                  options={venueOptions}
                  onValueChange={setVenue}
                />
              </div>
              <div className="toolbar-group">
                <span className="micro-label">Période</span>
                <SegmentedControl
                  label="Période des performances"
                  value={period}
                  options={periods}
                  onValueChange={setPeriod}
                />
              </div>
              <div className="performance-meta">
                <span className="live-indicator" title="Mis à jour à chaque snapshot">
                  <i />
                  Flux continu
                </span>
                <span className="performance-count">
                  {filtered.length} session{filtered.length > 1 ? "s" : ""} ·{" "}
                  {venueLabel(venue)}
                </span>
              </div>
            </div>

            {!filtered.length ? (
              <div className="panel performance-empty">
                <Empty
                  icon="chart"
                  title={
                    history.length
                      ? `Aucune session ${venueLabel(venue)} sur cette période.`
                      : "Aucune session sur cette période."
                  }
                  text={
                    history.length
                      ? "Changez de famille de marchés ou de période pour retrouver vos résultats."
                      : "Lancez une session pour alimenter vos performances."
                  }
                  action={
                    history.length && venue !== "all" ? (
                      <button
                        className="button"
                        type="button"
                        onClick={() => setVenue("all")}
                      >
                        Tout afficher
                      </button>
                    ) : undefined
                  }
                />
              </div>
            ) : (
              <>
                <StatsGrid
                  items={[
                    {
                      label: "Volume exécuté",
                      value: usd(facts.volume),
                      icon: "chart",
                      help: (
                        <RichPopover label="Volume exécuté">
                          Volume total des sessions de la période et de la
                          famille sélectionnée.
                        </RichPopover>
                      ),
                    },
                    {
                      label: "Net réalisé",
                      value: usd(facts.net),
                      icon: "target",
                      negative: facts.net != null && facts.net < 0,
                      help: (
                        <RichPopover label="Net réalisé">
                          Net des sessions terminées, frais inclus. Les
                          positions encore ouvertes restent dans Sessions.
                        </RichPopover>
                      ),
                    },
                    {
                      label: "Taux de réussite",
                      value: formatPercent(facts.winRate),
                      icon: "sessions",
                      help: (
                        <RichPopover label="Taux de réussite">
                          Part des sessions terminées avec un net positif. Les
                          sessions en cours et les neutres sont exclus.
                        </RichPopover>
                      ),
                    },
                    {
                      label: "Coût / 1M",
                      value: usd(facts.costPerMillion),
                      icon: "info",
                      help: (
                        <RichPopover label="Coût par million">
                          Coût calculé à partir du net réalisé, frais inclus,
                          rapporté au volume exécuté. Un coût négatif signifie
                          un gain net.
                        </RichPopover>
                      ),
                    },
                  ]}
                />

                <div className="performance-facts panel">
                  {factsStrip.map((item) => (
                    <div key={item.label}>
                      <span>{item.label}</span>
                      <strong title={item.value}>{item.value}</strong>
                      {item.hint && <small>{item.hint}</small>}
                    </div>
                  ))}
                </div>

                <Suspense
                  fallback={<Skeleton label="Chargement des graphiques" />}
                >
                  <PerformanceCharts history={filtered} period={period} />
                </Suspense>
                <p className="chart-caption">
                  Sessions démarrées sur la période · net réalisé, frais
                  inclus · les positions ouvertes restent dans Sessions.
                </p>

                <TradeJournalTable
                  rows={journalRows}
                  title="Journal des sessions"
                  subtitle={`${journalRows.length} session${journalRows.length > 1 ? "s" : ""} sur la période`}
                  emptyTitle="Aucune session sur la période."
                  emptyText="Lancez une session pour alimenter votre journal."
                />

                {summary.length > 0 && (
                  <div className="panel table-panel performance-strategies">
                    <DataTable
                      rows={summary}
                      rowKey={(row) => row.id}
                      caption="Résultats par stratégie"
                      columns={[
                        {
                          id: "name",
                          label: "Stratégie",
                          render: (row) => (
                            <span className="strategy-name">
                              {row.name}
                              <span
                                className={`venue-badge ${venueOf({ strategy: row.id })}`}
                              >
                                {row.label}
                              </span>
                            </span>
                          ),
                        },
                        {
                          id: "sessions",
                          label: "Sessions",
                          render: (row) =>
                            row.rows.length
                              ? row.rows.length
                              : "—",
                        },
                        {
                          id: "volume",
                          label: "Volume",
                          render: (row) => usd(row.facts.volume),
                        },
                        {
                          id: "net",
                          label: "Net réalisé",
                          render: (row) => (
                            <span
                              className={
                                (row.facts.net ?? 0) >= 0
                                  ? "positive"
                                  : "negative"
                              }
                            >
                              {usd(row.facts.net)}
                            </span>
                          ),
                        },
                        {
                          id: "win",
                          label: "Réussite",
                          render: (row) => formatPercent(row.facts.winRate),
                        },
                        {
                          id: "fills",
                          label: "Fills",
                          render: (row) => row.facts.fills ?? "—",
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
                        id: "venue",
                        label: "Famille",
                        render: (session) => (
                          <span
                            className={`venue-badge ${venueOf(session)}`}
                          >
                            {venueLabel(venueOf(session))}
                          </span>
                        ),
                      },
                      {
                        id: "started",
                        label: "Début",
                        render: (session) =>
                          new Date(session.started * 1000).toLocaleString(
                            "fr-FR",
                          ),
                        sort: (session) => session.started,
                      },
                      {
                        id: "volume",
                        label: "Volume",
                        render: (session) => usd(session.volume_usd),
                        sort: (session) => session.volume_usd,
                      },
                      {
                        id: "net",
                        label: "Net",
                        render: (session) => usd(session.net_pnl),
                        sort: (session) => session.net_pnl,
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
            )}
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
