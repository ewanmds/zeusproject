import { useId, type ReactNode } from "react";
import { Icon } from "../icons";

// Layro: turbostarter/fade-arc — CSS arc, indeterminate, with a textual status.
export function ArcLoader({ label = "Chargement" }: { label?: string }) {
  const id = useId();
  return <span className="pending" data-layro="turbostarter/fade-arc"><svg className="fade-arc" width="16" height="16" viewBox="0 0 24 24" aria-hidden="true">
    <defs><linearGradient id={id}><stop stopColor="currentColor" /><stop offset="1" stopColor="currentColor" stopOpacity=".15" /></linearGradient></defs>
    <circle cx="12" cy="12" r="9" fill="none" stroke={`url(#${id})`} strokeWidth="2" strokeDasharray="43 14" />
  </svg>{label}</span>;
}
// Layro: educlopez/alert — semantic inline variants and a dismiss action.
export function InlineAlert({ children, tone = "error", onClose }: {
  children: ReactNode; tone?: "error" | "warning" | "info"; onClose?: () => void;
}) {
  return <div className={"inline-alert " + tone} role={tone === "error" ? "alert" : "status"} data-layro="educlopez/alert">
    <Icon name={tone === "info" ? "info" : "alert"} size={17} /><div>{children}</div>
    {onClose && <button className="icon-btn" onClick={onClose} aria-label="Fermer le message"><Icon name="close" size={16} /></button>}
  </div>;
}
// Layro: nolly-studio/halo-notification — persistent, dismissible command feedback.
export function Notification({ message, onClose }: { message: string; onClose: () => void }) {
  return <div className="zeus-notification" role="status" data-layro="nolly-studio/halo-notification">
    <span className="notification-glyph"><Icon name="bell" size={18} /></span>
    <span>{message}</span><button className="icon-btn" onClick={onClose} aria-label="Fermer la notification"><Icon name="close" size={16} /></button>
  </div>;
}
// Layro: preetsuthar17/skeleton — reserved loading dimensions, never a fake zero.
export function Skeleton({ label = "Chargement des données", compact = false }: { label?: string; compact?: boolean }) {
  return <div className={compact ? "skeleton-lines" : "metrics-grid loading-grid"} role="status" aria-label={label} data-layro="preetsuthar17/skeleton">
    {(compact ? [0, 1] : [0, 1, 2, 3]).map((n) => <div className={compact ? "" : "panel metric"} key={n}><span className="skeleton-line" />{!compact && <span className="skeleton-line wide" />}</div>)}
    <span className="sr-only">{label}</span>
  </div>;
}
// Layro: preetsuthar17/empty — icon, title, explanation and a contextual next action.
export function Empty({ icon = "sessions", title, text, action }: {
  icon?: string; title: string; text: string; action?: ReactNode;
}) {
  return <div className="empty" data-layro="preetsuthar17/empty">
    <span className="empty-icon"><Icon name={icon} size={28} /></span>
    <h2>{title}</h2><p>{text}</p>{action}
  </div>;
}
// Layro: educlopez/stats-2 — responsive stat cards without fabricated trend badges.
export function StatsGrid({ items }: { items: { label: string; value: ReactNode; icon?: string; negative?: boolean; help?: ReactNode }[] }) {
  return <div className="metrics-grid" data-layro="educlopez/stats-2">{items.map((item) =>
    <div className="metric panel" key={item.label}><span className="metric-label">
      {item.icon && <Icon name={item.icon} size={17} />}{item.label}{item.help}
    </span><strong className={item.negative ? "negative" : ""}>{item.value}</strong></div>)}</div>;
}
