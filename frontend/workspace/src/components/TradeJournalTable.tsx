/**
 * TradeJournalTable — original Zeus trade journal card.
 *
 * Usage:
 *   import { TradeJournalTable } from "./components/TradeJournalTable";
 *
 *   <TradeJournalTable
 *     rows={trades}
 *     period={{
 *       value: period,
 *       options: [
 *         { value: "1", label: "24 h" },
 *         { value: "7", label: "7 j" },
 *         { value: "30", label: "30 j" },
 *         { value: "90", label: "90 j" },
 *       ],
 *       onChange: setPeriod,
 *     }}
 *     emptyAction={<button className="button primary">Ouvrir une session</button>}
 *   />
 *
 * Rows accept `date`/`time`/`started`, `market`/`name` and `size`/`volume`
 * aliases. Dates can be unix seconds, unix milliseconds, ISO strings or Date.
 * Statuses: "open" | "win" | "loss" | "stopped".
 */
import { useId } from "react";
import type { ReactNode } from "react";
import { clock, short, usd } from "../data";
import { ScrollArea } from "./layout";
import "../journal.css";

export type TradeJournalStatus = "open" | "win" | "loss" | "stopped";

export interface TradeJournalRow {
  id: string;
  date?: number | string | Date | null;
  time?: number | string | Date | null;
  started?: number | null;
  market?: string | null;
  name?: string | null;
  side?: string | null;
  size?: number | null;
  volume?: number | null;
  entry?: number | null;
  net_pnl?: number | null;
  status: TradeJournalStatus;
  duration_sec?: number | null;
}

export interface TradeJournalPeriodOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface TradeJournalPeriod {
  value: string;
  options: readonly TradeJournalPeriodOption[];
  onChange: (value: string) => void;
  label?: string;
}

export interface TradeJournalTableProps {
  rows: readonly TradeJournalRow[];
  loading?: boolean;
  period?: TradeJournalPeriod;
  title?: string;
  subtitle?: string;
  caption?: string;
  emptyState?: ReactNode;
  emptyTitle?: string;
  emptyText?: string;
  emptyAction?: ReactNode;
  skeletonRows?: number;
  loadingLabel?: string;
  className?: string;
}

const dateFormatter = new Intl.DateTimeFormat("fr-FR", {
  day: "2-digit",
  month: "short",
});
const dateWithYearFormatter = new Intl.DateTimeFormat("fr-FR", {
  day: "2-digit",
  month: "short",
  year: "numeric",
});
const timeFormatter = new Intl.DateTimeFormat("fr-FR", {
  hour: "2-digit",
  minute: "2-digit",
});
const fullDateFormatter = new Intl.DateTimeFormat("fr-FR", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

const statusMeta: Record<TradeJournalStatus, string> = {
  open: "Ouvert",
  win: "Gagné",
  loss: "Perdu",
  stopped: "Arrêté",
};

const skeletonSlots = [
  "date",
  "asset",
  "side",
  "size",
  "entry",
  "pnl",
  "status",
] as const;

type SideTone = "buy" | "sell" | "neutral";
type SideDirection = "up" | "down" | "flat";

function finite(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function toDate(
  value: number | string | Date | null | undefined,
): Date | null {
  if (value == null) return null;
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value) || value <= 0) return null;
    // Unix seconds are ~1.7e9; milliseconds are ~1.7e12.
    const milliseconds = value < 100_000_000_000 ? value * 1_000 : value;
    const parsed = new Date(milliseconds);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }
  const text = value.trim();
  if (!text) return null;
  if (/^\d+(\.\d+)?$/.test(text)) return toDate(Number(text));
  const parsed = new Date(text);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function sideInfo(side: string | null | undefined): {
  label: string;
  tone: SideTone;
  direction: SideDirection;
} {
  const raw = side?.trim();
  if (!raw) return { label: "—", tone: "neutral", direction: "flat" };
  const value = raw.toLowerCase();
  switch (value) {
    case "buy":
    case "bid":
      return {
        label: value === "bid" ? "Bid" : "Achat",
        tone: "buy",
        direction: "up",
      };
    case "yes":
      return { label: "Oui", tone: "buy", direction: "up" };
    case "long":
      return { label: "Long", tone: "buy", direction: "up" };
    case "sell":
    case "ask":
      return {
        label: value === "ask" ? "Ask" : "Vente",
        tone: "sell",
        direction: "down",
      };
    case "no":
      return { label: "Non", tone: "sell", direction: "down" };
    case "short":
      return { label: "Short", tone: "sell", direction: "down" };
    default:
      return { label: raw, tone: "neutral", direction: "flat" };
  }
}

function statusOf(value: TradeJournalStatus): TradeJournalStatus {
  const status = String(value ?? "").toLowerCase();
  if (status === "win" || status === "won") return "win";
  if (status === "loss" || status === "lost") return "loss";
  if (status === "stopped" || status === "stop") return "stopped";
  return "open";
}

function StatusIcon({ status }: { status: TradeJournalStatus }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {status === "win" ? (
        <path d="m5 12.5 4.2 4.2L19 7" />
      ) : status === "loss" ? (
        <path d="M6.5 6.5l11 11m0-11-11 11" />
      ) : status === "stopped" ? (
        <rect x="7" y="7" width="10" height="10" rx="2" />
      ) : (
        <>
          <circle cx="12" cy="12" r="7.5" />
          <circle cx="12" cy="12" r="2.5" fill="currentColor" stroke="none" />
        </>
      )}
    </svg>
  );
}

function SideIcon({ direction }: { direction: SideDirection }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="13"
      height="13"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {direction === "up" ? (
        <path d="M12 18V6m0 0-4.5 4.5M12 6l4.5 4.5" />
      ) : direction === "down" ? (
        <path d="M12 6v12m0 0 4.5-4.5M12 18l-4.5-4.5" />
      ) : (
        <path d="M6 12h12" />
      )}
    </svg>
  );
}

function EmptyGlyph() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="26"
      height="26"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M5.5 4.5h10.2A3.3 3.3 0 0 1 19 7.8V20H8.8a3.3 3.3 0 0 1-3.3-3.3V4.5Z" />
      <path d="M5.5 16.7h13.5" />
      <path d="M9 8.3h6.3M9 11.8h4.5" />
    </svg>
  );
}

function JournalRowView({ row }: { row: TradeJournalRow }) {
  const date =
    toDate(row.date) ?? toDate(row.time) ?? toDate(row.started);
  const market = row.market?.trim() || null;
  const rawName = row.name?.trim() || null;
  const compactMarket =
    market && market.length > 20 && !market.includes(" ")
      ? short(market)
      : market;
  const name = rawName || compactMarket || "Marché inconnu";
  const marketRef =
    rawName &&
    market &&
    market !== name &&
    market.length > 20 &&
    !market.includes(" ")
      ? short(market)
      : null;
  const side = sideInfo(row.side);
  const size = finite(row.size) ?? finite(row.volume);
  const entry = finite(row.entry);
  const pnl = finite(row.net_pnl);
  const duration = finite(row.duration_sec);
  const status = statusOf(row.status);
  const pnlValue = pnl == null ? "—" : (pnl > 0 ? "+" : "") + usd(pnl);
  const pnlTone =
    pnl == null
      ? "unknown"
      : pnl > 0
        ? "positive"
        : pnl < 0
          ? "negative"
          : "flat";
  const pnlLabel =
    pnl == null
      ? "Net P&L indisponible"
      : pnl > 0
        ? `Net P&L positif : ${pnlValue}`
        : pnl < 0
          ? `Net P&L négatif : ${pnlValue}`
          : `Net P&L nul : ${pnlValue}`;
  const fullDate = date ? fullDateFormatter.format(date) : "Date indisponible";
  const day = date
    ? date.getFullYear() === new Date().getFullYear()
      ? dateFormatter.format(date)
      : dateWithYearFormatter.format(date)
    : "—";

  return (
    <tr data-status={status}>
      <td className="journal-cell journal-date-cell" data-label="Date">
        <span className="journal-date" title={fullDate}>
          <span className="journal-date__day">{day}</span>
          <span className="journal-date__time">
            {date ? timeFormatter.format(date) : "—"}
            {date && duration != null && (
              <span className="journal-date__duration"> · {clock(duration)}</span>
            )}
          </span>
        </span>
      </td>
      <td className="journal-cell journal-asset-cell" data-label="Marché">
        <span className="journal-asset">
          <span className="journal-asset__name">{name}</span>
          {marketRef && (
            <span className="journal-asset__ref" title={market ?? undefined}>
              {marketRef}
            </span>
          )}
        </span>
      </td>
      <td className="journal-cell journal-side-cell" data-label="Sens">
        <span className={`journal-side journal-side--${side.tone}`}>
          <SideIcon direction={side.direction} />
          <span>{side.label}</span>
        </span>
      </td>
      <td className="journal-cell journal-size-cell" data-label="Taille">
        <span className="journal-number">{usd(size)}</span>
      </td>
      <td className="journal-cell journal-entry-cell" data-label="Entrée">
        <span className="journal-number">{usd(entry)}</span>
      </td>
      <td className="journal-cell journal-pnl-cell" data-label="Net P&L">
        <span className={`journal-pnl journal-pnl--${pnlTone}`}>
          <span aria-hidden="true">{pnlValue}</span>
          <span className="sr-only">{pnlLabel}</span>
        </span>
      </td>
      <td className="journal-cell journal-status-cell" data-label="Statut">
        <span className={`journal-status journal-status--${status}`}>
          <StatusIcon status={status} />
          <span>{statusMeta[status]}</span>
        </span>
      </td>
    </tr>
  );
}

function JournalSkeleton({
  count,
  label,
}: {
  count: number;
  label: string;
}) {
  const rows = Number.isFinite(count)
    ? Math.min(20, Math.max(1, Math.floor(count)))
    : 5;
  return (
    <div
      className="trade-journal__skeleton"
      role="status"
      aria-live="polite"
    >
      {Array.from({ length: rows }, (_, index) => (
        <div className="journal-skeleton-row" key={index} aria-hidden="true">
          {skeletonSlots.map((slot) => (
            <span
              className={`journal-skeleton-cell journal-skeleton-cell--${slot}`}
              key={slot}
            />
          ))}
        </div>
      ))}
      <span className="sr-only">{label}</span>
    </div>
  );
}

function JournalEmpty({
  title,
  text,
  action,
}: {
  title: string;
  text: string;
  action?: ReactNode;
}) {
  return (
    <div className="trade-journal__empty">
      <span className="journal-empty__glyph">
        <EmptyGlyph />
      </span>
      <div role="status">
        <p className="journal-empty__title">{title}</p>
        <p className="journal-empty__text">{text}</p>
      </div>
      {action}
    </div>
  );
}

export function TradeJournalTable({
  rows,
  loading = false,
  period,
  title = "Journal des trades",
  subtitle,
  caption = "Journal des transactions",
  emptyState,
  emptyTitle = "Aucune transaction.",
  emptyText = "Les trades apparaîtront ici dès qu’une position sera ouverte ou clôturée.",
  emptyAction,
  skeletonRows = 5,
  loadingLabel = "Chargement du journal",
  className,
}: TradeJournalTableProps) {
  const headingId = useId();
  const rootClass = ["trade-journal", className].filter(Boolean).join(" ");
  const summary =
    subtitle ??
    (loading
      ? "Chargement des transactions"
      : rows.length === 1
        ? "1 transaction"
        : `${rows.length} transactions`);

  return (
    <section
      className={rootClass}
      aria-labelledby={headingId}
      aria-busy={loading || undefined}
    >
      <header className="trade-journal__header">
        <div className="trade-journal__heading">
          <span className="trade-journal__eyebrow">Journal</span>
          <h2 id={headingId}>{title}</h2>
          <p>{summary}</p>
        </div>
        {period && period.options.length > 0 && (
          <div
            className="trade-journal__period"
            role="group"
            aria-label={period.label ?? "Période du journal"}
          >
            {period.options.map((option) => (
              <button
                key={option.value}
                type="button"
                aria-pressed={option.value === period.value}
                disabled={loading || option.disabled}
                onClick={() => period?.onChange(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>
        )}
      </header>
      {loading ? (
        <JournalSkeleton count={skeletonRows} label={loadingLabel} />
      ) : rows.length === 0 ? (
        emptyState ?? (
          <JournalEmpty
            title={emptyTitle}
            text={emptyText}
            action={emptyAction}
          />
        )
      ) : (
        <ScrollArea label={caption}>
          <table>
            <caption className="sr-only">{caption}</caption>
            <thead>
              <tr>
                <th scope="col" className="journal-col-date">
                  Date
                </th>
                <th scope="col" className="journal-col-asset">
                  Marché
                </th>
                <th scope="col" className="journal-col-side">
                  Sens
                </th>
                <th scope="col" className="journal-col-size">
                  Taille
                </th>
                <th scope="col" className="journal-col-entry">
                  Entrée
                </th>
                <th scope="col" className="journal-col-pnl">
                  Net P&amp;L
                </th>
                <th scope="col" className="journal-col-status">
                  Statut
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <JournalRowView key={row.id} row={row} />
              ))}
            </tbody>
          </table>
        </ScrollArea>
      )}
    </section>
  );
}
