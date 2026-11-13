/** Where does confidential data go (capability `data_flows`, `/data-flows`, every role reads): every
 *  live integration as a data flow from GET /reports/data-flows, the most sensitive first, with its risks (No DPA, No
 *  auth method, Down-classified). Two views in `?view=`: the table (default, the complete equivalent) and a graph
 *  (GET /reports/data-flows/graph) on the shared Cytoscape canvas, edges weighted by classification and risky ends
 *  marked, with a legend and a notice when the graph stops at 1,000 flows. The report's filters live in the URL:
 *  `min_classification`, `category`, `source`, `target` and `risk`; Export CSV sends the same. Loaded on first use. */
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { DATA_CATEGORIES, INTEGRATION_AUTH_METHODS, labelOf } from "@/api";
import { EmptyState, ErrorBanner, Loading, PageHead, TableRegion, TabStrip } from "@/components/app";
import { RecordLink } from "@/components/record-link";
import { ExportCsvButton, filterProblem, SelectFilter, Unavailable, useUrlFilters, type Option } from "@/components/url-filters";
import { DetailList, DetailRow, GapChip, StatTile, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Graph, type GraphEdge, type GraphNode } from "@/features/graph/graph-loader";
import { KIND_LABEL, recordPath } from "@/lib/kinds";
import { useTypedQuery, type QueryOf, type Schemas } from "@/lib/openapi";
import { canWrite, useMe } from "@/lib/session";
import { CategoryChips, CLASSIFICATION_LABEL, PersonalDataMark, RiskChips, RISKS, type Classification } from "./flow-ui";

type Flow = Schemas["DataFlow"];
type FlowGraph = Schemas["DataFlowGraph"];
type Query = QueryOf<"reports/data-flows", "get">;
const FILTERS = ["min_classification", "category", "source", "target", "risk"] as const;
const LEVELS: Option[] = [
  { value: "restricted", label: "Restricted only" }, { value: "confidential", label: "Confidential and above" },
  { value: "internal", label: "Internal and above" }, { value: "public", label: "Public and above" },
];
const ANY_RISK = RISKS.map((r) => r.value).join(",");
const RISK_OPTIONS: Option[] = [{ value: ANY_RISK, label: "Any risk" }, ...RISKS];
type View = "table" | "graph";
const VIEWS: { id: View; label: string }[] = [{ id: "table", label: "Table" }, { id: "graph", label: "Graph" }];
const classText = (c: Classification | null | undefined) => (c ? CLASSIFICATION_LABEL[c] : "Unclassified");

export function DataFlowsPage() {
  const me = useMe();
  if (!me.capabilities.includes("data_flows")) return <><PageHead title="Data flows" /><Unavailable what="The data-flow map" testId="flows-unavailable" /></>;
  return <DataFlows />;
}

function DataFlows() {
  const me = useMe();
  const f = useUrlFilters(FILTERS);
  const [params, setParams] = useSearchParams();
  const view: View = params.get("view") === "graph" ? "graph" : "table";
  const setView = (v: View) => setParams((cur) => { const n = new URLSearchParams(cur); if (v === "graph") n.set("view", "graph"); else n.delete("view"); return n; });
  const query = f.query as Partial<Query>;
  const report = useTypedQuery("reports/data-flows", { query });
  // The ends to filter by: every record at a flow's end, from the unfiltered report (the same request with no filter).
  const all = useTypedQuery("reports/data-flows");
  const [notice, setNotice] = useState("");
  const ends = (side: "source" | "target"): Option[] => [...new Map((all.data?.flows ?? []).map((x) => [x[side].id, x[side].name])).entries()]
    .map(([value, label]) => ({ value, label })).sort((a, b) => a.label.localeCompare(b.label));
  const search = new URLSearchParams(f.query as Record<string, string>).toString();
  const data = report.data;
  const flows = data?.flows;
  return (
    <>
      <PageHead title="Data flows" desc="Where classified data goes: every live integration as a flow, the most sensitive first, with what it carries and its risks.">
        <ExportCsvButton path={`reports/data-flows/export.csv${search ? `?${search}` : ""}`} fallback="softwarden-data-flows.csv" what="the data flows" onDone={setNotice} />
      </PageHead>
      <SuccessNote text={notice} />
      <form className="mb-4 flex flex-wrap items-end gap-3" aria-label="Filter the data flows" onSubmit={(e) => e.preventDefault()}>
        <SelectFilter id="flows-level" label="Classification" value={f.value("min_classification")} onChange={(v) => f.set("min_classification", v)} options={LEVELS} all="Any classification" className="w-56" />
        <SelectFilter id="flows-category" label="Data category" value={f.value("category")} onChange={(v) => f.set("category", v)} options={DATA_CATEGORIES} all="Any category" unknown="Several categories" className="w-56" />
        <SelectFilter id="flows-source" label="From" value={f.value("source")} onChange={(v) => f.set("source", v)} options={ends("source")} all="Any record" unknown="A record with no flows out" className="w-48" />
        <SelectFilter id="flows-target" label="To" value={f.value("target")} onChange={(v) => f.set("target", v)} options={ends("target")} all="Any record" unknown="A record with no flows in" className="w-48" />
        <SelectFilter id="flows-risk" label="Risk" value={f.value("risk")} onChange={(v) => f.set("risk", v)} options={RISK_OPTIONS} all="With or without risks" unknown="Several risks" className="w-56" />
        {f.filtered && <Button type="button" variant="outline" onClick={f.clear}>Clear filters</Button>}
      </form>
      {data && !data.vendor_risk && (
        <p className="mb-4 flex flex-wrap items-center gap-2 text-sm text-muted-foreground" data-testid="dpa-unchecked">
          <GapChip label="DPA status not checked (vendor risk is off)" />
          The vendor risk file isn't on, so whether a vendor has a DPA isn't known and No DPA is never raised.
        </p>
      )}
      <p className="sr-only" aria-live="polite">{flows ? `${flows.length} ${flows.length === 1 ? "flow" : "flows"} shown.` : ""}</p>
      {report.isError && (
        <div className="mb-4">
          <ErrorBanner message={filterProblem(report.error, "Couldn't load the data flows.")} onRetry={() => void report.refetch()} />
          {f.filtered && <Button variant="outline" onClick={f.clear}>Clear filters</Button>}
        </div>
      )}
      {!flows && !report.isError && <Loading what="the data flows" rows={5} />}
      {flows && flows.length === 0 && (f.filtered
        ? <EmptyState title="No data flows match these filters" action={<Button variant="outline" onClick={f.clear}>Clear filters</Button>}>Try a lower classification, another category or any risk.</EmptyState>
        : <EmptyState title="No data flows yet" action={canWrite(me) && <Button asChild variant="outline"><Link to="/integrations/new">Add integration</Link></Button>}>
            Every live integration is a data flow. {canWrite(me) ? "Add one, with its classification and data categories, to see where data goes." : "Editors and Admins add integrations; they show here with their classification and risks."}
          </EmptyState>)}
      {data && flows && flows.length > 0 && (
        <>
          <div className="mb-4 grid gap-3 sm:grid-cols-3">
            <StatTile level={2} id="flows-total" label="Flows" value={data.summary.flows} detail={`${data.summary.by_classification.restricted} restricted, ${data.summary.by_classification.confidential} confidential`} />
            <StatTile level={2} id="flows-risky" label="With a risk" value={data.summary.risky}
              detail={`${data.summary.by_risk.no_dpa} no DPA, ${data.summary.by_risk.no_auth_method} no auth method, ${data.summary.by_risk.down_classification} down-classified`} />
            <StatTile level={2} id="flows-personal" label="Carry personal data" value={data.summary.personal_data} detail="Personal, special-category or health data" />
          </div>
          <TabStrip tabs={VIEWS} tab={view} onSelect={setView} label="Data flow views" idPrefix="flows-" />
          <div role="tabpanel" id="flows-tabpanel" aria-labelledby={`flows-tab-${view}`}>
            {view === "table" ? <FlowTable flows={flows} filtered={f.filtered} /> : <FlowGraphView query={query} onShowTable={() => setView("table")} />}
          </div>
        </>
      )}
    </>
  );
}

// ---- the table -----------------------------------------------------------------------------------------------------

function End({ end }: { end: Flow["source"] }) {
  return (
    <span className="block">
      <RecordLink record={end} className="underline" />
      <span className="block text-sm text-muted-foreground">
        {KIND_LABEL[end.kind]}{end.instance ? ` (${end.instance.name})` : ""}, {classText(end.classification).toLowerCase()}{end.vendor ? `; vendor ${end.vendor.name}` : ""}
      </span>
    </span>
  );
}

function FlowTable({ flows, filtered }: { flows: Flow[]; filtered: boolean }) {
  return (
    <>
      <TableRegion label="Data flows table">
        <Table>
          <TableCaption className="sr-only">Data flows, the most sensitive first{filtered ? ", filtered" : ""}</TableCaption>
          <TableHeader><TableRow>
            <TableHead scope="col">Flow</TableHead><TableHead scope="col">From</TableHead><TableHead scope="col">To</TableHead>
            <TableHead scope="col">Classification</TableHead><TableHead scope="col">Data</TableHead><TableHead scope="col">Authentication</TableHead>
            <TableHead scope="col">Risks</TableHead>
          </TableRow></TableHeader>
          <TableBody>
            {flows.map((x) => (
              <TableRow key={x.id} className="align-top" data-flow-row={x.name}>
                <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                  <Link to={`/integrations/${x.id}`} className="underline">{x.name}</Link>
                  <span className="block text-sm font-normal text-muted-foreground">{x.direction === "two_way" ? "Both ways" : "One way"}</span>
                </TableHead>
                <TableCell className="min-w-40 whitespace-normal"><End end={x.source} /></TableCell>
                <TableCell className="min-w-40 whitespace-normal"><End end={x.target} /></TableCell>
                <TableCell className="whitespace-normal">{x.classification ? CLASSIFICATION_LABEL[x.classification] : <GapChip label="No classification" />}</TableCell>
                <TableCell className="min-w-44 whitespace-normal">
                  <span className="flex flex-wrap gap-1.5">
                    <CategoryChips categories={x.categories} empty={<span className="text-muted-foreground">No categories</span>} />
                    <PersonalDataMark on={x.contains_personal_data} />
                  </span>
                </TableCell>
                <TableCell className="whitespace-normal">{x.auth_method ? labelOf(INTEGRATION_AUTH_METHODS, x.auth_method) : <span className="text-muted-foreground">Not recorded</span>}</TableCell>
                <TableCell className="min-w-56 whitespace-normal">
                  {x.risks.length === 0 ? <span className="text-success-foreground"><span aria-hidden="true">✓ </span>No risks</span> : (
                    <>
                      <RiskChips risks={x.risks} />
                      <ul className="mt-1 grid gap-0.5 text-sm text-muted-foreground">{x.risks.map((r, i) => <li key={`${r.code}-${r.end}-${i}`}>{r.why}</li>)}</ul>
                    </>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableRegion>
      <p className="mt-2 text-sm text-muted-foreground">{flows.length} {flows.length === 1 ? "flow" : "flows"}, the most sensitive first, then by name. Archived and retired integrations aren't flows.</p>
    </>
  );
}

// ---- the graph -----------------------------------------------------------------------------------------------------

const COLUMN = 300;
const ROW = 84;
const fit = (text: string, n: number) => (text.length > n ? `${text.slice(0, n - 1)}…` : text);

/** Records as nodes at fixed places (no animated layout): senders left, records that send and receive in the middle,
 *  receivers right, by name. A flow's width is its classification's weight; a risky flow is drawn in the overdue tone
 *  with "!" before its label, and a record receiving one gets a heavy border and "! at risk" in its label. */
function toElements(g: FlowGraph) {
  const sends = new Set(g.edges.map((e) => e.source));
  const receives = new Set(g.edges.map((e) => e.target));
  const column = (id: string) => (sends.has(id) && !receives.has(id) ? 0 : sends.has(id) ? 1 : 2);
  const byColumn = new Map<number, string[]>();
  for (const n of g.nodes) byColumn.set(column(n.id), [...(byColumn.get(column(n.id)) ?? []), n.id]);
  const pos = new Map<string, { x: number; y: number }>();
  for (const [c, ids] of byColumn) ids.forEach((id, i) => pos.set(id, { x: c * COLUMN, y: (i - (ids.length - 1) / 2) * ROW }));
  const nodes: GraphNode[] = g.nodes.map((n) => {
    const words = [KIND_LABEL[n.kind], classText(n.classification), ...(n.archived ? ["Archived"] : n.retired ? ["Retired"] : [])];
    return {
      id: n.id, position: pos.get(n.id),
      label: `${fit(n.name, 24)}\n${words.join(" · ")}${n.risky ? " · ! at risk" : ""}`,
      spoken: `${n.name}, ${words.join(", ")}${n.vendor ? `, vendor ${n.vendor.name}` : ""}${n.risky ? ", receives data through a flow with a risk" : ""}`,
      classes: [`kind-${n.kind}`, n.risky ? "risky" : "", n.archived || n.retired ? "flagged" : ""].filter(Boolean).join(" "),
    };
  });
  const edges: GraphEdge[] = g.edges.map((e) => {
    const detail = e.detail as { direction?: string | null };
    return {
      id: e.id, source: e.source, target: e.target,
      label: `${e.risks.length ? "! " : ""}${fit(e.label, 30)}`,
      classes: ["integration", `w${Math.max(0, Math.min(4, e.weight))}`, detail.direction === "two_way" ? "two-way" : "",
        e.risks.length ? "risky" : "", column(e.source) === column(e.target) ? "arc" : ""].filter(Boolean).join(" "),
    };
  });
  const tallest = Math.max(1, ...[...byColumn.values()].map((ids) => ids.length));
  return { nodes, edges, height: Math.min(680, Math.max(380, tallest * ROW + 160)) };
}

const WIDTH = [1.5, 2, 3, 4.5, 6];

function FlowLegend({ legend }: { legend: FlowGraph["legend"] }) {
  return (
    <ul aria-label="Legend" className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-sm">
      {legend.map((l) => (
        <li key={l.label} className="flex items-center gap-2">
          <svg width="44" height="14" viewBox="0 0 44 14" aria-hidden="true" className="shrink-0">
            <path d="M2 7h40" strokeWidth={WIDTH[Math.max(0, Math.min(4, l.weight))]} strokeDasharray="7 5" className="stroke-accent-line" />
          </svg>
          {l.label} data
        </li>
      ))}
      <li className="flex items-center gap-2">
        <svg width="44" height="14" viewBox="0 0 44 14" aria-hidden="true" className="shrink-0"><path d="M2 7h40" strokeWidth={2} strokeDasharray="7 5" className="stroke-state-overdue" /></svg>
        A flow with a risk (its label starts with !)
      </li>
      <li className="flex items-center gap-2">
        <span aria-hidden="true" className="h-[18px] w-9 shrink-0 rounded-sm border-[3px] border-state-overdue bg-card" />
        Receives data through a risky flow (its label says “! at risk”)
      </li>
    </ul>
  );
}

function FlowGraphView({ query, onShowTable }: { query: Partial<Query>; onShowTable: () => void }) {
  const q = useTypedQuery("reports/data-flows/graph", { query });
  const [selected, setSelected] = useState<string | null>(null);
  const elements = useMemo(() => (q.data ? toElements(q.data) : null), [q.data]);
  if (q.isError) return <ErrorBanner message={filterProblem(q.error, "Couldn't load the graph.")} onRetry={() => void q.refetch()} />;
  if (!q.data || !elements) return <Loading what="the graph" rows={4} />;
  const g = q.data;
  const node = g.nodes.find((n) => n.id === selected) ?? null;
  const risky = g.nodes.filter((n) => n.risky).length;
  return (
    <div>
      {g.truncated && (
        <p className="mb-3 rounded-md border border-state-pending-border bg-state-pending px-3 py-2 text-sm text-state-pending-foreground" data-testid="graph-truncated">
          <span aria-hidden="true">◐ </span>The graph shows the first {g.edges.length.toLocaleString("en")} flows, the most sensitive first. Narrow it with the filters; the table and the CSV list every flow.
        </p>
      )}
      <p id="flows-graph-desc" hidden>
        {g.nodes.length} records and {g.edges.length} flows. {risky} {risky === 1 ? "record receives" : "records receive"} data through a flow with a risk.
        Senders are on the left, records that send and receive in the middle, receivers on the right. Thicker lines carry more sensitive data.
      </p>
      <Graph nodes={elements.nodes} edges={elements.edges} layout="preset" label="Data-flow graph" describedBy="flows-graph-desc"
        selected={selected} onSelect={setSelected} height={elements.height} tableNote="The Table view lists every flow with its risks."
        fallback={<p className="rounded-lg border bg-card px-4 py-3" role="status">The graph couldn't be loaded. <Button variant="outline" size="sm" onClick={onShowTable}>Show table</Button></p>}
        extras={<>
          <FlowLegend legend={g.legend} />
          <p className="mt-2 text-sm text-muted-foreground">
            Click a record, or focus the picture and use the arrow keys and Enter, to see its flows. The table lists every flow.{" "}
            <Button variant="outline" size="sm" onClick={onShowTable}>Show table</Button>
          </p>
        </>} />
      {node && <NodePanel node={node} graph={g} />}
    </div>
  );
}

function NodePanel({ node, graph }: { node: FlowGraph["nodes"][number]; graph: FlowGraph }) {
  const facts = (node.detail as { vendor_risk?: Schemas["DataFlowVendorRisk"] | null }).vendor_risk ?? null;
  const names = new Map(graph.nodes.map((n) => [n.id, n.name]));
  const out = graph.edges.filter((e) => e.source === node.id);
  const into = graph.edges.filter((e) => e.target === node.id);
  const to = recordPath(node.kind, node.id);
  const line = (e: FlowGraph["edges"][number], other: string) => {
    const integration = (e.detail as { integration?: { id: string; name: string } }).integration;
    return (
      <li key={e.id} className="flex flex-wrap items-center gap-1.5">
        {integration ? <Link to={`/integrations/${integration.id}`} className="underline">{integration.name}</Link> : e.label}
        <span className="text-muted-foreground">{other}, {classText(e.classification).toLowerCase()}</span>
        {e.risks.length > 0 && <RiskChips risks={e.risks.map((code) => ({ code }))} />}
      </li>
    );
  };
  return (
    <section aria-labelledby="flows-selected" className="mt-4 overflow-hidden rounded-lg border bg-card" data-testid="flows-selected">
      <h2 id="flows-selected" className="border-b px-4 py-2.5 font-display text-section font-semibold">Selected: {node.name}</h2>
      <DetailList>
        <DetailRow label="Record">{to ? <Link to={to} className="underline">{node.name}</Link> : node.name} <span className="text-muted-foreground">({KIND_LABEL[node.kind]})</span></DetailRow>
        <DetailRow label="Classification">{classText(node.classification)}</DetailRow>
        <DetailRow label="Vendor">{node.vendor?.name ?? <span className="text-muted-foreground">None</span>}</DetailRow>
        {facts && <DetailRow label="Vendor's DPA">{facts.dpa ? "Signed or current" : <GapChip label="No DPA" />}</DetailRow>}
        <DetailRow label="Sends">{out.length ? <ul className="grid gap-1">{out.map((e) => line(e, `to ${names.get(e.target)}`))}</ul> : <span className="text-muted-foreground">Nothing</span>}</DetailRow>
        <DetailRow label="Receives">{into.length ? <ul className="grid gap-1">{into.map((e) => line(e, `from ${names.get(e.source)}`))}</ul> : <span className="text-muted-foreground">Nothing</span>}</DetailRow>
      </DetailList>
    </section>
  );
}
