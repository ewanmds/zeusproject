import type { ReactNode } from "react";
import { Tabs } from "@base-ui/react/tabs";
// Layro: preetsuthar17/tabs — real tab/tabpanel relationships and roving keyboard focus.
export function DetailTabs({
  items,
}: {
  items: { id: string; label: string; content: ReactNode }[];
}) {
  return (
    <Tabs.Root
      defaultValue={items[0]?.id}
      className="detail-tabs"
      data-layro="preetsuthar17/tabs"
    >
      <Tabs.List className="detail-tabs-list">
        {items.map((item) => (
          <Tabs.Tab key={item.id} value={item.id}>
            {item.label}
          </Tabs.Tab>
        ))}
      </Tabs.List>
      {items.map((item) => (
        <Tabs.Panel key={item.id} value={item.id} className="detail-tab-panel">
          {item.content}
        </Tabs.Panel>
      ))}
    </Tabs.Root>
  );
}
