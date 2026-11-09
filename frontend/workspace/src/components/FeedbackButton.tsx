// Adapted from @preetsuthar17/button on Layro (MIT).
// Status stays controlled by the real command: a resolved promise is not proof of a fill.
import type { ComponentProps } from "react";
import { Pending } from "../ui";

export function FeedbackButton({
  loading = false, loadingLabel = "En cours", disabled, className = "", children, ...props
}: ComponentProps<"button"> & { loading?: boolean; loadingLabel?: string }) {
  return (
    <>
      <button {...props} type={props.type || "button"} className={`feedback-button ${className}`}
        data-layro="preetsuthar17/button"
        disabled={disabled || loading} aria-busy={loading || undefined} data-loading={loading || undefined}>
        <span className="feedback-content">
          <span className="feedback-idle" aria-hidden={loading || undefined}>{children}</span>
          <span className="feedback-loading" aria-hidden={!loading}>
            <Pending label={loadingLabel} />
          </span>
        </span>
      </button>
      <span className="sr-only" role="status">{loading ? loadingLabel : ""}</span>
    </>
  );
}
