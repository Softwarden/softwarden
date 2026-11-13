/** Searchable single-select (WAI-ARIA 1.2 editable combobox with list autocomplete), with an optional inline "+ Add" entry.
 *  Used for vendor and the owner/team pickers. Pass the `Field` id as `id`; `Field` supplies aria-invalid and aria-describedby. */
import { useId, useRef, useState } from "react";
import { useAnchoredLayer } from "@/components/anchored";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export type ComboOption = { id: string; label: string; hint?: string };

type Item = { kind: "clear" } | { kind: "option"; option: ComboOption } | { kind: "add" };

export function Combobox({ id, options, value, onChange, placeholder, clearLabel, add, disabled, className, remote, listLabel = "Suggestions", ...aria }: {
  id: string;
  /** Accessible name of the popup list, for example "Vendor suggestions". */
  listLabel?: string;
  options: ComboOption[];
  value: string;
  onChange: (id: string) => void;
  placeholder?: string;
  /** Text of the first entry that clears the value, for example "Not set (gap)". Omit for a required choice. */
  clearLabel?: string;
  /** Adds an entry at the end of the list, for example `+ Add vendor "Contoso"`. `onAdd` gets the text typed so far. */
  add?: { label: (typed: string) => string; onAdd: (typed: string) => void };
  disabled?: boolean;
  className?: string;
  /** Options come from a search, so don't filter them here. `onQueryChange` gets the text typed; `busy` shows "Searching…". */
  remote?: { onQueryChange: (text: string) => void; busy?: boolean };
  "aria-invalid"?: boolean;
  "aria-describedby"?: string;
}) {
  const listId = useId();
  const [query, setQuery] = useState<string | null>(null); // null = not editing; show the chosen label
  const [open, setOpen] = useState(false);
  // The highlighted option by identity, not position: remote results arrive and shrink while the list is open, and
  // the announced, highlighted and chosen option must stay the same one.
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const selected = options.find((o) => o.id === value);
  const typed = (query ?? "").trim();
  const needle = typed.toLowerCase();

  const matches = query === null || !needle || remote ? options : options.filter((o) => `${o.label} ${o.hint ?? ""}`.toLowerCase().includes(needle));
  const items: Item[] = [
    ...(clearLabel && !needle ? [{ kind: "clear" } as Item] : []),
    ...matches.map((option): Item => ({ kind: "option", option })),
    ...(add ? [{ kind: "add" } as Item] : []),
  ];
  const itemId = (i: number) => `${listId}-${i}`;
  const keyOf = (item: Item) => (item.kind === "option" ? `o:${item.option.id}` : item.kind);
  const found = items.findIndex((it) => keyOf(it) === activeKey);
  const active = items.length === 0 ? -1 : found < 0 ? 0 : found; // a vanished option falls back to the first
  const activate = (i: number) => setActiveKey(items[i] ? keyOf(items[i]) : null);

  function close() { setOpen(false); if (query !== null) remote?.onQueryChange(""); setQuery(null); }
  function choose(item: Item | undefined) {
    if (!item) return;
    if (item.kind === "add") { const text = typed; close(); add?.onAdd(text); return; }
    onChange(item.kind === "clear" ? "" : item.option.id);
    close();
  }
  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) { setOpen(true); activate(Math.max(0, items.findIndex((it) => it.kind === "option" && it.option.id === value))); return; }
      if (items.length === 0) return; // nothing to move to yet; results may still be on their way
      activate((active + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length);
    } else if (e.key === "Enter" && open) {
      e.preventDefault(); // choosing must not submit the form
      if (active >= 0) choose(items[active]);
    } else if (e.key === "Escape" && open) {
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === "Home" && open) { e.preventDefault(); activate(0); }
    else if (e.key === "End" && open) { e.preventDefault(); activate(items.length - 1); }
  }

  // The list is a fixed layer anchored to the field, so a dialog's scroll box never clips it.
  const box = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const layer = useAnchoredLayer(open, box, list);

  const label = (item: Item) => item.kind === "clear" ? clearLabel : item.kind === "add" ? add?.label(typed) : item.option.label;

  return (
    <div className="relative" ref={box}>
      <Input
        id={id} role="combobox" autoComplete="off" disabled={disabled} className={cn("pr-8", className)} placeholder={placeholder}
        aria-expanded={open} aria-controls={listId} aria-autocomplete="list"
        aria-activedescendant={open && active >= 0 ? itemId(active) : undefined}
        value={query ?? selected?.label ?? ""}
        onChange={(e) => { setQuery(e.target.value); setOpen(true); setActiveKey(null); remote?.onQueryChange(e.target.value.trim()); }}
        onClick={() => !open && setOpen(true)}
        onKeyDown={onKeyDown}
        onBlur={close}
        {...aria}
      />
      <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground" aria-hidden="true">▾</span>
      <span className="sr-only" role="status">{open ? `${matches.length} ${matches.length === 1 ? "match" : "matches"}` : ""}</span>
      <ul ref={list} id={listId} role="listbox" aria-label={listLabel} hidden={!open} style={layer}
        className="absolute z-50 mt-1 max-h-64 w-full overflow-auto rounded-md border bg-popover py-1 text-popover-foreground shadow-md">
        {open && items.map((item, i) => (
          <li key={item.kind === "option" ? item.option.id : item.kind} id={itemId(i)} role="option" aria-selected={item.kind === "option" && item.option.id === value}
            // mousedown would blur the input and close the list before the click lands
            onMouseDown={(e) => e.preventDefault()} onClick={() => choose(item)} onMouseMove={() => activate(i)}
            className={cn("cursor-pointer px-3 py-1.5 text-sm", i === active && "bg-accent text-accent-foreground",
              item.kind === "add" && "border-t font-semibold text-link", item.kind === "clear" && "text-muted-foreground")}>
            {label(item)}
            {item.kind === "option" && item.option.hint && <span className="block text-xs text-muted-foreground">{item.option.hint}</span>}
          </li>
        ))}
        {open && matches.length === 0 && !add && <li className="px-3 py-1.5 text-sm text-muted-foreground" role="option" aria-disabled="true" aria-selected="false">{remote?.busy ? "Searching…" : "No matches"}</li>}
      </ul>
    </div>
  );
}
