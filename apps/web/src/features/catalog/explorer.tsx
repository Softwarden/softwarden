import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { cap, INTEGRATION_AUTH_METHODS, labelOf, LinkKind, LinkState } from "@/api";
import { KIND_LABEL, pathOf } from "@/lib/kinds";
import { RecordLink } from "@/components/record-link";
import { canWrite, useMe } from "@/lib/session";
import { Chip, EmptyState, ErrorBanner, Gap, Loading, PageHead, TabStrip, TableRegion, useLoad } from "@/components/app";
import { PickedRecord, RecordPicker } from "@/components/record-picker";
import { DetailList, DetailRow, LifecycleChip, LinkStateChip, Panel, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ExplorerGraph, graphKeyOf } from "@/features/catalog/explorer-graph";
import { ConfirmLinkDialog, ConfirmTarget } from "@/features/catalog/link-confirmations";
import { phraseOf } from "@/lib/relationship-types";

/** Dependency explorer. Table and Graph are two views of one
 *  result set; the target, direction, depth, toggles and view all live in the URL, so a record's Map tab can deep-link
 *  here (`?target=<id>&depth=all`).
 *
 *  Depth 1 to 10 or all; the Graph is Cytoscape (explorer-graph.tsx; the static SVG prints, and stands in
 *  when the Cytoscape chunk can't load); links that close a loop (the response's `cycles`) are marked in the table, the
 *  path panel and the picture; data flows show their direction and authentication. */

export type Step = { id: string; name: string; kind: LinkKind; via: { relationship: string; type: string; type_name?: string } | null; instance?: string | null; previous_instance?: string | null };
export type DepLink = { relationship?: string; state: LinkState; why: string; last_confirmed_on: string | null };
export type Owner = { id: string; name: string; active: boolean };
export type DepNode = {
  id: string; kind: LinkKind; name: string; lifecycle: string; archived: boolean; direction: "dependents" | "depends_on";
  depth: number; cycle: boolean; path: Step[]; link: DepLink; technical_owner: Owner | null; gaps: string[];
};
type FlowEnd = { id: string; name: string; kind: LinkKind; instance?: { id: string; name: string } | null };
export type DepFlow = {
  id: string; name: string; lifecycle: string; archived: boolean; hop: number; depth: number;
  attached_to: { id: string; name: string; kind: LinkKind }; source: FlowEnd; target: FlowEnd; protocol: string; schedule: string; direction?: string; auth_method?: string;
  implemented_by: { id: string; name: string; lifecycle: string; archived: boolean }[]; vendor_native: boolean; link: DepLink; technical_owner: Owner | null;
};
/** A link that closes a loop: `source` depends on `target`; `closes_at` is the record already on the path it leads back to. */
export type DepCycle = { relationship: string; type: string; source: string; target: string; closes_at: string; reached_from: string; direction: "dependents" | "depends_on"; depth: number };
export type Dependencies = {
  target: { id: string; name: string; kind: LinkKind; lifecycle: string; archived: boolean };
  direction: Direction; depth: string; include_retired: boolean; show_instances: boolean;
  nodes: DepNode[]; data_flows: DepFlow[]; total: number; shown: number; capped: boolean;
  summary: { nodes: number; data_flows: number; needs_verification: number; unconfirmed: number; cycles: number; hidden_retired: number };
  cycles?: DepCycle[];
};

type Direction = "dependents" | "depends_on" | "both";
type View = "graph" | "table";
const DIRECTIONS: { id: Direction; label: string }[] = [{ id: "dependents", label: "Dependents" }, { id: "depends_on", label: "Depends on" }, { id: "both", label: "Both" }];
/** 1 to 10 levels (the API's MAX_DEPTH) or all. */
const DEPTHS = [...Array.from({ length: 10 }, (_, i) => ({ id: String(i + 1), label: i === 0 ? "1 level" : `${i + 1} levels` })), { id: "all", label: "All levels" }];
const isDepth = (v: string | null): v is string => !!v && DEPTHS.some((d) => d.id === v);
const DIRECTION_TEXT: Record<Direction, string> = { dependents: "Dependents", depends_on: "Dependencies", both: "Dependents and dependencies" };



/** Table below 640 px and with reduced motion; the graph otherwise. An explicit ?view= always wins. */
function defaultView(): View {
  const q = (m: string) => typeof window.matchMedia === "function" && window.matchMedia(m).matches;
  return q("(max-width: 639px)") || q("(prefers-reduced-motion: reduce)") ? "table" : "graph";
}

export function Explorer() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const targetId = params.get("target") ?? "";
  const asked = params.get("direction");
  const direction: Direction = asked === "depends_on" || asked === "both" ? asked : "dependents";
  const askedDepth = params.get("depth");
  const depth = isDepth(askedDepth) ? askedDepth : "1";
  const flows = params.get("flows") !== "0";
  const instances = params.get("instances") === "1";
  const retired = params.get("retired") === "1";
  const view: View = params.get("view") === "graph" ? "graph" : params.get("view") === "table" ? "table" : defaultView();

  const [picked, setPicked] = useState<PickedRecord | null>(null);
  const update = (changes: Record<string, string | null>, push = !("q" in changes)) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    setParams(next, { replace: !push });
  };
  const query = new URLSearchParams({ target: targetId, direction, depth, data_flows: String(flows), show_instances: String(instances), include_retired: String(retired) }).toString();
  const filtered = direction !== "dependents" || depth !== "1" || !flows || instances || retired;

  return (
    <>
      <PageHead title="Dependencies" desc="Pick an application or component to see what depends on it, or what it depends on. Data flows are listed apart because a data flow is not an operational dependency.">
        {/* The page's one filled button: it needs a target, and Viewers can't create assessments. */}
        {targetId && canWrite(me) && <Button asChild><Link to={`/assessments/new?target=${targetId}`}>Start change assessment</Link></Button>}
      </PageHead>
      <section aria-label="Dependency filters" className="mb-4 grid gap-x-8 gap-y-4 rounded-lg border bg-card p-4 md:grid-cols-[minmax(240px,360px)_auto_auto_auto]">
        <div className="flex flex-col gap-1.5">
          <label htmlFor="target" className="text-sm font-medium leading-none">Target (application or component)</label>
          <RecordPicker id="target" kinds={["application", "component", "ai_agent"]} value={picked?.id === targetId ? picked : null}
            placeholder="Search by name…" listLabel="Target suggestions"
            onChange={(r) => { setPicked(r); update({ target: r?.id ?? null }, true); }} />
        </div>
        <Radios legend="Direction" name="direction" value={direction} options={DIRECTIONS} onChange={(v) => update({ direction: v === "dependents" ? null : v })} />
        <div className="flex min-w-0 flex-col gap-1.5">
          <label htmlFor="depth" className="text-sm font-medium leading-none">Depth</label>
          <select id="depth" className="native-select w-auto" value={depth} onChange={(e) => update({ depth: e.target.value === "1" ? null : e.target.value })}>
            {DEPTHS.map((d) => <option key={d.id} value={d.id}>{d.label}</option>)}
          </select>
        </div>
        <fieldset className="min-w-0">
          <legend className="mb-1.5 text-sm font-medium leading-none">Show</legend>
          <div className="flex flex-col gap-1.5">
            <Check id="flows" label="Show data flows" checked={flows} onChange={(c) => update({ flows: c ? null : "0" })} />
            <Check id="instances" label="Show instances" checked={instances} onChange={(c) => update({ instances: c ? "1" : null })} />
            <Check id="retired" label="Include retired" checked={retired} onChange={(c) => update({ retired: c ? "1" : null })} />
          </div>
        </fieldset>
        {filtered && (
          <div className="md:col-span-4">
            <Button variant="outline" size="sm" onClick={() => update({ direction: null, depth: null, flows: null, instances: null, retired: null })}>Reset filters</Button>
          </div>
        )}
      </section>
      {!targetId ? (
        <EmptyState title="No target chosen">Choose an application or component to see what depends on it.</EmptyState>
      ) : (
        <Results key={query} query={query} view={view} onView={(v) => update({ view: v })} write={canWrite(me)} direction={direction} onTarget={(t) => picked?.id !== t.id && setPicked(t)} onShowRetired={() => update({ retired: "1" })} />
      )}
    </>
  );
}

function Radios<T extends string>({ legend, name, value, options, onChange }: {
  legend: string; name: string; value: T; options: { id: T; label: string }[]; onChange: (v: T) => void;
}) {
  return (
    <fieldset className="min-w-0">
      <legend className="mb-1.5 text-sm font-medium leading-none">{legend}</legend>
      <div className="flex flex-col gap-1.5">
        {options.map((o) => (
          <label key={o.id} className="flex items-center gap-2">
            <input type="radio" name={name} value={o.id} checked={value === o.id} onChange={() => onChange(o.id)} className="h-4 w-4 accent-primary" />
            {o.label}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function Check({ id, label, checked, onChange }: { id: string; label: string; checked: boolean; onChange: (c: boolean) => void }) {
  return <label htmlFor={id} className="flex items-center gap-2"><input id={id} type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />{label}</label>;
}

// ---- Results ------------------------------------------------------------------------------------------------------

const nodeKey = (n: DepNode) => `${n.direction}:${n.link.relationship}:${n.id}`;

/** The chain in dependency order: a dependent first ("Benefits Portal depends on Payroll Sync depends on Workday"),
 *  a dependency last. `types[i]` joins chain[i] to chain[i + 1]. With instances shown, a step carries the instance its
 *  link used: the API gives each step the instance on its own end (`instance`) and on the end before it (`previous_instance`). */
export function chainOf(n: DepNode) {
  const p = n.path;
  const steps = p.map((s, j) => {
    const instances = [...new Set([j > 0 ? s.instance : null, p[j + 1]?.previous_instance].filter(Boolean))];
    return { ...s, text: instances.length ? `${s.name} (${instances.join(" / ")})` : s.name };
  });
  const dependents = n.direction === "dependents";
  const chain = dependents ? [...steps].reverse() : steps;
  // Each link in words from its type's name ("depends on"); the chain is in dependency order, so it reads from the source.
  const verb = (via: Step["via"] | undefined) => (via ? phraseOf(via.type_name, via.type) : "depends on");
  const types = chain.slice(0, -1).map((_, i) => verb(dependents ? chain[i].via : chain[i + 1].via));
  return { chain, types };
}
function pathText(n: DepNode) {
  const { chain, types } = chainOf(n);
  return chain.map((s, i) => (i < types.length ? `${s.text} → ${types[i]} →` : s.text)).join(" ");
}
/** The one link a row was reached by, in words: the last step and the one before it. */
function sentenceOf(n: DepNode) {
  const [from, to] = n.direction === "dependents" ? [n.path[n.path.length - 1], n.path[n.path.length - 2]] : [n.path[n.path.length - 2], n.path[n.path.length - 1]];
  const via = (n.direction === "dependents" ? from : to).via;
  return `${from.name} ${via ? phraseOf(via.type_name, via.type) : "depends on"} ${to.name}`;
}

function Results({ query, view, onView, write, direction, onTarget, onShowRetired }: {
  query: string; view: View; onView: (v: View) => void; write: boolean; direction: Direction; onTarget: (t: PickedRecord) => void; onShowRetired: () => void;
}) {
  const load = useLoad<Dependencies>(`dependencies?${query}`);
  const [selected, setSelected] = useState("");
  // Show path moves focus to the selected path, so the next Tab is inside the panel wherever it sits.
  const [focusPanel, setFocusPanel] = useState(0);
  const select = (key: string) => { setSelected(key); setFocusPanel((n) => n + 1); };
  const backToRow = () => document.querySelector<HTMLElement>(`[data-show-path="${CSS.escape(selected)}"]`)?.focus();
  const [confirming, setConfirming] = useState<ConfirmTarget | null>(null);
  const [notice, setNotice] = useState("");
  const data = load.data;
  const chosen = data?.nodes.find((n) => nodeKey(n) === selected) ?? null;
  // A target that arrived in the URL gets its name into the picker from the result.
  useEffect(() => { if (data) onTarget(data.target); }, [data, onTarget]);

  if (load.error) return <ErrorBanner message={`Couldn't load dependencies. ${load.error}`} onRetry={load.reload} />;
  if (!data) return <Loading what="dependencies" rows={5} />;

  const empty = data.nodes.length === 0 && data.data_flows.length === 0;
  const t = data.target;
  const verify = data.summary.needs_verification;
  const loops = new Set((data.cycles ?? []).map((c) => c.relationship));
  const loopCount = data.cycles?.length ?? 0;
  /** A record picked on the picture: its row (the one it was reached by, not a loop's repeat). */
  const pickFromGraph = (graphKey: string) => {
    const n = data.nodes.find((x) => !x.cycle && `${x.direction}:${x.id}` === graphKey);
    if (n) setSelected(nodeKey(n));
  };
  const tabs = [{ id: "graph" as View, label: "Graph" }, { id: "table" as View, label: "Table" }];
  const emptyText = direction === "dependents"
    ? `Nothing is recorded as depending on ${t.name}. That doesn't mean nothing does.`
    : direction === "depends_on" ? `${t.name} has no recorded dependencies. That doesn't mean it has none.`
      : `Nothing is recorded as depending on ${t.name}, and ${t.name} has no recorded dependencies. That doesn't mean there are none.`;

  return (
    <>
      <SuccessNote text={notice} />
      <p className="mb-3 text-sm text-muted-foreground" role="status">
        {data.summary.nodes} {data.summary.nodes === 1 ? "record" : "records"} and {data.summary.data_flows} {data.summary.data_flows === 1 ? "data flow" : "data flows"} for{" "}
        <RecordLink record={t} className="underline" />
        {t.lifecycle === "retired" && " (retired)"}. {data.summary.hidden_retired > 0 && !data.include_retired && (
          <>{data.summary.hidden_retired} retired left out. <button type="button" className="text-link underline" onClick={onShowRetired}>Include retired</button>.</>
        )}
        {loopCount > 0 && <> <span aria-hidden="true">↻</span> {loopCount} {loopCount === 1 ? "link closes" : "links close"} a loop.</>}
      </p>
      {empty ? (
        <EmptyState title="No relationships recorded" action={write && pathOf(t) && <Button asChild variant="outline"><Link to={pathOf(t, "relationships")!}>Add relationship</Link></Button>}>
          {emptyText}
        </EmptyState>
      ) : (
        <>
          {data.capped && (
            <p className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" role="status">
              <span aria-hidden="true">⚠</span> Showing {data.shown} of {data.total} rows. {view === "graph" ? "Use the table or reduce depth." : "Reduce the depth, or turn off data flows, to see fewer."}
            </p>
          )}
          <TabStrip label="Dependency view" tabs={tabs} tab={view} onSelect={onView} />
          <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${view}`} className={`grid items-start gap-4 ${view === "table" ? "2xl:grid-cols-[minmax(0,1fr)_340px]" : ""}`}>
            <div className="min-w-0">
              {view === "graph"
                ? <ExplorerGraph data={data} selected={chosen ? graphKeyOf(chosen, t.id) : null} onSelect={pickFromGraph} onShowTable={() => onView("table")} />
                : <DependencyTable data={data} loops={loops} selected={selected} onSelect={select} />}
            </div>
            {/* In the graph view the panel sits below the picture, and says so when nothing is chosen. */}
            {(view === "table" || chosen) && <SidePanel node={chosen} data={data} write={write} focusSignal={focusPanel} onBack={view === "table" ? backToRow : undefined} onConfirm={(n) => setConfirming({ kind: "relationship", id: n.link.relationship!, sentence: sentenceOf(n), state: n.link.state, why: n.link.why })} />}
          </div>
        </>
      )}
      <p className="mt-4 flex items-start gap-2 text-sm">
        <span aria-hidden="true">⚠</span>
        <span><span>A missing path is not evidence that a change is safe.</span>{verify > 0 && ` ${verify} ${verify === 1 ? "link needs" : "links need"} verification.`}</span>
      </p>
      <ConfirmLinkDialog target={confirming} onClose={() => setConfirming(null)} onDone={(m) => { setConfirming(null); setNotice(m); load.reload(); }} />
    </>
  );
}

// ---- Table ----------------------------------------------------------------------------------------------------------

function OwnerCell({ owner, gaps }: { owner: Owner | null; gaps: string[] }) {
  if (!owner) return <Gap label="No technical owner" />;
  return <>{owner.name}{(!owner.active || gaps.includes("Inactive owner")) && <> <Gap label="Inactive owner" /></>}</>;
}

function DependencyTable({ data, loops, selected, onSelect }: { data: Dependencies; loops: Set<string>; selected: string; onSelect: (key: string) => void }) {
  const cols = 7;
  const t = data.target;
  return (
    <TableRegion label="Dependencies table">
      <Table>
        <TableCaption className="sr-only">{DIRECTION_TEXT[data.direction]} of {t.name}, nearest first. Data flows follow in their own group.</TableCaption>
        <TableHeader><TableRow>
          <TableHead scope="col">System</TableHead><TableHead scope="col">Kind</TableHead><TableHead scope="col" aria-sort="ascending">Depth</TableHead>
          <TableHead scope="col">Path from target</TableHead><TableHead scope="col">Link status</TableHead><TableHead scope="col">Owner</TableHead>
          <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
        </TableRow></TableHeader>
        {data.nodes.length > 0 && (
          <TableBody>
            {data.nodes.map((n) => {
              const key = nodeKey(n);
              return (
                <TableRow key={key} className={`align-top ${selected === key ? "bg-accent" : ""} ${n.lifecycle === "retired" ? "text-muted-foreground" : ""}`} aria-selected={selected === key || undefined}>
                  <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold [overflow-wrap:anywhere]">
                    <RecordLink record={n} className="underline" />
                    {n.lifecycle === "retired" && <LifecycleChip value="retired" className="ml-2 align-middle" />}
                    {n.archived && <Chip className="ml-2 align-middle">Archived</Chip>}
                    {closesLoop(n, loops) && <Chip className="ml-2 align-middle"><span aria-hidden="true">↻</span>Closes a loop<span className="sr-only"> (this link leads back to a record already on the path)</span></Chip>}
                    {data.direction === "both" && <span className="block text-xs font-normal text-muted-foreground">{n.direction === "dependents" ? "Depends on the target" : "The target depends on it"}</span>}
                  </TableHead>
                  <TableCell>{KIND_LABEL[n.kind] ?? cap(n.kind)}</TableCell>
                  <TableCell className="tabular">{n.depth}</TableCell>
                  <TableCell className="min-w-56 max-w-sm whitespace-normal [overflow-wrap:anywhere]">
                    {pathText(n)}
                    {closesLoop(n, loops) && <span className="mt-1 block text-xs text-muted-foreground"><span aria-hidden="true">↻ </span>Leads back to {n.name}, already on this path, so it isn't followed further.</span>}
                  </TableCell>
                  <TableCell className="whitespace-normal">
                    <LinkStateChip state={n.link.state} />
                    {n.link.last_confirmed_on && <span className="tabular block text-xs text-muted-foreground">{n.link.last_confirmed_on}</span>}
                  </TableCell>
                  <TableCell className="whitespace-normal"><OwnerCell owner={n.technical_owner} gaps={n.gaps} /></TableCell>
                  <TableCell className="text-right">
                    <Button size="sm" variant="outline" data-show-path={key} onClick={() => onSelect(key)}>Show path<span className="sr-only"> for {n.name}</span></Button>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        )}
        {data.data_flows.length > 0 && (
          <TableBody>
            <TableRow className="bg-table-head hover:bg-table-head">
              <TableHead colSpan={cols} scope="rowgroup" className="h-9 text-xs font-semibold text-muted-foreground">Data flows — data flow, not operational</TableHead>
            </TableRow>
            {data.data_flows.map((f) => (
              <TableRow key={f.id} className={`align-top ${f.lifecycle === "retired" ? "text-muted-foreground" : ""}`}>
                <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold [overflow-wrap:anywhere]">
                  <Link to={`/integrations/${f.id}`} className="underline">{f.name}</Link>
                  {f.lifecycle === "retired" && <LifecycleChip value="retired" className="ml-2 align-middle" />}
                  <span className="block text-xs font-normal text-muted-foreground">{endText(f.source)} <span aria-hidden="true">→</span><span className="sr-only"> to </span> {endText(f.target)}</span>
                </TableHead>
                <TableCell>Integration</TableCell>
                <TableCell className="tabular">{f.hop} hop</TableCell>
                <TableCell className="min-w-56 max-w-sm whitespace-normal [overflow-wrap:anywhere]">
                  {f.vendor_native ? "Vendor-native" : <>Implemented by {f.implemented_by.map((c, i) => <span key={c.id}>{i > 0 && ", "}<Link to={`/components/${c.id}`} className="underline">{c.name}</Link></span>)}</>}
                  {(f.protocol || f.schedule) && <> · {[f.protocol, f.schedule].filter(Boolean).join(" ")}</>}
                  {(f.direction || f.auth_method) && (
                    <span className="block text-xs text-muted-foreground">
                      {f.direction === "two_way" ? <><span aria-hidden="true">⇄ </span>Both ways</> : f.direction === "one_way" ? <><span aria-hidden="true">→ </span>One way</> : null}
                      {f.direction && f.auth_method && " · "}
                      {f.auth_method && `Authentication: ${labelOf(INTEGRATION_AUTH_METHODS, f.auth_method)}`}
                    </span>
                  )}
                  <span className="block text-xs text-muted-foreground">Attached to {f.attached_to.name}</span>
                </TableCell>
                <TableCell className="whitespace-normal">
                  <LinkStateChip state={f.link.state} />
                  {f.link.last_confirmed_on && <span className="tabular block text-xs text-muted-foreground">{f.link.last_confirmed_on}</span>}
                </TableCell>
                <TableCell className="whitespace-normal"><OwnerCell owner={f.technical_owner} gaps={[]} /></TableCell>
                <TableCell />
              </TableRow>
            ))}
          </TableBody>
        )}
      </Table>
    </TableRegion>
  );
}

function endText(e: FlowEnd) { return e.instance ? `${e.name} (${e.instance.name})` : e.name; }

/** A row whose link closes a loop: a cycle row, or a link the response lists in `cycles`. */
const closesLoop = (n: DepNode, loops: Set<string>) => n.cycle || (!!n.link.relationship && loops.has(n.link.relationship));

// ---- Side panel -----------------------------------------------------------------------------------------------------

function SidePanel({ node, data, write, focusSignal, onBack, onConfirm }: {
  node: DepNode | null; data: Dependencies; write: boolean; focusSignal: number; onBack?: () => void; onConfirm: (n: DepNode) => void;
}) {
  const icon = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><circle cx="3.5" cy="8" r="1.8" /><circle cx="12.5" cy="3.5" r="1.8" /><circle cx="12.5" cy="12.5" r="1.8" /><path d="M5.2 7.2 10.8 4.3M5.2 8.8l5.6 2.9" /></svg>;
  const { chain, types } = node ? chainOf(node) : { chain: [], types: [] };
  // Below the widest screens the panel sits under the table. Each Show path brings it into view and moves focus to its
  // heading; Back to the row returns to the button that chose it.
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!focusSignal) return;
    ref.current?.scrollIntoView({ block: "nearest" });
    document.getElementById("h-selected")?.focus({ preventScroll: true });
  }, [focusSignal]);
  return (
    <aside ref={ref} aria-label="Selected record" className="2xl:sticky 2xl:top-4">
      {/* The region stays mounted so a new selection is announced. */}
      <div aria-live="polite">
        <Panel id="h-selected" icon={icon} title={node ? `Selected: ${node.name}` : "Selected record"}>
          {!node ? (
            <p className="px-4 py-3 text-muted-foreground">Choose Show path on a row of the table to see how that record connects to {data.target.name}.</p>
          ) : (
            <div className="pb-3">
              <DetailList>
                <DetailRow label="Kind">{KIND_LABEL[node.kind] ?? cap(node.kind)}{node.archived && " · Archived"}</DetailRow>
                <DetailRow label="Lifecycle"><LifecycleChip value={node.lifecycle} /></DetailRow>
                <DetailRow label="Technical owner"><OwnerCell owner={node.technical_owner} gaps={node.gaps} /></DetailRow>
                <DetailRow label="Depth">{node.depth}</DetailRow>
              </DetailList>
              <h3 className="px-4 pb-1 pt-3 text-sm font-semibold">Path from target</h3>
              <ol className="px-4 text-sm" aria-label={`Path from ${data.target.name} to ${node.name}`}>
                {chain.map((s, i) => (
                  <li key={`${s.id}-${i}`} style={{ paddingLeft: `${i * 12}px` }}>
                    {i > 0 && <span aria-hidden="true">└ </span>}
                    {i > 0 && <span className="text-muted-foreground">{types[i - 1]} ▸ </span>}
                    {s.text}
                  </li>
                ))}
                {node.cycle && <li className="mt-1 text-muted-foreground"><span aria-hidden="true">↻</span> Closes a loop: this path returns to {chain[chain.length - 1]?.name}, which is already listed.</li>}
              </ol>
              <h3 className="px-4 pb-1 pt-3 text-sm font-semibold">Link status</h3>
              <p className="flex flex-wrap items-center gap-2 px-4 text-sm"><LinkStateChip state={node.link.state} /><span className="text-muted-foreground">{node.link.why}</span></p>
              <p className="px-4 pt-1 text-xs text-muted-foreground">On {sentenceOf(node)}.</p>
              <div className="flex flex-wrap gap-2 px-4 pt-3">
                {pathOf(node) && <Button asChild size="sm" variant="outline"><Link to={pathOf(node)!}>Open record<span className="sr-only"> {node.name}</span></Link></Button>}
                {write && <Button size="sm" variant="outline" onClick={() => onConfirm(node)}>Confirm link<span className="sr-only"> {sentenceOf(node)}</span></Button>}
                {onBack && <Button size="sm" variant="ghost" onClick={onBack}>Back to the selected row<span className="sr-only">, {node.name}</span></Button>}
              </div>
            </div>
          )}
        </Panel>
      </div>
    </aside>
  );
}
