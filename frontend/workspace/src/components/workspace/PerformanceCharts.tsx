// Magic UI shine-border and Recharts, adapted to the Zeus tokens.
// Charts are lazy-loaded by the Performance route and use real data only.
import { useId } from "react";
import {
  Area,
  AreaChart,
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  Pie,
  PieChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Session } from "../../types";
import { usd } from "../../data";
import {
  bucketPerformance,
  equityCurve,
  formatFactor,
  formatPercent,
  marketBreakdown,
  outcomeBreakdown,
  performanceFacts,
  venueLabel,
  type BucketPoint,
  type EquityPoint,
} from "../../presentation/performance";
import { Empty, InlineAlert } from "../feedback";
import { ShineBorder } from "../ui/shine-border";

const colors = {
  purple: "#8b5cf6",
  gold: "#f59e0b",
  green: "#10b981",
  red: "#ef4444",
  blue: "#38bdf8",
  slate: "#64748b",
};

const tooltipStyle = {
  background: "#0b1220",
  border: "1px solid rgba(148, 163, 184, 0.22)",
  borderRadius: 10,
  boxShadow: "0 12px 30px rgba(0, 0, 0, 0.28)",
  color: "#f8fafc",
  fontSize: 12,
  padding: "10px 12px",
};

function axisUsd(value: number) {
  const absolute = Math.abs(value);
  if (absolute === 0) return "$0";
  if (absolute < 0.01) return `$${value.toFixed(3)}`;
  if (absolute < 0.1) return `$${value.toFixed(2)}`;
  if (absolute < 1) return `$${value.toFixed(2)}`;
  if (absolute < 10) return `$${value.toFixed(1)}`;
  if (absolute >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`;
  if (absolute >= 1_000) return `$${(value / 1_000).toFixed(1)}k`;
  return `$${Math.round(value)}`;
}

function pct(value: number | null) {
  return formatPercent(value);
}

function factor(value: number | null) {
  return formatFactor(value);
}

function dateLabel(value: number, granularity: "session" | "day") {
  const date = new Date(value);
  return date.toLocaleString("fr-FR", {
    day: "2-digit",
    month: "short",
    ...(granularity === "session"
      ? { hour: "2-digit", minute: "2-digit" }
      : {}),
  });
}

type EquityPayload = { payload: EquityPoint };
function EquityTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: EquityPayload[];
}) {
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;
  return (
    <div className="chart-tooltip">
      <span>{dateLabel(point.at, "session")}</span>
      <strong>{point.name}</strong>
      <dl>
        <div>
          <dt>Session</dt>
          <dd className={point.net >= 0 ? "positive" : "negative"}>
            {usd(point.net)}
          </dd>
        </div>
        <div>
          <dt>Cumulé</dt>
          <dd className={point.cumulative >= 0 ? "positive" : "negative"}>
            {usd(point.cumulative)}
          </dd>
        </div>
      </dl>
    </div>
  );
}

type BucketPayload = { payload: BucketPoint };
function BucketTooltip({
  active,
  payload,
  granularity,
}: {
  active?: boolean;
  payload?: BucketPayload[];
  granularity: "session" | "day";
}) {
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;
  return (
    <div className="chart-tooltip">
      <span>{dateLabel(point.at, granularity)}</span>
      <strong>
        {point.sessions} session{point.sessions > 1 ? "s" : ""}
      </strong>
      <dl>
        <div>
          <dt>Volume</dt>
          <dd>{usd(point.volume)}</dd>
        </div>
        <div>
          <dt>Net</dt>
          <dd className={(point.net ?? 0) >= 0 ? "positive" : "negative"}>
            {usd(point.net)}
          </dd>
        </div>
        <div>
          <dt>Fills</dt>
          <dd>{point.fills ?? "—"}</dd>
        </div>
      </dl>
    </div>
  );
}

export default function PerformanceCharts({
  history,
  period,
}: {
  history: Session[];
  period: number;
}) {
  const rawId = useId().replace(/:/g, "");
  const equityFill = `equity-${rawId}`;
  const volumeFill = `volume-${rawId}`;
  const facts = performanceFacts(history);
  const equity = equityCurve(history);
  const buckets = bucketPerformance(history, period);
  const markets = marketBreakdown(history, 6);
  const outcomes = outcomeBreakdown(facts);
  const outcomeTotal = outcomes.reduce((total, row) => total + row.value, 0);
  const outcomeColors: Record<string, string> = {
    wins: colors.green,
    losses: colors.red,
    flat: colors.slate,
    running: colors.purple,
  };
  const equityTone = (facts.net ?? 0) >= 0 ? colors.green : colors.red;
  const maxMarketNet = Math.max(
    ...markets.map((row) => Math.abs(row.net ?? 0)),
    1,
  );
  const incomplete = equity.incomplete || buckets.incomplete;

  return (
    <div className="performance-charts-stack">
      <div className="performance-charts performance-charts-top">
        <section
          className="panel chart-panel performance-hero"
          data-layro="legions-developer/recharts-area-chart"
        >
          <ShineBorder
            borderWidth={1}
            duration={22}
            shineColor={[colors.purple]}
            className="opacity-25"
          />
          <header className="chart-head">
            <div>
              <span className="micro-label">Courbe de résultat</span>
              <h2>Net cumulé</h2>
              <p className="chart-subtitle">
                Sessions démarrées, net réalisé frais inclus.
              </p>
            </div>
            <strong className={equityTone === colors.green ? "positive" : "negative"}>
              {usd(facts.net)}
            </strong>
          </header>
          {!equity.points.length ? (
            <Empty
              icon="chart"
              title="Pas encore de courbe."
              text="Les sessions terminées dessineront votre résultat cumulé."
            />
          ) : (
            <div
              className="chart-frame chart-frame-equity"
              role="img"
              aria-label="Résultat net cumulé des sessions terminées ; valeurs exactes dans le journal ci-dessous."
            >
              <ResponsiveContainer width="100%" height="100%" minWidth={0}>
                <AreaChart
                  data={equity.points}
                  margin={{ top: 16, right: 10, bottom: 0, left: 0 }}
                >
                  <defs>
                    <linearGradient id={equityFill} x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={equityTone} stopOpacity={0.32} />
                      <stop offset="100%" stopColor={equityTone} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid vertical={false} stroke="#94a3b81a" />
                  <XAxis
                    dataKey="at"
                    type="number"
                    scale="time"
                    domain={["dataMin", "dataMax"]}
                    tickLine={false}
                    axisLine={false}
                    minTickGap={34}
                    tick={{ fill: colors.slate, fontSize: 11 }}
                    tickFormatter={(value) => dateLabel(Number(value), "session")}
                    tickMargin={10}
                  />
                  <YAxis
                    tickLine={false}
                    axisLine={false}
                    width={58}
                    tick={{ fill: colors.slate, fontSize: 11 }}
                    tickFormatter={(value) => axisUsd(Number(value))}
                  />
                  <ReferenceLine
                    y={0}
                    stroke="#94a3b866"
                    strokeDasharray="3 4"
                  />
                  <Tooltip content={<EquityTooltip />} />
                  <Area
                    type="monotone"
                    dataKey="cumulative"
                    stroke={equityTone}
                    strokeWidth={2.2}
                    fill={`url(#${equityFill})`}
                    dot={equity.points.length < 3 ? { r: 3, fill: equityTone } : false}
                    activeDot={{ r: 4, strokeWidth: 0 }}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}
        </section>

        <section
          className="panel chart-panel"
          data-layro="legions-developer/recharts-pie-chart"
        >
          <header className="chart-head">
            <div>
              <span className="micro-label">
                {facts.closed ? "Sessions terminées" : "En attente"}
              </span>
              <h2>{facts.closed ? "Taux de réussite" : "Sessions en cours"}</h2>
              <p className="chart-subtitle">
                {facts.closed
                  ? "Gains, pertes et neutres."
                  : "Le taux de réussite arrive à la première clôture."}
              </p>
            </div>
          </header>
          {!outcomeTotal ? (
            <Empty
              icon="target"
              title="Pas encore de résultat."
              text="Le taux de réussite apparaîtra après la première session terminée."
            />
          ) : (
            <>
              <div
                className="donut-wrap"
                role="img"
                aria-label={`Taux de réussite ${pct(facts.winRate)} sur ${facts.closed} sessions terminées.`}
              >
                <ResponsiveContainer width="100%" height="100%" minWidth={0}>
                  <PieChart>
                    <Pie
                      data={outcomes}
                      dataKey="value"
                      nameKey="label"
                      innerRadius="67%"
                      outerRadius="88%"
                      paddingAngle={2}
                      stroke="none"
                      isAnimationActive={false}
                    >
                      {outcomes.map((row) => (
                        <Cell key={row.key} fill={outcomeColors[row.key]} />
                      ))}
                    </Pie>
                    <Tooltip
                      contentStyle={tooltipStyle}
                      formatter={(value, name) => [
                        `${value} session${Number(value) > 1 ? "s" : ""}`,
                        name,
                      ]}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div className="donut-center" aria-hidden="true">
                  <strong>
                    {facts.closed ? pct(facts.winRate) : String(facts.running)}
                  </strong>
                  <span>{facts.closed ? "réussite" : "en cours"}</span>
                </div>
              </div>
              <ul className="chart-legend donut-legend">
                {outcomes.map((row) => (
                  <li key={row.key}>
                    <i style={{ background: outcomeColors[row.key] }} />
                    {row.label}
                    <strong>{row.value}</strong>
                  </li>
                ))}
              </ul>
              <div className="donut-facts">
                <span>
                  Profit factor <strong>{factor(facts.profitFactor)}</strong>
                </span>
                <span>
                  Durée moyenne{" "}
                  <strong>{facts.avgDuration == null ? "—" : `${Math.round(facts.avgDuration / 60)} min`}</strong>
                </span>
              </div>
            </>
          )}
        </section>
      </div>

      <div className="performance-charts performance-charts-bottom">
        <section
          className="panel chart-panel"
          data-layro="legions-developer/recharts-bar-chart"
        >
          <header className="chart-head">
            <div>
              <span className="micro-label">
                {buckets.granularity === "day" ? "Par jour" : "Par session"}
              </span>
              <h2>Volume et résultat</h2>
              <p className="chart-subtitle">Volume exécuté et net réalisé.</p>
            </div>
            <div className="chart-key" aria-hidden="true">
              <span><i style={{ background: colors.purple }} />Volume</span>
              <span><i style={{ background: colors.gold }} />Net</span>
            </div>
          </header>
          {!buckets.points.length ? (
            <Empty
              icon="chart"
              title="Aucune session sur cette période."
              text="Les volumes exécutés apparaîtront ici."
            />
          ) : (
            <div
              className="chart-frame"
              role="img"
              aria-label="Volume exécuté et résultat net par période ; valeurs exactes dans le journal ci-dessous."
            >
              <ResponsiveContainer width="100%" height="100%" minWidth={0}>
                <ComposedChart
                  data={buckets.points}
                  margin={{ top: 12, right: 4, bottom: 0, left: 0 }}
                >
                  <defs>
                    <linearGradient id={volumeFill} x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={colors.purple} stopOpacity={0.9} />
                      <stop offset="100%" stopColor={colors.purple} stopOpacity={0.28} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid vertical={false} stroke="#94a3b81a" />
                  <XAxis
                    dataKey="label"
                    tickLine={false}
                    axisLine={false}
                    interval="preserveStartEnd"
                    minTickGap={22}
                    tick={{ fill: colors.slate, fontSize: 11 }}
                    tickMargin={10}
                  />
                  <YAxis
                    yAxisId="volume"
                    tickLine={false}
                    axisLine={false}
                    width={54}
                    tick={{ fill: colors.slate, fontSize: 11 }}
                    tickFormatter={(value) => axisUsd(Number(value))}
                  />
                  <YAxis
                    yAxisId="net"
                    orientation="right"
                    tickLine={false}
                    axisLine={false}
                    width={48}
                    tick={{ fill: colors.slate, fontSize: 10 }}
                    tickFormatter={(value) => axisUsd(Number(value))}
                  />
                  <ReferenceLine
                    yAxisId="net"
                    y={0}
                    stroke="#94a3b866"
                    strokeDasharray="3 4"
                  />
                  <Tooltip content={<BucketTooltip granularity={buckets.granularity} />} />
                  <Bar
                    yAxisId="volume"
                    dataKey="volume"
                    fill={`url(#${volumeFill})`}
                    radius={[5, 5, 0, 0]}
                    maxBarSize={36}
                    isAnimationActive={false}
                  />
                  <Line
                    yAxisId="net"
                    type="monotone"
                    dataKey="net"
                    stroke={colors.gold}
                    strokeWidth={2}
                    dot={buckets.points.length < 4 ? { r: 2.5, fill: colors.gold } : false}
                    activeDot={{ r: 4, strokeWidth: 0 }}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          )}
        </section>

        <section className="panel chart-panel market-panel">
          <header className="chart-head">
            <div>
              <span className="micro-label">Top volumes</span>
              <h2>Marchés</h2>
              <p className="chart-subtitle">Résultat net par marché.</p>
            </div>
          </header>
          {!markets.length ? (
            <Empty
              icon="sessions"
              title="Aucun marché."
              text="Les marchés actifs apparaîtront ici."
            />
          ) : (
            <ul className="market-bars">
              {markets.map((row) => {
                const net = row.net;
                const width =
                  net == null
                    ? 0
                    : Math.max(4, (Math.abs(net) / maxMarketNet) * 100);
                return (
                  <li className="market-row" key={row.key}>
                    <div className="market-row-head">
                      <span className="market-row-name" title={row.name}>
                        {row.name}
                      </span>
                      <span className={`venue-badge ${row.venue}`}>
                        {venueLabel(row.venue)}
                      </span>
                      <strong className={(net ?? 0) >= 0 ? "positive" : "negative"}>
                        {usd(net)}
                      </strong>
                    </div>
                    <div className="market-row-meta">
                      <span>{usd(row.volume)} volume</span>
                      <span>{pct(row.winRate)} réussite</span>
                      <span>
                        {row.sessions} session{row.sessions > 1 ? "s" : ""}
                      </span>
                    </div>
                    <div className="market-track" aria-hidden="true">
                      <i
                        className={(net ?? 0) >= 0 ? "positive" : "negative"}
                        style={{ width: `${width}%` }}
                      />
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>

      {incomplete && (
        <InlineAlert tone="warning">
          Certaines sessions n’ont pas encore un comptage complet. Les valeurs
          manquantes ne sont pas remplacées par zéro.
        </InlineAlert>
      )}
    </div>
  );
}
