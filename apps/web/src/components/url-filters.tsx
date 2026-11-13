/** Filters kept in the URL for the report pages (vendor risk, end of life, data flows, shadow IT, access review, SSO
 *  coverage): a hook that reads and writes them (setting one drops the page cursor, which only works with the filters it
 *  came from), a labelled select, a text or number box written on Enter or when it loses focus, the CSV export button,
 *  a sortable header's button, a refused filter in words and the dashed block for a feature the edition lacks.
 *  Imported only by lazily loaded pages, so it never adds to the first bundle. */
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Problem } from "@/api";
import { ErrorBanner, Select } from "@/components/app";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useDownload } from "@/lib/download";
import { problemText } from "@/lib/query";
import { useMe } from "@/lib/session";

export type Option = { value: string; label: string };

/** The page's URL filters: `value(k)`, `set(k, v)` (blank removes it; drops the cursor; pushed, so Back undoes a filter
 *  change, except the search text `q` and a value typed in a box (`typed`), which replace), `setCursor(c)` (pushed, so Back returns to the previous page),
 *  `clear()` (pushed; the listed keys, the order and the cursor;
 *  other keys such as a view switch are kept), `query` (the listed keys that are set) and whether any is set. */
export function useUrlFilters<K extends string>(keys: readonly K[]) {
  const [params, setParams] = useSearchParams();
  const value = (k: string) => params.get(k) ?? "";
  const set = (k: string, v: string, typed = false) => setParams((cur) => {
    const n = new URLSearchParams(cur);
    if (v) n.set(k, v); else n.delete(k);
    n.delete("cursor");
    return n;
  }, { replace: k === "q" || typed });
  const setCursor = (c: string | null) => setParams((cur) => {
    const n = new URLSearchParams(cur);
    if (c) n.set("cursor", c); else n.delete("cursor");
    return n;
  });
  const clear = () => setParams((cur) => {
    const n = new URLSearchParams(cur);
    for (const k of [...keys, "ordering", "cursor"]) n.delete(k);
    return n;
  });
  const query: Partial<Record<K, string>> = {};
  for (const k of keys) if (value(k)) query[k] = value(k);
  const filtered = keys.some((k) => value(k));
  return { params, value, set, setCursor, clear, query, filtered };
}

/** What a refused filter says: the API's field errors, else its detail. */
export function filterProblem(error: unknown, fallback: string) {
  if (error instanceof Problem && Object.keys(error.errors).length) return `${Object.values(error.errors).flat().join(" ")} Change or clear the filters.`;
  return problemText(error, fallback);
}

/** A labelled select. `all` is the blank choice's words (`null`: no blank choice). A value the options don't have (an
 *  old link) stays chosen and is named by `unknown`, or shown as it is. */
export function SelectFilter({ id, label, value, onChange, options, all = "Any", unknown, className = "w-52" }: {
  id: string; label: string; value: string; onChange: (v: string) => void; options: Option[]; all?: string | null; unknown?: string; className?: string;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Select id={id} className={className} value={value} onChange={(e) => onChange(e.target.value)}>
        {all !== null && <option value="">{all}</option>}
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        {value && !options.some((o) => o.value === value) && <option value={value}>{unknown ?? value}</option>}
      </Select>
    </div>
  );
}

/** A box written to the URL on Enter or when it loses focus, not on every keystroke. `numeric` keeps digits only. */
export function CommitInput({ id, label, value, onChange, placeholder, numeric = false, className = "w-56", type = "text" }: {
  id: string; label: string; value: string; onChange: (v: string) => void; placeholder?: string; numeric?: boolean; className?: string; type?: "text" | "search";
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = () => { if (draft.trim() !== value) onChange(draft.trim()); };
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} type={type} inputMode={numeric ? "numeric" : undefined} className={className} value={draft} placeholder={placeholder} aria-describedby={`${id}-help`}
        onChange={(e) => setDraft(numeric ? e.target.value.replace(/[^0-9]/g, "") : e.target.value)} onBlur={commit}
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } }} />
      <span id={`${id}-help`} className="sr-only">Press Enter to apply.</span>
    </div>
  );
}

/** Export CSV (outline): the shared download helper, which says a refusal in words. While platform staff impersonate,
 *  the API refuses exports, so the button gives way to a sentence saying so (never a silent absence). */
export function ExportCsvButton({ path, fallback, what, onDone }: { path: string; fallback: string; what: string; onDone: (text: string) => void }) {
  const me = useMe();
  const exporter = useDownload();
  if (me.impersonation) {
    return <p className="self-center text-sm text-muted-foreground" data-testid="export-off"><span aria-hidden="true">⊘ </span>CSV export is off while you act as this member.</p>;
  }
  return (
    <>
      <Button variant="outline" disabled={exporter.busy} onClick={async () => {
        const name = await exporter.download(path, fallback);
        if (name) onDone(`Exported ${what} to ${name}. The export is recorded in the audit log.`);
      }}>{exporter.busy ? "Exporting…" : "Export CSV"}</Button>
      {exporter.error && <div className="basis-full"><ErrorBanner message={exporter.error} /></div>}
    </>
  );
}

/** A sortable column header's button (aria-sort goes on the th). `next` says what pressing it does
 *  ("most first"). */
export function SortButton({ label, active, descending, next, onClick }: { label: string; active: boolean; descending: boolean; next: string; onClick: () => void }) {
  return (
    <button type="button" className="inline-flex min-h-6 items-center gap-1 font-semibold" onClick={onClick}>
      {label}<span aria-hidden="true">{active ? (descending ? "▼" : "▲") : "↕"}</span>
      <span className="sr-only">, sort {next}</span>
    </button>
  );
}

/** A feature this organization can't use: a dashed block saying so, never a silent absence or a zero. `reason` replaces
 *  the default sentence's tail ("isn't available in this organization's edition."). */
export function Unavailable({ what, reason, testId }: { what: string; reason?: string; testId?: string }) {
  return (
    <p className="rounded-md border border-dashed border-border-strong bg-muted px-4 py-3 text-muted-foreground" data-testid={testId} data-unavailable="">
      <span aria-hidden="true">⊘ </span><span className="font-semibold text-foreground">{what} isn't available.</span>{" "}{reason ?? "This organization's edition doesn't include it."}
    </p>
  );
}
