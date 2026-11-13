/** Custom fields, `/custom-fields`, loaded on first use (React.lazy in App.tsx).
 *
 *  Every role reads the definitions; Admins add, change and delete them (the API's policy). The list is filtered by kind
 *  (`applies_to`) and type on the server and by a search over label, name and description here, all in the URL, in the
 *  API's order (weight, then name). A field's name never changes; its type is locked while records hold a value, and
 *  removing a choice or a kind those values use is refused by the API with a count, shown on the field it concerns.
 *  Deleting a field removes its values everywhere and drops it from saved filters, so the dialog says how many records
 *  lose a value and asks for a checkbox confirmation when any do. */
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, Problem } from "@/api";
import { Chip, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, Select, TableRegion, Textarea } from "@/components/app";
import { CheckboxPicker } from "@/components/checkbox-picker";
import { FilterBar } from "@/components/filter-bar";
import { SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Modal } from "@/features/admin/admin";
import {
  CF_CAPABILITY, CF_KIND_PLURAL, CF_KINDS, cfKindsFor, CF_TYPE_LABEL, CF_TYPES, CustomFieldInput, fromDraft, isChoiceType, toDraft, valueText,
  type CFDraft, type CFKind, type CFType, type CustomField,
} from "@/features/catalog/custom-fields";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { useMe } from "@/lib/session";

const records = (n: number) => `${n} ${n === 1 ? "record" : "records"}`;
/** "1 record holds", "3 records hold". */
const hold = (n: number) => `${records(n)} ${n === 1 ? "holds" : "hold"}`;
/** Everything a field's values show up in: refreshed after a definition changes or goes. */
const VALUE_PATHS = ["custom-fields", "applications", "components", "ai-agents", "integrations", "vendors", "saved-filters"];
const ID = (k: string) => `cfd-${k}`;

export function CustomFieldsPage() {
  const me = useMe();
  const admin = me.role === "admin";
  const capable = me.capabilities.includes(CF_CAPABILITY);
  const [params, setParams] = useSearchParams();
  const values = { applies_to: params.get("applies_to") ?? "", type: params.get("type") ?? "" };
  const q = params.get("q") ?? "";
  const apiQuery = new URLSearchParams(Object.entries(values).filter(([, v]) => v)).toString();
  const list = useApiQuery<CustomField[]>(`custom-fields${apiQuery ? `?${apiQuery}` : ""}`, { enabled: capable });
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState<CustomField | "new" | null>(null);
  const [deleting, setDeleting] = useState<CustomField | null>(null);
  const [notice, setNotice] = useState("");
  // The search and typed chips replace the history entry; a discrete filter change pushes one.
  const set = (k: string, v: string, typed = false) => setParams((cur) => { const n = new URLSearchParams(cur); if (v) n.set(k, v); else n.delete(k); return n; }, { replace: k === "q" || typed });
  const filtered = !!(apiQuery || q);
  const clear = () => setParams({});
  // After a delete the row, and with it the Delete button that opened the dialog, goes once the list refetches: then
  // focus the page heading rather than leaving it on the body.
  const [refocus, setRefocus] = useState(false);
  useEffect(() => {
    if (!refocus) return;
    const t = setTimeout(() => {
      if (!document.activeElement || document.activeElement === document.body) { document.querySelector<HTMLElement>("main h1")?.focus(); setRefocus(false); }
    }, 250);
    return () => clearTimeout(t);
  }, [refocus, list.data]);
  const done = (text: string, deleted = false) => {
    setEditing(null); setDeleting(null); setNotice(text);
    if (deleted) setRefocus(true);
    void invalidate(...VALUE_PATHS);
  };
  const needle = q.trim().toLowerCase();
  const rows = list.data?.filter((f) => !needle || `${f.label} ${f.name} ${f.description}`.toLowerCase().includes(needle));

  if (!capable) {
    return (
      <>
        <PageHead title="Custom fields" />
        <EmptyState title="Custom fields aren't available">This organization's edition doesn't include custom fields.</EmptyState>
      </>
    );
  }
  const badFilter = list.error instanceof Problem && list.error.status === 400;
  return (
    <>
      <PageHead title="Custom fields" desc="Fields your organization adds to applications, components, integrations, AI agents and vendors. Lower weights come first in forms.">
        {admin && <Button onClick={() => setEditing("new")}>Add custom field</Button>}
      </PageHead>
      {!admin && <p className="mb-3 text-sm text-muted-foreground">You can view custom fields. Admins add, change and delete them.</p>}
      <FilterBar id="fields" searchLabel="Search custom fields" searchPlaceholder="Label, name or description" search={q} onSearch={(v) => set("q", v)}
        fields={[
          { key: "applies_to", label: "Applies to", options: CF_KINDS.map((k) => ({ value: k.value, label: k.plural })) },
          { key: "type", label: "Type", options: CF_TYPES },
        ]}
        values={values} onChange={set} filtered={filtered} onClear={clear} />
      <SuccessNote text={notice} />
      {list.isError && (badFilter ? (
        <div className="mb-4 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
          <p className="font-semibold">Couldn't show custom fields with these filters. Nothing was changed.</p>
          <ul className="ml-5 mt-1 list-disc">{Object.entries((list.error as Problem).errors).map(([k, v]) => <li key={k}><code className="font-mono text-[13px]">{k}</code>: {v.join(" ")}</li>)}</ul>
          <div className="mt-2"><Button size="sm" variant="outline" onClick={clear}>Clear filters</Button></div>
        </div>
      ) : <ErrorBanner message={`${problemText(list.error, "Couldn't load custom fields.")} Nothing was changed.`} onRetry={() => void list.refetch()} />)}
      {list.isPending && <Loading what="custom fields" rows={4} />}
      {rows && rows.length === 0 && (filtered ? (
        <EmptyState title="No custom fields match these filters" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>Try another kind or type, or clear the filters.</EmptyState>
      ) : (
        <EmptyState title="No custom fields yet" action={admin ? <Button variant="outline" onClick={() => setEditing("new")}>Add custom field</Button> : undefined}>
          {admin
            ? "Add a field for what your organization tracks beyond the built-in ones, such as a business case, an annual cost or a certificate expiry date. It appears in the forms, detail pages, tables, filters and exports of the kinds you choose."
            : "Admins add fields here for what the organization tracks beyond the built-in ones. They then appear in forms, detail pages, tables and filters."}
        </EmptyState>
      ))}
      {rows && rows.length > 0 && (
        <TableRegion label="Custom fields table">
          <Table>
            <TableCaption className="sr-only">Custom fields, by weight, then name</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Label</TableHead><TableHead scope="col">Name</TableHead><TableHead scope="col">Type</TableHead>
              <TableHead scope="col">Applies to</TableHead><TableHead scope="col">Required</TableHead><TableHead scope="col">Default</TableHead>
              <TableHead scope="col">Weight</TableHead><TableHead scope="col">Values</TableHead>
              {admin && <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>}
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((f) => (
                <TableRow key={f.id}>
                  <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                    {f.label}
                    {f.description && <p className="mt-0.5 text-[13px] font-normal text-muted-foreground">{f.description}</p>}
                  </TableHead>
                  <TableCell><code className="font-mono text-[13px]">{f.name}</code></TableCell>
                  <TableCell className="whitespace-normal">
                    {CF_TYPE_LABEL[f.type] ?? f.type}
                    {isChoiceType(f.type) && <p className="text-[13px] text-muted-foreground">{f.choices.join(", ")}</p>}
                    {f.is_reminder_source && <Chip className="mt-1">Reminder source</Chip>}
                  </TableCell>
                  <TableCell className="whitespace-normal">{f.applies_to.map((k) => CF_KIND_PLURAL[k] ?? k).join(", ")}</TableCell>
                  <TableCell>{f.required ? "Required" : "Optional"}</TableCell>
                  <TableCell className="max-w-48 whitespace-normal [overflow-wrap:anywhere]">{f.default === null || f.default === undefined ? <span className="text-muted-foreground">None</span> : valueText(f, f.default)}</TableCell>
                  <TableCell className="tabular">{f.weight}</TableCell>
                  <TableCell>{f.usage ? records(f.usage) : <span className="text-muted-foreground">No values</span>}</TableCell>
                  {admin && (
                    <TableCell className="space-x-2 whitespace-nowrap text-right">
                      <Button size="sm" variant="outline" onClick={() => setEditing(f)}>Edit<span className="sr-only"> {f.label}</span></Button>
                      <Button size="sm" variant="outline" onClick={() => setDeleting(f)}>Delete<span className="sr-only"> {f.label}</span></Button>
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <Modal title={editing === "new" ? "Add custom field" : `Edit ${editing ? editing.label : ""}`} open={editing !== null} onClose={() => setEditing(null)}
        description={editing === "new" ? "The name is how the API, exports and filters refer to the field; it can't change later. The label can." : "The name can't change. Saving refreshes every form and list that shows the field."}>
        {editing === "new" && <FieldForm field={null} onCancel={() => setEditing(null)} onDone={done} />}
        {editing && editing !== "new" && (
          <OpenedEdit<CustomField> path={`custom-fields/${editing.id}`} what="custom field">
            {(f, write) => <FieldForm field={f} write={write} onCancel={() => setEditing(null)} onDone={done} />}
          </OpenedEdit>
        )}
      </Modal>
      <Modal title={`Delete ${deleting?.label ?? ""}?`} open={deleting !== null} onClose={() => setDeleting(null)}
        description="Deleting a custom field can't be undone. The audit log keeps a record, and each record's history keeps the values it had.">
        {deleting && <DeleteFieldForm field={deleting} onCancel={() => setDeleting(null)} onDone={(text) => done(text, true)} />}
      </Modal>
    </>
  );
}

// ---- add and edit ----------------------------------------------------------------------------------------------------------

/** A label as a field name: "Annual cost (EUR)" → "annual_cost_eur". */
export function slugOf(label: string) {
  const s = label.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 50).replace(/_+$/, "");
  return /^[a-z]/.test(s) ? s : s ? `field_${s}`.slice(0, 50) : "";
}

type FormState = {
  label: string; name: string; type: CFType; description: string; choices: string; required: boolean; default: CFDraft;
  applies_to: string[]; weight: string; is_reminder_source: boolean;
};

const choiceList = (text: string) => [...new Set(text.split("\n").map((c) => c.trim()).filter(Boolean))];
const sameList = (a: unknown[], b: unknown[]) => JSON.stringify(a) === JSON.stringify(b);

function FieldForm({ field, write, onCancel, onDone }: { field: CustomField | null; write?: OpenedWrite; onCancel: () => void; onDone: (text: string) => void }) {
  const me = useMe();
  const [form, setForm] = useState<FormState>(() => ({
    label: field?.label ?? "", name: field?.name ?? "", type: field?.type ?? "text", description: field?.description ?? "",
    choices: (field?.choices ?? []).join("\n"), required: field?.required ?? false, default: toDraft({ type: field?.type ?? "text" }, field?.default ?? null),
    applies_to: field?.applies_to ?? [], weight: String(field?.weight ?? 100), is_reminder_source: field?.is_reminder_source ?? false,
  }));
  // The name follows the label until it is typed in (create only: it never changes after).
  const [nameTyped, setNameTyped] = useState(false);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);
  const usage = field?.usage ?? 0;
  const locked = !!field && usage > 0;
  const choices = choiceList(form.choices);
  const choiceType = isChoiceType(form.type);
  const put = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((cur) => ({ ...cur, [k]: v }));

  function setLabel(label: string) {
    setForm((cur) => ({ ...cur, label, name: field || nameTyped ? cur.name : slugOf(label) }));
  }
  function setType(type: CFType) {
    // A default for another type wouldn't fit: it starts blank again.
    setForm((cur) => ({ ...cur, type, default: toDraft({ type }, null), is_reminder_source: type === "date" ? cur.is_reminder_source : false }));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const found: Record<string, string[]> = {};
    if (!form.label.trim()) found.label = ["Enter a label."];
    if (!field && !form.name.trim()) found.name = ["Enter a name: lowercase letters, digits and underscores."];
    if (choiceType && !choices.length) found.choices = ["A choice field needs at least one choice."];
    if (!form.applies_to.length) found.applies_to = ["Choose at least one kind of record."];
    const weight = Number(form.weight);
    if (!/^\d+$/.test(form.weight.trim()) || weight > 32767) found.weight = ["Use a whole number from 0 to 32767."];
    const def = fromDraft({ type: form.type }, form.default);
    if ("error" in def) found.default = [def.error];
    if (Object.keys(found).length) { setErrors(Object.fromEntries(Object.entries(found).map(([k, v]) => [ID(k), v]))); return; }
    const value = "value" in def ? def.value : null;
    const next = {
      label: form.label.trim(), type: form.type, description: form.description.trim(), choices: choiceType ? choices : [],
      required: form.required, default: value, applies_to: CF_KINDS.map((k) => k.value).filter((k) => form.applies_to.includes(k)),
      weight, is_reminder_source: form.type === "date" && form.is_reminder_source,
    };
    let body: Record<string, unknown>;
    if (!field) {
      body = { name: form.name.trim(), ...next };
      if (!choiceType) delete body.choices;
    } else {
      // A PATCH sends only what changed, so an unchanged type or kind list is never re-checked against stored values.
      const before: Record<string, unknown> = {
        label: field.label, type: field.type, description: field.description, choices: field.choices, required: field.required,
        default: field.default ?? null, applies_to: field.applies_to, weight: field.weight, is_reminder_source: field.is_reminder_source,
      };
      body = Object.fromEntries(Object.entries(next).filter(([k, v]) => k !== "default" && !sameList([v], [before[k]])));
      // The default is compared as the form holds it: a whole number typed as "5" is the stored 5, not a change.
      if (JSON.stringify(form.default) !== JSON.stringify(toDraft(field, field.default ?? null))) body.default = next.default;
      if (!Object.keys(body).length) { onDone(`No changes to ${field.label}.`); return; }
    }
    setBusy(true);
    setErrors({});
    setFormError("");
    try {
      const saved = await api<CustomField>(field ? `custom-fields/${field.id}` : "custom-fields", { method: field ? "PATCH" : "POST", body, headers: await write?.ifMatch() });
      onDone(field ? `${saved.label} saved.` : `${saved.label} added.`);
    } catch (err) {
      setBusy(false);
      if (isStaleWrite(err) && write) write.onStale();
      else if (err instanceof Problem && Object.keys(err.errors).length) {
        const { non_field: general, ...rest } = err.errors;
        setErrors(Object.fromEntries(Object.entries(rest).map(([k, v]) => [ID(k), v])));
        if (general) setFormError(general.join(" "));
      } else setFormError(`${problemText(err, "Something went wrong.")} Nothing was saved.`);
    }
  }

  const kindHelp = locked
    ? `${hold(usage)} a value. Removing a kind whose records hold values is refused; clear those values first.`
    : "The kinds of record the field appears on.";
  return (
    <form onSubmit={submit} noValidate aria-label={field ? `Edit ${field.label}` : "Add custom field"}>
      <fieldset disabled={busy} className="m-0 min-w-0 border-0 p-0">
        {formError && <ErrorBanner message={formError} />}
        <ErrorSummary errors={errors} focusOnShow="field" />
        <Field id={ID("label")} label="Label (required)" errors={errors} help="What people see, for example Business case. Unique in the organization.">
          <Input id={ID("label")} value={form.label} onChange={(e) => setLabel(e.target.value)} autoComplete="off" />
        </Field>
        {field ? (
          <Field id={ID("name")} label="Name" errors={errors} help="Can't be changed: the API, CSV columns (cf_name) and filters use it.">
            <Input id={ID("name")} value={field.name} readOnly className="font-mono" />
          </Field>
        ) : (
          <Field id={ID("name")} label="Name (required)" errors={errors} help="Lowercase letters, digits and single underscores, starting with a letter. Can't be changed later.">
            <Input id={ID("name")} value={form.name} onChange={(e) => { setNameTyped(true); put("name", e.target.value); }} autoComplete="off" spellCheck={false} className="font-mono" />
          </Field>
        )}
        {locked ? (
          <Field id={ID("type")} label="Type" errors={errors} help={`Locked: ${hold(usage)} a value for ${field!.label}, so its type can't change. Clear the values first, or add a new field.`}>
            <Input id={ID("type")} value={CF_TYPE_LABEL[field!.type]} readOnly />
          </Field>
        ) : (
          <Field id={ID("type")} label="Type (required)" errors={errors}>
            <Select id={ID("type")} value={form.type} onChange={(e) => setType(e.target.value as CFType)}>
              {CF_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
            </Select>
          </Field>
        )}
        {choiceType && (
          <Field id={ID("choices")} label="Choices (required)" errors={errors}
            help={locked ? "One per line. Records use some of these choices: removing one in use is refused, with a count." : "One per line, in the order they're offered. No commas or semicolons."}>
            <Textarea id={ID("choices")} value={form.choices} onChange={(e) => put("choices", e.target.value)} rows={4} />
          </Field>
        )}
        <Field id={ID("description")} label="Description" errors={errors} help="Optional help text shown under the field in forms.">
          <Textarea id={ID("description")} value={form.description} onChange={(e) => put("description", e.target.value)} rows={2} />
        </Field>
        <CheckboxPicker id={ID("applies_to")} legend="Applies to (required)"
          options={cfKindsFor(me.capabilities).concat(CF_KINDS.filter((k) => form.applies_to.includes(k.value) && !cfKindsFor(me.capabilities).includes(k))).map((k) => ({ id: k.value, label: k.plural }))}
          value={form.applies_to} onChange={(v) => put("applies_to", v as CFKind[])} errors={errors[ID("applies_to")]} help={kindHelp} columns />
        <CustomFieldInput id={ID("default")} label="Default" errors={errors}
          cf={{ name: "default", type: form.type, label: "Default", choices, required: false, description: "" }}
          draft={form.default} onChange={(d) => put("default", d)}
          help="Optional. Filled in on new records; leave blank for none." />
        <div className="mb-3 flex flex-col gap-1.5">
          <label className="flex items-center gap-2 text-sm">
            <input id={ID("required")} type="checkbox" checked={form.required} onChange={(e) => put("required", e.target.checked)} className="h-4 w-4 accent-primary"
              aria-invalid={errors[ID("required")] ? true : undefined}
              aria-describedby={[errors[ID("required")] && `${ID("required")}-error`, `${ID("required")}-help`].filter(Boolean).join(" ")} />
            Required
          </label>
          <span id={`${ID("required")}-help`} className="text-sm text-muted-foreground">New records need a value, and a value can't be cleared. Existing records without one can still be edited.</span>
          {errors[ID("required")] && <span id={`${ID("required")}-error`} className="text-sm text-error-foreground">{errors[ID("required")].join(" ")}</span>}
        </div>
        {form.type === "date" && (
          <div className="mb-3 flex flex-col gap-1.5">
            <label className="flex items-center gap-2 text-sm">
              <input id={ID("is_reminder_source")} type="checkbox" checked={form.is_reminder_source} onChange={(e) => put("is_reminder_source", e.target.checked)}
                className="h-4 w-4 accent-primary" aria-invalid={errors[ID("is_reminder_source")] ? true : undefined}
                aria-describedby={[errors[ID("is_reminder_source")] && `${ID("is_reminder_source")}-error`, `${ID("is_reminder_source")}-help`].filter(Boolean).join(" ")} />
              Reminder source
            </label>
            <span id={`${ID("is_reminder_source")}-help`} className="text-sm text-muted-foreground">The date can drive reminders, such as a certificate expiry.</span>
            {errors[ID("is_reminder_source")] && <span id={`${ID("is_reminder_source")}-error`} className="text-sm text-error-foreground">{errors[ID("is_reminder_source")].join(" ")}</span>}
          </div>
        )}
        <Field id={ID("weight")} label="Weight" errors={errors} help="Lower first, from 0 to 32767. New fields get 100.">
          <Input id={ID("weight")} inputMode="numeric" value={form.weight} onChange={(e) => put("weight", e.target.value)} className="w-32" />
        </Field>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
          <Button type="submit">{busy ? "Saving…" : field ? "Save" : "Add custom field"}</Button>
        </DialogFooter>
      </fieldset>
    </form>
  );
}

// ---- delete ---------------------------------------------------------------------------------------------------------------

function DeleteFieldForm({ field, onCancel, onDone }: { field: CustomField; onCancel: () => void; onDone: (text: string) => void }) {
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmError, setConfirmError] = useState("");
  // Read with its ETag as the confirmation opens; the delete sends it as If-Match.
  const opened = useOpenedRecord(`custom-fields/${field.id}`);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (field.usage > 0 && !confirmed) { setConfirmError(`Tick the box to confirm that the values are removed from ${records(field.usage)}.`); document.getElementById("cfd-confirm")?.focus(); return; }
    setBusy(true);
    setError("");
    try {
      await api(`custom-fields/${field.id}`, { method: "DELETE", headers: ifMatch(await opened.etag()) });
      onDone(field.usage ? `${field.label} deleted; ${records(field.usage)} no longer ${field.usage === 1 ? "holds" : "hold"} a value.` : `${field.label} deleted.`);
    } catch (err) {
      setBusy(false);
      setError(isStaleWrite(err) ? staleDeleteText("custom field") : `${problemText(err, "Couldn't delete the field.")} Nothing was deleted.`);
    }
  }
  return (
    <form onSubmit={submit} noValidate>
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
      <div className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" data-testid="delete-consequences">
        {field.usage > 0
          ? <p><span aria-hidden="true">⚠ </span><strong>{hold(field.usage)}</strong> a value for {field.label}. Deleting the field removes those values.</p>
          : <p>No record holds a value for {field.label}.</p>}
        <p className="mt-1">Saved filters that use {field.label} are updated to leave it out.</p>
      </div>
      {field.usage > 0 && (
        <div className="mb-3 flex flex-col gap-1.5">
          <label className="flex items-center gap-2 text-sm">
            <input id="cfd-confirm" type="checkbox" checked={confirmed} onChange={(e) => { setConfirmed(e.target.checked); setConfirmError(""); }} className="h-4 w-4 accent-primary"
              aria-invalid={confirmError ? true : undefined} aria-describedby={confirmError ? "cfd-confirm-error" : undefined} />
            Remove the {field.label} values from {records(field.usage)}
          </label>
          {confirmError && <span id="cfd-confirm-error" className="text-sm text-error-foreground">{confirmError}</span>}
        </div>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="destructive" disabled={busy}>{busy ? "Deleting…" : `Delete ${field.label}`}</Button>
      </DialogFooter>
    </form>
  );
}
