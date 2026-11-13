/** Contracts: the list (RecordTable, every filter in the URL, CSV and Excel export) and the add and edit
 *  form, with a live preview of the annualised cost and the cancellation deadline from `GET /contracts/preview`, so the
 *  rules live only in the API. Money is typed and sent as a string (never a float). An edit sends only what changed,
 *  with the ETag as If-Match: a change someone else saved first is refused (412) with "Load the latest version". The
 *  contract page is contract-detail.tsx, the renew page contract-renew.tsx. Loaded on first use (App.tsx). */
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, apiWithEtag, AUTO_RENEW_LABEL, orgToday, Problem, type AutoRenew, type Person, type PeriodUnit, type Renewal } from "@/api";
import { Breadcrumb, Chip, EmptyState, ErrorBanner, ErrorSummary, Field, Gap, Loading, PageHead, Select, Textarea } from "@/components/app";
import { Combobox, type ComboOption } from "@/components/combobox";
import { FilterBar, type FilterField } from "@/components/filter-bar";
import { RecordTable, useRecordTable, type RecordColumn } from "@/components/record-table";
import { SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useVendorQuickAdd } from "@/features/admin/admin";
import { CustomFieldInputs, customColumns, customFilterFields, unknownFilterFields, useCustomFieldForm, useCustomFields, type CustomField } from "@/features/catalog/custom-fields";
import { useUnsavedGuard } from "@/features/catalog/record-form";
import { DeadlineCell, PeriodField } from "@/features/money/renewals";
import { apiKey, problemText, useApiQuery } from "@/lib/query";
import { canWrite, savedState, useMe } from "@/lib/session";
import {
  AMOUNT_HELP, AMOUNT_PATTERN, BILLING_FREQUENCIES, CONTRACT_STATUSES, FREQUENCY_LABEL, moneyText, type BillingFrequency, type Contract,
  type ContractDetail, type ContractPreview, type ContractStatus, type Ref,
} from "./contract-types";
import { ContractStatusChip, Money, StaleBanner, useDownload } from "./contract-ui";

// ---- list ---------------------------------------------------------------------------------------------------------------

/** The list's filter parameters (beside `q`), as the API names them. */
const FILTER_KEYS = [
  "vendor", "status", "billing_frequency", "auto_renew", "cost_center", "currency", "end_after", "end_before",
  "deadline_after", "deadline_before", "renewal_obligation", "record", "archived",
];

function filterFields(vendors: Ref[], renewals: Renewal[]): FilterField[] {
  return [
    { key: "status", label: "Status", options: CONTRACT_STATUSES.map((s) => ({ value: s.value, label: s.label })), input: "choices" },
    { key: "vendor", label: "Vendor", options: vendors.map((v) => ({ value: v.id, label: v.name })) },
    { key: "billing_frequency", label: "Billing frequency", options: BILLING_FREQUENCIES.map((f) => ({ value: f.value, label: f.label })), input: "choices" },
    { key: "auto_renew", label: "Auto-renews", options: (["yes", "no", "unknown"] as const).map((v) => ({ value: v, label: AUTO_RENEW_LABEL[v] })) },
    { key: "renewal_obligation", label: "Renewal", options: [{ value: "none", label: "Not linked to a renewal" }, ...renewals.map((r) => ({ value: r.id, label: r.name }))] },
    { key: "deadline_after", label: "Cancellation deadline from", options: [], input: "date" },
    { key: "deadline_before", label: "Cancellation deadline to", options: [], input: "date" },
    { key: "end_after", label: "End date from", options: [], input: "date" },
    { key: "end_before", label: "End date to", options: [], input: "date" },
    { key: "cost_center", label: "Cost center", options: [], input: "text" },
    { key: "currency", label: "Currency", options: [], input: "text" },
    { key: "archived", label: "Archived", options: [{ value: "true", label: "Archived only" }, { value: "all", label: "Archived or not" }] },
  ];
}

function columns(fields: CustomField[]): RecordColumn<Contract>[] {
  return [
    { id: "name", header: "Contract", sortField: "name", cell: (c) => (c.archived ? <Chip className="ml-2 align-middle">Archived</Chip> : null) },
    { id: "contract_number", header: "Contract number", defaultVisible: false, cell: (c) => c.contract_number || "—" },
    { id: "vendor", header: "Vendor", cell: (c) => c.vendor?.name ?? <Gap label="No vendor" /> },
    { id: "status", header: "Status", cell: (c) => <ContractStatusChip status={c.status} /> },
    { id: "annualized_cost", header: "Annualized cost", className: "text-right", cell: (c) => (
      c.annualized_cost == null ? <Gap label="No amount" /> : <><Money amount={c.annualized_cost} currency={c.currency} /><span className="block text-xs text-muted-foreground">{FREQUENCY_LABEL[c.billing_frequency]}</span></>
    ) },
    { id: "end_date", header: "End date", sortField: "ends_on", sortText: ["soonest first, none last", "latest first"], cell: (c) => (c.end_date ? <time className="tabular" dateTime={c.end_date}>{c.end_date}</time> : <Gap label="No end date" />) },
    { id: "deadline", header: "Cancellation deadline", sortField: "deadline_on", sortText: ["soonest first, none last", "latest first"], className: "whitespace-normal min-w-44",
      cell: (c) => <DeadlineCell deadline={c.deadline} closed={c.status === "cancelled" || c.status === "expired" || c.status === "renewed"} /> },
    { id: "renewal", header: "Renewal", className: "whitespace-normal", cell: (c) => (c.renewal_obligation
      ? <><Link to={`/renewals/${c.renewal_obligation.id}`} className="underline">{c.renewal_obligation.name}</Link>{c.drives_renewal && <span className="block text-xs text-muted-foreground">Its dates come from this contract</span>}</>
      : <span className="text-muted-foreground">Not linked</span>) },
    { id: "cost_center", header: "Cost center", cell: (c) => c.cost_center || "—" },
    { id: "lines", header: "Lines", defaultVisible: false, className: "tabular", cell: (c) => c.line_count },
    { id: "pools", header: "License pools", defaultVisible: false, className: "tabular", cell: (c) => c.license_pool_count },
    { id: "renewal_owner", header: "Renewal owner", defaultVisible: false, cell: (c) => c.renewal_owner?.name ?? <Gap label="No renewal owner" /> },
    ...customColumns<Contract>(fields),
  ];
}

export function ContractsList() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const custom = useCustomFields("contract").fields;
  const cfFilters = useMemo(() => customFilterFields(custom ?? []), [custom]);
  const cfUnknown = custom ? unknownFilterFields(params, cfFilters) : [];
  const keys = [...FILTER_KEYS, ...new Set([...cfFilters.map((f) => f.key), ...[...params.keys()].filter((k) => k.startsWith("cf_"))])];
  const vendors = useApiQuery<Ref[]>("vendors", { staleTime: 30_000 });
  const renewals = useApiQuery<Renewal[]>("renewals", { staleTime: 30_000 });
  const q = (params.get("q") ?? "").trim();
  const values = Object.fromEntries(keys.filter((k) => params.get(k)).map((k) => [k, params.get(k)!]));
  const query = { ...values, ...(q ? { q } : {}) };
  const table = useRecordTable<Contract>({ id: "contracts", path: "contracts", query });
  const cols = useMemo(() => columns(custom ?? []), [custom]);
  const [notice, setNotice] = useState("");
  const exporter = useDownload();
  const write = canWrite(me);

  function update(changes: Record<string, string>, push = !("q" in changes)) {
    setParams((cur) => {
      const next = new URLSearchParams(cur);
      next.delete("cursor");
      next.delete("page");
      for (const [k, v] of Object.entries(changes)) { if (v) next.set(k, v); else next.delete(k); }
      return next;
    }, { replace: !push });
  }
  const filtered = Object.keys(query).length > 0;
  const clear = () => update(Object.fromEntries([...keys, "q"].map((k) => [k, ""])), true);
  const today = orgToday(me.organization.timezone);
  const presets: { id: string; label: string; values: Record<string, string> }[] = [
    { id: "due90", label: "Deadline in 90 days", values: { deadline_after: today, deadline_before: orgToday(me.organization.timezone, 90) } },
    { id: "unlinked", label: "Not linked to a renewal", values: { renewal_obligation: "none" } },
    { id: "archived", label: "Archived", values: { archived: "true" } },
  ];
  const isPreset = (v: Record<string, string>) => Object.keys(values).length === Object.keys(v).length && Object.entries(v).every(([k, x]) => values[k] === x);
  const presetBar = (
    <div role="group" aria-label="Quick filters" className="inline-flex flex-wrap overflow-hidden rounded-md border border-border-strong bg-card">
      {presets.map((p) => {
        const on = isPreset(p.values);
        return (
          <button key={p.id} type="button" aria-pressed={on}
            onClick={() => update({ ...Object.fromEntries(keys.map((k) => [k, ""])), ...(on ? {} : p.values) })}
            className="flex h-8 items-center px-3 text-[13px] text-muted-foreground aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground [&+&]:border-l [&+&]:border-border-strong">
            {p.label}
          </button>
        );
      })}
    </div>
  );
  async function exportAs(format: "csv" | "xlsx") {
    const name = await exporter.download(`contracts/export.${format}?${new URLSearchParams(query)}`, `softwarden-contracts.${format}`);
    if (name) setNotice(`Exported the contracts in this view to ${name}. The export is recorded in the audit log.`);
  }
  const empty = filtered ? (
    <EmptyState title="No contracts match these filters" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>
      {q ? `Nothing matches "${q}" with these filters. Try a shorter search or clear the filters.` : "No contracts are in this filter right now."}
    </EmptyState>
  ) : (
    <EmptyState title="No contracts yet" action={write ? (
      <span className="flex flex-wrap gap-2">
        <Button asChild variant="outline"><Link to="/contracts/new">Add contract</Link></Button>
        <Button asChild variant="outline"><Link to="/imports">Import CSV</Link></Button>
      </span>
    ) : undefined}>
      {write
        ? "A name is enough to start. Add the amount, billing frequency and end date to see the annualized cost and the cancellation deadline; link a renewal so its reminders follow the contract."
        : "Contracts hold what your organization pays for software and when it renews. Editors and Admins can add them."}
    </EmptyState>
  );
  return (
    <>
      <PageHead title="Contracts" desc="What each agreement costs a year, when it ends, the last day to cancel it, and the renewal that tracks it.">
        <Button variant="outline" disabled={exporter.busy} onClick={() => void exportAs("csv")}>Export CSV</Button>
        <Button variant="outline" disabled={exporter.busy} onClick={() => void exportAs("xlsx")}>Export Excel</Button>
        {write && <Button asChild><Link to="/contracts/new">Add contract</Link></Button>}
      </PageHead>
      {exporter.error && <ErrorBanner message={exporter.error} />}
      <FilterBar id="contracts" searchLabel="Search contracts" searchPlaceholder="Name, contract or PO number, vendor" search={q} onSearch={(v) => update({ q: v })}
        fields={[...filterFields(vendors.data ?? [], renewals.data ?? []), ...cfFilters, ...cfUnknown]} values={values}
        onChange={(k, v, typed) => update({ [k]: v }, !typed)} presets={presetBar} filtered={filtered} onClear={clear} />
      <SuccessNote text={notice} />
      <RecordTable table={table} caption="Contracts" noun={["contract", "contracts"]} columns={cols} rowHeader="name"
        rowHref={(c) => `/contracts/${c.id}`} selectable={false}
        defaultOrder={q ? { text: "best match, then name", field: null } : { text: "contract, A to Z", field: "name" }} empty={empty} onClearFilters={clear} />
    </>
  );
}

// ---- add and edit -------------------------------------------------------------------------------------------------------

type LinkMode = "none" | "link" | "create";
type Values = {
  name: string; vendor: string; contract_number: string; po_number: string; amount: string; currency: string;
  billing_frequency: BillingFrequency; start_date: string; end_date: string; auto_renew: AutoRenew; notice_value: string; notice_unit: PeriodUnit;
  renewal_owner: string; status: ContractStatus; cost_center: string; notes: string; link: LinkMode; renewal: string;
};
const EMPTY: Values = {
  name: "", vendor: "", contract_number: "", po_number: "", amount: "", currency: "USD", billing_frequency: "annual", start_date: "", end_date: "",
  auto_renew: "unknown", notice_value: "", notice_unit: "days", renewal_owner: "", status: "active", cost_center: "", notes: "", link: "none", renewal: "",
};
const valuesOf = (c: ContractDetail): Values => ({
  name: c.name, vendor: c.vendor?.id ?? "", contract_number: c.contract_number, po_number: c.po_number, amount: c.amount ?? "", currency: c.currency,
  billing_frequency: c.billing_frequency, start_date: c.start_date ?? "", end_date: c.end_date ?? "", auto_renew: c.auto_renew,
  notice_value: c.notice ? String(c.notice.value) : "", notice_unit: c.notice?.unit ?? "days", renewal_owner: c.renewal_owner?.id ?? "",
  status: c.status, cost_center: c.cost_center, notes: c.notes, link: c.renewal_obligation ? "link" : "none", renewal: c.renewal_obligation?.id ?? "",
});

/** The contract's own fields as the API takes them. */
function fieldsOf(v: Values): Record<string, unknown> {
  return {
    name: v.name.trim(), vendor: v.vendor || null, contract_number: v.contract_number.trim(), po_number: v.po_number.trim(),
    amount: v.amount.trim() || null, currency: v.currency.trim().toUpperCase(), billing_frequency: v.billing_frequency,
    start_date: v.start_date || null, end_date: v.end_date || null, auto_renew: v.auto_renew,
    notice_value: v.notice_value ? Number(v.notice_value) : null, notice_unit: v.notice_value ? v.notice_unit : "",
    renewal_owner: v.renewal_owner || null, status: v.status, cost_center: v.cost_center.trim(), notes: v.notes,
  };
}

/** The request body: every field with a value on create, only what changed on edit (the notice as a pair), and the
 *  renewal link when it is set or changed. */
function bodyOf(v: Values, before: Values | null): Record<string, unknown> {
  const all = fieldsOf(v);
  const link: Record<string, unknown> = v.link === "create" ? { create_renewal_obligation: true } : { renewal_obligation: v.link === "link" ? v.renewal || null : null };
  if (!before) {
    const created = Object.fromEntries(Object.entries(all).filter(([k, x]) => k === "name" || (x !== null && x !== "")));
    return { ...created, ...(v.link === "none" ? {} : link) };
  }
  const was = fieldsOf(before);
  const changed = Object.fromEntries(Object.entries(all).filter(([k, x]) => JSON.stringify(x) !== JSON.stringify(was[k])));
  if ("notice_value" in changed || "notice_unit" in changed) Object.assign(changed, { notice_value: all.notice_value, notice_unit: all.notice_unit });
  const linkChanged = v.link !== before.link || (v.link === "link" && v.renewal !== before.renewal);
  return { ...changed, ...(linkChanged ? link : {}) };
}

/** The annualised cost and deadline the API would work out from the form so far (debounced), announced politely. */
function usePreview(v: Values) {
  const [preview, setPreview] = useState<{ data: ContractPreview | null; error: string }>({ data: null, error: "" });
  const amount = v.amount.trim();
  const amountOk = !amount || AMOUNT_PATTERN.test(amount);
  useEffect(() => {
    const p = new URLSearchParams({ billing_frequency: v.billing_frequency, auto_renew: v.auto_renew });
    if (amount && amountOk) p.set("amount", amount);
    if (v.start_date) p.set("start_date", v.start_date);
    if (v.end_date) p.set("end_date", v.end_date);
    if (v.notice_value) { p.set("notice_value", v.notice_value); p.set("notice_unit", v.notice_unit); }
    let live = true;
    const t = setTimeout(() => {
      api<ContractPreview>(`contracts/preview?${p}`)
        .then((d) => { if (live) setPreview({ data: d, error: "" }); })
        .catch((e) => { if (live) setPreview({ data: null, error: e instanceof Problem ? Object.values(e.errors).flat().join(" ") || e.detail : "Couldn't work out the cost and deadline." }); });
    }, 300);
    return () => { live = false; clearTimeout(t); };
  }, [amount, amountOk, v.billing_frequency, v.start_date, v.end_date, v.auto_renew, v.notice_value, v.notice_unit]);
  return { ...preview, amountOk };
}

export function NewContract() {
  const me = useMe();
  const custom = useCustomFields("contract");
  if (!canWrite(me)) return <ReadOnly />;
  if (custom.error) return <ErrorBanner message={problemText(custom.error, "Couldn't load the custom fields.")} onRetry={custom.refetch} />;
  if (!custom.fields) return <Loading what="the form" />;
  return <ContractForm fields={custom.fields} />;
}

export function EditContract() {
  const { id } = useParams();
  return <EditContractPage key={id} id={id!} />;
}

function EditContractPage({ id }: { id: string }) {
  const me = useMe();
  const custom = useCustomFields("contract");
  // Read with the ETag (sent back as If-Match). Not cached: an edit always starts from the server's copy.
  const rec = useQuery({ queryKey: [...apiKey(`contracts/${id}`), "etag"], queryFn: () => apiWithEtag<ContractDetail>(`contracts/${id}`), gcTime: 0, retry: false, staleTime: Infinity });
  const [loaded, setLoaded] = useState<{ data: ContractDetail; etag: string | null } | null>(null);
  useEffect(() => { if (rec.data && !loaded) setLoaded(rec.data); }, [rec.data, loaded]);
  const [round, setRound] = useState(0);
  if (!canWrite(me)) return <ReadOnly />;
  if (rec.error instanceof Problem && rec.error.status === 404) {
    return <><PageHead title="Contract not found" desc="This contract doesn't exist or you don't have access." /><Button asChild variant="outline"><Link to="/contracts">Back to Contracts</Link></Button></>;
  }
  if (rec.error) return <ErrorBanner message={problemText(rec.error, "Couldn't load this contract.")} onRetry={() => void rec.refetch()} />;
  if (custom.error) return <ErrorBanner message={problemText(custom.error, "Couldn't load the custom fields.")} onRetry={custom.refetch} />;
  if (!loaded || !custom.fields) return <Loading what="the contract" />;
  async function loadLatest() {
    const latest = await rec.refetch();
    if (!latest.data) return;
    setLoaded(latest.data);
    setRound((n) => n + 1);
  }
  return <ContractForm key={round} fields={custom.fields} existing={loaded.data} etag={loaded.etag} onLoadLatest={() => void loadLatest()} />;
}

function ReadOnly() {
  return (
    <>
      <PageHead title="You can't change contracts" desc="Editors and Admins can add and change contracts. You can read them." />
      <Button asChild variant="outline"><Link to="/contracts">Back to Contracts</Link></Button>
    </>
  );
}

function ContractForm({ fields, existing, etag, onLoadLatest }: { fields: CustomField[]; existing?: ContractDetail; etag?: string | null; onLoadLatest?: () => void }) {
  const navigate = useNavigate();
  const people = useApiQuery<Person[]>("people", { staleTime: 30_000 });
  const vendors = useApiQuery<Ref[]>("vendors", { staleTime: 30_000 });
  const renewals = useApiQuery<Renewal[]>("renewals", { staleTime: 30_000 });
  const initial = useMemo(() => (existing ? valuesOf(existing) : EMPTY), [existing]);
  const [v, setV] = useState<Values>(initial);
  const cf = useCustomFieldForm(fields, existing ? "edit" : "create", existing?.custom_field_data);
  const [addedVendors, setAddedVendors] = useState<Ref[]>([]);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [formError, setFormError] = useState("");
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState(false);
  const preview = usePreview(v);
  const dirty = JSON.stringify(v) !== JSON.stringify(initial) || cf.dirty;
  const guard = useUnsavedGuard(dirty && !saving);
  const set = <K extends keyof Values>(k: K) => (value: Values[K]) => setV((cur) => ({ ...cur, [k]: value }));
  const vendorAdd = useVendorQuickAdd({
    focusId: "vendor",
    onAdded: (made) => { setAddedVendors((cur) => [...cur, made]); set("vendor")(made.id); setErrors(({ vendor: _drop, ...rest }) => rest); },
    onError: (text) => setErrors((cur) => ({ ...cur, vendor: [text] })),
  });
  const back = existing ? `/contracts/${existing.id}` : "/contracts";
  const loadError = people.error || vendors.error || renewals.error;
  if (loadError) return <ErrorBanner message={problemText(loadError, "Couldn't load the form.")} onRetry={() => { void people.refetch(); void vendors.refetch(); void renewals.refetch(); }} />;
  if (!people.data || !vendors.data || !renewals.data) return <Loading what="the form" />;

  const vendorOptions: ComboOption[] = [...vendors.data, ...addedVendors.filter((a) => !vendors.data!.some((b) => b.id === a.id))].map((x) => ({ id: x.id, label: x.name }));
  const peopleOptions: ComboOption[] = people.data.filter((p) => p.active || p.id === existing?.renewal_owner?.id)
    .map((p) => ({ id: p.id, label: p.active ? p.name : `${p.name} (inactive)`, hint: p.department?.name }));
  // Renewals another contract drives can't be linked (the API refuses with "Unlink one of them first"); they are listed
  // with that said, so the refusal isn't a surprise.
  const renewalOptions = renewals.data.filter((r) => !r.archived);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const found: Record<string, string[]> = {};
    if (!v.name.trim()) found.name = ["Enter a name."];
    if (v.amount.trim() && !AMOUNT_PATTERN.test(v.amount.trim())) found.amount = ["Enter the amount as a number with up to two decimal places, such as 12000 or 999.50."];
    if (v.currency.trim() && !/^[A-Za-z]{3}$/.test(v.currency.trim())) found.currency = ["Enter a three-letter currency code, such as USD or EUR."];
    if (v.link === "link" && !v.renewal) found.renewal_obligation = ["Choose the renewal to link, or choose another option."];
    const custom = cf.build();
    Object.assign(found, custom.errors);
    setErrors(found);
    setFormError("");
    setStale(false);
    if (Object.keys(found).length) return;
    const body = { ...bodyOf(v, existing ? initial : null), ...(custom.data ? { custom_field_data: custom.data } : {}) };
    if (existing && !Object.keys(body).length) { navigate(back, { state: { notice: "No changes to save." } }); return; }
    setSaving(true);
    try {
      const saved = await api<Contract>(existing ? `contracts/${existing.id}` : "contracts", {
        method: existing ? "PATCH" : "POST", body, headers: existing && etag ? { "If-Match": etag } : undefined,
      });
      navigate(`/contracts/${saved.id}`, { state: savedState(saved.name, existing ? "Contract saved." : `Contract added.${saved.annualized_cost ? ` Annualized cost ${moneyText(saved.annualized_cost, saved.currency)}.` : ""}`) });
    } catch (err) {
      setSaving(false);
      if (err instanceof Problem && err.status === 412) setStale(true);
      else if (err instanceof Problem && Object.keys(err.errors).length) setErrors(err.errors);
      else setFormError(`${problemText(err)} The contract wasn't saved. Nothing you typed was lost.`);
    }
  }

  const linkError = errors.renewal_obligation ?? errors.create_renewal_obligation;
  const chosen = renewalOptions.find((r) => r.id === v.renewal);
  return (
    <>
      <Breadcrumb trail={[{ to: "/contracts", label: "Contracts" }, ...(existing ? [{ to: back, label: existing.name }] : [])]} current={existing ? "Edit" : "Add contract"} />
      <PageHead title={existing ? `Edit ${existing.name}` : "Add contract"} docTitle={existing ? "Edit contract" : "Add contract"}
        desc="Only the name is required. The annualized cost and the cancellation deadline are worked out from what you enter." />
      {stale && onLoadLatest && <StaleBanner what="contract" onLoad={() => { setStale(false); onLoadLatest(); }} />}
      <Card className="max-w-4xl">
        <CardContent className="pt-6">
          <form onSubmit={submit} noValidate aria-label={existing ? "Edit contract" : "Add contract"}>
            <fieldset disabled={saving} className="m-0 min-w-0 border-0 p-0">
              {formError && <ErrorBanner message={formError} />}
              <ErrorSummary errors={errors} focusOnShow="field" />
              <div className="grid gap-x-6 md:grid-cols-2">
                <div className="md:col-span-2">
                  <Field id="name" label="Name (required)" errors={errors} help="For example: Microsoft 365 E5 enterprise agreement.">
                    <Input id="name" value={v.name} onChange={(e) => set("name")(e.target.value)} autoComplete="off" />
                  </Field>
                </div>
                <Field id="vendor" label="Vendor" errors={errors}>
                  <Combobox id="vendor" options={vendorOptions} value={v.vendor} onChange={set("vendor")} placeholder="Search vendors…" clearLabel="Not set (gap)" listLabel="Vendor suggestions"
                    add={{ label: (typed) => `+ Add vendor "${typed}"`, onAdd: (typed) => void vendorAdd.add(typed.trim()) }} />
                </Field>
                <Field id="renewal_owner" label="Renewal owner" errors={errors} help="Who decides whether to renew. An active person.">
                  <Combobox id="renewal_owner" options={peopleOptions} value={v.renewal_owner} onChange={set("renewal_owner")} placeholder="Search people…" clearLabel="Not set" listLabel="People suggestions" />
                </Field>
                <Field id="contract_number" label="Contract number" errors={errors}>
                  <Input id="contract_number" value={v.contract_number} onChange={(e) => set("contract_number")(e.target.value)} autoComplete="off" />
                </Field>
                <Field id="po_number" label="PO number" errors={errors}>
                  <Input id="po_number" value={v.po_number} onChange={(e) => set("po_number")(e.target.value)} autoComplete="off" />
                </Field>
              </div>

              <fieldset className="mt-2 border-t pt-4">
                <legend className="mb-3 font-display text-section font-semibold">Money</legend>
                <div className="grid gap-x-6 md:grid-cols-3">
                  <Field id="amount" label="Amount" errors={errors} help={AMOUNT_HELP[v.billing_frequency]}>
                    <Input id="amount" inputMode="decimal" value={v.amount} onChange={(e) => set("amount")(e.target.value.replace(/[^0-9.]/g, ""))} autoComplete="off" placeholder="12000.00" />
                  </Field>
                  <Field id="currency" label="Currency" errors={errors} help="Three letters. Amounts are never converted.">
                    <Input id="currency" value={v.currency} maxLength={3} onChange={(e) => set("currency")(e.target.value.toUpperCase())} autoComplete="off" className="w-24 uppercase" />
                  </Field>
                  <Field id="billing_frequency" label="Billing frequency" errors={errors}>
                    <Select id="billing_frequency" value={v.billing_frequency} onChange={(e) => set("billing_frequency")(e.target.value as BillingFrequency)}>
                      {BILLING_FREQUENCIES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
                    </Select>
                  </Field>
                  <div className="md:col-span-3">
                    <Field id="cost_center" label="Cost center" errors={errors} help="Spend reports use a line's record's cost center first, then this one.">
                      <Input id="cost_center" value={v.cost_center} onChange={(e) => set("cost_center")(e.target.value)} autoComplete="off" className="max-w-xs" />
                    </Field>
                  </div>
                </div>
              </fieldset>

              <fieldset className="mt-2 border-t pt-4">
                <legend className="mb-3 font-display text-section font-semibold">Term and notice</legend>
                <div className="grid gap-x-6 md:grid-cols-2">
                  <Field id="start_date" label="Start date" errors={errors}>
                    <Input id="start_date" type="date" value={v.start_date} onChange={(e) => set("start_date")(e.target.value)} />
                  </Field>
                  <Field id="end_date" label="End date" errors={errors} help="The cancellation deadline counts back from it. Needed to link a renewal.">
                    <Input id="end_date" type="date" value={v.end_date} onChange={(e) => set("end_date")(e.target.value)} />
                  </Field>
                  <fieldset className="mb-3 flex min-w-0 flex-col gap-1.5" aria-describedby={["auto_renew-help", errors.auto_renew && "auto_renew-error"].filter(Boolean).join(" ")}>
                    <legend className="mb-1.5 text-sm font-medium leading-none">Auto-renews</legend>
                    <div className="flex flex-wrap items-center gap-5">
                      {(["yes", "no", "unknown"] as const).map((x, i) => (
                        <label key={x} className="flex min-h-6 items-center gap-2 text-sm">
                          <input type="radio" id={i === 0 ? "auto_renew" : undefined} name="auto_renew" value={x} checked={v.auto_renew === x} onChange={() => set("auto_renew")(x)} className="h-4 w-4 accent-primary" />
                          {AUTO_RENEW_LABEL[x]}
                        </label>
                      ))}
                    </div>
                    <span id="auto_renew-help" className="text-sm text-muted-foreground">Unknown is treated as auto-renewing and shows as a gap.</span>
                    {errors.auto_renew && <span id="auto_renew-error" className="text-sm text-error-foreground">{errors.auto_renew.join(" ")}</span>}
                  </fieldset>
                  <PeriodField id="notice" label="Notice period" errors={errors} value={v.notice_value} unit={v.notice_unit}
                    onValue={set("notice_value")} onUnit={set("notice_unit")} help="How long before the end date you must cancel. Leave blank if the contract doesn't say." />
                  <Field id="status" label="Status" errors={errors} help="Signed terms become Active on their start date; ended ones Renewed or Expired.">
                    <Select id="status" value={v.status} onChange={(e) => set("status")(e.target.value as ContractStatus)}>
                      {CONTRACT_STATUSES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                    </Select>
                  </Field>
                </div>
                <div className="mb-3 rounded-md border bg-muted/40 px-3 py-2 text-sm" role="status" aria-live="polite" data-testid="contract-preview">
                  {!preview.amountOk ? "Enter the amount as a number to see the annualized cost."
                    : preview.error ? <span><span aria-hidden="true">! </span>{preview.error}</span>
                      : preview.data ? (
                        <>
                          <p><strong>Annualized cost: </strong>{moneyText(preview.data.annualized_cost, v.currency.toUpperCase() || "USD") ?? "none yet"}
                            <span className="block text-muted-foreground">{preview.data.annualization_note}</span></p>
                          <p className="mt-1"><strong>Cancellation deadline: </strong>{preview.data.deadline.date ? <>{preview.data.deadline.date} ({preview.data.deadline.label_text}){preview.data.deadline.timing && `, ${preview.data.deadline.timing.text}`}</> : "none yet"}
                            <span className="block text-muted-foreground">{preview.data.deadline.explanation}</span></p>
                        </>
                      ) : "Working out the annualized cost and deadline…"}
                </div>
              </fieldset>

              <fieldset className="mt-2 border-t pt-4" aria-describedby={["renewal-help", linkError && v.link !== "link" && "renewal-error"].filter(Boolean).join(" ")}>
                <legend className="mb-1 font-display text-section font-semibold">Renewal</legend>
                <p id="renewal-help" className="mb-3 text-sm text-muted-foreground">
                  A linked renewal takes this contract's end date, notice period and auto-renewal, so its reminders and this deadline never disagree.
                </p>
                <div className="flex flex-col gap-2">
                  {([["none", existing?.renewal_obligation ? "Unlink (the renewal keeps the dates it has)" : "Not linked"], ["link", "Link an existing renewal"], ["create", "Create a renewal for this contract"]] as const)
                    .filter(([m]) => m !== "create" || !existing?.renewal_obligation)
                    .map(([m, label]) => (
                      <label key={m} className="flex min-h-6 items-center gap-2 text-sm">
                        <input type="radio" name="renewal_link" id={m === "create" ? "create_renewal_obligation" : m === "none" ? "renewal_link" : undefined} value={m}
                          checked={v.link === m} onChange={() => set("link")(m)} className="h-4 w-4 accent-primary" />
                        {label}
                      </label>
                    ))}
                </div>
                {v.link === "link" && (
                  <div className="mt-3 max-w-md">
                    <Field id="renewal_obligation" label="Renewal to link" errors={linkError ? { renewal_obligation: linkError } : {}} help={chosen?.contract && chosen.contract.id !== existing?.id ? `Its dates come from the contract ${chosen.contract.name}; linking this one too is refused unless it renews or is renewed by that contract.` : undefined}>
                      <Select id="renewal_obligation" value={v.renewal} onChange={(e) => set("renewal")(e.target.value)}>
                        <option value="">Choose a renewal…</option>
                        {renewalOptions.map((r) => <option key={r.id} value={r.id}>{r.name}{r.contract && r.contract.id !== existing?.id ? ` (dates from ${r.contract.name})` : ""}</option>)}
                      </Select>
                    </Field>
                  </div>
                )}
                {v.link === "create" && <p className="mt-2 text-sm text-muted-foreground">The renewal is named after this contract, with its vendor, renewal owner, end date, notice period and the applications on its lines. It needs an end date.</p>}
                {linkError && v.link !== "link" && <p id="renewal-error" className="mt-2 text-sm text-error-foreground">{linkError.join(" ")}</p>}
              </fieldset>

              <div className="mt-4 border-t pt-4">
                <Field id="notes" label="Notes" errors={errors}>
                  <Textarea id="notes" rows={3} value={v.notes} onChange={(e) => set("notes")(e.target.value)} />
                </Field>
              </div>
              <div className="grid gap-x-6 md:grid-cols-2">
                <CustomFieldInputs form={cf} errors={errors} />
              </div>
              <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
                <Button asChild variant="outline"><Link to={back}>Cancel</Link></Button>
                <Button type="submit" id="contract-save">{saving ? "Saving…" : existing ? "Save contract" : "Add contract"}</Button>
              </div>
            </fieldset>
          </form>
          {guard}
          {vendorAdd.dialog}
        </CardContent>
      </Card>
    </>
  );
}
