import { cap } from "@/api";
import { isCatalogKind, KIND_LABEL } from "@/lib/kinds";
import { Button } from "@/components/ui/button";
import type { DepFlow, DepNode, Dependencies } from "@/features/catalog/explorer";
import { phraseOf } from "@/lib/relationship-types";

/** Static layered graph: one `role="img"` SVG with a title and a description, no interaction. Columns are
 *  the distance from the target (dependents to the left, dependencies to the right). The table is the complete
 *  equivalent. Meaning never rests on colour: edges differ by line pattern, a "?" badge marks Needs verification, and a
 *  retired record carries the word Retired.
 *
 *  The explorer's Graph view is Cytoscape (explorer-graph.tsx, loaded on first use), built from the same
 *  `layout()` model. This picture stays as the printed graph (a canvas prints as a screenshot of whatever is in view) and
 *  as the fallback when the Cytoscape chunk can't be loaded. */

const NODE_W = 184;
const NODE_H = 48;
const LAYER_W = 300;
const ROW_H = 84;
const PAD = 24;
const ARC_STEP = 16;

export type GNode = { key: string; id: string; name: string; kind: string; lifecycle: string; layer: number; gap: boolean; archived?: boolean; target?: boolean; ghost?: boolean };
/** `relationship`: the link's ID, so the links in the response's `cycles` can be marked; `typeName`: the link's type in
 *  words, for a type other than depends_on. */
export type GEdge = { key: string; from: string; to: string; style: "depends" | "flow" | "implements"; label?: string; verify: boolean; cycle: boolean; relationship?: string; typeName?: string };

const fit = (text: string, n: number) => (text.length > n ? `${text.slice(0, n - 1)}…` : text);
const hasGap = (n: DepNode) => n.technical_owner === null || n.gaps.includes("Inactive owner");

export function layout(data: Dependencies) {
  const t = data.target;
  const nodes: GNode[] = [{ key: "target", id: t.id, name: t.name, kind: t.kind, lifecycle: t.lifecycle, layer: 0, gap: false, target: true }];
  const byKey = new Map<string, GNode>([["target", nodes[0]]]);
  const add = (n: GNode) => { if (!byKey.has(n.key)) { byKey.set(n.key, n); nodes.push(n); } };
  for (const n of data.nodes) {
    if (n.cycle) continue;
    add({ key: `${n.direction}:${n.id}`, id: n.id, name: n.name, kind: n.kind, lifecycle: n.lifecycle, layer: n.direction === "dependents" ? -n.depth : n.depth, gap: hasGap(n), archived: n.archived });
  }
  const edges: GEdge[] = [];
  for (const n of data.nodes) {
    const len = n.path.length;
    if (len < 2) continue;
    const parent = len === 2 ? "target" : `${n.direction}:${n.path[len - 2].id}`;
    const child = n.cycle && n.id === t.id ? "target" : `${n.direction}:${n.id}`;
    if (!byKey.has(parent) || !byKey.has(child)) continue;
    const [from, to] = n.direction === "dependents" ? [child, parent] : [parent, child];
    const via = n.path[len - 1].via;
    edges.push({
      key: `e:${n.link.relationship}`, from, to, style: "depends", verify: n.link.state === "needs_verification", cycle: n.cycle, relationship: n.link.relationship,
      typeName: via && via.type !== "depends_on" ? phraseOf(via.type_name, via.type) : undefined,
    });
  }
  const anyNode = (id: string) => (id === t.id ? byKey.get("target") : [...byKey.values()].find((g) => g.id === id && !g.ghost));
  for (const f of data.data_flows) {
    const attached = anyNode(f.attached_to.id);
    if (!attached) continue;
    const otherEnd = f.source.id === attached.id && f.target.id !== attached.id ? f.target : f.source.id !== attached.id ? f.source : f.target;
    let other = anyNode(otherEnd.id);
    if (!other || other === attached) {
      const outward = attached.layer < 0 ? -1 : 1;
      other = { key: `flow-end:${f.id}`, id: otherEnd.id, name: otherEnd.name, kind: otherEnd.kind, lifecycle: "", layer: attached.layer + outward, gap: false, ghost: true };
      add(other);
    }
    const sourceFirst = f.source.id === attached.id;
    edges.push({ key: `f:${f.id}`, from: sourceFirst ? attached.key : other.key, to: sourceFirst ? other.key : attached.key, style: "flow", label: f.name, verify: f.link.state === "needs_verification", cycle: false });
    for (const c of f.implemented_by) {
      const impl = anyNode(c.id);
      if (impl) edges.push({ key: `i:${f.id}:${c.id}`, from: impl.key, to: `f:${f.id}`, style: "implements", verify: false, cycle: false });
    }
  }
  const layers = nodes.map((n) => n.layer);
  const min = Math.min(...layers);
  const max = Math.max(...layers);
  // An edge that doesn't join neighbouring columns (a cycle back to the target, a data flow across the picture) is drawn
  // as an arc above the nodes, so it never cuts through the columns in between.
  const layerOf = (key: string) => byKey.get(key)?.layer ?? 0;
  const arcs = edges.filter((e) => e.style !== "implements" && Math.abs(layerOf(e.to) - layerOf(e.from)) !== 1).length;
  const top = PAD + (arcs ? 24 + ARC_STEP * arcs : 0);
  const rows = new Map<number, number>();
  const pos = new Map<string, { x: number; y: number }>();
  for (const n of nodes) {
    const i = rows.get(n.layer) ?? 0;
    rows.set(n.layer, i + 1);
    pos.set(n.key, { x: PAD + (n.layer - min) * LAYER_W, y: top + i * ROW_H });
  }
  const width = PAD * 2 + (max - min) * LAYER_W + NODE_W;
  const height = top + PAD + (Math.max(...rows.values()) - 1) * ROW_H + NODE_H;
  return { nodes, edges, pos, width, height, top, layerOf };
}

export function describe(data: Dependencies) {
  const t = data.target.name;
  const list = (items: string[]) => (items.length > 12 ? `${items.slice(0, 12).join(", ")} and ${items.length - 12} more` : items.join(", "));
  const parts = [`${t} is the target, drawn in the middle. Columns show how many links away each record is.`];
  for (const dir of ["dependents", "depends_on"] as const) {
    for (const depth of [...new Set(data.nodes.filter((n) => n.direction === dir && !n.cycle).map((n) => n.depth))].sort()) {
      const names = data.nodes.filter((n) => n.direction === dir && n.depth === depth && !n.cycle).map((n) => n.name);
      parts.push(`${dir === "dependents" ? "Depend on it" : "It depends on"}, ${depth === 1 ? "1 link away" : `${depth} links away`}: ${list(names)}.`);
    }
  }
  if (data.data_flows.length) parts.push(`Data flows, not operational dependencies: ${list(data.data_flows.map((f) => `${f.name} (${f.source.name} to ${f.target.name})`))}.`);
  const n = data.summary.needs_verification;
  if (n) parts.push(`${n} ${n === 1 ? "link needs" : "links need"} verification, marked with a question mark.`);
  if (data.summary.cycles) parts.push(`${data.summary.cycles} ${data.summary.cycles === 1 ? "path leads" : "paths lead"} back to a record already shown, each link marked as closing a loop.`);
  parts.push("The table view lists the same records with their paths.");
  return parts.join(" ");
}

/** `print`: the copy shown only when printing (no Show table button); `fallback`: shown when the Cytoscape chunk can't
 *  load, with its own element IDs so it never clashes with the print copy. */
export function DependencyGraph({ data, onShowTable, variant = "fallback" }: { data: Dependencies; onShowTable?: () => void; variant?: "print" | "fallback" }) {
  const p = variant === "print" ? "" : "fb-";
  const { nodes, edges, pos, width, height, top, layerOf } = layout(data);
  const flowById = new Map<string, DepFlow>(data.data_flows.map((f) => [`f:${f.id}`, f]));
  const anchor = (key: string, toward: { x: number; y: number }) => {
    const p = pos.get(key)!;
    return toward.x >= p.x ? { x: p.x + NODE_W, y: p.y + NODE_H / 2 } : { x: p.x, y: p.y + NODE_H / 2 };
  };
  // Where each flow's label pill sits, so implements lines can end on it.
  const pill = new Map<string, { x: number; y: number }>();
  let arcLevel = 0;
  const geometry = edges.filter((e) => e.style !== "implements").map((e) => {
    const a = pos.get(e.from)!;
    const b = pos.get(e.to)!;
    if (Math.abs(layerOf(e.to) - layerOf(e.from)) !== 1) {
      // Arc over the top: from the top centre of one node to the top centre of the other.
      const fx = a.x + NODE_W / 2;
      const tx = b.x + NODE_W / 2;
      const apex = top - 20 - ARC_STEP * arcLevel++;
      const control = (apex - 0.25 * ((a.y + b.y) / 2)) / 0.75;
      const mx = (fx + tx) / 2 + (fx === tx ? NODE_W : 0);
      if (e.style === "flow") pill.set(e.key, { x: mx, y: apex });
      return { e, d: `M${fx} ${a.y} C${fx} ${control} ${tx} ${control} ${tx} ${b.y}`, mx, my: apex };
    }
    const p1 = anchor(e.from, b);
    const p2 = anchor(e.to, a);
    const mx = (p1.x + p2.x) / 2;
    const my = (p1.y + p2.y) / 2;
    if (e.style === "flow") pill.set(e.key, { x: mx, y: my });
    return { e, d: `M${p1.x} ${p1.y} C${mx} ${p1.y} ${mx} ${p2.y} ${p2.x} ${p2.y}`, mx, my };
  });

  return (
    <div>
      <div className="relative overflow-x-auto rounded-lg border bg-card" role="region" aria-label="Dependency graph, scrollable" tabIndex={0}>
        <svg role="img" aria-labelledby={`${p}graph-title ${p}graph-desc`} width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="block max-w-none font-sans">
          <title id={`${p}graph-title`}>Dependency graph for {data.target.name}</title>
          <desc id={`${p}graph-desc`}>{describe(data)}</desc>
          <defs>
            <marker id={`${p}arrow-solid`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0 0 10 5 0 10z" className="fill-foreground" /></marker>
            <marker id={`${p}arrow-open`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0 0 10 5 0 10" fill="none" strokeWidth="1.6" className="stroke-accent-line" /></marker>
          </defs>
          {geometry.map(({ e, d }) => (
            e.style === "flow"
              ? <path key={e.key} d={d} fill="none" strokeWidth="1.8" strokeDasharray="7 5" markerEnd={`url(#${p}arrow-open)`} className="stroke-accent-line" />
              : <path key={e.key} d={d} fill="none" strokeWidth="1.8" markerEnd={`url(#${p}arrow-solid)`} className="stroke-foreground" />
          ))}
          {edges.filter((e) => e.style === "implements").map((e) => {
            const p = pill.get(e.to);
            const a = pos.get(e.from)!;
            if (!p) return null;
            return <path key={e.key} d={`M${a.x + NODE_W / 2} ${a.y + NODE_H} Q${a.x + NODE_W / 2} ${p.y + 12} ${p.x} ${p.y + 12}`} fill="none" strokeWidth="2" strokeDasharray="1 5" strokeLinecap="round" className="stroke-muted-foreground" />;
          })}
          {nodes.map((n) => {
            const p = pos.get(n.key)!;
            return (
              <g key={n.key} transform={`translate(${p.x} ${p.y})`}>
                <rect width={NODE_W} height={NODE_H} rx="7" strokeWidth={n.target ? 3 : 1.5}
                  className={`${n.target ? "fill-accent stroke-accent-line" : n.lifecycle === "retired" ? "fill-muted stroke-border-strong" : "fill-card stroke-border-strong"}`} />
                <text x="10" y="20" className={`text-[13px] font-semibold ${n.lifecycle === "retired" ? "fill-muted-foreground" : "fill-foreground"}`}>{fit(n.name, 24)}</text>
                <text x="10" y="37" className="fill-muted-foreground text-[11px]">
                  {(isCatalogKind(n.kind) ? KIND_LABEL[n.kind] : cap(n.kind))}{n.target ? " · target" : n.lifecycle ? ` · ${cap(n.lifecycle)}` : ""}{n.ghost ? " · data flow end" : ""}
                </text>
                {n.gap && <text x={NODE_W - 10} y="37" textAnchor="end" className="fill-gap-foreground text-[11px] font-semibold">⚠ owner</text>}
              </g>
            );
          })}
          {geometry.filter(({ e }) => e.style === "flow").map(({ e, mx, my }) => {
            const f = flowById.get(e.key);
            return (
              <g key={`p-${e.key}`} transform={`translate(${mx - 70} ${my - 12})`}>
                <rect width="140" height="24" rx="12" strokeWidth="1.5" strokeDasharray="4 3" className="fill-card stroke-accent-line" />
                <text x="70" y="16" textAnchor="middle" className="fill-foreground text-[11px]">{fit(f?.name ?? "data flow", 20)}</text>
                {e.verify && <Badge x={132} y={0} />}
              </g>
            );
          })}
          {geometry.filter(({ e }) => e.style === "depends").map(({ e, mx, my }) => (
            <g key={`b-${e.key}`}>
              {e.verify && <Badge x={mx} y={my} />}
              {e.cycle && <text x={mx + (e.verify ? 14 : 0)} y={my + 4} className="fill-foreground text-[13px] font-semibold">↻</text>}
            </g>
          ))}
        </svg>
      </div>
      <Legend prefix={p} />
      {onShowTable && (
        <p className="mt-2 text-sm text-muted-foreground">
          The graph is a picture. To pick a record and read its path, use the table.{" "}
          <Button variant="outline" size="sm" onClick={onShowTable}>Show table</Button>
        </p>
      )}
    </div>
  );
}

/** "?" in a circle: Needs verification. Shape and glyph, not colour alone. */
function Badge({ x, y }: { x: number; y: number }) {
  return (
    <g transform={`translate(${x} ${y})`}>
      <circle r="9" strokeWidth="1.5" className="fill-state-pending stroke-state-pending-border" />
      <text y="4.5" textAnchor="middle" className="fill-state-pending-foreground text-[12px] font-bold">?</text>
    </g>
  );
}

function Legend({ prefix }: { prefix: string }) {
  const swatch = (children: React.ReactNode) => <svg width="44" height="14" viewBox="0 0 44 14" aria-hidden="true" className="shrink-0">{children}</svg>;
  return (
    <ul aria-label="Legend" className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-sm">
      <li className="flex items-center gap-2">{swatch(<path d="M2 7h34" strokeWidth="1.8" className="stroke-foreground" markerEnd={`url(#${prefix}arrow-solid)`} />)}Depends on, or another dependency type (the arrow points to what is depended on)</li>
      <li className="flex items-center gap-2">{swatch(<path d="M2 7h34" strokeWidth="1.8" strokeDasharray="7 5" className="stroke-accent-line" />)}Data flow, not operational</li>
      <li className="flex items-center gap-2">{swatch(<path d="M2 7h34" strokeWidth="2" strokeDasharray="1 5" strokeLinecap="round" className="stroke-muted-foreground" />)}Implements</li>
      <li className="flex items-center gap-2"><span aria-hidden="true" className="grid h-[18px] w-[18px] place-items-center rounded-full border-[1.5px] border-state-pending-border bg-state-pending text-xs font-bold text-state-pending-foreground">?</span>Needs verification</li>
      <li className="flex items-center gap-2"><span aria-hidden="true" className="font-semibold">↻</span>Closes a loop: the link leads back to a record already on the path</li>
    </ul>
  );
}
