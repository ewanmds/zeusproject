/**
 * StickyLaunchBar — presentational launch affordance pinned above the
 * 66px mobile bottom nav.
 *
 * Usage:
 *   <StickyLaunchBar
 *     amount="$1 250.00"
 *     status="Prêt à lancer"
 *     label="Lancer la session"
 *     loading={launching}
 *     disabled={!ready}
 *     error={launchError}
 *     onAction={launch}
 *     mobileOnly
 *   />
 *
 * `mobileOnly` renders nothing outside the mobile breakpoint, so no
 * duplicate action or accessible name is exposed alongside a desktop CTA.
 */
import { useEffect, useId, useState } from "react";
import "./sticky-launch.css";

const MOBILE_QUERY = "(max-width: 720px)";

function useMediaQuery(query: string) {
  const read = () =>
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(query).matches;

  const [matches, setMatches] = useState(read);

  useEffect(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    )
      return;
    const media = window.matchMedia(query);
    const sync = () => setMatches(media.matches);
    sync();
    media.addEventListener?.("change", sync);
    return () => media.removeEventListener?.("change", sync);
  }, [query]);

  return matches;
}

export interface StickyLaunchBarProps {
  /** Formatted amount displayed above the status, e.g. "$1 250.00". */
  amount: string;
  /** Short status line, truncated visually when space is tight. */
  status: string;
  /** Visible and accessible name of the single action button. */
  label: string;
  /** Visible label while loading; defaults to label. */
  loadingLabel?: string;
  /** Marks the button busy and prevents duplicate submissions. */
  loading?: boolean;
  /** Disables the action without a busy state. */
  disabled?: boolean;
  /** Optional failure message announced with role="alert". */
  error?: string | null;
  onAction: () => void;
  /** When true, the bar mounts only at <=720px, matching the bottom nav. */
  mobileOnly?: boolean;
}

export function StickyLaunchBar({
  amount,
  status,
  label,
  loadingLabel,
  loading = false,
  disabled = false,
  error,
  onAction,
  mobileOnly = false,
}: StickyLaunchBarProps) {
  const mobile = useMediaQuery(MOBILE_QUERY);
  const ids = useId();
  const amountId = `${ids}-amount`;
  const statusId = `${ids}-status`;

  if (mobileOnly && !mobile) return null;

  return (
    <div className="sticky-launch">
      <div className="sticky-launch__panel">
        <div className="sticky-launch__row">
          <div className="sticky-launch__info">
            <span
              className="sticky-launch__amount"
              id={amountId}
              title={amount}
            >
              {amount}
            </span>
            <span
              className="sticky-launch__status"
              id={statusId}
              title={status}
            >
              {status}
            </span>
          </div>
          <button
            type="button"
            className="sticky-launch__button"
            onClick={onAction}
            disabled={disabled || loading}
            aria-busy={loading}
            aria-describedby={`${amountId} ${statusId}`}
          >
            {loading && (
              <span className="sticky-launch__spinner" aria-hidden="true" />
            )}
            <span className="sticky-launch__label">
              {loading ? (loadingLabel ?? label) : label}
            </span>
          </button>
        </div>
        {error ? (
          <p className="sticky-launch__error" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}
