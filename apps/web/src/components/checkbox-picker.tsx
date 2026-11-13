/** A multi-pick as a group of checkboxes (an agent's models and channels, a team's members). A fieldset with a
 *  legend names the group; each option is a real checkbox with its label. With many options a filter box narrows the
 *  list (chosen options always stay visible), and the chosen ones are summed up in words above it. */
import { useId, useState } from "react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export type PickOption = { id: string; label: string; hint?: string };

export function CheckboxPicker({ id, legend, options, value, onChange, help, errors, filterAt = 8, disabled, empty, columns = false }: {
  /** Prefix for element IDs, and the ID the error summary links to (the first checkbox, or the filter box). */
  id: string;
  legend: string;
  options: PickOption[];
  value: string[];
  onChange: (ids: string[]) => void;
  help?: React.ReactNode;
  errors?: string[];
  /** Show the filter box from this many options. */
  filterAt?: number;
  disabled?: boolean;
  /** Shown instead of the list when there is nothing to choose from. */
  empty?: React.ReactNode;
  /** Lay short options out in columns (channels). */
  columns?: boolean;
}) {
  const [filter, setFilter] = useState("");
  const uid = useId();
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  const chosen = new Set(value);
  const needle = filter.trim().toLowerCase();
  const shown = options.filter((o) => !needle || chosen.has(o.id) || `${o.label} ${o.hint ?? ""}`.toLowerCase().includes(needle));
  // The live region says what changed ("Added Teams; 3 chosen."), not the whole list again.
  const [announcement, setAnnouncement] = useState("");
  const toggle = (oid: string) => {
    const label = options.find((o) => o.id === oid)?.label ?? "";
    const next = chosen.has(oid) ? value.filter((v) => v !== oid) : [...value, oid];
    setAnnouncement(`${chosen.has(oid) ? "Removed" : "Added"} ${label}; ${next.length} chosen.`);
    onChange(next);
  };
  const names = options.filter((o) => chosen.has(o.id)).map((o) => o.label);
  const filtering = options.length >= filterAt;
  const described = [errors?.length ? errorId : "", help ? helpId : ""].filter(Boolean).join(" ") || undefined;
  return (
    <fieldset className="mb-3 flex min-w-0 flex-col gap-1.5" aria-describedby={described} disabled={disabled}>
      <legend className="mb-1.5 text-sm font-medium leading-none">{legend}</legend>
      {options.length === 0 ? (empty ?? <p className="text-sm text-muted-foreground">Nothing to choose from yet.</p>) : <>
        <p className="text-sm text-muted-foreground">{names.length ? `Chosen: ${names.join(", ")}` : "None chosen."}</p>
        <p className="sr-only" aria-live="polite">{announcement}</p>
        {filtering && (
          <div className="flex flex-col gap-1">
            <label htmlFor={id} className="sr-only">Filter {legend.toLowerCase()}</label>
            <Input id={id} type="search" value={filter} onChange={(e) => setFilter(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") e.preventDefault(); /* filtering, never submitting the form */ }} placeholder={`Filter ${legend.toLowerCase()}…`} autoComplete="off" />
          </div>
        )}
        <ul className={cn("m-0 max-h-64 list-none overflow-y-auto rounded-md border border-input p-1", columns && "grid gap-x-4 sm:grid-cols-3")}>
          {shown.map((o, i) => (
            <li key={o.id}>
              <label htmlFor={i === 0 && !filtering ? id : `${uid}-${o.id}`} className="flex min-h-8 cursor-pointer items-center gap-2 rounded-sm px-2 py-1 text-sm hover:bg-row-hover">
                <input type="checkbox" id={i === 0 && !filtering ? id : `${uid}-${o.id}`} checked={chosen.has(o.id)} onChange={() => toggle(o.id)} className="h-4 w-4 shrink-0 accent-primary" />
                <span className="min-w-0">{o.label}{o.hint && <span className="text-muted-foreground"> · {o.hint}</span>}</span>
              </label>
            </li>
          ))}
          {shown.length === 0 && <li className="px-2 py-1 text-sm text-muted-foreground">Nothing matches “{filter.trim()}”.</li>}
        </ul>
      </>}
      {help && <span className="text-sm text-muted-foreground" id={helpId}>{help}</span>}
      {errors?.length ? <span className="text-sm text-error-foreground" id={errorId}>{errors.join(" ")}</span> : null}
    </fieldset>
  );
}
