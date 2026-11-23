import { useMemo, useState, useRef, useLayoutEffect } from "react";
import type { ReactNode } from "react";
import { Icon } from "../icons";
import { ScrollArea } from "./layout";
export interface Column<T> {
  id: string;
  label: string;
  render: (row: T) => ReactNode;
  sort?: (row: T) => string | number | null;
}
// Layro: preetsuthar17/table — scroll container, sticky header and semantic sort controls.
export function DataTable<T>({
  rows,
  columns,
  caption,
  rowKey,
  defaultSort,
  animateChanges = false,
}: {
  rows: readonly T[];
  columns: Column<T>[];
  caption: string;
  rowKey: (row: T) => string;
  defaultSort?: { id: string; direction: "asc" | "desc" };
  animateChanges?: boolean;
}) {
  const [sort, setSort] = useState({ id: defaultSort?.id ?? "", direction: defaultSort?.direction === "desc" ? -1 : 1 });
  const body = useRef<HTMLTableSectionElement>(null);
  const positions = useRef(new Map<string, number>());
  useLayoutEffect(() => {
    if (!animateChanges || !body.current) return;
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const next = new Map<string, number>();
    for (const row of body.current.rows) {
      const key = row.dataset.rowKey!;
      const top = row.offsetTop;
      const previous = positions.current.get(key);
      if (!reduced && previous != null && previous !== top && Math.abs(previous-top) < 160) {
        row.getAnimations().forEach((animation) => animation.cancel());
        row.animate([{ transform: `translateY(${previous-top}px)` }, { transform: "translateY(0)" }],
          { duration: 220, easing: "ease-out" });
      }
      next.set(key, top);
    }
    positions.current = next;
  });
  const sorted = useMemo(() => {
    const column = columns.find((item) => item.id === sort.id);
    if (!column?.sort) return rows;
    return [...rows].sort((a, b) => {
      const x = column.sort!(a),
        y = column.sort!(b);
      if (x == null) return y == null ? 0 : 1;
      if (y == null) return -1;
      return (
        sort.direction *
        (typeof x === "number" && typeof y === "number"
          ? x - y
          : String(x).localeCompare(String(y), "fr"))
      );
    });
  }, [rows, columns, sort]);
  return (
    <div className="zeus-data-table" data-layro="preetsuthar17/table">
      <ScrollArea label={caption}>
        <table>
          <caption>{caption}</caption>
          <thead>
            <tr>
              {columns.map((column) => (
                <th
                  scope="col"
                  key={column.id}
                  aria-sort={
                    sort.id === column.id
                      ? sort.direction > 0
                        ? "ascending"
                        : "descending"
                      : column.sort
                        ? "none"
                        : undefined
                  }
                >
                  {column.sort ? (
                    <button
                      onClick={() =>
                        setSort((old) => ({
                          id: column.id,
                          direction: old.id === column.id ? -old.direction : 1,
                        }))
                      }
                    >
                      {column.label}
                      <Icon name="sort" size={13} />
                    </button>
                  ) : (
                    column.label
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody ref={body}>
            {sorted.map((row) => (
              <tr key={rowKey(row)} data-row-key={rowKey(row)}>
                {columns.map((column) => (
                  <td key={column.id}>{column.render(row)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </ScrollArea>
    </div>
  );
}
