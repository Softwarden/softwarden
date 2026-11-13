import { useEffect, useRef, useState } from "react";
import { Link, Navigate, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  AGENT_AUTH_MODES, AGENT_CHANNELS, AGENT_PLATFORMS, AGENT_RISKS, AIModel, api, apiWithEtag, AUTH_METHODS, BUILT_BY, CatalogRecord, cap, Choice,
  COMPONENT_TYPES, CRITICALITIES, DATA_CLASSIFICATIONS, DEPLOYMENT_MODELS, ENVIRONMENTS, HOSTINGS, LICENSE_MODELS, LIFECYCLES, Person,
  PRECONDITION_FAILED, Problem, RetirementCheck, retirementCheck, RISK_TIERS, SoftwareCategory, typeLabel, UserRef, VALIDATION_STATUSES,
} from "@/api";
import { canWrite, savedState, useMe } from "@/lib/session";
import { apiKey, problemText, useApiQuery } from "@/lib/query";
import { apiPath, KIND_LABEL, KIND_NOUN, KIND_PLURAL, listPath, recordPath, type RecordKind } from "@/lib/kinds";
import {
  Breadcrumb, ErrorBanner, ErrorSummary, Field, Loading, PageHead, Select, Textarea, useLoad, useReturnFocus,
} from "@/components/app";
import { CheckboxPicker } from "@/components/checkbox-picker";
import { Combobox, ComboOption } from "@/components/combobox";
import { RetireDialog } from "@/components/retire-dialog";
import { TagInput, useTagNames } from "@/features/catalog/curation";
import { modelName } from "@/features/catalog/catalog-list";
import { useVendorQuickAdd } from "@/features/admin/admin";
import { triOf, TriSelect, triValue, useCompliance, type TriState } from "@/features/catalog/fields";
import { CustomFieldInputs, useCustomFieldForm, useCustomFields, type CustomField } from "@/features/catalog/custom-fields";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** Create / edit an application, component or AI agent. One form for the three record
 *  kinds; the kind is fixed after create. Routes: /applications/new (?kind=component for a component),
 *  /components/new (redirects there), /ai-agents/new, and /applications/:id/edit, /components/:id/edit,
 *  /ai-agents/:id/edit. An edit sends the record's ETag back as If-Match, so a change someone else saved meanwhile is
 *  refused (412) instead of overwritten. */

type Kind = RecordKind;
type Named = { id: string; name: string };

/** Text and single-choice fields, as the form holds them ("" for blank). Lists (tags, models, channels) are kept apart. */
export type FormValues = {
  name: string; lifecycle: string; criticality: string; vendor: string; purpose: string; built_by: string; license_model: string;
  documentation_url: string; support_url: string; component_type: string; host_runtime: string; repository_url: string;
  business_owner: string; technical_owner: string; support_team: string; it_steward: string;
  // shared
  department: string; cost_center: string; data_classification: string;
  // applications
  category: string; deployment_model: string; version: string; primary_url: string; admin_url: string; authentication_method: string;
  sso_enabled: TriState; scim_provisioning: TriState; entra_app_id: string; service_principal_id: string;
  // components (environment, pp_environment_id and external_id are an agent's too)
  hosting: string; runbook_url: string; schedule_or_trigger: string; environment: string; azure_resource_id: string;
  pp_environment_id: string; external_id: string;
  // AI agents
  platform: string; knowledge_sources: string; tools_connectors: string; auth_mode: string; run_as_identity: string;
  data_access_scope: string; human_in_loop: TriState; actions_can_write: TriState; risk_classification: string; repo_url: string;
  // compliance profile
  is_gxp: TriState; validation_status: string; validation_doc_url: string; last_periodic_review: string; next_periodic_review: string; risk_tier: string;
};
/** What the API accepts. Blank pick-lists are null; blank text is "". On create, blank optional fields are left out.
 *  `custom_field_data` carries only the custom fields that change. */
export type RecordPayload = Record<string, string | string[] | boolean | null | Record<string, unknown>>;
export type Lookups = {
  people: Person[]; teams: Named[]; vendors: Named[]; stewards: UserRef[]; departments: Named[];
  categories: SoftwareCategory[]; models: AIModel[];
};
export type NewPerson = { name: string; email: string; department: string };
/** A field the user changed, in words: shown again after a 412 so a reload doesn't lose what they typed. */
export type UnsavedValue = { label: string; value: string };

type OwnerField = "business_owner" | "technical_owner";
type ConfirmedKey = "business_owner" | "technical_owner" | "support_team" | "lifecycle" | "criticality";

/** Fields a review confirms. Changing one on a Confirmed record makes it Needs confirmation. */
const CONFIRMED_FIELDS: { key: ConfirmedKey; label: string }[] = [
  { key: "business_owner", label: "Business owner" },
  { key: "technical_owner", label: "Technical owner" },
  { key: "support_team", label: "Support team" },
  { key: "lifecycle", label: "Lifecycle" },
  { key: "criticality", label: "Criticality" },
];

const join = (labels: string[]) => (labels.length < 2 ? labels.join("") : `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`);

function initialValues(r: CatalogRecord | undefined, stewardId: string): FormValues {
  const owner = (p?: Person | null) => (p && p.active ? p.id : ""); // inactive people are not offered, so they start blank
  const t = (v?: string | null) => v ?? "";
  return {
    name: r?.name ?? "", lifecycle: r?.lifecycle ?? "active", criticality: r?.criticality ?? "", vendor: r?.vendor?.id ?? "",
    purpose: t(r?.purpose), built_by: t(r?.built_by), license_model: t(r?.license_model),
    documentation_url: t(r?.documentation_url), support_url: t(r?.support_url),
    component_type: r?.component_type || "other", host_runtime: t(r?.host_runtime), repository_url: t(r?.repository_url),
    business_owner: owner(r?.business_owner), technical_owner: owner(r?.technical_owner), support_team: r?.support_team?.id ?? "",
    it_steward: r ? (r.it_steward ? String(r.it_steward.id) : "") : stewardId,
    department: r?.department?.id ?? "", cost_center: t(r?.cost_center), data_classification: t(r?.data_classification),
    category: r?.category?.id ?? "", deployment_model: t(r?.deployment_model), version: t(r?.version), primary_url: t(r?.primary_url),
    admin_url: t(r?.admin_url), authentication_method: t(r?.authentication_method), sso_enabled: triOf(r?.sso_enabled),
    scim_provisioning: triOf(r?.scim_provisioning), entra_app_id: t(r?.entra_app_id), service_principal_id: t(r?.service_principal_id),
    hosting: t(r?.hosting), runbook_url: t(r?.runbook_url), schedule_or_trigger: t(r?.schedule_or_trigger), environment: t(r?.environment),
    azure_resource_id: t(r?.azure_resource_id), pp_environment_id: t(r?.pp_environment_id), external_id: t(r?.external_id),
    platform: t(r?.platform), knowledge_sources: t(r?.knowledge_sources), tools_connectors: t(r?.tools_connectors), auth_mode: t(r?.auth_mode),
    run_as_identity: t(r?.run_as_identity), data_access_scope: t(r?.data_access_scope), human_in_loop: triOf(r?.human_in_loop),
    actions_can_write: triOf(r?.actions_can_write), risk_classification: t(r?.risk_classification), repo_url: t(r?.repo_url),
    is_gxp: triOf(r?.is_gxp), validation_status: t(r?.validation_status), validation_doc_url: t(r?.validation_doc_url),
    last_periodic_review: t(r?.last_periodic_review), next_periodic_review: t(r?.next_periodic_review), risk_tier: t(r?.risk_tier),
  };
}

/** The text fields each kind sends (trimmed), the choice fields (as given) and the yes/no fields (null when not recorded). */
const KIND_TEXT: Record<Kind, (keyof FormValues)[]> = {
  application: ["documentation_url", "support_url", "version", "primary_url", "admin_url", "entra_app_id", "service_principal_id"],
  component: ["host_runtime", "repository_url", "documentation_url", "runbook_url", "schedule_or_trigger", "azure_resource_id", "pp_environment_id", "external_id"],
  ai_agent: ["pp_environment_id", "knowledge_sources", "tools_connectors", "run_as_identity", "data_access_scope", "repo_url", "external_id"],
};
const KIND_CHOICE: Record<Kind, (keyof FormValues)[]> = {
  application: ["built_by", "license_model", "deployment_model", "authentication_method"],
  component: ["built_by", "license_model", "component_type", "hosting", "environment"],
  ai_agent: ["platform", "environment", "auth_mode", "risk_classification"],
};
const KIND_TRI: Record<Kind, (keyof FormValues)[]> = {
  application: ["sso_enabled", "scim_provisioning"],
  component: [],
  ai_agent: ["human_in_loop", "actions_can_write"],
};

function buildPayload(v: FormValues, lists: { tags: string[]; models: string[]; channels: string[] }, kind: Kind, mode: "create" | "edit", compliance: boolean): RecordPayload {
  const orNull = (s: string) => s || null;
  const payload: RecordPayload = {
    name: v.name.trim(), lifecycle: v.lifecycle, criticality: orNull(v.criticality), purpose: v.purpose.trim(),
    business_owner: orNull(v.business_owner), technical_owner: orNull(v.technical_owner), support_team: orNull(v.support_team),
    it_steward: orNull(v.it_steward), tags: lists.tags,
    department: orNull(v.department), cost_center: v.cost_center.trim(), data_classification: v.data_classification,
  };
  for (const k of KIND_TEXT[kind]) payload[k] = (v[k] as string).trim();
  for (const k of KIND_CHOICE[kind]) payload[k] = v[k] as string;
  for (const k of KIND_TRI[kind]) payload[k] = triValue(v[k] as TriState);
  if (kind === "application") Object.assign(payload, { vendor: orNull(v.vendor), category: orNull(v.category) });
  if (kind === "ai_agent") Object.assign(payload, { models_used: lists.models, channels: lists.channels });
  // The compliance fields exist only while the profile is on; the API refuses them otherwise.
  if (compliance) {
    Object.assign(payload, {
      is_gxp: triValue(v.is_gxp), validation_status: orNull(v.validation_status), validation_doc_url: orNull(v.validation_doc_url.trim()),
      last_periodic_review: orNull(v.last_periodic_review), next_periodic_review: orNull(v.next_periodic_review), risk_tier: orNull(v.risk_tier),
    });
  }
  if (mode === "edit") return payload; // PATCH sends everything so a cleared field is cleared
  // Create: leave blank optional fields out (a blank steward is kept: it means "none", not "me").
  return Object.fromEntries(Object.entries(payload).filter(([k, val]) => k === "it_steward" || (val !== "" && val !== null && !(Array.isArray(val) && !val.length))));
}

// ---- Unsaved changes ----------------------------------------------------------------------------------------------

/** "Discard changes?" for in-app links and the browser's own leave prompt. BrowserRouter has no blocker API, so
 *  this catches clicks on same-site links (sidebar, breadcrumb, Cancel) before React Router sees them.
 *  Browser Back/Forward is covered only by the beforeunload prompt on a reload or tab close, not by this dialog. */
export function useUnsavedGuard(dirty: boolean) {
  const navigate = useNavigate();
  const [target, setTarget] = useState<string | null>(null);
  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    const click = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const a = (e.target as Element).closest?.("a[href]") as HTMLAnchorElement | null;
      if (!a || (a.target && a.target !== "_self") || a.origin !== location.origin) return;
      if (a.pathname === location.pathname && a.search === location.search) return; // in-page anchors such as error links
      e.preventDefault();
      e.stopPropagation();
      setTarget(a.pathname + a.search + a.hash);
    };
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", click, true);
    return () => { window.removeEventListener("beforeunload", beforeUnload); document.removeEventListener("click", click, true); };
  }, [dirty]);
  const returnFocus = useReturnFocus(target !== null);
  const dialog = (
    <Dialog open={target !== null} onOpenChange={(o) => !o && setTarget(null)}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Discard changes?</DialogTitle>
          <DialogDescription>You have unsaved changes. If you leave this page they are lost.</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => setTarget(null)}>Keep editing</Button>
          <Button type="button" onClick={() => { const to = target!; setTarget(null); navigate(to); }}>Discard changes</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
  return dialog;
}

// ---- The form ---------------------------------------------------------------------------------------------------------------

function SectionTitle({ children, note }: { children: React.ReactNode; note?: string }) {
  return (
    <div className="mb-3 mt-6 border-t pt-4 md:col-span-2">
      <h2 className="font-display text-section font-semibold">{children}</h2>
      {note && <p className="mt-0.5 text-sm text-muted-foreground">{note}</p>}
    </div>
  );
}


export type RecordFormProps = {
  mode: "create" | "edit";
  kind: Kind;
  /** Create only: called when the Kind radio changes (applications and components). Typed values are kept. */
  onKindChange?: (kind: Kind) => void;
  /** Edit only: the loaded record. Drives the starting values and the needs-confirmation note. */
  record?: CatalogRecord;
  lookups: Lookups;
  /** The signed-in user's id; the IT steward defaults to them on create. */
  currentUserId: number;
  /** Whether the compliance profile's fields are part of records now (useCompliance). */
  compliance: boolean;
  /** The custom fields that apply to the kind, loaded (useCustomFields), in weight order. */
  customFields: CustomField[];
  /** RFC 9457 problem `errors`, keyed by field name. Keys must match the field ids (name, lifecycle, business_owner, ...). */
  errors?: Record<string, string[]>;
  /** A failure that isn't tied to one field (network, 5xx). Shown above the form; typed values are kept. */
  formError?: React.ReactNode;
  /** True while the request is running: the button says "Saving…" and every control is disabled. */
  saving?: boolean;
  /** Non-blocking: "Another application is named Expense Hub. Save anyway?" Hidden again once the name is edited. */
  duplicateWarning?: string;
  /** The user chose "Save anyway". The page should resend the save with the duplicate confirmed. */
  onConfirmDuplicate?: () => void;
  /** `changes` are the fields this save changes, in words (label and shown value), for the 412 "Your unsaved values". */
  onSubmit: (payload: RecordPayload, changes: UnsavedValue[]) => void | Promise<void>;
  /** Offer inline "+ Add vendor" (useVendorQuickAdd: the name alone, or the vendor dialog when custom fields need it). */
  allowCreateVendor?: boolean;
  /** Inline "+ Add person": create it and return the saved person. Reject with a Problem to show its message. */
  onCreatePerson?: (person: NewPerson) => Promise<Person>;
  cancelTo: string;
};

const choiceOptions = (list: Choice[]) => list.map((c) => <option key={c.value} value={c.value}>{c.label}</option>);

export function RecordForm({
  mode, kind, onKindChange, record, lookups, currentUserId, compliance, customFields, errors = {}, formError, saving = false,
  duplicateWarning, onConfirmDuplicate, onSubmit, allowCreateVendor, onCreatePerson, cancelTo,
}: RecordFormProps) {
  const [initial] = useState(() => initialValues(record, String(currentUserId)));
  const [values, setValues] = useState<FormValues>(initial);
  const [initialLists] = useState(() => ({
    tags: record?.tags ?? [], models: (record?.models_used ?? []).map((m) => m.id), channels: record?.channels ?? [],
  }));
  const [tags, setTags] = useState<string[]>(initialLists.tags);
  const [models, setModels] = useState<string[]>(initialLists.models);
  const [channels, setChannels] = useState<string[]>(initialLists.channels);
  const tagNames = useTagNames();
  const [local, setLocal] = useState<Record<string, string[]>>({});
  const [extraVendors, setExtraVendors] = useState<Named[]>([]);
  const [extraPeople, setExtraPeople] = useState<Person[]>([]);
  const [vendorNote, setVendorNote] = useState<{ text: string; error: boolean } | null>(null);
  const [adding, setAdding] = useState<{ field: OwnerField; draft: NewPerson } | null>(null);
  const [addError, setAddError] = useState("");
  const [addBusy, setAddBusy] = useState(false);
  const [warnedName, setWarnedName] = useState<string | null>(null);
  const lists = { tags, models, channels };
  const custom = useCustomFieldForm(customFields, mode, record?.custom_field_data);
  const dirty = JSON.stringify(values) !== JSON.stringify(initial) || JSON.stringify(lists) !== JSON.stringify(initialLists) || custom.dirty;
  const guard = useUnsavedGuard(dirty && !saving);
  const shown = { ...local, ...errors };
  const set = <K extends keyof FormValues>(k: K, v: string) => setValues((cur) => ({ ...cur, [k]: v }));
  const on = (k: keyof FormValues) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => set(k, e.target.value);

  useEffect(() => { setWarnedName(duplicateWarning ? values.name : null); }, [duplicateWarning]); // eslint-disable-line react-hooks/exhaustive-deps
  const showDuplicate = duplicateWarning && warnedName === values.name;
  // A link to one field (`…/edit#entra_app_id`, from the SSO and MFA panel) lands on that field. The hash is then
  // cleared, so a remount doesn't move focus again.
  // The query string and router state stay.
  const hashNavigate = useNavigate();
  const here = useLocation();
  useEffect(() => {
    const id = /^#\w+$/.test(here.hash) ? here.hash.slice(1) : "";
    if (!id) return;
    setTimeout(() => document.getElementById(id)?.focus());
    hashNavigate({ pathname: here.pathname, search: here.search }, { replace: true, state: here.state });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // The record is Confirmed and a confirmed field now differs from what is saved.
  const savedFields = record ? initialRaw(record) : null;
  const changedConfirmed = mode === "edit" && record?.review_state === "confirmed" && savedFields
    ? CONFIRMED_FIELDS.filter((f) => values[f.key] !== savedFields[f.key]).map((f) => f.label) : [];

  const people: ComboOption[] = [...lookups.people, ...extraPeople.filter((p) => !lookups.people.some((q) => q.id === p.id))]
    .filter((p) => p.active)
    .map((p) => ({ id: p.id, label: p.name, hint: [p.department?.name, p.email].filter(Boolean).join(" · ") }));
  const vendors: ComboOption[] = [...lookups.vendors, ...extraVendors.filter((v) => !lookups.vendors.some((w) => w.id === v.id))]
    .map((v) => ({ id: v.id, label: v.name }));
  const teams: ComboOption[] = lookups.teams.map((t) => ({ id: t.id, label: t.name }));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const found: Record<string, string[]> = {};
    if (!values.name.trim()) found.name = ["Enter a name."];
    if (!values.lifecycle) found.lifecycle = ["Choose a lifecycle status."];
    const cf = custom.build(); // a JSON object that doesn't parse is caught here, before the API
    Object.assign(found, cf.errors);
    setLocal(found);
    if (Object.keys(found).length) return;
    const payload = buildPayload(values, lists, kind, mode, compliance);
    if (cf.data) payload.custom_field_data = cf.data;
    void onSubmit(payload, describeChanges());
  }

  /** The fields that differ from where the form started, as their labels and the values shown on screen (a select's
   *  chosen text, a combobox's name), read from the form's own controls. */
  function describeChanges(): UnsavedValue[] {
    const out: UnsavedValue[] = [];
    const labelOf = (k: string) => (document.querySelector(`label[for="${k}"]`)?.textContent ?? k).replace(/ \(required\)$/, "");
    for (const k of Object.keys(values) as (keyof FormValues)[]) {
      if (values[k] === initial[k]) continue;
      const el = document.getElementById(k);
      if (!el) continue;
      const shown = el instanceof HTMLSelectElement ? el.selectedOptions[0]?.text ?? "" : el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement ? el.value : values[k];
      out.push({ label: labelOf(k), value: shown || "(blank)" });
    }
    if (JSON.stringify(tags) !== JSON.stringify(initialLists.tags)) out.push({ label: "Tags", value: tags.join(", ") || "(none)" });
    if (JSON.stringify(models) !== JSON.stringify(initialLists.models)) out.push({ label: "Models", value: modelOptions.filter((o) => models.includes(o.id)).map((o) => o.label).join(", ") || "(none)" });
    if (JSON.stringify(channels) !== JSON.stringify(initialLists.channels)) out.push({ label: "Channels", value: AGENT_CHANNELS.filter((c) => channels.includes(c.value)).map((c) => c.label).join(", ") || "(none)" });
    out.push(...custom.changes());
    return out;
  }

  const vendorAdd = useVendorQuickAdd({
    focusId: "vendor",
    onAdded: (vendor) => {
      setExtraVendors((cur) => [...cur, vendor]);
      set("vendor", vendor.id);
      setVendorNote({ text: `Vendor ${vendor.name} added and selected.`, error: false });
    },
    onError: (text) => setVendorNote({ text, error: true }),
    onDialog: () => setVendorNote(null),
  });
  async function addVendor(typed: string) {
    const name = typed.trim();
    if (!name) { setVendorNote({ text: "Type the vendor name first, then choose Add vendor.", error: true }); return; }
    setVendorNote({ text: `Adding vendor ${name}…`, error: false });
    await vendorAdd.add(name);
  }

  async function addPerson() {
    if (!adding) return;
    if (!adding.draft.name.trim()) { setAddError("Enter a name."); return; }
    setAddBusy(true);
    setAddError("");
    try {
      const person = await onCreatePerson!({ ...adding.draft, name: adding.draft.name.trim(), email: adding.draft.email.trim() });
      setExtraPeople((cur) => [...cur, person]);
      set(adding.field, person.id);
      const field = adding.field;
      setAdding(null);
      setTimeout(() => document.getElementById(field)?.focus());
    } catch (err) {
      setAddError(err instanceof Problem ? (Object.values(err.errors).flat().join(" ") || err.detail) : "Couldn't add the person. Try again.");
    } finally {
      setAddBusy(false);
    }
  }

  const ownerPicker = (id: OwnerField, label: string) => {
    const saved = record?.[id];
    const help = saved && !saved.active ? `${saved.name} is inactive. Choose an active person, or leave blank to show a gap.` : undefined;
    return (
      <Field id={id} label={label} errors={shown} help={help}>
        <Combobox id={id} options={people} value={values[id]} onChange={(v) => set(id, v)} placeholder="Search people…"
          clearLabel="Not set (gap)" listLabel={`${label} suggestions`}
          add={onCreatePerson ? { label: (t) => (t ? `+ Add person "${t}"` : "+ Add person"), onAdd: (t) => { setAddError(""); setAdding({ field: id, draft: { name: t, email: "", department: "" } }); } } : undefined} />
      </Field>
    );
  };
  const text = (id: keyof FormValues, label: string, help?: string) => (
    <Field id={id} label={label} errors={shown} help={help}><Input id={id} value={values[id]} onChange={on(id)} autoComplete="off" /></Field>
  );
  const url = (id: keyof FormValues, label: string) => (
    <Field id={id} label={label} errors={shown}><Input id={id} type="url" inputMode="url" placeholder="https://…" value={values[id]} onChange={on(id)} /></Field>
  );
  const pick = (id: keyof FormValues, label: string, list: Choice[], help?: string, blank = "Not recorded") => (
    <Field id={id} label={label} errors={shown} help={help}>
      <Select id={id} value={values[id]} onChange={on(id)}><option value="">{blank}</option>{choiceOptions(list)}</Select>
    </Field>
  );
  const tri = (id: keyof FormValues, label: string, help?: string, yes?: string, no?: string) => (
    <Field id={id} label={label} errors={shown} help={help}>
      <TriSelect id={id} value={values[id] as TriState} onChange={(v) => set(id, v)} yes={yes} no={no} />
    </Field>
  );
  const long = (id: keyof FormValues, label: string, help?: string) => (
    <div className="md:col-span-2">
      <Field id={id} label={label} errors={shown} help={help}><Textarea id={id} value={values[id]} onChange={on(id)} /></Field>
    </div>
  );

  const stewardKnown = lookups.stewards.some((u) => String(u.id) === values.it_steward);
  const addLabel = adding ? (adding.field === "business_owner" ? "Business owner" : "Technical owner") : "";
  const noun = KIND_NOUN[kind][0];
  const modelOptions = lookups.models.map((m) => ({ id: m.id, label: modelName(m), hint: m.modality ? cap(m.modality) : undefined }));
  // A model the record uses but the list no longer offers (it can't be: deleting a used model is refused) stays visible.
  for (const m of record?.models_used ?? []) if (!modelOptions.some((o) => o.id === m.id)) modelOptions.push({ id: m.id, label: modelName(m), hint: undefined });

  return (
    <Card className="max-w-4xl">
      <CardContent className="pt-6">
        <form onSubmit={submit} noValidate aria-label={mode === "create" ? `Add ${noun}` : `Edit ${noun}`}>
          <fieldset disabled={saving} className="m-0 min-w-0 border-0 p-0">
            {formError && (typeof formError === "string" ? <ErrorBanner message={formError} /> : formError)}
            <ErrorSummary errors={shown} focusOnShow="field" />
            {showDuplicate && (
              <div className="mb-3 flex flex-wrap items-center gap-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" role="status">
                <span aria-hidden="true">⚠</span>
                <p className="min-w-0 flex-1 font-semibold">{duplicateWarning}</p>
                {onConfirmDuplicate && <Button type="button" size="sm" variant="outline" onClick={onConfirmDuplicate}>Save anyway</Button>}
              </div>
            )}

            <div className="grid gap-x-6 md:grid-cols-2">
              <Field id="name" label="Name (required)" errors={shown}>
                <Input id="name" value={values.name} onChange={on("name")} autoComplete="off"
                  onBlur={() => setLocal((l) => { const { name: _drop, ...rest } = l; return values.name.trim() ? rest : { ...rest, name: ["Enter a name."] }; })} />
              </Field>

              {mode === "create" && kind !== "ai_agent" ? (
                <fieldset className="mb-3 flex min-w-0 flex-col gap-1.5">
                  <legend className="mb-1.5 text-sm font-medium leading-none">Kind</legend>
                  <div className="flex h-9 items-center gap-5">
                    {(["application", "component"] as const).map((k) => (
                      <label key={k} className="flex items-center gap-2 text-sm">
                        <input type="radio" name="kind" value={k} checked={kind === k} onChange={() => onKindChange?.(k)} className="h-4 w-4 accent-primary" />
                        {KIND_LABEL[k]}
                      </label>
                    ))}
                  </div>
                </fieldset>
              ) : (
                <div className="mb-3 flex flex-col gap-1.5">
                  <span className="text-sm font-medium leading-none">Kind</span>
                  <p className="flex h-9 items-center text-sm">{KIND_LABEL[kind]} {mode === "edit" && <span className="ml-2 text-muted-foreground">(can't be changed)</span>}</p>
                </div>
              )}

              <Field id="lifecycle" label="Lifecycle (required)" errors={shown}>
                <Select id="lifecycle" value={values.lifecycle} onChange={on("lifecycle")}>
                  {LIFECYCLES.map((l) => <option key={l} value={l}>{cap(l)}</option>)}
                </Select>
              </Field>
              <Field id="criticality" label="Criticality" errors={shown} help="Not set shows as a gap on the record.">
                <Select id="criticality" value={values.criticality} onChange={on("criticality")}>
                  <option value="">Not set</option>
                  {CRITICALITIES.map((c) => <option key={c} value={c}>{cap(c)}</option>)}
                </Select>
              </Field>

              {kind === "application" && (
                <div className="md:col-span-2">
                  <Field id="vendor" label="Vendor" errors={shown}>
                    <Combobox id="vendor" options={vendors} value={values.vendor} onChange={(v) => set("vendor", v)} placeholder="Search vendors…"
                      clearLabel="No vendor" listLabel="Vendor suggestions"
                      add={allowCreateVendor ? { label: (t) => (t ? `+ Add vendor "${t}"` : "+ Add vendor"), onAdd: (t) => void addVendor(t) } : undefined} />
                  </Field>
                  {vendorNote && <p className={`-mt-1.5 mb-3 text-sm ${vendorNote.error ? "text-error-foreground" : "text-muted-foreground"}`} role={vendorNote.error ? "alert" : "status"}>{vendorNote.text}</p>}
                </div>
              )}
              <div className="md:col-span-2">
                <Field id="purpose" label="Purpose" errors={shown} help={kind === "ai_agent" ? "What the agent does and for whom, in a sentence or two." : "One or two sentences on what this is used for."}>
                  <Textarea id="purpose" value={values.purpose} onChange={on("purpose")} />
                </Field>
              </div>

              {kind === "application" && <>
                <Field id="category" label="Category" errors={shown} help="The organization's software categories; Editors and Admins keep the list.">
                  <Select id="category" value={values.category} onChange={on("category")}>
                    <option value="">Not recorded</option>
                    {lookups.categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                  </Select>
                </Field>
                {pick("deployment_model", "Deployment model", DEPLOYMENT_MODELS)}
                {text("version", "Version", "For example 2026.1 or 11.4.")}
                {pick("authentication_method", "Authentication method", AUTH_METHODS)}
                {tri("sso_enabled", "Single sign-on (SSO)", "No is a decision; Not recorded means nobody has checked.", "Yes, SSO is on", "No SSO")}
                {tri("scim_provisioning", "SCIM provisioning", undefined, "Yes, SCIM provisions accounts", "No SCIM")}
                <SectionTitle>Links</SectionTitle>
                {url("primary_url", "Primary URL")}
                {url("admin_url", "Admin URL")}
                {url("documentation_url", "Documentation URL")}
                {url("support_url", "Support URL")}
                {pick("built_by", "Built by", BUILT_BY.map((b) => ({ value: b, label: cap(b) })), undefined, "Not set")}
                {pick("license_model", "License model", LICENSE_MODELS.map((l) => ({ value: l, label: cap(l.replace(/_/g, " ")) })), undefined, "Not set")}
                <SectionTitle note="GUIDs from Microsoft Entra, for example 00000000-0000-0000-0000-000000000000.">Microsoft Entra</SectionTitle>
                {text("entra_app_id", "Application (client) ID")}
                {text("service_principal_id", "Service principal object ID")}
              </>}

              {kind === "component" && <>
                {pick("built_by", "Built by", BUILT_BY.map((b) => ({ value: b, label: cap(b) })), undefined, "Not set")}
                {pick("license_model", "License model", LICENSE_MODELS.map((l) => ({ value: l, label: cap(l.replace(/_/g, " ")) })), undefined, "Not set")}
                <SectionTitle>Runtime</SectionTitle>
                <Field id="component_type" label="Component type" errors={shown}>
                  <Select id="component_type" value={values.component_type} onChange={on("component_type")}>
                    {COMPONENT_TYPES.map((t) => <option key={t} value={t}>{typeLabel(t)}</option>)}
                  </Select>
                </Field>
                {text("host_runtime", "Host / runtime", "For example Azure Functions or a Windows server name.")}
                {pick("hosting", "Hosting", HOSTINGS)}
                {pick("environment", "Environment", ENVIRONMENTS)}
                {text("schedule_or_trigger", "Schedule or trigger", "For example nightly at 02:00, or when a file arrives.")}
                {url("repository_url", "Repository URL")}
                {url("documentation_url", "Documentation URL")}
                {url("runbook_url", "Runbook URL")}
                <SectionTitle note="Where the component lives in Azure, Power Platform or another system.">Identifiers</SectionTitle>
                {text("azure_resource_id", "Azure resource ID")}
                {text("pp_environment_id", "Power Platform environment ID")}
                {text("external_id", "External ID")}
              </>}

              {kind === "ai_agent" && <>
                <SectionTitle>Agent</SectionTitle>
                {pick("platform", "Platform", AGENT_PLATFORMS)}
                {pick("environment", "Environment", ENVIRONMENTS)}
                {pick("auth_mode", "Auth mode", AGENT_AUTH_MODES, "Whose identity the agent acts with.")}
                {text("run_as_identity", "Run-as identity", "The account or app registration the agent acts as.")}
                {pick("risk_classification", "Risk classification", AGENT_RISKS, "The EU AI Act's four tiers.")}
                <div className="md:col-span-2">
                  <CheckboxPicker id="models_used" legend="Models" options={modelOptions} value={models} onChange={setModels} errors={shown.models_used}
                    help="The AI models the agent uses. Models are kept on the AI models page."
                    empty={<p className="text-sm text-muted-foreground">No AI models are recorded yet. <Link to="/ai-models">Add models on the AI models page</Link>, then choose them here.</p>} />
                </div>
                <div className="md:col-span-2">
                  <CheckboxPicker id="channels" legend="Channels" options={AGENT_CHANNELS.map((c) => ({ id: c.value, label: c.label }))} value={channels} onChange={setChannels}
                    errors={shown.channels} help="Where people reach the agent." columns />
                </div>
                <SectionTitle note="Blank scope and an unrecorded human-in-loop decision show as gaps in Needs attention.">Governance</SectionTitle>
                {long("data_access_scope", "Data access scope", "What data the agent can reach, for example the HR SharePoint site, read only.")}
                {tri("human_in_loop", "Human in the loop", "Whether a person approves the agent's actions. No is a decision; Not recorded is a gap.", "Yes, a person approves", "No, it acts alone")}
                {tri("actions_can_write", "Actions can write", "Whether the agent can change data, not only read it.", "Yes, it can write", "No, read only")}
                {long("knowledge_sources", "Knowledge sources", "Sites, files and systems it answers from.")}
                {long("tools_connectors", "Tools and connectors", "Actions, plugins and connectors it can call.")}
                <SectionTitle>Identifiers</SectionTitle>
                {text("pp_environment_id", "Power Platform environment ID")}
                {text("external_id", "External ID", "The bot ID or schema name in the source platform.")}
                {url("repo_url", "Repository URL")}
              </>}

              <div className="md:col-span-2">
                <TagInput id="tags" label="Tags" tags={tags} onChange={setTags} suggestions={tagNames} errors={shown.tags} disabled={saving}
                  help="Optional. Press Enter or comma to add a tag, for example Finance. Tags are not case-sensitive." />
              </div>

              <CustomFieldInputs form={custom} errors={shown} />

              <SectionTitle>Organization</SectionTitle>
              <Field id="department" label="Department" errors={shown}>
                <Select id="department" value={values.department} onChange={on("department")}>
                  <option value="">Not recorded</option>
                  {lookups.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                </Select>
              </Field>
              {text("cost_center", "Cost center")}
              {pick("data_classification", "Data classification", DATA_CLASSIFICATIONS)}

              {compliance && <>
                <SectionTitle note="The compliance profile is on for this organization.">Compliance</SectionTitle>
                {tri("is_gxp", "GxP relevant", undefined, "Yes, GxP relevant", "Not GxP relevant")}
                {pick("validation_status", "Validation status", VALIDATION_STATUSES)}
                {url("validation_doc_url", "Validation document URL")}
                {pick("risk_tier", "Risk tier", RISK_TIERS)}
                <Field id="last_periodic_review" label="Last periodic review" errors={shown}>
                  <Input id="last_periodic_review" type="date" value={values.last_periodic_review} onChange={on("last_periodic_review")} />
                </Field>
                <Field id="next_periodic_review" label="Next periodic review" errors={shown}>
                  <Input id="next_periodic_review" type="date" value={values.next_periodic_review} onChange={on("next_periodic_review")} />
                </Field>
              </>}

              <SectionTitle note="Can be left blank; blanks show as gaps.">Ownership</SectionTitle>
              {ownerPicker("business_owner", "Business owner")}
              {ownerPicker("technical_owner", "Technical owner")}

              {adding && (
                <div role="group" aria-labelledby="add-person-title" className="mb-3 rounded-md border bg-table-head px-4 py-3 md:col-span-2"
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.target as HTMLElement).tagName === "INPUT") { e.preventDefault(); void addPerson(); }
                    if (e.key === "Escape") { e.stopPropagation(); setAdding(null); document.getElementById(adding.field)?.focus(); }
                  }}>
                  <h3 id="add-person-title" className="font-semibold">Add person</h3>
                  <p className="mb-3 text-sm text-muted-foreground">Saved to People &amp; Teams and chosen as the {addLabel.toLowerCase()}.</p>
                  {addError && <p className="mb-3 text-sm text-error-foreground" role="alert">{addError}</p>}
                  <div className="grid gap-x-4 md:grid-cols-3">
                    <div className="mb-3 flex flex-col gap-1.5">
                      <Label htmlFor="new-person-name">Name of new person (required)</Label>
                      <Input id="new-person-name" autoFocus value={adding.draft.name} aria-invalid={addError ? true : undefined}
                        onChange={(e) => setAdding({ ...adding, draft: { ...adding.draft, name: e.target.value } })} />
                    </div>
                    <div className="mb-3 flex flex-col gap-1.5">
                      <Label htmlFor="new-person-email">Email of new person</Label>
                      <Input id="new-person-email" type="email" value={adding.draft.email}
                        onChange={(e) => setAdding({ ...adding, draft: { ...adding.draft, email: e.target.value } })} />
                    </div>
                    <div className="mb-3 flex flex-col gap-1.5">
                      <Label htmlFor="new-person-department">Department of new person</Label>
                      <Select id="new-person-department" value={adding.draft.department}
                        onChange={(e) => setAdding({ ...adding, draft: { ...adding.draft, department: e.target.value } })}>
                        <option value="">Not set</option>
                        {lookups.departments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                      </Select>
                    </div>
                  </div>
                  <div className="flex gap-2">
                    <Button type="button" size="sm" variant="outline" disabled={addBusy} onClick={() => void addPerson()}>{addBusy ? "Adding…" : "Add person"}</Button>
                    <Button type="button" size="sm" variant="outline" disabled={addBusy}
                      onClick={() => { const f = adding.field; setAdding(null); setTimeout(() => document.getElementById(f)?.focus()); }}>Cancel</Button>
                  </div>
                </div>
              )}

              <Field id="support_team" label="Support team" errors={shown}>
                <Combobox id="support_team" options={teams} value={values.support_team} onChange={(v) => set("support_team", v)} placeholder="Search teams…"
                  clearLabel="Not set (gap)" listLabel="Support team suggestions" />
              </Field>
              <Field id="it_steward" label="IT steward" errors={shown} help="Admins and Editors only. With none, the record routes to the IT coordinator.">
                <Select id="it_steward" value={values.it_steward} onChange={on("it_steward")}>
                  <option value="">None</option>
                  {lookups.stewards.map((u) => <option key={u.id} value={u.id}>{u.name}{u.id === currentUserId ? " (me)" : ""}</option>)}
                  {values.it_steward && !stewardKnown && <option value={values.it_steward}>{record?.it_steward?.name ?? "Current steward"} (no longer an Admin or Editor)</option>}
                </Select>
              </Field>
            </div>

            <div role="status">
              {changedConfirmed.length > 0 && (
                <p className="mb-4 flex gap-2 rounded-md border border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground">
                  <span aria-hidden="true">ⓘ</span>
                  <span>Changing {join(changedConfirmed)} will mark this record Needs confirmation. Prior reviews are kept.</span>
                </p>
              )}
            </div>

            <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
              <Button asChild variant="outline"><Link to={cancelTo} aria-disabled={saving || undefined}>Cancel</Link></Button>
              <Button id="record-save" type="submit">{saving ? "Saving…" : "Save"}</Button>
            </div>
          </fieldset>
        </form>
        {guard}
        {vendorAdd.dialog}
      </CardContent>
    </Card>
  );
}

/** The saved ids behind the confirmed fields, in the same shape as the form values (for the needs-confirmation comparison). */
function initialRaw(r: CatalogRecord): Pick<FormValues, ConfirmedKey> {
  return {
    business_owner: r.business_owner?.id ?? "", technical_owner: r.technical_owner?.id ?? "", support_team: r.support_team?.id ?? "",
    lifecycle: r.lifecycle, criticality: r.criticality ?? "",
  };
}

// ---- Pages --------------------------------------------------------------------------------------------------------------------

function useLookups(kind: Kind) {
  const people = useLoad<Person[]>("people");
  const teams = useLoad<Named[]>("teams");
  const vendors = useLoad<Named[]>("vendors");
  const stewards = useLoad<UserRef[]>("stewards");
  const departments = useLoad<Named[]>("departments");
  // Categories are for applications (and the create form's Kind switch), models for agents.
  // Loaded for components too, so switching Kind on the create form never unmounts it (typed values are kept).
  const categories = useApiQuery<SoftwareCategory[]>("categories", { enabled: kind !== "ai_agent" });
  const models = useApiQuery<AIModel[]>("ai-models", { enabled: kind === "ai_agent" });
  const needed = [people, teams, vendors, stewards, departments];
  const extra = kind === "ai_agent" ? [models] : [categories];
  const failed = extra.find((q) => q.isError);
  const error = needed.find((l) => l.error)?.error || (failed ? problemText(failed.error, "Couldn't load the form's choices.") : "");
  const ready = needed.every((l) => l.data) && extra.every((q) => q.data);
  const lookups: Lookups | null = ready
    ? {
      people: people.data!, teams: teams.data!, vendors: vendors.data!, stewards: stewards.data!, departments: departments.data!,
      categories: categories.data ?? [], models: models.data ?? [],
    } : null;
  return { lookups, error, reload: () => { needed.forEach((l) => l.reload()); extra.forEach((q) => void q.refetch()); } };
}

/** Inline "+ Add person"; the new person is then chosen as the owner. */
function createPerson(person: NewPerson): Promise<Person> {
  return api<Person>("people", { method: "POST", body: { ...person, department: person.department || null } });
}

function NeedsWriteRole({ kind }: { kind: Kind }) {
  return (
    <>
      <PageHead title="You can't change records" desc={`Editors and Admins can add and edit ${KIND_NOUN[kind][1]}. You can view them.`} />
      <Button asChild variant="outline"><Link to={listPath(kind)}>Back to {KIND_PLURAL[kind]}</Link></Button>
    </>
  );
}

function LoadingForm({ what }: { what: string }) {
  return <Loading what={what} rows={4} />;
}

/** Shared save logic: POST for create, PATCH for edit (with If-Match when the record's ETag is known). Field errors go
 *  to the form; a 412 says someone else saved first; anything else is a banner. */
function useSave(method: "POST" | "PATCH", path: string, after: (saved: CatalogRecord, acknowledged?: { reason: string; dependents: number }) => void, etag?: string | null) {
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [formError, setFormError] = useState("");
  const [stale, setStale] = useState(false);
  // A same-named record is a warning, not a block: the API answers 409 with the
  // duplicate-name problem type, and "Save anyway" resends with confirm_duplicate.
  const [duplicate, setDuplicate] = useState<string | undefined>(undefined);
  const last = useRef<RecordPayload | null>(null);
  // Retiring a record that active records depend on is refused until it is acknowledged with a reason.
  const [retirement, setRetirement] = useState<RetirementCheck | null>(null);
  const headers = method === "PATCH" && etag ? { "If-Match": etag } : undefined;

  async function save(payload: RecordPayload, confirm = false) {
    last.current = payload;
    setSaving(true);
    setErrors({});
    setFormError("");
    setStale(false);
    setDuplicate(undefined);
    try {
      const saved = await api<CatalogRecord>(path, { method, body: confirm ? { ...payload, confirm_duplicate: true } : payload, headers });
      setSaving(false);
      after(saved);
    } catch (e) {
      setSaving(false);
      const check = retirementCheck(e);
      if (check) setRetirement(check);
      else if (e instanceof Problem && e.type === "/problems/duplicate-name") setDuplicate(e.detail);
      else if (e instanceof Problem && (e.status === 412 || e.type === PRECONDITION_FAILED)) setStale(true);
      else if (e instanceof Problem && Object.keys(e.errors).length) setErrors(e.errors);
      else setFormError(`${e instanceof Problem ? e.detail : "Something went wrong."} Your changes weren't saved. Nothing you typed was lost.`);
    }
  }
  const confirmDuplicate = () => { if (last.current) void save(last.current, true); };
  /** Resend the save with the acknowledgment. Resolves to a message to keep the dialog open, or null when it was saved. */
  async function acknowledge(reason: string): Promise<string | null> {
    if (!last.current || !retirement) return "Nothing to retry.";
    try {
      const saved = await api<CatalogRecord>(path, { method, body: { ...last.current, retirement_ack: true, retirement_ack_reason: reason }, headers });
      setRetirement(null);
      after(saved, { reason, dependents: retirement.dependents.length });
      return null;
    } catch (e) {
      if (!(e instanceof Problem)) return "Something went wrong. Try again. Nothing you typed was lost.";
      if (e.status === 412) { setRetirement(null); setStale(true); return null; }
      return Object.values(e.errors).flat().join(" ") || e.detail;
    }
  }
  return { saving, errors, formError, stale, clearStale: () => setStale(false), duplicate, save, confirmDuplicate, retirement, closeRetirement: () => setRetirement(null), acknowledge };
}

/** /applications/new (?kind=component) and /ai-agents/new. */
export function NewRecord({ kind: fixed }: { kind?: Kind } = {}) {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const kind: Kind = fixed ?? (params.get("kind") === "component" ? "component" : "application");
  const base = apiPath(kind);
  const { lookups, error, reload } = useLookups(kind);
  const compliance = useCompliance();
  // Both kinds' custom fields for the application form, so switching Kind never unmounts it (typed values are kept).
  const agentFields = useCustomFields("ai_agent", kind === "ai_agent");
  const appFields = useCustomFields("application", kind !== "ai_agent");
  const componentFields = useCustomFields("component", kind !== "ai_agent");
  const kindFields = kind === "ai_agent" ? agentFields : kind === "component" ? componentFields : appFields;
  const custom = {
    fields: kindFields.fields && (kind === "ai_agent" || (appFields.fields && componentFields.fields)) ? kindFields.fields : undefined,
    error: (kind === "ai_agent" ? [agentFields] : [appFields, componentFields]).find((c) => c.error)?.error ?? null,
    refetch: () => { agentFields.refetch(); appFields.refetch(); componentFields.refetch(); },
  };
  const save = useSave("POST", base, (saved) => navigate(recordPath(kind, saved.id)!, { state: savedState(saved.name, `${saved.name} added.`) }));
  if (!canWrite(me)) return <NeedsWriteRole kind={kind} />;
  const title = `Add ${KIND_NOUN[kind][0]}`;
  const trail = kind === "component" ? [{ to: "/applications", label: "Applications" }, { to: listPath("component"), label: "Components" }]
    : [{ to: listPath(kind), label: KIND_PLURAL[kind] }];
  const ready = lookups && !compliance.org.isPending && custom.fields;
  const failed = error || (custom.error ? problemText(custom.error, "Couldn't load the custom fields.") : "");
  return (
    <>
      <Breadcrumb trail={trail} current={title} />
      <PageHead docTitle={title} title={title} desc="Name and lifecycle are required. Everything else can be left blank and shows as a gap." />
      {failed && <ErrorBanner message={failed} onRetry={() => { reload(); custom.refetch(); }} />}
      {!ready && !failed && <LoadingForm what="form" />}
      {ready && (
        <RecordForm mode="create" kind={kind} onKindChange={fixed ? undefined : (k) => setParams(k === "component" ? { kind: "component" } : {}, { replace: true })}
          lookups={lookups} currentUserId={me.user.id} compliance={compliance.enabled} customFields={custom.fields!} errors={save.errors} formError={save.formError} saving={save.saving}
          duplicateWarning={save.duplicate} onConfirmDuplicate={save.confirmDuplicate} onSubmit={(p) => save.save(p)}
          allowCreateVendor onCreatePerson={createPerson} cancelTo={listPath(kind)} />
      )}
    </>
  );
}

export function NewAgent() {
  return <NewRecord kind="ai_agent" />;
}

/** /components/new is the same screen as /applications/new?kind=component. */
export function NewComponentRedirect() {
  return <Navigate to="/applications/new?kind=component" replace />;
}

export function EditApplication() {
  const { id } = useParams();
  return <EditRecord key={id} kind="application" id={id!} />;
}

export function EditComponent() {
  const { id } = useParams();
  return <EditRecord key={id} kind="component" id={id!} />;
}

export function EditAgent() {
  const { id } = useParams();
  return <EditRecord key={id} kind="ai_agent" id={id!} />;
}

function EditRecord({ kind, id }: { kind: Kind; id: string }) {
  const me = useMe();
  const navigate = useNavigate();
  const base = apiPath(kind);
  // Read with the ETag, which the save sends back as If-Match. Not cached: an edit always starts from the server's copy.
  const rec = useQuery({ queryKey: [...apiKey(`${base}/${id}`), "etag"], queryFn: () => apiWithEtag<CatalogRecord>(`${base}/${id}`), gcTime: 0, retry: false, staleTime: Infinity });
  // The copy the form started from, with its ETag, kept until "Load the latest version": a background refetch can't
  // swap the ETag (or the record) under what the user is typing.
  const [loaded, setLoaded] = useState<{ data: CatalogRecord; etag: string | null } | null>(null);
  useEffect(() => { if (rec.data && !loaded) setLoaded(rec.data); }, [rec.data, loaded]);
  // What the user changed in a save the API refused with 412, listed above the reloaded form.
  const [attempted, setAttempted] = useState<UnsavedValue[]>([]);
  const [unsaved, setUnsaved] = useState<UnsavedValue[]>([]);
  const { lookups, error: lookupError, reload: reloadLookups } = useLookups(kind);
  const custom = useCustomFields(kind);
  const error = lookupError || (custom.error ? problemText(custom.error, "Couldn't load the custom fields.") : "");
  const reload = () => { reloadLookups(); custom.refetch(); };
  const compliance = useCompliance();
  const detail = recordPath(kind, id)!;
  const save = useSave("PATCH", `${base}/${id}`, (saved, ack) => navigate(detail, {
    state: savedState(saved.name, ack ? `Retired with acknowledgment of ${ack.dependents} active ${ack.dependents === 1 ? "dependent" : "dependents"}. Reason saved to the audit history: ${ack.reason}` : `${saved.name} saved.`),
  }), loaded?.etag);
  // "Load the latest version" remounts the form with the server's values (what was typed here is dropped, as it says).
  const [round, setRound] = useState(0);
  if (!canWrite(me)) return <NeedsWriteRole kind={kind} />;
  if (rec.error instanceof Problem && rec.error.status === 404) {
    return (
      <>
        <PageHead title="Record not found" desc="This record doesn't exist or you don't have access." />
        <Button asChild variant="outline"><Link to={listPath(kind)}>Back to {KIND_PLURAL[kind]}</Link></Button>
      </>
    );
  }
  if (rec.error) return <ErrorBanner message={rec.error instanceof Problem ? rec.error.detail : "Couldn't load this record."} onRetry={() => void rec.refetch()} />;
  const r = loaded?.data;
  async function loadLatest() {
    const latest = await rec.refetch();
    if (!latest.data) return;
    setLoaded(latest.data);
    setUnsaved(attempted);
    save.clearStale();
    setRound((n) => n + 1);
  }
  const trail = [
    ...(kind === "ai_agent" ? [{ to: listPath(kind), label: KIND_PLURAL[kind] }] : [{ to: "/applications", label: "Applications" }]),
    ...(kind === "component" ? [{ to: listPath("component"), label: "Components" }] : []),
    ...(r ? [{ to: detail, label: r.name }] : []),
  ];
  const staleBanner = save.stale && (
    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert">
      <strong className="min-w-0 flex-1">Someone else saved changes to this {KIND_NOUN[kind][0]} after you opened it, so yours weren't saved. Load the latest version to see theirs; your values are then listed above the form so you can enter them again.</strong>
      <Button type="button" size="sm" variant="outline" onClick={() => void loadLatest()}>Load the latest version</Button>
    </div>
  );
  return (
    <>
      <Breadcrumb trail={trail} current="Edit" />
      <PageHead docTitle={r ? `Edit ${r.name}` : "Edit"} title={<span className="[overflow-wrap:anywhere]">{r ? `Edit ${r.name}` : "Edit"}</span>}
        desc="Name and lifecycle are required. Blank fields show as gaps." />
      {error && <ErrorBanner message={error} onRetry={reload} />}
      {(!r || !lookups || compliance.org.isPending || !custom.fields) && !error && <LoadingForm what={KIND_NOUN[kind][0]} />}
      {unsaved.length > 0 && (
        <section aria-labelledby="unsaved-title" className="mb-4 max-w-4xl rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground">
          <h2 id="unsaved-title" className="font-semibold">Your unsaved values</h2>
          <p className="text-sm">The form below shows the latest saved version. These are the values you tried to save; enter again the ones you still want.</p>
          <dl className="mt-2 grid grid-cols-[minmax(120px,max-content)_1fr] gap-x-4 gap-y-1 text-sm">
            {unsaved.map((u) => <div key={u.label} className="contents"><dt className="font-semibold">{u.label}</dt><dd className="[overflow-wrap:anywhere]">{u.value}</dd></div>)}
          </dl>
          <Button type="button" size="sm" variant="outline" className="mt-2" onClick={() => setUnsaved([])}>Dismiss</Button>
        </section>
      )}
      {r && lookups && !compliance.org.isPending && custom.fields && (
        <RecordForm key={round} mode="edit" kind={kind} record={r} lookups={lookups} currentUserId={me.user.id} compliance={compliance.enabled} customFields={custom.fields}
          errors={save.errors} formError={staleBanner || save.formError} saving={save.saving}
          duplicateWarning={save.duplicate} onConfirmDuplicate={save.confirmDuplicate} onSubmit={(p, changes) => { setAttempted(changes); return save.save(p); }}
          allowCreateVendor onCreatePerson={createPerson} cancelTo={detail} />
      )}
      <RetireDialog open={save.retirement !== null} onClose={save.closeRetirement} returnFocusId="record-save" name={r?.name ?? "this record"}
        check={save.retirement} onConfirm={save.acknowledge} />
    </>
  );
}
