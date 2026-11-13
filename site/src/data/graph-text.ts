/**
 * Words and geometry helpers for the dependency map, used by the server render and the browser alike.
 * The HTML built here only ever contains the constant names in graph.ts, never user input.
 */
import { DEPENDENCIES, FLOWS, NODES, impactOf, node, stepsText, type NodeId } from './graph';

const kindClass = (kind: string) => (kind.startsWith('Component') ? 'kind-component' : 'kind-application');

export function renderImpact(id: NodeId): string {
  const imp = impactOf(id);
  const n = node(id);
  const count = imp.affected.length;
  const parts: string[] = [];
  parts.push(`<p class="impact-title">If <strong>${n.name}</strong> changes</p>`);

  const sum: string[] = [];
  sum.push(
    count === 0
      ? 'Nothing in this map depends on it.'
      : `${count} ${count === 1 ? 'record depends' : 'records depend'} on it.`,
  );
  if (imp.loopEdges.length) sum.push('One link closes a loop and is marked, not followed again.');
  parts.push(`<p class="impact-sum">${sum.join(' ')}</p>`);

  if (count) {
    parts.push('<ol class="impact-list">');
    for (const a of imp.affected) {
      const an = node(a.id);
      const via = a.through.length ? `through ${a.through.map((t) => node(t).name).join(', then ')}` : 'depends on it directly';
      const loop = DEPENDENCIES.some((d) => imp.loopEdges.includes(d.id) && d.provider === a.id)
        ? ' <span class="chip pending"><span class="chip-glyph" aria-hidden="true">↻</span>Closes a loop</span>'
        : '';
      parts.push(
        `<li><span class="impact-name"><strong>${an.name}</strong> <span class="kind ${kindClass(an.kind)}">${an.kind}</span>${loop}</span><span class="impact-via">${stepsText(a.steps) === 'directly' ? via : `${stepsText(a.steps)}, ${via}`}</span></li>`,
      );
    }
    parts.push('</ol>');
  }

  if (imp.flowsOut.length || imp.flowsIn.length) {
    parts.push('<p class="impact-sub">Data flows, not operational</p><ul class="impact-flows">');
    for (const f of imp.flowsOut) parts.push(`<li>Sends to <strong>${node(f.to).name}</strong>: ${f.short.toLowerCase()} <span class="muted">(${f.detail})</span></li>`);
    for (const f of imp.flowsIn) parts.push(`<li>Receives from <strong>${node(f.from).name}</strong>: ${f.short.toLowerCase()} <span class="muted">(${f.detail})</span></li>`);
    parts.push('</ul>');
  }

  const deps = imp.dependsOn.map((d) => node(d).name);
  parts.push(
    `<p class="impact-foot">${n.name} ${deps.length ? `depends on ${deps.join(' and ')}.` : 'depends on nothing in this map.'}</p>`,
  );
  return parts.join('');
}

export const dependentCount = (id: NodeId) => impactOf(id).affected.length;

/** Every link in words: the complete text equivalent of the picture. */
export function allLinksInWords(): string[] {
  const lines = DEPENDENCIES.map(
    (d) => `${node(d.dependent).name} depends on ${node(d.provider).name} (${d.purpose.toLowerCase()}).`,
  );
  lines.push('Finance Ledger and Budget Planner depend on each other, so the explorer marks one of the two links as closing a loop.');
  for (const f of FLOWS) {
    lines.push(`${f.name}: data from ${node(f.from).name} to ${node(f.to).name} (${f.detail}). A data flow, not an operational dependency.`);
  }
  return lines;
}

/** Arrowhead for the end of a polyline, and the line trimmed so it stops at the arrow's base. */
export function arrowFor(points: [number, number][], size = 9, half = 4.5) {
  const [x1, y1] = points[points.length - 2];
  const [x2, y2] = points[points.length - 1];
  const len = Math.hypot(x2 - x1, y2 - y1) || 1;
  const ux = (x2 - x1) / len;
  const uy = (y2 - y1) / len;
  const bx = x2 - ux * size;
  const by = y2 - uy * size;
  const head: [number, number][] = [
    [x2, y2],
    [bx - uy * half, by + ux * half],
    [bx + uy * half, by - ux * half],
  ];
  const trimmed = points.slice(0, -1).concat([[x2 - ux * (size - 1), y2 - uy * (size - 1)]]) as [number, number][];
  return { head, trimmed };
}

export const LOOP_LABELS: Record<string, { x: number; y: number; anchor: 'start' | 'end' }> = {
  d4: { x: 506, y: 200, anchor: 'end' },
  d5: { x: 566, y: 200, anchor: 'start' },
};

export { NODES, DEPENDENCIES, FLOWS };
