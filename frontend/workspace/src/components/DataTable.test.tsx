import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DataTable } from "./DataTable";
import { LiveAmount } from "./LiveAmount";

describe("observed markets", () => {
  it("keeps rejected and zero-volume rows and defaults to descending public USD/min", () => {
    const rows = [{ id: "zero", volume: 0 }, { id: "active-rejected", volume: 120 }];
    const html = renderToStaticMarkup(<DataTable rows={rows} caption="Observations"
      rowKey={(row) => row.id} defaultSort={{ id: "volume", direction: "desc" }} animateChanges
      columns={[
        { id: "name", label: "Marché", render: (row) => row.id },
        { id: "volume", label: "USD/min", render: (row) => <LiveAmount value={row.volume} />, sort: (row) => row.volume },
      ]} />);
    expect(html).toContain('aria-sort="descending"');
    expect(html.indexOf("active-rejected")).toBeLessThan(html.indexOf('data-row-key="zero"'));
    expect(html).toContain("$0.00");
    expect(rows[0].id).toBe("zero");
  });
});
