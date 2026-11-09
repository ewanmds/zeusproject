import { Switch } from "@base-ui/react/switch";
// Layro: preetsuthar17/switch — controlled preference, independent of trading state.
export function ViewSwitch({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label className="view-switch">
      <span>Vue compacte</span>
      <Switch.Root
        checked={checked}
        onCheckedChange={onChange}
        className="switch-root"
        data-layro="preetsuthar17/switch"
      >
        <Switch.Thumb className="switch-thumb" />
      </Switch.Root>
    </label>
  );
}
