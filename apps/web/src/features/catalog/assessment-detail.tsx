import { useState } from "react";
import { uuidOrEmpty } from "@/lib/safe-path";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import {
  api, apiWithEtag, ASSESSMENT_STATUS_LABEL, ASSESSMENT_STATUSES, AssessmentDetail as Assessment, AssessmentRow, AssessmentStatus, cap, Problem, retirementCheck,
  RetirementCheck, SnapshotFull, SnapshotLinkRow, SnapshotNode, SnapshotOwner, SnapshotSummary, UserRef,
} from "@/api";
import { canWrite, useMe } from "@/lib/session";
import { KIND_LABEL } from "@/lib/kinds";
import { RecordLink } from "@/components/record-link";
import { attempt, Breadcrumb, Chip, EmptyState, ErrorBanner, ErrorSummary, Field, Gap, LazyPart, lazyPart, Loading, PageHead, Select, TableRegion, Textarea, useLoad, useReturnFocus } from "@/components/app";
import { MoreMenu } from "@/components/more-menu";
import { RetireAckForm, retireCopy } from "@/components/retire-dialog";
import { chip, DetailList, DetailRow, LifecycleChip, LinkStateChip, Panel, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { StaleFlag, StatusChip, typeText } from "@/features/catalog/assessments";
import { ExtLink } from "@/features/catalog/detail";
import { useUnsavedGuard } from "@/features/catalog/record-form";
import { phraseOf } from "@/lib/relationship-types";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { StaleBanner as ChangedSinceBanner } from "@/features/money/contract-ui";
import { ifMatch, isStaleWrite } from "@/lib/if-match";

/** Change assessment detail. No Retire action in the header (retiring
 *  goes through the record's lifecycle, or the status change); Refresh snapshot is the header primary while the snapshot
 *  is stale; the status changes through a "Change status" menu with a confirm step; Description and Findings are one
 *  form with one Save. */

const when = (at: string) => at.slice(0, 16).replace("T", " ");
const noun = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
const na = <span aria-label="Not applicable">—</span>;

const icon = (children: React.ReactNode) => <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">{children}</svg>;
const IconDetails = icon(<><rect x="3" y="2" width="10" height="12" rx="1.5" /><path d="M5.5 6h5M5.5 9h5" /></>);
const IconNotes = icon(<path d="M3 3h10M3 6.5h10M3 10h6" />);
/** Ticket links (capability `ticket_links`), loaded on first use. */
const TicketLinks = lazyPart(() => import("@/features/tickets/ticket-links").then((m) => m.TicketLinks));

export function AssessmentDetail() {
  const { id } = useParams();
  return <Detail key={id} id={id!} />;
}

function Detail({ id }: { id: string }) {
  const me = useMe();
  const write = canWrite(me);
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const load = useLoad<Assessment>(`assessments/${id}`);
  const [notice, setNotice] = useState(() => (location.state as { notice?: string } | null)?.notice ?? "");
  const [actionError, setActionError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [status, setStatus] = useState<AssessmentStatus | null>(null);
  const [editing, setEditing] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const a = load.data;

  if (load.status === 404) {
    return (
      <>
        <PageHead title="Assessment not found" desc="This assessment doesn't exist or you don't have access." />
        <Button asChild variant="outline"><Link to="/assessments">Back to Change assessments</Link></Button>
      </>
    );
  }
  if (load.error && !a) {
    return (
      <>
        <Breadcrumb trail={[{ to: "/assessments", label: "Change assessments" }]} current="Assessment" />
        <PageHead title="Change assessment" />
        <ErrorBanner message={`Couldn't load this assessment. ${load.error}`} onRetry={load.reload} />
      </>
    );
  }
  if (!a) {
    return (
      <>
        <Breadcrumb trail={[{ to: "/assessments", label: "Change assessments" }]} current="Assessment" />
        <PageHead title="Change assessment" docTitle="Change assessment" />
        <Loading what="the assessment" rows={4} />
      </>
    );
  }

  const snap = a.snapshot;
  const stale = a.stale;
  const viewing = uuidOrEmpty(params.get("snapshot"));
  const viewingEarlier = viewing !== "" && viewing !== snap?.id;

  async function refresh() {
    setRefreshing(true);
    setActionError("");
    try {
      const made = await api<AssessmentRow>(`assessments/${id}/refresh`, { method: "POST", body: {} });
      const c = made.snapshot?.counts;
      setNotice(`Snapshot refreshed${c ? `: ${noun(c.dependents, "dependent", "dependents")}, ${noun(c.data_flows, "data flow", "data flows")}` : ""}. The previous snapshot is kept in history.`);
      if (viewing) { const next = new URLSearchParams(params); next.delete("snapshot"); setParams(next, { replace: true }); }
      load.reload();
    } catch (e) {
      setNotice("");
      setActionError(`Couldn't refresh the snapshot. ${e instanceof Problem ? e.detail : "Try again."} The current snapshot is unchanged.`);
    } finally {
      setRefreshing(false);
      setTimeout(() => document.getElementById("refresh-snapshot")?.focus());
    }
  }
  const showSnapshot = (sid: string | null) => {
    const next = new URLSearchParams(params);
    if (sid) next.set("snapshot", sid); else next.delete("snapshot");
    setParams(next, { replace: false });
  };

  const others = ASSESSMENT_STATUSES.filter((s) => s !== a.status);
  return (
    <>
      <Breadcrumb trail={[{ to: "/assessments", label: "Change assessments" }]} current={a.title} />
      <PageHead title={<span className="[overflow-wrap:anywhere]">{a.title}</span>} docTitle={`${a.title} · Change assessment`}
        desc={<>{typeText(a.change_type)} assessment of <RecordLink record={a.target} className="underline" />. Documentation, not approval.</>}>
        {write && (
          <Button id="refresh-snapshot" variant={stale ? "default" : "outline"} disabled={refreshing} onClick={() => void refresh()}>
            {refreshing ? "Refreshing…" : "Refresh snapshot"}
          </Button>
        )}
        {write && <MoreMenu label="Change status" actions={others.map((s) => ({ label: ASSESSMENT_STATUS_LABEL[s], onSelect: () => setStatus(s) }))} />}
        {write && <Button variant="outline" onClick={() => setEditing(true)}>Edit</Button>}
      </PageHead>

      <SuccessNote text={notice} />
      {actionError && <ErrorBanner message={actionError} />}
      {stale && snap && <StaleBanner snap={snap} write={write} />}

      <Panel id="h-details" icon={IconDetails} title="Details" className="mb-5">
        <DetailList>
          <DetailRow label="Status"><span className="inline-flex flex-wrap items-center gap-1.5"><StatusChip status={a.status} />{stale && <StaleFlag since={snap?.stale_since} />}</span></DetailRow>
          <DetailRow label="Type">{typeText(a.change_type)}</DetailRow>
          <DetailRow label="Target">
            <RecordLink record={a.target} className="underline" /> <span className="text-muted-foreground">{KIND_LABEL[a.target.kind] ?? cap(a.target.kind)} · {cap(a.target.lifecycle)}</span>
            {a.target.archived && <Chip className="ml-2 align-middle">Archived</Chip>}
          </DetailRow>
          <DetailRow label="Owner">{a.owner?.name ?? "No owner"}</DetailRow>
          <DetailRow label="Target date"><span className="tabular">{a.target_date ?? "—"}</span></DetailRow>
          <DetailRow label="External ticket"><ExtLink url={a.ticket_url} /></DetailRow>
          {a.retirement_ack_reason && <DetailRow label="Retirement acknowledgment"><span className="[overflow-wrap:anywhere]">{a.retirement_ack_reason}</span></DetailRow>}
        </DetailList>
      </Panel>
      <div className="mb-5"><LazyPart what="the ticket links"><TicketLinks target={{ kind: "assessment", id: a.id, name: a.title, title: a.title }} /></LazyPart></div>

      {snap ? (
        <p className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
          <span>Snapshot taken <time dateTime={snap.taken_at} className="tabular">{when(snap.taken_at)}</time>{snap.taken_by && ` by ${snap.taken_by.name}`}</span>
          <button type="button" className="text-link underline" aria-expanded={historyOpen} aria-controls="snapshot-history" onClick={() => setHistoryOpen(!historyOpen)}>
            Snapshot history ({a.history.length})
          </button>
        </p>
      ) : <p className="mb-2 text-sm text-muted-foreground">No snapshot is saved for this assessment yet.</p>}
      {historyOpen && <History rows={a.history} viewing={viewing || snap?.id || ""} onView={(sid) => showSnapshot(sid === snap?.id ? null : sid)} />}

      {viewingEarlier ? <EarlierSnapshot id={id} sid={viewing} target={a.target.name} onBack={() => showSnapshot(null)} />
        : snap ? <AffectedSet snap={snap} target={a.target.name} />
          : <EmptyState title="No affected set yet">Refresh the snapshot to calculate what depends on {a.target.name}.</EmptyState>}

      <Panel id="h-notes" icon={IconNotes} title="Description and findings" className="mt-5">
        <NotesForm a={a} write={write} onSaved={(m) => { setNotice(m); setActionError(""); }} />
      </Panel>

      <StatusDialog a={a} status={status} onClose={() => setStatus(null)} onDone={(m) => { setStatus(null); setActionError(""); setNotice(m); load.reload(); }} />
      <EditDialog a={a} open={editing} onClose={() => setEditing(false)} onDone={() => { setEditing(false); setActionError(""); setNotice("Details saved."); load.reload(); }} />
    </>
  );
}

// ---- Stale banner and history ----------------------------------------------------------------------------------------

/** A status region (not an alert): it is read when the page loads and doesn't interrupt. The reasons are the API's sentences. */
function StaleBanner({ snap, write }: { snap: SnapshotSummary; write: boolean }) {
  return (
    <div role="status" className="mb-5 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground">
      <p className="font-semibold"><span aria-hidden="true">↻</span> This snapshot is stale</p>
      <p className="mt-1">Since <span className="tabular">{snap.stale_since?.slice(0, 10)}</span>:</p>
      <ul className="ml-5 mt-1 list-disc" aria-label="Why this snapshot is stale">{snap.stale_reasons.map((r) => <li key={r}>{r}</li>)}</ul>
      <p className="mt-2 text-sm">
        {write ? "Refresh snapshot recalculates the affected set from the catalog as it is now. " : "An Editor or Admin can refresh it. "}
        The current snapshot is kept in history. Nothing refreshes on its own.
      </p>
    </div>
  );
}

function History({ rows, viewing, onView }: { rows: SnapshotSummary[]; viewing: string; onView: (id: string) => void }) {
  return (
    <div id="snapshot-history" className="mb-4">
      <TableRegion label="Snapshot history table">
        <Table>
          <TableCaption className="sr-only">Snapshot history, newest first. Earlier snapshots are kept read-only.</TableCaption>
          <TableHeader><TableRow>
            <TableHead scope="col" aria-sort="descending">Taken</TableHead><TableHead scope="col">Taken by</TableHead><TableHead scope="col">Dependents</TableHead>
            <TableHead scope="col">Data flows</TableHead><TableHead scope="col">State</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
          </TableRow></TableHeader>
          <TableBody>
            {rows.map((s) => (
              <TableRow key={s.id}>
                <TableHead scope="row" className="tabular font-semibold text-foreground">{when(s.taken_at)}</TableHead>
                <TableCell>{s.taken_by?.name ?? "—"}</TableCell>
                <TableCell className="tabular">{s.counts.dependents}</TableCell>
                <TableCell className="tabular">{s.counts.data_flows}</TableCell>
                <TableCell className="whitespace-normal">
                  <span className="inline-flex flex-wrap gap-1.5">
                    <Chip>{s.is_current ? "Current" : "Earlier"}</Chip>
                    {s.stale && <StaleFlag since={s.stale_since} />}
                  </span>
                </TableCell>
                <TableCell className="text-right">
                  {viewing === s.id ? <span className="text-sm text-muted-foreground">Shown below</span>
                    : <Button size="sm" variant="outline" onClick={() => onView(s.id)}>View<span className="sr-only"> snapshot taken {when(s.taken_at)}</span></Button>}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableRegion>
    </div>
  );
}

function EarlierSnapshot({ id, sid, target, onBack }: { id: string; sid: string; target: string; onBack: () => void }) {
  const load = useLoad<SnapshotFull>(`assessments/${id}/snapshots/${sid}`);
  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-3 rounded-md border bg-card px-4 py-3" role="status">
        <p className="min-w-0 flex-1">{load.data ? <>You are viewing the snapshot taken <span className="tabular">{when(load.data.taken_at)}</span>{load.data.taken_by && ` by ${load.data.taken_by.name}`}. It is kept read-only.</> : "Loading the earlier snapshot…"}</p>
        <Button size="sm" variant="outline" onClick={onBack}>Back to the current snapshot</Button>
      </div>
      {load.error && <ErrorBanner message={`Couldn't load that snapshot. ${load.error}`} onRetry={load.reload} />}
      {!load.data && !load.error && <Loading what="the snapshot" rows={4} />}
      {load.data && <AffectedSet snap={load.data} target={target} />}
    </>
  );
}

// ---- Affected set ----------------------------------------------------------------------------------------------------

const linkOf = (snap: SnapshotFull, n: SnapshotNode): SnapshotLinkRow | undefined => {
  const previous = n.path[n.path.length - 2];
  if (n.role === "data_flow") return snap.links.find((l) => l.kind === "integration" && l.id === n.resource.id);
  if (!previous) return undefined;
  return relationshipBetween(snap, n.resource.id, previous.id, n.role === "implementer");
};

/** The relationship from `source` to `target` a node was reached by: implements for an implementer; for a dependent,
 *  depends_on first, else another type that counts as a dependency (an added type; replaces and implements never do). */
function relationshipBetween(snap: SnapshotFull, source: string, target: string, implementer: boolean) {
  const rows = snap.links.filter((l) => l.kind === "relationship" && l.source.id === source && l.target.id === target);
  if (implementer) return rows.find((l) => l.type === "implements");
  return rows.find((l) => l.type === "depends_on") ?? rows.find((l) => l.type !== "implements" && l.type !== "replaces");
}

/** The path in dependency order, as in the explorer: a dependent first, the target last, each link in words from its
 *  type's name ("Portal → depends on → Sync"). Snapshot rows carry `type_name` (the type's current name, else the one
 *  stored at capture, else the slug), so no request for the organization's types is needed. */
function pathText(snap: SnapshotFull, n: SnapshotNode) {
  const steps = [...n.path].reverse();
  return steps.map((s, i) => {
    if (i === steps.length - 1) return s.name;
    const link = relationshipBetween(snap, s.id, steps[i + 1].id, false);
    return `${s.name} → ${link ? phraseOf(link.type_name, link.type) : "depends on"} →`;
  }).join(" ");
}

/** Link status: the link's confirmation state now, then whether the link itself changed since the snapshot. */
function LinkSince({ link }: { link: SnapshotLinkRow | undefined }) {
  if (!link) return na;
  if (link.removed) return <span className={chip({ state: "overdue" })}><span aria-hidden="true">!</span>Link removed</span>;
  return (
    <>
      {link.link_state && <LinkStateChip state={link.link_state} />}
      {link.link_why && <span className="mt-1 block max-w-56 text-[13px] text-muted-foreground">{link.link_why}</span>}
      <span className="mt-1 block text-xs">
        {link.changed ? <><span aria-hidden="true">◐ </span><span className="font-semibold">Link changed</span> since the snapshot</> : "Unchanged since the snapshot"}
      </span>
    </>
  );
}

function ownerName(o: SnapshotOwner) { return o ? `${o.name}${o.active ? "" : " (inactive)"}` : null; }

function OwnersCell({ n, flow }: { n: SnapshotNode; flow: boolean }) {
  const tech = ownerName(n.owners_then.technical_owner);
  const biz = ownerName(n.owners_then.business_owner);
  const nowTech = ownerName(n.owners_now.technical_owner);
  const nowBiz = ownerName(n.owners_now.business_owner);
  return (
    <>
      <span className="block">Technical: {tech ?? <Gap label="No technical owner" />}</span>
      {!flow && <span className="block text-muted-foreground">Business: {biz ?? "none"}</span>}
      {n.changed.owners && (
        <span className="mt-1 block text-xs">
          <span aria-hidden="true">◐ </span><span className="font-semibold">Owners changed since the snapshot.</span> Now: technical {nowTech ?? "none"}{!flow && `, business ${nowBiz ?? "none"}`}.
        </span>
      )}
    </>
  );
}

function LifecycleCell({ n }: { n: SnapshotNode }) {
  return (
    <>
      {cap(n.lifecycle_then)}
      {n.changed.lifecycle && <span className="mt-1 block text-xs"><span aria-hidden="true">◐ </span><span className="font-semibold">Changed.</span> Now {cap(n.lifecycle_now)}</span>}
    </>
  );
}

function AffectedSet({ snap, target }: { snap: SnapshotFull; target: string }) {
  const nodes = snap.nodes.filter((n) => n.role === "dependent");
  const flows = snap.nodes.filter((n) => n.role === "data_flow");
  const implementers = snap.nodes.filter((n) => n.role === "implementer");
  const c = snap.counts;
  const changedLinks = snap.links.filter((l) => l.changed).length;
  const ownerGaps = nodes.filter((n) => !n.in_cycle && !n.owners_then.technical_owner).length;
  const retired = nodes.filter((n) => n.lifecycle_then === "retired").length;
  const empty = nodes.length === 0 && flows.length === 0;
  const heading = `Affected set — ${noun(c.dependents, "dependent", "dependents")}, ${noun(c.data_flows, "data flow", "data flows")}`;
  const cols = 7;
  return (
    <section aria-labelledby="h-affected">
      <h2 id="h-affected" tabIndex={-1} className="mb-1 font-display text-section font-semibold">{heading}</h2>
      <ul aria-label="Snapshot summary" className="mb-3 flex flex-wrap gap-x-5 gap-y-1 text-sm text-muted-foreground">
        <li>{noun(c.dependents, "dependent", "dependents")}</li>
        <li>{noun(c.data_flows, "data flow", "data flows")}</li>
        {c.implementers > 0 && <li>{noun(c.implementers, "implementing component", "implementing components")}</li>}
        {c.cycles > 0 && <li><span aria-hidden="true">↻ </span>{noun(c.cycles, "cycle", "cycles")}</li>}
        {retired > 0 && <li>{retired} already Retired</li>}
        {ownerGaps > 0 && <li><span aria-hidden="true">⚠ </span>{noun(ownerGaps, "dependent has", "dependents have")} no technical owner</li>}
        {changedLinks > 0 && <li><span aria-hidden="true">◐ </span>{noun(changedLinks, "link has", "links have")} changed since the snapshot</li>}
      </ul>
      {empty ? (
        <EmptyState title="No dependents recorded">Nothing is recorded as depending on {target}. A missing path is not evidence that a change is safe.</EmptyState>
      ) : (
        <TableRegion label="Affected set table">
          <Table>
            <TableCaption className="sr-only">Affected set for {target}: records that depend on it, with lifecycle and owners as they were when the snapshot was taken, then the data flows.</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">System</TableHead><TableHead scope="col">Kind</TableHead><TableHead scope="col">Depth</TableHead><TableHead scope="col">Path</TableHead>
              <TableHead scope="col">Lifecycle then</TableHead><TableHead scope="col">Owners then</TableHead><TableHead scope="col">Link status</TableHead>
            </TableRow></TableHeader>
            {nodes.length > 0 && (
              <TableBody>
                {nodes.map((n) => (
                  <TableRow key={n.id} className={`align-top ${n.lifecycle_then === "retired" ? "text-muted-foreground" : ""}`}>
                    <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold [overflow-wrap:anywhere]">
                      <RecordLink record={n.resource} className="underline" />
                      {n.lifecycle_then === "retired" && <LifecycleChip value="retired" className="ml-2 align-middle" />}
                      {n.resource.archived && <Chip className="ml-2 align-middle">Archived</Chip>}
                      {n.in_cycle && <Chip className="ml-2 align-middle"><span aria-hidden="true">↻</span>Cycle<span className="sr-only"> (this path returns to a record already in the set)</span></Chip>}
                    </TableHead>
                    <TableCell>{n.in_cycle ? na : (KIND_LABEL[n.resource.kind] ?? cap(n.resource.kind))}</TableCell>
                    <TableCell className="tabular">{n.depth}</TableCell>
                    <TableCell className="min-w-56 max-w-sm whitespace-normal [overflow-wrap:anywhere]">
                      {pathText(snap, n)}
                      {n.in_cycle && <span className="block text-xs">Cycle back to {n.resource.name}{n.resource.name === target ? ", the target" : ""}, flagged and not followed further.</span>}
                    </TableCell>
                    <TableCell className="whitespace-normal">{n.in_cycle ? na : <LifecycleCell n={n} />}</TableCell>
                    <TableCell className="min-w-44 whitespace-normal">{n.in_cycle ? na : <OwnersCell n={n} flow={false} />}</TableCell>
                    <TableCell className="whitespace-normal"><LinkSince link={linkOf(snap, n)} /></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            )}
            {flows.length > 0 && (
              <TableBody>
                <TableRow className="bg-table-head hover:bg-table-head">
                  <TableHead colSpan={cols} scope="rowgroup" className="h-9 text-xs font-semibold text-muted-foreground">Data flows — data flow, not operational</TableHead>
                </TableRow>
                {flows.map((f) => {
                  const link = linkOf(snap, f);
                  const attached = f.path[f.path.length - 2];
                  const implementedBy = implementers.filter((i) => i.path[i.path.length - 2]?.id === f.resource.id);
                  return (
                    <TableRow key={f.id} className={`align-top ${f.lifecycle_then === "retired" ? "text-muted-foreground" : ""}`}>
                      <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold [overflow-wrap:anywhere]">
                        <Link to={`/integrations/${f.resource.id}`} className="underline">{f.resource.name}</Link>
                        {f.lifecycle_then === "retired" && <LifecycleChip value="retired" className="ml-2 align-middle" />}
                        {f.resource.archived && <Chip className="ml-2 align-middle">Archived</Chip>}
                        {link && <span className="block text-xs font-normal text-muted-foreground">{link.source.name} <span aria-hidden="true">→</span><span className="sr-only"> to </span> {link.target.name}</span>}
                      </TableHead>
                      <TableCell>Integration</TableCell>
                      <TableCell className="tabular">1 hop</TableCell>
                      <TableCell className="min-w-56 max-w-sm whitespace-normal [overflow-wrap:anywhere]">
                        {implementedBy.length === 0 ? <Chip>Vendor-native</Chip> : <>Implemented by {implementedBy.map((i, k) => (
                          <span key={i.id}>{k > 0 && ", "}<Link to={`/components/${i.resource.id}`} className="underline">{i.resource.name}</Link>{i.resource.archived && <Chip className="ml-1 align-middle">Archived</Chip>}</span>
                        ))}</>}
                        {attached && <span className="block text-xs text-muted-foreground">Attached to {attached.name}</span>}
                      </TableCell>
                      <TableCell className="whitespace-normal"><LifecycleCell n={f} /></TableCell>
                      <TableCell className="min-w-44 whitespace-normal"><OwnersCell n={f} flow /></TableCell>
                      <TableCell className="whitespace-normal"><LinkSince link={link} /></TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            )}
          </Table>
        </TableRegion>
      )}
      <p className="mt-3 flex items-start gap-2 text-sm">
        <span aria-hidden="true">⚠</span>
        <span>A missing path is not evidence that a change is safe.{changedLinks > 0 && ` ${noun(changedLinks, "link has", "links have")} changed since the snapshot.`}{ownerGaps > 0 && ` ${noun(ownerGaps, "dependent has", "dependents have")} an owner gap.`}</span>
      </p>
    </section>
  );
}

// ---- Description and findings: one form, one Save ----------------------------------------------------------------------

function NotesForm({ a, write, onSaved }: { a: Assessment; write: boolean; onSaved: (message: string) => void }) {
  const [saved, setSaved] = useState({ description: a.description, findings: a.findings });
  const [values, setValues] = useState(saved);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);
  const [stale, setStale] = useState(false);
  // The description and findings as the server last had them (read or saved): what a save checks is still current.
  const [server, setServer] = useState(saved);
  const dirty = values.description !== saved.description || values.findings !== saved.findings;
  const guard = useUnsavedGuard(dirty && !saving);

  if (!write) {
    return (
      <DetailList className="pb-2">
        <DetailRow label="Description"><span className="whitespace-pre-wrap [overflow-wrap:anywhere]">{a.description || <span className="text-muted-foreground">No description.</span>}</span></DetailRow>
        <DetailRow label="Findings"><span className="whitespace-pre-wrap [overflow-wrap:anywhere]">{a.findings || <span className="text-muted-foreground">No findings recorded.</span>}</span></DetailRow>
      </DetailList>
    );
  }
  async function loadLatest() {
    const fresh = await apiWithEtag<Assessment>(`assessments/${a.id}`).catch(() => null);
    if (!fresh) return setFormError("Couldn't load the latest version. Try again.");
    const latest = { description: fresh.data.description, findings: fresh.data.findings };
    setSaved(latest);
    setServer(latest);
    setValues(latest);
    setStale(false);
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setErrors({});
    setFormError("");
    // The text stays in the boxes whatever happens: it is only replaced by what the server saved.
    // If-Match: the form stays open across this page's other writes (status, details, snapshot), so it reads the
    // assessment's ETag now and sends it only while the description and findings are still the ones it started from;
    // otherwise someone else changed them and the save shows "Load the latest version" instead of overwriting theirs.
    let stale412 = false;
    const failed = await attempt(async () => {
      const now = await apiWithEtag<Assessment>(`assessments/${a.id}`);
      if (now.data.description !== server.description || now.data.findings !== server.findings) { stale412 = true; return; }
      try {
        const done = await api<Assessment>(`assessments/${a.id}`, { method: "PATCH", body: values, headers: ifMatch(now.etag) });
        setServer({ description: done.description, findings: done.findings });
      } catch (err) {
        if (!isStaleWrite(err)) throw err;
        stale412 = true;
      }
    });
    setSaving(false);
    if (stale412) return setStale(true);
    if (failed) {
      if (Object.keys(failed.errors).length) setErrors(failed.errors);
      else setFormError(`${failed.detail} Your text wasn't saved. Nothing you typed was lost.`);
      return;
    }
    setSaved(values);
    onSaved("Description and findings saved.");
  }
  return (
    <form onSubmit={submit} noValidate aria-label="Description and findings" className="px-4 py-4">
      {stale && <ChangedSinceBanner what="assessment" onLoad={() => void loadLatest()} />}
      {formError && <ErrorBanner message={formError} />}
      <ErrorSummary errors={errors} />
      <Field id="description" label="Description" errors={errors} help="What is changing and why.">
        <Textarea id="description" rows={4} value={values.description} onChange={(e) => setValues({ ...values, description: e.target.value })} disabled={saving} />
      </Field>
      <Field id="findings" label="Findings" errors={errors} help="What you learned from the affected set, for example who confirmed what.">
        <Textarea id="findings" rows={5} value={values.findings} onChange={(e) => setValues({ ...values, findings: e.target.value })} disabled={saving} />
      </Field>
      <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
        <Button asChild variant="outline"><Link to={`/dependencies?target=${a.target.id}&depth=all&direction=dependents`}>Open dependency explorer</Link></Button>
        <Button type="submit" disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
      </div>
      {guard}
    </form>
  );
}

// ---- Change status (with the retirement acknowledgment) --------------------------------------------------------------------

function StatusDialog({ a, status, onClose, onDone }: { a: Assessment; status: AssessmentStatus | null; onClose: () => void; onDone: (message: string) => void }) {
  const returnFocus = useReturnFocus(status !== null);
  return (
    <Dialog open={status !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        {status && <StatusBody key={status} a={a} status={status} onClose={onClose} onDone={onDone} />}
      </DialogContent>
    </Dialog>
  );
}

/** Confirm step first. If the API answers that active records depend on the target of a retirement, the same dialog turns
 *  into the acknowledgment form, so there is one dialog and one place for focus to return from. */
function StatusBody({ a, status, onClose, onDone }: { a: Assessment; status: AssessmentStatus; onClose: () => void; onDone: (m: string) => void }) {
  const [check, setCheck] = useState<RetirementCheck | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const label = ASSESSMENT_STATUS_LABEL[status];
  const retiring = a.change_type === "retirement" && status === "ready" && a.target.lifecycle !== "retired" && a.status !== "ready";

  async function send(extra: Record<string, unknown> = {}): Promise<string | null> {
    try {
      await api(`assessments/${a.id}/status`, { method: "POST", body: { status, ...extra } });
      onDone(extra.retirement_ack ? `Status changed to ${label}. Retirement acknowledged; the reason is saved with this assessment.` : `Status changed to ${label}.`);
      return null;
    } catch (e) {
      const needs = retirementCheck(e);
      if (needs) { setCheck(needs); return null; }
      if (!(e instanceof Problem)) return "Something went wrong. Try again.";
      return Object.values(e.errors).flat().join(" ") || e.detail;
    }
  }
  async function confirm() {
    setBusy(true);
    setError("");
    const failed = await send();
    setBusy(false);
    if (failed) setError(failed);
  }

  if (check) {
    const copy = retireCopy("assessment", a.target.name, check);
    return (
      <>
        <DialogHeader>
          <DialogTitle className="leading-snug [overflow-wrap:anywhere]">{copy.title}</DialogTitle>
          <DialogDescription>{copy.description}</DialogDescription>
        </DialogHeader>
        <RetireAckForm mode="assessment" name={a.target.name} check={check} onCancel={onClose} onConfirm={(reason) => send({ retirement_ack: true, retirement_ack_reason: reason })} />
      </>
    );
  }
  return (
    <>
      <DialogHeader>
        <DialogTitle className="leading-snug [overflow-wrap:anywhere]">Change status to {label}?</DialogTitle>
        <DialogDescription>
          {a.title} is now {ASSESSMENT_STATUS_LABEL[a.status]}. Status records where the assessment stands. It doesn't approve or run the change.
        </DialogDescription>
      </DialogHeader>
      {retiring && <p className="text-sm text-muted-foreground">This assessment is for a retirement. If active records still depend on {a.target.name}, you will be asked to acknowledge them first.</p>}
      {error && <p role="alert" className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2">{error}</p>}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        <Button type="button" disabled={busy} onClick={() => void confirm()}>{busy ? "Saving…" : "Change status"}</Button>
      </DialogFooter>
    </>
  );
}

// ---- Edit details -----------------------------------------------------------------------------------------------------

function EditDialog({ a, open, onClose, onDone }: { a: Assessment; open: boolean; onClose: () => void; onDone: () => void }) {
  const returnFocus = useReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Edit details</DialogTitle>
          <DialogDescription>Title, owner, target date and ticket. The target and the type can't be changed; make a new assessment instead. Description and findings are edited below.</DialogDescription>
        </DialogHeader>
        {open && (
          <OpenedEdit<Assessment> path={`assessments/${a.id}`} what="assessment">
            {(fresh, write) => <EditForm a={fresh} write={write} onCancel={onClose} onDone={onDone} />}
          </OpenedEdit>
        )}
      </DialogContent>
    </Dialog>
  );
}

function EditForm({ a, write, onCancel, onDone }: { a: Assessment; write: OpenedWrite; onCancel: () => void; onDone: () => void }) {
  const stewards = useLoad<UserRef[]>("stewards");
  const [form, setForm] = useState({ title: a.title, owner: a.owner ? String(a.owner.id) : "", target_date: a.target_date ?? "", ticket_url: a.ticket_url });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.title.trim()) return setErrors({ title: ["Enter a title."] });
    setBusy(true);
    const failed = await attempt(async () => api(`assessments/${a.id}`, { method: "PATCH", body: { title: form.title.trim(), owner: form.owner || null, target_date: form.target_date || null, ticket_url: form.ticket_url.trim() }, headers: await write.ifMatch() }));
    setBusy(false);
    if (failed?.status === 412) return write.onStale();
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { title: [failed.detail] });
    onDone();
  }
  const known = (stewards.data ?? []).some((u) => String(u.id) === form.owner);
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      <Field id="title" label="Title (required)" errors={errors}><Input id="title" value={form.title} onChange={set("title")} autoComplete="off" /></Field>
      <Field id="owner" label="Owner" errors={errors}>
        <Select id="owner" value={form.owner} onChange={set("owner")}>
          <option value="">No owner</option>
          {(stewards.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
          {form.owner && !known && <option value={form.owner}>{a.owner?.name} (no longer an Admin or Editor)</option>}
        </Select>
      </Field>
      <Field id="target_date" label="Target date" errors={errors}><Input id="target_date" type="date" value={form.target_date} onChange={set("target_date")} /></Field>
      <Field id="ticket_url" label="External ticket URL" errors={errors}><Input id="ticket_url" type="url" inputMode="url" placeholder="https://…" value={form.ticket_url} onChange={set("ticket_url")} /></Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? "Saving…" : "Save details"}</Button>
      </DialogFooter>
    </form>
  );
}
