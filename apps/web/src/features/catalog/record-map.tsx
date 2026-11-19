/** A record's Map tab: everything the record is connected to, one or two steps
 *  out, as a Cytoscape picture with the table beside it (stacked on narrow screens). Loaded on first use from detail.tsx.
 *
 *  - `GET /<records>/<id>/map?depth=1|2`: nodes, edges, rows (one per edge, read from the near side) and categories with
 *    the record's own counts. The table groups rows by `categories`, so a category added on the API side (contracts and
 *    licence pools, say) shows with no change here.
 *  - The URL holds the depth (`depth=2`), the selected node (`node`) and the open "What depends on this" answer
 *    (`dependents`), so a link reproduces the view.
 *  - Selecting a node (click, Enter on the canvas, or Show on map in the table) highlights it in the picture and the
 *    table and offers Open, Center map here and What depends on this. The answer comes from the record's own
 *    `GET /<records>/<id>/dependencies?direction=dependents&depth=all`, the same body as the explorer's.
 *  - Archived and retired nodes and integrations are shown, never hidden: a dashed border plus the word. */
import { uuidOrEmpty } from "@/lib/safe-path";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { cap } from "@/api";
import { EmptyState, ErrorBanner, Loading, TableRegion, useLoad } from "@/components/app";
import { DetailList, DetailRow, LifecycleChip, LinkStateChip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { chainOf, type Dependencies } from "@/features/catalog/explorer";
import { Graph, GraphLegend, type GraphEdge, type GraphNode } from "@/features/graph/graph-loader";
import { apiPath, isCatalogKind, isRecordKind, KIND_LABEL, recordPath, type RecordKind } from "@/lib/kinds";
import { phraseOf } from "@/lib/relationship-types";
import { cn } from "@/lib/utils";

export type MapNode = {
  id: string; kind: string; name: string; lifecycle: string | null; archived: boolean; retired: boolean; path: string | null;
  map_path: string | null; expandable: boolean; detail: Record<string, unknown>; hop: number; root: boolean;
};
export type MapEdge = {
  id: string; source: string; target: string; relation: string; category: string; label: string; reads: string; reads_inverse: string;
  archived: boolean; retired: boolean; detail: Record<string, unknown>;
};
export type MapRow = { edge: string; category: string; near: string; other: string; reads: string; hop: number };
export type RecordMap = {
  root: string; depth: number; nodes: MapNode[]; edges: MapEdge[]; rows: MapRow[]; categories: { key: string; label: string }[];
  counts: Record<string, number>; truncated: boolean; max_nodes: number;
};

const OTHER_KINDS: Record<string, string> = { instance: "Instance", discovery_source: "Discovery source", contract: "Contract", license_pool: "License pool" };
export const kindWord = (kind: string) => (isCatalogKind(kind) ? KIND_LABEL[kind] : OTHER_KINDS[kind] ?? cap(kind.replace(/_/g, " ")));

/** The SPA page a map node opens, from its kind (records and integrations: their pages), as search results do. An
 *  instance opens its application's Instances tab; a discovery source the Discovery page, filtered to it; a contract
 *  or licence pool its own page. */
export function nodeHref(n: MapNode): string | null {
  if (isCatalogKind(n.kind)) return recordPath(n.kind, n.id);
  if (n.kind === "contract") return `/contracts/${n.id}`;
  if (n.kind === "license_pool") return `/license-pools/${n.id}`;
  if (n.kind === "instance") {
    const app = typeof n.detail.application === "string" ? n.detail.application : n.path?.split("/")[1];
    return app ? recordPath("application", app, "instances") : null;
  }
  if (n.kind === "discovery_source") {
    const connector = n.detail.connector as { id?: string } | undefined;
    const q = new URLSearchParams({ status: "all", ...(connector?.id ? { connector: connector.id } : {}), q: n.name });
    return `/discovery?${q}`;
  }
  return null;
}

/** A record's own map (its `map_path`), keeping the depth. */
function mapHref(n: MapNode, depth: number) {
  if (!n.map_path || !isRecordKind(n.kind)) return null;
  return `${recordPath(n.kind, n.id, "map")}${depth === 2 ? "&depth=2" : ""}`;
}

const flagsOf = (x: { archived: boolean; retired: boolean }) => [...(x.archived ? ["Archived"] : []), ...(x.retired ? ["Retired"] : [])];
const fit = (text: string, n: number) => (text.length > n ? `${text.slice(0, n - 1)}…` : text);
// Contracts and licence pools hang off the record like instances: a thin plain line; the node says what it is in words.
const EDGE_CLASS: Record<string, string> = {
  dependency: "dependency", relationship: "relationship", integration: "integration", instance: "instance", discovery: "discovery",
  contract: "instance", license_pool: "instance",
};

/** Archived or Retired as a word in a dashed outline (never colour alone). */
export function FlagChip({ text }: { text: string }) {
  return <span className="ml-1.5 inline-flex items-center rounded-sm border border-dashed border-border-strong bg-muted px-1.5 text-xs font-medium text-muted-foreground align-middle">{text}</span>;
}

function useParam() {
  const [params, setParams] = useSearchParams();
  const update = (changes: Record<string, string | null>) => setParams((cur) => {
    const next = new URLSearchParams(cur);
    for (const [k, v] of Object.entries(changes)) { if (v) next.set(k, v); else next.delete(k); }
    return next;
  // Depth, the selected node and its question are discrete choices: each pushes, so Back undoes it.
  });
  return [params, update] as const;
}

export function MapTab({ kind, id, name, write }: { kind: RecordKind; id: string; name: string; write: boolean }) {
  const [params, update] = useParam();
  const depth = params.get("depth") === "2" ? 2 : 1;
  const load = useLoad<RecordMap>(`${apiPath(kind)}/${id}/map?depth=${depth}`);
  // While another depth loads, the previous map stays on screen (useLoad keeps it).
  const data = load.data?.root === id ? load.data : null;
  const selectedId = params.get("node");
  const askedId = uuidOrEmpty(params.get("dependents")) || null;
  // Where focus goes after this render: a panel's heading, when its own action opened it (one target per panel, so
  // opening one never focuses the other). Runs after the render that changes the selection, the answer or the map, so
  // a panel that mounts in that render is there. Once the map is on screen without the panel (a node that isn't on it,
  // an empty map), the heading can't appear any more, so the request is dropped rather than kept for a later render.
  const [focusTo, setFocusTo] = useState<"map-selected" | "map-dependents" | null>(null);
  useEffect(() => {
    if (!focusTo) return;
    const heading = document.getElementById(focusTo);
    if (!heading) {
      if (data) setFocusTo(null);
      return;
    }
    heading.closest("section")?.scrollIntoView({ block: "nearest" });
    heading.focus({ preventScroll: true });
    setFocusTo(null);
  }, [focusTo, selectedId, askedId, data]);
  // The control that opened each panel, so closing the panel returns focus to it.
  const openers = useRef<{ select: HTMLElement | null; ask: HTMLElement | null }>({ select: null, ask: null });

  const byId = useMemo(() => new Map((data?.nodes ?? []).map((n) => [n.id, n])), [data]);
  const edgeById = useMemo(() => new Map((data?.edges ?? []).map((e) => [e.id, e])), [data]);
  const graph = useMemo(() => (data ? toGraph(data) : null), [data]);
  const selected = selectedId ? byId.get(selectedId) ?? null : null;
  const asked = askedId ? byId.get(askedId) ?? null : null;

  if (load.error && !data) return <ErrorBanner message={`Couldn't load the map. ${load.error}`} onRetry={load.reload} />;
  const steps = depth === 2 ? "2 steps" : "1 step";
  // A depth change that failed keeps the previous map on screen, so say so; one still loading says that.
  // The live region stays mounted (empty) while nothing loads, so "Loading 2 steps…" is announced when it fills in
  // rather than mounting with its text already inside.
  const switching = !load.error && !!data && data.depth !== depth;
  const depthState = <>
    {load.error && <ErrorBanner message={`Couldn't load the map at ${steps}. ${load.error} The map below is the one shown before.`} onRetry={load.reload} />}
    <p className={cn("text-sm text-muted-foreground", switching && "mb-3")} aria-live="polite" data-testid="map-depth-status">{switching ? `Loading ${steps}…` : ""}</p>
  </>;
  if (!data || !graph) return <Loading what="the map" rows={4} />;

  const root = byId.get(data.root);
  const depthToggle = (
    <div role="group" aria-label="How far out" className="inline-flex overflow-hidden rounded-md border">
      {[1, 2].map((d) => (
        <button key={d} type="button" aria-pressed={depth === d} onClick={() => update({ depth: d === 1 ? null : "2" })}
          className="px-3 py-1.5 text-sm aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground [&:not(:first-child)]:border-l">
          {d === 1 ? "1 step" : "2 steps"}
        </button>
      ))}
    </div>
  );

  if (!data.rows.length) {
    const what = data.categories.map((c) => c.label.toLowerCase()).join(", ");
    return (
      <>
        <div className="mb-3 flex flex-wrap items-center gap-3">{depthToggle}</div>
        {depthState}
        <EmptyState title="No connections yet" action={write && <Button asChild variant="outline"><Link to={recordPath(kind, id, "relationships")!}>Add relationship</Link></Button>}>
          Nothing is recorded as connected to {name}{what ? ` (${what})` : ""}. That doesn't mean it has none.
        </EmptyState>
      </>
    );
  }

  const opener = () => {
    const el = document.activeElement;
    return el instanceof HTMLElement && el !== document.body ? el : null;
  };
  /** Focus the control that opened a panel once the panel has gone, else the first of `fallbacks` on the page. */
  const returnFocus = (which: "select" | "ask", fallbacks: string[]) => setTimeout(() => {
    const kept = openers.current[which];
    const to = (kept?.isConnected ? kept : null) ?? fallbacks.map((f) => document.querySelector<HTMLElement>(f)).find(Boolean);
    to?.focus();
  });
  const choose = (nodeId: string, focus = false) => {
    openers.current.select = opener();
    update({ node: nodeId });
    if (focus) setFocusTo("map-selected");
  };
  const ask = (n: MapNode) => {
    openers.current.ask = opener();
    update({ node: n.id, dependents: n.id });
    setFocusTo("map-dependents");
  };
  const clearSelection = () => {
    update({ node: null });
    returnFocus("select", [`[data-show-node="${CSS.escape(selectedId ?? "")}"]`, "[data-testid=graph-canvas]"]);
  };
  const closeAnswer = () => {
    update({ dependents: null });
    returnFocus("ask", [`[data-ask-node="${CSS.escape(askedId ?? "")}"]`, "[data-testid=graph-canvas]"]);
  };
  const own = data.categories.filter((c) => (data.counts[c.key] ?? 0) > 0).map((c) => `${c.label}: ${data.counts[c.key]}`);
  const further = data.rows.filter((r) => r.hop === 2).length;

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2">
        {depthToggle}
        {root && <Button type="button" size="sm" variant="outline" data-ask-node={root.id} onClick={() => ask(root)}>What depends on {name}</Button>}
        <p className="text-sm text-muted-foreground" role="status">
          {own.length ? `${name}'s own connections: ${own.join(", ")}.` : `${name} has no direct connections.`}
          {depth === 2 && ` ${further} more ${further === 1 ? "connection" : "connections"} two steps away.`}
        </p>
      </div>
      {depthState}
      {data.truncated && (
        <p className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" role="status">
          <span aria-hidden="true">⚠ </span>The map stops at {data.max_nodes} items, so some connections aren't shown. {depth === 2 ? "Use 1 step, or open a connected record's own map." : "The counts above include them all."}
        </p>
      )}
      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,28rem)]">
        <div className="min-w-0">
          {/* Read only through aria-describedby (hidden), so it isn't heard twice in reading order. */}
          <p id="map-desc" hidden>{describe(data, byId)}</p>
          <Graph nodes={graph.nodes} edges={graph.edges} layout="concentric" label={`Map of what ${name} is connected to`} describedBy="map-desc"
            selected={selectedId} onSelect={(nid) => choose(nid)} height={depth === 2 ? 520 : 440}
            tableNote="The Connections table beside it lists every connection."
            extras={<>
              <GraphLegend items={[
                { key: "dependency", text: "Dependency (arrow to what is needed)" },
                { key: "relationship", text: "Other relationship" },
                { key: "flow", text: "Integration, a data flow" },
                { key: "instance", text: "Instance, contract or license pool (the node names which)" },
                { key: "discovery", text: "Discovery source" },
                { key: "flagged-node", text: "Archived or retired (also said in words)" },
              ]} />
              <p className="mt-2 text-sm text-muted-foreground">
                The map is a picture: click a node, or focus it and use the arrow keys and Enter, to select it. The table lists every connection.
              </p>
            </>} />
          {selected && (
            <Selected node={selected} root={root} depth={depth} onAsk={() => ask(selected)} onClear={clearSelection} />
          )}
          {/* Kept when the record isn't on this map (another depth, or a shared link): the answer is fetched by ID. */}
          {askedId && (!asked || isRecordKind(asked.kind)) && (
            <DependentsAnswer key={askedId} id={askedId} node={asked} onClose={closeAnswer} />
          )}
        </div>
        <ConnectionsTable data={data} name={name} byId={byId} edgeById={edgeById} selected={selectedId}
          onShow={(nid) => choose(nid, true)} onAsk={ask} />
      </div>
    </>
  );
}

// ---- the picture ------------------------------------------------------------------------------------------------------

function toGraph(data: RecordMap): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const nodes: GraphNode[] = data.nodes.map((n) => {
    const flags = flagsOf(n);
    const second = [kindWord(n.kind), ...(n.root ? ["this record"] : []), ...flags].join(" · ");
    return {
      id: n.id,
      label: `${fit(n.name, 24)}\n${second}`,
      spoken: `${n.name}, ${second}`,
      level: n.hop,
      classes: [`kind-${n.kind}`, n.root ? "root" : "", flags.length ? "flagged" : ""].filter(Boolean).join(" "),
    };
  });
  const edges: GraphEdge[] = data.edges.map((e) => {
    const flags = flagsOf(e);
    const named = e.category === "dependency" || e.category === "relationship" || e.category === "integration" || e.category === "discovery";
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      label: named ? `${fit(e.label, 22)}${flags.length ? ` (${flags.join(", ").toLowerCase()})` : ""}` : "",
      classes: [EDGE_CLASS[e.category] ?? "relationship", e.detail.direction === "two_way" ? "two-way" : "", flags.length ? "flagged" : ""].filter(Boolean).join(" "),
    };
  });
  return { nodes, edges };
}

function describe(data: RecordMap, byId: Map<string, MapNode>) {
  const root = byId.get(data.root);
  const parts = [`${root?.name ?? "The record"} is in the middle; what it is connected to is around it${data.depth === 2 ? ", and what those are connected to on the outer ring" : ""}.`];
  for (const c of data.categories) {
    const names = data.rows.filter((r) => r.category === c.key).map((r) => byId.get(r.other)?.name).filter(Boolean) as string[];
    if (names.length) parts.push(`${c.label}: ${names.length > 12 ? `${names.slice(0, 12).join(", ")} and ${names.length - 12} more` : names.join(", ")}.`);
  }
  parts.push("The table beside the map lists the same connections.");
  return parts.join(" ");
}

// ---- the table ---------------------------------------------------------------------------------------------------------

function ConnectionsTable({ data, name, byId, edgeById, selected, onShow, onAsk }: {
  data: RecordMap; name: string; byId: Map<string, MapNode>; edgeById: Map<string, MapEdge>; selected: string | null;
  onShow: (id: string) => void; onAsk: (n: MapNode) => void;
}) {
  const groups = data.categories.map((c) => ({ ...c, rows: data.rows.filter((r) => r.category === c.key) })).filter((g) => g.rows.length);
  // A category the API sends that isn't in `categories` still shows, last.
  const known = new Set(data.categories.map((c) => c.key));
  const strays = data.rows.filter((r) => !known.has(r.category));
  if (strays.length) groups.push({ key: "other", label: "Other connections", rows: strays });
  return (
    <TableRegion label="Connections table" className="min-w-0">
      <Table>
        <TableCaption className="sr-only">
          Everything {name} is connected to{data.depth === 2 ? ", up to two steps away" : ""}, grouped by kind of connection. Each connection reads from the record nearer to {name}.
        </TableCaption>
        <TableHeader><TableRow>
          <TableHead scope="col">Connected to</TableHead><TableHead scope="col">Connection</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
        </TableRow></TableHeader>
        {groups.map((g) => {
          const own = data.counts[g.key];
          const more = g.rows.filter((r) => r.hop === 2).length;
          return (
            <TableBody key={g.key} data-category={g.key}>
              <TableRow className="bg-table-head hover:bg-table-head">
                <TableHead colSpan={3} scope="rowgroup" className="h-9 text-xs font-semibold text-muted-foreground">
                  {g.label} ({own ?? g.rows.length}){more > 0 && `, and ${more} two steps away`}
                </TableHead>
              </TableRow>
              {g.rows.map((r) => {
                const other = byId.get(r.other);
                const near = byId.get(r.near);
                const edge = edgeById.get(r.edge);
                if (!other || !edge) return null;
                return <ConnectionRow key={r.edge} row={r} other={other} near={near} edge={edge} selected={selected === other.id} onShow={onShow} onAsk={onAsk} />;
              })}
            </TableBody>
          );
        })}
      </Table>
    </TableRegion>
  );
}

function ConnectionRow({ row, other, near, edge, selected, onShow, onAsk }: {
  row: MapRow; other: MapNode; near?: MapNode; edge: MapEdge; selected: boolean; onShow: (id: string) => void; onAsk: (n: MapNode) => void;
}) {
  const href = nodeHref(other);
  const integration = edge.detail.integration as { id: string; name: string } | undefined;
  const edgeFlags = flagsOf(edge);
  return (
    <TableRow aria-selected={selected || undefined} className={cn("align-top", selected && "bg-accent hover:bg-accent", (other.archived || other.retired) && "text-muted-foreground")}>
      <TableHead scope="row" className="min-w-36 whitespace-normal font-semibold [overflow-wrap:anywhere]">
        {href ? <Link to={href} className="underline">{other.name}</Link> : other.name}
        {other.archived && <FlagChip text="Archived" />}
        {other.retired && <FlagChip text="Retired" />}
        {selected && <span className="ml-1.5 text-xs font-semibold text-accent-foreground"><span aria-hidden="true">▸ </span>Selected</span>}
        <span className="block text-xs font-normal text-muted-foreground">
          {kindWord(other.kind)}{row.hop === 2 && near && ` · through ${near.name}`}
        </span>
      </TableHead>
      <TableCell className="whitespace-normal [overflow-wrap:anywhere]">
        {row.hop === 2 && near ? `${near.name} ${phraseOf(row.reads)}` : row.reads}
        {integration && (
          <span className="block text-xs text-muted-foreground">
            Integration <Link to={recordPath("integration", integration.id)!} className="underline">{integration.name}</Link>
            {edgeFlags.map((f) => <FlagChip key={f} text={f} />)}
            {typeof edge.detail.protocol === "string" && edge.detail.protocol && ` · ${edge.detail.protocol}`}
          </span>
        )}
        {other.kind === "contract" && <ContractNote node={other} edge={edge} />}
        {other.kind === "license_pool" && <PoolNote node={other} />}
        {other.kind === "discovery_source" && typeof edge.detail.decision === "string" && (
          <span className="block text-xs text-muted-foreground">{edge.label} · {cap(edge.detail.decision)}{edge.detail.gone ? " · no longer seen in the source" : ""}</span>
        )}
      </TableCell>
      <TableCell className="whitespace-normal text-right">
        <span className="inline-flex flex-col items-end gap-1.5">
          <Button size="sm" variant="outline" data-show-node={other.id} onClick={() => onShow(other.id)}>Show on map<span className="sr-only">, {other.name}</span></Button>
          {isRecordKind(other.kind) && (
            <Button size="sm" variant="outline" data-ask-node={other.id} onClick={() => onAsk(other)}>What depends on this<span className="sr-only">, {other.name}</span></Button>
          )}
        </span>
      </TableCell>
    </TableRow>
  );
}

/** A contract line on the map: its share and the contract's status and end date, in words. */
function ContractNote({ node, edge }: { node: MapNode; edge: MapEdge }) {
  const pct = typeof edge.detail.allocation_percent === "string" ? `${Number.parseFloat(edge.detail.allocation_percent)}% fixed share` : "Shares the rest";
  const status = typeof node.detail.status === "string" ? cap(node.detail.status.replace(/_/g, " ")) : "";
  const end = typeof node.detail.end_date === "string" ? `ends ${node.detail.end_date}` : "No end date";
  return <span className="block text-xs text-muted-foreground">{[pct, status, end].filter(Boolean).join(" · ")}</span>;
}

/** A licence pool on the map: its seats, in words. */
function PoolNote({ node }: { node: MapNode }) {
  const d = node.detail as { seats_purchased?: number; seats_assigned?: number; utilization?: number | null };
  if (typeof d.seats_purchased !== "number" || typeof d.seats_assigned !== "number") return null;
  const over = d.seats_assigned > d.seats_purchased;
  return (
    <span className="block text-xs text-muted-foreground">
      {d.seats_assigned} of {d.seats_purchased} seats assigned{d.utilization != null && ` (${d.utilization}%)`}{over && <> · <span aria-hidden="true">! </span>Overused</>}
    </span>
  );
}

// ---- the selected node -------------------------------------------------------------------------------------------------

function Selected({ node, root, depth, onAsk, onClear }: {
  node: MapNode; root?: MapNode; depth: number; onAsk: () => void; onClear: () => void;
}) {
  const href = nodeHref(node);
  const centre = node.root ? null : mapHref(node, depth);
  const connector = node.detail.connector as { name?: string } | undefined;
  return (
    <section aria-labelledby="map-selected" className="mt-4 rounded-lg border bg-card" data-testid="map-selected">
      <h3 id="map-selected" tabIndex={-1} className="border-b px-4 py-2.5 font-display text-section font-semibold [overflow-wrap:anywhere]">Selected: {node.name}</h3>
      <DetailList>
        <DetailRow label="Kind">{kindWord(node.kind)}{node.root && " (this record)"}</DetailRow>
        {node.lifecycle && <DetailRow label="Lifecycle"><LifecycleChip value={node.lifecycle} /></DetailRow>}
        {(node.archived || node.retired) && <DetailRow label="State">{flagsOf(node).join(", ")}</DetailRow>}
        {!node.root && <DetailRow label="Steps away">{node.hop} from {root?.name ?? "the record"}</DetailRow>}
        {node.kind === "instance" && typeof node.detail.environment === "string" && <DetailRow label="Environment">{node.detail.environment ? cap(node.detail.environment) : "Not recorded"}</DetailRow>}
        {node.kind === "discovery_source" && connector?.name && <DetailRow label="Found by">{connector.name}</DetailRow>}
      </DetailList>
      <div className="flex flex-wrap gap-2 px-4 pb-3 pt-1">
        {href && !node.root && <Button asChild size="sm" variant="outline"><Link to={href}>Open<span className="sr-only"> {node.name}</span></Link></Button>}
        {centre && <Button asChild size="sm" variant="outline"><Link to={centre}>Center map here<span className="sr-only">, on {node.name}</span></Link></Button>}
        {isRecordKind(node.kind) && <Button size="sm" variant="outline" onClick={onAsk}>What depends on this<span className="sr-only">, {node.name}</span></Button>}
        <Button size="sm" variant="ghost" onClick={onClear}>Clear selection</Button>
      </div>
    </section>
  );
}

// ---- "What depends on this" ----------------------------------------------------------------------------------------------

/** What depends on a record, at any depth: from its own `/dependencies` when it is on the map (its kind known), else
 *  from the explorer's `/dependencies?target=` (the same body), so a link whose record isn't on this map still answers. */
function DependentsAnswer({ id, node, onClose }: { id: string; node: MapNode | null; onClose: () => void }) {
  const query = "direction=dependents&depth=all&data_flows=false";
  const path = node && isRecordKind(node.kind) ? `${apiPath(node.kind)}/${id}/dependencies?${query}` : `dependencies?target=${id}&${query}`;
  const load = useLoad<Dependencies>(path, { strict: true });
  const data = load.data;
  const name = node?.name ?? data?.target.name ?? "this record";
  const cycles = new Set((data?.cycles ?? []).map((c) => c.relationship));
  const rows = data?.nodes.filter((n) => !n.cycle) ?? [];
  const loops = data?.nodes.filter((n) => n.cycle) ?? [];
  return (
    <section aria-labelledby="map-dependents" className="mt-4 rounded-lg border bg-card" data-testid="map-dependents">
      <h3 id="map-dependents" tabIndex={-1} className="border-b px-4 py-2.5 font-display text-section font-semibold [overflow-wrap:anywhere]">What depends on {name}</h3>
      {/* The summary line is the live region (role="status"), not the list under it. */}
      <div className="px-4 py-3">
        {load.error ? (
          <ErrorBanner message={`Couldn't load what depends on ${name}. ${load.error}`} onRetry={load.reload} />
        ) : !data ? (
          <Loading what={`what depends on ${name}`} />
        ) : !rows.length ? (
          <p role="status">Nothing is recorded as depending on {name}, at any depth. That doesn't mean nothing does.</p>
        ) : (
          <>
            <p className="mb-2" role="status">
              {rows.length} {rows.length === 1 ? "record depends" : "records depend"} on {name}, directly or through others
              {data.summary.hidden_retired > 0 && ` (${data.summary.hidden_retired} retired left out)`}.
              {loops.length > 0 && ` ${loops.length} ${loops.length === 1 ? "link closes" : "links close"} a loop.`}
              {data.capped && ` Showing the nearest ${data.shown} of ${data.total}.`}
            </p>
            <ul aria-label={`Records that depend on ${name}`} className="space-y-1.5 text-sm">
              {rows.map((n) => {
                const { chain, types } = chainOf(n);
                const href = recordPath(n.kind, n.id);
                return (
                  <li key={`${n.link.relationship}:${n.id}`}>
                    {href ? <Link to={href} className="font-semibold underline">{n.name}</Link> : <span className="font-semibold">{n.name}</span>}
                    {n.archived && <FlagChip text="Archived" />}
                    {n.lifecycle === "retired" && <FlagChip text="Retired" />}
                    <span className="text-muted-foreground"> · {kindWord(n.kind)} · {n.depth === 1 ? "directly" : `${n.depth} links away`}</span>
                    {n.depth > 1 && <span className="block text-xs text-muted-foreground">{chain.map((s, i) => (i < types.length ? `${s.text} → ${types[i]} → ` : s.text)).join("")}</span>}
                    {cycles.has(n.link.relationship ?? "") && <span className="block text-xs"><span aria-hidden="true">↻ </span>Closes a loop</span>}
                    <span className="sr-only"> Link status: </span><span className="ml-1 inline-block align-middle"><LinkStateChip state={n.link.state} /></span>
                  </li>
                );
              })}
            </ul>
          </>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          <Button asChild size="sm" variant="outline"><Link to={`/dependencies?target=${id}&depth=all`}>Open in Dependencies<span className="sr-only">, what depends on {name}</span></Link></Button>
          <Button size="sm" variant="ghost" onClick={onClose}>Close<span className="sr-only"> what depends on {name}</span></Button>
        </div>
      </div>
    </section>
  );
}
