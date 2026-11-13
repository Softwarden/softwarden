import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ASSESSMENT_STATUSES, ASSESSMENT_STATUS_LABEL, AssessmentRow, AssessmentStatus, ChangeType, LinkKind, Problem, UserRef } from "@/api";
import { canWrite, useMe } from "@/lib/session";
import { Breadcrumb, Chip, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, PageHead, Select, TableRegion, Textarea, useLoad } from "@/components/app";
import { PickedRecord, RecordPicker } from "@/components/record-picker";
import { chip, LifecycleChip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useUnsavedGuard } from "@/features/catalog/record-form";
import { RecordLink } from "@/components/record-link";

/** Change assessments: the list and the New assessment form. The detail page is in
 *  assessment-detail.tsx. An assessment is documentation, not approval; status is one of the assessment status values and "Stale" is
 *  a separate flag next to it, never a status. */

export const StatusChip = ({ status }: { status: AssessmentStatus }) => <Chip>{ASSESSMENT_STATUS_LABEL[status]}</Chip>;
/** The amber "Stale snapshot" flag beside the status chip: a glyph and text, not a colour alone. */
export const StaleFlag = ({ since }: { since?: string | null }) => (
  <span className={chip({ state: "pending" })}><span aria-hidden="true">↻</span>Stale snapshot{since && <span className="sr-only"> since {since.slice(0, 10)}</span>}</span>
);
export const typeText = (t: ChangeType) => (t === "retirement" ? "Retirement" : "Change");

// ---- List ---------------------------------------------------------------------------------------------------------

const OPEN = ["draft", "in_review", "ready"];
type StatusFilter = "open" | "all" | AssessmentStatus;
const FILTERS: { id: StatusFilter; label: string }[] = [
  { id: "open", label: "Open (Draft, In review, Ready)" }, { id: "all", label: "All statuses" },
  ...ASSESSMENT_STATUSES.map((s) => ({ id: s, label: ASSESSMENT_STATUS_LABEL[s] })),
];
const DEBOUNCE_MS = 300;

export function AssessmentsList() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const asked = params.get("status");
  const status: StatusFilter = FILTERS.some((f) => f.id === asked) ? (asked as StatusFilter) : "open";
  const staleOnly = params.get("stale") === "1";
  const q = (params.get("q") ?? "").trim();
  const [text, setText] = useState(params.get("q") ?? "");
  useEffect(() => { setText(params.get("q") ?? ""); }, [params]);
  const paramsRef = useRef(params);
  paramsRef.current = params;
  function update(changes: Record<string, string>, push = !("q" in changes)) {
    const next = new URLSearchParams(paramsRef.current);
    for (const [k, v] of Object.entries(changes)) {
      if (!v || (k === "status" && v === "open")) next.delete(k); else next.set(k, v);
    }
    setParams(next, { replace: !push });
  }
  useEffect(() => {
    if (text.trim() === q) return;
    const t = setTimeout(() => update({ q: text.trim() }), DEBOUNCE_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  const query = new URLSearchParams({
    ...(status === "open" ? { status: OPEN.join(",") } : status === "all" ? {} : { status }),
    ...(staleOnly ? { stale: "true" } : {}), ...(q ? { q } : {}),
  }).toString();
  const load = useLoad<AssessmentRow[]>(`assessments${query ? `?${query}` : ""}`);
  const rows = load.data;
  const filtered = status !== "open" || staleOnly || q !== "";
  const clear = () => { setText(""); update({ status: "", stale: "", q: "" }, true); };
  const write = canWrite(me);
  const count = rows?.length ?? 0;
  const statusLabel = FILTERS.find((f) => f.id === status)!.label;
  return (
    <>
      <PageHead title="Change assessments" desc="Save what a planned change or retirement could affect, with a snapshot and findings. An assessment is documentation, not approval.">
        {write && <Button asChild><Link to="/assessments/new">New assessment</Link></Button>}
      </PageHead>
      <div className="mb-4 flex flex-wrap items-end gap-x-6 gap-y-3">
        <form role="search" className="flex w-full max-w-sm flex-col gap-1.5" onSubmit={(e) => { e.preventDefault(); update({ q: text.trim() }); }}>
          <Label htmlFor="list-search">Search assessments</Label>
          <Input id="list-search" type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder="Title or target name" />
        </form>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="status-filter">Status</Label>
          <Select id="status-filter" value={status} onChange={(e) => update({ status: e.target.value })}>
            {FILTERS.map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}
          </Select>
        </div>
        <label className="flex h-9 items-center gap-2">
          <input type="checkbox" checked={staleOnly} onChange={(e) => update({ stale: e.target.checked ? "1" : "" })} />
          Stale snapshots only
        </label>
        {filtered && <Button variant="outline" size="sm" onClick={clear}>Clear filters</Button>}
      </div>
      <p className="sr-only" aria-live="polite">{rows ? `Showing ${count} ${count === 1 ? "assessment" : "assessments"}.` : ""}</p>
      {load.error && <ErrorBanner message={`Couldn't load assessments. ${load.error}`} onRetry={load.reload} />}
      {!rows && !load.error && <Loading what="assessments" rows={5} />}
      {rows && rows.length === 0 && (filtered ? (
        <EmptyState title="No assessments match these filters" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>
          {q ? `Nothing matches "${q}" with Status: ${statusLabel}${staleOnly ? " and stale snapshots only" : ""}. Try a shorter search or clear the filters.`
            : `No assessments are ${staleOnly ? "stale " : ""}with Status: ${statusLabel} right now.`}
        </EmptyState>
      ) : (
        <EmptyState title="No open assessments" action={write ? <Button asChild variant="outline"><Link to="/assessments/new">New assessment</Link></Button> : undefined}>
          {write ? "Nothing is Draft, In review or Ready. Start one before a change or a retirement: choose the application or component and Softwarden saves what depends on it. Closed and canceled assessments are under Status."
            : "Nothing is Draft, In review or Ready. Editors and Admins can start an assessment; closed and canceled ones are under Status."}
        </EmptyState>
      ))}
      {rows && rows.length > 0 && (
        <>
          <TableRegion label="Change assessments table">
            <Table>
              <TableCaption className="sr-only">Change assessments, most recent activity first</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Title</TableHead><TableHead scope="col">Target</TableHead><TableHead scope="col">Type</TableHead>
                <TableHead scope="col">Status</TableHead><TableHead scope="col">Owner</TableHead><TableHead scope="col">Target date</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((a) => (
                  <TableRow key={a.id} className="h-[52px] align-top">
                    <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]"><Link to={`/assessments/${a.id}`}>{a.title}</Link></TableHead>
                    <TableCell className="whitespace-normal [overflow-wrap:anywhere]">
                      <RecordLink record={a.target} />
                      {a.target.archived && <Chip className="ml-2 align-middle">Archived</Chip>}
                      {a.target.lifecycle === "retired" && <LifecycleChip value="retired" className="ml-2 align-middle" />}
                    </TableCell>
                    <TableCell>{typeText(a.change_type)}</TableCell>
                    <TableCell className="whitespace-normal">
                      <span className="inline-flex flex-wrap items-center gap-1.5"><StatusChip status={a.status} />{a.stale && <StaleFlag since={a.snapshot?.stale_since} />}</span>
                    </TableCell>
                    <TableCell>{a.owner?.name ?? "No owner"}</TableCell>
                    <TableCell className="tabular">{a.target_date ?? "—"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
          <p className="mt-3 text-sm text-muted-foreground">Showing {count} {count === 1 ? "assessment" : "assessments"}. Most recent activity first.</p>
        </>
      )}
    </>
  );
}

// ---- New assessment ---------------------------------------------------------------------------------------------------

/** The prefilled target arrives as an ID (from the Dependencies screen); an application or a component, so try both. */
function usePrefill(id: string) {
  const [record, setRecord] = useState<PickedRecord | null>(null);
  useEffect(() => {
    if (!id) return;
    let live = true;
    const found = (r: { id: string; name: string; lifecycle: string }, kind: LinkKind) => { if (live) setRecord({ id: r.id, name: r.name, kind, lifecycle: r.lifecycle }); };
    api<{ id: string; name: string; lifecycle: string }>(`applications/${id}`).then((r) => found(r, "application"))
      .catch(() => api<{ id: string; name: string; lifecycle: string }>(`components/${id}`).then((r) => found(r, "component")).catch(() => undefined));
    return () => { live = false; };
  }, [id]);
  return record;
}

export function NewAssessment() {
  const me = useMe();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const prefill = usePrefill(params.get("target") ?? "");
  const stewards = useLoad<UserRef[]>("stewards");
  const [target, setTarget] = useState<PickedRecord | null>(null);
  const [initialType] = useState<ChangeType | "">(params.get("type") === "retirement" ? "retirement" : params.get("type") === "change" ? "change" : "");
  const [type, setType] = useState<ChangeType | "">(initialType);
  const [form, setForm] = useState({ title: "", owner: String(me.user.id), target_date: "", ticket_url: "", description: "" });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (prefill) setTarget((t) => t ?? prefill); }, [prefill]);
  const dirty = (target?.id ?? "") !== (prefill?.id ?? "") || type !== initialType || form.title !== "" || form.ticket_url !== "" || form.description !== "" || form.target_date !== "";
  const guard = useUnsavedGuard(dirty && !saving);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => setForm({ ...form, [k]: e.target.value });

  if (!canWrite(me)) {
    return (
      <>
        <PageHead title="You can't create assessments" desc="Editors and Admins can create and change assessments. You can read them." />
        <Button asChild variant="outline"><Link to="/assessments">Back to Change assessments</Link></Button>
      </>
    );
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const found: Record<string, string[]> = {};
    if (!target) found.target = ["Choose an application or component."];
    if (!type) found.change_type = ["Choose change or retirement."];
    if (!form.title.trim()) found.title = ["Enter a title."];
    setErrors(found);
    setFormError("");
    if (Object.keys(found).length) return;
    setSaving(true);
    try {
      const body = {
        target: target!.id, change_type: type, title: form.title.trim(), owner: form.owner || null,
        ...(form.target_date ? { target_date: form.target_date } : {}), ...(form.ticket_url.trim() ? { ticket_url: form.ticket_url.trim() } : {}),
        ...(form.description.trim() ? { description: form.description.trim() } : {}),
      };
      const made = await api<AssessmentRow>("assessments", { method: "POST", body });
      const c = made.snapshot?.counts;
      navigate(`/assessments/${made.id}`, { state: { notice: `Assessment created. Affected set saved${c ? `: ${c.dependents} ${c.dependents === 1 ? "dependent" : "dependents"}, ${c.data_flows} ${c.data_flows === 1 ? "data flow" : "data flows"}` : ""}.` } });
    } catch (err) {
      setSaving(false);
      if (err instanceof Problem && Object.keys(err.errors).length) setErrors(err.errors);
      else setFormError(`${err instanceof Problem ? err.detail : "Something went wrong."} The assessment wasn't saved. Nothing you typed was lost.`);
    }
  }

  const placeholder = target ? `${type === "retirement" ? "Retire" : "Change to"} ${target.name}` : "For example: Retire Legacy Timeclock";
  return (
    <>
      <Breadcrumb trail={[{ to: "/assessments", label: "Change assessments" }]} current="New assessment" />
      <PageHead title="New assessment" desc="Choose what is changing and the kind of change. Softwarden calculates the affected set and saves it as the first snapshot." />
      <Card className="max-w-3xl">
        <CardContent className="pt-6">
          <form onSubmit={submit} noValidate aria-label="New assessment">
            <fieldset disabled={saving} className="m-0 min-w-0 border-0 p-0">
              {formError && <ErrorBanner message={formError} />}
              <ErrorSummary errors={errors} focusOnShow />
              <div className="grid gap-x-6 md:grid-cols-2">
                <div className="md:col-span-2">
                  <Field id="target" label="Target (required)" errors={errors} help="An application or component. Integrations are included through the records they connect.">
                    <RecordPicker id="target" kinds={["application", "component", "ai_agent"]} value={target} onChange={setTarget} placeholder="Search by name…" listLabel="Target suggestions" />
                  </Field>
                </div>
                <fieldset className="mb-3 flex min-w-0 flex-col gap-1.5 md:col-span-2" aria-describedby={errors.change_type ? "change_type-error" : undefined}>
                  <legend className="mb-1.5 text-sm font-medium leading-none">Type (required)</legend>
                  <div className="flex flex-wrap items-center gap-5">
                    {([["change", "Change"], ["retirement", "Retirement"]] as const).map(([v, label], i) => (
                      <label key={v} className="flex items-center gap-2 text-sm">
                        <input type="radio" id={i === 0 ? "change_type" : undefined} name="change_type" value={v} checked={type === v} onChange={() => setType(v)} className="h-4 w-4 accent-primary" />
                        {label}
                      </label>
                    ))}
                  </div>
                  {errors.change_type && <span className="text-sm text-error-foreground" id="change_type-error">{errors.change_type.join(" ")}</span>}
                </fieldset>
                <div className="md:col-span-2">
                  <Field id="title" label="Title (required)" errors={errors}>
                    <Input id="title" value={form.title} onChange={set("title")} autoComplete="off" placeholder={placeholder} />
                  </Field>
                </div>
                <Field id="owner" label="Owner" errors={errors} help="Admins and Editors only. Defaults to you.">
                  <Select id="owner" value={form.owner} onChange={set("owner")}>
                    <option value="">No owner</option>
                    {(stewards.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.name}{u.id === me.user.id ? " (me)" : ""}</option>)}
                  </Select>
                </Field>
                <Field id="target_date" label="Target date" errors={errors} help="When the change is planned. Optional.">
                  <Input id="target_date" type="date" value={form.target_date} onChange={set("target_date")} />
                </Field>
                <div className="md:col-span-2">
                  <Field id="ticket_url" label="External ticket URL" errors={errors} help="The change request in your ticket system. Softwarden doesn't track the ticket.">
                    <Input id="ticket_url" type="url" inputMode="url" placeholder="https://…" value={form.ticket_url} onChange={set("ticket_url")} />
                  </Field>
                </div>
                <div className="md:col-span-2">
                  <Field id="description" label="Description" errors={errors}>
                    <Textarea id="description" value={form.description} onChange={set("description")} />
                  </Field>
                </div>
              </div>
              <p className="mb-2 text-sm text-muted-foreground" role="status">{saving ? "Calculating affected set…" : ""}</p>
              <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
                <Button asChild variant="outline"><Link to="/assessments" aria-disabled={saving || undefined}>Cancel</Link></Button>
                <Button type="submit">{saving ? "Calculating affected set…" : "Create assessment"}</Button>
              </div>
            </fieldset>
          </form>
          {guard}
        </CardContent>
      </Card>
    </>
  );
}
