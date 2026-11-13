/**
 * The dependency map in three.js: the same records and links as the static SVG, laid out at the same coordinates,
 * with slight depth. Records that depend on the selected one come forward; a marker travels along each walked link,
 * one step after another, so the order in which a change spreads is visible. The camera drifts gently.
 *
 * Loaded only through a dynamic import from depmap.ts. Colours are read from the CSS tokens and re-read on every
 * theme change. Meaning stays in line pattern and words (the node texts say "Selected" and "Affected, N steps away").
 */
import {
  BufferGeometry,
  CanvasTexture,
  Color,
  DoubleSide,
  Float32BufferAttribute,
  Group,
  LinearFilter,
  Mesh,
  MeshBasicMaterial,
  PerspectiveCamera,
  PlaneGeometry,
  Raycaster,
  SRGBColorSpace,
  Scene,
  Vector2,
  WebGLRenderer,
} from 'three';
import { DEPENDENCIES, FLOWS, NODES, NODE_H, NODE_W, VIEW_H, VIEW_W, stepsText, type Impact, type NodeId } from '../data/graph';
import { LOOP_LABELS, arrowFor } from '../data/graph-text';
import type { SceneCallbacks, SceneHandle } from './depmap';

type Pt = [number, number];

interface Palette {
  card: string;
  text: string;
  muted: string;
  line: string;
  accentLine: string;
  accent: string;
  accentFg: string;
  loop: string;
  pending: string;
  pendingFg: string;
  pendingBorder: string;
  border: string;
}

function readPalette(): Palette {
  const cs = getComputedStyle(document.documentElement);
  const v = (name: string) => cs.getPropertyValue(name).trim() || '#888888';
  return {
    card: v('--card'),
    text: v('--text-strong'),
    muted: v('--muted-foreground'),
    line: v('--graph-line'),
    accentLine: v('--accent-line'),
    accent: v('--accent'),
    accentFg: v('--accent-foreground'),
    loop: v('--chart-2'),
    pending: v('--state-pending'),
    pendingFg: v('--state-pending-foreground'),
    pendingBorder: v('--state-pending-border'),
    border: v('--border-strong'),
  };
}

const FONT = '"Inter Variable", "Segoe UI", system-ui, sans-serif';
const TEX_SCALE = 4;

/** SVG coordinates (y down, origin top left) to world coordinates (y up, origin centre). */
const toWorld = ([x, y]: Pt): Pt => [x - VIEW_W / 2, VIEW_H / 2 - y];

function roundedRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

type NodeMode = 'selected' | 'affected' | 'normal';

function drawNode(canvas: HTMLCanvasElement, name: string, sub: string, mode: NodeMode, p: Palette) {
  const S = TEX_SCALE;
  canvas.width = NODE_W * S;
  canvas.height = NODE_H * S;
  const ctx = canvas.getContext('2d')!;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const bw = mode === 'selected' ? 2 : mode === 'affected' ? 1.5 : 1;
  roundedRect(ctx, (bw / 2) * S, (bw / 2) * S, (NODE_W - bw) * S, (NODE_H - bw) * S, 6 * S);
  ctx.fillStyle = mode === 'selected' ? p.accent : mode === 'affected' ? p.pending : p.card;
  ctx.fill();
  ctx.lineWidth = bw * S;
  ctx.strokeStyle = mode === 'selected' ? p.accentLine : mode === 'affected' ? p.pendingBorder : p.border;
  ctx.stroke();
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = p.text;
  ctx.font = `600 ${14 * S}px ${FONT}`;
  ctx.fillText(name, 12 * S, 25 * S);
  ctx.fillStyle = mode === 'selected' ? p.accentFg : mode === 'affected' ? p.pendingFg : p.muted;
  ctx.font = `400 ${12.5 * S}px ${FONT}`;
  ctx.fillText(sub, 12 * S, 44 * S);
}

/** A text label as a textured plane. */
function makeLabel(text: string, color: string) {
  const S = TEX_SCALE;
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d')!;
  ctx.font = `400 ${12 * S}px ${FONT}`;
  const w = Math.ceil(ctx.measureText(text).width / S) + 4;
  const h = 16;
  canvas.width = w * S;
  canvas.height = h * S;
  ctx.font = `400 ${12 * S}px ${FONT}`;
  ctx.fillStyle = color;
  ctx.textBaseline = 'middle';
  ctx.fillText(text, 2 * S, (h / 2) * S);
  const tex = new CanvasTexture(canvas);
  tex.colorSpace = SRGBColorSpace;
  tex.minFilter = LinearFilter;
  tex.generateMipmaps = false;
  const mesh = new Mesh(
    new PlaneGeometry(w, h),
    new MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false, side: DoubleSide }),
  );
  return { mesh, width: w };
}

/** Quads along a polyline, cut to a dash pattern (null: solid). Square caps cover the corners of solid lines. */
function lineGeometry(points: Pt[], width: number, pattern: number[] | null): BufferGeometry {
  const pos: number[] = [];
  const h = width / 2;
  const quad = (ax: number, ay: number, bx: number, by: number) => {
    const len = Math.hypot(bx - ax, by - ay) || 1;
    const ux = (bx - ax) / len;
    const uy = (by - ay) / len;
    const nx = -uy * h;
    const ny = ux * h;
    pos.push(ax + nx, ay + ny, 0, ax - nx, ay - ny, 0, bx - nx, by - ny, 0);
    pos.push(ax + nx, ay + ny, 0, bx - nx, by - ny, 0, bx + nx, by + ny, 0);
  };
  let pi = 0;
  let left = pattern ? pattern[0] : Infinity;
  for (let i = 0; i < points.length - 1; i++) {
    const [ax, ay] = points[i];
    const [bx, by] = points[i + 1];
    const len = Math.hypot(bx - ax, by - ay);
    const ux = (bx - ax) / len;
    const uy = (by - ay) / len;
    if (!pattern) {
      const capA = i === 0 ? 0 : h;
      quad(ax - ux * capA, ay - uy * capA, bx, by);
      continue;
    }
    let t = 0;
    while (t < len) {
      const step = Math.min(left, len - t);
      if (pi % 2 === 0) quad(ax + ux * t, ay + uy * t, ax + ux * (t + step), ay + uy * (t + step));
      t += step;
      left -= step;
      if (left <= 1e-6) {
        pi = (pi + 1) % pattern.length;
        left = pattern[pi];
      }
    }
  }
  const g = new BufferGeometry();
  g.setAttribute('position', new Float32BufferAttribute(pos, 3));
  return g;
}

function triangleGeometry(tri: Pt[]): BufferGeometry {
  const g = new BufferGeometry();
  g.setAttribute('position', new Float32BufferAttribute(tri.flatMap(([x, y]) => [x, y, 0]), 3));
  return g;
}

function pointAlong(points: Pt[], f: number): Pt {
  const segs = points.slice(1).map((p, i) => Math.hypot(p[0] - points[i][0], p[1] - points[i][1]));
  const total = segs.reduce((a, b) => a + b, 0);
  let d = f * total;
  for (let i = 0; i < segs.length; i++) {
    if (d <= segs[i] || i === segs.length - 1) {
      const k = segs[i] ? Math.min(1, d / segs[i]) : 0;
      return [points[i][0] + (points[i + 1][0] - points[i][0]) * k, points[i][1] + (points[i + 1][1] - points[i][1]) * k];
    }
    d -= segs[i];
  }
  return points[points.length - 1];
}

interface NodeView {
  id: NodeId;
  mesh: Mesh<PlaneGeometry, MeshBasicMaterial>;
  canvas: HTMLCanvasElement;
  texture: CanvasTexture;
  z: number;
  targetZ: number;
  opacity: number;
  targetOpacity: number;
  key: string;
}

export async function createScene(frame: HTMLElement, initial: Impact, cb: SceneCallbacks): Promise<SceneHandle> {
  try {
    await document.fonts?.load(`600 14px ${FONT}`);
  } catch {
    /* draw with the fallback font */
  }

  let palette = readPalette();
  let selection = initial;

  const renderer = new WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = SRGBColorSpace;
  renderer.setClearColor(new Color(0x000000), 0);
  const canvas = renderer.domElement;
  canvas.setAttribute('role', 'img');
  canvas.setAttribute(
    'aria-label',
    'Dependency map of the Contoso demo, drawn in 3D. The list of records beside it describes the same records and links.',
  );
  frame.appendChild(canvas);

  const scene = new Scene();
  const camera = new PerspectiveCamera(22, 1, 10, 8000);
  const world = new Group();
  scene.add(world);

  const edgeGroup = new Group();
  const labelGroup = new Group();
  const pulseGroup = new Group();
  world.add(edgeGroup, labelGroup, pulseGroup);

  // Nodes
  const nodes: NodeView[] = NODES.map((n) => {
    const c = document.createElement('canvas');
    const texture = new CanvasTexture(c);
    texture.colorSpace = SRGBColorSpace;
    texture.anisotropy = renderer.capabilities.getMaxAnisotropy();
    const mesh = new Mesh(
      new PlaneGeometry(NODE_W, NODE_H),
      new MeshBasicMaterial({ map: texture, transparent: true, side: DoubleSide, depthWrite: false }),
    );
    const [wx, wy] = toWorld([n.x + NODE_W / 2, n.y + NODE_H / 2]);
    mesh.position.set(wx, wy, 6);
    mesh.userData.id = n.id;
    world.add(mesh);
    return { id: n.id, mesh, canvas: c, texture, z: 6, targetZ: 6, opacity: 1, targetOpacity: 1, key: '' };
  });

  function paintNodes(force = false) {
    for (const v of nodes) {
      const n = NODES.find((x) => x.id === v.id)!;
      const a = selection.affected.find((x) => x.id === v.id);
      const mode: NodeMode = v.id === selection.selected ? 'selected' : a ? 'affected' : 'normal';
      const sub = mode === 'selected' ? '● Selected' : a ? `◐ Affected, ${stepsText(a.steps)}` : n.kind;
      v.targetZ = mode === 'selected' ? 34 : mode === 'affected' ? 12 + 6 * Math.max(0, 4 - a!.steps) : 4;
      v.targetOpacity = mode === 'normal' ? 0.6 : 1;
      const key = `${mode}|${sub}`;
      if (!force && key === v.key) continue;
      v.key = key;
      drawNode(v.canvas, n.name, sub, mode, palette);
      v.texture.needsUpdate = true;
    }
  }

  // Edges, labels and pulses are rebuilt on every selection or theme change: a handful of meshes.
  const disposeGroup = (g: Group) => {
    for (const child of [...g.children]) {
      const m = child as Mesh;
      m.geometry?.dispose();
      const mat = m.material as MeshBasicMaterial | undefined;
      mat?.map?.dispose();
      mat?.dispose();
      g.remove(child);
    }
  };

  interface Pulse {
    mesh: Mesh<PlaneGeometry, MeshBasicMaterial>;
    path: Pt[];
    delay: number;
  }
  let pulses: Pulse[] = [];

  function buildEdges() {
    disposeGroup(edgeGroup);
    disposeGroup(labelGroup);
    disposeGroup(pulseGroup);
    pulses = [];

    const add = (pointsSvg: Pt[], color: string, width: number, pattern: number[] | null, opacity: number) => {
      const { head, trimmed } = arrowFor(pointsSvg);
      const mat = new MeshBasicMaterial({ color: new Color(color), transparent: true, opacity, side: DoubleSide, depthWrite: false });
      edgeGroup.add(new Mesh(lineGeometry(trimmed.map(toWorld), width, pattern), mat));
      edgeGroup.add(new Mesh(triangleGeometry(head.map(toWorld)), mat));
    };

    for (const f of FLOWS) {
      const on = f.from === selection.selected || f.to === selection.selected;
      add(f.points, palette.accentLine, 1.6, [6, 5], on ? 1 : 0.4);
      const { mesh, width } = makeLabel(f.short, palette.muted);
      const [lx, ly] = toWorld(f.labelAt);
      const anchorX = f.id === 'f1' ? lx : lx + width / 2;
      mesh.position.set(anchorX, ly + 4, 1);
      (mesh.material as MeshBasicMaterial).opacity = on ? 1 : 0.5;
      labelGroup.add(mesh);
    }

    for (const d of DEPENDENCIES) {
      const tree = selection.treeEdges.includes(d.id);
      const loop = selection.loopEdges.includes(d.id);
      if (tree) add(d.points, palette.accentLine, 2.4, null, 1);
      else if (loop) add(d.points, palette.loop, 2, [8, 4, 2, 4], 1);
      else add(d.points, palette.line, 1.6, null, 0.45);

      if (loop && LOOP_LABELS[d.id]) {
        const l = LOOP_LABELS[d.id];
        const { mesh, width } = makeLabel('↻ Closes a loop', palette.muted);
        const [lx, ly] = toWorld([l.x, l.y]);
        mesh.position.set(l.anchor === 'end' ? lx - width / 2 : lx + width / 2, ly + 4, 1);
        labelGroup.add(mesh);
      }

      if (tree) {
        const dependent = selection.affected.find((a) => a.id === d.dependent);
        const mesh = new Mesh(
          new PlaneGeometry(7, 7),
          new MeshBasicMaterial({ color: new Color(palette.accentLine), transparent: true, opacity: 0, depthWrite: false }),
        );
        mesh.rotation.z = Math.PI / 4;
        mesh.position.z = 2;
        pulseGroup.add(mesh);
        pulses.push({ mesh, path: arrowFor(d.points).trimmed.map(toWorld), delay: ((dependent?.steps ?? 1) - 1) * 0.55 });
      }
    }
  }

  // Camera framing and gentle drift
  let distance = 1000;
  function resize() {
    const w = frame.clientWidth;
    const h = frame.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    const half = (camera.fov * Math.PI) / 360;
    const needH = (VIEW_H + 24) / 2 / Math.tan(half);
    const needW = (VIEW_W + 24) / 2 / (Math.tan(half) * camera.aspect);
    distance = Math.max(needH, needW) * 1.02;
    camera.updateProjectionMatrix();
  }

  const pointer = new Vector2(0, 0);
  const pointerSmooth = new Vector2(0, 0);
  const ndc = new Vector2();
  const raycaster = new Raycaster();
  let hovered: NodeId | null = null;

  function pick(e: PointerEvent): NodeId | null {
    const r = canvas.getBoundingClientRect();
    ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    const hit = raycaster.intersectObjects(nodes.map((n) => n.mesh), false)[0];
    return hit ? (hit.object.userData.id as NodeId) : null;
  }

  const onMove = (e: PointerEvent) => {
    const r = canvas.getBoundingClientRect();
    pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, ((e.clientY - r.top) / r.height) * 2 - 1);
    const id = pick(e);
    canvas.style.cursor = id ? 'pointer' : 'default';
    if (id !== hovered) {
      hovered = id;
      cb.onHover(id);
    }
  };
  const onLeave = () => {
    pointer.set(0, 0);
    if (hovered) {
      hovered = null;
      cb.onHover(null);
    }
  };
  const onClick = (e: PointerEvent) => {
    const id = pick(e);
    if (id) cb.onPick(id);
  };
  canvas.addEventListener('pointermove', onMove);
  canvas.addEventListener('pointerleave', onLeave);
  canvas.addEventListener('click', onClick as EventListener);

  // Render loop: only while the map is on screen and the tab is visible.
  let raf = 0;
  let last = performance.now();
  let clock = 0;
  let onScreen = true;

  function frameStep(now: number) {
    raf = 0;
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    clock += dt;
    const k = 1 - Math.exp(-dt * 7);

    pointerSmooth.lerp(pointer, 1 - Math.exp(-dt * 3));
    const yaw = Math.sin(clock * 0.13) * 0.05 + pointerSmooth.x * 0.07;
    const pitch = Math.cos(clock * 0.1) * 0.035 - pointerSmooth.y * 0.05;
    camera.position.set(
      Math.sin(yaw) * distance,
      Math.sin(pitch) * distance,
      Math.cos(yaw) * Math.cos(pitch) * distance,
    );
    camera.lookAt(0, 0, 0);

    for (const v of nodes) {
      v.z += (v.targetZ - v.z) * k;
      v.opacity += (v.targetOpacity - v.opacity) * k;
      v.mesh.position.z = v.z;
      v.mesh.material.opacity = v.opacity;
    }

    const cycle = 3.2;
    for (const p of pulses) {
      const t = ((clock - p.delay) % cycle + cycle) % cycle;
      const f = t / 1.1;
      if (f >= 0 && f <= 1) {
        const [x, y] = pointAlong(p.path, f);
        p.mesh.position.x = x;
        p.mesh.position.y = y;
        p.mesh.material.opacity = Math.sin(Math.PI * f);
      } else {
        p.mesh.material.opacity = 0;
      }
    }

    renderer.render(scene, camera);
    schedule();
  }

  function schedule() {
    if (!raf && onScreen && document.visibilityState === 'visible') {
      last = performance.now();
      raf = requestAnimationFrame(frameStep);
    }
  }

  const io = new IntersectionObserver((entries) => {
    onScreen = entries.some((e) => e.isIntersecting);
    schedule();
  });
  io.observe(frame);
  const onVisibility = () => schedule();
  document.addEventListener('visibilitychange', onVisibility);
  const ro = new ResizeObserver(() => {
    resize();
    renderer.render(scene, camera);
  });
  ro.observe(frame);

  const onTheme = () => {
    palette = readPalette();
    paintNodes(true);
    buildEdges();
  };
  window.addEventListener('softwarden:theme', onTheme);

  resize();
  paintNodes(true);
  buildEdges();
  // Settle nodes at their starting depth without animating in.
  for (const v of nodes) {
    v.z = v.targetZ;
    v.opacity = v.targetOpacity;
  }
  frameStep(performance.now());

  return {
    setSelection(imp: Impact) {
      selection = imp;
      paintNodes();
      buildEdges();
      schedule();
    },
    destroy() {
      if (raf) cancelAnimationFrame(raf);
      io.disconnect();
      ro.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('softwarden:theme', onTheme);
      canvas.removeEventListener('pointermove', onMove);
      canvas.removeEventListener('pointerleave', onLeave);
      canvas.removeEventListener('click', onClick as EventListener);
      disposeGroup(edgeGroup);
      disposeGroup(labelGroup);
      disposeGroup(pulseGroup);
      for (const v of nodes) {
        v.mesh.geometry.dispose();
        v.mesh.material.dispose();
        v.texture.dispose();
      }
      renderer.dispose();
      canvas.remove();
    },
  };
}
