import { cloneElement, isValidElement, useId, useState } from "react";
import type { ComponentProps, ReactNode } from "react";
import { Select } from "@base-ui/react/select";
import { Switch } from "@base-ui/react/switch";
import { Popover } from "@base-ui/react/popover";
import { Icon } from "../icons";

// Layro: nolly-studio/halo-input — focus rim, slots, error semantics and native precision.
export function HaloInput(props: ComponentProps<"input">) {
  return <input {...props} className={"halo-input " + (props.className || "")} data-layro="nolly-studio/halo-input" />;
}
// Layro: nolly-studio/halo-field — linked label, description and error slots.
export function HaloField({ label, children, error, description }: {
  label: string; children: ReactNode; error?: string; description?: string;
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
      if (value == null || !Number.isFinite(value)) message = "Entrez un montant valide.";
      else if (props.min != null && value < Number(props.min)) message = "Minimum : " + props.min;
      else if (props.max != null && value > Number(props.max)) message = "Maximum : " + props.max;
    }
    input = cloneElement(children, {
      id: inputId, "aria-invalid": !!message || undefined,
      "aria-describedby": [props["aria-describedby"], description ? id + "-description" : "", message ? id : ""].filter(Boolean).join(" ") || undefined,
      onBlur: (event) => { setTouched(true); props.onBlur?.(event); },
    });
  }
  return <div className="halo-field" data-layro="nolly-studio/halo-field">
    <label htmlFor={inputId}>{label}</label>{input}
    {description && <p id={id + "-description"} className="field-hint">{description}</p>}
    {message && <p id={id} className="error-text" role="alert">{message}</p>}
  </div>;
}
// Layro: preetsuthar17/select — headless Base UI list, keyboard and typeahead.
export function MarketSelect({ value, options, onChange, search = "" }: {
  value: string; options: { value: string; label: string }[]; onChange: (value: string) => void; search?: string;
}) {
  const filtered = options.filter((option) => option.label.toLowerCase().includes(search.toLowerCase()));
  return <Select.Root value={value || null} onValueChange={(next) => { if (next != null) onChange(next); }} items={options}>
    <Select.Trigger id="market" className="market-select" aria-label="Marché HIP-3" data-layro="preetsuthar17/select">
      <Select.Value>{() => options.find((option) => option.value === value)?.label || value || "Choisir un marché"}</Select.Value><Select.Icon><Icon name="chevron" size={16} /></Select.Icon>
    </Select.Trigger>
    <Select.Portal><Select.Positioner sideOffset={6} className="select-positioner">
      <Select.Popup className="select-popup"><Select.List>
        {filtered.length ? filtered.map((option) => <Select.Item className="select-item" key={option.value} value={option.value}>
          <Select.ItemText>{option.label}</Select.ItemText><Select.ItemIndicator><Icon name="check" size={15} /></Select.ItemIndicator>
        </Select.Item>) : <p className="field-hint">Aucun marché trouvé.</p>}
      </Select.List></Select.Popup>
    </Select.Positioner></Select.Portal>
  </Select.Root>;
}
// Layro: preetsuthar17/switch — controlled preference, independent of trading state.
export function ViewSwitch({ checked, onChange }: { checked: boolean; onChange: (value: boolean) => void }) {
  return <label className="view-switch"><span>Vue compacte</span>
    <Switch.Root checked={checked} onCheckedChange={onChange} className="switch-root" data-layro="preetsuthar17/switch">
      <Switch.Thumb className="switch-thumb" />
    </Switch.Root>
  </label>;
}
// Layro: educlopez/rich-popover — touch/keyboard help with managed dismissal.
export function RichPopover({ label, children }: { label: string; children: ReactNode }) {
  return <Popover.Root><Popover.Trigger className="help-trigger" aria-label={label} data-layro="educlopez/rich-popover"><Icon name="info" size={15} /></Popover.Trigger>
    <Popover.Portal><Popover.Positioner sideOffset={8} className="popover-positioner"><Popover.Popup className="rich-popover">
      <Popover.Title>{label}</Popover.Title><Popover.Description render={<div />}>{children}</Popover.Description>
      <Popover.Close className="button subtle small">Fermer</Popover.Close>
    </Popover.Popup></Popover.Positioner></Popover.Portal>
  </Popover.Root>;
}
// Layro: xxtomm/keyboard-shortcuts — readable platform-independent keycaps.
export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="keycap" data-layro="xxtomm/keyboard-shortcuts">{children}</kbd>;
}
