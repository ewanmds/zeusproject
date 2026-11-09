import { Popover } from "@base-ui/react/popover";
import type { ReactNode } from "react";
import { Icon } from "../icons";
// Layro: educlopez/rich-popover — touch/keyboard help with managed dismissal.
export function PopoverContent({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <Popover.Root defaultOpen>
      <Popover.Trigger
        className="help-trigger"
        aria-label={label}
        data-layro="educlopez/rich-popover"
      >
        <Icon name="info" size={15} />
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Positioner sideOffset={8} className="popover-positioner">
          <Popover.Popup className="rich-popover">
            <Popover.Title>{label}</Popover.Title>
            <Popover.Description render={<div />}>
              {children}
            </Popover.Description>
            <Popover.Close className="button subtle small">
              Fermer
            </Popover.Close>
          </Popover.Popup>
        </Popover.Positioner>
      </Popover.Portal>
    </Popover.Root>
  );
}
