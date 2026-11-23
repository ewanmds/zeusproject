import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DataTable } from "./DataTable";

describe("scanner default volume ranking", () => {
  it("renders descending USD/min and an accessible sort state without mutating rows", () => {
    const rows = [
      { market: "zero", volume: 0 },
      { market: "low", volume: 24.33 },
      { market: "high", volume: 550 },
    ];
    const html = renderToStaticMarkup(
      <DataTable rows={rows} rowKey={(row) => row.market} caption="Scanner"
        defaultSort={{ id: "volume", direction: "desc" }}
        columns={[
          { id: "market", label: "Marché", render: (row) => row.market },
          { id: "volume", label: "USD/min", render: (row) => row.volume, sort: (row) => row.volume },
        ]} />,
    );
    expect(html).toContain('aria-sort="descending"');
    expect(html.indexOf("<td>high</td>")).toBeLessThan(html.indexOf("<td>low</td>"));
    expect(html.indexOf("<td>low</td>")).toBeLessThan(html.indexOf("<td>zero</td>"));
    expect(rows.map((row) => row.market)).toEqual(["zero", "low", "high"]);
  });
});
