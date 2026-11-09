import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { Tabs } from "@base-ui/react/tabs";
import { Icon } from "../icons";
import { nav } from "../config";
import type { View } from "../types";

// Layro: magicuidesign/dock — stable hit areas, icon response only on fine pointers.
export function NavDock({ view, active }: { view: View; active: number }) {
  return <nav className="nav layro-dock" aria-label="Navigation principale" data-layro="magicuidesign/dock">
    {nav.map((item) => <a key={item.id} href={"#" + item.id} className={view === item.id ? "active" : ""}
      aria-current={view === item.id ? "page" : undefined}><Icon name={item.icon} size={17} /><span>{item.name}</span>
      {item.id === "sessions" && active > 0 && <span className="count">{active}</span>}</a>)}
  </nav>;
}
// Layro: preetsuthar17/tabs — real tab/tabpanel relationships and roving keyboard focus.
export function DetailTabs({ items }: { items: { id: string; label: string; content: ReactNode }[] }) {
  return <Tabs.Root defaultValue={items[0]?.id} className="detail-tabs" data-layro="preetsuthar17/tabs">
    <Tabs.List className="detail-tabs-list">{items.map((item) =>
      <Tabs.Tab key={item.id} value={item.id}>{item.label}</Tabs.Tab>)}</Tabs.List>
    {items.map((item) => <Tabs.Panel key={item.id} value={item.id} className="detail-tab-panel">{item.content}</Tabs.Panel>)}
  </Tabs.Root>;
}
// Layro: educlopez/animated-stepper — controlled, evidence-based completion.
export function SessionStepper({ connected, ready }: { connected: boolean; ready: boolean }) {
  const current = !connected ? 0 : !ready ? 1 : 2;
  return <ol className="session-steps" aria-label="Étapes du lancement" data-layro="educlopez/animated-stepper">
    {["Wallet", "Autorisation", "Session"].map((label, index) =>
      <li className={index < current ? "done" : index === current ? "current" : ""} aria-current={index === current ? "step" : undefined} key={label}>
        <span className="session-step-mark">{index < current ? <Icon name="check" size={11} /> : index + 1}</span>{label}</li>)}
  </ol>;
}
// Layro: preetsuthar17/scroll-area — overflow edges and a keyboard-accessible region.
export function ScrollArea({ children, label, className = "" }: { children: ReactNode; label: string; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => {
      const overflowing = el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
      el.tabIndex = overflowing ? 0 : -1;
      el.toggleAttribute("data-more", el.scrollTop + el.clientHeight < el.scrollHeight - 1);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    if (el.firstElementChild) observer.observe(el.firstElementChild);
    el.addEventListener("scroll", update, { passive: true });
    return () => { observer.disconnect(); el.removeEventListener("scroll", update); };
  }, []);
  return <div ref={ref} role="region" aria-label={label} className={"scroll-area " + className} data-layro="preetsuthar17/scroll-area">{children}</div>;
}
