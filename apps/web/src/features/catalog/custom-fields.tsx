/** Custom fields in records: the definitions an Admin keeps on /custom-fields, rendered in the record,
 *  integration and vendor forms (a typed input per field, in weight order), on detail pages (the Custom fields panel),
 *  as optional `cf_<name>` table columns and as `cf_<name>` filters.
 *
 *  The API is the contract (catalog/customfields.py): a record returns `custom_field_data` with every field that
 *  applies, in weight order, null when unset; a write sends only the names that change (null clears one); an error is
 *  a field error keyed `custom_field_data.<name>`, which is also the ID of the field's input here, so the error summary
 *  links straight to it. The admin page itself is lazy (features/admin/custom-fields-admin.tsx). */
import { useState } from "react";
import { Field, Select, Textarea } from "@/components/app";
import { CheckboxPicker } from "@/components/checkbox-picker";
import type { FilterField } from "@/components/filter-bar";
import type { RecordColumn } from "@/components/record-table";
import { chip, DetailList, DetailRow, Panel } from "@/components/sw";
import { Input } from "@/components/ui/input";
import { useApiQuery } from "@/lib/query";
import { useMe } from "@/lib/session";

export type CFType = "text" | "longtext" | "integer" | "decimal" | "boolean" | "date" | "url" | "select" | "multiselect" | "object";
/** The kinds a field can apply to (the API's CUSTOM_FIELD_KINDS). Contracts and licence pools, with the
 *  `contracts` capability. */
export type CFKind = "application" | "component" | "integration" | "ai_agent" | "vendor" | "contract" | "license_pool";
export type CustomField = {
  id: string; name: string; label: string; type: CFType; description: string; choices: string[]; required: boolean;
  default: unknown; applies_to: CFKind[]; weight: number; is_reminder_source: boolean;
  /** Records and vendors holding a value. */
  usage: number;
};
export type CFValues = Record<string, unknown>;

export const CF_KEY = "custom_field_data";
/** The input ID and the API's error key for a field's value. */
export const cfId = (name: string) => `${CF_KEY}.${name}`;
export const CF_CAPABILITY = "custom_fields";

export const CF_TYPES: { value: CFType; label: string }[] = [
  { value: "text", label: "Text (one line)" }, { value: "longtext", label: "Long text" }, { value: "integer", label: "Whole number" },
  { value: "decimal", label: "Decimal number" }, { value: "boolean", label: "Yes / no" }, { value: "date", label: "Date" },
  { value: "url", label: "Web address (URL)" }, { value: "select", label: "Choice (one)" }, { value: "multiselect", label: "Choices (any number)" },
  { value: "object", label: "JSON object" },
];
export const CF_TYPE_LABEL = Object.fromEntries(CF_TYPES.map((t) => [t.value, t.label])) as Record<CFType, string>;
export const CF_KINDS: { value: CFKind; label: string; plural: string }[] = [
  { value: "application", label: "Application", plural: "Applications" }, { value: "component", label: "Component", plural: "Components" },
  { value: "integration", label: "Integration", plural: "Integrations" }, { value: "ai_agent", label: "AI agent", plural: "AI agents" },
  { value: "vendor", label: "Vendor", plural: "Vendors" },
  { value: "contract", label: "Contract", plural: "Contracts" }, { value: "license_pool", label: "License pool", plural: "License pools" },
];
/** The kinds a field may newly apply to here: contracts and licence pools need the `contracts` capability. */
export const cfKindsFor = (capabilities: string[]) => CF_KINDS.filter((k) => (k.value !== "contract" && k.value !== "license_pool") || capabilities.includes("contracts"));
export const CF_KIND_PLURAL = Object.fromEntries(CF_KINDS.map((k) => [k.value, k.plural])) as Record<CFKind, string>;
export const isChoiceType = (t: CFType) => t === "select" || t === "multiselect";

/** The fields that apply to a kind, in weight order (GET custom-fields?applies_to=<kind>, every role). Nothing while the
 *  organization lacks the capability. */
export function useCustomFields(kind: CFKind, enabled = true) {
  const me = useMe();
  const on = enabled && me.capabilities.includes(CF_CAPABILITY);
  const q = useApiQuery<CustomField[]>(`custom-fields?applies_to=${kind}`, { enabled: on, staleTime: 30_000 });
  return {
    fields: on ? q.data : ([] as CustomField[]),
    pending: on && q.isPending,
    error: on && q.isError ? q.error : null,
    refetch: () => { if (on) void q.refetch(); },
  };
}

// ---- values ------------------------------------------------------------------------------------------------------------

/** What a form holds for one field: text for most types ("" is blank), a yes / no / not set answer for yes/no fields,
 *  the chosen choices for a multiple-choice field, and the JSON as typed for an object. */
export type CFDraft = string | string[];

export function toDraft(cf: Pick<CustomField, "type">, value: unknown): CFDraft {
  if (cf.type === "multiselect") return Array.isArray(value) ? value.map(String) : [];
  if (value === null || value === undefined) return "";
  if (cf.type === "boolean") return value === true ? "true" : value === false ? "false" : "";
  if (cf.type === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

export const OBJECT_HINT = 'Enter a JSON object, such as {"region": "EU"}.';

/** The value to send for a draft (null clears), or the message for a JSON object that doesn't parse. Whole and decimal
 *  numbers are sent as typed: the API reads them and answers with its own message ("Enter a whole number."). */
export function fromDraft(cf: Pick<CustomField, "type">, draft: CFDraft): { value: unknown } | { error: string } {
  if (Array.isArray(draft)) return { value: draft.length ? draft : null };
  const s = draft.trim();
  switch (cf.type) {
    case "boolean": return { value: draft === "true" ? true : draft === "false" ? false : null };
    case "longtext": return { value: s ? draft.replace(/\s+$/, "").replace(/^\s*\n/, "") : null };
    case "object": {
      if (!s) return { value: null };
      try {
        const parsed: unknown = JSON.parse(s);
        if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return { error: OBJECT_HINT };
        return { value: parsed };
      } catch {
        return { error: `This isn't valid JSON. ${OBJECT_HINT}` };
      }
    }
    default: return { value: s || null };
  }
}

const same = (a: CFDraft, b: CFDraft) => JSON.stringify(a) === JSON.stringify(b);

/** A form's custom field values: each starts from the record (edit) or the field's default (create); the payload to send
 *  and the client-side errors (a JSON object that doesn't parse). `fields` may change while the form is open (the create
 *  form's Kind switch): a field keeps what was typed in it, a new one starts from its default. */
export function useCustomFieldForm(fields: CustomField[], mode: "create" | "edit", stored?: CFValues | null) {
  const initialOf = (cf: CustomField) => toDraft(cf, mode === "create" ? cf.default : stored?.[cf.name]);
  const [edited, setEdited] = useState<Record<string, CFDraft>>({});
  const draftOf = (cf: CustomField) => (cf.name in edited ? edited[cf.name] : initialOf(cf));
  const drafts = Object.fromEntries(fields.map((cf) => [cf.name, draftOf(cf)]));
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const set = (name: string, draft: CFDraft) => setEdited((cur) => ({ ...cur, [name]: draft }));
  /** Check one field (on blur): only a JSON object can be refused before the API sees it. */
  function check(name: string) {
    const cf = fields.find((f) => f.name === name);
    if (!cf || cf.type !== "object") return;
    const out = fromDraft(cf, draftOf(cf));
    setErrors((cur) => {
      const { [cfId(name)]: _drop, ...rest } = cur;
      return "error" in out ? { ...rest, [cfId(name)]: [out.error] } : rest;
    });
  }
  const changed = (cf: CustomField) => !same(draftOf(cf), initialOf(cf));
  /** `data`: the partial object to send as custom_field_data, or undefined when nothing changed. Create sends what has
   *  a value, plus null for a default the user cleared (left out, the API would apply the default again); edit sends
   *  only the names that changed. `errors` are the client-side ones (also kept for the inputs). */
  function build(): { data?: CFValues; errors: Record<string, string[]> } {
    const data: CFValues = {};
    const found: Record<string, string[]> = {};
    for (const cf of fields) {
      if (mode === "edit" && !changed(cf)) continue;
      const out = fromDraft(cf, draftOf(cf));
      if ("error" in out) { found[cfId(cf.name)] = [out.error]; continue; }
      if (mode === "create" && out.value === null && (cf.default === null || cf.default === undefined)) continue;
      data[cf.name] = out.value;
    }
    setErrors(found);
    return { data: Object.keys(data).length ? data : undefined, errors: found };
  }
  /** The fields that differ from where the form started, as label and value in words (a 412's "Your unsaved values"). */
  function changes(): { label: string; value: string }[] {
    return fields.filter(changed).map((cf) => {
      const out = fromDraft(cf, drafts[cf.name]);
      return { label: cf.label, value: "error" in out ? String(drafts[cf.name]) : out.value === null ? "(blank)" : valueText(cf, out.value) };
    });
  }
  const dirty = fields.some(changed);
  return { fields, drafts, set, check, build, changes, dirty, errors };
}
export type CustomFieldForm = ReturnType<typeof useCustomFieldForm>;

// ---- inputs ------------------------------------------------------------------------------------------------------------

const HINT: Partial<Record<CFType, string>> = {
  integer: "A whole number, for example 12.",
  decimal: "A number, for example 12.5.",
  url: "A full address starting with https://.",
  object: OBJECT_HINT,
};

/** One field's input, labelled with the field's label (and "(required)"), its description as help text. `id` defaults
 *  to the API's error key. Also the admin page's Default input (with a definition being edited). */
export function CustomFieldInput({ cf, draft, onChange, onBlur, errors, id = cfId(cf.name), label, help }: {
  cf: Pick<CustomField, "name" | "type" | "label" | "choices" | "required" | "description">; draft: CFDraft; onChange: (d: CFDraft) => void; onBlur?: () => void;
  errors: Record<string, string[]>; id?: string; label?: string; help?: React.ReactNode;
}) {
  const text = typeof draft === "string" ? draft : "";
  const name = label ?? `${cf.label}${cf.required ? " (required)" : ""}`;
  const helpText = help ?? ([cf.description, HINT[cf.type]].filter(Boolean).join(" ") || undefined);
  const on = (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => onChange(e.target.value);
  if (cf.type === "multiselect") {
    return (
      <CheckboxPicker id={id} legend={name} options={cf.choices.map((c) => ({ id: c, label: c }))} value={Array.isArray(draft) ? draft : []}
        onChange={(v) => onChange(cf.choices.filter((c) => v.includes(c)))} errors={errors[id]} help={helpText} columns={cf.choices.length > 3} />
    );
  }
  const control = (() => {
    switch (cf.type) {
      case "longtext": return <Textarea id={id} value={text} onChange={on} onBlur={onBlur} rows={4} />;
      case "object": return <Textarea id={id} value={text} onChange={on} onBlur={onBlur} rows={5} spellCheck={false} className="font-mono" />;
      case "integer": return <Input id={id} inputMode="numeric" value={text} onChange={on} onBlur={onBlur} autoComplete="off" />;
      case "decimal": return <Input id={id} inputMode="decimal" value={text} onChange={on} onBlur={onBlur} autoComplete="off" />;
      case "date": return <Input id={id} type="date" value={text} onChange={on} onBlur={onBlur} />;
      case "url": return <Input id={id} type="url" inputMode="url" placeholder="https://…" value={text} onChange={on} onBlur={onBlur} />;
      case "boolean": return (
        <Select id={id} value={text} onChange={on} onBlur={onBlur}>
          <option value="">Not set</option><option value="true">Yes</option><option value="false">No</option>
        </Select>
      );
      case "select": return (
        <Select id={id} value={text} onChange={on} onBlur={onBlur}>
          <option value="">Not set</option>
          {cf.choices.map((c) => <option key={c} value={c}>{c}</option>)}
          {text && !cf.choices.includes(text) && <option value={text}>{text}</option>}
        </Select>
      );
      default: return <Input id={id} value={text} onChange={on} onBlur={onBlur} autoComplete="off" />;
    }
  })();
  return <Field id={id} label={name} errors={errors} help={helpText}>{control}</Field>;
}

/** The form section: a heading and one input per field, in weight order. Long text, JSON and multiple choices span
 *  both columns. Nothing when no field applies. */
export function CustomFieldInputs({ form, errors, heading = true, idPrefix = "" }: {
  form: CustomFieldForm; errors: Record<string, string[]>; heading?: boolean;
  /** Prefix for the input IDs (and so the error keys looked up), for a form opened over another one. */
  idPrefix?: string;
}) {
  if (!form.fields.length) return null;
  const own = idPrefix ? Object.fromEntries(Object.entries(form.errors).map(([k, v]) => [`${idPrefix}${k}`, v])) : form.errors;
  const shown = { ...own, ...errors };
  return <>
    {heading && (
      <div className="mb-3 mt-6 border-t pt-4 md:col-span-2">
        <h2 className="font-display text-section font-semibold">Custom fields</h2>
        <p className="mt-0.5 text-sm text-muted-foreground">Fields your organization's Admins added.</p>
      </div>
    )}
    {form.fields.map((cf) => {
      const wide = cf.type === "longtext" || cf.type === "object" || cf.type === "multiselect";
      return (
        <div key={cf.name} className={wide ? "md:col-span-2" : undefined}>
          <CustomFieldInput cf={cf} id={`${idPrefix}${cfId(cf.name)}`} draft={form.drafts[cf.name]} onChange={(d) => form.set(cf.name, d)} onBlur={() => form.check(cf.name)} errors={shown} />
        </div>
      );
    })}
  </>;
}

// ---- values as text --------------------------------------------------------------------------------------------------

const numberText = (v: unknown, places: number) => (typeof v === "number" ? new Intl.NumberFormat("en-US", { maximumFractionDigits: places }).format(v) : String(v));

/** A value in words, for summaries: "Yes", "12,500", "Gold, Silver". */
export function valueText(cf: Pick<CustomField, "type">, value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not set";
  switch (cf.type) {
    case "boolean": return value ? "Yes" : "No";
    case "integer": return numberText(value, 0);
    case "decimal": return numberText(value, 6);
    case "multiselect": return Array.isArray(value) ? value.join(", ") : String(value);
    case "object": return JSON.stringify(value);
    default: return String(value);
  }
}

const NOT_SET = <span className="text-muted-foreground">Not set</span>;

/** A value as shown on a detail page (`compact` in a table cell: long text and JSON cut short, one line). */
export function CFValue({ cf, value, compact = false }: { cf: Pick<CustomField, "type" | "label">; value: unknown; compact?: boolean }) {
  if (value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length)) return NOT_SET;
  switch (cf.type) {
    case "url": {
      const url = String(value);
      return /^https?:\/\//i.test(url)
        ? <a href={url} target="_blank" rel="noreferrer noopener" className="[overflow-wrap:anywhere]">{url}<span className="sr-only"> (opens in a new tab)</span></a>
        : <span className="[overflow-wrap:anywhere]">{url}</span>;
    }
    case "date": return <time dateTime={String(value)} className="tabular">{String(value)}</time>;
    case "integer": case "decimal": return <span className="tabular">{valueText(cf, value)}</span>;
    case "multiselect":
      return (
        <ul aria-label={cf.label} className="m-0 flex list-none flex-wrap gap-1 p-0">
          {(value as unknown[]).map((v) => <li key={String(v)}><span className={chip({ state: "neutral" })}>{String(v)}</span></li>)}
        </ul>
      );
    case "longtext": {
      const s = String(value);
      if (compact) return <span title={s}>{s.length > 80 ? `${s.slice(0, 80).trimEnd()}…` : s}</span>;
      return <span className="block max-w-prose whitespace-pre-line [overflow-wrap:anywhere]">{s}</span>;
    }
    case "object": {
      if (compact) {
        const s = JSON.stringify(value);
        return <code className="font-mono text-[13px]">{s.length > 60 ? `${s.slice(0, 60)}…` : s}</code>;
      }
      return <pre role="region" className="max-h-72 overflow-auto rounded-md border bg-table-head p-2 font-mono text-[13px] leading-5" tabIndex={0} aria-label={`${cf.label}, JSON`}>{JSON.stringify(value, null, 2)}</pre>;
    }
    default: return <span className="[overflow-wrap:anywhere]">{valueText(cf, value)}</span>;
  }
}

const IconFields = (
  <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
    <rect x="2" y="2.5" width="12" height="11" rx="1.5" /><path d="M5 6h6M5 8.5h6M5 11h3.5" />
  </svg>
);

/** The detail page's Custom fields panel: each field that applies, with its value or "Not set". Nothing when no field
 *  applies to the kind. */
export function CustomFieldsPanel({ kind, values }: { kind: CFKind; values?: CFValues | null }) {
  const { fields, error, refetch, pending } = useCustomFields(kind);
  if (pending) {
    return (
      <Panel id="h-custom" icon={IconFields} title="Custom fields">
        <p className="px-4 py-3 text-sm text-muted-foreground" aria-live="polite">Loading custom fields…</p>
      </Panel>
    );
  }
  if (error) {
    return (
      <Panel id="h-custom" icon={IconFields} title="Custom fields">
        <p className="px-4 py-3 text-sm" role="alert">Couldn't load the custom fields. <button type="button" className="underline" onClick={refetch}>Retry</button></p>
      </Panel>
    );
  }
  if (!fields?.length) return null;
  return (
    <Panel id="h-custom" icon={IconFields} title="Custom fields">
      <DetailList>
        {fields.map((cf) => <DetailRow key={cf.name} label={cf.label}><CFValue cf={cf} value={values?.[cf.name]} /></DetailRow>)}
      </DetailList>
    </Panel>
  );
}

// ---- table columns and filters ---------------------------------------------------------------------------------------------

/** Optional `cf_<name>` columns, off by default. */
export function customColumns<T extends { id: string; custom_field_data?: CFValues | null }>(fields: CustomField[]): RecordColumn<T>[] {
  return fields.map((cf) => ({
    id: `cf_${cf.name}`, header: cf.label, defaultVisible: false,
    className: cf.type === "longtext" || cf.type === "multiselect" ? "min-w-48 whitespace-normal" : undefined,
    cell: (row) => <CFValue cf={cf} value={row.custom_field_data?.[cf.name]} compact />,
  }));
}

const SET_OPTIONS = [{ value: "true", label: "Has a value" }, { value: "false", label: "No value" }];

/** The list filters a field takes (README "Custom fields"): text, long text and URL contain; choices any of; yes/no;
 *  numbers and dates exact, from and to; every field "is set". */
export function customFilterFields(fields: CustomField[]): FilterField[] {
  return fields.flatMap((cf): FilterField[] => {
    const key = `cf_${cf.name}`;
    const set: FilterField = { key: `${key}__set`, label: `${cf.label} is set`, options: SET_OPTIONS };
    switch (cf.type) {
      case "text": case "longtext": case "url": return [{ key, label: `${cf.label} contains`, options: [], input: "text" }, set];
      case "select": case "multiselect": return [{ key, label: cf.label, options: cf.choices.map((c) => ({ value: c, label: c })), input: "choices" }, set];
      case "boolean": return [{ key, label: cf.label, options: [{ value: "true", label: "Yes" }, { value: "false", label: "No" }] }, set];
      case "integer": case "decimal": case "date": {
        const input = cf.type === "date" ? "date" : "number";
        const step = cf.type === "integer" ? "1" : "any";
        return [
          { key, label: `${cf.label} is`, options: [], input, step },
          { key: `${key}__gte`, label: `${cf.label} from`, options: [], input, step },
          { key: `${key}__lte`, label: `${cf.label} to`, options: [], input, step },
          set,
        ];
      }
      default: return [set];
    }
  });
}

/** `cf_` parameters in the URL that no field here defines (a stale link or a deleted field): shown as chips the user can
 *  remove, and sent as they are, so the list's 400 names them. */
export function unknownFilterFields(params: URLSearchParams, known: FilterField[]): FilterField[] {
  const keys = new Set(known.map((f) => f.key));
  return [...new Set([...params.keys()].filter((k) => k.startsWith("cf_") && !keys.has(k)))]
    .map((k) => ({ key: k, label: `Unknown filter ${k}`, options: [], input: "text" as const }));
}
