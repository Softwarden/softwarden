/** A popup list (Combobox, MoreMenu) placed in a fixed layer anchored to its control, instead of `absolute` inside its
 *  container: a dialog scrolls inside itself now, and an absolute list in it was clipped by the dialog's
 *  edge. Fixed positioning takes it out of every scroll box (DialogContent has no transform once open, so the viewport is
 *  its containing block), and it stays in the React tree and the DOM where it was, inside the dialog's focus and
 *  pointer scope, so it needs no portal.
 *
 *  It opens on `side`, and flips to the other side only when its own height (measured, capped at `maxHeight`) doesn't
 *  fit on that side and the other side has more room; its height is then capped to the room it has. Across, it keeps at
 *  least 8 px inside the viewport (a right-aligned menu at 320 px used to start at x=-104). It follows the control on
 *  scroll and resize. */
import { useLayoutEffect, useState, type CSSProperties, type RefObject } from "react";

const GAP = 4;
const MARGIN = 8;

export function useAnchoredLayer(open: boolean, anchor: RefObject<HTMLElement | null>, layer: RefObject<HTMLElement | null>, {
  side = "bottom", align = "left", maxHeight = 256, matchWidth = true,
}: { side?: "bottom" | "top"; align?: "left" | "right"; maxHeight?: number; matchWidth?: boolean } = {}): CSSProperties | undefined {
  const [style, setStyle] = useState<CSSProperties>();
  useLayoutEffect(() => {
    if (!open) { setStyle(undefined); return; }
    const place = () => {
      const el = anchor.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const vw = document.documentElement.clientWidth;
      const vh = window.innerHeight;
      const below = vh - r.bottom - GAP - MARGIN;
      const above = r.top - GAP - MARGIN;
      // The layer's own height (its content, whatever max-height it has now), capped; before it is in the page, the cap.
      // scrollHeight is the content and padding; the borders (offsetHeight - clientHeight) are on top of it.
      const el2 = layer.current;
      const need = Math.min(maxHeight, el2 ? el2.scrollHeight + el2.offsetHeight - el2.clientHeight : maxHeight);
      const up = side === "top" ? !(need > above && below > above) : need > below && above > below;
      const s: CSSProperties = { position: "fixed", maxHeight: Math.max(0, Math.min(maxHeight, up ? above : below)) };
      if (up) s.bottom = vh - r.top + GAP; else s.top = r.bottom + GAP;
      // Across: aligned to the control's left or right edge, then kept MARGIN inside the viewport.
      const room = vw - 2 * MARGIN;
      const width = Math.min(matchWidth ? r.width : (layer.current?.offsetWidth ?? r.width), room);
      const wanted = align === "right" ? r.right - width : r.left;
      s.left = Math.max(MARGIN, Math.min(wanted, vw - MARGIN - width));
      if (matchWidth) s.width = width;
      s.maxWidth = room;
      setStyle(s);
    };
    place();
    // Its content can change while it is open (a picker's search results arrive): place it again for its new size.
    const resized = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(place);
    if (layer.current) resized?.observe(layer.current);
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => { resized?.disconnect(); window.removeEventListener("scroll", place, true); window.removeEventListener("resize", place); };
  }, [open, anchor, layer, side, align, maxHeight, matchWidth]);
  return style;
}
