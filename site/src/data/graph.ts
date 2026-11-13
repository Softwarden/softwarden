/**
 * The dependency scenario from the Contoso demo seed (apps/api/catalog/management/commands/seed_demo.py, LINKS and
 * INTEGRATIONS). One source for the static SVG, the text equivalent and the three.js scene.
 * Coordinates are in a 840 x 340 box, y down (SVG convention).
 */
export type NodeId = 'workday' | 'export' | 'ledger' | 'dashboard' | 'budget' | 'zoom';

export interface GraphNode {
  id: NodeId;
  name: string;
  kind: string;
  x: number;
  y: number;
}

export const NODE_W = 176;
export const NODE_H = 60;
export const VIEW_W = 840;
export const VIEW_H = 340;

export const NODES: GraphNode[] = [
  { id: 'workday', name: 'Workday HR', kind: 'Application', x: 16, y: 96 },
  { id: 'export', name: 'Nightly Payroll Export', kind: 'Component, script', x: 232, y: 96 },
  { id: 'ledger', name: 'Finance Ledger', kind: 'Application', x: 448, y: 96 },
  { id: 'dashboard', name: 'Executive Dashboard', kind: 'Application', x: 664, y: 96 },
  { id: 'zoom', name: 'Zoom Meetings', kind: 'Application', x: 232, y: 236 },
  { id: 'budget', name: 'Budget Planner', kind: 'Application', x: 448, y: 236 },
];

export const node = (id: NodeId) => NODES.find((n) => n.id === id)!;

/** A "depends on" link: `dependent` depends on `provider`. Drawn from the provider to the dependent (the way impact travels). */
export interface DependencyEdge {
  id: string;
  type: 'dependency';
  provider: NodeId;
  dependent: NodeId;
  purpose: string;
  points: [number, number][];
}

/** An integration: data moves from `from` to `to`. Not an operational dependency. */
export interface FlowEdge {
  id: string;
  type: 'flow';
  from: NodeId;
  to: NodeId;
  name: string;
  /** Label on the picture. */
  short: string;
  detail: string;
  points: [number, number][];
  labelAt: [number, number];
}

export const DEPENDENCIES: DependencyEdge[] = [
  { id: 'd1', type: 'dependency', provider: 'workday', dependent: 'export', purpose: 'Reads approved payroll', points: [[192, 126], [232, 126]] },
  { id: 'd2', type: 'dependency', provider: 'export', dependent: 'ledger', purpose: 'Loads payroll journals from the export', points: [[408, 126], [448, 126]] },
  { id: 'd3', type: 'dependency', provider: 'ledger', dependent: 'dashboard', purpose: 'Reads actuals', points: [[624, 126], [664, 126]] },
  { id: 'd4', type: 'dependency', provider: 'ledger', dependent: 'budget', purpose: 'Reads actuals', points: [[516, 156], [516, 236]] },
  { id: 'd5', type: 'dependency', provider: 'budget', dependent: 'ledger', purpose: 'Reads budget codes', points: [[556, 236], [556, 156]] },
];

export const FLOWS: FlowEdge[] = [
  {
    id: 'f1',
    type: 'flow',
    from: 'workday',
    to: 'ledger',
    name: 'Workday to Finance Ledger payroll feed',
    short: 'Payroll feed',
    detail: 'SFTP, nightly; implemented by Nightly Payroll Export',
    points: [[104, 96], [104, 52], [536, 52], [536, 96]],
    labelAt: [320, 42],
  },
  {
    id: 'f2',
    type: 'flow',
    from: 'workday',
    to: 'zoom',
    name: 'Workday to Zoom user provisioning',
    short: 'User provisioning',
    detail: 'SCIM, continuous',
    points: [[104, 156], [104, 266], [232, 266]],
    labelAt: [114, 257],
  },
];

export interface Affected {
  id: NodeId;
  steps: number;
  /** Names of the records between the selected one and this one. */
  through: NodeId[];
}

export interface Impact {
  selected: NodeId;
  affected: Affected[];
  /** Dependency edges walked from the selected record (the affected set's paths). */
  treeEdges: string[];
  /** Dependency edges that lead back to a record already on the path. Marked, not followed. */
  loopEdges: string[];
  flowsOut: FlowEdge[];
  flowsIn: FlowEdge[];
  dependsOn: NodeId[];
}

/** Everything that depends on `id`, at any depth, the way the app's dependency explorer walks it. */
export function impactOf(id: NodeId): Impact {
  const affected: Affected[] = [];
  const treeEdges: string[] = [];
  const loopEdges: string[] = [];
  const seen = new Set<NodeId>([id]);
  const pathTo = new Map<NodeId, NodeId[]>([[id, []]]);
  const queue: NodeId[] = [id];
  while (queue.length) {
    const cur = queue.shift()!;
    for (const e of DEPENDENCIES.filter((d) => d.provider === cur)) {
      const curPath = pathTo.get(cur)!;
      if (e.dependent === id || curPath.includes(e.dependent)) {
        loopEdges.push(e.id);
        continue;
      }
      if (seen.has(e.dependent)) continue;
      seen.add(e.dependent);
      const through = cur === id ? [] : [...curPath, cur].filter((n) => n !== id);
      pathTo.set(e.dependent, [...curPath, cur]);
      affected.push({ id: e.dependent, steps: through.length + 1, through });
      treeEdges.push(e.id);
      queue.push(e.dependent);
    }
  }
  // An edge between two affected records that wasn't walked (it points back up the path) closes a loop too.
  for (const e of DEPENDENCIES) {
    if (treeEdges.includes(e.id) || loopEdges.includes(e.id)) continue;
    if (seen.has(e.provider) && seen.has(e.dependent)) loopEdges.push(e.id);
  }
  return {
    selected: id,
    affected,
    treeEdges,
    loopEdges,
    flowsOut: FLOWS.filter((f) => f.from === id),
    flowsIn: FLOWS.filter((f) => f.to === id),
    dependsOn: DEPENDENCIES.filter((d) => d.dependent === id).map((d) => d.provider),
  };
}

export const stepsText = (n: number) => (n === 1 ? 'directly' : `${n} steps away`);
