import { useEffect, useId, useRef, useState } from "react";
import type { Session, View } from "../../types";
import { nav } from "../../config";
import { Icon } from "../../icons";
import { Dialog } from "../Dialog";
import { HaloInput, Kbd } from "../fields";
import { SegmentedControl } from "../SegmentedControl";
import { ScrollArea } from "../layout";
import { Empty } from "../feedback";

export type SearchResult = { id: string; kind: "page" | "market" | "session"; label: string; description: string; view?: View; market?: string; session?: Session };
// Layro: kokonut-labs/ai-input-search — scoped search tools, chips and real result sources.
// Searches the current workspace only; the reference's pretend AI run and demo data are removed.
export function GlobalSearch({ sessions, markets, onSelect, onClose }: {
  sessions: Session[]; markets: { key: string; symbol?: string; name?: string }[];
  onSelect: (result: SearchResult) => void; onClose: () => void;
}) {
  const [query, setQuery] = useState(""), [scope, setScope] = useState("all"), [index, setIndex] = useState(0);
  const input = useRef<HTMLInputElement>(null), id = useId();
  useEffect(() => { input.current?.focus(); }, []);
  const normalize = (text: string) => text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const results: SearchResult[] = [
    ...nav.map((item) => ({ id: "page-" + item.id, kind: "page" as const, label: item.name, description: "Navigation", view: item.id })),
    ...sessions.map((session) => ({ id: "session-" + session.id, kind: "session" as const, label: session.name, description: "Session · " + session.strategy, session })),
    ...markets.filter((market) => !!market.key).map((market) => ({ id: "market-" + market.key, kind: "market" as const, label: market.symbol || market.name || market.key, description: "Marché HIP-3", market: market.key })),
  ].filter((item) => (scope === "all" || scope === item.kind) && normalize(item.label + " " + item.description).includes(normalize(query))).slice(0, 30);
  const current = Math.min(index, Math.max(0, results.length - 1));
  return <Dialog title="Rechercher dans Zeus" onClose={onClose} wide>
    <div className="global-search" data-layro="kokonut-labs/ai-input-search">
      <div className="search-input"><Icon name="search" size={19} /><HaloInput ref={input} value={query} placeholder="Marché, session ou page…"
        aria-label="Recherche globale" role="combobox" aria-expanded="true" aria-controls={id} aria-activedescendant={results[current] ? id + "-" + current : undefined}
        onChange={(e) => { setQuery(e.target.value); setIndex(0); }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault(); setIndex((old) => Math.max(0, Math.min(results.length - 1, old + (event.key === "ArrowDown" ? 1 : -1))));
          }
          if (event.key === "Enter" && results[current]) { event.preventDefault(); onSelect(results[current]); }
        }} /></div>
      <SegmentedControl label="Rechercher parmi" value={scope} options={[{ value: "all", label: "Tout" }, { value: "market", label: "Marchés" }, { value: "session", label: "Sessions" }]} onValueChange={(value) => { setScope(value); setIndex(0); }} />
      <ScrollArea label="Résultats de recherche" className="search-results">
        <div role="listbox" id={id} aria-label="Résultats">{results.map((result, i) =>
          <button role="option" aria-selected={current === i} id={id + "-" + i} key={result.id} onClick={() => onSelect(result)} onPointerMove={() => setIndex(i)}>
            <Icon name={result.kind === "session" ? "sessions" : result.kind === "market" ? "book" : "arrow"} size={18} />
            <span><strong>{result.label}</strong><small>{result.description}</small></span><Icon name="arrow" size={15} /></button>)}</div>
        {!results.length && <Empty title="Aucun résultat." text="Essayez un autre nom de marché ou de session." />}
      </ScrollArea>
      <p className="search-hint"><Kbd>↑ ↓</Kbd> sélectionner <Kbd>Entrée</Kbd> ouvrir <Kbd>Échap</Kbd> fermer</p>
    </div>
  </Dialog>;
}
