import {
  cloneElement,
  isValidElement,
  lazy,
  Suspense,
  useId,
  useState,
} from "react";
import type { ComponentProps, ReactNode } from "react";
import { Icon } from "../icons";

// Layro: nolly-studio/halo-input — focus rim, slots, error semantics and native precision.
export function HaloInput(props: ComponentProps<"input">) {
  return (
    <input
      {...props}
      className={"halo-input " + (props.className || "")}
      data-layro="nolly-studio/halo-input"
    />
  );
}
// Layro: nolly-studio/halo-field — linked label, description and error slots.
export function HaloField({
  label,
  children,
  error,
  description,
}: {
  label: string;
  children: ReactNode;
  error?: string;
  description?: string;
}) {
  const id = useId();
  const [touched, setTouched] = useState(false);
  let input = children;
  let message = error;
  let inputId = id + "-input";
  if (isValidElement<ComponentProps<"input">>(children)) {
    const props = children.props;
    inputId = props.id || inputId;
    const value = props.value === "" ? null : Number(props.value);
    if (!message && touched && props.type === "number") {
      if (value == null || !Number.isFinite(value))
        message = "Entrez un montant valide.";
      else if (props.min != null && value < Number(props.min))
        message = "Minimum : " + props.min;
      else if (props.max != null && value > Number(props.max))
        message = "Maximum : " + props.max;
    }
    input = cloneElement(children, {
      id: inputId,
      "aria-invalid": !!message || undefined,
      "aria-describedby":
        [
          props["aria-describedby"],
          description ? id + "-description" : "",
          message ? id : "",
        ]
          .filter(Boolean)
          .join(" ") || undefined,
      onBlur: (event) => {
        setTouched(true);
        props.onBlur?.(event);
      },
    });
  }
  return (
    <div className="halo-field" data-layro="nolly-studio/halo-field">
      <label htmlFor={inputId}>{label}</label>
      {input}
      {description && (
        <p id={id + "-description"} className="field-hint">
          {description}
        </p>
      )}
      {message && (
        <p id={id} className="error-text" role="alert">
          {message}
        </p>
      )}
    </div>
  );
}
// Layro: educlopez/rich-popover — load managed popup code on the first interaction.
const PopoverContent = lazy(() =>
  import("./PopoverContent").then((m) => ({ default: m.PopoverContent })),
);
export function RichPopover({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  const [requested, setRequested] = useState(false);
  const trigger = (
    <button
      className="help-trigger"
      disabled={requested}
      aria-busy={requested || undefined}
      aria-label={label}
      data-layro="educlopez/rich-popover"
      onClick={() => setRequested(true)}
    >
      <Icon name="info" size={15} />
    </button>
  );
  return requested ? (
    <Suspense fallback={trigger}>
      <PopoverContent label={label}>{children}</PopoverContent>
    </Suspense>
  ) : (
    trigger
  );
}
// Layro: xxtomm/keyboard-shortcuts — readable platform-independent keycaps.
export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="keycap" data-layro="xxtomm/keyboard-shortcuts">
      {children}
    </kbd>
  );
}
