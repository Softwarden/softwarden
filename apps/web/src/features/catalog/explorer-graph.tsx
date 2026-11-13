/** The explorer's Graph view on Cytoscape. The model is the static graph's `layout()` (columns by
 *  distance from the target: dependents left, dependencies right), drawn at preset positions so the picture is stable
 *  and never animates. A data flow is a small dashed pill on its line, so the components implementing it can point at it
 *  with a dotted line, as in the static picture. Links in the response's `cycles` are drawn dash-dot and labelled
 *  "closes a loop". Clicking a record, or Enter on it with the canvas focused, selects it for the path panel. */
import { useMemo } from "react";
import { cap } from "@/api";
import type { Dependencies, DepNode } from "@/features/catalog/explorer";
import { DependencyGraph, describe, layout } from "@/features/catalog/dependency-graph";
import { Graph, GraphLegend, type GraphEdge, type GraphNode } from "@/features/graph/graph-loader";
import { isCatalogKind, KIND_LABEL } from "@/lib/kinds";
import { Button } from "@/components/ui/button";

const COLUMN = 280;
const ROW = 84;
const fit = (text: string, n: number) => (text.length > n ? `${text.slice(0, n - 1)}…` : text);

/** The graph key of a table row: `${direction}:${id}`, or "target" for a loop back to the target. */
export const graphKeyOf = (n: DepNode, targetId: string) => (n.cycle && n.id === targetId ? "target" : `${n.direction}:${n.id}`);

function toElements(data: Dependencies) {
  const { nodes: gnodes, edges: gedges } = layout(data);
  const closing = new Set((data.cycles ?? []).map((c) => c.relationship));
  const perLayer = new Map<number, number>();
  for (const n of gnodes) perLayer.set(n.layer, (perLayer.get(n.layer) ?? 0) + 1);
  const seen = new Map<number, number>();
  const pos = new Map<string, { x: number; y: number }>();
  for (const n of gnodes) {
    const i = seen.get(n.layer) ?? 0;
    seen.set(n.layer, i + 1);
    pos.set(n.key, { x: n.layer * COLUMN, y: (i - ((perLayer.get(n.layer) ?? 1) - 1) / 2) * ROW });
  }
  const layerOf = new Map(gnodes.map((n) => [n.key, n.layer]));
  const nodes: GraphNode[] = gnodes.map((n) => {
    const kind = isCatalogKind(n.kind) ? KIND_LABEL[n.kind] : cap(n.kind);
    const words = [kind, ...(n.target ? ["target"] : n.lifecycle ? [cap(n.lifecycle)] : []), ...(n.archived ? ["Archived"] : []), ...(n.ghost ? ["data flow end"] : [])];
    const second = `${words.join(" · ")}${n.gap ? " · ⚠ owner" : ""}`;
    const flagged = n.lifecycle === "retired" || n.archived;
    return {
      id: n.key, label: `${fit(n.name, 24)}\n${second}`, spoken: `${n.name}, ${words.join(", ")}${n.gap ? ", no technical owner" : ""}`,
      position: pos.get(n.key), selectable: !n.target && !n.ghost,
      classes: [isCatalogKind(n.kind) ? `kind-${n.kind}` : "", n.target ? "root" : "", n.ghost ? "ghost" : "", flagged ? "flagged" : ""].filter(Boolean).join(" "),
    };
  });

  const edges: GraphEdge[] = [];
  const ids = new Set<string>();
  const unique = (id: string) => { let k = id; for (let i = 2; ids.has(k); i++) k = `${id}#${i}`; ids.add(k); return k; };
  const pills = new Map<string, string>(); // flow edge key -> pill node id
  const stacked = new Map<string, number>();
  for (const e of gedges.filter((x) => x.style !== "implements")) {
    const span = Math.abs((layerOf.get(e.to) ?? 0) - (layerOf.get(e.from) ?? 0));
    if (e.style === "flow") {
      // The flow's name on a pill between its ends; the pill is a decoration, not something to select.
      const a = pos.get(e.from)!;
      const b = pos.get(e.to)!;
      const spot = `${Math.round((a.x + b.x) / 2)}:${Math.round((a.y + b.y) / 2)}`;
      const k = stacked.get(spot) ?? 0;
      stacked.set(spot, k + 1);
      const pill = `pill:${e.key}`;
      pills.set(e.key, pill);
      const x = (a.x + b.x) / 2 + (span === 0 ? COLUMN / 2 : 0);
      const y = (a.y + b.y) / 2 + (span === 0 ? 0 : ROW / 2) + k * 30;
      const name = e.label ?? "data flow";
      nodes.push({ id: pill, label: `${e.verify ? "? " : ""}${fit(name, 20)}`, spoken: `${name}, data flow`, position: { x, y }, selectable: false, classes: `flow${e.verify ? " verify" : ""}` });
      edges.push({ id: unique(`${e.key}:a`), source: e.from, target: pill, classes: "flow no-arrow" });
      edges.push({ id: unique(`${e.key}:b`), source: pill, target: e.to, classes: "flow" });
      continue;
    }
    const loop = e.cycle || (!!e.relationship && closing.has(e.relationship));
    const words = [...(e.verify ? ["?"] : []), ...(loop ? ["↻ closes a loop"] : []), ...(e.typeName ? [e.typeName] : [])];
    edges.push({
      id: unique(`${e.key}:${e.from}>${e.to}`), source: e.from, target: e.to, label: words.join(" "),
      classes: ["dependency", e.verify ? "verify" : "", loop ? "cycle" : span !== 1 ? "arc" : ""].filter(Boolean).join(" "),
    });
  }
  for (const e of gedges.filter((x) => x.style === "implements")) {
    const pill = pills.get(e.to);
    if (pill) edges.push({ id: unique(`${e.key}`), source: e.from, target: pill, classes: "implements" });
  }
  const tallest = Math.max(1, ...perLayer.values());
  return { nodes, edges, height: Math.min(640, Math.max(360, tallest * ROW + 160)) };
}

export function ExplorerGraph({ data, selected, onSelect, onShowTable }: {
  data: Dependencies; selected: string | null; onSelect: (graphKey: string) => void; onShowTable: () => void;
}) {
  const { nodes, edges, height } = useMemo(() => toElements(data), [data]);
  const loops = data.cycles?.length ?? 0;
  return (
    <div>
      {/* Read only through aria-describedby (hidden), so it isn't heard twice in reading order. */}
      <p id="cy-graph-desc" hidden>{describe(data)}{loops > 0 && ` ${loops} ${loops === 1 ? "link closes" : "links close"} a loop, drawn dash-dot.`}</p>
      <Graph nodes={nodes} edges={edges} layout="preset" label={`Dependency graph for ${data.target.name}`} describedBy="cy-graph-desc"
        selected={selected} onSelect={onSelect} height={height}
        tableNote="Show table switches to the Table view, which lists the same records with their paths."
        // The chunk didn't load: the static picture with its own legend and Show table, on screen only (the print copy
        // below is what prints, so the graph never prints twice).
        fallback={<div className="print:hidden"><DependencyGraph data={data} variant="fallback" onShowTable={onShowTable} /></div>}
        extras={<>
          <GraphLegend items={[
            { key: "dependency", text: "Depends on, or another dependency type (the arrow points to what is depended on)" },
            { key: "flow", text: "Data flow, not operational" },
            { key: "implements", text: "Implements" },
            { key: "verify", text: "Needs verification" },
            { key: "cycle", text: "Closes a loop: the link leads back to a record already on the path" },
            { key: "flagged-node", text: "Retired or archived (also said in words)" },
          ]} />
          <p className="mt-2 text-sm text-muted-foreground">
            The graph is a picture: click a record, or focus the picture and use the arrow keys and Enter, to read its path. The table lists the same records.{" "}
            <Button variant="outline" size="sm" onClick={onShowTable}>Show table</Button>
          </p>
        </>} />
      {/* A canvas prints as whatever is in view; the printed page gets the whole layered picture instead. */}
      <div className="hidden print:block"><DependencyGraph data={data} variant="print" /></div>
    </div>
  );
}
