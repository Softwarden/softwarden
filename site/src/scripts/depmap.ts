/**
 * The dependency map's controller. Works on the static SVG on its own; when the section nears the viewport, and only
 * if motion is welcome, the frame is wide enough and WebGL works, it loads the three.js scene (its own chunk) and
 * hands it the same selection. The list beside the map stays the complete text equivalent either way.
 */
import { NODES, impactOf, stepsText, type NodeId, type Impact } from '../data/graph';
import { renderImpact } from '../data/graph-text';

export interface SceneHandle {
  setSelection(imp: Impact): void;
  destroy(): void;
}

export interface SceneCallbacks {
  onHover(id: NodeId | null): void;
  onPick(id: NodeId): void;
}

const MIN_3D_WIDTH = 600;

function webglAvailable(): boolean {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch {
    return false;
  }
}

export function mountDepMap(root: HTMLElement) {
  const svg = root.querySelector<SVGSVGElement>('[data-map-svg]')!;
  const frame = root.querySelector<HTMLElement>('[data-map-frame]')!;
  const panel = root.querySelector<HTMLElement>('[data-impact]')!;
  const hint = root.querySelector<HTMLElement>('[data-map-hint]');
  const buttons = Array.from(root.querySelectorAll<HTMLButtonElement>('[data-select]'));
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  let pinned = (root.dataset.initial as NodeId) ?? 'workday';
  let preview: NodeId | null = null;
  let shown: NodeId | null = null;
  let scene: SceneHandle | null = null;
  let loading = false;

  function paintSvg(imp: Impact) {
    svg.querySelectorAll<SVGGElement>('[data-node]').forEach((g) => {
      const id = g.dataset.node as NodeId;
      const a = imp.affected.find((x) => x.id === id);
      const selected = id === imp.selected;
      g.classList.toggle('is-selected', selected);
      g.classList.toggle('is-affected', !!a);
      g.classList.toggle('is-dim', !selected && !a);
      const sub = g.querySelector('.n-sub');
      if (sub) {
        sub.textContent = selected
          ? '● Selected'
          : a
            ? `◐ Affected, ${stepsText(a.steps)}`
            : NODES.find((n) => n.id === id)!.kind;
      }
    });
    svg.querySelectorAll<SVGGElement>('[data-edge]').forEach((g) => {
      const id = g.dataset.edge!;
      if (g.classList.contains('e-flow')) {
        const on = imp.flowsOut.some((f) => f.id === id) || imp.flowsIn.some((f) => f.id === id);
        g.classList.toggle('is-flow-on', on);
        g.classList.toggle('is-dim', !on);
        return;
      }
      const tree = imp.treeEdges.includes(id);
      const loop = imp.loopEdges.includes(id);
      g.classList.toggle('is-tree', tree);
      g.classList.toggle('is-loop', loop);
      g.classList.toggle('is-dim', !tree && !loop);
    });
    svg.querySelectorAll<SVGTextElement>('[data-loop-label]').forEach((t) => {
      t.setAttribute('visibility', imp.loopEdges.includes(t.dataset.loopLabel!) ? 'visible' : 'hidden');
    });
  }

  function show(id: NodeId) {
    if (id === shown) return;
    shown = id;
    const imp = impactOf(id);
    paintSvg(imp);
    panel.innerHTML = renderImpact(id);
    scene?.setSelection(imp);
  }

  const refresh = () => show(preview ?? pinned);

  function pin(id: NodeId) {
    pinned = id;
    buttons.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.select === id)));
    refresh();
  }

  buttons.forEach((b) => {
    const id = b.dataset.select as NodeId;
    b.addEventListener('click', () => pin(id));
    b.addEventListener('mouseenter', () => {
      preview = id;
      refresh();
    });
    b.addEventListener('mouseleave', () => {
      preview = null;
      refresh();
    });
    b.addEventListener('focus', () => {
      preview = id;
      refresh();
    });
    b.addEventListener('blur', () => {
      preview = null;
      refresh();
    });
  });

  svg.querySelectorAll<SVGGElement>('[data-node]').forEach((g) => {
    const id = g.dataset.node as NodeId;
    g.addEventListener('pointerenter', () => {
      preview = id;
      refresh();
    });
    g.addEventListener('pointerleave', () => {
      preview = null;
      refresh();
    });
    g.addEventListener('click', () => pin(id));
  });

  async function load3D() {
    if (scene || loading) return;
    // Measure the scrolling box, not the frame: on small screens the frame is wider than the view and scrolls.
    const visibleWidth = frame.parentElement?.clientWidth ?? frame.clientWidth;
    if (reduceMotion.matches || visibleWidth < MIN_3D_WIDTH || !webglAvailable()) return;
    loading = true;
    try {
      const mod = await import('./depmap-three');
      if (reduceMotion.matches) return;
      scene = await mod.createScene(frame, impactOf(shown ?? pinned), {
        onHover(id) {
          preview = id;
          refresh();
        },
        onPick(id) {
          pin(id);
        },
      });
      frame.dataset.mode = '3d';
      svg.setAttribute('aria-hidden', 'true');
      if (hint) hint.textContent = 'Hover a record in the picture, or choose one in the list.';
    } catch (err) {
      // The static picture stays. Nothing else depends on the scene.
      console.warn('Dependency map: 3D view unavailable', err);
      scene = null;
    } finally {
      loading = false;
    }
  }

  function unload3D() {
    if (!scene) return;
    scene.destroy();
    scene = null;
    delete frame.dataset.mode;
    svg.removeAttribute('aria-hidden');
  }

  reduceMotion.addEventListener('change', () => (reduceMotion.matches ? unload3D() : load3D()));

  let near = false;
  const io = new IntersectionObserver(
    (entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        near = true;
        io.disconnect();
        void load3D();
      }
    },
    { rootMargin: '400px 0px' },
  );
  io.observe(root);

  let resizeTimer = 0;
  window.addEventListener('resize', () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      if (near && !scene) void load3D();
    }, 250);
  });

  shown = null;
  show(pinned);
}
