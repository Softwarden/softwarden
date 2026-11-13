/** FilterBar: the search box, quick presets, filter chips with remove, an "Add filter" menu, the
 *  saved-view selector and "Clear filters". The caller keeps every value in the URL; this component only reports
 *  changes. Search is debounced (300 ms) and follows the URL when it changes elsewhere (back button, saved filter).
 *
 *  A chip is a labelled select (the filter's name is its label, so "Tag" finds the tag filter) and a remove button.
 *  "Add filter" adds an empty chip and focuses its select; choosing a value writes it to the URL. Removing a chip
 *  removes the parameter and returns focus to "Add filter". */
import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { MoreMenu } from "@/components/more-menu";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export type FilterOption = { value: string; label: string };
/** `input`: how the chip takes its value. A select of `options` (the default); a text, number or date box (custom field
 *  filters: contains, exact, from, to), written to the URL after a pause or on Enter; or `choices`, a toggle button per
 *  option whose pressed ones are sent comma-separated (any of them). */
export type FilterField = {
  key: string; label: string; options: FilterOption[]; input?: "select" | "text" | "number" | "date" | "choices"; step?: string;
  /** A control of the caller's own in place of the select (a searchable picker when the options are too many to list,
   *  such as contracts); it gets the chip's id, so the chip's label names it. */
  render?: (id: string, value: string, onChange: (v: string) => void) => React.ReactNode;
};

const DEBOUNCE_MS = 300;

export function FilterBar({ id, searchLabel, searchPlaceholder, search, onSearch, fields, values, onChange, presets, saved, filtered, onClear }: {
  /** Prefix for element IDs ("list"). */
  id: string;
  searchLabel: string;
  searchPlaceholder?: string;
  /** The search as it is in the URL. */
  search: string;
  onSearch: (q: string) => void;
  fields: FilterField[];
  /** Current values by field key, from the URL ("" or missing: not set). */
  values: Record<string, string>;
  /** `typed`: the value comes from a box being typed in (text, number, date), which replaces the history entry like
   *  the search; a select, a choice or removing a chip is a discrete change, which pushes one. */
  onChange: (key: string, value: string, typed?: boolean) => void;
  /** Quick filters (pressed-state buttons), shown beside the search. */
  presets?: React.ReactNode;
  /** The saved-view selector and its actions, shown on the second row. */
  saved?: React.ReactNode;
  /** Whether anything differs from the default view, which shows "Clear filters". */
  filtered: boolean;
  onClear: () => void;
}) {
  const [text, setText] = useState(search);
  const [pending, setPending] = useState<string[]>([]);
  const addButton = useRef<HTMLDivElement>(null);

  // The box follows the URL (another filter applied, Back pressed); typing is written to the URL after a pause.
  // Only when the URL says something else: "foo " typed (a space before the next word) stays as typed.
  useEffect(() => { if (search !== text.trim()) setText(search); }, [search]); // eslint-disable-line react-hooks/exhaustive-deps
  const latest = useRef(onSearch);
  latest.current = onSearch;
  useEffect(() => {
    if (text.trim() === search) return;
    const t = setTimeout(() => latest.current(text.trim()), DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [text]); // eslint-disable-line react-hooks/exhaustive-deps

  const active = fields.filter((f) => values[f.key] || pending.includes(f.key));
  const available = fields.filter((f) => !active.includes(f));
  const focusAdd = () => setTimeout(() => addButton.current?.querySelector("button")?.focus());

  function add(key: string) {
    setPending((p) => [...p, key]);
    setTimeout(() => document.getElementById(`${id}-filter-${key}`)?.focus());
  }
  function remove(key: string) {
    setPending((p) => p.filter((k) => k !== key));
    if (values[key]) onChange(key, "");
    focusAdd();
  }
  function clear() {
    setText("");
    setPending([]);
    onClear();
  }

  return (
    <div className="mb-4 flex flex-col gap-3">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
        <form role="search" className="flex w-full max-w-sm flex-col gap-1.5" onSubmit={(e) => { e.preventDefault(); onSearch(text.trim()); }}>
          <Label htmlFor={`${id}-search`}>{searchLabel}</Label>
          <Input id={`${id}-search`} type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder={searchPlaceholder} />
        </form>
        {presets}
      </div>
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filters">
        {active.length > 0 && (
          <ul aria-label="Active filters" className="m-0 flex list-none flex-wrap gap-2 p-0">
            {active.map((f) => (
              <li key={f.key} className={`inline-flex min-h-8 flex-wrap items-center gap-1 border border-border-strong bg-card pl-3 pr-0.5 text-[13px] ${f.input === "choices" ? "rounded-2xl py-0.5" : "rounded-full"}`}>
                {f.input === "choices"
                  ? <span id={`${id}-filter-${f.key}-label`} className="font-semibold">{f.label}</span>
                  : <label htmlFor={`${id}-filter-${f.key}`} className="font-semibold">{f.label}</label>}
                <span aria-hidden="true">:</span>
                <ChipControl id={`${id}-filter-${f.key}`} field={f} value={values[f.key] ?? ""} onChange={(v, typed) => onChange(f.key, v, typed)} />
                <button type="button" aria-label={`Remove ${f.label} filter`} onClick={() => remove(f.key)}
                  className="sw-target inline-grid size-7 place-items-center rounded-full text-muted-foreground hover:bg-accent hover:text-accent-foreground">
                  <X aria-hidden="true" className="size-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
        <div ref={addButton}>
          <MoreMenu label="Add filter" menuLabel="Add a filter" size="sm" align="left"
            actions={available.map((f) => ({ label: f.label, onSelect: () => add(f.key) }))} />
        </div>
        {filtered && <Button variant="outline" size="sm" onClick={clear}>Clear filters</Button>}
      </div>
      {saved}
    </div>
  );
}

/** The value part of a chip: a select, a text / number / date box, or toggle buttons for choices. */
function ChipControl({ id, field, value, onChange }: { id: string; field: FilterField; value: string; onChange: (v: string, typed?: boolean) => void }) {
  if (field.render) return <span className="inline-block w-56">{field.render(id, value, onChange)}</span>;
  const input = field.input ?? "select";
  if (input === "choices") {
    const chosen = value ? value.split(",") : [];
    // Values the field doesn't offer (a stale link) stay listed, so they can be turned off.
    const options = [...field.options, ...chosen.filter((c) => !field.options.some((o) => o.value === c)).map((c) => ({ value: c, label: c }))];
    const toggle = (v: string) => {
      const next = chosen.includes(v) ? chosen.filter((c) => c !== v) : [...chosen, v];
      onChange(options.map((o) => o.value).filter((o) => next.includes(o)).join(","));
    };
    return (
      <span role="group" aria-labelledby={`${id}-label`} className="inline-flex flex-wrap gap-1 py-0.5">
        {options.map((o, i) => (
          <button key={o.value} id={i === 0 ? id : undefined} type="button" aria-pressed={chosen.includes(o.value)} onClick={() => toggle(o.value)}
            className="inline-flex h-7 items-center gap-1 rounded-full border border-border-strong px-2 text-[13px] text-muted-foreground aria-pressed:border-primary aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground">
            {chosen.includes(o.value) && <span aria-hidden="true">✓</span>}{o.label}
          </button>
        ))}
      </span>
    );
  }
  if (input === "select") {
    return (
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)}
        className="h-7 max-w-48 rounded-sm bg-transparent px-1 text-[13px] focus-visible:outline-2 focus-visible:outline-ring">
        <option value="">Any</option>
        {field.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        {value && !field.options.some((o) => o.value === value) && <option value={value}>{value}</option>}
      </select>
    );
  }
  return <ChipInput id={id} type={input} step={field.step} value={value} onChange={(v) => onChange(v, true)} />;
}

/** A typed chip value, written to the URL after a pause (dates at once), on Enter and on leaving the box; it follows the
 *  URL when that changes elsewhere. */
function ChipInput({ id, type, step, value, onChange }: { id: string; type: "text" | "number" | "date"; step?: string; value: string; onChange: (v: string) => void }) {
  const [text, setText] = useState(value);
  useEffect(() => { if (value !== text.trim()) setText(value); }, [value]); // eslint-disable-line react-hooks/exhaustive-deps
  const latest = useRef(onChange);
  latest.current = onChange;
  useEffect(() => {
    if (text.trim() === value) return;
    const t = setTimeout(() => latest.current(text.trim()), type === "date" ? 0 : DEBOUNCE_MS * 2);
    return () => clearTimeout(t);
  }, [text]); // eslint-disable-line react-hooks/exhaustive-deps
  const commit = () => { if (text.trim() !== value) onChange(text.trim()); };
  return (
    <input id={id} type={type === "text" ? "search" : type} step={type === "number" ? (step ?? "any") : undefined} value={text}
      onChange={(e) => setText(e.target.value)} onBlur={commit}
      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } }}
      className={`h-7 rounded-sm border border-input bg-transparent px-1.5 text-[13px] focus-visible:outline-2 focus-visible:outline-ring ${type === "date" ? "w-36" : type === "number" ? "w-28" : "w-40"}`} />
  );
}
