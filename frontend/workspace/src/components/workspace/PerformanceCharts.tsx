// Layro: legions-developer/recharts-bar-chart and recharts-pie-chart (MIT).
// Shared Recharts engine, real data only, loaded on the Performance route.
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  PieChart,
  Pie,
  Cell,
} from "recharts";
import type { Session } from "../../types";
import { strategies } from "../../config";
import { usd } from "../../data";
import {
  sumAvailable,
  volumeByPeriod,
  type Venue,
} from "../../presentation/performance";
import { Empty } from "../feedback";

const venueColors = {
  hip4: "#8b5cf6",
  hip3: "#f59e0b",
};
const strategyColors: Record<string, string> = {
  outcome: "#8b5cf6",
  bbo: "#f59e0b",
  mid: "#34d399",
};
const fallbackColors = ["#8b5cf6", "#f59e0b", "#34d399"];

function axisUsd(value: number) {
  const absolute = Math.abs(value);
  if (absolute >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`;
  if (absolute >= 1_000) return `$${(value / 1_000).toFixed(1)}k`;
  return `$${Math.round(value)}`;
}

export default function PerformanceCharts({
  history,
  period,
  venue,
}: {
  history: Session[];
  period: number;
  venue: Venue;
}) {
  const volume = volumeByPeriod(history, period);
  const breakdown = strategies
    .map((strategy) => ({
      id: strategy.id,
      name: strategy.name,
      value: sumAvailable(
        history.filter((session) => session.strategy === strategy.id),
        "volume_usd",
      ),
    }))
    .filter((row) => row.value > 0);
  const total = breakdown.reduce((sum, row) => sum + row.value, 0);
  const showHip4 = venue === "all" || venue === "hip4";
  const showHip3 = venue === "all" || venue === "hip3";
  const title =
    volume.granularity === "hour" ? "Volume par heure" : "Volume par jour";

  return (
    <div className="performance-charts">
      <section
        className="panel chart-panel"
        data-layro="legions-developer/recharts-bar-chart"
      >
        <div className="chart-title-row">
          <h2>{title}</h2>
          <div className="chart-key" aria-hidden="true">
            {showHip4 && (
              <span>
                <i style={{ background: venueColors.hip4 }} />
                HIP-4
              </span>
            )}
            {showHip3 && (
              <span>
                <i style={{ background: venueColors.hip3 }} />
                HIP-3
              </span>
            )}
          </div>
        </div>
        {history.length === 0 ? (
          <Empty
            icon="chart"
            title="Aucune session sur cette période."
            text="Les volumes exécutés apparaîtront ici."
          />
        ) : (
          <div
            className="chart-frame"
            role="img"
            aria-label={`${title} par famille de marchés ; valeurs exactes dans le tableau ci-dessous.`}
          >
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <BarChart
                data={volume.points}
                margin={{ top: 20, right: 14, bottom: 4, left: 4 }}
              >
                <CartesianGrid vertical={false} stroke="#94a3b81a" />
                <XAxis
                  dataKey="label"
                  tickLine={false}
                  axisLine={false}
                  interval="preserveStartEnd"
                  minTickGap={22}
                  stroke="#94a3b8"
                  fontSize={12}
                  tickMargin={10}
                />
                <YAxis
                  tickLine={false}
                  axisLine={false}
                  stroke="#94a3b8"
                  fontSize={11}
                  width={54}
                  tickFormatter={(value) => axisUsd(Number(value))}
                />
                <Tooltip
                  cursor={{ fill: "#8b5cf610" }}
                  formatter={(value, name) => [usd(Number(value)), name]}
                  labelFormatter={(label) => String(label)}
                  contentStyle={{
                    background: "#0f172a",
                    border: "1px solid #94a3b840",
                    borderRadius: 8,
                  }}
                />
                {showHip4 && (
                  <Bar
                    dataKey="hip4"
                    name="HIP-4"
                    stackId="volume"
                    fill={venueColors.hip4}
                    radius={showHip3 ? [0, 0, 0, 0] : [5, 5, 0, 0]}
                    maxBarSize={64}
                    isAnimationActive={false}
                  />
                )}
                {showHip3 && (
                  <Bar
                    dataKey="hip3"
                    name="HIP-3"
                    stackId="volume"
                    fill={venueColors.hip3}
                    radius={[5, 5, 0, 0]}
                    maxBarSize={64}
                    isAnimationActive={false}
                  />
                )}
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </section>

      <section
        className="panel chart-panel"
        data-layro="legions-developer/recharts-pie-chart"
      >
        <h2>Volume par stratégie</h2>
        {total <= 0 ? (
          <Empty
            icon="chart"
            title="Aucun volume exécuté."
            text="La répartition se construit avec les fills de vos sessions."
          />
        ) : (
          <>
            <div
              className="chart-frame pie-frame"
              role="img"
              aria-label="Répartition du volume par stratégie ; valeurs dans le tableau ci-dessous."
            >
              <ResponsiveContainer width="100%" height="100%" minWidth={0}>
                <PieChart>
                  <Pie
                    data={breakdown}
                    dataKey="value"
                    nameKey="name"
                    innerRadius="64%"
                    outerRadius="88%"
                    isAnimationActive={false}
                    stroke="none"
                  >
                    {breakdown.map((row, index) => (
                      <Cell
                        key={row.id}
                        fill={strategyColors[row.id] ?? fallbackColors[index]}
                      />
                    ))}
                  </Pie>
                  <Tooltip
                    formatter={(value) => usd(Number(value))}
                    contentStyle={{
                      background: "#0f172a",
                      border: "1px solid #94a3b840",
                      borderRadius: 8,
                    }}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <ul className="chart-legend">
              {breakdown.map((row, index) => (
                <li key={row.id}>
                  <i
                    style={{
                      background:
                        strategyColors[row.id] ?? fallbackColors[index],
                    }}
                  />
                  {row.name}
                  <strong>{usd(row.value)}</strong>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
    </div>
  );
}
