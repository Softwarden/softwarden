/** Keyboard-first navigation, on top of the command palette:
 *
 *  - Ctrl+K or Cmd+K: the command palette (also while typing in a field).
 *  - `/`: the page's search box when it has one, otherwise the command palette.
 *  - `g` then a letter: go to a page (the letters are in src/components/nav.ts and in the help dialog).
 *  - `c`: create a record of the list's kind (Editors and Admins); elsewhere the palette's create actions.
 *  - `?`: this help. `[`: collapse or expand the sidebar.
 *
 *  Single-key shortcuts are ignored while typing in a field, inside an open menu, while a dialog is open, while an input
 *  method is composing, and with Ctrl, Alt or Cmd held (AltGr, which some layouts need for `[` and `/`, is allowed), so
 *  they never take a character from the user. They can be turned off (WCAG 2.1.4): "Single-key shortcuts" in the help
 *  dialog and on the Profile page, per user in this browser, On by default. Ctrl/Cmd+K always works. */
import { useEffect, useRef } from "react";
import { prefKey, usePref } from "@/lib/prefs";
import { useLocation, useNavigate } from "react-router-dom";
import type { Me } from "@/api";
import { useReturnFocus } from "@/components/app";
import { createInContext, navFor } from "@/components/nav";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";

/** Whether a key press belongs to what the user is typing or operating, not to the app. */
export function isTypingTarget(target: EventTarget | null) {
  const el = target as HTMLElement | null;
  if (!el || !el.closest) return false;
  if (el.isContentEditable) return true;
  if (el.closest('[role="menu"], [role="listbox"], [role="combobox"], [role="dialog"]')) return true;
  const tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") return !["checkbox", "radio", "button", "submit", "reset", "range", "color", "file"].includes((el as HTMLInputElement).type);
  return false;
}

const SEQUENCE_MS = 1500;

/** Whether this is a Mac, where the palette is Cmd+K. */
export const IS_MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
export const PALETTE_KEYS = IS_MAC ? ["⌘", "K"] : ["Ctrl", "K"];

/** The single-key shortcut setting (WCAG 2.1.4), per user in this browser. On by default. */
export function useSingleKeyShortcuts(me: Me) {
  return usePref<boolean>(prefKey(me.user.id, "shortcuts.single"), true);
}

/** The setting as a switch, for the help dialog and the Profile page. */
export function SingleKeySwitch({ me }: { me: Me }) {
  const [on, setOn] = useSingleKeyShortcuts(me);
  return (
    <div className="flex flex-wrap items-center gap-3">
      <button type="button" role="switch" aria-checked={on} aria-describedby="single-key-help" onClick={() => setOn(!on)}
        className="inline-flex h-8 items-center gap-2 rounded-md border border-border-strong bg-card px-3 text-sm font-medium hover:bg-accent hover:text-accent-foreground">
        <span aria-hidden="true" className={`inline-block h-4 w-7 rounded-full border border-border-strong p-0.5 ${on ? "bg-primary" : "bg-muted"}`}>
          <span className={`block size-2.5 rounded-full ${on ? "translate-x-3 bg-primary-foreground" : "bg-muted-foreground"}`} />
        </span>
        Single-key shortcuts<span aria-hidden="true">: {on ? "On" : "Off"}</span>
      </button>
      <span id="single-key-help" className="text-sm text-muted-foreground">
        Keys such as g, c, / and ? without Ctrl. Turn them off if they get in the way of speech input or another tool. {PALETTE_KEYS.join("+")} always works.
      </span>
    </div>
  );
}

export function useShortcuts({ me, paletteOpen, openPalette, closePalette, openHelp, toggleRail, announce }: {
  me: Me;
  paletteOpen: boolean;
  openPalette: (search?: string) => void;
  closePalette: () => void;
  openHelp: () => void;
  toggleRail: () => void;
  /** Says something in the shell's live region (for example why `c` did nothing). */
  announce: (text: string) => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [single] = useSingleKeyShortcuts(me);
  const latest = useRef({ me, single, paletteOpen, openPalette, closePalette, openHelp, toggleRail, announce, navigate, location });
  latest.current = { me, single, paletteOpen, openPalette, closePalette, openHelp, toggleRail, announce, navigate, location };

  useEffect(() => {
    let gAt = 0;
    function onKey(e: KeyboardEvent) {
      if (e.isComposing || e.keyCode === 229) return; // an input method is composing a character
      const s = latest.current;
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (s.paletteOpen) s.closePalette();
        else if (!document.querySelector('[role="dialog"]')) s.openPalette();
        return;
      }
      if (!s.single) { gAt = 0; return; }
      // AltGr types [ and / on many European layouts; only those two keys may arrive with Ctrl+Alt.
      const altGr = e.ctrlKey && e.altKey && !e.metaKey && (e.key === "[" || e.key === "/");
      if (e.defaultPrevented || e.repeat || (!altGr && (e.ctrlKey || e.metaKey || e.altKey))) return;
      if (isTypingTarget(e.target) || document.querySelector('[role="dialog"]')) { gAt = 0; return; }
      const key = e.key;
      if (gAt && Date.now() - gAt < SEQUENCE_MS) {
        gAt = 0;
        const item = navFor(s.me).find((n) => n.key === key.toLowerCase());
        if (item) { e.preventDefault(); s.navigate(item.to); }
        return;
      }
      gAt = 0;
      if (key === "g") { gAt = Date.now(); return; }
      if (key === "?") { e.preventDefault(); s.openHelp(); return; }
      if (key === "[") { e.preventDefault(); s.toggleRail(); return; }
      if (key === "/") {
        e.preventDefault();
        const search = document.querySelector<HTMLInputElement>('main form[role="search"] input, main input[type="search"]');
        if (search) { search.focus(); search.select(); } else s.openPalette();
        return;
      }
      if (key === "c") {
        e.preventDefault();
        if (s.me.role === "viewer") { s.announce("Viewers can't create records. Editors and Admins can."); return; }
        const target = createInContext(s.location.pathname, new URLSearchParams(s.location.search));
        if (target) s.navigate(target.to);
        else s.openPalette("Create");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

/** Every shortcut, as the help dialog and the palette list them. */
export function shortcutList(me: Me, single = true) {
  const palette = { keys: PALETTE_KEYS, label: "Open the command palette" };
  if (!single) return [{ group: "Anywhere", items: [palette] }];
  const pages = navFor(me).filter((n) => n.key).map((n) => ({ keys: ["g", n.key!], label: `Go to ${n.label}` }));
  return [
    { group: "Anywhere", items: [
      palette,
      { keys: ["/"], label: "Search this list, or open the command palette" },
      ...(me.role !== "viewer" ? [{ keys: ["c"], label: "Create a record of this list's kind" }] : []),
      { keys: ["["], label: "Collapse or expand the sidebar" },
      { keys: ["?"], label: "Show these shortcuts" },
    ] },
    { group: "Go to a page", items: pages },
  ];
}

export function ShortcutHelp({ open, onOpenChange, me }: { open: boolean; onOpenChange: (o: boolean) => void; me: Me }) {
  const returnFocus = useReturnFocus(open);
  const [single] = useSingleKeyShortcuts(me);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
          <DialogDescription>
            {single ? "Single keys work when you aren't typing in a field. Press g, then the letter, to go to a page." : "Single-key shortcuts are off. Turn them on to use g, c, / and ?."}
          </DialogDescription>
        </DialogHeader>
        <SingleKeySwitch me={me} />
        {shortcutList(me, single).map((g) => (
          <section key={g.group} aria-labelledby={`keys-${g.group}`}>
            <h2 id={`keys-${g.group}`} className="mb-1.5 font-display text-section font-semibold">{g.group}</h2>
            <table className="w-full text-sm">
              <caption className="sr-only">{g.group} shortcuts</caption>
              <thead className="sr-only"><tr><th scope="col">Keys</th><th scope="col">Action</th></tr></thead>
              <tbody>
                {g.items.map((s) => (
                  <tr key={s.label} className="border-b border-border-subtle last:border-0">
                    <td className="w-28 py-1.5 pr-3 whitespace-nowrap"><Keys keys={s.keys} /></td>
                    <td className="py-1.5">{s.label}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        ))}
      </DialogContent>
    </Dialog>
  );
}

export function Keys({ keys }: { keys: string[] }) {
  return (
    <span className="inline-flex items-center gap-1">
      {keys.map((k, i) => (
        <kbd key={i} className="inline-grid h-6 min-w-6 place-items-center rounded-sm border border-border-strong bg-muted px-1.5 font-mono text-xs text-foreground">{k}</kbd>
      ))}
    </span>
  );
}
