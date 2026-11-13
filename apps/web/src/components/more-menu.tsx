/** "More" actions menu button (WAI-ARIA menu button pattern). Esc, Tab and outside clicks close it; focus returns to the button.
 *  A selected action runs after focus is back on the button, so a dialog it opens returns focus there when it closes. */
import { useEffect, useId, useRef, useState } from "react";
import { useAnchoredLayer } from "@/components/anchored";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** `checked` makes the item a radio choice (menuitemradio) shown with a check mark when chosen. */
export type MenuAction = { label: string; onSelect: () => void; danger?: boolean; checked?: boolean };

/** `about` names what the menu acts on, for screen readers, when several menus share a visible label (one per row).
 *  `trigger` replaces the button's visible content (the accessible name is still `label`, plus `about`). */
/** `align` puts the menu under the button's left or right edge; `side: "top"` opens it upwards (the sidebar's user
 *  menu); `triggerClassName` replaces the outline button style (the ink sidebar). */
export function MoreMenu({ actions, label = "More", about, size, trigger, menuLabel, align = "right", side = "bottom", triggerClassName, caret = true, disabled = false }: {
  actions: MenuAction[]; label?: string; about?: string; size?: "sm"; trigger?: React.ReactNode; menuLabel?: string;
  align?: "left" | "right"; side?: "top" | "bottom"; triggerClassName?: string; caret?: boolean; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  // Roving tabindex: the item last focused is the one in the tab order, so a menu taller than its cap (a scroll box)
  // has keyboard-reachable content (axe scrollable-region-focusable); arrows still move between items.
  const [active, setActive] = useState(0);
  const wrap = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const menuId = useId();
  // A fixed layer anchored to the button, so a dialog's or table's scroll box never clips the menu.
  const menu = useRef<HTMLDivElement>(null);
  const layer = useAnchoredLayer(open, button, menu, { side, align, maxHeight: 420, matchWidth: false });

  const focusItem = (i: number) => items.current[(i + actions.length) % actions.length]?.focus();
  const close = (returnFocus = true) => { setOpen(false); if (returnFocus) button.current?.focus(); };

  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [open]);

  function onButtonKey(e: React.KeyboardEvent) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    setOpen(true);
    setTimeout(() => focusItem(e.key === "ArrowDown" ? 0 : -1));
  }

  function onMenuKey(e: React.KeyboardEvent) {
    const at = items.current.findIndex((el) => el === document.activeElement);
    const moves: Record<string, () => void> = {
      ArrowDown: () => focusItem(at + 1), ArrowUp: () => focusItem(at - 1),
      Home: () => focusItem(0), End: () => focusItem(-1),
      Escape: () => close(), Tab: () => close(false),
    };
    if (!moves[e.key]) return;
    if (e.key !== "Tab") e.preventDefault();
    moves[e.key]();
  }

  if (!actions.length) return null;
  return (
    <div ref={wrap} className="relative">
      <button ref={button} type="button" disabled={disabled} className={triggerClassName ?? buttonVariants({ variant: "outline", size })} aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined}
        aria-label={trigger ? `${label}${about ? ` for ${about}` : ""}` : undefined}
        onClick={() => { if (open) return close(); setOpen(true); setTimeout(() => focusItem(Math.max(0, actions.findIndex((a) => a.checked)))); }} onKeyDown={onButtonKey}>
        {trigger ?? <>{label}{about && <span className="sr-only"> for {about}</span>}</>}{caret && <span aria-hidden="true">▾</span>}
      </button>
      {open && (
        <div ref={menu} id={menuId} role="menu" aria-label={menuLabel ?? `${label} actions${about ? ` for ${about}` : ""}`} onKeyDown={onMenuKey} style={layer}
          className={cn("absolute z-50 min-w-52 overflow-auto rounded-md border bg-popover p-1 text-popover-foreground shadow-md",
            // Until the anchored layer is placed (the first frame), the menu sits beside the button as before.
            !layer && (align === "right" ? "right-0" : "left-0"), !layer && (side === "bottom" ? "top-full mt-1" : "bottom-full mb-1"))}>
          {actions.map((a, i) => (
            <button key={a.label} ref={(el) => { items.current[i] = el; }} type="button" role={a.checked === undefined ? "menuitem" : "menuitemradio"}
              aria-checked={a.checked} tabIndex={i === active ? 0 : -1} onFocus={() => setActive(i)}
              className={cn("flex w-full items-center rounded-sm px-3 py-2 text-left text-sm hover:bg-accent hover:text-accent-foreground focus-visible:bg-accent focus-visible:text-accent-foreground focus-visible:outline-none",
                a.danger && "text-error-foreground")}
              onClick={() => { close(); a.onSelect(); }}>
              {a.checked !== undefined && <span aria-hidden="true" className="mr-2 inline-block w-3">{a.checked ? "✓" : ""}</span>}
              {a.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
