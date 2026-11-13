import { cloneElement, useEffect, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { api, Attention, AttentionQueue, AttentionScope, cap, CatalogRecord, DECISION_LABEL, LinkRow, MissingQueueRow, Problem, SEVERITY_LABEL, Snooze, StaleAssessmentRow } from "@/api";
import { canWrite, useMe } from "@/lib/session";
import { andList } from "@/lib/utils";
import { useInvalidate } from "@/lib/query";
import { EmptyState, ErrorBanner, Gap, LazyPart, lazyPart, Loading, PageHead, StateChip, useLoad } from "@/components/app";
import { Button } from "@/components/ui/button";
import { chip, CriticalityMark, GapChips, LinkStateChip, ScopeToggle, StewardCell, Tally } from "@/components/sw";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { OwnersDialog, ReviewDialog } from "@/features/catalog/detail";
import { QuietMenu, ReminderActions, ReminderStatusChip, SnoozeMark } from "@/features/notify/notify";
import { DeadlineCell } from "@/features/money/renewals";
import { apiPath, editPath, isRecordKind, KIND_LABEL, pathOf, recordPath } from "@/lib/kinds";
import { RecordLink } from "@/components/record-link";
import { phraseOf } from "@/lib/relationship-types";
import type { MissingTarget } from "@/features/discovery/reconcile";
import { EolStatusChip } from "@/features/eol/eol-ui";
import { CLASSIFICATION_LABEL, RiskChips } from "@/features/flows/flow-ui";

// Keep or archive a record a source no longer lists, loaded on first use.
const MissingDialog = lazyPart(() => import("@/features/discovery/reconcile").then((m) => m.MissingDialog));

// ---- Needs attention ----------------------------------------------------------

const IconOwners = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4"><circle cx="6" cy="5.5" r="2.5" /><path d="M1.5 14c.6-2.6 2.3-4 4.5-4s3.9 1.4 4.5 4" /><path d="M11.5 4.5v4M9.5 6.5h4" strokeLinecap="round" /></svg>;
const IconReview = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="2.5" y="2" width="11" height="12" rx="1.5" /><path d="m5.5 8.5 1.8 1.8 3.4-3.6" strokeLinecap="round" strokeLinejoin="round" /></svg>;
const IconLink = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><path d="M6.5 9.5a2.5 2.5 0 0 0 3.5 0l2.5-2.5a2.5 2.5 0 0 0-3.5-3.5L8 4.5" /><path d="M9.5 6.5a2.5 2.5 0 0 0-3.5 0L3.5 9a2.5 2.5 0 0 0 3.5 3.5L8 11.5" /></svg>;
const IconStale = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><path d="M13 8a5 5 0 1 1-1.5-3.5" /><path d="M13 2.5v3h-3" strokeLinejoin="round" /></svg>;
const IconDiscovery = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><circle cx="7" cy="7" r="4.5" /><path d="m10.5 10.5 3.5 3.5" /><path d="M5 7h4M7 5v4" /></svg>;
const IconAgent = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="5" width="10" height="8" rx="2" /><path d="M8 5V2.5M6 9h.01M10 9h.01" /></svg>;
const IconQuality = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M3 13V8M8 13V3M13 13v-7" /><path d="M1.5 13.5h13" /></svg>;
const IconRenewal = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><rect x="2" y="3" width="12" height="11" rx="1.5" /><path d="M2 6.5h12M5.5 1.5v3M10.5 1.5v3" /><path d="M8 8.5v2l1.3 1" /></svg>;
const IconSeats = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3.5" width="12" height="9" rx="1.5" /><path d="M5 6.5h6M5 9.5h3.5" /></svg>;
const IconGone = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><circle cx="7" cy="7" r="4.5" strokeDasharray="2 2" /><path d="m10.5 10.5 3.5 3.5" /></svg>;
const IconVendorRisk = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round"><path d="M8 1.8 13 3.6v3.7c0 3-2 5.4-5 6.3-3-.9-5-3.3-5-6.3V3.6z" /><path d="M8 5.5v3M8 10.5h.01" strokeLinecap="round" /></svg>;
const IconEol = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="12" height="11" rx="1.5" /><path d="M2 6.5h12M5.5 1.5v3M10.5 1.5v3M6.5 9l3 3M9.5 9l-3 3" /></svg>;
const IconFlows = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><circle cx="3.5" cy="8" r="1.8" /><circle cx="12.5" cy="4" r="1.8" /><circle cx="12.5" cy="12" r="1.8" /><path d="M5.2 7.3 10.8 4.7M5.2 8.7l5.6 2.6" strokeDasharray="2 1.5" /></svg>;
const IconBell = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M4 11V7a4 4 0 0 1 8 0v4l1.5 1.5h-11z" /><path d="M6.5 14a1.6 1.6 0 0 0 3 0" /></svg>;
const SCOPES = ["mine", "all", "unassigned"] as const;

/** A queue: the heading and count always show. An empty queue is one line; a populated
 *  one is a disclosure, open by default, whose state the heading's button announces. */
function QueueSection({ id, icon, title, count, note, empty, open, onToggle, children, compact = false }: {
  id: string; icon: React.ReactNode; title: string; count: number; note: string; empty: string; open: boolean; onToggle: () => void;
  children: React.ReactNode;
  /** An all-clear queue listed inside the shared Nothing waiting card: its heading and sentence on one row. */
  compact?: boolean;
}) {
  const body = `${id}-body`;
  if (compact) {
    return (
      <section className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 border-t border-border-subtle px-4 py-2" aria-labelledby={id}>
        <h3 id={id} tabIndex={-1} className="text-sm font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring">{title}<span className="sr-only"> (0)</span></h3>
        <span className="text-sm text-state-confirmed-foreground">✓ {empty}</span>
      </section>
    );
  }
  return (
    <section className="mb-6 overflow-hidden rounded-lg border bg-card" aria-labelledby={id}>
      <div className={`flex min-h-[53px] flex-wrap items-center gap-2.5 px-4 py-2.5 ${count > 0 && open ? "border-b" : ""}`}>
        <span className="grid h-7 w-7 place-items-center rounded-md bg-accent text-accent-foreground" aria-hidden="true">{icon}</span>
        <h2 id={id} tabIndex={-1} className="font-display text-section font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring">
          {count === 0 ? <>{title}<span className="sr-only"> (0)</span></> : (
            <button type="button" aria-expanded={open} aria-controls={body} onClick={onToggle} className="inline-flex min-h-6 items-center gap-1.5 text-left">
              <span aria-hidden="true" className="w-3 text-muted-foreground">{open ? "▾" : "▸"}</span>{title}<span className="sr-only"> ({count})</span>
            </button>
          )}
        </h2>
        <span aria-hidden="true"><Tally n={count} /></span>
        {count === 0
          ? <span className="text-sm text-state-confirmed-foreground">✓ {empty}</span>
          : <span className="ml-auto text-xs text-muted-foreground">{note}</span>}
      </div>
      {/* The queue's table scrolls sideways here, in a labelled focusable region, when the page is narrow. */}
      {count > 0 && <div id={body} hidden={!open} className="relative overflow-x-auto" role="region" aria-label={`Queue table: ${title}`} tabIndex={0}>{children}</div>}
    </section>
  );
}

/** The queues with items, in their usual order, then the all-clear ones together: one line naming them,
 *  and a disclosure that lists each with its sentence. Before, fourteen all-clear cards came before the first queue with
 *  work in it. */
function Queues({ queues, clearOpen, onClearToggle }: { queues: React.ReactElement<React.ComponentProps<typeof QueueSection>>[]; clearOpen: boolean; onClearToggle: () => void }) {
  const busy = queues.filter((q) => q.props.count > 0);
  const clear = queues.filter((q) => q.props.count === 0);
  return (
    <>
      {busy.map((q) => cloneElement(q, { key: q.props.id }))}
      {clear.length > 0 && (
        <section className="mb-6 rounded-lg border bg-card" aria-labelledby="h-all-clear" data-testid="all-clear">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5">
            <h2 id="h-all-clear" className="font-display text-section font-semibold">
              <button type="button" aria-expanded={clearOpen} aria-controls="all-clear-body" onClick={onClearToggle} className="inline-flex min-h-6 items-center gap-1.5 text-left">
                <span aria-hidden="true" className="w-3 text-muted-foreground">{clearOpen ? "▾" : "▸"}</span>Nothing waiting ({clear.length})
              </button>
            </h2>
            <span className="text-sm text-state-confirmed-foreground"><span aria-hidden="true">✓ </span>Nothing in {andList(clear.map((q) => q.props.title))}.</span>
          </div>
          <div id="all-clear-body" hidden={!clearOpen}>
            {clear.map((q) => cloneElement(q, { key: q.props.id, compact: true }))}
          </div>
        </section>
      )}
    </>
  );
}

/** What an empty queue may claim depends on the scope: only All speaks for the whole catalog. */
const EMPTY: Record<AttentionQueue, Record<AttentionScope, string>> = {
  ownership_gaps: {
    all: "No ownership gaps. Every active record has owners and a support team.",
    mine: "No ownership gaps on records assigned to you.",
    unassigned: "No ownership gaps on records without an IT steward.",
  },
  agent_gaps: {
    all: "No AI agent gaps. Every live agent has an owner, a data access scope and a human-in-loop decision.",
    mine: "No AI agent gaps on agents assigned to you.",
    unassigned: "No AI agent gaps on agents without an IT steward.",
  },
  reviews: {
    all: "No reviews due. Every active record has a current review.",
    mine: "No reviews due for records assigned to you.",
    unassigned: "No reviews due for records without an IT steward.",
  },
  links_to_verify: {
    all: "No links need verification. Links never confirmed are listed on each record's Relationships tab.",
    mine: "No links to verify are assigned to you.",
    unassigned: "No links to verify without an IT steward.",
  },
  stale_assessments: {
    all: "No stale assessments. Every open assessment still matches the catalog.",
    mine: "None of your open assessments is stale.",
    unassigned: "No stale assessments without an owner.",
  },
  renewal_actions: {
    all: "No renewal actions are due. Nothing is overdue or within the reminder window.",
    mine: "No renewal actions are due for renewals you follow up.",
    unassigned: "No renewal actions are due for renewals without an IT follow-up.",
  },
  renewal_gaps: {
    all: "No renewal gaps. Every renewal has its dates, applications and people.",
    mine: "No renewal gaps on renewals you follow up.",
    unassigned: "No renewal gaps on renewals without an IT follow-up.",
  },
  discovery_candidates: {
    all: "No discovery candidates. Every application a source reported is linked or dismissed.",
    mine: "No discovery candidates are routed to you.",
    unassigned: "No discovery candidates without a reviewer.",
  },
  discovery_missing: {
    all: "Every linked record is still listed by its source, or someone kept or archived it.",
    mine: "No records assigned to you are missing from their source.",
    unassigned: "No missing records without an IT steward or reviewer.",
  },
  data_quality: {
    all: "No data-quality failures. Every active record passes the checks that are on, or has an exception.",
    mine: "No data-quality failures on records assigned to you.",
    unassigned: "No data-quality failures on records without an IT steward.",
  },
  seat_counts: {
    all: "No proposed seat counts. Every license pool matches what its sources report, or the difference was dismissed.",
    mine: "No proposed seat counts on pools of applications assigned to you.",
    unassigned: "No proposed seat counts on pools of applications without an IT steward.",
  },
  vendor_risk: {
    all: "No vendor risk gaps. High and critical vendors have current assurance, and vendors with personal data a signed DPA.",
    mine: "No vendor risk gaps are routed to you.",
    unassigned: "No vendor risk gaps without a coordinator.",
  },
  end_of_life: {
    all: "No tracked version is past end of life or within 180 days of it.",
    mine: "No versions at or near end of life on records assigned to you.",
    unassigned: "No versions at or near end of life on records without an IT steward.",
  },
  data_flows: {
    all: "No risky data flows. Confidential and personal data go to vendors with a DPA, with authentication, and no lower than its classification.",
    mine: "No risky data flows on integrations assigned to you.",
    unassigned: "No risky data flows on integrations without an IT steward.",
  },
};

/** Reminders aren't a snooze subject (their own snooze applies to everyone), so they sit outside EMPTY. */
const REMINDERS_EMPTY: Record<AttentionScope, string> = {
  all: "No reminders due. Contract, periodic review, custom date and credential deadlines are outside the reminder window.",
  mine: "No reminders due for items routed to you.",
  unassigned: "No reminders due for items without an IT steward or owner.",
};

const GAP_FIELD: Record<string, string> = {
  "No business owner": "business_owner", "No technical owner": "technical_owner", "Inactive owner": "technical_owner", "No support team": "support_team",
  "No owner": "business_owner",
};

/** Record review or Assign owner straight from a queue row: the record's own dialog, in one
 *  activation. Cancel returns focus here; Save hands the notice to the page, which moves focus if the row goes. */
function QueueTask({ task, row, gaps, onSaved }: {
  task: "review" | "owners"; row: { id: string; kind: string; name: string }; gaps?: string[]; onSaved: (message: string) => void;
}) {
  const [rec, setRec] = useState<CatalogRecord | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // The record's own API collection (applications, components, ai-agents); never a guessed plural.
  const base = isRecordKind(row.kind) ? apiPath(row.kind) : null;
  async function start() {
    if (!base) return setError("This kind of record can't be opened here.");
    setBusy(true);
    setError("");
    try {
      setRec(await api<CatalogRecord>(`${base}/${row.id}`));
      setOpen(true);
    } catch (e) {
      setError(e instanceof Problem ? e.detail : "Couldn't open the record.");
    } finally {
      setBusy(false);
    }
  }
  const done = (message: string) => { setOpen(false); onSaved(message); };
  return (
    <>
      <Button size="sm" variant="queue" data-queue-action="" aria-busy={busy || undefined} onClick={() => void start()}>
        {task === "review" ? "Record review" : "Assign owner"}<span className="sr-only"> for {row.name}</span>
      </Button>
      {error && <span className="text-sm text-error-foreground" role="alert">{error}</span>}
      {rec && base && task === "owners" && <OwnersDialog rec={rec} base={base} focus={GAP_FIELD[gaps?.[0] ?? ""] ?? ""} open={open} onOpenChange={setOpen} onDone={done} />}
      {rec && base && task === "review" && <ReviewDialog rec={rec} base={base} open={open} onOpenChange={setOpen} onDone={done} />}
    </>
  );
}

const endName = (e: { name: string; instance: string | null }) => (e.instance ? `${e.name} (${e.instance})` : e.name);
/** A relationship row's verb from its type's current name ("Depends on" reads "depends on"), as the digest words it. */
const verbOf = (r: LinkRow) => phraseOf(r.type_name, r.type);

export function NeedsAttention() {
  // Scope lives in the URL (?scope=all) so the view is shareable and survives reload.
  const [params, setParams] = useSearchParams();
  const asked = params.get("scope");
  const scope: AttentionScope = asked === "all" || asked === "unassigned" ? asked : "mine";
  const setScope = (s: AttentionScope) => setParams(s === "mine" ? {} : { scope: s });
  // Strict: while a new scope loads, or if it fails, no rows or actions of the previous scope are shown.
  const { data, error, reload, latest } = useLoad<Attention>(`needs-attention?scope=${scope}`, { strict: true });
  const me = useMe();
  // The API counts every queue for each scope in one response, so the toggle never needs a second request; the counts
  // don't depend on the scope, so the last ones stay on the toggle while another scope loads.
  const counts = (data ?? latest)?.counts ?? {};
  // Sections the user closed; a jump link opens its section and moves focus to the heading.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  // The all-clear queues share one line; its disclosure opens to show each one's sentence.
  const [clearOpen, setClearOpen] = useState(false);
  const toggle = (id: string) => setCollapsed((cur) => { const next = new Set(cur); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  function jump(e: React.MouseEvent, id: string) {
    e.preventDefault();
    setCollapsed((cur) => { const next = new Set(cur); next.delete(id); return next; });
    setClearOpen(true);
    requestAnimationFrame(() => { const h = document.getElementById(id); h?.scrollIntoView({ block: "start" }); h?.focus(); });
  }
  // A link to one queue (the dashboard's counts: `/?scope=all#h-reviews`) opens that queue and moves focus to its
  // heading once the rows are in.
  const location = useLocation();
  const landed = useRef(false);
  useEffect(() => {
    if (landed.current || !data || !/^#h-[a-z-]+$/.test(location.hash)) return;
    landed.current = true;
    const id = location.hash.slice(1);
    setCollapsed((cur) => { const next = new Set(cur); next.delete(id); return next; });
    setClearOpen(true);
    requestAnimationFrame(() => { const h = document.getElementById(id); h?.scrollIntoView({ block: "start" }); h?.focus(); });
  }, [data, location.hash]);
  const section = (id: string, queue: AttentionQueue) => ({ open: !collapsed.has(id), onToggle: () => toggle(id), empty: EMPTY[queue][scope] });
  // After a dialog saves, the row may leave the queue: focus then goes to the next row's action in that queue, or its
  // heading when it is empty.
  const pendingFocus = useRef<{ queue: string; index: number } | null>(null);
  function taskSaved(queue: string, index: number, message: string) {
    pendingFocus.current = { queue, index };
    setQuiet({ text: message });
    reload();
  }
  useEffect(() => {
    const want = pendingFocus.current;
    if (!want || !data) return;
    pendingFocus.current = null;
    const active = document.activeElement;
    if (active && active !== document.body && active.isConnected) return; // the opener is still there
    const actions = document.querySelectorAll<HTMLElement>(`#${want.queue}-body [data-queue-action]`);
    (actions[Math.min(want.index, actions.length - 1)] ?? document.getElementById(want.queue))?.focus();
  }, [data]);
  const agentGaps = data?.agent_gaps ?? [];
  const qualityRows = data?.data_quality ?? [];
  const seatRows = data?.seat_counts ?? [];
  const reminderRows = data?.reminders ?? [];
  const missingRows = data?.discovery_missing ?? [];
  const vendorRiskRows = data?.vendor_risk ?? [];
  const eolRows = data?.end_of_life ?? [];
  const flowRows = data?.data_flows ?? [];
  const [missing, setMissing] = useState<{ mode: "acknowledge" | "archive"; target: MissingTarget; index: number } | null>(null);
  // Every queue the page shows, as the API counts them in `counts` (the navigation badge), so the page and the badge agree.
  const total = data ? data.ownership_gaps.length + agentGaps.length + data.reviews.length + data.links_to_verify.length + data.stale_assessments.length
    + data.renewal_actions.length + data.renewal_gaps.length + data.discovery_candidates.length + missingRows.length + qualityRows.length + seatRows.length + reminderRows.length + vendorRiskRows.length
    + eolRows.length + flowRows.length : 0;
  // A reminder action changes the reminder for everyone: refetch this scope and the badge's.
  const invalidate = useInvalidate();
  function reminderDone(index: number, text: string, gone: boolean) {
    if (gone) pendingFocus.current = { queue: "h-reminders", index };
    setQuiet({ text });
    void invalidate("needs-attention");
  }
  // Snooze and acknowledge: only the signed-in user's notifications change, so the row stays and is marked.
  const [quiet, setQuiet] = useState<{ text: string; undo?: () => Promise<void> } | null>(null);
  const quietDone = (text: string, undo?: () => Promise<void>) => { setQuiet({ text, undo }); reload(); };
  const quietCell = (queue: AttentionQueue, subjectId: string, name: string, snooze: Snooze | null | undefined) => (
    canWrite(me) ? <QuietMenu target={{ queue, subjectId, name, snooze: snooze ?? null }} onDone={quietDone} /> : null
  );
  const recordName = (r: { id: string; kind: string; name: string }) => (
    <><RecordLink record={r} className="underline" />{r.kind !== "application" && isRecordKind(r.kind) && <> <span className="text-muted-foreground font-normal">{KIND_LABEL[r.kind]}</span></>}</>
  );
  // No page for the row's kind (one this build doesn't know): no button, rather than a link somewhere else.
  const nextStep = (label: string, r: { id: string; kind: string; name: string }) => pathOf(r) && (
    <Button asChild size="sm" variant="queue">
      <Link to={pathOf(r)!}>{canWrite(me) ? label : "View record"}<span className="sr-only"> for {r.name}</span></Link>
    </Button>
  );
  const head = (cols: string[]) => (
    <TableHeader><TableRow>
      {cols.map((c) => <TableHead key={c}>{c}</TableHead>)}
      <TableHead><span className="sr-only">Next step</span></TableHead>
    </TableRow></TableHeader>
  );
  // A relationship reads "Source depends on Target"; an integration shows its own name and its two ends.
  const linkName = (r: LinkRow) => (
    r.link_kind === "integration"
      ? <><RecordLink record={r.record} className="underline" /> <span className="font-normal text-muted-foreground">Integration, {endName(r.source)} to {endName(r.target)}</span></>
      : <><RecordLink record={r.record} className="underline">{endName(r.source)}</RecordLink> <span className="font-normal text-muted-foreground">{verbOf(r)}</span> <RecordLink record={r.target} className="underline">{endName(r.target)}</RecordLink></>
  );
  const linkLabel = (r: LinkRow) => (r.link_kind === "integration" ? r.record.name : `${endName(r.source)} ${verbOf(r)} ${endName(r.target)}`);
  // A relationship is confirmed from its source record's Relationships tab; an integration from its own page.
  const linkTarget = (r: LinkRow) => pathOf(r.record, r.link_kind === "integration" ? undefined : "relationships");
  const staleSub = (r: StaleAssessmentRow) => <>{cap(r.change_type)} of {r.target.name}</>;
  return (
    <>
      <PageHead title="Needs attention" desc={data ? `${total} ${total === 1 ? "item" : "items"} derived from your catalog records.` : "Work derived from your catalog records."}>
        <ScopeToggle scope={scope} scopes={SCOPES} onChange={setScope} counts={counts} />
      </PageHead>
      <p className="sr-only" aria-live="polite">{data ? `Showing ${total} items${scope === "mine" ? " assigned to you" : scope === "unassigned" ? " with no steward or owner" : ""}.` : ""}</p>
      {error && <ErrorBanner message={error} onRetry={reload} />}
      <div className={quiet ? "mb-4 flex flex-wrap items-center gap-3 rounded-md border border-success-border bg-success-bg px-3 py-2 text-success-foreground" : ""}>
        {/* The status region stays mounted so each change is announced. */}
        <span role="status">{quiet ? `✓ ${quiet.text}` : ""}</span>
        {quiet?.undo && <Button size="sm" variant="outline" onClick={() => { void quiet.undo!().then(() => { setQuiet({ text: "Undone. Notifications are back on." }); reload(); }); }}>Undo</Button>}
      </div>
      {!data && !error && <Loading what="Needs attention" rows={4} />}
      {/* A new installation with nothing listed (the page's own total too: reminders aren't in the API's counts yet). */}
      {data && data.records === 0 && data.counts.all === 0 && total === 0 && (
        <EmptyState title="Nothing needs attention yet" action={canWrite(me) && <Button asChild><Link to="/applications/new">Add application</Link></Button>}>
          Add applications and components to see ownership and review gaps here.
        </EmptyState>
      )}
      {data && (data.records > 0 || data.counts.all > 0 || total > 0) && (
        <>
          {/* Jump links to the queues with items only: the Nothing waiting line names the empty ones. */}
          {total > 0 && <nav aria-label="Queues" className="mb-4 text-sm text-muted-foreground">
            <ul className="flex flex-wrap gap-x-4 gap-y-1">
              {([["h-owners", "Ownership gaps", data.ownership_gaps.length], ["h-agents", "AI agent gaps", agentGaps.length], ["h-reviews", "Reviews", data.reviews.length],
                ["h-links", "Links to verify", data.links_to_verify.length], ["h-stale", "Stale assessments", data.stale_assessments.length],
                ["h-renewal-actions", "Renewal actions", data.renewal_actions.length], ["h-renewal-gaps", "Renewal gaps", data.renewal_gaps.length],
                ["h-discovery", "Discovery candidates", data.discovery_candidates.length],
                ["h-missing", "No longer seen in source", missingRows.length], ["h-quality", "Data quality", qualityRows.length],
                ["h-seats", "Seat counts", seatRows.length], ["h-vendor-risk", "Vendor risk", vendorRiskRows.length],
                ["h-eol", "End of life", eolRows.length], ["h-flows", "Data flows", flowRows.length],
                ["h-reminders", "Reminders", reminderRows.length]] as const).filter(([, , n]) => n > 0).map(([id, label, n]) => (
                <li key={id}><a href={`#${id}`} className="inline-block min-h-6 leading-6" onClick={(e) => jump(e, id)}>{label} <span className="tabular font-semibold text-foreground">{n}</span></a></li>
              ))}
            </ul>
          </nav>}
          <Queues clearOpen={clearOpen} onClearToggle={() => setClearOpen(!clearOpen)} queues={[
          <QueueSection id="h-owners" icon={IconOwners} title="Ownership gaps" count={data.ownership_gaps.length}
            note="Sorted by criticality, then name" {...section("h-owners", "ownership_gaps")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">Ownership gaps</TableCaption>
              {head(["Record", "Gap", "Why", "IT steward"])}
              <TableBody>
                {data.ownership_gaps.map((r, i) => (
                  <TableRow key={r.id} className="h-[60px]">
                    <TableHead scope="row" className="font-semibold text-foreground">{recordName(r)}</TableHead>
                    <TableCell data-label="Gap" className="whitespace-normal"><GapChips gaps={r.gaps} /></TableCell>
                    <TableCell data-label="Why" className="max-w-xs whitespace-normal text-muted-foreground">{r.why}</TableCell>
                    <TableCell data-label="IT steward"><StewardCell s={r.steward} /></TableCell>
                    <TableCell className="next-step text-right"><span className="inline-flex flex-wrap items-center justify-end gap-2"><SnoozeMark snooze={r.snooze ?? null} />{canWrite(me) ? <QueueTask task="owners" row={r} gaps={r.gaps} onSaved={(m) => taskSaved("h-owners", i, m)} /> : nextStep("Assign owner", r)}{quietCell("ownership_gaps", r.id, r.name, r.snooze)}</span></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-agents" icon={IconAgent} title="AI agent gaps" count={agentGaps.length}
            note="Sorted by criticality, then name" {...section("h-agents", "agent_gaps")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">AI agent gaps</TableCaption>
              {head(["AI agent", "Gap", "Why", "IT steward"])}
              <TableBody>
                {agentGaps.map((r, i) => (
                  <TableRow key={r.id} className="h-[60px]">
                    <TableHead scope="row" className="font-semibold text-foreground"><RecordLink record={r} className="underline" /></TableHead>
                    <TableCell data-label="Gap" className="whitespace-normal"><GapChips gaps={r.gaps} /></TableCell>
                    <TableCell data-label="Why" className="max-w-xs whitespace-normal text-muted-foreground">{r.why}</TableCell>
                    <TableCell data-label="IT steward"><StewardCell s={r.steward} /></TableCell>
                    <TableCell className="next-step text-right">
                      <span className="inline-flex flex-wrap items-center justify-end gap-2">
                        <SnoozeMark snooze={r.snooze ?? null} />
                        {/* An ownerless agent gets the owner dialog, as in Ownership gaps; the agent's own gaps are fields of its form. */}
                        {canWrite(me) && r.gaps.includes("No owner")
                          ? <QueueTask task="owners" row={r} gaps={r.gaps} onSaved={(m) => taskSaved("h-agents", i, m)} />
                          : (
                            <Button asChild size="sm" variant="queue">
                              <Link to={canWrite(me) ? editPath("ai_agent", r.id) : recordPath("ai_agent", r.id)!} data-queue-action="">{canWrite(me) ? "Fill gaps" : "View agent"}<span className="sr-only"> for {r.name}</span></Link>
                            </Button>
                          )}
                        {quietCell("agent_gaps", r.id, r.name, r.snooze)}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-reviews" icon={IconReview} title="Reviews" count={data.reviews.length}
            note="Sorted by state (Overdue, Needs confirmation, Unreviewed), then due date" {...section("h-reviews", "reviews")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">Reviews</TableCaption>
              {head(["Record", "Review state", "Why", "Due", "IT steward"])}
              <TableBody>
                {data.reviews.map((r, i) => (
                  <TableRow key={r.id} className="h-[60px]">
                    <TableHead scope="row" className="font-semibold text-foreground">{recordName(r)}</TableHead>
                    <TableCell data-label="State"><StateChip state={r.state} /></TableCell>
                    <TableCell data-label="Why" className="max-w-xs whitespace-normal text-muted-foreground">{r.why}</TableCell>
                    <TableCell data-label="Due" className="tabular">{r.due ?? "—"}</TableCell>
                    <TableCell data-label="IT steward"><StewardCell s={r.steward} /></TableCell>
                    <TableCell className="next-step text-right"><span className="inline-flex flex-wrap items-center justify-end gap-2"><SnoozeMark snooze={r.snooze ?? null} />{canWrite(me) ? <QueueTask task="review" row={r} onSaved={(m) => taskSaved("h-reviews", i, m)} /> : nextStep("Record review", r)}{quietCell("reviews", r.id, r.name, r.snooze)}</span></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-links" icon={IconLink} title="Links to verify" count={data.links_to_verify.length}
            note="Sorted by last confirmed, oldest first" {...section("h-links", "links_to_verify")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">Links to verify</TableCaption>
              {head(["Link", "Link state", "Why", "Last confirmed", "IT steward"])}
              <TableBody>
                {data.links_to_verify.map((r) => (
                  <TableRow key={`${r.link_kind}-${r.id}`} className="h-[60px]">
                    <TableHead scope="row" className="font-semibold text-foreground">{linkName(r)}</TableHead>
                    <TableCell data-label="Link state"><LinkStateChip state="needs_verification" /></TableCell>
                    <TableCell data-label="Why" className="max-w-xs whitespace-normal text-muted-foreground">{r.why}</TableCell>
                    <TableCell data-label="Last confirmed" className="tabular">{r.last_confirmed_on}</TableCell>
                    <TableCell data-label="IT steward"><StewardCell s={r.steward} /></TableCell>
                    <TableCell className="next-step text-right">
                      <span className="inline-flex flex-wrap items-center justify-end gap-2">
                        <SnoozeMark snooze={r.snooze ?? null} />
                        {linkTarget(r) && (
                          <Button asChild size="sm" variant="queue">
                            <Link to={linkTarget(r)!}>{canWrite(me) ? "Confirm link" : "View link"}<span className="sr-only"> for {linkLabel(r)}</span></Link>
                          </Button>
                        )}
                        {quietCell("links_to_verify", `${r.link_kind}:${r.id}`, linkLabel(r), r.snooze)}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-stale" icon={IconStale} title="Stale assessments" count={data.stale_assessments.length}
            note="Sorted by target date" {...section("h-stale", "stale_assessments")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">Stale assessments</TableCaption>
              {head(["Assessment", "Snapshot", "Why", "Target date", "Assessment owner"])}
              <TableBody>
                {data.stale_assessments.map((r) => (
                  <TableRow key={r.id} className="h-[60px]">
                    <TableHead scope="row" className="font-semibold text-foreground">
                      <Link to={`/assessments/${r.id}`} className="underline">{r.title}</Link>{" "}
                      <span className="font-normal text-muted-foreground">{staleSub(r)}</span>
                    </TableHead>
                    <TableCell data-label="Snapshot"><span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Stale since {r.stale_since}</span></TableCell>
                    <TableCell data-label="Why" className="max-w-xs whitespace-normal text-muted-foreground">{r.why}</TableCell>
                    <TableCell data-label="Target date" className="tabular">{r.target_date ?? "—"}</TableCell>
                    <TableCell data-label="Owner"><StewardCell s={r.steward} /></TableCell>
                    <TableCell className="next-step text-right">
                      <span className="inline-flex flex-wrap items-center justify-end gap-2">
                        <SnoozeMark snooze={r.snooze ?? null} />
                        <Button asChild size="sm" variant="queue">
                          <Link to={`/assessments/${r.id}`}>{canWrite(me) ? "Open and refresh" : "View assessment"}<span className="sr-only"> for {r.title}</span></Link>
                        </Button>
                        {quietCell("stale_assessments", r.id, r.title, r.snooze)}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-renewal-actions" icon={IconRenewal} title="Renewal actions" count={data.renewal_actions.length}
            note="Sorted by action deadline" {...section("h-renewal-actions", "renewal_actions")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">Renewal actions</TableCaption>
              {head(["Renewal", "Action deadline", "Decision", "IT follow-up"])}
              <TableBody>
                {data.renewal_actions.map((r) => (
                  <TableRow key={r.id} className="h-[60px]">
                    <TableHead scope="row" className="font-semibold text-foreground">
                      <Link to={`/renewals/${r.id}`} className="underline">{r.name}</Link>
                      {r.vendor && <span className="block font-normal text-muted-foreground">{r.vendor}</span>}
                    </TableHead>
                    <TableCell data-label="Action deadline" className="whitespace-normal">
                      <DeadlineCell deadline={{ date: r.deadline, label: r.label, label_text: r.label_text, explanation: r.why, timing: r.timing }} />
                    </TableCell>
                    <TableCell data-label="Decision" className="whitespace-normal">
                      {r.decision ? <>{DECISION_LABEL[r.decision]}<span className="block text-sm text-muted-foreground">Not completed</span></> : "No decision"}
                    </TableCell>
                    <TableCell data-label="IT follow-up"><StewardCell s={r.steward} /></TableCell>
                    <TableCell className="next-step text-right">
                      <span className="inline-flex flex-wrap items-center justify-end gap-2">
                        <SnoozeMark snooze={r.snooze} />
                        <Button asChild size="sm" variant="queue">
                          <Link to={`/renewals/${r.id}`}>{canWrite(me) ? (r.decision ? "Record completion" : "Record decision") : "View renewal"}<span className="sr-only"> for {r.name}</span></Link>
                        </Button>
                        {quietCell("renewal_actions", r.id, r.name, r.snooze)}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-renewal-gaps" icon={IconRenewal} title="Renewal gaps" count={data.renewal_gaps.length}
            note="Sorted by action deadline, none last" {...section("h-renewal-gaps", "renewal_gaps")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">Renewal gaps</TableCaption>
              {head(["Renewal", "Gap", "Action deadline", "IT follow-up"])}
              <TableBody>
                {data.renewal_gaps.map((r) => (
                  <TableRow key={r.id} className="h-[60px]">
                    <TableHead scope="row" className="font-semibold text-foreground">
                      <Link to={`/renewals/${r.id}`} className="underline">{r.name}</Link>
                      {r.vendor && <span className="block font-normal text-muted-foreground">{r.vendor}</span>}
                    </TableHead>
                    <TableCell data-label="Gap" className="whitespace-normal"><GapChips gaps={r.gaps} /></TableCell>
                    <TableCell data-label="Action deadline" className="tabular whitespace-normal">{r.deadline ? `${r.deadline} (${r.label_text})` : "—"}</TableCell>
                    <TableCell data-label="IT follow-up"><StewardCell s={r.steward} /></TableCell>
                    <TableCell className="next-step text-right">
                      <span className="inline-flex flex-wrap items-center justify-end gap-2">
                        <SnoozeMark snooze={r.snooze} />
                        <Button asChild size="sm" variant="queue">
                          <Link to={`/renewals/${r.id}`}>{canWrite(me) ? "Fill gaps" : "View renewal"}<span className="sr-only"> for {r.name}</span></Link>
                        </Button>
                        {quietCell("renewal_gaps", r.id, r.name, r.snooze)}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-discovery" icon={IconDiscovery} title="Discovery candidates" count={data.discovery_candidates.length}
            note="One row per connector" {...section("h-discovery", "discovery_candidates")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">Discovery candidates</TableCaption>
              {head(["Connector", "Candidates", "Reviewer"])}
              <TableBody>
                {data.discovery_candidates.map((r) => (
                  <TableRow key={r.id} className="h-[60px]">
                    <TableHead scope="row" className="font-semibold text-foreground">
                      <Link to={`/discovery?connector=${r.id}`} className="underline">{r.name}</Link>
                    </TableHead>
                    <TableCell data-label="Candidates" className="whitespace-normal">{r.why}</TableCell>
                    <TableCell data-label="Reviewer"><StewardCell s={r.steward} /></TableCell>
                    <TableCell className="next-step text-right">
                      <span className="inline-flex flex-wrap items-center justify-end gap-2">
                        <SnoozeMark snooze={r.snooze} />
                        <Button asChild size="sm" variant="queue">
                          <Link to={`/discovery?connector=${r.id}`}>{canWrite(me) ? "Review candidates" : "View candidates"}<span className="sr-only"> from {r.name}</span></Link>
                        </Button>
                        {quietCell("discovery_candidates", r.id, r.name, r.snooze)}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-missing" icon={IconGone} title="No longer seen in source" count={missingRows.length}
            note="One row per record a source stopped listing; oldest first" {...section("h-missing", "discovery_missing")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">No longer seen in source</TableCaption>
              {head(["Record", "Source", "Since", "IT steward"])}
              <TableBody>
                {missingRows.map((r: MissingQueueRow, i) => {
                  const target: MissingTarget = { observation: r.observation.id, sourceName: r.observation.name, connector: r.connector.name, since: r.since, record: r.record };
                  return (
                    <TableRow key={r.id} className="h-[60px]" data-missing={r.id}>
                      <TableHead scope="row" className="font-semibold text-foreground">{recordName(r.record)}</TableHead>
                      <TableCell data-label="Source" className="whitespace-normal">
                        “{r.observation.name}” in <Link to={`/discovery?status=missing&connector=${r.connector.id}`} className="underline">{r.connector.name}</Link>
                        <span className="block text-sm text-muted-foreground"><span aria-hidden="true">! </span>No longer seen in source</span>
                      </TableCell>
                      <TableCell data-label="Since" className="tabular"><time dateTime={r.since}>{r.since}</time></TableCell>
                      <TableCell data-label="IT steward"><StewardCell s={r.steward} /></TableCell>
                      <TableCell className="next-step text-right">
                        <span className="inline-flex flex-wrap items-center justify-end gap-2">
                          <SnoozeMark snooze={r.snooze} />
                          {canWrite(me) ? <>
                            <Button size="sm" variant="queue" data-queue-action="" onClick={() => setMissing({ mode: "acknowledge", target, index: i })}>Keep record<span className="sr-only"> {r.record.name}</span></Button>
                            <Button size="sm" variant="outline" onClick={() => setMissing({ mode: "archive", target, index: i })}>Archive<span className="sr-only"> {r.record.name}</span></Button>
                          </> : nextStep("View record", r.record)}
                          {quietCell("discovery_missing", r.id, r.record.name, r.snooze)}
                        </span>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-quality" icon={IconQuality} title="Data quality" count={qualityRows.length}
            note="One row per check and IT steward; most severe first" {...section("h-quality", "data_quality")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">Data quality</TableCaption>
              {head(["Check", "Records", "Why", "IT steward"])}
              <TableBody>
                {qualityRows.map((r) => {
                  const to = `/data-quality?rule=${encodeURIComponent(r.rule)}&it_steward=${r.it_steward ?? "none"}`;
                  return (
                    <TableRow key={r.id} className="h-[60px]">
                      <TableHead scope="row" className="font-semibold text-foreground">
                        <Link to={to} className="underline">{r.label}</Link>
                        <span className="block font-normal text-muted-foreground">{SEVERITY_LABEL[r.severity]} severity</span>
                      </TableHead>
                      <TableCell data-label="Records" className="tabular">{r.count}</TableCell>
                      <TableCell data-label="Why" className="max-w-xs whitespace-normal text-muted-foreground">{r.why}</TableCell>
                      <TableCell data-label="IT steward"><StewardCell s={r.steward} /></TableCell>
                      <TableCell className="next-step text-right">
                        <span className="inline-flex flex-wrap items-center justify-end gap-2">
                          <SnoozeMark snooze={r.snooze} />
                          <Button asChild size="sm" variant="queue">
                            <Link to={to}>{canWrite(me) ? "Fix records" : "View records"}<span className="sr-only"> failing {r.label}{r.steward.name ? `, ${r.steward.name}` : ""}</span></Link>
                          </Button>
                          {quietCell("data_quality", r.id, `${r.label} (${r.steward.name})`, r.snooze)}
                        </span>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-seats" icon={IconSeats} title="Seat counts" count={seatRows.length}
            note="One row per license pool a source disagrees with; by application" {...section("h-seats", "seat_counts")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">Seat counts</TableCaption>
              {head(["License pool", "Sources", "Why", "IT steward"])}
              <TableBody>
                {seatRows.map((r) => {
                  const to = `/license-pools/${r.id}?tab=review`;
                  return (
                    <TableRow key={r.id} className="h-[60px]">
                      <TableHead scope="row" className="font-semibold text-foreground">
                        <Link to={`/license-pools/${r.id}`} className="underline">{r.name}</Link>
                        <span className="block font-normal text-muted-foreground">{r.application.name}</span>
                      </TableHead>
                      <TableCell data-label="Sources" className="tabular">{r.proposals}</TableCell>
                      <TableCell data-label="Why" className="max-w-xs whitespace-normal text-muted-foreground">{r.why}</TableCell>
                      <TableCell data-label="IT steward"><StewardCell s={r.steward} /></TableCell>
                      <TableCell className="next-step text-right">
                        <span className="inline-flex flex-wrap items-center justify-end gap-2">
                          <SnoozeMark snooze={r.snooze} />
                          <Button asChild size="sm" variant="queue">
                            <Link to={to} data-queue-action="">{canWrite(me) ? "Review seat counts" : "View seat counts"}<span className="sr-only"> for {r.name}, {r.application.name}</span></Link>
                          </Button>
                          {quietCell("seat_counts", r.id, `${r.name} (${r.application.name})`, r.snooze)}
                        </span>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-vendor-risk" icon={IconVendorRisk} title="Vendor risk" count={vendorRiskRows.length}
            note="One row per vendor; by risk tier, then name" {...section("h-vendor-risk", "vendor_risk")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">Vendor risk</TableCaption>
              {head(["Vendor", "Risk tier", "Gap", "Why", "Coordinator"])}
              <TableBody>
                {vendorRiskRows.map((r) => {
                  const to = `/vendors/${r.id}?tab=risk`;
                  return (
                    <TableRow key={r.id} className="h-[60px]" data-vendor-risk={r.id}>
                      <TableHead scope="row" className="font-semibold text-foreground"><Link to={to} className="underline">{r.name}</Link></TableHead>
                      <TableCell data-label="Risk tier">{/* As TierMark (features/vendors/risk-ui) shows it, without pulling that chunk into the first bundle. */}
                        {r.risk_tier ? <CriticalityMark value={r.risk_tier} suffix=" risk" /> : <span className={chip({ state: "unreviewed" })}><span aria-hidden="true">○</span>Not assessed</span>}</TableCell>
                      <TableCell data-label="Gap" className="whitespace-normal"><GapChips gaps={r.gaps.map((g) => g.label)} /></TableCell>
                      <TableCell data-label="Why" className="max-w-xs whitespace-normal text-muted-foreground">{r.why}</TableCell>
                      <TableCell data-label="Coordinator"><StewardCell s={r.steward} /></TableCell>
                      <TableCell className="next-step text-right">
                        <span className="inline-flex flex-wrap items-center justify-end gap-2">
                          <SnoozeMark snooze={r.snooze} />
                          <Button asChild size="sm" variant="queue">
                            <Link to={to} data-queue-action="">{canWrite(me) ? "Open risk file" : "View risk file"}<span className="sr-only"> of {r.name}</span></Link>
                          </Button>
                          {quietCell("vendor_risk", r.id, r.name, r.snooze)}
                        </span>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-eol" icon={IconEol} title="End of life" count={eolRows.length}
            note="Past end of life or within 180 days; soonest first" {...section("h-eol", "end_of_life")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">End of life</TableCaption>
              {head(["Record", "Version", "Status", "Why", "IT steward"])}
              <TableBody>
                {eolRows.map((r) => {
                  const name = r.instance ? `${r.record.name} (${r.instance.name})` : r.record.name;
                  const to = recordPath(r.record.kind, r.record.id);
                  return (
                    <TableRow key={r.id} className="h-[60px]" data-eol={r.record.name}>
                      <TableHead scope="row" className="font-semibold text-foreground">
                        {recordName(r.record)}
                        {r.instance && <span className="block font-normal text-muted-foreground">Instance {r.instance.name}</span>}
                      </TableHead>
                      <TableCell data-label="Version">{r.product_label}{r.cycle ? ` ${r.cycle}` : ""}</TableCell>
                      <TableCell data-label="Status">
                        <EolStatusChip row={{ status: r.status, days_remaining: r.days_remaining, unknown_reason: null }} />
                        {r.eol_date && <span className="mt-1 block text-sm text-muted-foreground tabular">{r.eol_date}</span>}
                      </TableCell>
                      <TableCell data-label="Why" className="max-w-xs whitespace-normal text-muted-foreground">{r.why}</TableCell>
                      <TableCell data-label="IT steward"><StewardCell s={r.steward} /></TableCell>
                      <TableCell className="next-step text-right">
                        <span className="inline-flex flex-wrap items-center justify-end gap-2">
                          <SnoozeMark snooze={r.snooze} />
                          {to && (
                            <Button asChild size="sm" variant="queue">
                              <Link to={to} data-queue-action="">{canWrite(me) ? "Open record" : "View record"}<span className="sr-only"> for {name}</span></Link>
                            </Button>
                          )}
                          {quietCell("end_of_life", r.id, name, r.snooze)}
                        </span>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-flows" icon={IconFlows} title="Data flows" count={flowRows.length}
            note="One row per risky flow; the most sensitive first" {...section("h-flows", "data_flows")}>
            <Table className="queue-table">
              <TableCaption className="sr-only">Data flows</TableCaption>
              {head(["Flow", "Classification", "Risks", "Why", "IT steward"])}
              <TableBody>
                {flowRows.map((r) => (
                  <TableRow key={r.id} className="h-[60px]" data-flow={r.name}>
                    <TableHead scope="row" className="font-semibold text-foreground">
                      <Link to={`/integrations/${r.id}`} className="underline">{r.name}</Link>
                      <span className="block font-normal text-muted-foreground">{r.source.name} to {r.target.name}</span>
                    </TableHead>
                    <TableCell data-label="Classification">{r.classification ? CLASSIFICATION_LABEL[r.classification] : <Gap label="No classification" />}</TableCell>
                    <TableCell data-label="Risks" className="whitespace-normal"><RiskChips risks={r.risks} /></TableCell>
                    <TableCell data-label="Why" className="max-w-xs whitespace-normal text-muted-foreground">{r.why}</TableCell>
                    <TableCell data-label="IT steward"><StewardCell s={r.steward} /></TableCell>
                    <TableCell className="next-step text-right">
                      <span className="inline-flex flex-wrap items-center justify-end gap-2">
                        <SnoozeMark snooze={r.snooze} />
                        <Button asChild size="sm" variant="queue">
                          <Link to={`/integrations/${r.id}`} data-queue-action="">{canWrite(me) ? "Open integration" : "View integration"}<span className="sr-only"> {r.name}</span></Link>
                        </Button>
                        {quietCell("data_flows", r.id, r.name, r.snooze)}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </QueueSection>,
          <QueueSection id="h-reminders" icon={IconBell} title="Reminders" count={reminderRows.length}
            note="Sorted by due date" open={!collapsed.has("h-reminders")} onToggle={() => toggle("h-reminders")} empty={REMINDERS_EMPTY[scope]}>
            <Table className="queue-table">
              <TableCaption className="sr-only">Reminders</TableCaption>
              {head(["Reminder", "Due", "Status", "IT steward"])}
              <TableBody>
                {reminderRows.map((r, i) => (
                  <TableRow key={r.id} className="h-[60px]">
                    <TableHead scope="row" className="font-semibold text-foreground">
                      {r.path ? <Link to={r.path} className="underline">{r.name}</Link> : r.name}
                      <span className="block font-normal text-muted-foreground">{r.label}</span>
                    </TableHead>
                    <TableCell data-label="Due" className="whitespace-normal">
                      <DeadlineCell deadline={{ date: r.due, label: null, label_text: null, explanation: r.why, timing: r.timing }} />
                    </TableCell>
                    <TableCell data-label="Status"><ReminderStatusChip status={r.status} until={r.snoozed_until} /></TableCell>
                    <TableCell data-label="IT steward"><StewardCell s={r.steward} /></TableCell>
                    <TableCell className="next-step text-right">
                      <span className="inline-flex flex-wrap items-center justify-end gap-2">
                        {r.path && (
                          <Button asChild size="sm" variant="queue">
                            <Link to={r.path} data-queue-action="">{canWrite(me) ? "Open" : "View"}<span className="sr-only"> {r.name}, {r.label}</span></Link>
                          </Button>
                        )}
                        {canWrite(me) && <ReminderActions target={{ id: r.id, name: `${r.name}, ${r.label}`, status: r.status, sourceType: r.source_type }}
                          onDone={(text, gone) => reminderDone(i, text, gone)} />}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </QueueSection>,
          ]} />
        </>
      )}
      {missing && (
        <LazyPart what="the dialog">
          <MissingDialog mode={missing.mode} target={missing.target} onClose={() => setMissing(null)}
            onDone={(text) => { const i = missing.index; setMissing(null); taskSaved("h-missing", i, text); void invalidate("needs-attention", "discovery"); }} />
        </LazyPart>
      )}
    </>
  );
}
