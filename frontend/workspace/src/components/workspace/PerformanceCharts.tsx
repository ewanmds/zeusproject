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
import { aggregate } from "../../presentation/session";
import { Empty, InlineAlert } from "../feedback";
const colors = ["#8b5cf6", "#f59e0b", "#34d399"];
export default function PerformanceCharts({ history }: { history: Session[] }) {
  const data = history
    .slice()
    .reverse()
    .map((s, index) => ({
      name: String(index + 1),
      market: s.name,
      volume: s.volume_usd,
    }));
  const complete = aggregate(history, "volume_usd") != null;
  const breakdown = strategies.map((st) => ({
    name: st.name,
    value:
      aggregate(
        history.filter((s) => s.strategy === st.id),
        "volume_usd",
      ) ?? 0,
  }));
  const total = breakdown.reduce((sum, row) => sum + row.value, 0);
  return (
    <div className="performance-charts">
      <section
        className="panel chart-panel"
        data-layro="legions-developer/recharts-bar-chart"
      >
        <h2>Volume par session</h2>
        {!history.length ? (
          <Empty
            icon="chart"
            title="Aucune session sur cette période."
            text="Les volumes exécutés apparaîtront ici."
          />
        ) : (
          <div
            className="chart-frame"
            role="img"
            aria-label="Volume exécuté par session ; valeurs exactes dans le tableau ci-dessous."
          >
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <BarChart
                data={data}
                margin={{ top: 20, right: 14, bottom: 4, left: 4 }}
              >
                <CartesianGrid vertical={false} stroke="#94a3b81a" />
                <XAxis
                  dataKey="name"
                  tickLine={false}
                  axisLine={false}
                  stroke="#94a3b8"
                  fontSize={12}
                />
                <YAxis
                  tickLine={false}
                  axisLine={false}
                  stroke="#94a3b8"
                  fontSize={11}
                  width={52}
                />
                <Tooltip
                  cursor={{ fill: "#8b5cf610" }}
                  formatter={(value) => [usd(Number(value)), "Volume"]}
                  labelFormatter={(_label, payload) =>
                    payload?.[0]?.payload?.market || "Session"
                  }
                  contentStyle={{
                    background: "#0f172a",
                    border: "1px solid #94a3b840",
                    borderRadius: 8,
                  }}
                />
                <Bar
                  dataKey="volume"
                  fill="#8b5cf6"
                  radius={[5, 5, 0, 0]}
                  isAnimationActive={false}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
        {!complete && (
          <InlineAlert tone="warning">
            Certaines valeurs sont indisponibles ; les données manquantes ne
            sont pas représentées par zéro.
          </InlineAlert>
        )}
      </section>
      <section
        className="panel chart-panel"
        data-layro="legions-developer/recharts-pie-chart"
      >
        <h2>Volume par stratégie</h2>
        {!complete ? (
          <Empty
            title="Données incomplètes."
            text="La répartition sera disponible après vérification."
          />
        ) : total <= 0 ? (
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
                    {breakdown.map((row, i) => (
                      <Cell key={row.name} fill={colors[i]} />
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
              {breakdown.map((row, i) => (
                <li key={row.name}>
                  <i style={{ background: colors[i] }} />
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
