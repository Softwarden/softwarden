/** DiffView: a before-and-after table of field changes, for audit diffs now and, later, discovery
 *  drift and import previews.
 *
 *  - `rows` are field rows as the API's audit diff returns them (`catalog/audit_log.event_diff`): a field, its label,
 *    `change` (changed, added: only after, removed: only before), `before` / `after` (a value, an `{id, display}`
 *    reference, or a list of either) and, for sets such as tags, the entries `added` and `removed`.
 *  - `mode`: `changed` shows Change, Before and After columns, with every changed cell tinted in the pending tone and
 *    the change also said in words (never colour alone); `created` shows one Value column (what the record was created
 *    with); `deleted` shows one Last value column.
 *  - Set entries that joined are chips marked "+" (and "added" for screen readers) in After; entries that left are
 *    struck through and marked "−" ("removed") in Before.
 *  - `source` explains where the rows came from: `snapshots` needs no note; `changes` (an event written before
 *    snapshots) says only the fields the event listed are shown; `none` says no field-level detail was recorded and
 *    shows `summary` instead of a table. */
import { Info } from "lucide-react";
import { TableRegion } from "@/components/app";
import { cap } from "@/api";
import { chip, LifecycleChip } from "@/components/sw";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

export type DiffRow = {
  field: string;
  label: string;
  change: "added" | "removed" | "changed";
  before: unknown;
  after: unknown;
  added?: unknown[];
  removed?: unknown[];
};
export type DiffSource = "snapshots" | "changes" | "none";
export type DiffMode = "changed" | "created" | "deleted";

type Identity = { id: string | number; display: string | null };
const isIdentity = (v: unknown): v is Identity => typeof v === "object" && v !== null && "id" in v && "display" in v;
const blank = (v: unknown) => v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0)
  || (typeof v === "object" && v !== null && !Array.isArray(v) && Object.keys(v).length === 0);
/** One set entry's identity, to tell which entries joined or left. */
const entryKey = (v: unknown) => (isIdentity(v) ? `id:${v.id}` : `value:${typeof v === "string" ? v : JSON.stringify(v)}`);

/** A value in words: a reference by its name, a flag as Yes or No, a small object as "key: value" pairs. */
export function valueText(v: unknown): string {
  if (blank(v)) return "Not set";
  if (isIdentity(v)) return v.display ?? "a deleted record";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (Array.isArray(v)) return v.map(valueText).join(", ");
  if (typeof v === "object") return Object.entries(v as Record<string, unknown>).map(([k, x]) => `${k.replace(/_/g, " ")}: ${valueText(x)}`).join("; ");
  return String(v);
}

/** Mode from the event's snapshots: no "before" is a creation, no "after" a deletion. */
export function diffMode(prechange: unknown, postchange: unknown): DiffMode {
  if (prechange == null && postchange != null) return "created";
  if (postchange == null && prechange != null) return "deleted";
  return "changed";
}

function NotSet() {
  return <span className="italic">Not set</span>;
}

/** Fields whose values are codes ("high", "on_prem"): shown as words. Free text and URLs are shown as entered. */
const CODED = new Set(["criticality", "environment", "status", "component_type", "change_type", "decision", "auto_renew", "role", "type", "notice_unit", "term_unit"]);

function Value({ field, value }: { field: string; value: unknown }) {
  if (blank(value)) return <NotSet />;
  if (field === "lifecycle" && typeof value === "string") return <LifecycleChip value={value} />;
  if (CODED.has(field) && typeof value === "string" && /^[a-z_]+$/.test(value)) return <>{cap(value.replace(/_/g, " "))}</>;
  if (typeof value === "string") return <span className="whitespace-pre-wrap [overflow-wrap:anywhere]">{value}</span>;
  return <span className="[overflow-wrap:anywhere]">{valueText(value)}</span>;
}

/** A set (tags, covered applications) as chips; `marked` entries are the ones that joined (after) or left (before). */
function SetValue({ items, marked, side }: { items: unknown[]; marked: Set<string>; side: "before" | "after" }) {
  if (items.length === 0) return <NotSet />;
  return (
    <ul className="m-0 flex list-none flex-wrap gap-1 p-0">
      {items.map((v) => {
        const key = entryKey(v);
        const hit = marked.has(key);
        if (!hit) return <li key={key} className={chip({ state: "neutral" })}>{valueText(v)}</li>;
        return side === "after"
          ? <li key={key} className={chip({ state: "confirmed" })} data-diff="added"><span aria-hidden="true">+</span>{valueText(v)}<span className="sr-only"> (added)</span></li>
          : <li key={key} className={cn(chip({ state: "neutral" }), "line-through")} data-diff="removed"><span aria-hidden="true">−</span>{valueText(v)}<span className="sr-only"> (removed)</span></li>;
      })}
    </ul>
  );
}

function Side({ row, side }: { row: DiffRow; side: "before" | "after" }) {
  const value = row[side];
  const isSet = row.added !== undefined || row.removed !== undefined;
  if (isSet && (Array.isArray(value) || value == null)) {
    const marked = new Set((side === "after" ? row.added ?? [] : row.removed ?? []).map(entryKey));
    return <SetValue items={Array.isArray(value) ? value : []} marked={marked} side={side} />;
  }
  return <Value field={row.field} value={value} />;
}

const CHANGE = {
  changed: { glyph: "◐", text: "Changed" },
  added: { glyph: "+", text: "Set" },
  removed: { glyph: "−", text: "Cleared" },
} as const;

/** For sets: "2 added, 1 removed". */
function setCounts(row: DiffRow) {
  const parts = [row.added?.length ? `${row.added.length} added` : "", row.removed?.length ? `${row.removed.length} removed` : ""].filter(Boolean);
  return parts.join(", ");
}

function Frame({ bare, label, children }: { bare: boolean; label: string; children: React.ReactNode }) {
  return bare ? <div className="rounded-lg border bg-card">{children}</div> : <TableRegion label={label}>{children}</TableRegion>;
}

const NOTES: Record<Exclude<DiffSource, "snapshots">, string> = {
  changes: "This event was recorded before full before-and-after snapshots were kept, so only the fields it listed are shown, and people and records are named as they are now.",
  none: "No field-level detail was recorded for this event. Events such as sign-ins, exports, snoozes and older records of some changes keep only the summary.",
};

export function DiffView({ rows, source, mode, caption, summary, bare = false }: {
  rows: DiffRow[];
  source: DiffSource;
  mode: DiffMode;
  /** Names the table and its scroll region: "Changes in Fields changed, 2026-11-04 10:12". */
  caption: string;
  /** Shown when there is nothing to compare. */
  summary?: string;
  /** Inside a table that already scrolls in its own region (a History row): no region of its own, so no extra tab
   *  stop. */
  bare?: boolean;
}) {
  if (source === "none" || rows.length === 0) {
    return (
      <div className="rounded-md border bg-card px-4 py-3" data-testid="diff-none">
        <p className="flex items-start gap-2"><Info aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
          {source === "none" ? NOTES.none : "Nothing changed: every field kept its value."}</p>
        {summary && <p className="mt-1 text-muted-foreground">Summary: {summary}</p>}
      </div>
    );
  }
  const single = mode !== "changed";
  return (
    <div>
      {source === "changes" && (
        <p className="mb-2 flex items-start gap-2 rounded-md border border-info-border bg-info-bg px-3 py-2" data-testid="diff-changes-note">
          <Info aria-hidden="true" className="mt-0.5 size-4 shrink-0" />{NOTES.changes}
        </p>
      )}
      {mode === "created" && <p className="mb-2 text-muted-foreground">Created with these values; there was nothing before.</p>}
      {mode === "deleted" && <p className="mb-2 text-muted-foreground">Deleted; these were its last values.</p>}
      <Frame bare={bare} label={`${caption} table`}>
        <Table data-testid="diff-view">
          <TableCaption className="sr-only">{caption}{single ? "" : ". Changed values are highlighted and marked in the Change column."}</TableCaption>
          <TableHeader><TableRow>
            <TableHead scope="col">Field</TableHead>
            {single ? <TableHead scope="col">{mode === "created" ? "Value" : "Last value"}</TableHead> : <>
              <TableHead scope="col">Change</TableHead><TableHead scope="col">Before</TableHead><TableHead scope="col">After</TableHead>
            </>}
          </TableRow></TableHeader>
          <TableBody>
            {rows.map((row) => {
              const c = CHANGE[row.change];
              const counts = setCounts(row);
              const tint = "bg-state-pending text-state-pending-foreground";
              return (
                <TableRow key={row.field} data-change={row.change}>
                  <TableHead scope="row" className="h-auto min-w-36 py-2.5 align-top font-semibold text-foreground">{row.label}</TableHead>
                  {single ? (
                    <TableCell className="min-w-48 whitespace-normal align-top"><Side row={row} side={mode === "created" ? "after" : "before"} /></TableCell>
                  ) : <>
                    <TableCell className="align-top">
                      <span className="inline-flex items-center gap-1.5 font-semibold"><span aria-hidden="true">{c.glyph}</span>{c.text}</span>
                      {counts && <span className="block text-xs text-muted-foreground">{counts}</span>}
                    </TableCell>
                    <TableCell className={cn("min-w-40 whitespace-normal align-top", tint)} data-side="before"><Side row={row} side="before" /></TableCell>
                    <TableCell className={cn("min-w-40 whitespace-normal align-top", tint)} data-side="after"><Side row={row} side="after" /></TableCell>
                  </>}
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </Frame>
    </div>
  );
}
