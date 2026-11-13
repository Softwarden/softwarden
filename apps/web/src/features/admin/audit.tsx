/** Audit activity on a record (any role) and the organization audit log (Admin only),
 *  with request ids, snapshots and related targets.
 *
 *  - The log (`/admin/audit`): filters in the URL (actor, action, from, to, target_type, target, request_id), newest
 *    first, 50 at a time with "Show older events" (the API's cursor is the last event's ID; it has no previous page or
 *    ordering, so this keeps its own table rather than RecordTable). Each event's label opens its detail page.
 *  - One event (`/admin/audit/:eventId`): who, when, what it concerns, its request, and a DiffView of its snapshots.
 *  - A record's Audit activity tab: the same table; each row expands to its DiffView through the record's own event
 *    route (`<base>/<id>/audit/<event id>`, any role). Events on the record's children (attachments, instances, links,
 *    reviews…) carry `child`: the row shows the child's kind and its sentence ("Added attachment 'Contract'").
 *    "Only this record" (`?children=false` in the URL) keeps the record's own events.
 *  - Export: CSV with the log's filters, and JSON lines for a SIEM with the `audit.export` capability. */
import { useEffect, useId, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import { ScrollText } from "lucide-react";
import { api, Problem, UserRef } from "@/api";
import { DownloadError, saveDownload } from "@/lib/download";
import { AdminOnly, Breadcrumb, EmptyState, ErrorBanner, isNotFound, Loading, NotFound, PageHead, Select, TableRegion, useLoad } from "@/components/app";
import { DiffView, diffMode, type DiffRow, type DiffSource } from "@/components/diff-view";
import { chip, DetailList, DetailRow, Panel } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { apiKey, problemText, useApiQuery } from "@/lib/query";
import { useMe } from "@/lib/session";
import { isRecordKind, pathOf, recordPath } from "@/lib/kinds";
import { attachmentsPage } from "@/features/attachments/paths";

type TargetKind = "application" | "component" | "ai_agent" | "integration" | "person" | "relationship" | "assessment" | "renewal" | "vendor" | "contract" | "license_pool";
export type AuditEvent = {
  id: string; at: string; actor: { id: number; name: string } | null; action: string; label: string; summary: string;
  target: {
    kind: TargetKind; id: string; name: string;
    /** For a relationship: its source record, where the link is listed. */
    record?: { kind: "application" | "component" | "ai_agent"; id: string } | null;
  } | null;
  /** The row the event wrote (a review, an instance…) and, for such a child row, its parent record. */
  target_type: string; target_id: string | null;
  related_target_type: string; related_target_id: string | null;
  request_id: string | null;
  /** In a record's History: the child the event is about, or null for the record's own event. `kind` is
   *  attachment, instance, relationship, link_confirmation, review, quality_exception, integration, renewal_cycle or
   *  other (a child type this build doesn't know; `kind_label` "Related"). `id` is null for an older event. */
  child?: AuditChild | null;
};
export type AuditChild = { kind: string; kind_label: string; id: string | null; name: string; sentence: string };
export type AuditEventDetail = AuditEvent & {
  prechange: Record<string, unknown> | null;
  postchange: Record<string, unknown> | null;
  diff_source: DiffSource;
  diff: DiffRow[];
};
type Option = { value: string; label: string };
type AuditPage = { results: AuditEvent[]; next: string | null; actions?: Option[]; target_types?: Option[] };

/** "2026-10-31T09:12+01:00" → "2026-10-31 09:12" (already in the organization timezone). */
const when = (at: string) => at.slice(0, 16).replace("T", " ");

/** A child row's part of its record, in words, for events written on it rather than on the record itself. */
const CHILD: Record<string, { phrase: string; noun: string; tab: (t: NonNullable<AuditEvent["target"]>) => string | null }> = {
  ApplicationInstance: { phrase: "an instance", noun: "instance", tab: (t) => (t.kind === "application" ? `/applications/${t.id}?tab=instances` : null) },
  RecordReview: { phrase: "a review", noun: "review", tab: (t) => (isRecordKind(t.kind) ? recordPath(t.kind, t.id, "history") : null) },
  LinkConfirmation: {
    phrase: "a link confirmation",
    noun: "link confirmation",
    tab: (t) => (t.kind === "integration" ? recordPath("integration", t.id, "history") : t.kind === "relationship" && t.record ? pathOf(t.record, "relationships") : null),
  },
  RenewalCycle: { phrase: "a renewal cycle", noun: "renewal cycle", tab: (t) => (t.kind === "renewal" ? `/renewals/${t.id}?tab=history` : null) },
  Relationship: { phrase: "a relationship", noun: "relationship", tab: (t) => (t.kind === "relationship" && t.record ? pathOf(t.record, "relationships") : null) },
  // Attachments on records and vendors, and data-quality exceptions on records.
  Attachment: { phrase: "an attachment", noun: "attachment", tab: (t) => attachmentsPage(t.kind, t.id) },
  QualityException: { phrase: "a data-quality exception", noun: "data-quality exception", tab: (t) => (isRecordKind(t.kind) ? `/data-quality?tab=exceptions&record=${t.id}` : null) },
};
/** The child an event concerns, or null when it is about the record (or account, setting) itself. */
export function childOf(e: Pick<AuditEvent, "target_type" | "related_target_type">) {
  const c = CHILD[e.target_type];
  return c && (e.related_target_type || e.target_type === "Relationship") ? c : null;
}

function targetHref(t: AuditEvent["target"]): string | null {
  if (!t) return null;
  if (t.kind === "person") return "/admin/people";
  if (t.kind === "assessment") return `/assessments/${t.id}`;
  if (t.kind === "relationship") return t.record ? pathOf(t.record, "relationships") : null;
  if (t.kind === "renewal") return `/renewals/${t.id}`;
  if (t.kind === "vendor") return `/vendors/${t.id}`;
  if (t.kind === "contract") return `/contracts/${t.id}`;
  if (t.kind === "license_pool") return `/license-pools/${t.id}`;
  // A record (application, component, AI agent, integration); a kind this build doesn't know is named, not linked.
  return recordPath(t.kind, t.id);
}

function targetLink(t: AuditEvent["target"]) {
  if (!t) return null;
  const href = targetHref(t);
  return href ? <Link to={href}>{t.name}</Link> : <>{t.name}</>;
}

/** Loads one page at a time; "Show older events" appends the next page. */
function usePaged(path: string) {
  const first = useLoad<AuditPage>(path, { strict: true });
  const [more, setMore] = useState<{ rows: AuditEvent[]; next: string | null } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const rows = [...(first.data?.results ?? []), ...(more?.rows ?? [])];
  const next = more ? more.next : first.data?.next ?? null;
  async function loadMore() {
    if (!next) return;
    setLoadingMore(true);
    setError("");
    try {
      const page = await api<AuditPage>(`${path}${path.includes("?") ? "&" : "?"}cursor=${next}`);
      setMore({ rows: [...(more?.rows ?? []), ...page.results], next: page.next });
    } catch (e) {
      setError(e instanceof Problem ? e.detail : "Couldn't load more events.");
    } finally {
      setLoadingMore(false);
    }
  }
  return { first, rows, next, loadMore, loadingMore, error };
}

function EventCell({ e, href }: { e: AuditEvent; href?: string }) {
  // A History row names its child itself (the kind as a badge, the sentence under Details).
  const child = e.child === undefined ? childOf(e) : null;
  // The log's filters go with the link, so the event page leads back to the log as it was.
  const { search } = useLocation();
  return (
    <>
      {/* A block of its own: beside the "On a review" line, axe would read it as a link inside running text. */}
      {href ? <span className="block"><Link to={href} state={{ search }}>{e.label}</Link></span> : e.label}
      {child && <span className="block text-xs text-muted-foreground">On {child.phrase}</span>}
      {e.child && <span className={`${chip({ state: "neutral" })} mt-1`} data-testid="child-kind">{e.child.kind_label}</span>}
    </>
  );
}

/** The log's table (`detailHref` links each event to its page) or a record's (`expand` loads a row's changes). */
function EventsTable({ rows, label, caption, showTarget, detailHref, expand }: {
  rows: AuditEvent[]; label: string; caption: string; showTarget: boolean;
  detailHref?: (e: AuditEvent) => string;
  expand?: (e: AuditEvent) => string;
}) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (id: string) => setOpen((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const columns = 4 + (showTarget ? 1 : 0) + (expand ? 1 : 0);
  const base = useId();
  return (
    <TableRegion label={label}>
      <Table>
        <TableCaption className="sr-only">{caption}</TableCaption>
        <TableHeader><TableRow>
          <TableHead scope="col" aria-sort="descending">When</TableHead><TableHead scope="col">Actor</TableHead><TableHead scope="col">Event</TableHead>
          {showTarget && <TableHead scope="col">Record</TableHead>}<TableHead scope="col">Details</TableHead>
          {expand && <TableHead scope="col"><span className="sr-only">Changes</span></TableHead>}
        </TableRow></TableHeader>
        <TableBody>
          {rows.map((e) => {
            const isOpen = open.has(e.id);
            const panel = `${base}-changes-${e.id}`;
            return [
              <TableRow key={e.id}>
                <TableHead scope="row" id={`${panel}-when`} className="tabular h-auto py-2 align-top whitespace-nowrap font-normal text-foreground"><time dateTime={e.at}>{when(e.at)}</time></TableHead>
                <TableCell className="align-top">{e.actor?.name ?? "System"}</TableCell>
                <TableCell className="align-top" id={`${panel}-event`}><EventCell e={e} href={detailHref?.(e)} /></TableCell>
                {showTarget && <TableCell className="align-top">{targetLink(e.target) ?? "—"}</TableCell>}
                <TableCell className="max-w-md align-top whitespace-normal [overflow-wrap:anywhere]">{e.child ? e.child.sentence : e.summary || "—"}</TableCell>
                {expand && (
                  <TableCell className="align-top text-right">
                    {/* Named by its text, described by the row's event and time: an aria-label naming the event would
                        also name this cell after it. */}
                    <Button size="sm" variant="outline" aria-expanded={isOpen} aria-controls={isOpen ? panel : undefined} onClick={() => toggle(e.id)}
                      aria-describedby={`${panel}-event ${panel}-when`}>
                      {isOpen ? "Hide changes" : "Show changes"}
                    </Button>
                  </TableCell>
                )}
              </TableRow>,
              expand && isOpen && (
                <TableRow key={`${e.id}-changes`} className="hover:bg-transparent">
                  <TableCell colSpan={columns} id={panel} className="whitespace-normal bg-background px-4 py-3">
                    <EventChanges path={expand(e)} event={e} bare />
                  </TableCell>
                </TableRow>
              ),
            ];
          })}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

/** One event's DiffView, loaded on demand (a History row, the event page). */
function EventChanges({ path, event, bare }: { path: string; event: AuditEvent; bare?: boolean }) {
  const q = useApiQuery<AuditEventDetail>(path);
  if (q.isError) return <ErrorBanner message={problemText(q.error, "Couldn't load this event's changes.")} onRetry={() => void q.refetch()} />;
  if (!q.data) return <Loading what="changes" />;
  return <EventDiff detail={q.data} fallbackSummary={event.summary} bare={bare} />;
}

function EventDiff({ detail, fallbackSummary, bare }: { detail: AuditEventDetail; fallbackSummary?: string; bare?: boolean }) {
  return (
    <DiffView rows={detail.diff} source={detail.diff_source} mode={detail.diff_source === "snapshots" ? diffMode(detail.prechange, detail.postchange) : "changed"}
      caption={`Changes in ${detail.label}, ${when(detail.at)}`} summary={detail.summary || fallbackSummary} bare={bare} />
  );
}

function MoreButton({ paged }: { paged: ReturnType<typeof usePaged> }) {
  return (
    <>
      {paged.error && <div className="mt-3"><ErrorBanner message={paged.error} onRetry={() => void paged.loadMore()} /></div>}
      {paged.next && <div className="mt-3"><Button variant="outline" disabled={paged.loadingMore} onClick={() => void paged.loadMore()}>{paged.loadingMore ? "Loading…" : "Show older events"}</Button></div>}
    </>
  );
}

/** What a record's History includes besides its own events, by the record's API collection. */
const CHILDREN_TEXT: Record<string, string> = {
  applications: "its attachments, instances, reviews, relationships, integrations and data-quality exceptions",
  components: "its attachments, reviews, relationships, integrations and data-quality exceptions",
  "ai-agents": "its attachments, reviews, relationships and data-quality exceptions",
  integrations: "its link confirmations, attachments, data-quality exceptions and the relationships that name it",
  renewals: "its cycles",
  contracts: "its lines and attachments",
};
const RECORD_CHILDREN = "its related items";

/** The Audit activity tab on application, component, AI agent, integration and renewal detail pages (any role). Events
 *  on the record's children are included unless "Only this record" (`?children=false`) is on. */
export function ActivityTab({ base, id, withChildren = true }: { base: string; id: string; withChildren?: boolean }) {
  const [params, setParams] = useSearchParams();
  const own = params.get("children") === "false";
  const path = `${base}/${id}/audit${own ? "?children=false" : ""}`;
  const toggle = (on: boolean) => setParams((cur) => {
    const next = new URLSearchParams(cur);
    if (on) next.set("children", "false"); else next.delete("children");
    return next;
  });
  const children = CHILDREN_TEXT[base] ?? RECORD_CHILDREN;
  // A kind with no children (licence pools): its events only, without the toggle.
  if (!withChildren) return <ActivityResults key={path} path={path} own={false} expand={(e) => `${base}/${id}/audit/${e.id}`} />;
  return (
    <>
      <div className="mb-3 flex flex-col gap-0.5">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input type="checkbox" checked={own} onChange={(e) => toggle(e.target.checked)} className="h-4 w-4 accent-primary" aria-describedby="activity-own-help" />
          Only this record
        </label>
        <span id="activity-own-help" className="pl-6 text-sm text-muted-foreground">
          {own ? `Showing the record's own events. Clear this to include events on ${children}.` : `Also shows events on ${children}.`}
        </span>
      </div>
      <ActivityResults key={path} path={path} own={own} expand={(e) => `${base}/${id}/audit/${e.id}`} />
    </>
  );
}

function ActivityResults({ path, own, expand }: { path: string; own: boolean; expand: (e: AuditEvent) => string }) {
  const paged = usePaged(path);
  if (paged.first.error) return <ErrorBanner message={paged.first.error} onRetry={paged.first.reload} />;
  if (!paged.first.data) return <Loading what="audit activity" rows={3} />;
  if (paged.rows.length === 0) {
    return own
      ? <EmptyState title="No events on the record itself">Its own changes appear here. Clear "Only this record" to see events on its related items too.</EmptyState>
      : <EmptyState title="No activity yet">Changes, reviews, links and instance edits appear here with who made them and when.</EmptyState>;
  }
  return (
    <>
      <EventsTable rows={paged.rows} label="Audit activity table" showTarget={false} expand={expand}
        caption={`Audit activity${own ? " of the record itself" : ", with related items"}, newest first. Show changes opens each event's before and after values.`} />
      <MoreButton paged={paged} />
    </>
  );
}

// ---- the organization's log (Admin) ---------------------------------------------------------------------------------

export const AUDIT_FILTERS = ["actor", "action", "from", "to", "target_type", "target", "related_target", "request_id"] as const;
type FilterKey = (typeof AUDIT_FILTERS)[number];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Administration › Audit log. */
export function AuditLog() {
  return (
    <AdminOnly title="Audit log" what="every change, sign-in and account event in the organization">
      <AuditLogView />
    </AdminOnly>
  );
}

function AuditLogView() {
  const users = useLoad<(UserRef & { role: string })[]>("users");
  const [params, setParams] = useSearchParams();
  const values = Object.fromEntries(AUDIT_FILTERS.map((k) => [k, params.get(k) ?? ""])) as Record<FilterKey, string>;
  const [options, setOptions] = useState<{ actions: Option[]; target_types: Option[] }>({ actions: [], target_types: [] });
  // A discrete change (a select, Clear) pushes a history entry so Back undoes it; a typed one (a date box) replaces.
  const set = (key: FilterKey, value: string, typed = false) => setParams((cur) => {
    const next = new URLSearchParams(cur);
    if (value) next.set(key, value); else next.delete(key);
    return next;
  }, { replace: typed });
  const filtered = AUDIT_FILTERS.some((k) => values[k]);
  const query = new URLSearchParams(AUDIT_FILTERS.filter((k) => values[k]).map((k) => [k, values[k]])).toString();
  const path = `audit-events${query ? `?${query}` : ""}`;
  const exporter = useAuditExport(query);
  return (
    <>
      <PageHead title="Audit log" desc="Every change, sign-in and account event in this organization. Passwords and other secrets are never recorded.">
        {exporter.buttons}
      </PageHead>
      {exporter.messages}
      <form className="mb-4 flex flex-col gap-3" role="search" aria-label="Filter the audit log" onSubmit={(e) => e.preventDefault()}>
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="audit-actor">Actor</Label>
            <Select id="audit-actor" value={values.actor} onChange={(e) => set("actor", e.target.value)}>
              <option value="">Anyone</option>
              {(users.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
              {values.actor && !(users.data ?? []).some((u) => String(u.id) === values.actor) && <option value={values.actor}>User #{values.actor}</option>}
            </Select>
          </div>
          <EventFilter value={values.action} actions={options.actions} onChange={(v) => set("action", v)} />
          <DateFilter id="audit-from" label="From" value={values.from} onApply={(v) => set("from", v, true)} />
          <DateFilter id="audit-to" label="To" value={values.to} onApply={(v) => set("to", v, true)} />
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="audit-target-type">Target type</Label>
            <Select id="audit-target-type" value={values.target_type} onChange={(e) => set("target_type", e.target.value)}>
              <option value="">Any</option>
              {options.target_types.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              {values.target_type && !options.target_types.some((o) => o.value === values.target_type) && <option value={values.target_type}>{values.target_type}</option>}
            </Select>
          </div>
        </div>
        <div className="flex flex-wrap items-start gap-3">
          <IdFilter id="audit-target" label="Target ID" help="The record or row an event wrote." value={values.target} onApply={(v) => set("target", v, true)} />
          <IdFilter id="audit-related" label="Parent record ID" help="A record's instances, reviews, confirmations or cycles." value={values.related_target} onApply={(v) => set("related_target", v, true)} />
          <IdFilter id="audit-request" label="Request ID" help="Every event of one request, job or command." value={values.request_id} onApply={(v) => set("request_id", v, true)} />
          {filtered && <Button type="button" variant="outline" className="mt-6" onClick={() => setParams(new URLSearchParams())}>Clear filters</Button>}
        </div>
      </form>
      <LogResults key={path} path={path} filtered={filtered} onOptions={setOptions} />
    </>
  );
}

/** Export the log as it is filtered: CSV for every Admin; JSON lines for a SIEM with the
 *  `audit.export` capability (hidden otherwise). The file is fetched, as the record exports are, so a refusal can be
 *  shown here: more than 100,000 matching events is a 400 `/problems/too-many-events` naming the count, and the user narrows the dates. */
function useAuditExport(query: string) {
  const me = useMe();
  const client = useQueryClient();
  const siem = me.capabilities.includes("audit.export");
  const [busy, setBusy] = useState<"csv" | "jsonl" | null>(null);
  const [error, setError] = useState<{ text: string; tooMany: boolean } | null>(null);
  const [notice, setNotice] = useState("");
  async function download(format: "csv" | "jsonl") {
    setBusy(format);
    setError(null);
    setNotice("");
    try {
      // The endpoints stream; the file is saved once it has all arrived.
      const name = await saveDownload(`audit-events/export.${format}${query ? `?${query}` : ""}`, `softwarden-audit-log.${format}`);
      setNotice(`Downloaded ${name}${query ? " with the current filters" : ""}. The export is recorded in the audit log.`);
      // The export's own event is now in the log.
      void client.invalidateQueries({ queryKey: apiKey("audit-events") });
    } catch (e) {
      const refused = e instanceof DownloadError ? e : null;
      setError({ text: refused?.message ?? "The export failed.", tooMany: refused?.status === 400 && refused.body.type === "/problems/too-many-events" });
    } finally {
      setBusy(null);
    }
  }
  const narrow = () => document.getElementById("audit-from")?.focus();
  return {
    buttons: <>
      <Button variant="outline" disabled={busy !== null} onClick={() => void download("csv")} aria-describedby="audit-export-help">
        {busy === "csv" ? "Preparing CSV…" : "Export CSV"}
      </Button>
      {siem && (
        <Button variant="outline" disabled={busy !== null} onClick={() => void download("jsonl")} aria-describedby="audit-export-help">
          {busy === "jsonl" ? "Preparing JSON lines…" : "Export for SIEM (JSON lines)"}
        </Button>
      )}
      <p id="audit-export-help" className="basis-full text-sm text-muted-foreground sm:text-right">
        {query ? "Exports the events that match the filters below." : "Exports every event; filter below to export fewer."}
      </p>
    </>,
    messages: <>
      {error && (
        <div className="mb-4 rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3" role="alert" data-testid="audit-export-error">
          <p className="font-semibold">Nothing was downloaded. {error.text}</p>
          {error.tooMany && (
            <p className="mt-1 flex flex-wrap items-center gap-2">
              Set a shorter range with From and To (or add filters), then export again.
              <Button size="sm" variant="outline" onClick={narrow}>Choose dates</Button>
            </p>
          )}
        </div>
      )}
      {/* One status region: "Preparing the export…" while the file streams in (it is held in memory until it has all
          arrived, which takes a while for a long range), then the result. */}
      <p className="sr-only" role="status" data-testid="audit-export-status">{busy ? "Preparing the export…" : notice}</p>
      {notice && <p className="mb-4 flex items-center gap-2 rounded-md border border-success-border bg-success-bg px-3 py-2 text-success-foreground" aria-hidden="true"><span>✓</span>{notice}</p>}
    </>,
  };
}

/** A date filter, applied on Enter or when focus leaves the box (typing a date would otherwise reload the log at every
 *  keystroke, with half-typed dates). */
function DateFilter({ id, label, value, onApply }: { id: string; label: string; value: string; onApply: (v: string) => void }) {
  const [text, setText] = useState(value);
  useEffect(() => { setText(value); }, [value]);
  const apply = () => { if (text !== value) onApply(text); };
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} type="date" value={text} onChange={(e) => setText(e.target.value)} onBlur={apply}
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); apply(); } }} />
    </div>
  );
}

/** A UUID filter: applied on Enter or when focus leaves the box, and only once it is a whole ID. */
function IdFilter({ id, label, help, value, onApply }: { id: string; label: string; help: string; value: string; onApply: (v: string) => void }) {
  const [text, setText] = useState(value);
  const [error, setError] = useState("");
  useEffect(() => { setText(value); setError(""); }, [value]);
  function apply() {
    const v = text.trim();
    if (v === value) return;
    if (v && !UUID.test(v)) { setError("Enter the whole ID, for example 3f2b8c1e-9d4a-4f6b-8a2e-1c5d7e9f0a3b."); return; }
    setError("");
    onApply(v.toLowerCase());
  }
  return (
    <div className="flex w-full max-w-sm flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} value={text} spellCheck={false} autoComplete="off" className="font-mono" aria-invalid={error ? true : undefined}
        aria-describedby={`${id}-help${error ? ` ${id}-error` : ""}`}
        onChange={(e) => setText(e.target.value)} onBlur={apply} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); apply(); } }} />
      <span id={`${id}-help`} className="text-sm text-muted-foreground">{help} Press Enter to apply.</span>
      {error && <span id={`${id}-error`} className="text-sm text-error-foreground">{error}</span>}
    </div>
  );
}

const EVENT_GROUPS = [
  { value: "", label: "All events" },
  { value: "resource", label: "Record changes" },
  { value: "review", label: "Reviews" },
  { value: "relationship", label: "Relationships" },
  { value: "link", label: "Link confirmations" },
  { value: "assessment", label: "Assessments" },
  { value: "renewal", label: "Renewals" },
  { value: "renewal_cycle", label: "Renewal cycles" },
  { value: "notification", label: "Snoozes and acknowledgments" },
  { value: "digest", label: "Digest retries" },
  { value: "instance", label: "Instances" },
  { value: "person", label: "People" },
  { value: "user.login", label: "Sign-ins" },
  { value: "user.login_failed", label: "Failed sign-ins" },
  { value: "user", label: "Users and roles" },
  { value: "organization", label: "Settings" },
];

function EventFilter({ value, actions, onChange }: { value: string; actions: Option[]; onChange: (v: string) => void }) {
  const known = EVENT_GROUPS.some((g) => g.value === value) || actions.some((a) => a.value === value);
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor="audit-event">Event</Label>
      <Select id="audit-event" value={value} onChange={(e) => onChange(e.target.value)}>
        <optgroup label="Groups">
          {EVENT_GROUPS.map((g) => <option key={g.value} value={g.value}>{g.label}</option>)}
        </optgroup>
        {actions.length > 0 && (
          <optgroup label="One event">
            {/* Group values (user.login) are also actions; the group entry above covers them. */}
            {actions.filter((a) => !EVENT_GROUPS.some((g) => g.value === a.value)).map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
          </optgroup>
        )}
        {!known && <option value={value}>{value}</option>}
      </Select>
    </div>
  );
}

function LogResults({ path, filtered, onOptions }: { path: string; filtered: boolean; onOptions: (o: { actions: Option[]; target_types: Option[] }) => void }) {
  const paged = usePaged(path);
  const data = paged.first.data;
  useEffect(() => {
    if (data?.actions && data.target_types) onOptions({ actions: data.actions, target_types: data.target_types });
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps
  if (paged.first.error) return <ErrorBanner message={`${paged.first.error} Nothing was changed.`} onRetry={paged.first.reload} />;
  if (!data) return <Loading what="audit events" rows={5} />;
  if (paged.rows.length === 0) {
    return filtered
      ? <EmptyState title="No events match these filters">Change or clear the filters to see more events.</EmptyState>
      : <EmptyState title="No events yet">Changes, sign-ins and account events appear here as soon as anyone signs in or edits a record.</EmptyState>;
  }
  return (
    <>
      <EventsTable rows={paged.rows} label="Audit log table" caption={`Audit log${filtered ? ", filtered" : ""}, newest first`} showTarget
        detailHref={(e) => `/admin/audit/${e.id}`} />
      <MoreButton paged={paged} />
    </>
  );
}

// ---- one event (Admin) ------------------------------------------------------------------------------------------------

export function AuditEventPage() {
  return (
    <AdminOnly title="Audit event" what="one audit event of the organization with its before and after values">
      <AuditEventView />
    </AdminOnly>
  );
}

function AuditEventView() {
  const { eventId = "" } = useParams();
  const location = useLocation();
  // Back to the log as it was filtered when the event was opened from it.
  const search = (location.state as { search?: string } | null)?.search ?? "";
  const q = useApiQuery<AuditEventDetail>(`audit-events/${eventId}`);
  const back = <Breadcrumb trail={[{ to: `/admin/audit${search.startsWith("?") ? search : ""}`, label: "Audit log" }]} current={q.data?.label ?? "Event"} />;
  if (q.isError) {
    if (isNotFound(q.error)) return <NotFound what="audit event" back={{ to: "/admin/audit", label: "Audit log" }} trail={[{ to: "/admin/audit", label: "Audit log" }]} />;
    return <>{back}<PageHead title="Audit event" /><ErrorBanner message={problemText(q.error, "Couldn't load this event.")} onRetry={() => void q.refetch()} /></>;
  }
  if (!q.data) return <>{back}<PageHead title="Audit event" /><Loading what="the event" rows={3} /></>;
  const e = q.data;
  const child = childOf(e);
  const childHref = child && e.target ? child.tab(e.target) : null;
  return (
    <>
      {back}
      <PageHead title={e.label} docTitle={`${e.label} · Audit log`} desc={<>{e.actor?.name ?? "System"} · <time dateTime={e.at} className="tabular">{when(e.at)}</time></>} />
      <div className="grid gap-5">
        <Panel id="h-event" icon={<ScrollText className="size-4" aria-hidden="true" />} title="Event">
          <DetailList>
            <DetailRow label="When"><time dateTime={e.at} className="tabular">{when(e.at)}</time></DetailRow>
            <DetailRow label="Actor">{e.actor?.name ?? "System"}</DetailRow>
            <DetailRow label="Event">{e.label} <code className="ml-1 font-mono text-[13px] text-muted-foreground">{e.action}</code></DetailRow>
            <DetailRow label="Record">{targetLink(e.target) ?? <span className="text-muted-foreground">None: an account or settings event</span>}</DetailRow>
            {child && <DetailRow label="Concerns">{childHref ? <Link to={childHref}>{`The ${child.noun}s of ${e.target?.name ?? "the record"}`}</Link> : `A ${child.noun}`}</DetailRow>}
            <DetailRow label="Details">{e.summary || "—"}</DetailRow>
            <DetailRow label="Request">
              {e.request_id ? <>
                <code className="font-mono text-[13px] [overflow-wrap:anywhere]">{e.request_id}</code>
                <span className="block"><Link to={`/admin/audit?request_id=${e.request_id}`} className="inline-block min-h-6 leading-6">Show every event from the same request</Link></span>
              </> : <span className="text-muted-foreground">Not recorded (an event from before request IDs)</span>}
            </DetailRow>
            {e.target_id && (
              <DetailRow label="Filter the log">
                <Link to={`/admin/audit?target=${e.target_id}`} className="inline-block min-h-6 leading-6">Every event on this {child ? child.noun : "target"}</Link>
                {e.related_target_id && <span className="block"><Link to={`/admin/audit?related_target=${e.related_target_id}`} className="inline-block min-h-6 leading-6">Every event on the parts of {e.target?.name ?? "this record"}</Link></span>}
              </DetailRow>
            )}
          </DetailList>
        </Panel>
        <section aria-labelledby="h-changes">
          <h2 id="h-changes" className="mb-2 font-display text-section font-semibold">Changes</h2>
          <EventDiff detail={e} />
        </section>
      </div>
    </>
  );
}
