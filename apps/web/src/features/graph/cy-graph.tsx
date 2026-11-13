/** The Cytoscape canvas shared by a record's Map tab and the dependency explorer. Loaded on first use
 *  (`graph-loader.tsx`), so Cytoscape stays out of the first bundle.
 *
 *  - Colours are tokens: read from the CSS custom properties on <html> when the graph is drawn, and read again when the
 *    theme changes (`data-theme`), so the canvas follows Light and Dark like the rest of the page. No colour is written
 *    here.
 *  - Meaning never rests on colour: archived and retired nodes have a dashed border and the word in their label, links
 *    differ by line pattern, and the legend beside each graph says what each pattern means.
 *  - No animated layout or viewport change anywhere, so prefers-reduced-motion has nothing to stop.
 *  - The canvas is `role="application"` with `aria-roledescription="graph"`, a name, the caller's summary and the key
 *    instructions (both `hidden` elements, read only through aria-describedby): it takes the arrow keys itself, so a
 *    screen reader passes them through instead of browsing. The arrows move a focus ring between nodes (announced in a
 *    live region) and Enter selects one, the same as clicking it. The table beside it is the complete equivalent.
 *  - In development builds (the e2e suite runs against one) the Cytoscape instance is on `window.__softwardenGraph`, so
 *    tests read what was drawn without Cytoscape's private fields; production builds leave it out. */
import { useEffect, useId, useRef, useState } from "react";
import cytoscape, { type Core, type ElementDefinition, type NodeSingular, type StylesheetJson } from "cytoscape";
import { Button } from "@/components/ui/button";

export type GraphNode = {
  id: string;
  /** Drawn on the node; "\n" starts a second line. */
  label: string;
  /** Read out when the keyboard focus ring reaches the node. */
  spoken: string;
  classes?: string;
  /** For the preset layout. */
  position?: { x: number; y: number };
  /** For the concentric layout: 0 in the middle. */
  level?: number;
  /** Clicking or Enter selects it (false for decorations such as a data flow's label node). */
  selectable?: boolean;
};
export type GraphEdge = { id: string; source: string; target: string; label?: string; classes?: string };

export type GraphProps = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  layout: "concentric" | "preset";
  /** The picture's accessible name, and the ID of an element outside it that describes it. */
  label: string;
  describedBy?: string;
  selected?: string | null;
  onSelect?: (id: string) => void;
  height?: number;
  /** Shown instead when the graph's code can't be loaded (graph-loader.tsx); ignored here. */
  fallback?: React.ReactNode;
  /** Shown after the canvas only when it loaded (its legend and help), never beside the fallback. */
  extras?: React.ReactNode;
  /** The last sentence of the hidden key instructions: where the equivalent table is ("The table beside it lists every
   *  connection."), since that differs between the map and the explorer. */
  tableNote?: string;
};

declare global {
  interface Window { __softwardenGraph?: Core }
}

/** The token values the canvas needs, read from <html>. */
function palette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(`--${name}`).trim();
  return {
    card: v("card"), foreground: v("foreground"), muted: v("muted"), mutedForeground: v("muted-foreground"),
    border: v("border-strong"), accent: v("accent"), accentLine: v("accent-line"), ring: v("ring"),
    pending: v("state-pending"), pendingBorder: v("state-pending-border"), pendingForeground: v("state-pending-foreground"),
    overdue: v("state-overdue"),
    application: v("kind-application"), component: v("kind-component"), integration: v("kind-integration"), agent: v("kind-agent"),
    font: getComputedStyle(document.body).fontFamily,
  };
}

function stylesheet(): StylesheetJson {
  const c = palette();
  return [
    {
      selector: "node",
      style: {
        shape: "round-rectangle", width: 168, height: 46, "background-color": c.card, "border-width": 1.5, "border-color": c.border,
        "border-style": "solid", label: "data(label)", color: c.foreground, "font-family": c.font, "font-size": 12,
        "text-wrap": "wrap", "text-max-width": "156px", "text-valign": "center", "text-halign": "center", "line-height": 1.3,
      },
    },
    { selector: "node.kind-application", style: { "background-color": c.application } },
    { selector: "node.kind-component", style: { "background-color": c.component } },
    { selector: "node.kind-ai_agent", style: { "background-color": c.agent } },
    { selector: "node.kind-integration", style: { "background-color": c.integration } },
    { selector: "node.kind-instance", style: { shape: "rectangle" } },
    { selector: "node.kind-discovery_source", style: { shape: "cut-rectangle" } },
    { selector: "node.kind-contract", style: { shape: "tag" } },
    { selector: "node.kind-license_pool", style: { shape: "hexagon" } },
    // Archived and retired: a dashed border (and the word in the label), muted fill.
    { selector: "node.flagged", style: { "border-style": "dashed", "border-width": 2, "background-color": c.muted, color: c.mutedForeground } },
    { selector: "node.ghost", style: { "border-style": "dotted", "border-width": 2, "background-color": c.card, color: c.mutedForeground } },
    { selector: "node.root", style: { "border-width": 3, "border-color": c.accentLine, "background-color": c.accent } },
    // A data flow's name in the explorer: a dashed pill on the flow.
    { selector: "node.flow", style: { width: 140, height: 26, shape: "round-rectangle", "border-style": "dashed", "border-color": c.accentLine, "background-color": c.card, "font-size": 11 } },
    { selector: "node.verify", style: { "border-color": c.pendingBorder } },
    {
      selector: "edge",
      style: {
        width: 1.8, "line-color": c.foreground, "curve-style": "bezier", "target-arrow-shape": "triangle", "target-arrow-color": c.foreground,
        "arrow-scale": 1.1, label: "data(label)", "font-family": c.font, "font-size": 10, color: c.foreground,
        "text-background-color": c.card, "text-background-opacity": 1, "text-background-padding": "2px", "text-rotation": "none",
      },
    },
    // Other relationship types (implements, replaces, added ones): dotted, an open arrow.
    { selector: "edge.relationship, edge.implements", style: { "line-style": "dotted", "line-color": c.mutedForeground, "target-arrow-color": c.mutedForeground, "target-arrow-shape": "vee", width: 2 } },
    { selector: "edge.implements", style: { "target-arrow-shape": "none" } },
    // Data flows and integrations: dashed line blue, a hollow arrow; both ends for two-way.
    { selector: "edge.integration, edge.flow", style: { "line-style": "dashed", "line-dash-pattern": [7, 5], "line-color": c.accentLine, "target-arrow-color": c.accentLine, "target-arrow-fill": "hollow" } },
    { selector: "edge.two-way", style: { "source-arrow-shape": "triangle", "source-arrow-color": c.accentLine, "source-arrow-fill": "hollow" } },
    { selector: "edge.instance", style: { width: 1.5, "line-color": c.mutedForeground, "target-arrow-shape": "none" } },
    { selector: "edge.discovery", style: { width: 1.5, "line-style": "dashed", "line-dash-pattern": [2, 4], "line-color": c.mutedForeground, "target-arrow-shape": "none" } },
    // An archived or retired integration: sparse dashes, and the word in its label.
    { selector: "edge.flagged", style: { "line-style": "dashed", "line-dash-pattern": [3, 7], color: c.mutedForeground } },
    // A link that closes a loop: dash-dot, heavier, and "closes a loop" in its label.
    { selector: "edge.cycle", style: { "line-style": "dashed", "line-dash-pattern": [12, 4, 2, 4], width: 2.6, "curve-style": "unbundled-bezier", "control-point-distances": [-70], "control-point-weights": [0.5] } },
    { selector: "edge.arc", style: { "curve-style": "unbundled-bezier", "control-point-distances": [-90], "control-point-weights": [0.5] } },
    { selector: "edge.verify", style: { "text-background-color": c.pending, color: c.pendingForeground, "text-border-color": c.pendingBorder, "text-border-width": 1, "text-border-opacity": 1 } },
    { selector: "edge.no-arrow", style: { "target-arrow-shape": "none" } },
    // The data-flow map: a flow's width is its classification's weight (0 unclassified to 4 restricted); a
    // risky flow is drawn in the overdue tone (its label also starts with "!"), and a record receiving one gets a
    // heavy border in that tone (its label also says "! at risk").
    { selector: "edge.w0", style: { width: 1.5 } },
    { selector: "edge.w1", style: { width: 2 } },
    { selector: "edge.w2", style: { width: 3 } },
    { selector: "edge.w3", style: { width: 4.5 } },
    { selector: "edge.w4", style: { width: 6 } },
    { selector: "edge.risky", style: { "line-color": c.overdue, "target-arrow-color": c.overdue, "source-arrow-color": c.overdue } },
    { selector: "node.risky", style: { "border-color": c.overdue, "border-width": 3 } },
    // Selection and the keyboard ring last, so they show over the weights and the risky tone: a picked flow is 1.5 px
    // wider than its weight.
    { selector: "node.picked", style: { "border-width": 4, "border-style": "double", "border-color": c.foreground } },
    { selector: "node.kbd", style: { "outline-width": 2, "outline-color": c.ring, "outline-offset": 4, "outline-style": "solid" } },
    { selector: "edge.picked", style: { width: 3.2 } },
    { selector: "edge.w0.picked", style: { width: 3 } },
    { selector: "edge.w1.picked", style: { width: 3.5 } },
    { selector: "edge.w2.picked", style: { width: 4.5 } },
    { selector: "edge.w3.picked", style: { width: 6 } },
    { selector: "edge.w4.picked", style: { width: 7.5 } },
  ];
}

const SPACING = { padding: 24, animate: false } as const;

export function CyGraph({ nodes, edges, layout, label, describedBy, selected, onSelect, height = 448, extras, tableNote }: GraphProps) {
  const keysId = useId();
  const box = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const select = useRef(onSelect);
  select.current = onSelect;
  const [ring, setRing] = useState<string | null>(null);
  const [spoken, setSpoken] = useState("");
  const order = nodes.filter((n) => n.selectable !== false);

  // One Cytoscape instance per set of elements; no animation anywhere.
  useEffect(() => {
    if (!box.current) return;
    const elements: ElementDefinition[] = [
      ...nodes.map((n) => ({ group: "nodes" as const, data: { id: n.id, label: n.label, level: n.level ?? 0, selectable: n.selectable !== false }, classes: n.classes, position: n.position })),
      ...edges.map((e) => ({ group: "edges" as const, data: { id: e.id, source: e.source, target: e.target, label: e.label ?? "" }, classes: e.classes })),
    ];
    const maxLevel = Math.max(0, ...nodes.map((n) => n.level ?? 0));
    const cy = cytoscape({
      container: box.current,
      elements,
      style: stylesheet(),
      layout: layout === "preset"
        ? { name: "preset", fit: true, ...SPACING }
        : { name: "concentric", fit: true, concentric: (n: NodeSingular) => maxLevel + 1 - (n.data("level") as number), levelWidth: () => 1, minNodeSpacing: 36, avoidOverlap: true, startAngle: (3 / 2) * Math.PI, ...SPACING },
      minZoom: 0.25,
      maxZoom: 2.5,
      userZoomingEnabled: false, // the page scrolls under the wheel; zoom with the buttons
      boxSelectionEnabled: false,
      autounselectify: true, // selection is React's, shown with the `picked` class
    });
    cy.on("tap", "node", (e) => {
      const node = e.target as NodeSingular;
      if (node.data("selectable")) select.current?.(node.id());
    });
    cyRef.current = cy;
    if (import.meta.env.DEV) window.__softwardenGraph = cy;
    // Follow the theme: restyle from the tokens whenever <html data-theme> changes.
    const observer = new MutationObserver(() => cy.style(stylesheet()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      observer.disconnect();
      if (import.meta.env.DEV && window.__softwardenGraph === cy) delete window.__softwardenGraph;
      cy.destroy();
      cyRef.current = null;
    };
  }, [nodes, edges, layout]);

  // The selection, drawn: the node and its links.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().removeClass("picked");
    if (!selected) return;
    const node = cy.getElementById(selected);
    if (node.nonempty()) node.addClass("picked").connectedEdges().addClass("picked");
  }, [selected, nodes, edges]);

  // The keyboard focus ring, kept in view without animation.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.nodes().removeClass("kbd");
    if (!ring) return;
    const node = cy.getElementById(ring);
    if (node.empty()) return;
    node.addClass("kbd");
    const p = node.renderedPosition();
    if (p.x < 0 || p.y < 0 || p.x > cy.width() || p.y > cy.height()) cy.center(node);
  }, [ring, nodes, edges]);

  function onKey(e: React.KeyboardEvent) {
    if (!order.length) return;
    const at = order.findIndex((n) => n.id === ring);
    let to = -1;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") to = at < 0 ? 0 : (at + 1) % order.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") to = at < 0 ? order.length - 1 : (at - 1 + order.length) % order.length;
    else if (e.key === "Home") to = 0;
    else if (e.key === "End") to = order.length - 1;
    else if ((e.key === "Enter" || e.key === " ") && at >= 0) {
      e.preventDefault();
      onSelect?.(order[at].id);
      setSpoken(`Selected ${order[at].spoken}.`);
      return;
    } else if (e.key === "Escape" && ring) {
      setRing(null);
      setSpoken("");
      return;
    }
    if (to < 0) return;
    e.preventDefault();
    setRing(order[to].id);
    setSpoken(`${order[to].spoken}. ${to + 1} of ${order.length}. Enter selects it.`);
  }

  const zoom = (factor: number) => {
    const cy = cyRef.current;
    if (cy) cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  };
  return (
    <div className="print:hidden">
      <div className="mb-2 flex justify-end gap-1">
        <Button type="button" size="sm" variant="outline" onClick={() => zoom(1.25)}>Zoom in</Button>
        <Button type="button" size="sm" variant="outline" onClick={() => zoom(0.8)}>Zoom out</Button>
        <Button type="button" size="sm" variant="outline" onClick={() => cyRef.current?.fit(undefined, SPACING.padding)}>Fit</Button>
      </div>
      <p id={keysId} hidden>
        Arrow keys move between the items, Home and End go to the first and last, Enter or Space selects one, Escape clears the focus ring
        and Tab leaves the picture.{tableNote ? ` ${tableNote}` : ""}
      </p>
      <div ref={box} role="application" aria-roledescription="graph" aria-label={label} aria-describedby={[describedBy, keysId].filter(Boolean).join(" ")} tabIndex={0} onKeyDown={onKey}
        onBlur={() => setRing(null)} data-testid="graph-canvas" style={{ height }} className="w-full rounded-lg border bg-card" />
      <p className="sr-only" aria-live="polite">{spoken}</p>
      {extras}
    </div>
  );
}

export default CyGraph;
