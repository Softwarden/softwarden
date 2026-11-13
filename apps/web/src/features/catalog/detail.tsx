import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { apiKey } from "@/lib/query";
import {
  AGENT_AUTH_MODES, AGENT_PLATFORMS, AGENT_RISKS, api, apiWithEtag, AUTH_METHODS, CatalogRecord, Problem, cap, CRITICALITIES, DATA_CLASSIFICATIONS, DEPLOYMENT_MODELS,
  ENVIRONMENTS as RECORD_ENVIRONMENTS, HOSTINGS, Instance, labelOf, orgToday, Person, Relationship, Renewal, Review, RISK_TIERS, typeLabel, UserRef, VALIDATION_STATUSES,
} from "@/api";
import { apiPath, editPath, KIND_LABEL, KIND_PLURAL, listPath, type RecordKind } from "@/lib/kinds";
import { ChannelChips, modelName } from "@/features/catalog/catalog-list";
import { TriText } from "@/features/catalog/fields";
import { CustomFieldsPanel } from "@/features/catalog/custom-fields";
import { canWrite, useMe, useLandingNotice } from "@/lib/session";
import {
  attempt, Breadcrumb, Chip, EmptyState, ErrorBanner, ErrorSummary, Field, Gap, LazyPart, lazyPart, Loading, PageHead, Select, StateChip, TableRegion, TabStrip, useLoad, useReturnFocus,
} from "@/components/app";
import { Combobox, ComboOption } from "@/components/combobox";
import { ActivityTab } from "@/features/admin/audit";
import { RelationshipsTab } from "@/features/catalog/relationships";
import { DeadlineCell } from "@/features/money/renewals";
import { ArchiveDialog, DeleteDialog } from "@/components/lifecycle-dialogs";
import { MoreMenu, MenuAction } from "@/components/more-menu";
import { chip, CriticalityMark, DetailList, GapChips, DetailRow, KindTile, LifecycleChip, Panel, SuccessNote } from "@/components/sw";
import { DiscoverySources } from "@/features/discovery/discovery";
import { AttachmentsTab, useAttachmentsLabel } from "@/features/attachments/tab";
import { CompletenessBar, RecordQualityPanel } from "@/features/quality/completeness";
import { TagChips } from "@/features/catalog/curation";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { ANY_REVISION, ifMatch, staleDeleteText, useOpenedRecord } from "@/lib/if-match";

/** Application detail, component detail and AI agent detail. One screen, three kinds. */

type Named = { id: string; name: string };
type Kind = RecordKind;
type TabId = "overview" | "instances" | "relationships" | "map" | "renewals" | "attachments" | "history" | "activity";

/** The Map tab is loaded on first use; its picture loads Cytoscape in a chunk of its own. */
const MapTab = lazyPart(() => import("@/features/catalog/record-map").then((m) => m.MapTab));
/** The End of life panel (capability `eol`): applications and components, loaded on first use. */
const EndOfLifePanel = lazyPart(() => import("@/features/eol/eol-panel").then((m) => m.EndOfLifePanel));
/** SSO and MFA coverage (capability `discovery.entra`): applications, loaded on first use. */
const CoveragePanel = lazyPart(() => import("@/features/discovery/coverage-panel").then((m) => m.CoveragePanel));

const svg = (children: React.ReactNode) => <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">{children}</svg>;
const IconOwners = svg(<><circle cx="6" cy="5.5" r="2.5" /><path d="M1.5 14c.6-2.6 2.3-4 4.5-4s3.9 1.4 4.5 4" /><path d="M11.5 4.5v4M9.5 6.5h4" /></>);
const IconReview = svg(<><rect x="2.5" y="2" width="11" height="12" rx="1.5" /><path d="m5.5 8.5 1.8 1.8 3.4-3.6" /></>);
const IconDetails = svg(<><rect x="2.5" y="2" width="11" height="12" rx="1.5" /><path d="M5.5 5.5h5M5.5 8h5M5.5 10.5h3" /></>);
const IconAgent = svg(<><rect x="3" y="5" width="10" height="8" rx="2" /><path d="M8 5V2.5M6 9h.01M10 9h.01" /></>);
const IconShield = svg(<><path d="M8 1.8 13 3.6v3.7c0 3-2 5.4-5 6.3-3-.9-5-3.3-5-6.3V3.6z" /><path d="m6 8 1.5 1.5L10.5 6.5" /></>);
const IconRuntime = svg(<><rect x="2" y="3" width="12" height="9" rx="1.5" /><path d="M5 14h6M5.5 6.5 7 8 5.5 9.5M8.5 9.5H11" /></>);

export const dash = <span aria-label="Not set">—</span>;
/** A choice value's label, or a dash when not recorded. */
const choice = (list: { value: string; label: string }[], value?: string | null) => (value ? labelOf(list, value) : dash);
export const plain = (s?: string) => (s ? cap(s.replace(/_/g, " ")) : dash);

/** A link to a documentation or support URL. Only http(s) URLs become links. */
export function ExtLink({ url }: { url?: string }) {
  if (!url) return dash;
  let u: URL;
  try { u = new URL(url); } catch { return <>{url}</>; }
  if (!/^https?:$/.test(u.protocol)) return <>{url}</>;
  return <a href={u.href} target="_blank" rel="noreferrer noopener" className="[overflow-wrap:anywhere]">{u.host + (u.pathname === "/" ? "" : u.pathname)}<span className="sr-only"> (opens in a new tab)</span></a>;
}

export const day = (iso?: string | null) => (iso ? iso.slice(0, 10) : "");

// ---- Route elements ---------------------------------------------------------------

export function ApplicationDetail() {
  const { id } = useParams();
  return <RecordDetail key={id} kind="application" id={id!} />;
}

export function ComponentDetail() {
  const { id } = useParams();
  return <RecordDetail key={id} kind="component" id={id!} />;
}

export function AgentDetail() {
  const { id } = useParams();
  return <RecordDetail key={id} kind="ai_agent" id={id!} />;
}

function DetailSkeleton({ what }: { what: string }) {
  const bar = (w: string) => <span className={`block h-3.5 rounded-sm bg-muted ${w}`} />;
  return (
    <div aria-live="polite">
      <p className="sr-only">Loading {what}…</p>
      <div aria-hidden="true">
        <div className="mb-5 space-y-3"><span className="block h-7 w-1/3 rounded-sm bg-muted" />{bar("w-1/2")}</div>
        <div className="mb-5 rounded-lg border bg-card px-4 py-4"><div className="space-y-3">{bar("w-1/3")}{bar("w-1/4")}</div></div>
        <div className="grid gap-4 md:grid-cols-2">
          {[0, 1].map((i) => (
            <div key={i} className="space-y-4 rounded-lg border bg-card px-4 py-4">{bar("w-1/4")}{bar("w-3/4")}{bar("w-2/3")}{bar("w-3/4")}{bar("w-1/2")}</div>
          ))}
        </div>
      </div>
    </div>
  );
}

function NotFound({ kind }: { kind: Kind }) {
  return (
    <>
      <PageHead title="Record not found" desc="This record doesn't exist or you don't have access." />
      <Button asChild variant="outline"><Link to={listPath(kind)}>Back to {KIND_PLURAL[kind]}</Link></Button>
    </>
  );
}

function RecordDetail({ kind, id }: { kind: Kind; id: string }) {
  const base = apiPath(kind);
  const me = useMe();
  const rec = useLoad<CatalogRecord>(`${base}/${id}`);
  const reviews = useLoad<Review[]>(`${base}/${id}/reviews`);
  const rels = useLoad<Relationship[]>(`relationships?resource=${id}`);
  // Renewals cover applications only; a component's list is always empty, so its tab isn't shown.
  const renewals = useLoad<Renewal[]>(`renewals?application=${id}`);
  const attachmentsLabel = useAttachmentsLabel(me, kind, id);
  const [params, setParams] = useSearchParams();
  const [dialog, setDialog] = useState<"owners" | "review" | null>(null);
  const [lifecycle, setLifecycle] = useState<"archive" | "delete" | null>(null);
  // The delete confirmation reads the record with its ETag as it opens; the delete sends it as If-Match.
  const deleteRead = useOpenedRecord(lifecycle === "delete" ? `${base}/${id}` : null);
  const [pending, setPending] = useState(false);
  const [refusal, setRefusal] = useState("");
  // A notice passed by the edit form, for example the reason given when a record was retired with acknowledgment.
  const [toast, setToast, toastQuiet] = useLandingNotice();
  const [actionError, setActionError] = useState("");
  const navigate = useNavigate();
  const headingRef = useRef<HTMLDivElement>(null);

  const [ownerFocus, setOwnerFocus] = useState("");
  const [undo, setUndo] = useState<{ text: string; spec: UndoSpec } | null>(null);
  const clearUndo = useCallback(() => setUndo(null), []);

  function done(message: string, spec?: UndoSpec) {
    setDialog(null);
    setActionError("");
    if (spec) { setToast(""); setUndo({ text: message, spec }); } else { setUndo(null); setToast(message); }
    rec.reload();
    reviews.reload();
  }
  /** Undo restores the previous values only if the ones it set are still current; otherwise the API refuses and names who changed them. */
  async function onUndo() {
    if (!undo) return;
    const { restore, expected } = undo.spec;
    setUndo(null);
    try {
      // `expected` is the undo's own check (the values it set are still current), so any revision is fine.
      await api(`${base}/${id}`, { method: "PATCH", body: { ...restore, expected, undo: true }, headers: ANY_REVISION });
      setToast("Undone.");
    } catch (e) {
      setActionError(e instanceof Problem ? e.detail : "Couldn't undo. Try again.");
    }
    rec.reload();
  }
  function openLifecycle(which: "archive" | "delete") {
    setPending(false);
    setRefusal("");
    setLifecycle(which);
  }

  /** Runs a lifecycle request. A 409 is the API refusing (shown in the dialog); anything else uses the page error display. */
  async function lifecycleRequest(path: string, method: string, message: string, after?: () => void) {
    setPending(true);
    setRefusal("");
    try {
      await api(path, { method, headers: method === "DELETE" ? ifMatch(await deleteRead.etag()) : undefined });
      setLifecycle(null);
      if (after) return after();
      setToast(message);
      rec.reload();
    } catch (e) {
      if (e instanceof Problem && e.status === 412) setRefusal(staleDeleteText("record"));
      else if (e instanceof Problem && e.status === 409) setRefusal(e.detail);
      else {
        setLifecycle(null);
        setToast("");
        setActionError(e instanceof Problem ? e.detail : "Something went wrong. Try again.");
      }
    } finally {
      setPending(false);
    }
  }
  const onArchive = () => lifecycleRequest(`${base}/${id}/archive`, "POST", "Archived.");
  const onDelete = () => lifecycleRequest(`${base}/${id}`, "DELETE", "", () => navigate(listPath(kind)));
  /** Restore has no dialog: one click, a success note, focus on the heading. */
  async function onRestore() {
    setActionError("");
    setPending(true);
    try {
      await api(`${base}/${id}/restore`, { method: "POST" });
      setToast("Restored.");
      rec.reload();
      setTimeout(() => headingRef.current?.querySelector<HTMLElement>("h1")?.focus(), 0);
    } catch (e) {
      setToast("");
      setActionError(e instanceof Problem ? e.detail : "Something went wrong. Try again.");
    } finally {
      setPending(false);
    }
  }

  if (rec.status === 404) return <NotFound kind={kind} />;
  if (rec.error) return <ErrorBanner message={rec.error} onRetry={rec.reload} />;
  if (!rec.data) return <DetailSkeleton what={kind} />;
  const r = rec.data;
  const write = canWrite(me);
  const isAdmin = me.role === "admin";

  const tabs: { id: TabId; label: string }[] = [
    { id: "overview", label: "Overview & ownership" },
    ...(kind === "application" ? [{ id: "instances" as const, label: `Instances${r.instances ? ` (${r.instances.length})` : ""}` }] : []),
    { id: "relationships", label: `Relationships${rels.data ? ` (${rels.data.length})` : ""}` },
    { id: "map", label: "Map" },
    ...(kind === "application" ? [{ id: "renewals" as const, label: `Renewals${renewals.data ? ` (${renewals.data.length})` : ""}` }] : []),
    ...(attachmentsLabel ? [{ id: "attachments" as const, label: attachmentsLabel }] : []),
    { id: "history", label: `Review history${reviews.data ? ` (${reviews.data.length})` : ""}` },
    { id: "activity", label: "Audit activity" },
  ];
  const tab = tabs.find((t) => t.id === params.get("tab"))?.id ?? "overview";
  const setTab = (t: TabId) => setParams(t === "overview" ? {} : { tab: t });
  async function copyId() {
    try {
      await navigator.clipboard.writeText(r.id);
      setToast("Softwarden ID copied.");
    } catch {
      setToast(`Couldn't copy. The Softwarden ID is ${r.id}.`);
    }
  }
  const actions: MenuAction[] = [
    ...(write ? [r.archived ? { label: "Restore", onSelect: () => void onRestore() } : { label: "Archive", onSelect: () => openLifecycle("archive") }] : []),
    ...(isAdmin ? [{ label: "Delete", danger: true, onSelect: () => openLifecycle("delete") }] : []),
    { label: "Copy Softwarden ID", onSelect: () => void copyId() },
  ];

  const trail = kind === "component"
    ? [{ to: "/applications", label: "Applications" }, { to: listPath("component"), label: "Components" }]
    : [{ to: listPath(kind), label: KIND_PLURAL[kind] }];
  const facts = [
    KIND_LABEL[kind],
    ...(kind === "component" ? [typeLabel(r.component_type)] : []),
    ...(kind === "application" && r.vendor ? [`Vendor: ${r.vendor.name}`] : []),
    ...(kind === "ai_agent" && r.platform ? [labelOf(AGENT_PLATFORMS, r.platform)] : []),
  ];

  return (
    <>
      <Breadcrumb trail={trail} current={r.name} />
      <div ref={headingRef}><PageHead title={<span className="[overflow-wrap:anywhere]">{r.name}</span>} docTitle={r.name} desc={
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
          <KindTile kind={kind} /><span>{facts.join(" · ")}</span>
          <LifecycleChip value={r.lifecycle} />
          {r.criticality && <CriticalityMark value={r.criticality} suffix=" criticality" />}
          {r.archived && <Chip>Archived</Chip>}
        </span>
      }>
        <MoreMenu actions={actions} />
        {write && <Button asChild variant="outline"><Link to={editPath(kind, r.id)}>Edit<span className="sr-only"> {r.name}</span></Link></Button>}
        {write && <Button onClick={() => setDialog("review")}>Record review</Button>}
      </PageHead></div>
      {actionError && <ErrorBanner message={actionError} />}

      {r.archived && (
        <div className="mb-5 flex flex-wrap items-center gap-3 rounded-md border border-border-strong bg-muted px-4 py-3" role="region" aria-label="Archived record">
          <p className="min-w-0 flex-1">
            Archived{r.archived_at && ` on ${day(r.archived_at)}`}{r.archived_by && ` by ${r.archived_by.name}`}. Hidden from day-to-day views; history and links kept.
          </p>
          {write && <Button variant="outline" size="sm" disabled={pending} onClick={() => void onRestore()}>Restore</Button>}
        </div>
      )}

      <div className="mb-5 grid overflow-hidden rounded-lg border bg-card sm:grid-cols-2" role="group" aria-label="Record status">
        <div className="meta-line flex flex-col gap-1.5 px-4 py-3 sm:border-r">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Review</span>
          <span className="flex flex-wrap items-center gap-2"><StateChip state={r.review_state} /><span className="text-sm text-muted-foreground">{r.review_why}</span></span>
        </div>
        <div className="flex flex-col gap-1.5 border-t px-4 py-3 sm:border-t-0">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{kind === "ai_agent" ? "Gaps" : "Ownership"}</span>
          <span className="flex flex-wrap items-center gap-1">
            {r.gaps.length ? <><span className="mr-1 text-sm">{r.gaps.length} {r.gaps.length === 1 ? "gap" : "gaps"}:</span><GapChips gaps={r.gaps} /></> : <span className="text-sm text-state-confirmed-foreground">✓ Complete</span>}
          </span>
        </div>
        {r.quality && (
          <div className="flex flex-wrap items-center gap-2 border-t px-4 py-3 sm:col-span-2" data-testid="record-completeness">
            <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Data quality</span>
            <CompletenessBar score={r.quality.score} label="Completeness" />
            <span className="text-sm text-muted-foreground">
              {r.quality.failing.length ? `${r.quality.failing.length} failing ${r.quality.failing.length === 1 ? "check" : "checks"}` : "Passes every check that applies"}
              {r.quality.excepted.length > 0 && `, ${r.quality.excepted.length} not applicable`}
            </span>
          </div>
        )}
      </div>
      {undo ? <UndoNote text={undo.text} onUndo={() => void onUndo()} onExpire={clearUndo} /> : <SuccessNote text={toast} quiet={toastQuiet} />}

      <TabStrip tabs={tabs} tab={tab} onSelect={setTab} label={`${r.name} sections`} />
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "overview" && <>
          {r.quality && <div className="mb-4"><RecordQualityPanel record={{ id: r.id, kind, name: r.name }} quality={r.quality} onChanged={rec.reload} /></div>}
          <Overview r={r} kind={kind} reviews={reviews} write={write} onEdit={(field) => { setOwnerFocus(field ?? ""); setDialog("owners"); }} onHistory={() => setTab("history")} onCopy={() => void copyId()}
            onReload={(text) => { setToast(text); rec.reload(); }} />
        </>}
        {tab === "instances" && <InstancesTab id={r.id} appName={r.name} write={write} onChanged={rec.reload} />}
        {tab === "relationships" && <RelationshipsTab rec={r} kind={kind} rels={rels} write={write} />}
        {tab === "map" && <LazyPart what="the map"><MapTab kind={kind} id={r.id} name={r.name} write={write} /></LazyPart>}
        {tab === "renewals" && <RenewalsTab name={r.name} load={renewals} write={write} />}
        {tab === "attachments" && <AttachmentsTab parent={{ type: kind, id: r.id, name: r.name }} />}
        {tab === "history" && <HistoryTab load={reviews} />}
        {tab === "activity" && <ActivityTab base={base} id={r.id} />}
      </div>

      <OwnersDialog rec={r} base={base} focus={ownerFocus} open={dialog === "owners"} onOpenChange={(o) => setDialog(o ? "owners" : null)} onDone={done} />
      <ReviewDialog rec={r} base={base} open={dialog === "review"} onOpenChange={(o) => setDialog(o ? "review" : null)} onDone={done} />
      <ArchiveDialog name={r.name} open={lifecycle === "archive"} onOpenChange={(o) => !o && setLifecycle(null)} onConfirm={onArchive} pending={pending} refusal={refusal || undefined} />
      <DeleteDialog name={r.name} open={lifecycle === "delete"} onOpenChange={(o) => !o && setLifecycle(null)} onConfirm={onDelete} pending={pending} refusal={refusal || undefined}
        onArchiveInstead={() => openLifecycle("archive")} />
    </>
  );
}

// ---- Renewals tab --------------------------------------------------------------------------

function RenewalsTab({ name, load, write }: { name: string; load: ReturnType<typeof useLoad<Renewal[]>>; write: boolean }) {
  if (load.error) return <ErrorBanner message={load.error} onRetry={load.reload} />;
  if (!load.data) return <Loading what="renewals" rows={2} />;
  if (load.data.length === 0) {
    return (
      <EmptyState title="No renewals cover this application" action={write ? <Button asChild variant="outline"><Link to="/renewals/new">Add renewal</Link></Button> : undefined}>
        Renewals whose current cycle covers {name} appear here with their action deadline.
      </EmptyState>
    );
  }
  return (
    <ul className="grid gap-2">
      {load.data.map((x) => (
        <li key={x.id} className="rounded-lg border bg-card px-4 py-3">
          <Link to={`/renewals/${x.id}`} className="font-semibold">{x.name}</Link>
          <span className="text-muted-foreground"> · current cycle renews {x.cycle.renewal_date ?? "on a date not yet recorded"}</span>
          <div className="mt-1"><DeadlineCell deadline={x.deadline} closed={!x.cycle.open} /></div>
          {x.gaps.length > 0 && <div className="mt-1"><GapChips gaps={x.gaps} /></div>}
        </li>
      ))}
    </ul>
  );
}

// ---- Overview tab ---------------------------------------------------------------------

export function OwnerValue({ person, gap, field, assign }: { person: Person | null; gap: string; field: string; assign?: (field: string) => void }) {
  if (!person) return <GapWithAssign label={gap} field={field} assign={assign} />;
  return person.active ? <>{person.name}{person.department && <span className="text-muted-foreground"> ({person.department.name})</span>}</>
    : <GapWithAssign label={`${person.name} (inactive)`} field={field} assign={assign} />;
}

export function GapWithAssign({ label, field, assign }: { label: string; field: string; assign?: (field: string) => void }) {
  return (
    <span className="flex flex-wrap items-center gap-2">
      <Gap label={label} />
      {assign && <Button size="sm" variant="outline" onClick={() => assign(field.replace(" ", "_"))} aria-label={`Assign ${field}`}>Assign</Button>}
    </span>
  );
}

function Overview({ r, kind, reviews, write, onEdit, onHistory, onCopy, onReload }: {
  r: CatalogRecord; kind: Kind; reviews: ReturnType<typeof useLoad<Review[]>>; write: boolean;
  onEdit: (field?: string) => void; onHistory: () => void; onCopy: () => void; onReload: (notice: string) => void;
}) {
  const me = useMe();
  const assign = write ? onEdit : undefined;
  return (
    <div className="grid gap-4">
      <div className="grid items-start gap-4 md:grid-cols-2">
        <Panel id="h-own" icon={IconOwners} title="Ownership (confirmed fields)"
          action={write && <Button size="sm" variant="outline" onClick={() => onEdit()}>Edit ownership</Button>}>
          <DetailList className="ownership">
            <DetailRow label="Business owner"><OwnerValue person={r.business_owner} gap="No business owner" field="business owner" assign={assign} /></DetailRow>
            <DetailRow label="Technical owner"><OwnerValue person={r.technical_owner} gap="No technical owner" field="technical owner" assign={assign} /></DetailRow>
            <DetailRow label="Support team">{r.support_team?.name ?? <GapWithAssign label="No support team" field="support team" assign={assign} />}</DetailRow>
            <DetailRow label="Lifecycle"><LifecycleChip value={r.lifecycle} /></DetailRow>
            <DetailRow label="Criticality">{r.criticality ? <CriticalityMark value={r.criticality} /> : <GapWithAssign label="No criticality" field="criticality" assign={assign} />}</DetailRow>
          </DetailList>
          <DetailList className="steward border-t">
            <DetailRow label="IT steward">{r.it_steward?.name ?? "—"}</DetailRow>
          </DetailList>
        </Panel>
        {kind === "component" ? (
          <Panel id="h-runtime" icon={IconRuntime} title="Runtime">
            <DetailList>
              <DetailRow label="Component type">{typeLabel(r.component_type)}</DetailRow>
              <DetailRow label="Host / runtime">{r.host_runtime || dash}</DetailRow>
              <DetailRow label="Hosting">{choice(HOSTINGS, r.hosting)}</DetailRow>
              <DetailRow label="Environment">{choice(RECORD_ENVIRONMENTS, r.environment)}</DetailRow>
              <DetailRow label="Schedule or trigger">{r.schedule_or_trigger || dash}</DetailRow>
              <DetailRow label="Repository"><ExtLink url={r.repository_url} /></DetailRow>
              <DetailRow label="Documentation"><ExtLink url={r.documentation_url} /></DetailRow>
              <DetailRow label="Runbook"><ExtLink url={r.runbook_url} /></DetailRow>
              <DetailRow label="Built by">{plain(r.built_by)}</DetailRow>
              <DetailRow label="License model">{plain(r.license_model)}</DetailRow>
            </DetailList>
          </Panel>
        ) : <ReviewCard reviews={reviews} onHistory={onHistory} />}
      </div>
      {kind === "component" && <ReviewCard reviews={reviews} onHistory={onHistory} />}
      {kind === "ai_agent" && (
        <div className="grid items-start gap-4 md:grid-cols-2">
          <Panel id="h-agent" icon={IconAgent} title="Agent">
            <DetailList>
              <DetailRow label="Platform">{choice(AGENT_PLATFORMS, r.platform)}</DetailRow>
              <DetailRow label="Models">
                {r.models_used?.length ? (
                  <ul className="flex flex-col gap-0.5">{r.models_used.map((m) => <li key={m.id}>{modelName(m)}</li>)}</ul>
                ) : <span className="text-muted-foreground">None recorded</span>}
              </DetailRow>
              <DetailRow label="Channels"><ChannelChips channels={r.channels} /></DetailRow>
              <DetailRow label="Environment">{choice(RECORD_ENVIRONMENTS, r.environment)}</DetailRow>
              <DetailRow label="Auth mode">{choice(AGENT_AUTH_MODES, r.auth_mode)}</DetailRow>
              <DetailRow label="Run-as identity">{r.run_as_identity || dash}</DetailRow>
              <DetailRow label="Risk classification">{choice(AGENT_RISKS, r.risk_classification)}</DetailRow>
            </DetailList>
          </Panel>
          <Panel id="h-governance" icon={IconShield} title="Governance">
            <DetailList>
              <DetailRow label="Data access scope">{r.data_access_scope ? <span className="whitespace-pre-line">{r.data_access_scope}</span> : <Gap label="No data access scope" />}</DetailRow>
              <DetailRow label="Human in the loop">{r.human_in_loop == null ? <Gap label="No human-in-loop decision" /> : <TriText value={r.human_in_loop} yes="Yes, a person approves" no="No, it acts alone" />}</DetailRow>
              <DetailRow label="Actions can write"><TriText value={r.actions_can_write} yes="Yes, it can write" no="No, read only" /></DetailRow>
              <DetailRow label="Knowledge sources">{r.knowledge_sources ? <span className="whitespace-pre-line">{r.knowledge_sources}</span> : dash}</DetailRow>
              <DetailRow label="Tools and connectors">{r.tools_connectors ? <span className="whitespace-pre-line">{r.tools_connectors}</span> : dash}</DetailRow>
            </DetailList>
          </Panel>
        </div>
      )}
      {kind !== "ai_agent" && me.capabilities.includes("eol") && (
        <LazyPart what="the end-of-life panel">
          <EndOfLifePanel kind={kind} id={r.id} name={r.name} version={kind === "application" ? r.version : undefined} block={r.end_of_life} write={write} onChanged={onReload} />
        </LazyPart>
      )}
      {r.sso_coverage && <LazyPart what="the SSO and MFA coverage"><CoveragePanel id={r.id} name={r.name} block={r.sso_coverage} write={write} /></LazyPart>}
      <Panel id="h-details" icon={IconDetails} title="Details">
        <DetailList>
          <DetailRow label="Purpose">{r.purpose || dash}</DetailRow>
          {kind === "application" && <>
            {/* A gap-relevant field left blank is its gap chip; the dash only means "not applicable". */}
            <DetailRow label="Vendor">{r.vendor?.name ?? (r.built_by === "internal" ? dash : <Gap label="No vendor" />)}</DetailRow>
            <DetailRow label="Category">{r.category?.name ?? dash}</DetailRow>
            <DetailRow label="Deployment model">{choice(DEPLOYMENT_MODELS, r.deployment_model)}</DetailRow>
            <DetailRow label="Version">{r.version || (r.deployment_model === "saas" ? dash : <span className="text-muted-foreground">Not recorded</span>)}</DetailRow>
            <DetailRow label="Authentication method">{r.authentication_method ? labelOf(AUTH_METHODS, r.authentication_method) : <Gap label="No authentication method" />}</DetailRow>
            <DetailRow label="Single sign-on (SSO)"><TriText value={r.sso_enabled} /></DetailRow>
            <DetailRow label="SCIM provisioning"><TriText value={r.scim_provisioning} /></DetailRow>
            <DetailRow label="Primary URL"><ExtLink url={r.primary_url} /></DetailRow>
            <DetailRow label="Admin URL"><ExtLink url={r.admin_url} /></DetailRow>
            <DetailRow label="Documentation"><ExtLink url={r.documentation_url} /></DetailRow>
            <DetailRow label="Support"><ExtLink url={r.support_url} /></DetailRow>
            <DetailRow label="Built by">{plain(r.built_by)}</DetailRow>
            <DetailRow label="License model">{plain(r.license_model)}</DetailRow>
            <DetailRow label="Entra application ID">{r.entra_app_id ? <code className="font-mono text-[13px] [overflow-wrap:anywhere]">{r.entra_app_id}</code> : dash}</DetailRow>
            <DetailRow label="Service principal ID">{r.service_principal_id ? <code className="font-mono text-[13px] [overflow-wrap:anywhere]">{r.service_principal_id}</code> : dash}</DetailRow>
          </>}
          {kind === "component" && <>
            <DetailRow label="Azure resource ID">{r.azure_resource_id ? <code className="font-mono text-[13px] [overflow-wrap:anywhere]">{r.azure_resource_id}</code> : dash}</DetailRow>
          </>}
          {kind !== "application" && <>
            <DetailRow label="Power Platform environment ID">{r.pp_environment_id || dash}</DetailRow>
            <DetailRow label="External ID">{r.external_id || dash}</DetailRow>
          </>}
          {kind === "ai_agent" && <DetailRow label="Repository"><ExtLink url={r.repo_url} /></DetailRow>}
          <DetailRow label="Department">{r.department?.name ?? dash}</DetailRow>
          <DetailRow label="Cost center">{r.cost_center || dash}</DetailRow>
          <DetailRow label="Data classification">{choice(DATA_CLASSIFICATIONS, r.data_classification)}</DetailRow>
          <DetailRow label="Tags">{r.tags?.length ? <TagChips tags={r.tags} /> : dash}</DetailRow>
          <DetailRow label="Softwarden ID"><SoftwardenId id={r.id} onCopy={onCopy} /></DetailRow>
        </DetailList>
      </Panel>
      <CustomFieldsPanel kind={kind} values={r.custom_field_data} />
      {/* The compliance profile: its keys are in the record only while the organization has it on. */}
      {"is_gxp" in r && (
        <Panel id="h-compliance" icon={IconShield} title="Compliance">
          <DetailList>
            <DetailRow label="GxP relevant"><TriText value={r.is_gxp} /></DetailRow>
            <DetailRow label="Validation status">{choice(VALIDATION_STATUSES, r.validation_status)}</DetailRow>
            <DetailRow label="Validation document"><ExtLink url={r.validation_doc_url ?? undefined} /></DetailRow>
            <DetailRow label="Risk tier">{choice(RISK_TIERS, r.risk_tier)}</DetailRow>
            <DetailRow label="Last periodic review">{r.last_periodic_review ? <time dateTime={r.last_periodic_review} className="tabular">{r.last_periodic_review}</time> : dash}</DetailRow>
            <DetailRow label="Next periodic review">{r.next_periodic_review ? <time dateTime={r.next_periodic_review} className="tabular">{r.next_periodic_review}</time> : dash}</DetailRow>
          </DetailList>
        </Panel>
      )}
      <DiscoverySources id={r.id} kind={kind} name={r.name} write={write} />
    </div>
  );
}

function SoftwardenId({ id, onCopy }: { id: string; onCopy: () => void }) {
  return (
    <span className="flex flex-wrap items-center gap-2">
      <code className="font-mono text-[13px]" title={id}>{id.slice(0, 4)}…{id.slice(-4)}<span className="sr-only"> (full ID {id})</span></code>
      <Button size="sm" variant="outline" onClick={onCopy}>Copy<span className="sr-only"> Softwarden ID</span></Button>
    </span>
  );
}

function ReviewCard({ reviews, onHistory }: { reviews: ReturnType<typeof useLoad<Review[]>>; onHistory: () => void }) {
  const latest = reviews.data?.[0];
  return (
    <Panel id="h-review" icon={IconReview} title="Review">
      {reviews.error ? <div className="px-4 pt-4"><ErrorBanner message={reviews.error} onRetry={reviews.reload} /></div>
        : !reviews.data ? <div className="px-4 py-3"><Loading what="review" /></div>
        : !latest ? <p className="px-4 py-3 text-muted-foreground">Never reviewed. Record a review once an owner has confirmed the details through your usual channel.</p>
        : (
          <DetailList>
            <DetailRow label="Confirmed"><time dateTime={latest.confirmation_date} className="tabular">{latest.confirmation_date}</time></DetailRow>
            <DetailRow label="Confirmed by">{latest.confirmed_by.name}</DetailRow>
            <DetailRow label="Recorded by">{latest.recorded_by.name}</DetailRow>
            <DetailRow label="Evidence">{latest.evidence}{latest.inactive_reason && <span className="text-muted-foreground"> · Reason: {latest.inactive_reason}</span>}</DetailRow>
            <DetailRow label="Next review"><time dateTime={latest.next_review_date} className="tabular">{latest.next_review_date}</time></DetailRow>
          </DetailList>
        )}
      {reviews.data && reviews.data.length > 0 && (
        <div className="border-t px-4 py-3"><Button size="sm" variant="outline" onClick={onHistory}>View history</Button></div>
      )}
    </Panel>
  );
}

// ---- Instances and Review history tabs ------------------------------------------------------

const STATUS: Record<Instance["status"], { variant: "confirmed" | "pending" | "overdue" | "neutral"; glyph: string; label: string }> = {
  operational: { variant: "confirmed", glyph: "✓", label: "Operational" },
  degraded: { variant: "pending", glyph: "◐", label: "Degraded" },
  offline: { variant: "overdue", glyph: "!", label: "Offline" },
  unknown: { variant: "neutral", glyph: "?", label: "Unknown" },
};

function InstancesTab({ id, appName, write, onChanged }: { id: string; appName: string; write: boolean; onChanged: () => void }) {
  const load = useLoad<Instance[]>(`applications/${id}/instances`);
  const [editing, setEditing] = useState<Instance | "new" | null>(null);
  const [deleting, setDeleting] = useState<Instance | null>(null);
  const [notice, setNotice] = useState("");
  const editFocus = useReturnFocus(editing !== null);
  const deleteFocus = useReturnFocus(deleting !== null);
  const rows = load.data;
  function done(text: string) { setEditing(null); setDeleting(null); setNotice(text); load.reload(); onChanged(); }
  if (load.error) return <ErrorBanner message={load.error} onRetry={load.reload} />;
  if (!rows) return <Loading what="instances" rows={3} />;
  const add = write && <Button variant="outline" onClick={() => setEditing("new")}>Add instance</Button>;
  return (
    <>
      <SuccessNote text={notice} />
      {rows.length === 0 ? (
        <EmptyState title="No instances recorded" action={add}>Instances are optional; add one if this application has separate production and sandbox deployments.</EmptyState>
      ) : <>
        {add && <div className="mb-3 flex justify-end">{add}</div>}
        <TableRegion label="Instances table">
          <Table>
            <TableCaption className="sr-only">Instances, in the order they were added</TableCaption>
            <TableHeader><TableRow><TableHead>Name</TableHead><TableHead>Environment</TableHead><TableHead>URL</TableHead><TableHead>Status</TableHead>{write && <TableHead><span className="sr-only">Actions</span></TableHead>}</TableRow></TableHeader>
            <TableBody>
              {rows.map((i) => {
                const s = STATUS[i.status] ?? STATUS.unknown;
                return (
                  <TableRow key={i.id} className="h-[52px]">
                    <TableHead scope="row" className="whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">{i.name}</TableHead>
                    <TableCell>{i.environment ? cap(i.environment) : "—"}</TableCell>
                    <TableCell className="whitespace-normal"><ExtLink url={i.url} /></TableCell>
                    <TableCell><span className={chip({ state: s.variant })}><span aria-hidden="true">{s.glyph}</span>{s.label}</span></TableCell>
                    {write && (
                      <TableCell className="space-x-2 text-right">
                        <Button size="sm" variant="outline" onClick={() => setEditing(i)}>Edit<span className="sr-only"> {i.name}</span></Button>
                        <Button size="sm" variant="outline" onClick={() => setDeleting(i)}>Delete<span className="sr-only"> {i.name}</span></Button>
                      </TableCell>
                    )}
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableRegion>
      </>}
      <Dialog open={editing !== null} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent className="max-w-lg" {...editFocus}>
          <DialogHeader>
            <DialogTitle>{editing === "new" ? `Add instance — ${appName}` : `Edit instance — ${editing?.name ?? ""}`}</DialogTitle>
            <DialogDescription>One deployment of {appName}, such as Production or Sandbox. Names are unique per application.</DialogDescription>
          </DialogHeader>
          {editing === "new" && <InstanceForm appId={id} appName={appName} instance={null} onCancel={() => setEditing(null)} onDone={done} />}
          {editing && editing !== "new" && (
            <OpenedEdit<Instance> path={`applications/${id}/instances/${editing.id}`} what="instance">
              {(instance, write) => <InstanceForm appId={id} appName={appName} instance={instance} write={write} onCancel={() => setEditing(null)} onDone={done} />}
            </OpenedEdit>
          )}
        </DialogContent>
      </Dialog>
      <Dialog open={deleting !== null} onOpenChange={(o) => !o && setDeleting(null)}>
        <DialogContent className="max-w-md" {...deleteFocus}>
          <DialogHeader>
            <DialogTitle>Delete {deleting?.name ?? ""}?</DialogTitle>
            <DialogDescription>The instance is removed from {appName}. This can't be undone.</DialogDescription>
          </DialogHeader>
          {deleting && <InstanceDelete appId={id} instance={deleting} onCancel={() => setDeleting(null)} onDone={done} />}
        </DialogContent>
      </Dialog>
    </>
  );
}

const ENVIRONMENTS = ["Production", "Sandbox", "Implementation", "Test", "Development"];

function InstanceForm({ appId, appName, instance, write, onCancel, onDone }: {
  appId: string; appName: string; instance: Instance | null; write?: OpenedWrite; onCancel: () => void; onDone: (t: string) => void;
}) {
  const [form, setForm] = useState({ name: instance?.name ?? "", environment: instance?.environment ?? "", status: instance?.status ?? "unknown", url: instance?.url ?? "" });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  // The API reports the name as `name`; the field id is instance-name so it can't clash with ids elsewhere on the page.
  const { name: nameErrors, ...rest } = errors;
  const shown = nameErrors ? { "instance-name": nameErrors, ...rest } : rest;
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name.trim()) return setErrors({ name: ["Enter a name."] });
    const path = `applications/${appId}/instances${instance ? `/${instance.id}` : ""}`;
    const failed = await attempt(async () => api(path, { method: instance ? "PATCH" : "POST", body: form, headers: await write?.ifMatch() }));
    if (failed?.status === 412 && write) return write.onStale();
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { name: [failed.detail] });
    onDone(instance ? `${form.name.trim()} saved.` : `${form.name.trim()} added to ${appName}.`);
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={shown} />
      <Field id="instance-name" label="Name (required)" errors={shown}>
        <Input id="instance-name" value={form.name} onChange={set("name")} autoComplete="off" />
      </Field>
      <Field id="environment" label="Environment" errors={shown} help="For example Production, Sandbox or Implementation.">
        <Input id="environment" list="environment-options" value={form.environment} onChange={set("environment")} autoComplete="off" />
      </Field>
      <datalist id="environment-options">{ENVIRONMENTS.map((e) => <option key={e} value={e} />)}</datalist>
      <Field id="status" label="Status" errors={shown}>
        <Select id="status" value={form.status} onChange={set("status")}>
          {(Object.keys(STATUS) as Instance["status"][]).map((s) => <option key={s} value={s}>{STATUS[s].label}</option>)}
        </Select>
      </Field>
      <Field id="url" label="URL" errors={shown}>
        <Input id="url" type="url" inputMode="url" placeholder="https://…" value={form.url} onChange={set("url")} />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit">{instance ? "Save" : "Add instance"}</Button>
      </DialogFooter>
    </form>
  );
}

function InstanceDelete({ appId, instance, onCancel, onDone }: { appId: string; instance: Instance; onCancel: () => void; onDone: (t: string) => void }) {
  const [error, setError] = useState("");
  // Read with its ETag as the confirmation opens; the delete sends it as If-Match.
  const opened = useOpenedRecord(`applications/${appId}/instances/${instance.id}`);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const failed = await attempt(async () => api(`applications/${appId}/instances/${instance.id}`, { method: "DELETE", headers: ifMatch(await opened.etag()) }));
    if (failed?.status === 412) return setError(staleDeleteText("instance"));
    if (failed) return setError(failed.detail);
    onDone(`${instance.name} deleted.`);
  }
  return (
    <form onSubmit={submit} noValidate>
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="destructive">Delete {instance.name}</Button>
      </DialogFooter>
    </form>
  );
}

function HistoryTab({ load }: { load: ReturnType<typeof useLoad<Review[]>> }) {
  if (load.error) return <ErrorBanner message={load.error} onRetry={load.reload} />;
  if (!load.data) return <Loading what="review history" rows={3} />;
  if (load.data.length === 0) {
    return <EmptyState title="Never reviewed">Record a review once an owner has confirmed the details through your usual channel.</EmptyState>;
  }
  return (
    <TableRegion label="Review history table">
      <Table>
        <TableCaption className="sr-only">Review history, newest first. Reviews can't be edited.</TableCaption>
        <TableHeader><TableRow>
          <TableHead aria-sort="descending">Confirmed</TableHead><TableHead>Confirmed by</TableHead><TableHead>Recorded by</TableHead>
          <TableHead>Evidence / reference</TableHead><TableHead>Next review</TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {load.data.map((v) => (
            <TableRow key={v.id} className="h-[52px]">
              <TableHead scope="row" className="tabular font-semibold text-foreground"><time dateTime={v.confirmation_date}>{v.confirmation_date}</time></TableHead>
              <TableCell>{v.confirmed_by.name}{!v.confirmed_by.active && <span className="text-muted-foreground"> (inactive)</span>}</TableCell>
              <TableCell>{v.recorded_by.name}</TableCell>
              <TableCell className="max-w-sm whitespace-normal [overflow-wrap:anywhere]">{v.evidence}{v.inactive_reason && <span className="text-muted-foreground"> · Reason: {v.inactive_reason}</span>}</TableCell>
              <TableCell className="tabular"><time dateTime={v.next_review_date}>{v.next_review_date}</time></TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

// ---- Assign owners and Record review dialogs (moved from pages.tsx; `base` picks applications or components) ------------

type DialogProps = { rec: CatalogRecord; base: string; open: boolean; onOpenChange: (open: boolean) => void; onDone: (message: string, undo?: UndoSpec) => void };

/** What the owner dialog edits: an application, a component or an integration (which has no business owner or criticality). */
export type OwnerTarget = Pick<CatalogRecord, "id" | "name" | "technical_owner" | "support_team" | "it_steward"> & Partial<Pick<CatalogRecord, "business_owner" | "criticality" | "review_state">>;

export function OwnersDialog({ rec, base, focus, open, onOpenChange, onDone }: { rec: OwnerTarget; base: string; focus: string; open: boolean; onOpenChange: (open: boolean) => void; onDone: (message: string, undo?: UndoSpec) => void }) {
  const returnFocus = useReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Assign owners — {rec.name}</DialogTitle>
          <DialogDescription>{base === "integrations"
            ? "Only active people are offered. Changing the technical owner makes a confirmed link Needs verification."
            : "Only active people are offered. Changing a confirmed value marks a reviewed record Needs confirmation."}</DialogDescription>
        </DialogHeader>
        {open && <OwnersForm rec={rec} base={base} focus={focus} onCancel={() => onOpenChange(false)} onDone={onDone} />}
      </DialogContent>
    </Dialog>
  );
}

type OwnerKey = "business_owner" | "technical_owner" | "support_team" | "criticality" | "it_steward";
/** A save the Undo note can reverse: the previous values, and the values that must still be current. */
export type UndoSpec = { restore: Record<string, string | null>; expected: Record<string, string | null> };

const OWNER_LABELS: Record<OwnerKey, string> = {
  business_owner: "Business owner", technical_owner: "Technical owner", support_team: "Support team", criticality: "Criticality", it_steward: "IT steward",
};
const CONFIRMED_KEYS: OwnerKey[] = ["business_owner", "technical_owner", "support_team", "criticality"];

function ownerValues(rec: OwnerTarget): Record<OwnerKey, string> {
  return {
    business_owner: rec.business_owner?.id ?? "", technical_owner: rec.technical_owner?.id ?? "", support_team: rec.support_team?.id ?? "",
    criticality: rec.criticality ?? "", it_steward: rec.it_steward ? String(rec.it_steward.id) : "",
  };
}

/** Reads the record again with its ETag, so the save sends If-Match: a change someone else saved since the page loaded
 *  is refused (412) rather than overwritten, and "Load the latest version" restarts the form from it. */
function OwnersForm({ rec, base, focus, onCancel, onDone }: {
  rec: OwnerTarget; base: string; focus: string; onCancel: () => void; onDone: (m: string, undo?: UndoSpec) => void;
}) {
  const path = `${base}/${rec.id}`;
  const fresh = useQuery({ queryKey: [...apiKey(path), "etag"], queryFn: () => apiWithEtag<OwnerTarget>(path), gcTime: 0, staleTime: Infinity, retry: false });
  const [round, setRound] = useState(0);
  const [stale, setStale] = useState(false);
  if (fresh.error) return <ErrorBanner message={fresh.error instanceof Problem ? fresh.error.detail : "Couldn't load the record."} onRetry={() => void fresh.refetch()} />;
  if (!fresh.data) return <Loading what="the record" />;
  async function loadLatest() {
    await fresh.refetch();
    setStale(false);
    setRound((n) => n + 1);
  }
  return (
    <>
      {stale && (
        <div className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">
          <p className="font-semibold">Someone else changed {rec.name} after this dialog opened, so your change wasn't saved.</p>
          <Button type="button" size="sm" variant="outline" className="mt-2" onClick={() => void loadLatest()}>Load the latest version</Button>
        </div>
      )}
      <OwnersFormBody key={round} rec={fresh.data.data} etag={fresh.data.etag} base={base} focus={focus} onCancel={onCancel} onDone={onDone} onStale={() => setStale(true)} />
    </>
  );
}

function OwnersFormBody({ rec, etag, base, focus, onCancel, onDone, onStale }: {
  rec: OwnerTarget; etag: string | null; base: string; focus: string; onCancel: () => void; onDone: (m: string, undo?: UndoSpec) => void; onStale: () => void;
}) {
  const integration = base === "integrations";
  const people = useLoad<Person[]>("people?active=true");
  const teams = useLoad<Named[]>("teams");
  const stewards = useLoad<UserRef[]>("stewards");
  const departments = useLoad<Named[]>("departments");
  const [before] = useState(() => ownerValues(rec));
  const [form, setForm] = useState(before);
  const [added, setAdded] = useState<Person[]>([]);
  const [adding, setAdding] = useState<{ field: "business_owner" | "technical_owner"; name: string; email: string; department: string } | null>(null);
  const [addError, setAddError] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const set = (k: OwnerKey, v: string) => setForm((f) => ({ ...f, [k]: v }));
  const ready = !!people.data && !!teams.data;
  // Opened from a gap's Assign: start on that field.
  useEffect(() => { if (ready && focus) document.getElementById(focus)?.focus(); }, [ready, focus]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const changed = (Object.keys(form) as OwnerKey[]).filter((k) => form[k] !== before[k]);
    if (!changed.length) return onCancel();
    const body = Object.fromEntries(changed.map((k) => [k, form[k] || null]));
    let refused = false;
    const failed = await attempt(() => api(`${base}/${rec.id}`, { method: "PATCH", body, headers: etag ? { "If-Match": etag } : undefined }).catch((e) => {
      refused = e instanceof Problem && e.status === 412;
      throw e;
    }));
    if (refused) return onStale();
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { [integration ? "technical_owner" : "business_owner"]: [failed.detail] });
    const nameOf = (k: OwnerKey) => {
      const v = form[k];
      if (!v) return "not set";
      if (k === "support_team") return teams.data?.find((t) => t.id === v)?.name ?? "the chosen team";
      if (k === "it_steward") return stewards.data?.find((u) => String(u.id) === v)?.name ?? "the chosen steward";
      if (k === "criticality") return cap(v);
      return allPeople.find((p) => p.id === v)?.name ?? "the chosen person";
    };
    const what = changed.length === 1 ? `${OWNER_LABELS[changed[0]]} set to ${nameOf(changed[0])}.` : "Ownership saved.";
    const reconfirm = integration ? ""
      : rec.review_state === "confirmed" && changed.some((k) => CONFIRMED_KEYS.includes(k)) ? " Record is now Needs confirmation." : "";
    onDone(what + reconfirm, {
      restore: Object.fromEntries(changed.map((k) => [k, before[k] || null])),
      expected: Object.fromEntries(changed.map((k) => [k, form[k] || null])),
    });
  }
  async function addPerson() {
    if (!adding) return;
    if (!adding.name.trim()) return setAddError("Enter a name.");
    try {
      const p = await api<Person>("people", { method: "POST", body: { name: adding.name.trim(), email: adding.email.trim(), department: adding.department || null } });
      setAdded((a) => [...a, p]);
      set(adding.field, p.id);
      const field = adding.field;
      setAdding(null);
      setTimeout(() => document.getElementById(field)?.focus());
    } catch (err) {
      setAddError(err instanceof Problem ? (Object.values(err.errors).flat().join(" ") || err.detail) : "Couldn't add the person. Try again.");
    }
  }
  if (!people.data || !teams.data) return <Loading what="people and teams" />;
  const allPeople = [...people.data, ...added];
  const personOptions: ComboOption[] = allPeople.map((p) => ({ id: p.id, label: p.name, hint: [p.department?.name, p.email].filter(Boolean).join(" · ") }));
  const personPicker = (id: "business_owner" | "technical_owner") => (
    <Field id={id} label={OWNER_LABELS[id]} errors={errors}>
      <Combobox id={id} options={personOptions} value={form[id]} onChange={(v) => set(id, v)} placeholder="Search people…" clearLabel="Not set (gap)"
        listLabel={`${OWNER_LABELS[id]} suggestions`}
        add={{ label: (t) => (t ? `+ Add person "${t}"` : "+ Add person"), onAdd: (t) => { setAddError(""); setAdding({ field: id, name: t, email: "", department: "" }); } }} />
    </Field>
  );
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      {!integration && personPicker("business_owner")}
      {personPicker("technical_owner")}
      {adding && (
        <div role="group" aria-labelledby="owner-add-person" className="mb-3 rounded-md border bg-table-head px-3 py-3"
          onKeyDown={(e) => { if (e.key === "Enter" && (e.target as HTMLElement).tagName === "INPUT") { e.preventDefault(); void addPerson(); } }}>
          <h3 id="owner-add-person" className="mb-2 font-semibold">Add person</h3>
          {addError && <p className="mb-2 text-sm text-error-foreground" role="alert">{addError}</p>}
          <Field id="owner-new-name" label="Name of new person (required)"><Input id="owner-new-name" autoFocus value={adding.name} onChange={(e) => setAdding({ ...adding, name: e.target.value })} /></Field>
          <Field id="owner-new-email" label="Email of new person"><Input id="owner-new-email" type="email" value={adding.email} onChange={(e) => setAdding({ ...adding, email: e.target.value })} /></Field>
          <Field id="owner-new-department" label="Department of new person">
            <Select id="owner-new-department" value={adding.department} onChange={(e) => setAdding({ ...adding, department: e.target.value })}>
              <option value="">Not set</option>
              {(departments.data ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </Select>
          </Field>
          <div className="flex gap-2">
            <Button type="button" size="sm" variant="outline" onClick={() => void addPerson()}>Add person</Button>
            <Button type="button" size="sm" variant="outline" onClick={() => { const f = adding.field; setAdding(null); setTimeout(() => document.getElementById(f)?.focus()); }}>Cancel</Button>
          </div>
        </div>
      )}
      <Field id="support_team" label="Support team" errors={errors}>
        <Combobox id="support_team" options={teams.data.map((t) => ({ id: t.id, label: t.name }))} value={form.support_team} onChange={(v) => set("support_team", v)}
          placeholder="Search teams…" clearLabel="Not set (gap)" listLabel="Support team suggestions" />
      </Field>
      {!integration && (
        <Field id="criticality" label="Criticality" errors={errors}>
          <Select id="criticality" value={form.criticality} onChange={(e) => set("criticality", e.target.value)}>
            <option value="">Not set (gap)</option>
            {CRITICALITIES.map((c) => <option key={c} value={c}>{cap(c)}</option>)}
          </Select>
        </Field>
      )}
      <Field id="it_steward" label="IT steward" errors={errors}>
        <Select id="it_steward" value={form.it_steward} onChange={(e) => set("it_steward", e.target.value)}>
          <option value="">None (routes to the IT coordinator)</option>
          {(stewards.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
        </Select>
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit">Save</Button>
      </DialogFooter>
    </form>
  );
}

/** The success note after an ownership save, with Undo for 10 seconds. Announced through its status region. */
export function UndoNote({ text, onUndo, onExpire }: { text: string; onUndo: () => void; onExpire: () => void }) {
  useEffect(() => { const t = setTimeout(onExpire, 10_000); return () => clearTimeout(t); }, [onExpire]);
  return (
    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-md border border-success-border bg-success-bg px-3 py-2 text-success-foreground">
      <span aria-hidden="true">✓</span>
      <p className="min-w-0 flex-1" role="status">{text}</p>
      <Button size="sm" variant="outline" onClick={onUndo}>Undo</Button>
    </div>
  );
}

export function ReviewDialog({ rec, base, open, onOpenChange, onDone }: DialogProps) {
  const returnFocus = useReturnFocus(open);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Record review — {rec.name}</DialogTitle>
          <DialogDescription>You're recording that someone confirmed these values. Owners don't sign in; record how you got the confirmation.</DialogDescription>
        </DialogHeader>
        {open && <ReviewForm rec={rec} base={base} onCancel={() => onOpenChange(false)} onDone={onDone} />}
      </DialogContent>
    </Dialog>
  );
}

function ReviewForm({ rec, base, onCancel, onDone }: { rec: CatalogRecord; base: string; onCancel: () => void; onDone: (m: string) => void }) {
  const people = useLoad<Person[]>("people");
  const today = orgToday(useMe().organization.timezone);
  const [form, setForm] = useState({ confirmed_by: "", confirmation_date: today, evidence: "", inactive_reason: "" });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const chosen = people.data?.find((p) => p.id === form.confirmed_by);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const failed = await attempt(() => api(`${base}/${rec.id}/reviews`, { method: "POST", body: form }));
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { evidence: [failed.detail] });
    onDone("Review recorded.");
  }
  const sorted = [...(people.data ?? [])].sort((x, y) => Number(y.active) - Number(x.active));
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      <DetailList className="mb-4 rounded-md border bg-table-head [&>dd]:py-1.5 [&>dt]:py-1.5">
        <DetailRow label="Business owner">{rec.business_owner?.name ?? <Gap label="No business owner" />}</DetailRow>
        <DetailRow label="Technical owner">{rec.technical_owner?.name ?? <Gap label="No technical owner" />}</DetailRow>
        <DetailRow label="Support team">{rec.support_team?.name ?? <Gap label="No support team" />}</DetailRow>
        <DetailRow label="Lifecycle"><LifecycleChip value={rec.lifecycle} /></DetailRow>
        <DetailRow label="Criticality">{rec.criticality ? <CriticalityMark value={rec.criticality} /> : <Gap label="No criticality" />}</DetailRow>
      </DetailList>
      <Field id="confirmed_by" label="Confirmed by (required)" errors={errors}>
        <Select id="confirmed_by" value={form.confirmed_by} onChange={set("confirmed_by")}>
          <option value="">Choose a person</option>
          {sorted.map((p) => <option key={p.id} value={p.id}>{p.name}{p.active ? "" : " (inactive)"}</option>)}
        </Select>
      </Field>
      {chosen && !chosen.active && (
        <Field id="inactive_reason" label="Reason for using an inactive person (required)" errors={errors}>
          <Input id="inactive_reason" value={form.inactive_reason} onChange={set("inactive_reason")} />
        </Field>
      )}
      <Field id="confirmation_date" label="Confirmation date (required)" errors={errors}>
        <Input id="confirmation_date" type="date" value={form.confirmation_date} max={today} onChange={set("confirmation_date")} />
      </Field>
      <Field id="evidence" label="Evidence / reference (required)" errors={errors} help="The next review defaults to the organization's interval after the confirmation date.">
        <Input id="evidence" value={form.evidence} onChange={set("evidence")} placeholder="For example: email from Dana, 31 Oct" />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit">Record review</Button>
      </DialogFooter>
    </form>
  );
}
