/** The dashboard's Data quality block (part of the dashboard in features/reports/dashboard.tsx,
 *  loaded with it and Recharts). It renders the data-quality summary:
 *
 *  - StatTiles: the compliance percentage (passing checks over every live record and rule that is on, exceptions left
 *    out), complete records, failing checks and exceptions.
 *  - A ChartCard of failing records per check: horizontal bars in `chart-1` with the count as a direct label at the end
 *    of each bar and the check's name on the axis (no legend needed: one series, labelled). The same numbers are in the
 *    table under it, with links to the failing records, so the chart is never the only way to read them.
 *  - The breakdown by kind (records, complete, compliance) as a table.
 */
import { Link } from "react-router-dom";
import { Bar, BarChart, LabelList, ResponsiveContainer, XAxis, YAxis } from "recharts";
import { SEVERITY_LABEL, type QualitySummary } from "@/api";
import { EmptyState, TableRegion } from "@/components/app";
import { ChartCard, StatTile } from "@/components/sw";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { KIND_PLURAL } from "@/lib/kinds";
import { CompletenessBar } from "./completeness";

const pct = (n: number | null) => (n === null ? "—" : `${n}%`);

/** The dashboard's Data quality block, from the summary the dashboard already has (`GET /dashboard` `data_quality`,
 *  the same body as `GET data-quality/summary`). */
export function DataQualitySection({ s }: { s: QualitySummary }) {
  return (
    <section aria-labelledby="h-dq" className="mb-6">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="h-dq" tabIndex={-1} className="font-display text-section font-semibold outline-none">Data quality</h2>
        <Link to="/data-quality" className="text-sm">Open Data quality</Link>
      </div>
      {s.records === 0 ? (
        <EmptyState title="No live records to check yet">
          Data-quality checks run on active, proposed, evaluating and deprecated records that aren't archived. Add applications, components or AI agents to see their completeness here.
        </EmptyState>
      ) : <Panels s={s} />}
    </section>
  );
}

function Panels({ s }: { s: QualitySummary }) {
  const failing = s.by_rule.filter((r) => r.failing > 0);
  const data = failing.map((r) => ({ label: r.label, failing: r.failing }));
  const height = Math.max(120, data.length * 36 + 24);
  return (
    <div className="grid gap-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile id="stat-compliance" label="Compliance" value={pct(s.score)}
          detail={s.score === null ? "No checks apply yet." : `${s.passing.toLocaleString()} of ${(s.passing + s.failing).toLocaleString()} checks pass, as of ${s.as_of}.`} />
        <StatTile id="stat-complete" label="Complete records" value={`${s.complete.toLocaleString()} of ${s.records.toLocaleString()}`}
          detail="Live records failing no check." />
        <StatTile id="stat-failing" label="Failing checks" value={s.failing.toLocaleString()}
          detail={<Link to="/data-quality">See the failing records</Link>} />
        <StatTile id="stat-excepted" label="Not applicable" value={s.excepted.toLocaleString()}
          detail={<><Link to="/data-quality?tab=exceptions" className="underline">Exceptions</Link> · {s.rules.on} {s.rules.on === 1 ? "check" : "checks"} on{s.rules.off ? `, ${s.rules.off} off` : ""}</>} />
      </div>
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <ChartCard id="chart-failing" title="Failing records by check"
          summary={failing.length ? `${failing.length} ${failing.length === 1 ? "check has" : "checks have"} failing records; the table below has the same numbers.` : "Every live record passes every check that is on."}>
          {failing.length > 0 ? (
            // Decorative for screen readers: the table below carries the same numbers with links.
            <div aria-hidden="true" style={{ height }} data-testid="failing-chart">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={data} layout="vertical" accessibilityLayer={false} margin={{ top: 4, right: 40, bottom: 4, left: 8 }}>
                  <XAxis type="number" hide allowDecimals={false} />
                  <YAxis type="category" dataKey="label" width={170} tickLine={false} axisLine={false}
                    tick={{ fill: "var(--foreground)", fontSize: 12 }} />
                  <Bar dataKey="failing" fill="var(--chart-1)" radius={[0, 3, 3, 0]} isAnimationActive={false} barSize={18}>
                    <LabelList dataKey="failing" position="right" fill="var(--foreground)" fontSize={12} />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : <p className="px-2 text-sm text-success-foreground">✓ Nothing failing.</p>}
        </ChartCard>
        <section aria-labelledby="h-by-kind" className="overflow-hidden rounded-lg border bg-card">
          <h3 id="h-by-kind" className="border-b px-4 py-2.5 font-display text-section font-semibold">By kind</h3>
          <TableRegion label="Data quality by kind table" className="rounded-none border-0">
            <Table>
              <TableCaption className="sr-only">Data quality by record kind: live records, complete records and compliance</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Kind</TableHead><TableHead scope="col">Live records</TableHead><TableHead scope="col">Complete</TableHead><TableHead scope="col">Compliance</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {s.by_kind.map((k) => (
                  <TableRow key={k.kind}>
                    <TableHead scope="row" className="font-semibold text-foreground"><Link to={`/data-quality?kind=${k.kind}`}>{KIND_PLURAL[k.kind] ?? k.kind}</Link></TableHead>
                    <TableCell className="tabular">{k.records}</TableCell>
                    <TableCell className="tabular">{k.complete}</TableCell>
                    <TableCell>{k.score === null ? "—" : <CompletenessBar score={k.score} label={`${KIND_PLURAL[k.kind] ?? k.kind} compliance`} />}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
        </section>
      </div>
      <section aria-labelledby="h-by-rule" className="overflow-hidden rounded-lg border bg-card">
        <h3 id="h-by-rule" className="border-b px-4 py-2.5 font-display text-section font-semibold">By check</h3>
        <TableRegion label="Data quality by check table" className="rounded-none border-0">
          <Table>
            <TableCaption className="sr-only">Data quality by check, most severe first: failing, not applicable and passing records, and compliance</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Check</TableHead><TableHead scope="col">Severity</TableHead><TableHead scope="col">Failing</TableHead>
              <TableHead scope="col">Not applicable</TableHead><TableHead scope="col">Passing</TableHead><TableHead scope="col">Compliance</TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {s.by_rule.map((r) => (
                <TableRow key={r.id} data-rule={r.id}>
                  <TableHead scope="row" className="font-semibold text-foreground">{r.label}</TableHead>
                  <TableCell>{SEVERITY_LABEL[r.severity]}</TableCell>
                  <TableCell className="tabular">{r.failing > 0 ? <Link to={`/data-quality?rule=${r.id}`}>{r.failing}<span className="sr-only"> records failing {r.label}</span></Link> : 0}</TableCell>
                  <TableCell className="tabular">{r.excepted}</TableCell>
                  <TableCell className="tabular">{r.passing}</TableCell>
                  <TableCell>{r.score === null ? "—" : <CompletenessBar score={r.score} label={`${r.label} compliance`} />}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      </section>
    </div>
  );
}
