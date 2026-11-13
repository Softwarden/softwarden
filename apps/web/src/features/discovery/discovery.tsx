/** Discovery (ADR-007): candidates from read-only connectors to link, create or dismiss, with
 *  each connector's state (its connection's test, the queued or running run, the last run). Setting up a
 *  source, connector settings, connections and run history are their own pages (setup.tsx, connector-settings.tsx,
 *  connections.tsx, runs.tsx), loaded on first use. Nothing here writes to the source, and no decision changes a
 *  curated value: linking only records that the source object is this record. */
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, AssistantSuggestion, CandidateCount, CandidatePage, Connector, DiscoverySource, Observation, ObservationStatus, orgToday, Problem, SuggestedMatch, typeLabel, when } from "@/api";
import { canWrite, useMe } from "@/lib/session";
import { apiPath, KIND_LABEL, KIND_NOUN, type RecordKind } from "@/lib/kinds";
import { RecordLink } from "@/components/record-link";
import { attempt, EmptyState, ErrorBanner, ErrorSummary, Field, LazyPart, lazyPart, Loading, PageHead, Select, Textarea, TableRegion, useReturnFocus, useLoad } from "@/components/app";
import { PickedRecord, RecordPicker } from "@/components/record-picker";
import { chip, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { RunStatusChip, runSentence, untestedText } from "@/features/discovery/discovery-ui";
import { useInvalidate } from "@/lib/query";
import { ifMatch, isStaleWrite, useOpenedRecord } from "@/lib/if-match";
import type { MissingTarget, UndoTarget } from "@/features/discovery/reconcile";
import { acceptedText, AssistantPanel, hasAssistant, SuggestionCell, type AssistantAction } from "@/features/discovery/assistant-ui";

// The reconciliation dialogs (source differs, undo, no longer seen) load on first use.
const DriftDialog = lazyPart(() => import("@/features/discovery/reconcile").then((m) => m.DriftDialog));
const UndoDialog = lazyPart(() => import("@/features/discovery/reconcile").then((m) => m.UndoDialog));
const MissingDialog = lazyPart(() => import("@/features/discovery/reconcile").then((m) => m.MissingDialog));
// The assistant's dismiss and reject dialogs too.
const AssistantNoteDialog = lazyPart(() => import("@/features/discovery/assistant-dialogs").then((m) => m.AssistantNoteDialog));

type Filter = { id: string; label: string; count: CandidateCount };
/** The queue's views, in three groups; the status is in the URL (`?status=`), "To decide" (candidates) by default. */
const FILTER_GROUPS: { label: string; filters: Filter[] }[] = [
  { label: "To decide", filters: [
    { id: "candidates", label: "To decide", count: "candidates" },
    { id: "new", label: "New", count: "new" },
    { id: "changed", label: "Changed since dismissed", count: "changed" },
  ] },
  { label: "Decided", filters: [
    { id: "linked", label: "Linked", count: "linked" },
    { id: "differs", label: "Source differs", count: "differs" },
    { id: "missing", label: "No longer seen in source", count: "missing" },
    { id: "dismissed", label: "Dismissed", count: "dismissed" },
  ] },
  { label: "Not in the queue", filters: [
    { id: "out_of_scope", label: "Out of scope", count: "out_of_scope" },
    { id: "gone", label: "Gone before a decision", count: "gone" },
  ] },
];
const FILTERS = FILTER_GROUPS.flatMap((g) => g.filters);
/** Each status as a chip: a glyph and its words. */
const STATUS: Record<ObservationStatus, { text: string; glyph: string; state: "neutral" | "pending" | "confirmed" }> = {
  new: { text: "New", glyph: "○", state: "neutral" },
  changed: { text: "Changed since dismissed", glyph: "◐", state: "pending" },
  linked: { text: "Linked", glyph: "✓", state: "confirmed" },
  dismissed: { text: "Dismissed", glyph: "⊘", state: "neutral" },
  gone: { text: "Gone before a decision", glyph: "○", state: "neutral" },
  out_of_scope: { text: "Out of scope", glyph: "⊘", state: "neutral" },
};
const EMPTY_TEXT: Record<string, string> = {
  differs: "No linked record differs from what its source says, or every difference was kept.",
  missing: "Every linked record is still listed by its source, or someone kept or archived it.",
  out_of_scope: "No source object is left out by a connector's scope.",
};

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const AGENT_TEXT: Record<string, string> = { copilot_studio: "Copilot Studio", agent_identity: "Entra agent identity" };
const DETAIL_TEXT: Record<string, string> = {
  resource_id: "Resource ID", subscription: "Subscription", location: "Location", state: "State", site_kind: "Kind", tags: "Tags",
  environment: "Environment", authentication: "Authentication", halo_asset_id: "Halo asset ID", asset_type: "Asset type",
};
const detailValue = (v: unknown) => (Array.isArray(v) ? v.join(", ") : v && typeof v === "object" ? Object.entries(v).map(([k, x]) => `${k}: ${String(x)}`).join(", ") : String(v ?? ""));

/** What a candidate would become: Application, Component (its type) or AI agent (its platform). */
export function ObservationKindChip({ o }: { o: Observation }) {
  const kind = o.kind === "component" || o.kind === "ai_agent" ? o.kind : "application";
  const extra = kind === "component" && o.component_type ? typeLabel(o.component_type) : kind === "ai_agent" && o.agent ? AGENT_TEXT[o.agent] : "";
  return <span className={chip({ state: "neutral" })} data-kind={kind}>{KIND_LABEL[kind]}{extra && `: ${extra}`}</span>;
}

/** The credentials seen on the app, live ones first, each with its end date in words. */
function Credentials({ o, today }: { o: Observation; today: string }) {
  const list = o.credentials ?? [];
  if (!list.length) return null;
  return (
    <ul className="mt-1 text-sm font-normal">
      {list.map((c) => {
        const kind = c.credential_type === "certificate" ? "Certificate" : "Client secret";
        const end = c.expires_on;
        const days = end ? Math.round((Date.parse(end) - Date.parse(today)) / 86_400_000) : null;
        return (
          <li key={c.id} className="text-muted-foreground">
            {kind}{c.display_name && <> “{c.display_name}”</>}:{" "}
            {c.removed_at ? <>removed from the source {when(c.removed_at)}</>
              : end === null ? "no end date"
              : days! < 0 ? <span className="font-semibold text-error-foreground"><span aria-hidden="true">! </span>expired {end}</span>
              : <>ends <time dateTime={end} className="tabular">{end}</time> ({days === 0 ? "today" : `in ${plural(days!, "day")}`})</>}
          </li>
        );
      })}
    </ul>
  );
}

const score = (m: SuggestedMatch) => `${Math.round(m.score * 100)}% name match`;

/** One connector's status: the run in progress or the last run in words, a failure as an alert, a connection that
 *  needs a test, and Run now (not while a run is queued or running) and Settings for Admins. */
function ConnectorStatus({ c, onRun }: { c: Connector; onRun: () => void }) {
  const me = useMe();
  const admin = me.role === "admin";
  const failed = !c.active_run && c.last_run?.status === "failed";
  const untested = c.connection && !c.connection.test_current;
  return (
    <div className={`mb-4 rounded-lg border bg-card px-4 py-3 ${failed ? "border-l-4 border-error-border" : ""}`} data-testid={`connector-${c.name}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h2 className="font-display text-section font-semibold">{c.name}</h2>
        <span className="text-sm text-muted-foreground">{[c.name !== c.source_label && c.source_label, c.simulated && "Simulated tenant (synthetic data)"].filter(Boolean).join(" · ")}</span>
        {!c.available && <span className={chip({ state: "neutral" })} data-testid="connector-unavailable"><span aria-hidden="true">⊘</span>Unavailable</span>}
        {!c.enabled && <span className={chip({ state: "neutral" })}>Turned off</span>}
        {c.active_run && <RunStatusChip status={c.active_run.status} />}
        <span className="ml-auto flex gap-2">
          {admin && c.available && (
            <Button size="sm" variant="outline" onClick={onRun} disabled={!!c.active_run || !c.enabled}>
              {c.active_run ? (c.active_run.status === "queued" ? "Run queued" : "Running") : "Run now"}<span className="sr-only"> {c.name}</span>
            </Button>
          )}
          <Button asChild size="sm" variant="outline"><Link to={`/discovery/connectors/${c.id}`}>{admin ? "Settings" : "Details"}<span className="sr-only"> for {c.name}</span></Link></Button>
        </span>
      </div>
      <p className={`mt-1 text-sm ${failed ? "text-error-foreground" : "text-muted-foreground"}`} role={failed ? "alert" : undefined}>
        {c.active_run ? runSentence(c.active_run) : c.last_run ? runSentence(c.last_run) : "Not run yet."}
      </p>
      {!c.available && (
        <p className="mt-1 text-sm text-muted-foreground"><span aria-hidden="true">⊘ </span>{c.source_label} isn't available here, so this connector doesn't run. It is kept as it is, with what it found.</p>
      )}
      {c.available && untested && (
        <p className="mt-1 text-sm"><span aria-hidden="true">◐ </span>{untestedText(c.connection!)}{admin && <> <Link className="underline" to="/discovery/connections">Open Connections</Link></>}</p>
      )}
    </div>
  );
}

type Pending =
  | { mode: "link" | "create" | "dismiss"; obs: Observation; via?: AssistantSuggestion }
  | { mode: "assistant-dismiss" | "assistant-reject"; obs: Observation; suggestion: AssistantSuggestion }
  | { mode: "drift"; obs: Observation }
  | { mode: "undo"; target: UndoTarget }
  | { mode: "acknowledge" | "archive"; target: MissingTarget };

/** A moment as the organization's date (YYYY-MM-DD), as Needs attention dates "since". */
const orgDate = (iso: string, timeZone: string) =>
  new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(iso));
const missingTarget = (o: Observation, timeZone: string): MissingTarget => ({
  observation: o.id, sourceName: o.name, connector: o.connector.name, since: o.gone_at ? orgDate(o.gone_at, timeZone) : null, record: o.decision!.record!,
});
const undoTarget = (o: Observation): UndoTarget => ({ id: o.id, name: o.name, connector: o.connector.name, decision: o.decision! });

export function DiscoveryPage() {
  const me = useMe();
  const write = canWrite(me);
  const today = orgToday(me.organization.timezone);
  const connectors = useLoad<Connector[]>("connectors");
  const [params, setParams] = useSearchParams();
  const asked = params.get("status") ?? "";
  const status = FILTERS.some((f) => f.id === asked) || asked === "all" ? asked : "candidates";
  const connector = params.get("connector") ?? "";
  const [text, setText] = useState(params.get("q") ?? "");
  const q = (params.get("q") ?? "").trim();
  // The page's position is in the URL too (`cursor`, an offset the API gives back); any other change starts again.
  const cursor = /^\d+$/.test(params.get("cursor") ?? "") ? params.get("cursor")! : "";
  function update(changes: Record<string, string>) {
    const next = new URLSearchParams(params);
    if (!("cursor" in changes)) next.delete("cursor");
    for (const [k, v] of Object.entries(changes)) { if (v) next.set(k, v); else next.delete(k); }
    setParams(next, { replace: !("cursor" in changes) });
  }
  const setCursor = (c: string) => update({ cursor: c });
  useEffect(() => {
    if (text.trim() === q) return;
    const t = setTimeout(() => update({ q: text.trim() }), 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);
  const query = new URLSearchParams({ status, ...(connector ? { connector } : {}), ...(q ? { q } : {}), ...(cursor ? { cursor } : {}) });
  const page = useLoad<CandidatePage>(`discovery/candidates?${query}`, { strict: true });
  const counts = (page.data ?? page.latest)?.counts;
  const [notice, setNotice] = useState<{ text: string; undo?: Observation; error?: boolean; focus?: boolean } | null>(null);
  // The candidate whose assistant suggestion is being accepted in one click: its Accept and Reject wait meanwhile.
  const [accepting, setAccepting] = useState<string | null>(null);
  // After a one-click Accept the row leaves the queue: focus goes to the notice's Undo, or the page heading.
  useEffect(() => {
    if (!notice?.focus) return;
    requestAnimationFrame(() => (document.getElementById("notice-undo") ?? document.querySelector<HTMLElement>("main h1"))?.focus());
  }, [notice]);
  const assistant = hasAssistant(me);
  const [dialog, setDialog] = useState<Pending | null>(null);
  const invalidate = useInvalidate();

  async function runNow(c: Connector) {
    const failed = await attempt(() => api(`connectors/${c.id}/run`, { method: "POST" }));
    setNotice({ text: failed ? `Not started: ${failed.detail}` : `Run queued for ${c.name}. The worker starts it within a minute; reload to see the result.` });
    connectors.reload();
  }
  function refresh() {
    page.reload();
    connectors.reload();
    void invalidate("needs-attention");
  }
  function decided(text: string, obs: Observation) {
    setDialog(null);
    setNotice({ text, undo: obs });
    refresh();
  }
  function done(text: string) {
    setDialog(null);
    setNotice({ text });
    refresh();
  }
  // The assistant's suggestion: accepting a link is one call; a create opens the create form prefilled,
  // a dismiss its optional reason; reject takes an optional note.
  async function onAssistant(a: AssistantAction) {
    if (a.kind === "reject") return setDialog({ mode: "assistant-reject", obs: a.obs, suggestion: a.suggestion });
    if (a.suggestion.action === "create") return setDialog({ mode: "create", obs: a.obs, via: a.suggestion });
    if (a.suggestion.action === "dismiss") return setDialog({ mode: "assistant-dismiss", obs: a.obs, suggestion: a.suggestion });
    if (accepting) return;
    setAccepting(a.obs.id);
    try {
      const r = await api<Observation>(`discovery/observations/${a.obs.id}/assistant/accept`, { method: "POST", body: { suggestion: a.suggestion.id } });
      setNotice({ text: acceptedText(a.obs.name, r), undo: r, focus: true });
    } catch (e) {
      setNotice({ text: `Not accepted: ${e instanceof Problem ? e.detail : "something went wrong. Try again."}`, error: true });
    } finally {
      setAccepting(null);
    }
    refresh();
  }
  // Undo of the decision just made, from the notice: it only removes the decision (a created record stays).
  async function undo(obs: Observation) {
    const failed = await attempt(() => api(`discovery/observations/${obs.id}/reopen`, { method: "POST", body: {} }));
    const kept = obs.decision?.decision === "created" && obs.decision.record ? ` ${obs.decision.record.name} stays; archive it from its page if it isn't needed.` : "";
    setNotice({ text: failed ? failed.detail : `Undone. ${obs.name} is a candidate again.${kept}` });
    refresh();
  }

  const list = connectors.data;
  const current = FILTERS.find((f) => f.id === status);
  return (
    <>
      <PageHead title="Discovery" desc="What read-only sources have seen, to link to a record, add as a new record, or dismiss, and where a linked record differs from its source or is no longer listed. Nothing is written back to the source, and nothing changes a record unless you apply it.">
        {me.role === "admin" && list && list.length > 0 && <Button asChild variant="outline"><Link to="/discovery/setup">Add a source</Link></Button>}
        {me.role === "admin" && <Button asChild variant="outline"><Link to="/discovery/connections">Connections</Link></Button>}
        <Button asChild variant="outline"><Link to="/discovery/runs">Run history</Link></Button>
      </PageHead>
      {connectors.error && <ErrorBanner message={`Couldn't load connectors. ${connectors.error}`} onRetry={connectors.reload} />}
      {!list && !connectors.error && <Loading what="discovery" rows={3} />}
      {list && list.length === 0 && (
        <EmptyState title="No discovery source is set up"
          action={me.role === "admin" ? <Button asChild><Link to="/discovery/setup">Set up a discovery source</Link></Button> : undefined}>
          {me.role === "admin"
            ? `Connect Microsoft Entra ID (or another source) with the guided setup to list its applications here as candidates.${me.simulation_allowed ? " Until you connect a real tenant, use the simulated tenant." : ""}`
            : "An Admin can connect Microsoft Entra ID or another source. Its applications then appear here as candidates."}
        </EmptyState>
      )}
      <div className={notice ? `mb-4 flex flex-wrap items-center gap-3 rounded-md border px-3 py-2 ${notice.error ? "border-l-4 border-error-border bg-error-bg" : "border-success-border bg-success-bg text-success-foreground"}` : ""}>
        <span role="status">{notice ? `${notice.error ? "!" : "✓"} ${notice.text}` : ""}</span>
        {notice?.undo && write && <Button id="notice-undo" size="sm" variant="outline" onClick={() => void undo(notice.undo!)}>Undo</Button>}
      </div>
      {list && list.length > 0 && (
        <>
          {list.map((c) => <ConnectorStatus key={c.id} c={c} onRun={() => void runNow(c)} />)}
          {assistant && <AssistantPanel me={me} connector={list.find((c) => c.id === connector) ?? null} onFinished={refresh} />}
          {!write && <p className="mb-3 text-sm text-muted-foreground">You can read the queue. Editors and Admins link, create and dismiss, apply a source's values, and keep or archive records a source no longer lists.</p>}
          <div className="mb-3 flex flex-col gap-2">
            {FILTER_GROUPS.map((g) => (
              <div key={g.label} className="flex flex-wrap items-center gap-2" role="group" aria-label={`Show: ${g.label}`}>
                <span className="w-32 text-xs font-semibold text-muted-foreground" aria-hidden="true">{g.label}</span>
                {g.filters.map((f) => (
                  <button key={f.id} type="button" aria-pressed={status === f.id} onClick={() => update({ status: f.id === "candidates" ? "" : f.id })}
                    className="h-8 rounded-md border border-border-strong bg-card px-3 text-[13px] text-muted-foreground aria-pressed:border-accent-line aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground">
                    {f.label}{counts && <span className="tabular"> {counts[f.count] ?? 0}</span>}
                  </button>
                ))}
              </div>
            ))}
          </div>
          <div className="mb-4 flex flex-wrap items-end gap-x-6 gap-y-3">
            <form role="search" className="flex w-full max-w-sm flex-col gap-1.5" onSubmit={(e) => { e.preventDefault(); update({ q: text.trim() }); }}>
              <Label htmlFor="discovery-search">Search source names</Label>
              <Input id="discovery-search" type="search" value={text} onChange={(e) => setText(e.target.value)} />
            </form>
            {list.length > 1 && (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="connector-filter">Connector</Label>
                <Select id="connector-filter" value={connector} onChange={(e) => update({ connector: e.target.value })}>
                  <option value="">All</option>
                  {list.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </Select>
              </div>
            )}
          </div>
          {page.error && <ErrorBanner message={`Couldn't load candidates. ${page.error}`} onRetry={page.reload} />}
          {!page.data && !page.error && <Loading what="candidates" rows={5} />}
          {page.data && page.data.results.length === 0 && (
            <EmptyState title={status === "candidates" ? "Nothing to decide" : "Nothing here"}
              action={q || connector ? <Button variant="outline" onClick={() => { setText(""); update({ q: "", connector: "" }); }}>Clear search and connector</Button> : undefined}>
              {q ? `Nothing ${status === "candidates" ? "to decide " : ""}matches "${q}".`
                : status === "candidates" ? "Every application the source reported is linked or dismissed. A new or changed one appears here after the next run."
                : EMPTY_TEXT[status] ?? "No source objects have this status."}
            </EmptyState>
          )}
          {page.data && page.data.results.length > 0 && (
            <>
              <TableRegion label="Discovery candidates table">
                <Table>
                  <TableCaption className="sr-only">Source objects{current ? `: ${current.label}` : ""}, by name</TableCaption>
                  <TableHeader><TableRow>
                    <TableHead scope="col">Source object</TableHead><TableHead scope="col">Status</TableHead>
                    <TableHead scope="col">Suggested owners</TableHead><TableHead scope="col">Similar records</TableHead>
                    {assistant && <TableHead scope="col">Assistant's suggestion</TableHead>}
                    <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
                  </TableRow></TableHeader>
                  <TableBody>
                    {page.data.results.map((o) => <CandidateRow key={o.id} o={o} write={write} today={today} timeZone={me.organization.timezone} open={setDialog} assistant={assistant ? (a) => void onAssistant(a) : null} accepting={accepting} note={(text, error) => setNotice({ text, error })} />)}
                  </TableBody>
                </Table>
              </TableRegion>
              <div className="mt-3 flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
                <span>Showing {plural(page.data.results.length, "source object")}{cursor ? ` from number ${Number(cursor) + 1}` : ""}.</span>
                {cursor && <Button size="sm" variant="outline" onClick={() => setCursor(String(Math.max(Number(cursor) - 50, 0) || ""))}>Previous page</Button>}
                {page.data.next && <Button size="sm" variant="outline" onClick={() => setCursor(page.data!.next!)}>Next page</Button>}
              </div>
            </>
          )}
        </>
      )}
      {dialog && (dialog.mode === "link" || dialog.mode === "create" || dialog.mode === "dismiss") && <DecisionDialog mode={dialog.mode} obs={dialog.obs} via={dialog.via} onClose={() => setDialog(null)} onDone={decided} />}
      {(dialog?.mode === "assistant-dismiss" || dialog?.mode === "assistant-reject") && (
        <LazyPart what="the assistant's dialog">
          <AssistantNoteDialog mode={dialog.mode === "assistant-dismiss" ? "dismiss" : "reject"} obs={dialog.obs} suggestion={dialog.suggestion} onClose={() => setDialog(null)}
            onDone={(text, obs) => (obs ? decided(text, obs) : done(text))} />
        </LazyPart>
      )}
      {dialog?.mode === "drift" && <LazyPart what="the differences"><DriftDialog observation={dialog.obs.id} write={write} onClose={() => setDialog(null)} onDone={done} /></LazyPart>}
      {dialog?.mode === "undo" && <LazyPart what="the undo dialog"><UndoDialog target={dialog.target} onClose={() => setDialog(null)} onDone={done} /></LazyPart>}
      {(dialog?.mode === "acknowledge" || dialog?.mode === "archive") && <LazyPart what="the dialog"><MissingDialog mode={dialog.mode} target={dialog.target} onClose={() => setDialog(null)} onDone={done} /></LazyPart>}
    </>
  );
}

/** A source object's Entra app ID, shortened, with the whole ID for screen readers and on hover, and
 *  Copy. The result is said in the page's status line. */
function AppId({ id, name, note }: { id: string; name: string; note: (text: string, error?: boolean) => void }) {
  async function copy() {
    try { await navigator.clipboard.writeText(id); note(`App ID of ${name} copied.`); } catch { note(`Couldn't copy. The app ID of ${name} is ${id}.`, true); }
  }
  return (
    <span className="flex flex-wrap items-center gap-2 text-sm font-normal text-muted-foreground" data-testid="app-id">
      <span>App ID <code className="font-mono text-xs" title={id}>{id.slice(0, 8)}…</code><span className="sr-only"> (full ID {id})</span></span>
      <Button size="xs" variant="outline" onClick={() => void copy()}>Copy<span className="sr-only"> the app ID of {name}</span></Button>
    </span>
  );
}

/** One source object: what it is (kind, publisher, IDs, source details, credentials), its status with why, suggested
 *  owners and similar records with their scores, and the next steps the role may take. */
function CandidateRow({ o, write, today, timeZone, open, assistant, accepting, note }: { o: Observation; write: boolean; today: string; timeZone: string; open: (p: Pending) => void; assistant: ((a: AssistantAction) => void) | null; accepting: string | null; note: (text: string, error?: boolean) => void }) {
  const st = STATUS[o.status] ?? STATUS.new;
  const differs = (o.differences ?? []).filter((d) => !d.ignored);
  const record = o.decision?.record ?? null;
  const details = Object.entries(o.details ?? {}).filter(([, v]) => v !== "" && v !== null && !(Array.isArray(v) && !v.length));
  const undecided = o.status === "new" || o.status === "changed";
  return (
    <TableRow className="align-top" data-observation={o.id}>
      <TableHead scope="row" className="h-auto min-w-48 max-w-sm whitespace-normal py-2 font-semibold text-foreground [overflow-wrap:anywhere]">
        {o.name}
        <span className="mt-1 flex flex-wrap gap-1.5 font-normal"><ObservationKindChip o={o} /></span>
        <span className="block text-sm font-normal text-muted-foreground">
          {o.connector.name} · {o.publisher || "Unknown publisher"}{!o.enabled && " · disabled in source"}</span>
        {o.app_id && <AppId id={o.app_id} name={o.name} note={note} />}
        {o.description && <span className="block text-sm font-normal text-muted-foreground">{o.description}</span>}
        {details.length > 0 && (
          <span className="block text-sm font-normal text-muted-foreground">{details.map(([k, v]) => `${DETAIL_TEXT[k] ?? k.replace(/_/g, " ")}: ${detailValue(v)}`).join(" · ")}</span>
        )}
        <Credentials o={o} today={today} />
      </TableHead>
      <TableCell className="whitespace-normal">
        <span className={chip({ state: st.state })}><span aria-hidden="true">{st.glyph}</span>{st.text}</span>
        {record && <span className="block text-sm">{o.decision?.decision === "created" ? "created" : "to"} <RecordLink record={record} /></span>}
        {o.decision?.decision === "dismissed" && <span className="block max-w-xs text-sm text-muted-foreground [overflow-wrap:anywhere]">“{o.decision.reason}”</span>}
        {o.decision && <span className="block text-sm text-muted-foreground">{o.decision.decided_by?.name ?? "Someone"}, {when(o.decision.decided_at)}</span>}
        {o.status === "changed" && <span className="block text-sm text-muted-foreground">Name, state or owners changed in the source since it was dismissed.</span>}
        {o.status === "out_of_scope" && (
          <span className="block max-w-xs text-sm text-muted-foreground" data-testid="out-of-scope-why">
            Left out by {o.connector.name}'s scope: {o.out_of_scope_label || o.out_of_scope}{o.out_of_scope_at ? `, since ${when(o.out_of_scope_at)}` : ""}. The source still lists it, so it keeps its state and is never “no longer seen”.
          </span>
        )}
        {o.status === "gone" && o.gone_at && <span className="block text-sm text-muted-foreground">No longer listed since {when(o.gone_at)}.</span>}
        {differs.length > 0 && (
          <span className="mt-1 block"><span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Source differs</span>
            <span className="block text-sm text-muted-foreground">{differs.map((d) => d.label).join(", ")}</span></span>
        )}
        {o.missing && (
          <span className="mt-1 block"><span className={chip({ state: "pending" })}><span aria-hidden="true">!</span>No longer seen in source</span>
            <span className="block text-sm text-muted-foreground">Since {when(o.gone_at)}</span></span>
        )}
        {o.decision?.gone_resolution && o.gone_at && (
          <span className="block text-sm text-muted-foreground">
            No longer seen since {when(o.gone_at)}: {o.decision.gone_resolution === "kept" ? "record kept" : "record archived"} by {o.decision.gone_acknowledged_by?.name ?? "someone"}{o.decision.gone_note ? ` (“${o.decision.gone_note}”)` : ""}
          </span>
        )}
      </TableCell>
      <TableCell className="whitespace-normal">
        {o.suggested_owners.length === 0 ? <span className="text-muted-foreground">None in source</span> : (
          <ul className="text-sm">{o.suggested_owners.map((w, i) => (
            <li key={w.email || w.external_id || `${w.name}-${i}`}>
              {w.resolved === false ? <span className={chip({ state: "gap" })}><span aria-hidden="true">⚠</span>Owner not resolved</span>
                : w.person ? <>{w.person.name}{!w.person.active && " (inactive)"}</> : <>{w.name} <span className="text-muted-foreground">(no Person with this email)</span></>}
            </li>
          ))}</ul>
        )}
      </TableCell>
      <TableCell className="whitespace-normal">
        {o.suggested_matches.length === 0 ? <span className="text-muted-foreground">—</span> : (
          <ul className="text-sm">{o.suggested_matches.map((m) => (
            <li key={m.id}><RecordLink record={m} className="inline-block min-h-6 leading-6" />
              <span className="text-muted-foreground">{m.kind !== "application" && ` (${KIND_NOUN[m.kind]?.[0] ?? m.kind})`}, <span className="tabular">{score(m)}</span></span></li>
          ))}</ul>
        )}
      </TableCell>
      {assistant && <TableCell className="whitespace-normal"><SuggestionCell o={o} write={write} onAction={assistant} busy={accepting === o.id} /></TableCell>}
      <TableCell className="text-right">
        <span className="inline-flex flex-wrap justify-end gap-2 xl:flex-nowrap" data-testid="candidate-actions">
          {write && undecided && <>
            <Button size="sm" variant="queue" onClick={() => open({ mode: "link", obs: o })}>Link to record<span className="sr-only">: {o.name}</span></Button>
            <Button size="sm" variant="outline" onClick={() => open({ mode: "create", obs: o })}>Create record<span className="sr-only"> from {o.name}</span></Button>
            <Button size="sm" variant="outline" onClick={() => open({ mode: "dismiss", obs: o })}>Dismiss<span className="sr-only"> {o.name}</span></Button>
          </>}
          {(o.differences ?? []).length > 0 && (
            <Button size="sm" variant={write ? "queue" : "outline"} onClick={() => open({ mode: "drift", obs: o })}>{write ? "Review differences" : "View differences"}<span className="sr-only"> for {o.name}</span></Button>
          )}
          {write && o.missing && record && <>
            <Button size="sm" variant="queue" onClick={() => open({ mode: "acknowledge", target: missingTarget(o, timeZone) })}>Keep record<span className="sr-only"> {record.name}</span></Button>
            <Button size="sm" variant="outline" onClick={() => open({ mode: "archive", target: missingTarget(o, timeZone) })}>Archive record<span className="sr-only"> {record.name}</span></Button>
          </>}
          {write && o.decision && o.status !== "changed" && (
            <Button size="sm" variant="outline" onClick={() => open({ mode: "undo", target: undoTarget(o) })}>Undo decision<span className="sr-only"> on {o.name}</span></Button>
          )}
        </span>
      </TableCell>
    </TableRow>
  );
}

const TITLES = { link: "Link to an existing record", create: "Create a record from this application", dismiss: "Dismiss this candidate" };
const CREATE_KINDS: RecordKind[] = ["application", "component", "ai_agent"];
const defaultKind = (o: Observation): RecordKind => (o.kind === "component" || o.kind === "ai_agent" ? o.kind : "application");

/** Link, create or dismiss by hand; with `via` (create only), the create form accepting the assistant's suggestion. Also
 *  used by the shadow IT page (shadow-it.tsx) for an app's candidate. */
export function DecisionDialog({ mode, obs, via, onClose, onDone }: { mode: "link" | "create" | "dismiss"; obs: Observation; via?: AssistantSuggestion; onClose: () => void; onDone: (text: string, obs: Observation) => void }) {
  const [picked, setPicked] = useState<PickedRecord | null>(null);
  const [name, setName] = useState(obs.name.slice(0, 200));
  const [kind, setKind] = useState<RecordKind>(defaultKind(obs));
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);
  const [more, setMore] = useState<SuggestedMatch[] | null>(null);
  const [moreError, setMoreError] = useState("");
  const focus = useReturnFocus(true);
  // A dismissal reads the candidate with its ETag as the dialog opens and sends it as If-Match.
  const opened = useOpenedRecord(mode === "dismiss" ? `discovery/observations/${obs.id}` : null);
  const matches = more ?? obs.suggested_matches;
  async function showMore() {
    setMoreError("");
    try {
      setMore((await api<{ results: SuggestedMatch[] }>(`discovery/observations/${obs.id}/suggestions?limit=10`)).results);
    } catch (e) {
      setMoreError(e instanceof Problem ? e.detail : "Couldn't load more suggestions.");
    }
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setErrors({});
    setDetail("");
    try {
      if (mode === "link") {
        if (!picked) { setErrors({ resource: ["Choose the record this application is."] }); return; }
        const r = await api<Observation>(`discovery/observations/${obs.id}/link`, { method: "POST", body: { resource: picked.id } });
        onDone(`Linked ${obs.name} to ${picked.name}. Nothing on ${picked.name} changed.`, r);
      } else if (mode === "create") {
        const r = via
          ? await api<Observation>(`discovery/observations/${obs.id}/assistant/accept`, { method: "POST", body: { suggestion: via.id, name, kind } })
          : await api<Observation>(`discovery/observations/${obs.id}/create`, { method: "POST", body: { name, kind } });
        onDone(via ? acceptedText(obs.name, r) : `Created ${r.decision?.record?.name ?? name} and linked it. It has no owners yet, so it is in Needs attention.`, r);
      } else {
        const r = await api<Observation>(`discovery/observations/${obs.id}/dismiss`, { method: "POST", body: { reason }, headers: ifMatch(await opened.etag()) });
        onDone(`Dismissed ${obs.name}. It comes back only if its name, state or owners change in the source.`, r);
      }
    } catch (err) {
      if (isStaleWrite(err)) setDetail("This candidate changed after you opened this (a run or someone else updated it), so it wasn't dismissed. Close this and check it, then try again.");
      else if (err instanceof Problem) { setErrors(err.errors); setDetail(Object.keys(err.errors).length ? "" : err.detail); }
      else setDetail("Something went wrong. Try again.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...focus}>
        <DialogHeader>
          <DialogTitle className="leading-snug [overflow-wrap:anywhere]">{via ? "Accept: create a record from this application" : TITLES[mode]}</DialogTitle>
          <DialogDescription>
            {mode === "link" && `Record that ${obs.name} in ${obs.connector.name} is an existing application, component or AI agent. Several source objects can link to one record. Nothing on the record changes.`}
            {mode === "create" && `A new record named after ${obs.name}, Active, with its description and homepage. Suggested owners aren't applied; assign owners on the record.${via ? " The decision is recorded as accepting the assistant's suggestion." : ""}`}
            {mode === "dismiss" && `${obs.name} stays out of the candidates unless its name, state or owners change in the source.`}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate className="grid gap-2">
          <ErrorSummary errors={errors} />
          {detail && <p role="alert" className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2">{detail}</p>}
          {via?.reason && <p className="rounded-md border bg-muted px-3 py-2 text-sm [overflow-wrap:anywhere]"><span className="font-semibold">Assistant's reason: </span><span>{via.reason}</span></p>}
          {mode === "link" && (
            <>
              {matches.length > 0 && (
                <fieldset className="mb-2">
                  <legend className="text-sm font-semibold">Similar names, best first</legend>
                  <div className="mt-1 flex flex-wrap gap-2">
                    {matches.map((m) => (
                      <Button key={m.id} type="button" size="sm" variant="outline" aria-pressed={picked?.id === m.id} onClick={() => setPicked({ id: m.id, name: m.name, kind: m.kind })}>
                        {`${m.name}${m.kind !== "application" ? ` (${KIND_NOUN[m.kind]?.[0] ?? m.kind})` : ""}, ${Math.round(m.score * 100)}%`}
                      </Button>
                    ))}
                  </div>
                </fieldset>
              )}
              {more === null && (
                <div><Button type="button" size="sm" variant="link" className="px-0" onClick={() => void showMore()}>Show more suggestions</Button></div>
              )}
              {more !== null && more.length === 0 && <p className="text-sm text-muted-foreground">No record has a similar name. Search for it below.</p>}
              {moreError && <p role="alert" className="text-sm text-error-foreground">{moreError}</p>}
              <Field id="resource" label="Record" errors={errors} help="Or search any application, component or AI agent by name.">
                <RecordPicker id="resource" value={picked} onChange={setPicked} kinds={["application", "component", "ai_agent"]} listLabel="Applications, components and AI agents" />
              </Field>
            </>
          )}
          {mode === "create" && (
            <>
              <Field id="name" label="Name" errors={errors}><Input id="name" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} /></Field>
              <Field id="kind" label="Kind" errors={errors} help={obs.kind === "ai_agent" ? "The source reports an AI agent." : obs.kind === "component" ? "The source reports a component." : undefined}>
                <Select id="kind" value={kind} onChange={(e) => setKind(e.target.value as RecordKind)}>
                  {CREATE_KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
                </Select>
              </Field>
            </>
          )}
          {mode === "dismiss" && (
            <Field id="reason" label="Why dismiss it? (required)" errors={errors} help="Saved with the decision and the audit history, for example: a developer's test registration.">
              <Textarea id="reason" value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
            </Field>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={busy}>{busy ? "Saving…" : mode === "link" ? "Link" : mode === "create" ? (via ? "Accept: create and link" : "Create and link") : "Dismiss"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---- A record's discovery sources -----------------------------------------------------------------------------------

/** Source objects linked to this record. Shown only when there are some. "Source differs" lists each field with the
 *  record's value and the source's; nothing is copied unless someone applies it (Review differences, an audited edit).
 *  Unlink undoes the decision after a confirmation; the observation becomes a candidate again. */
export function DiscoverySources({ id, kind, name, write }: { id: string; kind: RecordKind; name: string; write: boolean }) {
  const load = useLoad<DiscoverySource[]>(`${apiPath(kind)}/${id}/discovery-sources`);
  const [notice, setNotice] = useState("");
  const [dialog, setDialog] = useState<{ mode: "drift"; observation: string } | { mode: "undo"; target: UndoTarget } | null>(null);
  const invalidate = useInvalidate();
  if (!load.data || load.data.length === 0) return notice ? <SuccessNote text={notice} /> : null;
  function done(text: string) {
    setDialog(null);
    setNotice(text);
    // The record itself may have changed (an applied value), and its history with it.
    void invalidate(`${apiPath(kind)}/${id}`, "needs-attention");
    load.reload();
  }
  return (
    <section className="overflow-hidden rounded-lg border bg-card" aria-labelledby="h-sources">
      <div className="flex items-center gap-2.5 border-b px-4 py-2.5">
        <h2 id="h-sources" className="font-display text-section font-semibold">Discovery sources</h2>
        <span className="ml-auto text-xs text-muted-foreground">Read-only; copied onto this record only when someone applies a value</span>
      </div>
      {notice && <div className="px-4 pt-3"><SuccessNote text={notice} /></div>}
      <ul>
        {load.data.map((s) => {
          const all = s.differences ?? [];
          const open = all.filter((d) => !d.ignored);
          return (
            <li key={s.observation} className="border-b border-border-subtle px-4 py-3 last:border-0" data-source={s.observation}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold">{s.source_values.name}</span>
                <span className="text-sm text-muted-foreground">{s.connector.name !== s.connector.source_label ? `${s.connector.name} (${s.connector.source_label})` : s.connector.source_label}{s.app_id && <> · App ID <span className="font-mono text-xs">{s.app_id}</span></>}</span>
                {!s.enabled && <span className={chip({ state: "neutral" })}>Disabled in source</span>}
                {s.gone_at && <span className={chip({ state: "pending" })}><span aria-hidden="true">!</span>No longer seen in source since {when(s.gone_at)}</span>}
                <span className="ml-auto inline-flex flex-wrap gap-2">
                  {all.length > 0 && <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "drift", observation: s.observation })}>{write ? "Review differences" : "View differences"}<span className="sr-only"> from {s.source_values.name}</span></Button>}
                  {write && (
                    <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "undo", target: {
                      id: s.observation, name: s.source_values.name, connector: s.connector.name,
                      decision: { decision: s.decision, reason: "", record: { id, kind, name } },
                    } })}>Unlink…<span className="sr-only"> {s.source_values.name}</span></Button>
                  )}
                </span>
              </div>
              {open.length > 0 && (
                <div className="mt-2">
                  <p className="text-sm"><span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Source differs</span>{" "}
                    {s.connector.name} says {open.length === 1 ? "one field" : `${open.length} fields`} differently. This record keeps its values unless you apply the source's.</p>
                  <dl className="mt-1 grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[max-content_1fr]">
                    {open.map((d) => (
                      <div key={d.field} className="contents">
                        <dt className="font-medium">{d.label}</dt>
                        <dd className="[overflow-wrap:anywhere]">“{d.source}” in the source since {when(d.since)}; {d.curated ? `“${d.curated}”` : "not set"} here</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}
              <p className="mt-1 text-sm text-muted-foreground">{s.decision === "created" ? "Created from this source" : "Linked"} by {s.decided_by?.name ?? "someone"} on {when(s.decided_at)} · last seen {when(s.last_seen_at)}</p>
            </li>
          );
        })}
      </ul>
      {dialog?.mode === "drift" && <LazyPart what="the differences"><DriftDialog observation={dialog.observation} write={write} onClose={() => setDialog(null)} onDone={done} /></LazyPart>}
      {dialog?.mode === "undo" && <LazyPart what="the undo dialog"><UndoDialog target={dialog.target} onClose={() => setDialog(null)} onDone={done} /></LazyPart>}
    </section>
  );
}
