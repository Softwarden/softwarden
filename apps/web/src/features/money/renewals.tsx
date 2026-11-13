import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  api, AUTO_RENEW_LABEL, AutoRenew, CoveredApp, Deadline, DECISION_LABEL, Person, PeriodUnit, Problem, Renewal, UserRef,
} from "@/api";
import { canWrite, savedState, useMe } from "@/lib/session";
import { Breadcrumb, Chip, EmptyState, ErrorBanner, ErrorSummary, Field, Gap, Loading, PageHead, Select, TableRegion, useLoad } from "@/components/app";
import { Combobox, ComboOption } from "@/components/combobox";
import { PickedRecord, RecordPicker } from "@/components/record-picker";
import { chip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useUnsavedGuard } from "@/features/catalog/record-form";
import { useVendorQuickAdd } from "@/features/admin/admin";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { isStaleWrite } from "@/lib/if-match";

/** Renewals: the list and the add and edit form. The renewal page is in
 *  renewal-detail.tsx. The action deadline is always worked out by the API (renewals/preview while typing), so the
 *  rules live in one place. A decision is shown apart from completion; neither is ever implied by time passing. */

// ---- shared pieces ----------------------------------------------------------------------------------------------------

const LABEL_RULE: Record<string, string> = {
  entered: "Entered: the deadline was typed in.",
  calculated: "Calculated: renewal date minus the notice period.",
  assumed: "Assumed: renewal date minus the default lead time, until a notice period or deadline is recorded.",
  renewal_action: "Renewal action: renewal date minus the lead time, to renew before it expires.",
};

/** Date, basis label (as text) and timing; overdue says so in words and with a glyph, not colour alone. */
export function DeadlineCell({ deadline, closed }: { deadline: Deadline; closed?: boolean }) {
  if (!deadline.date) return <Gap label="No deadline" />;
  const t = deadline.timing;
  return (
    <span className="inline-flex flex-col gap-0.5">
      <span className="inline-flex flex-wrap items-center gap-1.5">
        <span className="tabular font-semibold">{deadline.date}</span>
        {deadline.label && (
          <span className={chip({ state: deadline.label === "assumed" ? "gap" : "neutral" })} title={LABEL_RULE[deadline.label]}>
            {deadline.label === "assumed" && <span aria-hidden="true">⚠</span>}{deadline.label_text}
          </span>
        )}
      </span>
      {deadline.label && <span className="sr-only">{LABEL_RULE[deadline.label]}</span>}
      {closed ? <span className="text-sm text-muted-foreground">Cycle complete</span>
        : t && <span className={t.overdue ? "text-sm font-semibold text-error-foreground" : "text-sm text-muted-foreground"}>{t.overdue && <span aria-hidden="true">! </span>}{t.text}</span>}
    </span>
  );
}

export const decisionText = (r: { decision: string | null }) => (r.decision ? DECISION_LABEL[r.decision as keyof typeof DECISION_LABEL] : "No decision");

/** The chosen applications as a list with Remove, and a search to add one more (applications only). */
export function CoverPicker({ id, value, onChange }: { id: string; value: CoveredApp[]; onChange: (apps: CoveredApp[]) => void }) {
  const add = (r: PickedRecord | null) => {
    if (r && !value.some((a) => a.id === r.id)) onChange([...value, { id: r.id, name: r.name, lifecycle: r.lifecycle ?? "", archived: false }]);
  };
  return (
    <div className="flex flex-col gap-2">
      {value.length > 0 ? (
        <ul className="flex flex-wrap gap-2" aria-label="Covered applications">
          {value.map((a) => (
            <li key={a.id} className="inline-flex items-center gap-1.5 rounded-md border bg-muted/40 py-0.5 pl-2.5 pr-1 text-sm">
              {a.name}{a.lifecycle === "retired" && <span className="text-muted-foreground"> (Retired)</span>}
              <Button type="button" size="xs" variant="ghost" onClick={() => onChange(value.filter((x) => x.id !== a.id))}>
                Remove<span className="sr-only"> {a.name}</span>
              </Button>
            </li>
          ))}
        </ul>
      ) : <p className="text-sm text-muted-foreground">No applications yet. A renewal without applications is allowed and shows as a gap.</p>}
      <RecordPicker id={id} kinds={["application"]} value={null} onChange={add} placeholder="Add an application…" listLabel="Application suggestions" />
    </div>
  );
}

// ---- list ---------------------------------------------------------------------------------------------------------------

const QUICK = [
  { id: "due30", label: "Due in 30 days", query: { due_within: "30" } },
  { id: "overdue", label: "Overdue", query: { overdue: "true" } },
  { id: "gaps", label: "Has gaps", query: { has_gaps: "true" } },
  { id: "nodecision", label: "No decision", query: { decision: "none" } },
  { id: "mine", label: "Assigned to me", query: { mine: "true" } },
] as const;
const DEBOUNCE_MS = 300;

export function RenewalsList() {
  const me = useMe();
  const stewards = useLoad<UserRef[]>("stewards");
  const [params, setParams] = useSearchParams();
  const quick = QUICK.find((f) => f.id === params.get("quick"));
  const auto = params.get("auto") ?? "";
  const followup = params.get("followup") ?? "";
  const archived = params.get("archived") === "1";
  const q = (params.get("q") ?? "").trim();
  const [text, setText] = useState(params.get("q") ?? "");
  useEffect(() => { setText(params.get("q") ?? ""); }, [params]);
  const paramsRef = useRef(params);
  paramsRef.current = params;
  function update(changes: Record<string, string>, push = !("q" in changes)) {
    const next = new URLSearchParams(paramsRef.current);
    for (const [k, v] of Object.entries(changes)) { if (v) next.set(k, v); else next.delete(k); }
    setParams(next, { replace: !push });
  }
  useEffect(() => {
    if (text.trim() === q) return;
    const t = setTimeout(() => update({ q: text.trim() }), DEBOUNCE_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  const query = new URLSearchParams({
    ...(quick?.query ?? {}), ...(auto ? { auto_renew: auto } : {}), ...(followup ? { followup } : {}),
    ...(archived ? { archived: "true" } : {}), ...(q ? { q } : {}),
  }).toString();
  const load = useLoad<Renewal[]>(`renewals${query ? `?${query}` : ""}`);
  const rows = load.data;
  const filtered = Boolean(quick || auto || followup || archived || q);
  const clear = () => { setText(""); update({ quick: "", auto: "", followup: "", archived: "", q: "" }, true); };
  const write = canWrite(me);
  const count = rows?.length ?? 0;
  return (
    <>
      <PageHead title="Renewals" desc="Everything that renews, its next action deadline and how that date was worked out, who follows up, and what was decided. A decision isn't completion.">
        {write && <Button asChild><Link to="/renewals/new">Add renewal</Link></Button>}
      </PageHead>
      <div className="mb-3 flex flex-wrap items-center gap-2" role="group" aria-label="Quick filters">
        {QUICK.map((f) => (
          <button key={f.id} type="button" aria-pressed={quick?.id === f.id} onClick={() => update({ quick: quick?.id === f.id ? "" : f.id })}
            className="h-8 rounded-md border border-border-strong bg-card px-3 text-[13px] text-muted-foreground aria-pressed:border-accent-line aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground">
            {f.label}
          </button>
        ))}
      </div>
      <div className="mb-4 flex flex-wrap items-end gap-x-6 gap-y-3">
        <form role="search" className="flex w-full max-w-sm flex-col gap-1.5" onSubmit={(e) => { e.preventDefault(); update({ q: text.trim() }); }}>
          <Label htmlFor="list-search">Search renewals</Label>
          <Input id="list-search" type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder="Renewal, vendor or application" />
        </form>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="auto-filter">Auto-renewal</Label>
          <Select id="auto-filter" value={auto} onChange={(e) => update({ auto: e.target.value })}>
            <option value="">Any</option>
            {(["yes", "no", "unknown"] as const).map((v) => <option key={v} value={v}>{AUTO_RENEW_LABEL[v]}</option>)}
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="followup-filter">IT follow-up</Label>
          <Select id="followup-filter" value={followup} onChange={(e) => update({ followup: e.target.value })}>
            <option value="">Anyone</option>
            <option value="none">Nobody (gap)</option>
            {(stewards.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
          </Select>
        </div>
        <label className="flex h-9 items-center gap-2">
          <input type="checkbox" checked={archived} onChange={(e) => update({ archived: e.target.checked ? "1" : "" })} />
          Archived only
        </label>
        {filtered && <Button variant="outline" size="sm" onClick={clear}>Clear filters</Button>}
      </div>
      <p className="sr-only" aria-live="polite">{rows ? `Showing ${count} ${count === 1 ? "renewal" : "renewals"}.` : ""}</p>
      {load.error && <ErrorBanner message={`Couldn't load renewals. ${load.error}`} onRetry={load.reload} />}
      {!rows && !load.error && <Loading what="renewals" rows={5} />}
      {rows && rows.length === 0 && (filtered ? (
        <EmptyState title="No renewals match these filters" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>
          {q ? `Nothing matches "${q}". Try a shorter search or clear the filters.` : "Nothing matches these filters right now."}
        </EmptyState>
      ) : (
        <EmptyState title="No renewals tracked" action={write ? <Button asChild variant="outline"><Link to="/renewals/new">Add renewal</Link></Button> : undefined}>
          {write ? "Add one with a vendor, renewal date and notice period; anything missing shows as a gap."
            : "Editors and Admins can add renewals. Each one shows its next action deadline here."}
        </EmptyState>
      ))}
      {rows && rows.length > 0 && (
        <>
          <TableRegion label="Renewals table">
            <Table>
              <TableCaption className="sr-only">Renewals, by action deadline, no deadline last</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Renewal</TableHead><TableHead scope="col">Covers</TableHead>
                <TableHead scope="col" aria-sort="ascending">Action deadline</TableHead><TableHead scope="col">Auto-renewal</TableHead>
                <TableHead scope="col">Responsible</TableHead><TableHead scope="col" className="hidden lg:table-cell">IT follow-up</TableHead>
                <TableHead scope="col">Decision</TableHead><TableHead scope="col">Gaps</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((r) => (
                  <TableRow key={r.id} className="align-top">
                    <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                      <Link to={`/renewals/${r.id}`}>{r.name}</Link>
                      <span className="block font-normal text-muted-foreground">{r.vendor?.name ?? "No vendor"}</span>
                      {r.archived && <Chip className="mt-1">Archived</Chip>}
                    </TableHead>
                    <TableCell className="max-w-56 whitespace-normal [overflow-wrap:anywhere]">
                      {r.cycle.applications.length ? r.cycle.applications.map((a) => a.name).join(", ") : <Gap label="No applications" />}
                    </TableCell>
                    <TableCell className="whitespace-normal"><DeadlineCell deadline={r.deadline} closed={!r.cycle.open} /></TableCell>
                    <TableCell>{r.auto_renew === "unknown" ? <Gap label="Unknown" /> : AUTO_RENEW_LABEL[r.auto_renew]}</TableCell>
                    <TableCell className="whitespace-normal">{r.responsible?.name ?? <Gap label="None" />}</TableCell>
                    <TableCell className="hidden whitespace-normal lg:table-cell">{r.followup?.name ?? <Gap label="None" />}</TableCell>
                    <TableCell className="whitespace-normal">
                      {decisionText(r.cycle)}{r.cycle.decision && !r.cycle.open && <span className="block text-sm text-muted-foreground">Completed {r.cycle.completed_on}</span>}
                      {r.cycle.decision && r.cycle.open && <span className="block text-sm text-muted-foreground">Not completed</span>}
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      {r.gaps.length === 0 ? <span className="text-muted-foreground">None</span> : (
                        <>
                          <span className={chip({ state: "gap" })}><span aria-hidden="true">⚠</span>{r.gaps.length}<span className="sr-only"> {r.gaps.length === 1 ? "gap" : "gaps"}:</span></span>
                          <ul className="mt-1 text-sm text-muted-foreground">{r.gaps.map((g) => <li key={g}>{g}</li>)}</ul>
                        </>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
          <p className="mt-3 text-sm text-muted-foreground">Showing {count} {count === 1 ? "renewal" : "renewals"}. Earliest action deadline first; no deadline last.</p>
        </>
      )}
    </>
  );
}

// ---- add and edit -----------------------------------------------------------------------------------------------------

type Named = { id: string; name: string };
const UNITS: PeriodUnit[] = ["days", "months", "years"];
const empty = {
  name: "", vendor: "", responsible: "", followup: "", auto_renew: "unknown" as AutoRenew,
  renewal_date: "", notice_value: "", notice_unit: "months" as PeriodUnit, explicit_deadline: "", term_value: "", term_unit: "years" as PeriodUnit,
};
type Values = typeof empty;

/** The deadline the API would work out from what is typed so far, announced politely as it changes. */
function useDeadlinePreview(v: Values, enabled: boolean) {
  const [preview, setPreview] = useState<Deadline | null>(null);
  useEffect(() => {
    if (!enabled) return;
    const params = new URLSearchParams({ auto_renew: v.auto_renew });
    if (v.renewal_date) params.set("renewal_date", v.renewal_date);
    if (v.explicit_deadline) params.set("explicit_deadline", v.explicit_deadline);
    if (v.notice_value) { params.set("notice_value", v.notice_value); params.set("notice_unit", v.notice_unit); }
    let live = true;
    const t = setTimeout(() => {
      api<Deadline>(`renewals/preview?${params}`).then((d) => { if (live) setPreview(d); }).catch(() => { if (live) setPreview(null); });
    }, DEBOUNCE_MS);
    return () => { live = false; clearTimeout(t); };
  }, [enabled, v.auto_renew, v.renewal_date, v.explicit_deadline, v.notice_value, v.notice_unit]);
  return preview;
}

export function NewRenewal() {
  return <RenewalForm />;
}

export function EditRenewal() {
  const { id } = useParams();
  // The form starts from a read with the ETag, which the save sends back as If-Match.
  return (
    <OpenedEdit<Renewal> key={id} path={`renewals/${id}`} what="renewal" back={{ to: "/renewals", label: "Renewals" }}>
      {(existing, write) => <RenewalForm existing={existing} write={write} />}
    </OpenedEdit>
  );
}

function RenewalForm({ existing, write }: { existing?: Renewal; write?: OpenedWrite }) {
  const me = useMe();
  const navigate = useNavigate();
  const people = useLoad<Person[]>("people");
  const vendors = useLoad<Named[]>("vendors");
  const stewards = useLoad<UserRef[]>("stewards");
  const initial: Values = existing ? {
    ...empty, name: existing.name, vendor: existing.vendor?.id ?? "", responsible: existing.responsible?.active ? existing.responsible.id : "",
    followup: existing.followup ? String(existing.followup.id) : "", auto_renew: existing.auto_renew,
  } : empty;
  const [v, setV] = useState<Values>(initial);
  const [apps, setApps] = useState<CoveredApp[]>([]);
  const [addedVendors, setAddedVendors] = useState<Named[]>([]);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);
  const preview = useDeadlinePreview(v, !existing);
  const dirty = JSON.stringify(v) !== JSON.stringify(initial) || apps.length > 0;
  const guard = useUnsavedGuard(dirty && !saving);
  const set = (k: keyof Values) => (value: string) => setV((cur) => ({ ...cur, [k]: value }));
  const back = existing ? `/renewals/${existing.id}` : "/renewals";
  const vendorAdd = useVendorQuickAdd({
    focusId: "vendor",
    onAdded: (made) => { setAddedVendors((cur) => [...cur, made]); set("vendor")(made.id); setErrors(({ vendor: _drop, ...rest }) => rest); },
    onError: (text) => setErrors((cur) => ({ ...cur, vendor: [text] })),
  });

  if (!canWrite(me)) {
    return (
      <>
        <PageHead title="You can't change renewals" desc="Editors and Admins can add and change renewals. You can read them." />
        <Button asChild variant="outline"><Link to="/renewals">Back to Renewals</Link></Button>
      </>
    );
  }
  const loadError = people.error || vendors.error || stewards.error;
  if (loadError) return <ErrorBanner message={loadError} onRetry={() => { people.reload(); vendors.reload(); stewards.reload(); }} />;
  if (!people.data || !vendors.data || !stewards.data) return <Loading what="the form" />;

  const vendorOptions: ComboOption[] = [...vendors.data, ...addedVendors.filter((a) => !vendors.data!.some((b) => b.id === a.id))].map((x) => ({ id: x.id, label: x.name }));
  const peopleOptions: ComboOption[] = people.data.filter((p) => p.active).map((p) => ({ id: p.id, label: p.name, hint: p.department?.name }));
  const addVendor = (name: string) => vendorAdd.add(name.trim());

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const found: Record<string, string[]> = {};
    if (!v.name.trim()) found.name = ["Enter a name."];
    setErrors(found);
    setFormError("");
    if (Object.keys(found).length) return;
    setSaving(true);
    const obligation = {
      name: v.name.trim(), vendor: v.vendor || null, responsible: v.responsible || null,
      followup: v.followup ? Number(v.followup) : null, auto_renew: v.auto_renew,
    };
    const cycle = {
      renewal_date: v.renewal_date || null, explicit_deadline: v.explicit_deadline || null,
      ...(v.notice_value ? { notice_value: v.notice_value, notice_unit: v.notice_unit } : {}),
      ...(v.term_value ? { term_value: v.term_value, term_unit: v.term_unit } : {}),
      applications: apps.map((a) => a.id),
    };
    try {
      const saved = existing
        ? await api<Renewal>(`renewals/${existing.id}`, { method: "PATCH", body: obligation, headers: await write?.ifMatch() })
        : await api<Renewal>("renewals", { method: "POST", body: { ...obligation, ...cycle } });
      navigate(`/renewals/${saved.id}`, { state: savedState(saved.name, existing ? "Renewal saved." : `Renewal added. Action deadline: ${saved.deadline.date ?? "none yet"}${saved.deadline.label_text ? ` (${saved.deadline.label_text})` : ""}.`) });
    } catch (err) {
      setSaving(false);
      if (isStaleWrite(err) && write) write.onStale();
      else if (err instanceof Problem && Object.keys(err.errors).length) setErrors(err.errors);
      else setFormError(`${err instanceof Problem ? err.detail : "Something went wrong."} The renewal wasn't saved. Nothing you typed was lost.`);
    }
  }

  const deadlineLabel = v.auto_renew === "no" ? "Action deadline" : "Cancellation deadline";
  return (
    <>
      <Breadcrumb trail={[{ to: "/renewals", label: "Renewals" }, ...(existing ? [{ to: back, label: existing.name }] : [])]} current={existing ? "Edit" : "Add renewal"} />
      <PageHead title={existing ? `Edit ${existing.name}` : "Add renewal"} docTitle={existing ? "Edit renewal" : "Add renewal"}
        desc={existing ? "The renewal's own details. Change dates, notice period and covered applications on the current cycle." : "Record what renews and its first cycle. Anything you leave out shows as a gap, so you can come back to it."} />
      <Card className="max-w-3xl">
        <CardContent className="pt-6">
          <form onSubmit={submit} noValidate aria-label={existing ? "Edit renewal" : "Add renewal"}>
            <fieldset disabled={saving} className="m-0 min-w-0 border-0 p-0">
              {formError && <ErrorBanner message={formError} />}
              <ErrorSummary errors={errors} focusOnShow />
              <div className="grid gap-x-6 md:grid-cols-2">
                <div className="md:col-span-2">
                  <Field id="name" label="Name (required)" errors={errors} help="For example: Workday subscription.">
                    <Input id="name" value={v.name} onChange={(e) => set("name")(e.target.value)} autoComplete="off" />
                  </Field>
                </div>
                <Field id="vendor" label="Vendor" errors={errors}>
                  <Combobox id="vendor" options={vendorOptions} value={v.vendor} onChange={set("vendor")} placeholder="Search vendors…" clearLabel="Not set" listLabel="Vendor suggestions"
                    add={{ label: (typed) => `+ Add vendor "${typed}"`, onAdd: (typed) => void addVendor(typed) }} />
                </Field>
                <Field id="responsible" label="Responsible person" errors={errors} help="Who owns the decision. Never emailed; IT asks them through the usual channels.">
                  <Combobox id="responsible" options={peopleOptions} value={v.responsible} onChange={set("responsible")} placeholder="Search people…" clearLabel="Not set (gap)" listLabel="People suggestions" />
                </Field>
                <Field id="followup" label="IT follow-up" errors={errors} help="An Admin or Editor who gets the reminders. Without one, the IT coordinator does.">
                  <Select id="followup" value={v.followup} onChange={(e) => set("followup")(e.target.value)}>
                    <option value="">Nobody (gap)</option>
                    {stewards.data.map((u) => <option key={u.id} value={u.id}>{u.name}{u.id === me.user.id ? " (me)" : ""}</option>)}
                  </Select>
                </Field>
                <fieldset className="mb-3 flex min-w-0 flex-col gap-1.5" aria-describedby="auto_renew-help">
                  <legend className="mb-1.5 text-sm font-medium leading-none">Auto-renews</legend>
                  <div className="flex flex-wrap items-center gap-5">
                    {(["yes", "no", "unknown"] as const).map((x, i) => (
                      <label key={x} className="flex items-center gap-2 text-sm">
                        <input type="radio" id={i === 0 ? "auto_renew" : undefined} name="auto_renew" value={x} checked={v.auto_renew === x} onChange={() => set("auto_renew")(x)} className="h-4 w-4 accent-primary" />
                        {AUTO_RENEW_LABEL[x]}
                      </label>
                    ))}
                  </div>
                  <span id="auto_renew-help" className="text-sm text-muted-foreground">
                    {existing?.contract ? `Comes from the contract ${existing.contract.name} while it drives this renewal; change it there.` : "Unknown is treated as auto-renewing and shows as a gap."}
                  </span>
                  {errors.auto_renew && <span className="text-sm text-error-foreground">{errors.auto_renew.join(" ")}</span>}
                </fieldset>
              </div>
              {!existing && (
                <fieldset className="mt-2 border-t pt-4">
                  <legend className="mb-3 font-display text-section font-semibold">First cycle</legend>
                  <div className="grid gap-x-6 md:grid-cols-2">
                    <div className="md:col-span-2">
                      <Field id="applications" label="Covered applications" errors={errors}>
                        <CoverPicker id="applications" value={apps} onChange={setApps} />
                      </Field>
                    </div>
                    <Field id="renewal_date" label="Renewal date" errors={errors} help="When the current term ends or renews.">
                      <Input id="renewal_date" type="date" value={v.renewal_date} onChange={(e) => set("renewal_date")(e.target.value)} />
                    </Field>
                    <Field id="explicit_deadline" label={`${deadlineLabel} (optional)`} errors={errors} help="If the contract states it. It wins over any calculation.">
                      <Input id="explicit_deadline" type="date" value={v.explicit_deadline} onChange={(e) => set("explicit_deadline")(e.target.value)} />
                    </Field>
                    <PeriodField id="notice" label="Notice period" errors={errors} value={v.notice_value} unit={v.notice_unit}
                      onValue={set("notice_value")} onUnit={(u) => set("notice_unit")(u)} help="Months count calendar months: 2 months before March 31 is January 31." />
                    <PeriodField id="term" label="Term length" errors={errors} value={v.term_value} unit={v.term_unit}
                      onValue={set("term_value")} onUnit={(u) => set("term_unit")(u)} help="Used to suggest the next cycle's renewal date." />
                  </div>
                  <div className="mb-3 rounded-md border bg-muted/40 px-3 py-2 text-sm" role="status" aria-live="polite">
                    {preview ? (
                      <>
                        <strong>Action deadline: </strong>
                        {preview.date ? <>{preview.date} ({preview.label_text})</> : "none yet"}
                        <span className="block text-muted-foreground">{preview.explanation}</span>
                      </>
                    ) : "Working out the action deadline…"}
                  </div>
                </fieldset>
              )}
              <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
                <Button asChild variant="outline"><Link to={back}>Cancel</Link></Button>
                <Button type="submit">{saving ? "Saving…" : existing ? "Save renewal" : "Add renewal"}</Button>
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

/** A number with a unit. The number carries the field id so error summary links focus it. */
export function PeriodField({ id, label, errors, value, unit, onValue, onUnit, help }: {
  id: string; label: string; errors: Record<string, string[]>; value: string; unit: PeriodUnit;
  onValue: (v: string) => void; onUnit: (u: PeriodUnit) => void; help?: string;
}) {
  const err = errors[`${id}_value`] ?? errors[`${id}_unit`];
  return (
    <fieldset className="mb-3 flex min-w-0 flex-col gap-1.5" aria-describedby={[help && `${id}-help`, err && `${id}-error`].filter(Boolean).join(" ") || undefined}>
      <legend className="mb-1.5 text-sm font-medium leading-none">{label}</legend>
      <div className="flex gap-2">
        <Label htmlFor={`${id}_value`} className="sr-only">{label}, number</Label>
        <Input id={`${id}_value`} inputMode="numeric" className="w-24" value={value} onChange={(e) => onValue(e.target.value.replace(/[^0-9]/g, ""))} aria-invalid={err ? true : undefined} />
        <Label htmlFor={`${id}_unit`} className="sr-only">{label}, unit</Label>
        <Select id={`${id}_unit`} value={unit} onChange={(e) => onUnit(e.target.value as PeriodUnit)}>
          {UNITS.map((u) => <option key={u} value={u}>{u}</option>)}
        </Select>
      </div>
      {help && <span id={`${id}-help`} className="text-sm text-muted-foreground">{help}</span>}
      {err && <span id={`${id}-error`} className="text-sm text-error-foreground">{err.join(" ")}</span>}
    </fieldset>
  );
}
