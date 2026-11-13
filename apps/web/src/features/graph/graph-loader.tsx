/** Loads the Cytoscape canvas (cy-graph.tsx) on first use, in its own chunk, and the legend that explains its line
 *  patterns. If the chunk can't be loaded (offline, or the app was updated since this page loaded), the caller's
 *  `fallback` shows instead, or a note that the table beside it lists everything. */
import { lazy, Suspense } from "react";
import type { GraphProps } from "@/features/graph/cy-graph";

export type { GraphEdge, GraphNode, GraphProps } from "@/features/graph/cy-graph";

function GraphUnavailable({ fallback }: GraphProps) {
  return (
    <>
      {fallback ?? (
        <p className="rounded-lg border bg-card px-4 py-3" role="status">
          The picture couldn't be loaded. Reload the page to try again; the table lists every connection.
        </p>
      )}
    </>
  );
}

const Loaded = lazy(() => import("@/features/graph/cy-graph")
  .then((m) => ({ default: m.CyGraph }))
  .catch(() => ({ default: GraphUnavailable })));

export function Graph(props: GraphProps) {
  return (
    <Suspense fallback={<div className="grid place-items-center rounded-lg border bg-card text-muted-foreground" style={{ height: props.height ?? 448 }} aria-live="polite">Loading the picture…</div>}>
      <Loaded {...props} />
    </Suspense>
  );
}

/** What each line pattern means, as a swatch plus words (never colour alone). The swatches use the same token colours
 *  and dash patterns as the canvas. */
export type LegendKey = "dependency" | "relationship" | "flow" | "implements" | "instance" | "discovery" | "flagged" | "cycle" | "verify" | "flagged-node";

const LINE: Record<Exclude<LegendKey, "verify" | "flagged-node">, { dash?: string; width?: number; cls: string }> = {
  dependency: { cls: "stroke-foreground" },
  relationship: { dash: "1 4", width: 2, cls: "stroke-muted-foreground" },
  flow: { dash: "7 5", cls: "stroke-accent-line" },
  implements: { dash: "1 5", width: 2, cls: "stroke-muted-foreground" },
  instance: { width: 1.5, cls: "stroke-muted-foreground" },
  discovery: { dash: "2 4", width: 1.5, cls: "stroke-muted-foreground" },
  flagged: { dash: "3 7", cls: "stroke-foreground" },
  cycle: { dash: "12 4 2 4", width: 2.6, cls: "stroke-foreground" },
};

export function GraphLegend({ items }: { items: { key: LegendKey; text: string }[] }) {
  return (
    <ul aria-label="Legend" className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-sm print:hidden">
      {items.map(({ key, text }) => (
        <li key={key} className="flex items-center gap-2">
          {key === "verify" ? (
            <span aria-hidden="true" className="grid h-[18px] min-w-[18px] place-items-center rounded-sm border border-state-pending-border bg-state-pending px-1 text-xs font-bold text-state-pending-foreground">?</span>
          ) : key === "flagged-node" ? (
            <span aria-hidden="true" className="h-[18px] w-9 shrink-0 rounded-sm border-2 border-dashed border-border-strong bg-muted" />
          ) : (
            <svg width="44" height="14" viewBox="0 0 44 14" aria-hidden="true" className="shrink-0">
              <path d="M2 7h40" strokeWidth={LINE[key].width ?? 1.8} strokeDasharray={LINE[key].dash} strokeLinecap={key === "implements" ? "round" : undefined} className={LINE[key].cls} />
            </svg>
          )}
          {key === "cycle" && <span aria-hidden="true" className="font-semibold">↻</span>}
          {text}
        </li>
      ))}
    </ul>
  );
}
