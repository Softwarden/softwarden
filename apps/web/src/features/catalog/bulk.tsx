/** Bulk edit and bulk delete on the record lists. Loaded on first use from
 *  catalog-list.tsx, so the list's first bundle stays small.
 *
 *  Bulk edit (`POST bulk-edit/preview` then `/apply`, Editors and Admins): owners, support team, department, IT steward,
 *  lifecycle, criticality, tags, custom field values and the next review date. The preview shows each record's before
 *  and after per field and who will need confirmation; apply is all or nothing. A request-level field error (a custom
 *  field value, `custom_field_data.<name>`) is shown on its input; a refused apply lists each record's `errors` and
 *  `field_errors`; a retirement refused because active records outside the selection depend on the record explains
 *  how to go on.
 *
 *  Bulk delete (`POST bulk-delete/preview` then `/apply`, Admins): the preview lists which records can be deleted, what
 *  blocks the others (in words: "2 relationships, 1 instance") and what goes with each (attachments, data-quality
 *  exceptions). Delete is offered only when nothing is blocked; "Deselect blocked" narrows the selection. A record
 *  referenced since the preview makes apply answer 409 with nothing deleted: those rows are shown blocked. */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, cap, CRITICALITIES, LIFECYCLES, Person, Problem, type NamedRef, type UserRef } from "@/api";
import { Chip, ErrorBanner, ErrorSummary, Field, Loading, Select, TableRegion, useLoad, useReturnFocus } from "@/components/app";
import { Combobox, ComboOption } from "@/components/combobox";
import { chip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cfId, CF_KEY, CustomFieldInput, fromDraft, toDraft, useCustomFields, valueText, type CFDraft, type CustomField } from "@/features/catalog/custom-fields";
import { TagInput, useTagNames } from "@/features/catalog/curation";
import { KIND_NOUN, recordPath, type RecordKind } from "@/lib/kinds";
import { problemText } from "@/lib/query";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
/** "a, b and c" */
const listWords = (parts: string[]) => (parts.length <= 1 ? parts.join("") : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`);

/** Focus the page heading when the dialog's opener (the selection bar) went away, as it does after an apply. */
function useBulkReturnFocus(open: boolean) {
  const rf = useReturnFocus(open);
  return (e: Event) => {
    rf.onCloseAutoFocus(e);
    if (!document.activeElement || document.activeElement === document.body) document.querySelector<HTMLElement>("h1")?.focus();
  };
}

// ---- Bulk edit ---------------------------------------------------------------------------------------------------------------

type Named = { id: string; name: string };
type Change = { before: unknown; after: unknown };
type PreviewRow = { id: string; name: string; changes: Record<string, Change>; unchanged: boolean; will_need_confirmation: boolean; errors: string[]; codes?: string[] };
type Summary = { total: number; changed: number; unchanged: number; errors: number; will_need_confirmation: number };
type Preview = { summary: Summary; rows: PreviewRow[] };
type Applied = Preview & { updated: number; unchanged: number };
/** A row of a refused apply (400 `rows`): messages, and the single-edit field errors the record's write was refused with. */
type Refusal = { id: string; name: string; errors: string[]; codes?: string[]; field_errors: Record<string, string[]> };

const FIELD_LABEL: Record<string, string> = {
  business_owner: "Business owner", technical_owner: "Technical owner", support_team: "Support team", department: "Department",
  it_steward: "IT steward", lifecycle: "Lifecycle", criticality: "Criticality", tags: "Tags", next_review_date: "Next review date",
  add_tags: "Add tags", remove_tags: "Remove tags",
};
/** The input each API field error belongs to (custom fields use their API key, `custom_field_data.<name>`, as their ID). */
const FIELD_ID: Record<string, string> = {
  business_owner: "business_owner", technical_owner: "technical_owner", support_team: "support_team", department: "bulk-department",
  it_steward: "bulk-it-steward", lifecycle: "bulk-lifecycle", criticality: "bulk-criticality", next_review_date: "bulk-date",
  add_tags: "bulk-add-tags", remove_tags: "bulk-remove-tags",
};
const CLEAR = "__clear";
/** The API's row codes when a retirement is refused for what depends on the record (catalog/bulk._retirements). */
const RETIRE_REFUSAL = new Set(["retire_dependents", "retire_dependency_loop"]);

function labelFor(field: string, cfs: CustomField[]) {
  if (field.startsWith(`${CF_KEY}.`)) {
    const name = field.slice(CF_KEY.length + 1);
    return cfs.find((c) => c.name === name)?.label ?? name;
  }
  return FIELD_LABEL[field] ?? field.replace(/_/g, " ");
}

function show(field: string, v: unknown, cfs: CustomField[]): string {
  if (field.startsWith(`${CF_KEY}.`)) {
    const cf = cfs.find((c) => c.name === field.slice(CF_KEY.length + 1));
    return cf ? valueText(cf, v) : v === null || v === undefined ? "Not set" : typeof v === "object" ? JSON.stringify(v) : String(v);
  }
  if (v === null || v === undefined || v === "") return field === "tags" ? "no tags" : "not set";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "no tags";
  if (typeof v === "object") return (v as Named).name;
  if (field === "lifecycle" || field === "criticality") return cap(String(v));
  return String(v);
}

/** Bulk edit: choose changes, preview each record's before and after, then apply in one step. */
export function BulkEditDialog({ open, onOpenChange, kind, ids, onDone }: {
  open: boolean; onOpenChange: (o: boolean) => void; kind: RecordKind; ids: string[]; onDone: (text: string) => void;
}) {
  const onCloseAutoFocus = useBulkReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-3xl" onCloseAutoFocus={onCloseAutoFocus}>
        <DialogHeader>
          <DialogTitle>Bulk edit {plural(ids.length, ...KIND_NOUN[kind])}</DialogTitle>
          <DialogDescription>
            Leave a field alone to keep each record's value. Category, vendor and the other fields of one kind are changed on each record's own page.
          </DialogDescription>
        </DialogHeader>
        {open && <BulkForm kind={kind} ids={ids} onCancel={() => onOpenChange(false)} onDone={(t) => { onOpenChange(false); onDone(t); }} />}
      </DialogContent>
    </Dialog>
  );
}

function BulkForm({ kind, ids, onCancel, onDone }: { kind: RecordKind; ids: string[]; onCancel: () => void; onDone: (text: string) => void }) {
  const people = useLoad<Person[]>("people?active=true");
  const teams = useLoad<Named[]>("teams");
  const stewards = useLoad<UserRef[]>("stewards");
  const departments = useLoad<NamedRef[]>("departments");
  const custom = useCustomFields(kind);
  const tagNames = useTagNames();
  const [refs, setRefs] = useState({ business_owner: "", technical_owner: "", support_team: "", department: "", it_steward: "" });
  const [lifecycle, setLifecycle] = useState("");
  const [criticality, setCriticality] = useState("");
  const [addTags, setAddTags] = useState<string[]>([]);
  const [removeTags, setRemoveTags] = useState<string[]>([]);
  const [date, setDate] = useState("");
  // Custom fields to change, in the order chosen, and what each is set to (blank clears it).
  const [cfChosen, setCfChosen] = useState<string[]>([]);
  const [cfDrafts, setCfDrafts] = useState<Record<string, CFDraft>>({});
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [formError, setFormError] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [refusals, setRefusals] = useState<Map<string, Refusal> | null>(null);
  const [busy, setBusy] = useState(false);
  const focusAfter = useRef<string | null>(null);
  useEffect(() => {
    if (!focusAfter.current) return;
    document.getElementById(focusAfter.current)?.focus();
    focusAfter.current = null;
  });

  const lookups = [people, teams, stewards, departments];
  const failed = lookups.find((l) => l.error);
  if (failed || custom.error) {
    return <ErrorBanner message={failed?.error || problemText(custom.error, "Couldn't load the custom fields.")} onRetry={() => { lookups.forEach((l) => l.reload()); custom.refetch(); }} />;
  }
  if (lookups.some((l) => !l.data) || custom.pending) return <Loading what="people, teams and fields" />;
  const cfs = custom.fields ?? [];

  /** The `changes` body and any client-side errors (a JSON object that doesn't parse). */
  function build(): { changes: Record<string, unknown>; local: Record<string, string[]> } {
    const changes: Record<string, unknown> = {};
    const local: Record<string, string[]> = {};
    for (const [k, v] of Object.entries(refs)) if (v) changes[k] = v === CLEAR ? null : k === "it_steward" ? Number(v) : v;
    if (lifecycle) changes.lifecycle = lifecycle;
    if (criticality) changes.criticality = criticality === CLEAR ? null : criticality;
    if (addTags.length) changes.add_tags = addTags;
    if (removeTags.length) changes.remove_tags = removeTags;
    if (date) changes.next_review_date = date;
    if (cfChosen.length) {
      const data: Record<string, unknown> = {};
      for (const name of cfChosen) {
        const cf = cfs.find((c) => c.name === name);
        if (!cf) continue;
        const out = fromDraft(cf, cfDrafts[name] ?? toDraft(cf, null));
        if ("error" in out) local[cfId(name)] = [out.error];
        else data[name] = out.value;
      }
      changes[CF_KEY] = data;
    }
    return { changes, local };
  }

  /** Field errors from the API, keyed by the input they belong to; anything else goes to the banner. */
  function placeErrors(err: Problem) {
    const placed: Record<string, string[]> = {};
    const loose: string[] = [];
    for (const [k, v] of Object.entries(err.errors)) {
      if (k.startsWith(`${CF_KEY}.`) && cfChosen.includes(k.slice(CF_KEY.length + 1))) placed[k] = v;
      else if (FIELD_ID[k]) placed[FIELD_ID[k]] = v;
      else loose.push(...(k === CF_KEY || k.startsWith(`${CF_KEY}.`) ? v.map((m) => `${labelFor(k, cfs)}: ${m}`) : v));
    }
    setErrors(placed);
    setFormError(loose.length ? loose.join(" ") : Object.keys(placed).length ? "" : err.detail);
  }

  async function runPreview(e: React.FormEvent) {
    e.preventDefault();
    setFormError("");
    const { changes, local } = build();
    if (Object.keys(local).length) return setErrors(local);
    if (!Object.keys(changes).length || (Object.keys(changes).length === 1 && changes[CF_KEY] && !Object.keys(changes[CF_KEY] as object).length)) {
      return setErrors({ business_owner: ["Choose at least one change."] });
    }
    setErrors({});
    setBusy(true);
    try {
      setPreview(await api<Preview>("bulk-edit/preview", { method: "POST", body: { kind, ids, changes } }));
      setRefusals(null);
    } catch (err) {
      if (err instanceof Problem && err.status === 400) placeErrors(err);
      else setFormError(problemText(err, "Couldn't preview the changes. Nothing was changed. Try again."));
    } finally {
      setBusy(false);
    }
  }
  async function apply() {
    setFormError("");
    setBusy(true);
    try {
      const done = await api<Applied>("bulk-edit/apply", { method: "POST", body: { kind, ids, changes: build().changes } });
      onDone(`Updated ${plural(done.updated, "record", "records")}; ${done.unchanged} unchanged.${done.summary.will_need_confirmation ? ` ${plural(done.summary.will_need_confirmation, "record is", "records are")} now Needs confirmation.` : ""}`);
    } catch (err) {
      if (err instanceof Problem && Array.isArray(err.body.rows)) {
        const rows = err.body.rows as Refusal[];
        setRefusals(new Map(rows.map((r) => [r.id, { ...r, errors: r.errors ?? [], field_errors: r.field_errors ?? {} }])));
        setFormError(`Nothing was changed: ${plural(rows.length, "record", "records")} can't take this change. Each one says why below.`);
      } else if (err instanceof Problem && err.status === 400) {
        // A field error on apply (a custom field changed since the preview): back to the form, on the field.
        setPreview(null);
        placeErrors(err);
      } else setFormError(`${problemText(err, "Something went wrong.")} Nothing was changed. Try again.`);
    } finally {
      setBusy(false);
    }
  }

  if (preview) {
    return <PreviewView kind={kind} preview={preview} refusals={refusals} cfs={cfs} formError={formError} busy={busy}
      retiring={lifecycle === "retired"}
      onBack={() => { setPreview(null); setRefusals(null); setFormError(""); }} onCancel={onCancel} onApply={() => void apply()} />;
  }

  const pickOptions = (list: { id: string; label: string; hint?: string }[], what: string): ComboOption[] => [{ id: CLEAR, label: `Clear ${what} (shows as a gap)` }, ...list];
  const personOptions = pickOptions(people.data!.map((p) => ({ id: p.id, label: p.name, hint: [p.department?.name, p.email].filter(Boolean).join(" · ") })), "owner");
  const teamOptions = pickOptions(teams.data!.map((t) => ({ id: t.id, label: t.name })), "support team");
  const refField = (key: "business_owner" | "technical_owner" | "support_team", label: string, options: ComboOption[]) => (
    <Field id={key} label={label} errors={errors}>
      <Combobox id={key} options={options} value={refs[key]} onChange={(v) => setRefs((o) => ({ ...o, [key]: v }))}
        placeholder="Leave unchanged" clearLabel="Leave unchanged" listLabel={`${label} suggestions`} />
    </Field>
  );
  const offered = cfs.filter((c) => !cfChosen.includes(c.name));
  function chooseField(name: string) {
    if (!name) return;
    setCfChosen((cur) => [...cur, name]);
    focusAfter.current = cfId(name);
  }
  function dropField(cf: CustomField) {
    setCfChosen((cur) => cur.filter((n) => n !== cf.name));
    setCfDrafts(({ [cf.name]: _drop, ...rest }) => rest);
    setErrors(({ [cfId(cf.name)]: _gone, ...rest }) => rest);
    focusAfter.current = "bulk-cf-pick";
  }

  return (
    <form onSubmit={runPreview} noValidate>
      {formError && <ErrorBanner message={formError} />}
      <ErrorSummary errors={errors} />
      <div className="grid gap-x-6 md:grid-cols-2">
        {refField("business_owner", "Business owner", personOptions)}
        {refField("technical_owner", "Technical owner", personOptions)}
        {refField("support_team", "Support team", teamOptions)}
        <Field id="bulk-it-steward" label="IT steward" errors={errors} help="Admins and Editors only. With none, records route to the IT coordinator.">
          <Select id="bulk-it-steward" value={refs.it_steward} onChange={(e) => setRefs((o) => ({ ...o, it_steward: e.target.value }))}>
            <option value="">Leave unchanged</option>
            <option value={CLEAR}>Clear IT steward (routes to the IT coordinator)</option>
            {stewards.data!.map((u) => <option key={u.id} value={String(u.id)}>{u.name}</option>)}
          </Select>
        </Field>
        <Field id="bulk-department" label="Department" errors={errors}>
          <Select id="bulk-department" value={refs.department} onChange={(e) => setRefs((o) => ({ ...o, department: e.target.value }))}>
            <option value="">Leave unchanged</option>
            <option value={CLEAR}>Clear department</option>
            {departments.data!.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
          </Select>
        </Field>
        <Field id="bulk-lifecycle" label="Lifecycle" errors={errors}
          help="Retiring a record that active records outside the selection depend on is refused; select them too, or retire it on its own page.">
          <Select id="bulk-lifecycle" value={lifecycle} onChange={(e) => setLifecycle(e.target.value)}>
            <option value="">Leave unchanged</option>
            {LIFECYCLES.map((l) => <option key={l} value={l}>{cap(l)}</option>)}
          </Select>
        </Field>
        <Field id="bulk-criticality" label="Criticality" errors={errors}>
          <Select id="bulk-criticality" value={criticality} onChange={(e) => setCriticality(e.target.value)}>
            <option value="">Leave unchanged</option>
            <option value={CLEAR}>Clear criticality (shows as a gap)</option>
            {CRITICALITIES.map((c) => <option key={c} value={c}>{cap(c)}</option>)}
          </Select>
        </Field>
        <Field id="bulk-date" label="Next review date" errors={errors} help="Updates the latest review's date. No new review is recorded.">
          <Input id="bulk-date" type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </Field>
        <TagInput id="bulk-add-tags" label="Add tags" tags={addTags} onChange={setAddTags} suggestions={tagNames} errors={errors["bulk-add-tags"]} />
        <TagInput id="bulk-remove-tags" label="Remove tags" tags={removeTags} onChange={setRemoveTags} suggestions={tagNames} errors={errors["bulk-remove-tags"]}
          help="Records without the tag are left as they are." />
      </div>
      {cfs.length > 0 && (
        <section aria-labelledby="bulk-cf-heading" className="mb-3 mt-2 border-t pt-4">
          <h3 id="bulk-cf-heading" className="font-display text-section font-semibold">Custom fields</h3>
          <p className="mb-3 mt-0.5 text-sm text-muted-foreground">
            Choose a field, then the value every selected record gets. Leave the value blank (or Not set) to clear it on every selected record.
          </p>
          {cfChosen.map((name) => {
            const cf = cfs.find((c) => c.name === name);
            if (!cf) return null;
            return (
              <div key={name} className="mb-3 rounded-md border border-border-subtle px-3 pt-3" data-testid={`bulk-cf-${name}`}>
                <CustomFieldInput cf={cf} draft={cfDrafts[name] ?? toDraft(cf, null)} errors={errors}
                  onChange={(d) => setCfDrafts((cur) => ({ ...cur, [name]: d }))}
                  help={[cf.description, "Blank clears it on every selected record."].filter(Boolean).join(" ")} />
                <div className="mb-3">
                  <Button type="button" size="sm" variant="outline" onClick={() => dropField(cf)}>Don't change {cf.label}</Button>
                </div>
              </div>
            );
          })}
          {offered.length > 0 && (
            <div className="flex max-w-sm flex-col gap-1.5">
              <Label htmlFor="bulk-cf-pick">{cfChosen.length ? "Change another custom field" : "Change a custom field"}</Label>
              <Select id="bulk-cf-pick" value="" onChange={(e) => chooseField(e.target.value)}>
                <option value="">Choose a field…</option>
                {offered.map((c) => <option key={c.name} value={c.name}>{c.label}</option>)}
              </Select>
            </div>
          )}
        </section>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? "Previewing…" : "Preview changes"}</Button>
      </DialogFooter>
    </form>
  );
}

function PreviewView({ kind, preview, refusals, cfs, formError, busy, retiring, onBack, onCancel, onApply }: {
  kind: RecordKind; preview: Preview; refusals: Map<string, Refusal> | null; cfs: CustomField[]; formError: string; busy: boolean; retiring: boolean;
  onBack: () => void; onCancel: () => void; onApply: () => void;
}) {
  const s = preview.summary;
  const refused = refusals?.size ?? 0;
  return (
    <div>
      {formError && <ErrorBanner message={formError} />}
      <p className="mb-1 font-semibold" role="status">
        {plural(s.changed, "record", "records")} will change; {s.will_need_confirmation} will need confirmation; {s.unchanged} unchanged{s.errors ? `; ${plural(s.errors, "has an error", "have errors")}` : ""}.
      </p>
      <p className="mb-3 text-sm text-muted-foreground">
        A record needs confirmation when an owner, the support team, the lifecycle or the criticality changes away from what its last review confirmed.
      </p>
      <TableRegion label="Bulk edit preview table" className="mb-4 max-h-[45vh] overflow-auto">
        <Table>
          <TableCaption className="sr-only">Each record's values before and after the bulk edit</TableCaption>
          <TableHeader><TableRow><TableHead scope="col">Record</TableHead><TableHead scope="col">Before → after</TableHead><TableHead scope="col">Result</TableHead></TableRow></TableHeader>
          <TableBody>
            {preview.rows.map((r) => {
              const refusal = refusals?.get(r.id);
              const errors = [...r.errors, ...(refusal?.errors ?? [])];
              const fieldErrors = Object.entries(refusal?.field_errors ?? {});
              const blocked = errors.length > 0 || fieldErrors.length > 0;
              const retireRefused = retiring && [...(r.codes ?? []), ...(refusal?.codes ?? [])].some((c) => RETIRE_REFUSAL.has(c));
              return (
                <TableRow key={r.id} data-testid="bulk-preview-row">
                  <TableHead scope="row" className="min-w-32 whitespace-normal align-top font-semibold text-foreground [overflow-wrap:anywhere]">{r.name}</TableHead>
                  <TableCell className="whitespace-normal align-top">
                    {Object.entries(r.changes).length === 0 ? <span className="text-muted-foreground">No changes</span> : (
                      <ul className="m-0 list-none p-0">
                        {Object.entries(r.changes).map(([field, c]) => (
                          <li key={field}>
                            <span className="font-medium">{labelFor(field, cfs)}:</span> {show(field, c.before, cfs)}
                            <span aria-hidden="true"> → </span><span className="sr-only"> changes to </span>{show(field, c.after, cfs)}
                          </li>
                        ))}
                      </ul>
                    )}
                    {blocked && (
                      <ul className="m-0 mt-1 list-none p-0 text-error-foreground">
                        {errors.map((m) => <li key={m}><span aria-hidden="true">✕ </span>Error: {m}</li>)}
                        {fieldErrors.map(([k, v]) => <li key={k}><span aria-hidden="true">✕ </span>{labelFor(k, cfs)}: {v.join(" ")}</li>)}
                      </ul>
                    )}
                    {retireRefused && <RetireHelp kind={kind} id={r.id} name={r.name} />}
                  </TableCell>
                  <TableCell className="whitespace-normal align-top">
                    {blocked ? <Chip><span aria-hidden="true">✕</span>Can't change</Chip>
                      : r.unchanged ? <span className="text-muted-foreground">Unchanged</span>
                      : <span className="flex flex-wrap gap-1.5"><Chip><span aria-hidden="true">→</span>Will change</Chip>{r.will_need_confirmation && <Chip><span aria-hidden="true">◐</span>Will need confirmation</Chip>}</span>}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </TableRegion>
      <DialogFooter>
        <Button type="button" variant="outline" disabled={busy} onClick={onBack}>Back</Button>
        <Button type="button" variant="outline" disabled={busy} onClick={onCancel}>Cancel</Button>
        <Button type="button" disabled={busy || s.errors > 0 || refused > 0 || s.changed === 0} onClick={onApply}>
          {busy ? "Applying…" : s.errors > 0 || refused > 0 ? "Fix the errors to apply" : s.changed === 0 ? "Nothing to apply" : `Apply to ${plural(s.changed, "record", "records")}`}
        </Button>
      </DialogFooter>
    </div>
  );
}

/** How to go on when retiring a record is refused for what depends on it: the record's own page (where the
 *  dependents can be acknowledged with a reason) and what depends on it (its Map tab's answer, with links to each). */
function RetireHelp({ kind, id, name }: { kind: RecordKind; id: string; name: string }) {
  const page = recordPath(kind, id);
  const dependents = `${recordPath(kind, id, "map")}&dependents=${id}`;
  return (
    <div className="mt-1 text-sm" data-testid="retire-help">
      <p>A bulk edit can't acknowledge what depends on a record. Retire it on its own page, or select its dependents too.</p>
      <ul className="m-0 mt-0.5 list-none p-0">
        {page && <li><Link to={page} className="inline-block min-h-6 leading-6">Retire {name} on its own page</Link></li>}
        <li><Link to={dependents} className="inline-block min-h-6 leading-6">What depends on {name}</Link></li>
      </ul>
    </div>
  );
}

// ---- Bulk delete -----------------------------------------------------------------------------------------------------------

type DeleteRow = {
  id: string; name: string; can_delete: boolean; blocking_references: Record<string, number>;
  removes: { attachments: number; quality_exceptions: number }; errors: string[];
};
type DeletePreview = { kind: RecordKind; summary: { total: number; deletable: number; blocked: number }; rows: DeleteRow[] };
type DeleteConflictRow = { id: string; name: string; blocking_references: Record<string, number>; errors: string[] };

/** "2 relationships, 1 instance" (the API's nouns, made plural as it does). */
export function referencesText(refs: Record<string, number>) {
  return Object.entries(refs).map(([noun, n]) => `${n} ${noun}${n === 1 ? "" : "s"}`).join(", ");
}
function removesText(r: DeleteRow["removes"]) {
  return listWords([
    ...(r.attachments ? [plural(r.attachments, "attachment", "attachments")] : []),
    ...(r.quality_exceptions ? [plural(r.quality_exceptions, "data-quality exception", "data-quality exceptions")] : []),
  ]);
}

/** Bulk delete (Admins). `onDeselect` takes the blocked records out of the list's selection; the dialog then
 *  previews the rest. */
export function BulkDeleteDialog({ open, onOpenChange, kind, ids, onDone, onDeselect }: {
  open: boolean; onOpenChange: (o: boolean) => void; kind: RecordKind; ids: string[]; onDone: (text: string) => void;
  onDeselect: (ids: string[]) => void;
}) {
  const onCloseAutoFocus = useBulkReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-3xl" onCloseAutoFocus={onCloseAutoFocus}>
        <DialogHeader>
          <DialogTitle>Delete {plural(ids.length, ...KIND_NOUN[kind])}</DialogTitle>
          <DialogDescription>
            Deleting can't be undone. A record other records still reference can't be deleted; archive it instead. The audit log keeps each deleted record's history.
          </DialogDescription>
        </DialogHeader>
        {open && <DeleteBody kind={kind} ids={ids} onCancel={() => onOpenChange(false)} onDeselect={onDeselect}
          onDone={(t) => { onOpenChange(false); onDone(t); }} />}
      </DialogContent>
    </Dialog>
  );
}

function DeleteBody({ kind, ids, onCancel, onDone, onDeselect }: {
  kind: RecordKind; ids: string[]; onCancel: () => void; onDone: (text: string) => void; onDeselect: (ids: string[]) => void;
}) {
  const [noun, nouns] = KIND_NOUN[kind];
  const key = ids.join(",");
  const q = useQuery({
    queryKey: ["bulk-delete-preview", kind, key],
    queryFn: () => api<DeletePreview>("bulk-delete/preview", { method: "POST", body: { kind, ids } }),
    gcTime: 0, staleTime: 0, retry: false,
  });
  const [conflict, setConflict] = useState<{ detail: string; rows: DeleteConflictRow[] } | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [confirmError, setConfirmError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const focusSummary = useRef(false);
  // A new selection (after Deselect blocked) starts again: no conflict, nothing ticked; the summary takes focus.
  useEffect(() => { setConflict(null); setConfirmed(false); setConfirmError(""); setError(""); }, [key]);
  useEffect(() => {
    if (focusSummary.current && q.data) { focusSummary.current = false; document.getElementById("bulk-delete-summary")?.focus(); }
  }, [q.data]);

  if (q.isError) {
    return <>
      <ErrorBanner message={`${problemText(q.error, "Couldn't check the selected records.")} Nothing was deleted.`} onRetry={() => void q.refetch()} />
      <DialogFooter><Button type="button" variant="outline" onClick={onCancel}>Close</Button></DialogFooter>
    </>;
  }
  if (!q.data) return <Loading what={`what blocks each of the ${plural(ids.length, noun, nouns)}`} rows={Math.min(ids.length, 4)} />;

  // Rows a 409 named are blocked now, whatever the preview said.
  const late = new Map((conflict?.rows ?? []).map((r) => [r.id, r]));
  const rows: DeleteRow[] = q.data.rows.map((r) => {
    const c = late.get(r.id);
    return c ? { ...r, can_delete: false, blocking_references: c.blocking_references, errors: c.errors } : r;
  });
  const blocked = rows.filter((r) => !r.can_delete);
  const deletable = rows.filter((r) => r.can_delete);
  const extras = deletable.reduce((t, r) => ({ attachments: t.attachments + r.removes.attachments, quality_exceptions: t.quality_exceptions + r.removes.quality_exceptions }), { attachments: 0, quality_exceptions: 0 });
  const goesWith = removesText(extras);
  const what = plural(deletable.length, noun, nouns);

  function deselect() {
    focusSummary.current = true;
    onDeselect(blocked.map((r) => r.id));
  }
  async function remove(e: React.FormEvent) {
    e.preventDefault();
    if (!confirmed) {
      setConfirmError(`Tick the box to confirm that ${what} ${deletable.length === 1 ? "is" : "are"} deleted.`);
      document.getElementById("bulk-delete-confirm")?.focus();
      return;
    }
    setBusy(true);
    setError("");
    try {
      const done = await api<{ deleted: number }>("bulk-delete/apply", { method: "POST", body: { kind, ids } });
      onDone(`Deleted ${plural(done.deleted, noun, nouns)}.${goesWith ? ` Their ${goesWith} went with them.` : ""} Each delete is recorded in the audit log.`);
    } catch (err) {
      setBusy(false);
      if (err instanceof Problem && err.status === 409 && Array.isArray(err.body.rows)) {
        setConflict({ detail: err.detail, rows: err.body.rows as DeleteConflictRow[] });
        setConfirmed(false);
        focusSummary.current = false;
        setTimeout(() => document.getElementById("bulk-delete-summary")?.focus());
      } else setError(`${problemText(err, "Couldn't delete the records.")} Nothing was deleted. Try again.`);
    }
  }

  return (
    <form onSubmit={remove} noValidate>
      {error && <ErrorBanner message={error} />}
      {conflict && (
        <div className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
          <p className="font-semibold">Nothing was deleted.</p>
          <p>{conflict.detail} Other records started referencing {conflict.rows.length === 1 ? "it" : "them"} after the check; they're marked Blocked below.</p>
        </div>
      )}
      <p id="bulk-delete-summary" tabIndex={-1} className="mb-1 font-semibold outline-none" role="status">
        {blocked.length === 0
          ? rows.length === 1 ? `The selected ${noun} can be deleted.` : `All ${plural(rows.length, noun, nouns)} can be deleted.`
          : `${deletable.length} of ${rows.length} can be deleted; ${plural(blocked.length, "is", "are")} blocked.`}
      </p>
      {blocked.length > 0 && (
        <p className="mb-3 text-sm">
          Nothing is deleted while any selected record is blocked: other records still reference {blocked.length === 1 ? "it" : "them"}.
          {deletable.length > 0 ? " Deselect the blocked ones to delete the rest, and archive those instead." : " Archive them instead, or remove what references them first."}
        </p>
      )}
      <TableRegion label="Bulk delete preview table" className="mb-4 max-h-[45vh] overflow-auto">
        <Table>
          <TableCaption className="sr-only">Whether each selected record can be deleted, what blocks it and what is deleted with it</TableCaption>
          <TableHeader><TableRow><TableHead scope="col">Record</TableHead><TableHead scope="col">Result</TableHead><TableHead scope="col">Details</TableHead></TableRow></TableHeader>
          <TableBody>
            {rows.map((r) => {
              const extra = removesText(r.removes);
              const refs = referencesText(r.blocking_references);
              return (
                <TableRow key={r.id} data-testid="bulk-delete-row">
                  <TableHead scope="row" className="min-w-32 whitespace-normal align-top font-semibold text-foreground [overflow-wrap:anywhere]">{r.name}</TableHead>
                  <TableCell className="align-top">
                    {r.can_delete
                      ? <span className={chip({ state: "neutral" })}><span aria-hidden="true">✓</span>Can be deleted</span>
                      : <span className={chip({ state: "pending" })}><span aria-hidden="true">✕</span>Blocked</span>}
                  </TableCell>
                  <TableCell className="whitespace-normal align-top">
                    {r.can_delete
                      ? (extra ? `Also deletes its ${extra}.` : "Nothing else is deleted with it.")
                      : refs ? <>Referenced by {refs}. Archive it instead.</> : r.errors.join(" ")}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </TableRegion>
      {blocked.length === 0 && (
        <div className="mb-3 flex flex-col gap-1.5">
          <label className="flex items-start gap-2 text-sm">
            <input id="bulk-delete-confirm" type="checkbox" checked={confirmed} onChange={(e) => { setConfirmed(e.target.checked); setConfirmError(""); }}
              className="mt-0.5 h-4 w-4 accent-primary" aria-invalid={confirmError ? true : undefined} aria-describedby={confirmError ? "bulk-delete-confirm-error" : undefined} />
            <span>Permanently delete {what}{goesWith ? `, with their ${goesWith}` : ""}. This can't be undone.</span>
          </label>
          {confirmError && <span id="bulk-delete-confirm-error" className="text-sm text-error-foreground">{confirmError}</span>}
        </div>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        {blocked.length > 0 && deletable.length > 0 && (
          <Button type="button" variant="outline" onClick={deselect}>Deselect blocked ({blocked.length})</Button>
        )}
        {blocked.length === 0 && (
          <Button type="submit" variant="destructive" disabled={busy}>{busy ? "Deleting…" : `Delete ${what}`}</Button>
        )}
      </DialogFooter>
    </form>
  );
}
