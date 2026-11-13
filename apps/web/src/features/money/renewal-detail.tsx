import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, AUTO_RENEW_LABEL, CoveredApp, Decision, DECISION_LABEL, periodText, PeriodUnit, Problem, Renewal, RenewalCycle } from "@/api";
import { canWrite, useMe, useLandingNotice } from "@/lib/session";
import { ActivityTab } from "@/features/admin/audit";
import { attempt, Breadcrumb, Chip, EmptyState, ErrorBanner, ErrorSummary, Field, Gap, LazyPart, lazyPart, Loading, NotFound, PageHead, TabStrip, TableRegion, Textarea, useLoad, useReturnFocus } from "@/components/app";
import { ArchiveDialog, DeleteDialog } from "@/components/lifecycle-dialogs";
import { MenuAction, MoreMenu } from "@/components/more-menu";
import { DetailList, DetailRow, LifecycleChip, Panel, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { CoverPicker, DeadlineCell, decisionText, PeriodField } from "@/features/money/renewals";
import { moneyText } from "@/features/money/contract-types";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { ifMatch, staleDeleteText, useOpenedRecord } from "@/lib/if-match";

/** Ticket links (capability `ticket_links`), loaded on first use. */
const TicketLinks = lazyPart(() => import("@/features/tickets/ticket-links").then((m) => m.TicketLinks));

/** The renewal page: the current cycle's action deadline and how it
 *  was worked out, its terms and covered applications, then three numbered steps: decision, completion, next cycle.
 *  A decision never completes a cycle, and nothing renews by time passing. */

type Tab = "cycle" | "history" | "activity";

/** One sentence per gap, saying what to add. */
const GAP_TEXT: Record<string, string> = {
  "Assumed deadline": "Assumed deadline: renewal date minus the default lead time. Add the notice period or cancellation deadline to replace it.",
  "No renewal date": "No renewal date. The entered deadline still drives reminders; add the renewal date too.",
  "No deadline": "No action deadline can be worked out. Add a renewal date or the cancellation deadline. No reminders are sent until then.",
  "Auto-renewal unknown": "Auto-renewal is unknown, so it is treated as auto-renewing. Find out and record Yes or No.",
  "No applications": "No covered applications. Add the applications this renewal is for.",
  "All covered applications retired: confirm cancellation": "All covered applications are retired: confirm cancellation.",
  "No responsible person": "No responsible person. Choose who owns the decision.",
  "Inactive responsible person": "The responsible person is inactive. Choose someone else.",
  "No IT follow-up": "No IT follow-up. Reminders go to the IT coordinator until someone is assigned.",
  "Decision without completion": "Decision without completion: the deadline has passed and nothing records that the decision was carried out.",
  "Next cycle not established": "This cycle is complete. Establish the next cycle so the next deadline is tracked.",
};

export function RenewalDetail() {
  const { id } = useParams();
  const me = useMe();
  const write = canWrite(me);
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab");
  const tab: Tab = asked === "history" || asked === "activity" ? asked : "cycle";
  const load = useLoad<Renewal>(`renewals/${id}`);
  const [notice, setNotice, noticeQuiet] = useLandingNotice();
  const [dialog, setDialog] = useState<null | "terms" | "covers" | "next" | "archive" | "delete">(null);
  // The delete confirmation reads the renewal with its ETag as it opens; the delete sends it as If-Match.
  const deleteRead = useOpenedRecord(dialog === "delete" ? `renewals/${id}` : null);
  const [pending, setPending] = useState(false);
  const [refusal, setRefusal] = useState("");
  const [actionError, setActionError] = useState("");
  const r = load.data;
  if (load.status === 404) return <NotFound what="renewal" back={{ to: "/renewals", label: "Renewals" }} trail={[{ to: "/renewals", label: "Renewals" }]} />;
  if (load.error) {
    return (
      <>
        <Breadcrumb trail={[{ to: "/renewals", label: "Renewals" }]} current="Renewal" />
        <PageHead title="Couldn't load this renewal" />
        <ErrorBanner message={load.error} onRetry={load.reload} />
      </>
    );
  }
  if (!r) return <Loading what="renewal" rows={4} />;
  const c = r.cycle;
  const done = (message: string) => { setDialog(null); setActionError(""); setRefusal(""); setNotice(message); load.reload(); };
  async function lifecycle(path: string, method: string, message: string, after?: () => void) {
    setPending(true);
    setRefusal("");
    const failed = await attempt(async () => api(path, { method, headers: method === "DELETE" ? ifMatch(await deleteRead.etag()) : undefined }));
    setPending(false);
    if (failed?.status === 412) return setRefusal(staleDeleteText("renewal"));
    if (failed) return setRefusal(failed.detail);
    if (after) return after();
    done(message);
  }
  const actions: MenuAction[] = write ? [
    r.archived ? { label: "Restore", onSelect: () => void lifecycle(`renewals/${id}/restore`, "POST", "Restored.") }
      : { label: "Archive", onSelect: () => { setRefusal(""); setDialog("archive"); } },
    ...(me.role === "admin" ? [{ label: "Delete", danger: true, onSelect: () => { setRefusal(""); setDialog("delete"); } }] : []),
  ] : [];
  const tabs = [
    { id: "cycle" as const, label: `Current cycle (#${c.sequence})` },
    { id: "history" as const, label: `Cycle history (${r.history?.length ?? 0})` },
    { id: "activity" as const, label: "Audit activity" },
  ];
  const facts = [
    `Vendor ${r.vendor?.name ?? "not set"}`, `Auto-renewal ${AUTO_RENEW_LABEL[r.auto_renew]}`,
    `Responsible ${r.responsible?.name ?? "not set"}`, `IT follow-up ${r.followup?.name ?? "not set"}`,
  ];
  return (
    <>
      <Breadcrumb trail={[{ to: "/renewals", label: "Renewals" }]} current={r.name} />
      <PageHead title={r.name} desc={facts.join(" · ")}>
        {write && <Button asChild variant="outline"><Link to={`/renewals/${id}/edit`}>Edit</Link></Button>}
        <MoreMenu actions={actions} />
      </PageHead>
      <SuccessNote text={notice} quiet={noticeQuiet} />
      {actionError && <ErrorBanner message={actionError} />}
      {refusal && !dialog && <ErrorBanner message={refusal} />}
      {r.archived && <p className="mb-4 rounded-md border bg-muted px-3 py-2">Archived. It's left out of Needs attention and the digest, and its history is kept. {write && "Restore it from More."}</p>}
      {r.snooze && (
        <p className="mb-4 rounded-md border bg-card px-3 py-2" role="note">
          {r.snooze.kind === "snooze" ? `Your notifications are snoozed until ${r.snooze.until}.` : "You acknowledged this until its next reminder."} The deadline is unchanged.
        </p>
      )}
      {r.gaps.length > 0 && (
        <section aria-label="Gaps" className="mb-4 rounded-md border-[1.5px] border-dashed border-gap-border bg-gap px-4 py-3 text-gap-foreground">
          <p className="font-semibold"><span aria-hidden="true">⚠ </span>{r.gaps.length === 1 ? "1 gap" : `${r.gaps.length} gaps`}</p>
          <ul className="ml-5 list-disc">{r.gaps.map((g) => <li key={g}>{GAP_TEXT[g] ?? g}</li>)}</ul>
        </section>
      )}
      {r.contract && <ContractNote contract={r.contract} />}
      <div className="mb-5"><LazyPart what="the ticket links"><TicketLinks target={{ kind: "renewal", id: r.id, name: r.name, title: `Renewal: ${r.name}`, cycles: [r.cycle, ...(r.history ?? [])] }} /></LazyPart></div>
      <TabStrip tabs={tabs} tab={tab} label="Renewal sections" onSelect={(t) => setParams(t === "cycle" ? {} : { tab: t })} />
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "cycle" && (
          <CurrentCycle r={r} write={write && !r.archived} onEdit={setDialog}
            onSaved={done} onError={setActionError} />
        )}
        {tab === "history" && <History cycles={r.history ?? []} />}
        {tab === "activity" && <ActivityTab base="renewals" id={r.id} />}
      </div>
      <TermsDialog r={r} open={dialog === "terms"} onClose={() => setDialog(null)} onDone={() => done("Cycle terms saved.")} />
      <CoversDialog r={r} open={dialog === "covers"} onClose={() => setDialog(null)} onDone={() => done("Covered applications saved.")} />
      <NextCycleDialog r={r} open={dialog === "next"} onClose={() => setDialog(null)} onDone={(n) => done(`Cycle ${n.sequence} established. The completed cycle is in Cycle history, unchanged.`)} />
      <ArchiveDialog name={r.name} open={dialog === "archive"} onOpenChange={(o) => !o && setDialog(null)} pending={pending} refusal={refusal || undefined}
        onConfirm={() => lifecycle(`renewals/${id}/archive`, "POST", "Archived.")} />
      <DeleteDialog name={r.name} open={dialog === "delete"} onOpenChange={(o) => !o && setDialog(null)} pending={pending} refusal={refusal || undefined}
        onConfirm={() => lifecycle(`renewals/${id}`, "DELETE", "", () => navigate("/renewals"))} onArchiveInstead={() => { setRefusal(""); setDialog("archive"); }} />
    </>
  );
}

/** "one source of truth": while a contract drives the renewal, its open cycle takes the contract's end date (as
 *  the renewal date), notice period and auto-renewal, and the renewal refuses to change them (a 400 on those fields). */
export const CONTRACT_OWNED = "The renewal date, notice period, cancellation deadline and auto-renewal";
function ContractNote({ contract }: { contract: { id: string; name: string } }) {
  return (
    <div className="mb-4 flex flex-wrap items-start gap-2 rounded-md border border-l-4 border-info-border bg-info-bg px-4 py-3 text-info-foreground" role="note" data-testid="renewal-contract-note">
      <span aria-hidden="true">ⓘ</span>
      <p className="min-w-0 flex-1">
        Dates come from the contract <Link to={`/contracts/${contract.id}`} className="font-semibold underline">{contract.name}</Link>.{" "}
        {CONTRACT_OWNED} follow that contract, so its deadline and these reminders never disagree. Change them on the contract; edits to them here are refused.
      </p>
    </div>
  );
}

function Reminders({ c }: { c: RenewalCycle }) {
  if (!c.deadline.date) return <p className="text-sm text-muted-foreground">No reminders until there is an action deadline.</p>;
  const word = (x: RenewalCycle["reminders"][number]) =>
    // Judged from the API's own day count (organization time), never the browser's clock.
    x.status === "not_sent" ? (c.deadline.timing && c.deadline.timing.days < x.days ? "passed, not sent" : `due ${x.on}`)
      : x.status === "sent" ? `sent ${x.sent_on}` : x.status === "failed" ? `failed ${x.sent_on}` : x.status === "skipped" ? `not sent (email off) ${x.sent_on}` : `sending ${x.sent_on}`;
  return (
    <p className="text-sm">
      <span className="font-medium">Reminders: </span>
      {c.reminders.map((x, i) => <span key={x.days}>{i > 0 && " · "}{x.days} days, {word(x)}</span>)}
    </p>
  );
}

function CurrentCycle({ r, write, onEdit, onSaved, onError }: {
  r: Renewal; write: boolean; onEdit: (d: "terms" | "covers" | "next") => void; onSaved: (m: string) => void; onError: (m: string) => void;
}) {
  const c = r.cycle;
  const open = c.open && write;
  const overdueFrom = c.deadline.date ? new Date(new Date(`${c.deadline.date}T00:00:00Z`).getTime() + 86400000).toISOString().slice(0, 10) : null;
  return (
    <div className="grid gap-5">
      <Panel id="h-deadline" icon={<span aria-hidden="true">◷</span>} title="Action deadline">
        <div className="grid gap-2 px-4 py-3">
          <DeadlineCell deadline={c.deadline} closed={!c.open} />
          <p>{c.deadline.explanation}{c.deadline.date && c.open && ` It's the last on-time date; overdue from ${overdueFrom}.`}</p>
          {c.open && <Reminders c={c} />}
        </div>
      </Panel>
      <div className="grid gap-5 lg:grid-cols-2">
        <Panel id="h-terms" icon={<span aria-hidden="true">≡</span>} title="Cycle terms"
          action={open ? <Button size="sm" variant="outline" onClick={() => onEdit("terms")}>Edit terms</Button> : undefined}>
          {r.contract && c.open && <p className="border-b border-border-subtle px-4 py-2 text-sm text-muted-foreground">From the contract {r.contract.name}, except the term length.</p>}
          <DetailList>
            <DetailRow label="Renewal date">{c.renewal_date ?? <Gap label="Not set" />}</DetailRow>
            <DetailRow label="Notice period">{periodText(c.notice)}</DetailRow>
            <DetailRow label={r.auto_renew === "no" ? "Action deadline (optional)" : "Cancellation deadline (optional)"}>{c.explicit_deadline ?? "—"}</DetailRow>
            <DetailRow label="Term length">{periodText(c.term)}</DetailRow>
          </DetailList>
        </Panel>
        <Panel id="h-covers" icon={<span aria-hidden="true">▣</span>} title="Covers (this cycle)" count={c.applications.length}
          action={open ? <Button size="sm" variant="outline" onClick={() => onEdit("covers")}>Edit covered applications</Button> : undefined}>
          {c.applications.length === 0 ? <p className="px-4 py-3"><Gap label="No applications" /></p> : (
            <ul className="grid gap-1 px-4 py-3">
              {c.applications.map((a) => (
                <li key={a.id}><Link to={`/applications/${a.id}`} className="inline-block min-h-6 leading-6">{a.name}</Link>
                  {a.lifecycle === "retired" && <LifecycleChip value="retired" className="ml-2" />}{a.archived && <Chip className="ml-2">Archived</Chip>}</li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
      <DecisionStep r={r} write={open} onSaved={onSaved} onError={onError} />
      <CompletionStep r={r} write={open} onSaved={onSaved} />
      <NextStep r={r} write={write} onNext={() => onEdit("next")} />
    </div>
  );
}

const fieldsetClass = "rounded-lg border bg-card px-4 pb-4 pt-2";
const legendClass = "px-1 font-display text-section font-semibold";

function DecisionStep({ r, write, onSaved, onError }: { r: Renewal; write: boolean; onSaved: (m: string) => void; onError: (m: string) => void }) {
  const c = r.cycle;
  const [decision, setDecision] = useState<Decision | "">(c.decision ?? "");
  const [decidedOn, setDecidedOn] = useState(c.decided_on ?? "");
  const [notes, setNotes] = useState(c.decision_notes);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!decision) return setErrors({ decision: ["Choose Renew, Cancel or Renegotiate."] });
    setSaving(true);
    const failed = await attempt(() => api(`renewals/${r.id}/cycles/${c.id}/decision`, { method: "POST", body: { decision, decision_notes: notes, ...(decidedOn ? { decided_on: decidedOn } : {}) } }));
    setSaving(false);
    if (failed) { setErrors(failed.errors); if (!Object.keys(failed.errors).length) onError(failed.detail); return; }
    setErrors({});
    onSaved(`Decision saved: ${DECISION_LABEL[decision]}. The cycle isn't complete until you record what was done.`);
  }
  return (
    <form onSubmit={save} noValidate aria-label="Decision">
      <fieldset className={fieldsetClass} disabled={!write || saving}>
        <legend className={legendClass}>1. Decision</legend>
        <ErrorSummary errors={errors} />
        <fieldset className="mb-3" aria-describedby={errors.decision ? "decision-error" : undefined}>
          <legend className="mb-1.5 text-sm font-medium">What was decided</legend>
          <div className="flex flex-wrap gap-5">
            {(["renew", "cancel", "renegotiate"] as const).map((d, i) => (
              <label key={d} className="flex items-center gap-2 text-sm">
                <input type="radio" id={i === 0 ? "decision" : undefined} name="decision" value={d} checked={decision === d} onChange={() => setDecision(d)} className="h-4 w-4 accent-primary" />
                {DECISION_LABEL[d]}
              </label>
            ))}
          </div>
          {errors.decision && <span id="decision-error" className="text-sm text-error-foreground">{errors.decision.join(" ")}</span>}
        </fieldset>
        <div className="grid gap-x-6 md:grid-cols-[200px_1fr]">
          <Field id="decided_on" label="Decided on" errors={errors} help="Defaults to today.">
            <Input id="decided_on" type="date" value={decidedOn} onChange={(e) => setDecidedOn(e.target.value)} />
          </Field>
          <Field id="decision_notes" label="Notes" errors={errors}>
            <Textarea id="decision_notes" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
          </Field>
        </div>
        <p className="mb-3 text-sm text-muted-foreground">A decision isn't completion. Choosing Cancel doesn't mean notice was sent.</p>
        {!c.open && <p className="text-sm">Decided {c.decided_on ? `on ${c.decided_on}` : ""}: {decisionText(c)}.</p>}
        {write && <div className="flex justify-end"><Button type="submit">{saving ? "Saving…" : "Save decision"}</Button></div>}
      </fieldset>
    </form>
  );
}

function CompletionStep({ r, write, onSaved }: { r: Renewal; write: boolean; onSaved: (m: string) => void }) {
  const c = r.cycle;
  const [completedOn, setCompletedOn] = useState(c.completed_on ?? "");
  const [evidence, setEvidence] = useState(c.completion_evidence);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    const found: Record<string, string[]> = {};
    if (!completedOn) found.completed_on = ["Enter the completion date."];
    if (!evidence.trim()) found.completion_evidence = ["Enter evidence or a reference, such as a PO number or an email."];
    if (Object.keys(found).length) return setErrors(found);
    setSaving(true);
    const failed = await attempt(() => api(`renewals/${r.id}/cycles/${c.id}/completion`, { method: "POST", body: { completed_on: completedOn, completion_evidence: evidence.trim() } }));
    setSaving(false);
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { completed_on: [failed.detail] });
    setErrors({});
    onSaved("Completion recorded. Establish the next cycle when you're ready.");
  }
  const blocked = c.open && !c.decision;
  return (
    <form onSubmit={save} noValidate aria-label="Completion">
      <fieldset className={fieldsetClass} disabled={!write || saving || blocked}>
        <legend className={legendClass}>2. Completion</legend>
        <ErrorSummary errors={errors} />
        {blocked && <p className="mb-2 text-sm">Record the decision first.</p>}
        <div className="grid gap-x-6 md:grid-cols-[200px_1fr]">
          <Field id="completed_on" label="Completed on (required)" errors={errors}>
            <Input id="completed_on" type="date" value={completedOn} onChange={(e) => setCompletedOn(e.target.value)} />
          </Field>
          <Field id="completion_evidence" label="Evidence or reference (required)" errors={errors} help="For example: PO 44120 signed, or the email confirming cancellation.">
            <Input id="completion_evidence" value={evidence} onChange={(e) => setEvidence(e.target.value)} autoComplete="off" />
          </Field>
        </div>
        <p className="mb-3 text-sm text-muted-foreground">Record what actually happened. Time passing never marks a cycle renewed.</p>
        {write && !blocked && <div className="flex justify-end"><Button type="submit" variant="outline">{saving ? "Recording…" : "Record completion"}</Button></div>}
      </fieldset>
    </form>
  );
}

function NextStep({ r, write, onNext }: { r: Renewal; write: boolean; onNext: () => void }) {
  const c = r.cycle;
  return (
    <section className={fieldsetClass} aria-labelledby="h-next">
      <h2 id="h-next" className={`${legendClass} pt-1`}>3. Next cycle</h2>
      {c.open ? (
        <p className="mt-1 text-sm text-muted-foreground">
          After completion, establish the next cycle{c.next_renewal_date ? `: its renewal date will start as ${c.next_renewal_date} (${c.renewal_date} plus the ${periodText(c.term)} term)` : ""},
          with the same notice period and covered applications. This cycle then moves to history unchanged.
        </p>
      ) : (
        <div className="mt-1 flex flex-wrap items-center gap-3">
          <p className="text-sm">This cycle was completed on {c.completed_on}. Nothing is tracked until the next cycle exists.</p>
          {write && <Button onClick={onNext}>Establish next cycle</Button>}
        </div>
      )}
    </section>
  );
}

/** What a contract renew decision changed, in words: "12,000.00 USD a year to 13,200.00 USD; ends 2027-06-30 to 2028-06-30". */
export function termsChangeText(t: RenewalCycle["terms_change"]): string | null {
  if (!t) return null;
  const money = (a?: string | null, c?: string | null) => moneyText(a, c ?? "")?.trim() ?? "no amount";
  const parts = [];
  if (t.old_annualized_cost !== t.new_annualized_cost || t.old_currency !== t.new_currency) {
    parts.push(`${money(t.old_annualized_cost, t.old_currency)} a year to ${t.new_annualized_cost || t.new_currency ? money(t.new_annualized_cost, t.new_currency) : "nothing (canceled)"}`);
  } else if (t.old_annualized_cost) parts.push(`${money(t.old_annualized_cost, t.old_currency)} a year, unchanged`);
  if (t.old_end_date || t.new_end_date) parts.push(`ends ${t.old_end_date ?? "not set"}${t.new_end_date ? ` to ${t.new_end_date}` : ""}`);
  return parts.join("; ") || null;
}

function History({ cycles }: { cycles: RenewalCycle[] }) {
  if (cycles.length === 0) return <EmptyState title="No earlier cycles">Completed cycles move here when the next one is established, with their dates, decision and evidence unchanged.</EmptyState>;
  return (
    <TableRegion label="Cycle history table">
      <Table>
        <TableCaption className="sr-only">Earlier cycles, newest first</TableCaption>
        <TableHeader><TableRow>
          <TableHead scope="col">#</TableHead><TableHead scope="col">Renewal date</TableHead><TableHead scope="col">Action deadline</TableHead>
          <TableHead scope="col">Covered</TableHead><TableHead scope="col">Decision</TableHead><TableHead scope="col">Completed on</TableHead><TableHead scope="col">Evidence</TableHead>
          <TableHead scope="col">Terms change</TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {cycles.map((h) => (
            <TableRow key={h.id} className="align-top">
              <TableHead scope="row" className="tabular">{h.sequence}</TableHead>
              <TableCell className="tabular">{h.renewal_date ?? "—"}</TableCell>
              <TableCell>{h.deadline.date ? `${h.deadline.date} ${h.deadline.label_text}` : "—"}</TableCell>
              <TableCell className="whitespace-normal">{h.applications.map((a) => a.name).join(", ") || "—"}</TableCell>
              <TableCell>{decisionText(h)}</TableCell>
              <TableCell className="tabular">{h.completed_on ?? "—"}</TableCell>
              <TableCell className="max-w-xs whitespace-normal [overflow-wrap:anywhere]">{h.completion_evidence || "—"}</TableCell>
              <TableCell className="whitespace-normal">{termsChangeText(h.terms_change) ?? "—"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

// ---- dialogs ------------------------------------------------------------------------------------------------------------

function DialogShell({ open, onClose, title, description, children }: { open: boolean; onClose: () => void; title: string; description: string; children: React.ReactNode }) {
  const returnFocus = useReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-xl" {...returnFocus}>
        <DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>{description}</DialogDescription></DialogHeader>
        {open && children}
      </DialogContent>
    </Dialog>
  );
}

type Terms = { renewal_date: string; explicit_deadline: string; notice_value: string; notice_unit: PeriodUnit; term_value: string; term_unit: PeriodUnit };
const termsOf = (c: { renewal_date: string | null; explicit_deadline: string | null; notice: RenewalCycle["notice"]; term: RenewalCycle["term"] }): Terms => ({
  renewal_date: c.renewal_date ?? "", explicit_deadline: c.explicit_deadline ?? "",
  notice_value: c.notice ? String(c.notice.value) : "", notice_unit: c.notice?.unit ?? "months",
  term_value: c.term ? String(c.term.value) : "", term_unit: c.term?.unit ?? "years",
});
const termsBody = (t: Terms) => ({
  renewal_date: t.renewal_date || null, explicit_deadline: t.explicit_deadline || null,
  notice_value: t.notice_value || null, notice_unit: t.notice_value ? t.notice_unit : "",
  term_value: t.term_value || null, term_unit: t.term_value ? t.term_unit : "",
});

function TermsFields({ t, set, errors, autoRenew }: { t: Terms; set: (t: Terms) => void; errors: Record<string, string[]>; autoRenew: string }) {
  return (
    <div className="grid gap-x-6 sm:grid-cols-2">
      <Field id="renewal_date" label="Renewal date" errors={errors}>
        <Input id="renewal_date" type="date" value={t.renewal_date} onChange={(e) => set({ ...t, renewal_date: e.target.value })} />
      </Field>
      <Field id="explicit_deadline" label={autoRenew === "no" ? "Action deadline (optional)" : "Cancellation deadline (optional)"} errors={errors}>
        <Input id="explicit_deadline" type="date" value={t.explicit_deadline} onChange={(e) => set({ ...t, explicit_deadline: e.target.value })} />
      </Field>
      <PeriodField id="notice" label="Notice period" errors={errors} value={t.notice_value} unit={t.notice_unit}
        onValue={(v) => set({ ...t, notice_value: v })} onUnit={(u) => set({ ...t, notice_unit: u })} />
      <PeriodField id="term" label="Term length" errors={errors} value={t.term_value} unit={t.term_unit}
        onValue={(v) => set({ ...t, term_value: v })} onUnit={(u) => set({ ...t, term_unit: u })} />
    </div>
  );
}

/** The cycle edits read the renewal with its ETag as the dialog opens and start from that copy; the save sends the ETag
 *  as If-Match, so a change someone else saved meanwhile shows "Load the latest version". */
function TermsDialog({ r, open, onClose, onDone }: { r: Renewal; open: boolean; onClose: () => void; onDone: () => void }) {
  return (
    <DialogShell open={open} onClose={onClose} title={`Edit cycle ${r.cycle.sequence} terms`} description="Changing a date or the notice period works out the action deadline again. Earlier cycles don't change.">
      <OpenedEdit<Renewal> path={`renewals/${r.id}`} what="renewal">
        {(fresh, write) => <TermsForm r={fresh} write={write} onClose={onClose} onDone={onDone} />}
      </OpenedEdit>
    </DialogShell>
  );
}

function TermsForm({ r, write, onClose, onDone }: { r: Renewal; write: OpenedWrite; onClose: () => void; onDone: () => void }) {
  const [t, setT] = useState<Terms>(termsOf(r.cycle));
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    const failed = await attempt(async () => api(`renewals/${r.id}/cycles/${r.cycle.id}`, { method: "PATCH", body: termsBody(t), headers: await write.ifMatch() }));
    setSaving(false);
    if (failed?.status === 412) { setErrors({}); return write.onStale(); }
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { renewal_date: [failed.detail] });
    onDone();
  }
  return (
    <form onSubmit={save} noValidate>
      {r.contract && (
        <p className="mb-3 rounded-md border border-l-4 border-info-border bg-info-bg px-3 py-2 text-sm text-info-foreground" data-testid="terms-contract-note">
          <span aria-hidden="true">ⓘ </span>The renewal date, notice period and cancellation deadline come from the contract {r.contract.name}. Only the term length can change here; change the others on <Link to={`/contracts/${r.contract.id}/edit`} className="underline">the contract</Link>.
        </p>
      )}
      <ErrorSummary errors={errors} />
      <TermsFields t={t} set={setT} errors={errors} autoRenew={r.auto_renew} />
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : "Save terms"}</Button>
      </DialogFooter>
    </form>
  );
}

function CoversDialog({ r, open, onClose, onDone }: { r: Renewal; open: boolean; onClose: () => void; onDone: () => void }) {
  return (
    <DialogShell open={open} onClose={onClose} title="Edit covered applications" description="For this cycle only. Earlier cycles keep the applications they covered.">
      <OpenedEdit<Renewal> path={`renewals/${r.id}`} what="renewal">
        {(fresh, write) => <CoversForm r={fresh} write={write} onClose={onClose} onDone={onDone} />}
      </OpenedEdit>
    </DialogShell>
  );
}

function CoversForm({ r, write, onClose, onDone }: { r: Renewal; write: OpenedWrite; onClose: () => void; onDone: () => void }) {
  const [apps, setApps] = useState<CoveredApp[]>(r.cycle.applications);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    const failed = await attempt(async () => api(`renewals/${r.id}/cycles/${r.cycle.id}`, { method: "PATCH", body: { applications: apps.map((a) => a.id) }, headers: await write.ifMatch() }));
    setSaving(false);
    if (failed?.status === 412) { setErrors({}); return write.onStale(); }
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { applications: [failed.detail] });
    onDone();
  }
  return (
    <form onSubmit={save} noValidate>
      <ErrorSummary errors={errors} />
      <Field id="applications" label="Covered applications" errors={errors}>
        <CoverPicker id="applications" value={apps} onChange={setApps} />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : "Save applications"}</Button>
      </DialogFooter>
    </form>
  );
}

function NextCycleDialog({ r, open, onClose, onDone }: { r: Renewal; open: boolean; onClose: () => void; onDone: (cycle: RenewalCycle) => void }) {
  const c = r.cycle;
  const [t, setT] = useState<Terms>({ ...termsOf(c), renewal_date: c.next_renewal_date ?? "", explicit_deadline: "" });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState(false);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      const made = await api<Renewal>(`renewals/${r.id}/cycles`, { method: "POST", body: termsBody(t) });
      onDone(made.cycle);
    } catch (err) {
      setErrors(err instanceof Problem && Object.keys(err.errors).length ? err.errors : { renewal_date: [err instanceof Problem ? err.detail : "Something went wrong."] });
    } finally {
      setSaving(false);
    }
  }
  return (
    <DialogShell open={open} onClose={onClose} title={`Establish cycle ${c.sequence + 1}`}
      description={c.next_renewal_date ? `The renewal date starts as ${c.next_renewal_date}: ${c.renewal_date} plus the ${periodText(c.term)} term. The covered applications are copied; change them afterwards if needed.` : "Enter the next renewal date. The notice period, term and covered applications are copied from this cycle."}>
      <form onSubmit={save} noValidate>
        <ErrorSummary errors={errors} />
        <TermsFields t={t} set={setT} errors={errors} autoRenew={r.auto_renew} />
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={saving}>{saving ? "Establishing…" : "Establish next cycle"}</Button>
        </DialogFooter>
      </form>
    </DialogShell>
  );
}
