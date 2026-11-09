import { Select } from "@base-ui/react/select";
import { Icon } from "../icons";
// Layro: preetsuthar17/select — headless Base UI list, keyboard and typeahead.
export function MarketSelect({
  value,
  options,
  onChange,
  search = "",
}: {
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
  search?: string;
}) {
  const filtered = options.filter((option) =>
    option.label.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <Select.Root
      value={value || null}
      onValueChange={(next) => {
        if (next != null) onChange(next);
      }}
      items={options}
    >
      <Select.Trigger
        id="market"
        className="market-select"
        aria-label="Marché HIP-3"
        data-layro="preetsuthar17/select"
      >
        <Select.Value>
          {() =>
            options.find((option) => option.value === value)?.label ||
            value ||
            "Choisir un marché"
          }
        </Select.Value>
        <Select.Icon>
          <Icon name="chevron" size={16} />
        </Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Positioner sideOffset={6} className="select-positioner">
          <Select.Popup className="select-popup">
            <Select.List>
              {filtered.length ? (
                filtered.map((option) => (
                  <Select.Item
                    className="select-item"
                    key={option.value}
                    value={option.value}
                  >
                    <Select.ItemText>{option.label}</Select.ItemText>
                    <Select.ItemIndicator>
                      <Icon name="check" size={15} />
                    </Select.ItemIndicator>
                  </Select.Item>
                ))
              ) : (
                <p className="field-hint">Aucun marché trouvé.</p>
              )}
            </Select.List>
          </Select.Popup>
        </Select.Positioner>
      </Select.Portal>
    </Select.Root>
  );
}
