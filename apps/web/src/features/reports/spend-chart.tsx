/** The dashboard's spend chart (Recharts, loaded only with the dashboard): one currency, one dimension, horizontal bars
 *  largest first, each with its amount as a direct label. A group's bar is `chart-1`, a named gap's `chart-2` (and its
 *  name starts with ⚠, as the gap chip does), "Everything else" `chart-6` (its name starts with …). Colours come from the
 *  token CSS variables, never written here. The chart is decoration for screen readers: the table under it has the same
 *  rows and numbers. */
import { Bar, BarChart, Cell, LabelList, ResponsiveContainer, XAxis, YAxis } from "recharts";
import { currencyDecimals, groupDigits } from "@/features/money/contract-types";
import type { SpendGroup } from "./report-types";

const fillOf = (g: SpendGroup) => (g.gap === "other" ? "var(--chart-6)" : g.gap ? "var(--chart-2)" : "var(--chart-1)");
const nameOf = (g: SpendGroup) => (g.gap === "other" ? `… ${g.label}` : g.gap ? `⚠ ${g.label}` : g.label);

export function SpendChart({ rows, currency, testId }: { rows: SpendGroup[]; currency: string; testId?: string }) {
  // The bar's length only: the amount shown is the API's decimal text, regrouped with the currency's decimals, as the
  // table under it shows it (no ".00" for JPY).
  const decimals = currencyDecimals(currency);
  const data = rows.map((g) => ({ name: nameOf(g), value: Number(g.amount), text: groupDigits(g.amount, decimals), fill: fillOf(g) }));
  const height = Math.max(120, data.length * 34 + 24);
  return (
    <div aria-hidden="true" style={{ height }} data-testid={testId}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" accessibilityLayer={false} margin={{ top: 4, right: 96, bottom: 4, left: 8 }}>
          <XAxis type="number" hide domain={[0, "dataMax"]} />
          <YAxis type="category" dataKey="name" width={180} tickLine={false} axisLine={false} interval={0}
            tick={{ fill: "var(--foreground)", fontSize: 12 }} />
          <Bar dataKey="value" radius={[0, 3, 3, 0]} isAnimationActive={false} barSize={18}>
            {data.map((d, i) => <Cell key={i} fill={d.fill} />)}
            <LabelList dataKey="text" position="right" fill="var(--foreground)" fontSize={12} />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** The chart's key: a swatch and words for each kind of bar, so colour is never the only key. */
export const SPEND_LEGEND = [
  { label: "A group", swatch: "bg-chart-1" },
  { label: "⚠ A named gap (cost with no group)", swatch: "bg-chart-2" },
  { label: "… Everything else (smaller groups together)", swatch: "bg-chart-6" },
];
