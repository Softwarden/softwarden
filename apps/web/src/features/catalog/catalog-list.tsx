import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  AGENT_AUTH_MODES, AGENT_CHANNELS, AGENT_PLATFORMS, AGENT_RISKS, AIModel, AIProvider, AUTH_METHODS, cap, CatalogRecord, COMPONENT_TYPES, CRITICALITIES,
  DATA_CLASSIFICATIONS, DEPLOYMENT_MODELS, ENVIRONMENTS, HOSTINGS, labelOf, LIFECYCLES, NamedRef, RISK_TIERS, SoftwareCategory, STATE_LABEL, typeLabel,
  VALIDATION_STATUSES, type Person, type ReviewState, type TagInfo, type UserRef,
} from "@/api";
import { canWrite, useMe } from "@/lib/session";
import { useApiQuery } from "@/lib/query";
import { apiPath, KIND_NOUN, KIND_PLURAL, newPath, recordPath, type RecordKind } from "@/lib/kinds";
import { Chip, EmptyState, Gap, LazyPart, lazyPart, PageHead, StateChip } from "@/components/app";
import { ExportDialog, MAX_BULK, Query, SavedViews, TagChips, templateUrl, type ExportFormat, type SavedFilter, type ViewColumns } from "@/features/catalog/curation";
import { triFilter, TriText, useCompliance } from "@/features/catalog/fields";
import { customColumns, customFilterFields, unknownFilterFields, useCustomFields } from "@/features/catalog/custom-fields";
import { FilterBar, type FilterField } from "@/components/filter-bar";
import { CompletenessBar, hasQuality, useQualityRules } from "@/features/quality/completeness";
import { BulkBar, RecordTable, useRecordTable, visibleColumnIds, type RecordColumn } from "@/components/record-table";
import { chip, CriticalityMark, GapChips, LifecycleChip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";

// Bulk edit and bulk delete load on first use, so the list's first bundle stays small.
const BulkEditDialog = lazyPart(() => import("@/features/catalog/bulk").then((m) => m.BulkEditDialog));
const BulkDeleteDialog = lazyPart(() => import("@/features/catalog/bulk").then((m) => m.BulkDeleteDialog));

/** The record lists on RecordTable: applications and components (one page with a Kind switch) and AI agents.
 *  Kind, preset, search, filters, sort and page live in the URL: ?kind=components&preset=overdue&q=pay. */

type Preset = "active" | "missing-owners" | "overdue" | "archived";

const ACTIVE_SCOPE = "lifecycle=active,deprecated&archived=false";

const PRESETS: { id: Preset; label: string; query: string }[] = [
  // "Active" means lifecycle active or deprecated and not archived; queues and gaps use the same scope.
  { id: "active", label: "Active", query: `${ACTIVE_SCOPE}` },
  { id: "missing-owners", label: "Missing owners", query: `${ACTIVE_SCOPE}&gap=true` },
  { id: "overdue", label: "Overdue reviews", query: `${ACTIVE_SCOPE}&review_state=overdue` },
  { id: "archived", label: "Archived", query: "archived=true" },
];

type Row = CatalogRecord;

/** Filter parameters a saved filter or a filter chip can set, per kind. When any is in the URL the list uses them
 *  instead of a preset button. `tag` and `q` are kept apart (they combine with a preset). */
const SHARED_KEYS = ["lifecycle", "archived", "gap", "review_state", "imported", "criticality", "department", "data_classification", "owner", "support_team", "it_steward"];
const COMPLIANCE_KEYS = ["is_gxp", "validation_status", "risk_tier", "periodic_review_due"];
/** Data quality filters: the completeness score below / at least a percentage, and a failing rule. */
const QUALITY_KEYS = ["completeness__lt", "completeness__gte", "quality_rule"];
const KIND_KEYS: Record<RecordKind, string[]> = {
  application: ["category", "deployment_model", "vendor", "authentication_method", "sso_enabled", "scim_provisioning"],
  component: ["component_type", "hosting", "environment"],
  ai_agent: ["platform", "channel", "risk_classification", "human_in_loop", "actions_can_write", "model", "provider", "auth_mode", "environment"],
};
/** Filters whose values are record IDs. */
const ID_KEYS = ["category", "vendor", "department", "model", "provider", "quality_rule", "owner", "support_team", "it_steward"];
const rawKeys = (kind: RecordKind, compliance: boolean, quality: boolean) => [...SHARED_KEYS, ...KIND_KEYS[kind], ...(compliance ? COMPLIANCE_KEYS : []), ...(quality ? QUALITY_KEYS : [])];

const yesNo = (yes: string, no: string) => [{ value: "true", label: yes }, { value: "false", label: no }];
const opts = (list: { value: string; label: string }[]) => list.map((c) => ({ value: c.value, label: c.label }));
const named = (list?: NamedRef[]) => (list ?? []).map((n) => ({ value: n.id, label: n.name }));

type Lookups = {
  tags: string[]; departments?: NamedRef[]; people?: Person[]; teams?: NamedRef[]; stewards?: UserRef[]; categories?: SoftwareCategory[]; vendors?: NamedRef[]; models?: AIModel[]; providers?: AIProvider[];
  /** Data-quality rules that are on and check this kind; null without the data_quality capability. */
  rules?: { id: string; label: string }[] | null;
};

function filterFields(kind: RecordKind, l: Lookups, compliance: boolean): FilterField[] {
  const own: FilterField[] = kind === "application" ? [
    { key: "category", label: "Category", options: named(l.categories) },
    { key: "deployment_model", label: "Deployment model", options: opts(DEPLOYMENT_MODELS) },
    { key: "vendor", label: "Vendor", options: named(l.vendors) },
    { key: "authentication_method", label: "Authentication", options: opts(AUTH_METHODS) },
    { key: "sso_enabled", label: "SSO", options: triFilter("SSO on", "No SSO") },
    { key: "scim_provisioning", label: "SCIM", options: triFilter("SCIM on", "No SCIM") },
  ] : kind === "component" ? [
    { key: "component_type", label: "Type", options: COMPONENT_TYPES.map((t) => ({ value: t, label: typeLabel(t) })) },
    { key: "hosting", label: "Hosting", options: opts(HOSTINGS) },
    { key: "environment", label: "Environment", options: opts(ENVIRONMENTS) },
  ] : [
    { key: "platform", label: "Platform", options: opts(AGENT_PLATFORMS) },
    { key: "channel", label: "Channel", options: opts(AGENT_CHANNELS) },
    { key: "risk_classification", label: "Risk classification", options: opts(AGENT_RISKS) },
    { key: "human_in_loop", label: "Human in the loop", options: triFilter() },
    { key: "actions_can_write", label: "Actions can write", options: triFilter("Can write", "Read only") },
    { key: "model", label: "Model", options: (l.models ?? []).map((m) => ({ value: m.id, label: modelName(m) })) },
    { key: "provider", label: "Provider", options: named(l.providers) },
    { key: "auth_mode", label: "Auth mode", options: opts(AGENT_AUTH_MODES) },
    { key: "environment", label: "Environment", options: opts(ENVIRONMENTS) },
  ];
  return [
    { key: "lifecycle", label: "Lifecycle", options: [{ value: "active,deprecated", label: "Active or Deprecated" }, ...LIFECYCLES.map((x) => ({ value: x, label: cap(x) }))] },
    { key: "criticality", label: "Criticality", options: CRITICALITIES.map((c) => ({ value: c, label: cap(c) })) },
    { key: "review_state", label: "Review state", options: (Object.keys(STATE_LABEL) as ReviewState[]).map((s) => ({ value: s, label: STATE_LABEL[s] })) },
    { key: "gap", label: "Ownership gaps", options: yesNo("Has ownership gaps", "No ownership gaps") },
    ...own,
    // Owner matches the business or the technical owner (the list API's `owner`); people who left are still offered,
    // since finding what they owned is the point.
    { key: "owner", label: "Owner (business or technical)", options: (l.people ?? []).map((p) => ({ value: p.id, label: !p.active ? `${p.name} (inactive)` : p.name })) },
    { key: "support_team", label: "Support team", options: named(l.teams) },
    { key: "it_steward", label: "IT steward", options: (l.stewards ?? []).map((u) => ({ value: String(u.id), label: u.name })) },
    { key: "department", label: "Department", options: named(l.departments) },
    { key: "data_classification", label: "Data classification", options: opts(DATA_CLASSIFICATIONS) },
    ...(compliance ? [
      { key: "is_gxp", label: "GxP", options: yesNo("GxP relevant", "Not GxP relevant") },
      { key: "validation_status", label: "Validation status", options: opts(VALIDATION_STATUSES) },
      { key: "risk_tier", label: "Risk tier", options: opts(RISK_TIERS) },
      { key: "periodic_review_due", label: "Periodic review", options: yesNo("Periodic review due", "Periodic review not due") },
    ] : []),
    ...(kind !== "ai_agent" ? [{ key: "imported", label: "Imported", options: yesNo("Created by an import", "Not imported") }] : []),
    ...(l.rules ? [
      { key: "completeness__lt", label: "Completeness below (%)", options: [], input: "number" as const, step: "1" },
      { key: "completeness__gte", label: "Completeness at least (%)", options: [], input: "number" as const, step: "1" },
      { key: "quality_rule", label: "Failing check", options: l.rules.map((r) => ({ value: r.id, label: r.label })) },
    ] : []),
    { key: "archived", label: "Archived", options: [{ value: "false", label: "Not archived" }, { value: "true", label: "Archived only" }, { value: "all", label: "Archived or not" }] },
    { key: "tag", label: "Tag", options: l.tags.map((t) => ({ value: t, label: t })) },
  ];
}

export const modelName = (m: { name: string; version: string; provider: NamedRef }) => `${m.provider.name} ${m.name}${m.version ? ` ${m.version}` : ""}`;

/** Channel names as small neutral chips. */
export function ChannelChips({ channels }: { channels?: string[] }) {
  if (!channels?.length) return <span className="text-muted-foreground">None recorded</span>;
  return (
    <ul aria-label="Channels" className="m-0 flex list-none flex-wrap gap-1 p-0">
      {channels.map((c) => <li key={c}><span className={chip({ state: "neutral" })}>{labelOf(AGENT_CHANNELS, c)}</span></li>)}
    </ul>
  );
}

const orDash = (s?: string) => s || "—";

function recordColumns(kind: RecordKind, compliance: boolean, quality: boolean): RecordColumn<Row>[] {
  const person = (p: Row["business_owner"], gap: string) => (p ? <span>{p.name}{!p.active && <span className="text-muted-foreground"> (inactive)</span>}</span> : <Gap label={gap} />);
  const own: RecordColumn<Row>[] = kind === "application" ? [
    { id: "vendor", header: "Vendor", defaultVisible: false, cell: (a) => a.vendor?.name ?? "—" },
    { id: "category", header: "Category", defaultVisible: false, cell: (a) => a.category?.name ?? "—" },
    { id: "deployment_model", header: "Deployment model", defaultVisible: false, cell: (a) => orDash(labelOf(DEPLOYMENT_MODELS, a.deployment_model)) },
    { id: "authentication_method", header: "Authentication", defaultVisible: false, cell: (a) => orDash(labelOf(AUTH_METHODS, a.authentication_method)) },
    { id: "sso_enabled", header: "SSO", defaultVisible: false, cell: (a) => <TriText value={a.sso_enabled} /> },
  ] : kind === "component" ? [
    { id: "component_type", header: "Type", cell: (a) => typeLabel(a.component_type) },
    { id: "hosting", header: "Hosting", defaultVisible: false, cell: (a) => orDash(labelOf(HOSTINGS, a.hosting)) },
    { id: "environment", header: "Environment", defaultVisible: false, cell: (a) => orDash(labelOf(ENVIRONMENTS, a.environment)) },
  ] : [
    { id: "platform", header: "Platform", cell: (a) => orDash(labelOf(AGENT_PLATFORMS, a.platform)) },
    { id: "models_used", header: "Models", className: "whitespace-normal", cell: (a) => (a.models_used?.length ? a.models_used.map(modelName).join(", ") : "—") },
    { id: "risk_classification", header: "Risk", sortField: "risk_classification", sortText: ["least to most risky", "most to least risky"], cell: (a) => orDash(labelOf(AGENT_RISKS, a.risk_classification)) },
    { id: "channels", header: "Channels", defaultVisible: false, className: "whitespace-normal", cell: (a) => <ChannelChips channels={a.channels} /> },
    { id: "human_in_loop", header: "Human in the loop", defaultVisible: false, cell: (a) => (a.human_in_loop == null ? <Gap label="No human-in-loop decision" /> : <TriText value={a.human_in_loop} />) },
    { id: "actions_can_write", header: "Actions can write", defaultVisible: false, cell: (a) => <TriText value={a.actions_can_write} /> },
    { id: "environment", header: "Environment", defaultVisible: false, cell: (a) => orDash(labelOf(ENVIRONMENTS, a.environment)) },
  ];
  return [
    {
      id: "name", header: "Name", sortField: "name",
      cell: (a) => <>
        {a.archived && <Chip className="ml-2 align-middle">Archived</Chip>}
        {a.tags && a.tags.length > 0 && <div className="mt-1 font-normal"><TagChips tags={a.tags} /></div>}
      </>,
    },
    ...own.filter((c) => c.defaultVisible !== false),
    { id: "lifecycle", header: "Lifecycle", sortField: "lifecycle", sortText: ["in lifecycle order", "in reverse lifecycle order"], cell: (a) => <LifecycleChip value={a.lifecycle} /> },
    { id: "criticality", header: "Criticality", sortField: "criticality", sortText: ["most to least critical", "least to most critical"], defaultVisible: false, cell: (a) => (a.criticality ? <CriticalityMark value={a.criticality} /> : <Gap label="No criticality" />) },
    { id: "review_state", header: "Review state", cell: (a) => <StateChip state={a.review_state} /> },
    { id: "gaps", header: "Gaps", className: "whitespace-normal", cell: (a) => (a.gaps.length ? <GapChips gaps={a.gaps} /> : "—") },
    ...(quality ? [{
      id: "completeness", header: "Completeness", sortField: "completeness", sortText: ["least complete first", "most complete first"] as [string, string],
      cell: (a: Row) => (a.quality ? <CompletenessBar score={a.quality.score} /> : "—"),
    }] : []),
    { id: "business_owner", header: "Business owner", defaultVisible: false, cell: (a) => person(a.business_owner, "No business owner") },
    { id: "technical_owner", header: "Technical owner", defaultVisible: false, cell: (a) => person(a.technical_owner, "No technical owner") },
    { id: "support_team", header: "Support team", defaultVisible: false, cell: (a) => (a.support_team ? a.support_team.name : <Gap label="No support team" />) },
    { id: "it_steward", header: "IT steward", defaultVisible: false, cell: (a) => (a.it_steward ? a.it_steward.name : <Chip>Unassigned</Chip>) },
    { id: "next_review_date", header: "Next review", defaultVisible: false, cell: (a) => (a.next_review_date ? <time dateTime={a.next_review_date}>{a.next_review_date}</time> : "—") },
    ...own.filter((c) => c.defaultVisible === false),
    { id: "department", header: "Department", defaultVisible: false, cell: (a) => a.department?.name ?? "—" },
    { id: "data_classification", header: "Data classification", defaultVisible: false, cell: (a) => orDash(labelOf(DATA_CLASSIFICATIONS, a.data_classification)) },
    ...(compliance ? [
      { id: "validation_status", header: "Validation status", defaultVisible: false, cell: (a: Row) => orDash(labelOf(VALIDATION_STATUSES, a.validation_status)) },
      { id: "risk_tier", header: "Risk tier", defaultVisible: false, cell: (a: Row) => orDash(labelOf(RISK_TIERS, a.risk_tier)) },
    ] : []),
  ];
}

/** The lookups behind the filter chips' options (and the words for IDs in saved-filter and export summaries). */
function useListLookups(kind: RecordKind, quality: boolean): Lookups {
  const tags = useApiQuery<TagInfo[]>("tags", { staleTime: 30_000 });
  const departments = useApiQuery<NamedRef[]>("departments", { staleTime: 30_000 });
  const people = useApiQuery<Person[]>("people", { staleTime: 30_000 });
  const teams = useApiQuery<NamedRef[]>("teams", { staleTime: 30_000 });
  const stewards = useApiQuery<UserRef[]>("stewards", { staleTime: 30_000 });
  const categories = useApiQuery<SoftwareCategory[]>("categories", { staleTime: 30_000, enabled: kind === "application" });
  const vendors = useApiQuery<NamedRef[]>("vendors", { staleTime: 30_000, enabled: kind === "application" });
  const models = useApiQuery<AIModel[]>("ai-models", { staleTime: 30_000, enabled: kind === "ai_agent" });
  const providers = useApiQuery<AIProvider[]>("ai-providers", { staleTime: 30_000, enabled: kind === "ai_agent" });
  const rules = useQualityRules(quality);
  return {
    tags: tags.data?.map((t) => t.name) ?? [], departments: departments.data, people: people.data, teams: teams.data, stewards: stewards.data, categories: categories.data, vendors: vendors.data,
    models: models.data, providers: providers.data,
    rules: quality ? (rules.data ?? []).filter((r) => r.status === "on" && r.kinds.includes(kind)) : null,
  };
}

/** /applications (applications, or components with ?kind=components). */
export function Applications() {
  const [params] = useSearchParams();
  const kind: RecordKind = params.get("kind") === "components" ? "component" : "application";
  return <RecordList key={kind} kind={kind} />;
}

/** /ai-agents. */
export function AgentsList() {
  return <RecordList kind="ai_agent" />;
}

function RecordList({ kind }: { kind: RecordKind }) {
  const [params, setParams] = useSearchParams();
  const me = useMe();
  const complianceState = useCompliance();
  const compliance = complianceState.enabled;
  const quality = hasQuality(me);
  // Custom field filters (cf_<name>) join the raw keys, and so does any cf_ parameter in the URL that no field defines
  // (a stale link): it is sent as it is, so the list's 400 names it, and shown as a chip that can be removed.
  const customFields = useCustomFields(kind).fields;
  const cfFilters = useMemo(() => customFilterFields(customFields ?? []), [customFields]);
  // Until the definitions arrive every cf_ parameter is sent, but none is shown as unknown.
  const cfUnknown = customFields ? unknownFilterFields(params, cfFilters) : [];
  const cfKeys = [...new Set([...cfFilters.map((f) => f.key), ...[...params.keys()].filter((k) => k.startsWith("cf_"))])];
  const RAW_KEYS = [...rawKeys(kind, compliance, quality), ...cfKeys];
  const CLEAR_RAW = Object.fromEntries(RAW_KEYS.map((k) => [k, ""]));
  const plural = KIND_PLURAL[kind];
  const [noun, nouns] = KIND_NOUN[kind];
  const preset = (PRESETS.find((p) => p.id === params.get("preset"))?.id ?? "active") as Preset;
  const q = (params.get("q") ?? "").trim();
  const tag = (params.get("tag") ?? "").trim();
  const raw = RAW_KEYS.filter((k) => params.get(k));
  // The open saved view: `?view=<id>`, set when a view is opened and in the link a weekly email carries.
  const viewId = params.get("view") ?? "";
  const custom = raw.length > 0 || params.get("preset") === "all"; // a saved filter's own parameters, not a preset button

  function update(changes: Record<string, string>, push = !("q" in changes)) {
    setParams((cur) => {
      const next = new URLSearchParams(cur);
      // A filter change starts again at the first page: a cursor belongs to the query it came from.
      next.delete("cursor");
      next.delete("page");
      for (const [k, v] of Object.entries(changes)) {
        if (!v || (k === "preset" && v === "active")) next.delete(k);
        else next.set(k, v);
      }
      return next;
    }, { replace: !push });
  }

  const [notice, setNotice] = useState("");
  const [dialog, setDialog] = useState<"export" | "bulk" | "delete" | null>(null);
  // A bulk dialog stays mounted once opened, so it can close (and give focus back) like any other dialog.
  const [bulkUsed, setBulkUsed] = useState({ bulk: false, delete: false });
  const openBulk = (which: "bulk" | "delete") => { setBulkUsed((u) => ({ ...u, [which]: true })); setDialog(which); };
  const [format, setFormat] = useState<ExportFormat>("csv");
  const lookups = useListLookups(kind, quality);

  // The list's query: the raw filter parameters (a saved filter or chips) or the preset, plus search and tag.
  const presetQuery: Query = Object.fromEntries(new URLSearchParams(PRESETS.find((p) => p.id === preset)!.query));
  const scope: Query = custom ? Object.fromEntries(raw.map((k) => [k, params.get(k)!])) : presetQuery;
  const filterQuery: Query = { ...scope, ...(q ? { q } : {}), ...(tag ? { tag } : {}) };
  const path = apiPath(kind);
  const table = useRecordTable<Row>({ id: path, path, query: filterQuery, maxSelection: MAX_BULK });
  const columns = useMemo(() => [...recordColumns(kind, compliance, quality), ...customColumns<Row>(customFields ?? [])], [kind, compliance, quality, customFields]);
  // Every column the list can have is known once the custom fields and the compliance setting have loaded.
  const columnsReady = customFields !== undefined && !complianceState.org.isPending;
  const viewColumns: ViewColumns = useMemo(() => ({
    all: columns.map((c) => ({ id: c.id, header: c.header })),
    shown: visibleColumnIds(columns, table.hidden, "name"),
    defaults: visibleColumnIds(columns, null, "name"),
  }), [columns, table.hidden]);
  /** A view's columns become the table's column choice (empty: the list's defaults). */
  function applyViewColumns(ids: string[]) {
    const own = ids.filter((id) => columns.some((c) => c.id === id));
    if (!own.length) table.setHidden(null);
    else table.setHidden(columns.filter((c) => c.id !== "name" && !own.includes(c.id)).map((c) => c.id), columns.map((c) => c.id));
  }
  const rows = table.rows;

  const filtered = custom || preset !== "active" || q !== "" || tag !== "";
  const clear = () => update({ preset: "", q: "", tag: "", view: "", ...CLEAR_RAW }, true);
  const tagOptions = [...lookups.tags];
  if (tag && !tagOptions.some((t) => t.toLowerCase() === tag.toLowerCase())) tagOptions.push(tag);
  const fields = [...filterFields(kind, { ...lookups, tags: tagOptions }, compliance), ...cfFilters, ...cfUnknown];
  // Words for the IDs summaries would otherwise show ("Category: Finance" rather than an ID), and the custom field
  // filters' labels ("Annual cost from: 1000" rather than cf_annual_cost__gte).
  const names = Object.fromEntries([
    ...fields.filter((f) => ID_KEYS.includes(f.key)).flatMap((f) => f.options.map((o) => [`${f.key}=${o.value}`, o.label])),
    ...cfFilters.map((f) => [f.key, f.label]),
  ]);
  /** Opening a saved view replaces every filter in the URL with the view's query (the sort and page size stay), with
   *  `preset=all` (exactly these parameters, never a preset's) and `view=<id>`: the same shape as the link in a weekly
   *  email (catalog/saved_views.list_link). Its columns apply when the view has loaded (applyViewColumns). */
  function openView(view: SavedFilter, query: Query = view.query, replace = false) {
    const next = new URLSearchParams();
    if (kind === "component") next.set("kind", "components");
    for (const k of ["ordering", "page_size"]) if (params.get(k)) next.set(k, params.get(k)!);
    for (const [k, v] of Object.entries(query)) if (v) next.set(k, v);
    next.set("preset", "all");
    // Built-ins aren't marked by ID: they are matched by their query, so changing a filter by hand leaves them.
    if (!view.builtin) next.set("view", view.id);
    setParams(next, { replace });
  }
  /** A chip changed. Changing a filter by hand leaves the preset, keeping what the preset meant as chips
   *  ("Changing any filter by hand clears the preset"). */
  function changeFilter(key: string, value: string, typed = false) {
    if (key === "tag") return update({ tag: value }, !typed);
    const current: Query = custom ? Object.fromEntries(RAW_KEYS.map((k) => [k, params.get(k) ?? ""])) : { ...CLEAR_RAW, ...presetQuery };
    const next: Query = { ...current, [key]: value };
    // When the last chip goes the list shows every record (preset=all), not the default Active view.
    update({ ...next, preset: RAW_KEYS.some((k) => next[k]) ? "" : "all" }, !typed);
  }
  const values: Query = { ...(custom ? Object.fromEntries(raw.map((k) => [k, params.get(k)!])) : {}), tag };
  const write = canWrite(me);
  const admin = me.role === "admin";
  const selecting = write && table.selected.size > 0;
  const importable = kind !== "ai_agent"; // CSV import of agents comes later (no template.csv)
  const openExport = (f: ExportFormat) => { setFormat(f); setDialog("export"); };

  const presets = (
    <div role="group" aria-label="Quick filters" className="inline-flex flex-wrap overflow-hidden rounded-md border border-border-strong bg-card">
      {PRESETS.map((p) => (
        <button key={p.id} type="button" aria-pressed={!custom && preset === p.id} onClick={() => update({ preset: !custom && preset === p.id ? "" : p.id, view: "", ...CLEAR_RAW })}
          className="flex h-8 items-center px-3 text-[13px] text-muted-foreground aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground [&+&]:border-l [&+&]:border-border-strong">
          {p.label}
        </button>
      ))}
    </div>
  );

  const addLabel = `Add ${noun}`;
  const empty = filtered ? (
    <EmptyState title={`No ${nouns} match these filters`}
      action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>
      {q ? `Nothing matches "${q}" with these filters. Try a shorter search or clear the filters.` : `No ${nouns} are in this filter right now.`}
    </EmptyState>
  ) : kind === "application" ? (
    <EmptyState title="No applications yet"
      action={write ? (
        <span className="flex flex-wrap gap-2">
          <Button asChild variant="outline"><Link to="/applications/new">Add application</Link></Button>
          <Button asChild variant="outline"><Link to="/imports">Import CSV</Link></Button>
          <Button asChild variant="outline"><a href={templateUrl("application")} download>Download template</a></Button>
        </span>
      ) : undefined}>
      {write
        ? "A name and a lifecycle status are enough to start. Owners can be added later and show as gaps."
        : "Editors and Admins can add applications. Ask one of them to add the first record."}
    </EmptyState>
  ) : kind === "component" ? (
    <EmptyState title="No components yet"
      action={write ? (
        <span className="flex flex-wrap gap-2">
          <Button asChild variant="outline"><Link to="/imports">Import CSV</Link></Button>
          <Button asChild variant="outline"><a href={templateUrl("component")} download>Download template</a></Button>
        </span>
      ) : undefined}>
      Components are the parts an application is built from. None have been added to this catalog yet.
    </EmptyState>
  ) : (
    <EmptyState title="No AI agents yet"
      action={write ? <Button asChild variant="outline"><Link to="/ai-agents/new">Add AI agent</Link></Button> : undefined}>
      {write
        ? "Record each agent your organization runs: a name and a lifecycle are enough to start. Its owners, data access scope and human-in-loop decision show as gaps until they're recorded."
        : "AI agents are the Copilot Studio, Claude, OpenAI and custom agents your organization runs. Editors and Admins can add them."}
    </EmptyState>
  );

  return (
    <>
      {/* While rows are selected the bulk action bar replaces the header's actions; the title stays. */}
      <PageHead title={plural} desc={kind === "ai_agent" ? "Agents, the models they use and who answers for them. Missing owners and decisions show as gaps." : "Missing owners and dates show as gaps."}>
        {!selecting && <>
          <Button variant="outline" onClick={() => openExport("csv")}>Export CSV…</Button>
          <Button variant="outline" onClick={() => openExport("xlsx")}>Export Excel…</Button>
          {write && importable && <Button asChild variant="outline"><a href={templateUrl(kind)} download>Download template</a></Button>}
          {write && importable && <Button asChild variant="outline"><Link to="/imports">Import CSV</Link></Button>}
          {write && <Button asChild><Link to={newPath(kind)}>{addLabel}</Link></Button>}
        </>}
      </PageHead>
      {selecting && (
        <BulkBar table={table} noun={[noun, nouns]}>
          <Button size="sm" variant="outline" onClick={() => openBulk("bulk")}>Bulk edit…</Button>
          {admin && <Button size="sm" variant="outline" onClick={() => openBulk("delete")}>Bulk delete…</Button>}
        </BulkBar>
      )}
      {/* Right under the bar that opens them: while their code loads, and if it can't load (PartUnavailable, an alert),
          the message is next to the button rather than below the table. The dialogs themselves render in a portal. */}
      {bulkUsed.bulk && (
        <LazyPart what="bulk edit">
          <BulkEditDialog open={dialog === "bulk"} onOpenChange={(o) => setDialog(o ? "bulk" : null)} kind={kind} ids={[...table.selected]}
            onDone={(text) => { setNotice(text); table.clearSelection(); void table.list.refetch(); }} />
        </LazyPart>
      )}
      {bulkUsed.delete && admin && (
        <LazyPart what="bulk delete">
          <BulkDeleteDialog open={dialog === "delete"} onOpenChange={(o) => setDialog(o ? "delete" : null)} kind={kind} ids={[...table.selected]}
            onDone={(text) => { setNotice(text); table.clearSelection(); void table.list.refetch(); }}
            onDeselect={(drop) => table.setSelection(Object.fromEntries([...table.selected].filter((id) => !drop.includes(id)).map((id) => [id, true])))} />
        </LazyPart>
      )}

      {/* Applications and Components are two sidebar entries (and the g a, g c shortcuts); the page no longer repeats them
          as a Kind switch above the list. The owner chose this on 2026-11-10; it shipped in 1.0.0 once
          the bundle work it waited on was done. It also gives the table that row back. */}

      <FilterBar key={kind} id="list" searchLabel={`Search ${nouns}`} searchPlaceholder="Name, description or purpose" search={q} onSearch={(v) => update({ q: v })}
        fields={fields} values={values} onChange={changeFilter} presets={presets} filtered={filtered} onClear={clear}
        saved={<SavedViews kind={kind} query={filterQuery} viewId={viewId} columns={viewColumns} columnsReady={columnsReady} canShare={write}
          bare={!!viewId && !params.get("preset") && raw.length === 0 && !q && !tag}
          onOpen={openView} onClose={() => update({ view: "" })} onColumns={applyViewColumns} onNotice={setNotice} names={names} />} />
      <SuccessNote text={notice} />

      <RecordTable table={table} caption={plural} noun={[noun, nouns]} columns={columns} rowHeader="name" kind={kind}
        rowHref={(a) => recordPath(kind, a.id)!} selectable={write}
        defaultOrder={q ? { text: "best match, then name", field: null } : { text: "name, A to Z", field: "name" }} empty={empty} onClearFilters={clear} />

      <ExportDialog open={dialog === "export"} onOpenChange={(o) => setDialog(o ? "export" : null)} kind={kind} query={filterQuery} format={format} names={names}
        emptyList={!!rows && rows.length === 0} onDone={setNotice} />
    </>
  );
}
