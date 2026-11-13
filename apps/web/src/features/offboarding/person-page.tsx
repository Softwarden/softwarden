/** The person page (`/people/:id`, every role reads; loaded on first use): where the person appears
 *  (GET people/{id}/footprint), every section in the API's order with its count and links, and the reassign flow for
 *  Editors and Admins (POST people/{id}/reassign: a dry run first, always, then apply). Licences are the named gap the
 *  API gives ("Licenses: not read; needs User.Read.All"); the account and notification-rule sections are Admin-only,
 *  so other roles don't see them at all. Export CSV is hidden while platform staff impersonate (the API refuses it). */
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Problem, type Person } from "@/api";
import { Breadcrumb, ErrorBanner, ErrorSummary, isNotFound, Loading, NotFound, PageHead, TableRegion, useReturnFocus } from "@/components/app";
import { Combobox } from "@/components/combobox";
import { chip, GapChip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ExportCsvButton } from "@/components/url-filters";
import { typedApi, useTypedQuery, type Schemas } from "@/lib/openapi";
import { useApiQuery, useInvalidate } from "@/lib/query";
import { canWrite, useMe } from "@/lib/session";
import type { Me } from "@/api";

type Footprint = Schemas["Footprint"];
type Section = Schemas["FootprintSection"];
type Result = Schemas["ReassignResult"];
type ReassignRow = Schemas["ReassignRow"];
type Role = Schemas["ReassignRoleEnum"];

const ADMIN_SECTIONS = new Set(["account", "notification_rules"]);
const words = (s: string) => { const t = s.replace(/_/g, " "); return t.charAt(0).toUpperCase() + t.slice(1); };
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** The roles the API reassigns, its words for each, and what an organisation or role needs to offer it. */
const ROLES: { id: Role; label: string; help: string; capability?: string; admin?: boolean }[] = [
  { id: "business_owner", label: "Business owner", help: "Of applications, components and AI agents" },
  { id: "technical_owner", label: "Technical owner", help: "Of applications, components, AI agents and integrations" },
  { id: "it_steward", label: "IT steward", help: "Needs the new person's account to be an active Admin or Editor" },
  { id: "vendor_account_manager", label: "Vendor account manager", help: "Account manager of vendors" },
  { id: "renewal_responsible", label: "Renewal responsible", help: "Responsible for renewals", capability: "renewals" },
  { id: "renewal_followup", label: "Renewal follow-up", help: "Needs the new person's account to be an active Admin or Editor", capability: "renewals" },
  { id: "contract_renewal_owner", label: "Contract renewal owner", help: "Renewal owner of contracts", capability: "contracts" },
  { id: "quality_exception_owner", label: "Data-quality exception owner", help: "Accountable for data-quality exceptions", capability: "data_quality" },
  { id: "notification_rules", label: "Notification rules", help: "Audience, escalation and fallback (Admins only)", admin: true },
];
const roleLabel = (r: string) => ROLES.find((x) => x.id === r)?.label ?? words(r);
const rolesFor = (me: Me) => ROLES.filter((r) => (!r.capability || me.capabilities.includes(r.capability)) && (!r.admin || me.role === "admin"));

export function PersonPage() {
  const { id = "" } = useParams();
  const me = useMe();
  const q = useTypedQuery("people/{id}/footprint", { path: { id } });
  const [notice, setNotice] = useState("");
  const [reassigning, setReassigning] = useState(false);
  const fp = q.data;
  const trail = [{ to: "/admin/people", label: "People & Teams" }];
  if (q.isError && isNotFound(q.error)) return <NotFound what="person" back={{ to: "/admin/people", label: "People & Teams" }} trail={trail} />;
  if (q.isError) {
    return (
      <>
        <Breadcrumb trail={trail} current="Person" />
        <PageHead title="Person" />
        <ErrorBanner message={q.error instanceof Problem ? q.error.detail : "Couldn't load this person's footprint."} onRetry={() => void q.refetch()} />
      </>
    );
  }
  if (!fp) return <><Breadcrumb trail={trail} current="Person" /><Loading what="this person's footprint" rows={4} /></>;
  const p = fp.person;
  const sections = fp.sections.filter((s) => me.role === "admin" || !ADMIN_SECTIONS.has(s.key));
  return (
    <>
      <Breadcrumb trail={trail} current={p.name} />
      <PageHead title={p.name} desc={<>
        {p.email || "No email"} · {p.anonymized ? "Anonymized (inactive)" : p.active ? "Active" : "Inactive"}
        {p.account && <> · Account {p.account.email}, {p.account.active ? (p.account.role ? `${words(p.account.role)} here` : "no longer a member here") : "deactivated"}</>}
      </>}>
        <ExportCsvButton path={`people/${p.id}/footprint.csv`} fallback="softwarden-footprint.csv" what={`${p.name}'s footprint`} onDone={setNotice} />
        {canWrite(me) && <Button onClick={() => setReassigning(true)}>Reassign roles…</Button>}
      </PageHead>
      <SuccessNote text={notice} />
      {!canWrite(me) && <p className="mb-3 text-sm text-muted-foreground" data-testid="read-only">You can read where {p.name} appears. Editors and Admins reassign their roles.</p>}
      <h2 className="mb-1 font-display text-section font-semibold">Footprint</h2>
      <p className="mb-4 text-muted-foreground" data-testid="footprint-total">
        {plural(fp.total, "item")} across the sections Softwarden can read. Archived items are included and marked. Team memberships are listed but never moved.
      </p>
      <div className="grid grid-cols-1 gap-3">
        {sections.map((s) => <FootprintSection key={s.key} s={s} />)}
      </div>
      {reassigning && (
        <ReassignDialog footprint={fp} onClose={() => setReassigning(false)} onApplied={(text) => { setReassigning(false); setNotice(text); void q.refetch(); }} />
      )}
    </>
  );
}

function FootprintSection({ s }: { s: Section }) {
  const id = `fp-${s.key}`;
  return (
    <section aria-labelledby={id} className="rounded-lg border bg-card px-4 py-3" data-section={s.key}>
      <h3 id={id} className="font-semibold">{s.label}{s.available && <span className="font-normal text-muted-foreground"> ({s.count})</span>}</h3>
      {!s.available ? (
        <div className="mt-1.5">
          {s.key === "licences" ? <GapChip label={s.reason} /> : (
            <span className="inline-flex max-w-full items-center gap-1.5 rounded-sm border border-dashed border-border-strong bg-muted px-2 text-sm"><span aria-hidden="true">⊘</span>{s.reason}</span>
          )}
        </div>
      ) : s.items.length === 0 ? <p className="mt-1 text-sm text-muted-foreground">None.</p> : (
        <ul className="mt-1.5 grid gap-1.5">
          {s.items.map((i, n) => (
            <li key={`${i.type}-${i.id ?? n}`} className="flex flex-wrap items-center gap-1.5">
              {i.path ? <Link to={i.path} className="underline [overflow-wrap:anywhere]">{i.name}</Link> : <span className="[overflow-wrap:anywhere]">{i.name}</span>}
              <span className="text-sm text-muted-foreground">({words(i.type)})</span>
              {i.roles.map((r) => <span key={r} className={chip({ state: "neutral" })}>{roleLabel(r)}</span>)}
              {i.archived && <span className={chip({ state: "retired" })}><span aria-hidden="true">▣</span>Archived</span>}
              {i.detail && <span className="basis-full text-sm text-muted-foreground">{i.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ---- reassign ---------------------------------------------------------------------------------------------------------

const ROW_STATUS: Record<ReassignRow["status"], { glyph: string; text: string; state: "pending" | "confirmed" | "neutral" | "overdue" }> = {
  planned: { glyph: "◐", text: "Will change", state: "pending" },
  changed: { glyph: "✓", text: "Changed", state: "confirmed" },
  skipped: { glyph: "⊘", text: "Skipped", state: "neutral" },
  refused: { glyph: "!", text: "Refused", state: "overdue" },
};

function ReassignRows({ rows, caption }: { rows: ReassignRow[]; caption: string }) {
  if (rows.length === 0) return <p className="text-muted-foreground">Nothing holds these roles, so nothing would change.</p>;
  return (
    <TableRegion label={`${caption} table`}>
      <Table>
        <TableCaption className="sr-only">{caption}</TableCaption>
        <TableHeader><TableRow><TableHead scope="col">Item</TableHead><TableHead scope="col">Roles</TableHead><TableHead scope="col">Outcome</TableHead></TableRow></TableHeader>
        <TableBody>
          {rows.map((r) => {
            const s = ROW_STATUS[r.status];
            return (
              <TableRow key={`${r.type}-${r.id}`} className="align-top" data-row-status={r.status}>
                <TableHead scope="row" className="max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                  {r.path ? <Link to={r.path} className="underline">{r.name}</Link> : r.name}
                  <span className="block text-sm font-normal text-muted-foreground">{words(r.type)}</span>
                </TableHead>
                <TableCell className="whitespace-normal">{r.roles.map(roleLabel).join(", ")}</TableCell>
                <TableCell className="min-w-48 whitespace-normal">
                  <span className={chip({ state: s.state })}><span aria-hidden="true">{s.glyph}</span>{s.text}</span>
                  {r.reason && <span className="mt-1 block text-sm">{r.reason}</span>}
                  {(r.errors ?? []).map((e) => <span key={e} className="mt-1 block text-sm text-error-foreground">{e}</span>)}
                  {Object.entries(r.field_errors ?? {}).map(([k, v]) => <span key={k} className="mt-1 block text-sm text-error-foreground">{words(k)}: {v.join(" ")}</span>)}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

/** What a dry run was sent with: Apply sends exactly this, never the form's current values. */
type Sent = { to: string; roles: Role[] };
type Step =
  | { at: "choose" }
  | { at: "preview"; plan: Result; sent: Sent }
  | { at: "stale"; sent: Sent }
  | { at: "refused"; rows: ReassignRow[]; detail: string }
  | { at: "done"; result: Result };
const REASSIGN_STALE = "/problems/reassign-stale";

function ReassignDialog({ footprint, onClose, onApplied }: { footprint: Footprint; onClose: () => void; onApplied: (text: string) => void }) {
  const me = useMe();
  const person = footprint.person;
  const people = useApiQuery<Person[]>("people?active=true");
  const invalidate = useInvalidate();
  const offered = rolesFor(me);
  const [to, setTo] = useState("");
  const [roles, setRoles] = useState<Role[]>(offered.filter((r) => !r.admin).map((r) => r.id));
  const [step, setStep] = useState<Step>({ at: "choose" });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);
  const focus = useReturnFocus(true);
  const options = (people.data ?? []).filter((x) => x.id !== person.id).map((x) => ({ id: x.id, label: x.name, hint: [x.department?.name, x.email].filter(Boolean).join(" · ") }));

  async function preview(sent: Sent) {
    setBusy(true); setErrors({}); setDetail("");
    try {
      const plan = await typedApi("post", "people/{id}/reassign", { path: { id: person.id }, body: { ...sent, dry_run: true } });
      setStep({ at: "preview", plan, sent });
    } catch (e) {
      if (e instanceof Problem && Object.keys(e.errors).length) { setErrors(e.errors); setStep({ at: "choose" }); }
      else setDetail(e instanceof Problem ? e.detail : "Something went wrong. Nothing was saved. Try again.");
    } finally {
      setBusy(false);
    }
  }
  async function choose(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    const local: Record<string, string[]> = {};
    if (!to) local.to = ["Choose the person who takes over."];
    if (!roles.length) local.roles = ["Choose at least one role."];
    if (Object.keys(local).length) { setErrors(local); return; }
    await preview({ to, roles: [...roles] });
  }
  /** The real run, with exactly what the preview was made of and its plan_hash: a 409 says the rows changed since. */
  async function apply(plan: Result, sent: Sent) {
    if (busy) return;
    setBusy(true); setDetail("");
    try {
      const result = await typedApi("post", "people/{id}/reassign", { path: { id: person.id }, body: { ...sent, dry_run: false, plan_hash: plan.plan_hash } });
      setStep({ at: "done", result });
      void invalidate("people", "reports");
    } catch (e) {
      if (e instanceof Problem && e.type === REASSIGN_STALE) setStep({ at: "stale", sent });
      else if (e instanceof Problem && Array.isArray(e.body.rows)) setStep({ at: "refused", rows: e.body.rows as ReassignRow[], detail: e.detail });
      else setDetail(e instanceof Problem ? (Object.values(e.errors).flat().join(" ") || e.detail) : "Something went wrong. Nothing was saved. Try again.");
    } finally {
      setBusy(false);
    }
  }
  const toggle = (r: Role) => setRoles((cur) => (cur.includes(r) ? cur.filter((x) => x !== r) : [...cur, r]));
  const close = () => {
    if (busy) return; // a run in flight finishes first, so its outcome is always shown
    if (step.at === "done") onApplied(`Reassigned ${plural(step.result.changed, "item")} from ${person.name} to ${step.result.to.name}.`);
    else onClose();
  };

  return (
    <Dialog open onOpenChange={(o) => !o && close()}>
      <DialogContent className="max-w-3xl" {...focus}
        onEscapeKeyDown={(e) => { if (busy) e.preventDefault(); }} onInteractOutside={(e) => { if (busy) e.preventDefault(); }}>
        <DialogHeader>
          <DialogTitle>Reassign {person.name}'s roles</DialogTitle>
          <DialogDescription>
            {step.at === "choose" && "Choose who takes over and which roles. You'll see a preview first; nothing is saved until you apply it."}
            {step.at === "preview" && "Preview: what applying would change. Nothing has been saved yet."}
            {(step.at === "refused" || step.at === "stale") && "Nothing was saved."}
            {step.at === "done" && "Applied. Each change is in the audit log, marked as offboarding."}
          </DialogDescription>
        </DialogHeader>
        {detail && <p role="alert" className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2">{detail}</p>}
        {step.at === "choose" && (
          <form noValidate onSubmit={(e) => void choose(e)} className="grid gap-3">
            <ErrorSummary errors={errors} />
            <fieldset disabled={busy} className="grid gap-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="to">Hand over to (required)</Label>
                {people.isError ? <ErrorBanner message="Couldn't load the people to choose from." onRetry={() => void people.refetch()} />
                  : !people.data ? <p className="text-muted-foreground" aria-live="polite">Loading people…</p> : (
                    <Combobox id="to" options={options} value={to} onChange={setTo} placeholder="Search active people…" listLabel="People who can take over" disabled={busy}
                      aria-invalid={errors.to ? true : undefined} aria-describedby={errors.to ? "to-error" : "to-help"} />
                  )}
                <span id="to-help" className="text-sm text-muted-foreground">Only active people can take over.</span>
                {errors.to && <span id="to-error" className="text-sm text-error-foreground">{errors.to.join(" ")}</span>}
              </div>
              <fieldset aria-describedby={errors.roles ? "roles-error" : undefined}>
                <legend className="mb-1.5 font-semibold">Roles to hand over</legend>
                {offered.map((r, i) => (
                  <label key={r.id} className="flex min-h-8 items-start gap-2 py-0.5 text-sm">
                    <input id={i === 0 ? "roles" : undefined} type="checkbox" className="mt-1 h-4 w-4 accent-primary" checked={roles.includes(r.id)} onChange={() => toggle(r.id)} />
                    <span><span className="font-semibold">{r.label}</span><span className="block text-muted-foreground">{r.help}</span></span>
                  </label>
                ))}
                {errors.roles && <span id="roles-error" className="text-sm text-error-foreground">{errors.roles.join(" ")}</span>}
              </fieldset>
            </fieldset>
            {people.data && options.length === 0 && <p className="text-muted-foreground">No one else is active. Add another person on People & Teams first.</p>}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={onClose} disabled={busy}>Cancel</Button>
              <Button type="submit" disabled={busy}>{busy ? "Checking…" : "Preview changes"}</Button>
            </DialogFooter>
          </form>
        )}
        {step.at === "preview" && (
          <div className="grid gap-3">
            <p role="status" data-testid="reassign-summary">
              {plural(step.plan.changed, "item")} would change to {step.plan.to.name}, {step.plan.skipped} skipped, {step.plan.refused} refused.
            </p>
            <ReassignRows rows={step.plan.rows} caption={`Preview of handing ${person.name}'s roles to ${step.plan.to.name}`} />
            <DialogFooter className="flex-wrap items-center">
              {step.plan.refused > 0 && <p id="apply-why" className="mr-auto text-sm text-error-foreground">Apply needs every row to pass. Fix the refused rows, or untick their roles, and preview again.</p>}
              {step.plan.refused === 0 && step.plan.changed === 0 && <p id="apply-why" className="mr-auto text-sm text-muted-foreground">Nothing would change, so there is nothing to apply.</p>}
              <Button variant="outline" disabled={busy} onClick={() => setStep({ at: "choose" })}>Back</Button>
              <Button disabled={busy || step.plan.refused > 0 || step.plan.changed === 0} aria-describedby={step.plan.refused > 0 || step.plan.changed === 0 ? "apply-why" : undefined}
                onClick={() => void apply(step.plan, step.sent)}>{busy ? "Applying…" : `Apply ${plural(step.plan.changed, "change")}`}</Button>
            </DialogFooter>
          </div>
        )}
        {step.at === "stale" && (
          <div className="grid gap-3">
            <p role="alert" className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" data-testid="reassign-stale">The records changed since the preview. Preview again.</p>
            <DialogFooter>
              <Button variant="outline" disabled={busy} onClick={onClose}>Close</Button>
              <Button disabled={busy} onClick={() => void preview(step.sent)}>{busy ? "Checking…" : "Preview again"}</Button>
            </DialogFooter>
          </div>
        )}
        {step.at === "refused" && (
          <div className="grid gap-3">
            <p role="alert" className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2">{step.detail} Nothing was saved.</p>
            <ReassignRows rows={step.rows} caption="Rows that were refused" />
            <DialogFooter>
              <Button variant="outline" onClick={onClose}>Close</Button>
              <Button onClick={() => setStep({ at: "choose" })}>Back to the choices</Button>
            </DialogFooter>
          </div>
        )}
        {step.at === "done" && (
          <div className="grid gap-3">
            <p role="status" className="text-success-foreground" data-testid="reassign-done"><span aria-hidden="true">✓ </span>
              Reassigned {plural(step.result.changed, "item")} from {person.name} to {step.result.to.name}{step.result.skipped ? `; ${step.result.skipped} skipped` : ""}.
            </p>
            <ReassignRows rows={step.result.rows} caption={`Handed ${person.name}'s roles to ${step.result.to.name}`} />
            <DialogFooter><Button onClick={close}>Done</Button></DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
