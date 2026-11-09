import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Icon } from "../icons";

// Layro: educlopez/dialog — native focus trap, labelled content and safe focus return.
export function Dialog({
  title,
  children,
  onClose,
  wide = false,
  variant = "dialog",
  locked = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
  variant?: "dialog" | "sheet" | "alert";
  locked?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const old = document.activeElement as HTMLElement | null;
    ref.current?.showModal();
    return () => {
      if (old?.isConnected) old.focus({ preventScroll: true });
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className={
        "modal " +
        (wide ? "wide " : "") +
        (variant === "sheet" ? "zeus-sheet" : "")
      }
      aria-label={title}
      role={variant === "alert" ? "alertdialog" : undefined}
      data-layro={
        variant === "sheet"
          ? "preetsuthar17/sheet"
          : variant === "alert"
            ? "preetsuthar17/alert-dialog"
            : "educlopez/dialog"
      }
      onCancel={(e) => {
        e.preventDefault();
        if (!locked) onClose();
      }}
      onClick={(e) => {
        const box = e.currentTarget.getBoundingClientRect();
        if (
          e.target === e.currentTarget &&
          !locked &&
          (e.clientX < box.left ||
            e.clientX > box.right ||
            e.clientY < box.top ||
            e.clientY > box.bottom)
        )
          onClose();
      }}
    >
      {variant === "sheet" && (
        <div className="sheet-handle" aria-hidden="true" />
      )}
      <div className="modal-head">
        <div>
          <span className="eyebrow">ZEUS WORKSPACE</span>
          <h2>{title}</h2>
        </div>
        <button
          className="icon-btn"
          onClick={onClose}
          disabled={locked}
          aria-label="Fermer"
        >
          <Icon name="close" />
        </button>
      </div>
      {children}
    </dialog>
  );
}
// Layro: preetsuthar17/sheet — edge panel with a single mounted form and focus trap.
export function Sheet(props: Omit<Parameters<typeof Dialog>[0], "variant">) {
  return <Dialog {...props} variant="sheet" />;
}
// Layro: preetsuthar17/alert-dialog — explicit confirmation for an important action.
export function AlertDialog(
  props: Omit<Parameters<typeof Dialog>[0], "variant">,
) {
  return <Dialog {...props} variant="alert" />;
}
export function useMobile() {
  const [mobile, setMobile] = useState(
    () => window.matchMedia("(max-width: 600px)").matches,
  );
  useEffect(() => {
    const query = window.matchMedia("(max-width: 600px)");
    const change = () => setMobile(query.matches);
    query.addEventListener("change", change);
    return () => query.removeEventListener("change", change);
  }, []);
  return mobile;
}
// Layro: ncdai/apple-accordion — controlled progressive disclosure with stateful content.
export function ResponsiveOptions({
  summary,
  children,
}: {
  summary: ReactNode;
  children: ReactNode;
}) {
  const mobile = useMobile();
  const [open, setOpen] = useState(false);
  if (mobile)
    return (
      <>
        <button
          className="advanced options-trigger"
          onClick={() => setOpen(true)}
        >
          {summary}
        </button>
        {open && (
          <Sheet title="Options de session" onClose={() => setOpen(false)}>
            {children}
            <button
              className="button primary full"
              onClick={() => setOpen(false)}
            >
              Terminer les réglages
            </button>
          </Sheet>
        )}
      </>
    );
  return (
    <details
      className="advanced zeus-accordion"
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
      data-layro="ncdai/apple-accordion"
    >
      <summary>{summary}</summary>
      {children}
    </details>
  );
}
