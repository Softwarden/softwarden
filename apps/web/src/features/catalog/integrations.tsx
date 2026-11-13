import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  api, cap, DATA_CATEGORIES, DATA_CLASSIFICATIONS, type DataCategory, Integration, INTEGRATION_AUTH_METHODS, INTEGRATION_DIRECTIONS, labelOf, LinkConfirmation, Page, Person, Problem, Relationship, Team, UserRef, LIFECYCLES,
  type Choice,
} from "@/api";
import { canWrite, savedState, useMe, useLandingNotice } from "@/lib/session";
import { AttachmentsTab, useAttachmentsLabel } from "@/features/attachments/tab";
import { ActivityTab } from "@/features/admin/audit";
import {
  attempt, Breadcrumb, Chip, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, NotFound, PageHead, Select, TableRegion, TabStrip, useLoad, useReturnFocus,
} from "@/components/app";
import { Combobox, ComboOption } from "@/components/combobox";
import { ArchiveDialog, DeleteDialog } from "@/components/lifecycle-dialogs";
import { MenuAction, MoreMenu } from "@/components/more-menu";
import { InstancePicker, PickedRecord, RecordPicker, useInstanceGate } from "@/components/record-picker";
import { DetailList, DetailRow, GapChips, KindTile, LifecycleChip, LinkStateChip, Panel, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { dash, day, ExtLink, GapWithAssign, OwnersDialog, OwnerValue, UndoNote, UndoSpec } from "@/features/catalog/detail";
import { ConfirmationHistory, ConfirmLinkDialog, ConfirmTarget, HistoryDialog } from "@/features/catalog/link-confirmations";
import { useUnsavedGuard } from "@/features/catalog/record-form";
import { confirmTarget, RemoveLink } from "@/features/catalog/relationships";
import { RecordLink } from "@/components/record-link";
import { CustomFieldInputs, CustomFieldsPanel, useCustomFieldForm, useCustomFields, type CustomField } from "@/features/catalog/custom-fields";
import { problemText } from "@/lib/query";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { ANY_REVISION, ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { FilterBar, type FilterField } from "@/components/filter-bar";
import { CheckboxPicker } from "@/components/checkbox-picker";
import { CategoryChips, PersonalDataMark } from "@/features/flows/flow-ui";

/** Integrations: list, detail and create/edit. An integration is a data flow between two records; it is never an
 *  operational dependency. It has a technical owner, a support team and an IT steward, and no business owner or criticality. */

const svg = (children: React.ReactNode) => <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">{children}</svg>;
const IconFlow = svg(<><path d="M2 5h10M9.5 2.5 12 5l-2.5 2.5" /><path d="M14 11H4M6.5 8.5 4 11l2.5 2.5" /></>);
const IconOwners = svg(<><circle cx="6" cy="5.5" r="2.5" /><path d="M1.5 14c.6-2.6 2.3-4 4.5-4s3.9 1.4 4.5 4" /><path d="M11.5 4.5v4M9.5 6.5h4" /></>);
const IconBuild = svg(<><rect x="2" y="3" width="12" height="9" rx="1.5" /><path d="M5 14h6M5.5 6.5 7 8 5.5 9.5M8.5 9.5H11" /></>);

type Named = { id: string; name: string };

function End({ end }: { end: Integration["source"] }) {
  return (
    <>
      <RecordLink record={end} className="underline" />
      {end.kind !== "application" && <span className="text-muted-foreground"> Component</span>}
      {end.instance && <span className="text-muted-foreground"> ({end.instance.name})</span>}
      {end.archived && <Chip className="ml-2 align-middle">Archived</Chip>}
    </>
  );
}

/** A choice value's label, or the dash when it isn't recorded. */
const choiceText = (list: Choice[], value?: string) => (value ? labelOf(list, value) : dash);
/** Arrow between the ends: one way, or both ways when the integration says so (glyph plus words for screen readers). */
function FlowArrow({ direction }: { direction?: string }) {
  return direction === "two_way"
    ? <><span aria-hidden="true">⇄</span><span className="sr-only"> both ways with </span></>
    : <><span aria-hidden="true">→</span><span className="sr-only"> to </span></>;
}

const gapsOf = (i: Pick<Integration, "technical_owner" | "support_team">) => [
  ...(!i.technical_owner || !i.technical_owner.active ? [i.technical_owner ? `${i.technical_owner.name} (inactive)` : "No technical owner"] : []),
  ...(!i.support_team ? ["No support team"] : []),
];

// ---- List -------------------------------------------------------------------------------------------------------

type Preset = "active" | "vendor-native" | "archived";
const ACTIVE_SCOPE = "lifecycle=active,deprecated&archived=false";
const PRESETS: { id: Preset; label: string; query: string }[] = [
  { id: "active", label: "Active", query: ACTIVE_SCOPE },
  { id: "vendor-native", label: "Vendor-native", query: `${ACTIVE_SCOPE}&implemented=false` },
  { id: "archived", label: "Archived", query: "archived=true" },
];
/** Filters on the integration fields: any of the chosen values, comma-separated in the URL and sent as is. */
const FIELD_FILTERS: FilterField[] = [
  { key: "direction", label: "Direction", options: INTEGRATION_DIRECTIONS, input: "choices" },
  { key: "auth_method", label: "Authentication", options: INTEGRATION_AUTH_METHODS, input: "choices" },
  { key: "data_classification", label: "Data classification", options: DATA_CLASSIFICATIONS, input: "choices" },
  // Data-flow map: integrations carrying any of the chosen kinds of data.
  { key: "data_category", label: "Data category", options: DATA_CATEGORIES, input: "choices" },
];

export function IntegrationsList() {
  const [params, setParams] = useSearchParams();
  const me = useMe();
  const preset = (PRESETS.find((p) => p.id === params.get("preset"))?.id ?? "active") as Preset;
  const q = (params.get("q") ?? "").trim();
  const values = Object.fromEntries(FIELD_FILTERS.map((f) => [f.key, params.get(f.key) ?? ""]));
  const paramsRef = useRef(params);
  paramsRef.current = params;
  function update(changes: Record<string, string>, push = !("q" in changes)) {
    const next = new URLSearchParams(paramsRef.current);
    for (const [k, v] of Object.entries(changes)) {
      if (!v || (k === "preset" && v === "active")) next.delete(k); else next.set(k, v);
    }
    setParams(next, { replace: !push });
  }

  const [rows, setRows] = useState<Integration[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [moreError, setMoreError] = useState("");
  const [loadingMore, setLoadingMore] = useState(false);
  const request = useRef(0);
  const fieldQuery = FIELD_FILTERS.filter((f) => values[f.key]).map((f) => `&${f.key}=${encodeURIComponent(values[f.key])}`).join("");
  const query = `${PRESETS.find((p) => p.id === preset)!.query}${q ? `&q=${encodeURIComponent(q)}` : ""}${fieldQuery}`;
  const load = useCallback(() => {
    const mine = ++request.current;
    setError(""); setRows(null); setNext(null);
    api<Page<Integration>>(`integrations?${query}`)
      .then((p) => { if (mine === request.current) { setRows(p.results); setNext(p.next); } })
      .catch((e) => { if (mine === request.current) setError(e instanceof Problem ? e.detail : "Couldn't load integrations."); });
  }, [query]);
  useEffect(load, [load]);
  async function loadMore() {
    if (!next) return;
    const mine = request.current;
    setLoadingMore(true); setMoreError("");
    try {
      const p = await api<Page<Integration>>(`integrations?${query}&cursor=${encodeURIComponent(next)}`);
      if (mine !== request.current) return;
      setRows((r) => [...(r ?? []), ...p.results]);
      setNext(p.next);
    } catch (e) {
      setMoreError(e instanceof Problem ? e.detail : "Couldn't load more.");
    } finally {
      setLoadingMore(false);
    }
  }

  const fieldFiltered = FIELD_FILTERS.some((f) => values[f.key]);
  const filtered = preset !== "active" || q !== "" || fieldFiltered;
  const clear = () => update({ preset: "", q: "", ...Object.fromEntries(FIELD_FILTERS.map((f) => [f.key, ""])) }, true);
  const write = canWrite(me);
  const count = rows?.length ?? 0;
  const label = PRESETS.find((p) => p.id === preset)!.label;
  return (
    <>
      <PageHead title="Integrations" desc="Data flows between applications and components. A data flow is not an operational dependency.">
        {write && <Button asChild><Link to="/integrations/new">Add integration</Link></Button>}
      </PageHead>
      <FilterBar id="list" searchLabel="Search integrations" searchPlaceholder="Name, data purpose or description" search={q} onSearch={(v) => update({ q: v })}
        fields={FIELD_FILTERS} values={values} onChange={(k, v, typed) => update({ [k]: v }, !typed)} filtered={filtered} onClear={clear}
        presets={
          <div role="group" aria-label="Filter" className="inline-flex flex-wrap overflow-hidden rounded-md border border-border-strong bg-card">
            {PRESETS.map((p) => (
              <button key={p.id} type="button" aria-pressed={preset === p.id} onClick={() => update({ preset: preset === p.id ? "" : p.id })}
                className="flex h-8 items-center px-3 text-[13px] text-muted-foreground aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground [&+&]:border-l [&+&]:border-border-strong">
                {p.label}
              </button>
            ))}
          </div>
        } />
      <p className="sr-only" aria-live="polite">{rows ? `Showing ${count} ${count === 1 ? "integration" : "integrations"}${next ? ". More available" : ""}.` : ""}</p>
      {error && <ErrorBanner message={error} onRetry={load} />}
      {!rows && !error && <Loading what="integrations" rows={5} />}
      {rows && rows.length === 0 && (filtered ? (
        <EmptyState title="No integrations match these filters" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>
          {q ? `Nothing matches "${q}" with the ${label} filter${fieldFiltered ? " and the other filters" : ""}. Try a shorter search or clear the filters.`
            : fieldFiltered ? `No integrations in the ${label} filter have the chosen values. An integration whose value isn't recorded never matches.`
              : `No integrations are in the ${label} filter right now.`}
        </EmptyState>
      ) : (
        <EmptyState title="No integrations yet" action={write ? <Button asChild variant="outline"><Link to="/integrations/new">Add integration</Link></Button> : undefined}>
          {write ? "An integration needs a name, a source and a target. Owners can be added later and show as gaps."
            : "Editors and Admins can add integrations. Ask one of them to record the first data flow."}
        </EmptyState>
      ))}
      {rows && rows.length > 0 && (
        <>
          <TableRegion label="Integrations table">
            <Table>
              <TableCaption className="sr-only">Integrations, sorted by name A to Z</TableCaption>
              <TableHeader><TableRow>
                <TableHead aria-sort="ascending">Name</TableHead><TableHead>Data flow</TableHead><TableHead>Protocol</TableHead><TableHead>Authentication</TableHead>
                <TableHead>Link state</TableHead><TableHead>Implemented by</TableHead><TableHead>Gaps</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((i) => (
                  <TableRow key={i.id} className="h-[52px] align-top">
                    <TableHead scope="row" className="min-w-40 max-w-xs whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                      <Link to={`/integrations/${i.id}`}>{i.name}</Link>
                      {i.archived && <Chip className="ml-2 align-middle">Archived</Chip>}
                    </TableHead>
                    <TableCell className="whitespace-normal"><End end={i.source} /> <FlowArrow direction={i.direction} /> <End end={i.target} /></TableCell>
                    <TableCell>{i.protocol || dash}</TableCell>
                    <TableCell className="whitespace-normal">{choiceText(INTEGRATION_AUTH_METHODS, i.auth_method)}</TableCell>
                    <TableCell className="whitespace-normal"><LinkStateChip state={i.link_state} /></TableCell>
                    <TableCell className="whitespace-normal">
                      {i.vendor_native ? <Chip>Vendor-native</Chip> : i.implemented_by.map((c) => c.name).join(", ")}
                    </TableCell>
                    <TableCell className="whitespace-normal">{gapsOf(i).length ? <GapChips gaps={gapsOf(i)} /> : "—"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <p className="text-sm text-muted-foreground">Showing {count} {count === 1 ? "integration" : "integrations"}. Sorted by name, A to Z.</p>
            {next && <Button variant="outline" size="sm" onClick={loadMore} disabled={loadingMore}>{loadingMore ? "Loading…" : "Load more"}</Button>}
            {moreError && <span role="alert" className="text-sm text-error-foreground">{moreError} <button type="button" className="underline" onClick={loadMore}>Retry</button></span>}
          </div>
        </>
      )}
    </>
  );
}

// ---- Detail -----------------------------------------------------------------------------------------------------

type TabId = "overview" | "attachments" | "history" | "activity";

export function IntegrationDetail() {
  const { id } = useParams();
  return <Detail key={id} id={id!} />;
}

function Detail({ id }: { id: string }) {
  const me = useMe();
  const navigate = useNavigate();
  const rec = useLoad<Integration>(`integrations/${id}`);
  const impl = useLoad<Relationship[]>(`relationships?resource=${id}&type=implements`);
  const confirmations = useLoad<LinkConfirmation[]>(`integrations/${id}/confirmations`);
  const attachmentsLabel = useAttachmentsLabel(me, "integration", id);
  const [confirming, setConfirming] = useState<ConfirmTarget | null>(null);
  const [params, setParams] = useSearchParams();
  const [ownersOpen, setOwnersOpen] = useState(false);
  const [ownerFocus, setOwnerFocus] = useState("");
  const [lifecycle, setLifecycle] = useState<"archive" | "delete" | null>(null);
  // The delete confirmation reads the integration with its ETag as it opens; the delete sends it as If-Match.
  const deleteRead = useOpenedRecord(lifecycle === "delete" ? `integrations/${id}` : null);
  const [pending, setPending] = useState(false);
  const [refusal, setRefusal] = useState("");
  // The form's note after a save ("Payroll feed saved.").
  const [toast, setToast, toastQuiet] = useLandingNotice();
  const [actionError, setActionError] = useState("");
  const [undo, setUndo] = useState<{ text: string; spec: UndoSpec } | null>(null);
  const clearUndo = useCallback(() => setUndo(null), []);
  const headingRef = useRef<HTMLDivElement>(null);

  function ownersDone(message: string, spec?: UndoSpec) {
    setOwnersOpen(false);
    setActionError("");
    if (spec) { setToast(""); setUndo({ text: message, spec }); } else { setUndo(null); setToast(message); }
    rec.reload();
  }
  async function onUndo() {
    if (!undo) return;
    const { restore, expected } = undo.spec;
    setUndo(null);
    try {
      // `expected` is the undo's own check (the values it set are still current), so any revision is fine.
      await api(`integrations/${id}`, { method: "PATCH", body: { ...restore, expected, undo: true }, headers: ANY_REVISION });
      setToast("Undone.");
    } catch (e) {
      setActionError(e instanceof Problem ? e.detail : "Couldn't undo. Try again.");
    }
    rec.reload();
  }
  function openLifecycle(which: "archive" | "delete") { setPending(false); setRefusal(""); setLifecycle(which); }
  async function lifecycleRequest(path: string, method: string, message: string, after?: () => void) {
    setPending(true); setRefusal("");
    try {
      await api(path, { method, headers: method === "DELETE" ? ifMatch(await deleteRead.etag()) : undefined });
      setLifecycle(null);
      if (after) return after();
      setToast(message);
      rec.reload();
    } catch (e) {
      if (e instanceof Problem && e.status === 412) setRefusal(staleDeleteText("integration"));
      else if (e instanceof Problem && e.status === 409) setRefusal(e.detail);
      else { setLifecycle(null); setToast(""); setActionError(e instanceof Problem ? e.detail : "Something went wrong. Try again."); }
    } finally {
      setPending(false);
    }
  }
  async function onRestore() {
    setActionError(""); setPending(true);
    try {
      await api(`integrations/${id}/restore`, { method: "POST" });
      setToast("Restored.");
      rec.reload();
      setTimeout(() => headingRef.current?.querySelector<HTMLElement>("h1")?.focus(), 0);
    } catch (e) {
      setToast(""); setActionError(e instanceof Problem ? e.detail : "Something went wrong. Try again.");
    } finally {
      setPending(false);
    }
  }

  if (rec.status === 404) {
    return (
      <>
        <PageHead title="Integration not found" desc="This integration doesn't exist or you don't have access." />
        <Button asChild variant="outline"><Link to="/integrations">Back to Integrations</Link></Button>
      </>
    );
  }
  if (rec.error) return <ErrorBanner message={rec.error} onRetry={rec.reload} />;
  if (!rec.data) return <Loading what="integration" rows={4} />;
  const r = rec.data;
  const write = canWrite(me);
  const isAdmin = me.role === "admin";
  const tabs: { id: TabId; label: string }[] = [
    { id: "overview", label: "Overview & ownership" },
    ...(attachmentsLabel ? [{ id: "attachments" as const, label: attachmentsLabel }] : []),
    { id: "history", label: `Confirmation history${confirmations.data ? ` (${confirmations.data.length})` : ""}` },
    { id: "activity", label: "Audit activity" },
  ];
  const tab = tabs.find((t) => t.id === params.get("tab"))?.id ?? "overview";
  const setTab = (t: TabId) => setParams(t === "overview" ? {} : { tab: t });
  async function copyId() {
    try { await navigator.clipboard.writeText(r.id); setToast("Softwarden ID copied."); } catch { setToast(`Couldn't copy. The Softwarden ID is ${r.id}.`); }
  }
  const actions: MenuAction[] = [
    ...(write ? [r.archived ? { label: "Restore", onSelect: () => void onRestore() } : { label: "Archive", onSelect: () => openLifecycle("archive") }] : []),
    ...(isAdmin ? [{ label: "Delete", danger: true, onSelect: () => openLifecycle("delete") }] : []),
    { label: "Copy Softwarden ID", onSelect: () => void copyId() },
  ];
  const gaps = gapsOf(r);
  const assign = write ? (field: string) => { setOwnerFocus(field); setOwnersOpen(true); } : undefined;
  return (
    <>
      <Breadcrumb trail={[{ to: "/integrations", label: "Integrations" }]} current={r.name} />
      <div ref={headingRef}><PageHead title={<span className="[overflow-wrap:anywhere]">{r.name}</span>} docTitle={r.name} desc={
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
          <KindTile kind="integration" /><span>{["Integration", ...(r.protocol ? [r.protocol] : [])].join(" · ")}</span>
          <LifecycleChip value={r.lifecycle} />
          {r.vendor_native && <Chip>Vendor-native</Chip>}
          {r.archived && <Chip>Archived</Chip>}
        </span>
      }>
        <MoreMenu actions={actions} />
        {write && <Button asChild variant="outline"><Link to={`/integrations/${r.id}/edit`}>Edit<span className="sr-only"> {r.name}</span></Link></Button>}
        {write && <Button onClick={() => setConfirming({ kind: "integration", id: r.id, sentence: `${r.name}: ${r.source.name} to ${r.target.name}`, state: r.link_state, why: r.link_why })}>Confirm link</Button>}
      </PageHead></div>
      {actionError && <ErrorBanner message={actionError} />}
      {r.archived && (
        <div className="mb-5 flex flex-wrap items-center gap-3 rounded-md border border-border-strong bg-muted px-4 py-3" role="region" aria-label="Archived integration">
          <p className="min-w-0 flex-1">Archived{r.archived_at && ` on ${day(r.archived_at)}`}{r.archived_by && ` by ${r.archived_by.name}`}. Hidden from day-to-day views; history and links kept.</p>
          {write && <Button variant="outline" size="sm" disabled={pending} onClick={() => void onRestore()}>Restore</Button>}
        </div>
      )}

      <div className="mb-5 grid overflow-hidden rounded-lg border bg-card sm:grid-cols-2" role="group" aria-label="Integration status">
        <div className="flex flex-col gap-1.5 px-4 py-3 sm:border-r">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Link state</span>
          <span className="flex flex-wrap items-center gap-2"><LinkStateChip state={r.link_state} /><span className="text-sm text-muted-foreground">{r.link_why}</span></span>
        </div>
        <div className="flex flex-col gap-1.5 border-t px-4 py-3 sm:border-t-0">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Ownership</span>
          <span className="flex flex-wrap items-center gap-1">
            {gaps.length ? <><span className="mr-1 text-sm">{gaps.length} {gaps.length === 1 ? "gap" : "gaps"}:</span><GapChips gaps={gaps} /></> : <span className="text-sm text-state-confirmed-foreground">✓ Complete</span>}
          </span>
        </div>
      </div>
      {undo ? <UndoNote text={undo.text} onUndo={() => void onUndo()} onExpire={clearUndo} /> : <SuccessNote text={toast} quiet={toastQuiet} />}

      <TabStrip tabs={tabs} tab={tab} onSelect={setTab} label={`${r.name} sections`} />
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "overview" && (
          <div className="grid gap-4">
            <div className="grid items-start gap-4 md:grid-cols-2">
              <Panel id="h-flow" icon={IconFlow} title="Data flow" note="data flow, not operational">
                <DetailList>
                  <DetailRow label="Source"><End end={r.source} /></DetailRow>
                  <DetailRow label="Target"><End end={r.target} /></DetailRow>
                  <DetailRow label="Data purpose">{r.data_purpose || dash}</DetailRow>
                  <DetailRow label="Protocol">{r.protocol || dash}</DetailRow>
                  <DetailRow label="Schedule">{r.schedule || dash}</DetailRow>
                  <DetailRow label="Direction">{choiceText(INTEGRATION_DIRECTIONS, r.direction)}</DetailRow>
                  <DetailRow label="Authentication">{choiceText(INTEGRATION_AUTH_METHODS, r.auth_method)}</DetailRow>
                  <DetailRow label="Data classification">{choiceText(DATA_CLASSIFICATIONS, r.data_classification)}</DetailRow>
                  <DetailRow label="Data categories">
                    <span className="flex flex-wrap items-center gap-1.5" data-testid="integration-categories">
                      <CategoryChips categories={r.data_categories} empty={<span className="text-muted-foreground">None recorded</span>} />
                      <PersonalDataMark on={r.contains_personal_data} />
                    </span>
                  </DetailRow>
                  <DetailRow label="Documentation"><ExtLink url={r.documentation_url} /></DetailRow>
                  <DetailRow label="Monitoring"><ExtLink url={r.monitoring_url} /></DetailRow>
                </DetailList>
              </Panel>
              <Panel id="h-own" icon={IconOwners} title="Ownership" action={write && <Button size="sm" variant="outline" onClick={() => { setOwnerFocus(""); setOwnersOpen(true); }}>Edit ownership</Button>}>
                <DetailList className="ownership">
                  <DetailRow label="Technical owner"><OwnerValue person={r.technical_owner as Person | null} gap="No technical owner" field="technical owner" assign={assign} /></DetailRow>
                  <DetailRow label="Support team">{(r.support_team as Team | null)?.name ?? <GapWithAssign label="No support team" field="support team" assign={assign} />}</DetailRow>
                  <DetailRow label="Lifecycle"><LifecycleChip value={r.lifecycle} /></DetailRow>
                </DetailList>
                <DetailList className="steward border-t">
                  <DetailRow label="IT steward">{(r.it_steward as UserRef | null)?.name ?? "—"}</DetailRow>
                </DetailList>
              </Panel>
            </div>
            <ImplementedBy integration={r} impl={impl} write={write} onChanged={() => { rec.reload(); impl.reload(); }} />
            {r.description && (
              <Panel id="h-desc" icon={IconBuild} title="Description"><p className="px-4 py-3">{r.description}</p></Panel>
            )}
            <CustomFieldsPanel kind="integration" values={r.custom_field_data} />
          </div>
        )}
        {tab === "attachments" && <AttachmentsTab parent={{ type: "integration", id: r.id, name: r.name }} />}
        {tab === "history" && <ConfirmationHistory load={confirmations} />}
        {tab === "activity" && <ActivityTab base="integrations" id={r.id} />}
      </div>

      <ConfirmLinkDialog target={confirming} onClose={() => setConfirming(null)} onDone={(m) => { setConfirming(null); setUndo(null); setToast(m); rec.reload(); confirmations.reload(); }} />
      <OwnersDialog rec={r} base="integrations" focus={ownerFocus} open={ownersOpen} onOpenChange={setOwnersOpen} onDone={ownersDone} />
      <ArchiveDialog name={r.name} open={lifecycle === "archive"} onOpenChange={(o) => !o && setLifecycle(null)} onConfirm={() => lifecycleRequest(`integrations/${id}/archive`, "POST", "Archived.")} pending={pending} refusal={refusal || undefined} />
      <DeleteDialog name={r.name} open={lifecycle === "delete"} onOpenChange={(o) => !o && setLifecycle(null)} pending={pending} refusal={refusal || undefined}
        onConfirm={() => lifecycleRequest(`integrations/${id}`, "DELETE", "", () => navigate("/integrations"))} onArchiveInstead={() => openLifecycle("archive")} />
    </>
  );
}

// ---- Implemented by ---------------------------------------------------------------------------------------------

function ImplementedBy({ integration, impl, write, onChanged }: {
  integration: Integration; impl: ReturnType<typeof useLoad<Relationship[]>>; write: boolean; onChanged: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<Relationship | null>(null);
  const [confirming, setConfirming] = useState<ConfirmTarget | null>(null);
  const [history, setHistory] = useState<ConfirmTarget | null>(null);
  const [notice, setNotice] = useState("");
  const addFocus = useReturnFocus(adding);
  const removeFocus = { onCloseAutoFocus: (e: Event) => { e.preventDefault(); document.getElementById("impl-add")?.focus(); } };
  function done(text: string) { setAdding(false); setRemoving(null); setConfirming(null); setNotice(text); onChanged(); }
  const rows = impl.data;
  const addButton = write && <Button id="impl-add" size="sm" variant="outline" onClick={() => setAdding(true)}>Add implementer</Button>;
  return (
    <Panel id="h-impl" icon={IconBuild} title="Implemented by" count={rows?.length} action={addButton}>
      <div className="px-4"><SuccessNote text={notice} /></div>
      {impl.error ? <div className="px-4 py-3"><ErrorBanner message={impl.error} onRetry={impl.reload} /></div>
        : !rows ? <div className="px-4 py-3"><Loading what="implementers" /></div>
        : rows.length === 0 ? (
          <p className="px-4 py-3 text-muted-foreground">
            {integration.vendor_native ? "Vendor-native: no component in this catalog implements this integration. The vendor runs it." : "No implementers recorded."}
            {write && " If a script, job or flow of yours carries this data, add it as an implementer."}
          </p>
        ) : (
          <TableRegion label="Implemented by table">
            <Table>
              <TableCaption className="sr-only">Components that implement {integration.name}, sorted by name A to Z</TableCaption>
              <TableHeader><TableRow>
                <TableHead aria-sort="ascending">Component</TableHead><TableHead>Link state</TableHead><TableHead>Purpose</TableHead>
                <TableHead><span className="sr-only">Actions</span></TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {[...rows].sort((a, b) => a.source.name.localeCompare(b.source.name)).map((r) => (
                  <TableRow key={r.id} className="h-[52px] align-top">
                    <TableHead scope="row" className="whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                      <RecordLink record={r.source} />{r.source.archived && <Chip className="ml-2 align-middle">Archived</Chip>}
                    </TableHead>
                    <TableCell className="whitespace-normal"><LinkStateChip state={r.link_state} /><span className="mt-1 block max-w-56 text-[13px] text-muted-foreground">{r.link_why}</span></TableCell>
                    <TableCell className="max-w-sm whitespace-normal text-muted-foreground [overflow-wrap:anywhere]">{r.purpose || "—"}</TableCell>
                    <TableCell className="whitespace-normal text-right">
                      <span className="inline-flex flex-wrap justify-end gap-2">
                        {write && <Button size="sm" variant="outline" onClick={() => setConfirming(confirmTarget(r))}>Confirm link<span className="sr-only"> with implementer {r.source.name}</span></Button>}
                        <Button size="sm" variant="outline" onClick={() => setHistory(confirmTarget(r))}>History<span className="sr-only"> of confirmations for implementer {r.source.name}</span></Button>
                        {write && <Button size="sm" variant="outline" onClick={() => setRemoving(r)}>Remove<span className="sr-only"> implementer {r.source.name}</span></Button>}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
        )}
      <ConfirmLinkDialog target={confirming} onClose={() => setConfirming(null)} onDone={done} />
      <HistoryDialog target={history} onClose={() => setHistory(null)} />
      <Dialog open={adding} onOpenChange={setAdding}>
        <DialogContent className="max-w-lg" {...addFocus}>
          <DialogHeader>
            <DialogTitle>Add implementer — {integration.name}</DialogTitle>
            <DialogDescription>Choose the component (a script, job or flow) that carries this data flow. Vendor-native integrations have none.</DialogDescription>
          </DialogHeader>
          {adding && <AddImplementer integration={integration} onCancel={() => setAdding(false)} onDone={done} />}
        </DialogContent>
      </Dialog>
      <Dialog open={removing !== null} onOpenChange={(o) => !o && setRemoving(null)}>
        <DialogContent className="max-w-md" {...removeFocus}>
          {removing && <RemoveLink rel={removing} onCancel={() => setRemoving(null)} onDone={() => done("Implementer removed.")} />}
        </DialogContent>
      </Dialog>
    </Panel>
  );
}

function AddImplementer({ integration, onCancel, onDone }: { integration: Integration; onCancel: () => void; onDone: (t: string) => void }) {
  const [component, setComponent] = useState<PickedRecord | null>(null);
  const [purpose, setPurpose] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!component) return setErrors({ "impl-component": ["Choose a component."] });
    setBusy(true);
    const failed = await attempt(() => api("relationships", { method: "POST", body: { type: "implements", source: component.id, target: integration.id, purpose: purpose.trim() } }));
    setBusy(false);
    if (failed) {
      const mapped: Record<string, string[]> = {};
      for (const [k, v] of Object.entries(failed.errors)) (mapped[k === "purpose" ? "impl-purpose" : "impl-component"] ??= []).push(...v);
      return setErrors(Object.keys(mapped).length ? mapped : { "impl-component": [failed.detail] });
    }
    onDone(`${component.name} added as an implementer.`);
  }
  return (
    <form onSubmit={submit} noValidate>
      <ErrorSummary errors={errors} />
      <Field id="impl-component" label="Component (required)" errors={errors} help="Search components by name.">
        <RecordPicker id="impl-component" value={component} kinds={["component"]} onChange={setComponent} listLabel="Component suggestions" />
      </Field>
      <Field id="impl-purpose" label="Purpose" errors={errors}>
        <Input id="impl-purpose" value={purpose} onChange={(e) => setPurpose(e.target.value)} placeholder="For example: runs the nightly export" />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? "Adding…" : "Add implementer"}</Button>
      </DialogFooter>
    </form>
  );
}

// ---- Create and edit ----------------------------------------------------------------------------------------------

function Textarea(props: React.ComponentProps<"textarea">) {
  return (
    <textarea rows={3} {...props}
      className="w-full min-w-0 rounded-md border border-input bg-transparent px-3 py-2 text-base shadow-xs outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 aria-invalid:border-destructive disabled:opacity-50 md:text-sm" />
  );
}

type FormValues = {
  name: string; lifecycle: string; source: PickedRecord | null; sourceInstance: string; target: PickedRecord | null; targetInstance: string;
  data_purpose: string; protocol: string; schedule: string; documentation_url: string;
  direction: string; auth_method: string; monitoring_url: string; data_classification: string; data_categories: DataCategory[];
  technical_owner: string; support_team: string; it_steward: string;
};

function IntegrationForm({ mode, record, write, onDone, cancelTo }: { mode: "create" | "edit"; record?: Integration; write?: OpenedWrite; onDone: (saved: Integration) => void; cancelTo: string }) {
  const custom = useCustomFields("integration");
  if (custom.error) return <ErrorBanner message={problemText(custom.error, "Couldn't load the custom fields.")} onRetry={custom.refetch} />;
  if (!custom.fields) return <Loading what="form" rows={4} />;
  return <IntegrationFormBody mode={mode} record={record} write={write} onDone={onDone} cancelTo={cancelTo} customFields={custom.fields} />;
}

function IntegrationFormBody({ mode, record, write, onDone, cancelTo, customFields }: {
  mode: "create" | "edit"; record?: Integration; write?: OpenedWrite; onDone: (saved: Integration) => void; cancelTo: string; customFields: CustomField[];
}) {
  const me = useMe();
  const people = useLoad<Person[]>("people?active=true");
  const teams = useLoad<Named[]>("teams");
  const stewards = useLoad<UserRef[]>("stewards");
  const end = (e?: Integration["source"]): PickedRecord | null => (e ? { id: e.id, name: e.name, kind: e.kind } : null);
  const [v, setV] = useState<FormValues>({
    name: record?.name ?? "", lifecycle: record?.lifecycle ?? "active",
    source: end(record?.source), sourceInstance: record?.source.instance?.id ?? "", target: end(record?.target), targetInstance: record?.target.instance?.id ?? "",
    data_purpose: record?.data_purpose ?? "", protocol: record?.protocol ?? "", schedule: record?.schedule ?? "", documentation_url: record?.documentation_url ?? "",
    direction: record?.direction ?? "", auth_method: record?.auth_method ?? "", monitoring_url: record?.monitoring_url ?? "", data_classification: record?.data_classification ?? "",
    data_categories: record?.data_categories ?? [],
    technical_owner: "", support_team: "", it_steward: String(me.user.id),
  });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [formError, setFormError] = useState("");
  const [duplicate, setDuplicate] = useState("");
  const [saving, setSaving] = useState(false);
  const [initial] = useState(v);
  const gate = useInstanceGate();
  const custom = useCustomFieldForm(customFields, mode, record?.custom_field_data);
  // "Discard changes?" before leaving with unsaved edits; the same guard as the application form.
  const guard = useUnsavedGuard((JSON.stringify(v) !== JSON.stringify(initial) || custom.dirty) && !saving);
  const set = <K extends keyof FormValues>(k: K, value: FormValues[K]) => setV((cur) => ({ ...cur, [k]: value }));
  const on = (k: keyof FormValues) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => set(k, e.target.value as never);
  // A choice field: blank is "Not recorded", as on the record form.
  const pick = (k: "direction" | "auth_method" | "data_classification", label: string, list: Choice[], help: string) => (
    <Field id={k} label={label} errors={errors} help={help}>
      <Select id={k} value={v[k]} onChange={on(k)}>
        <option value="">Not recorded</option>
        {list.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
        {v[k] && !list.some((c) => c.value === v[k]) && <option value={v[k]}>{v[k]}</option>}
      </Select>
    </Field>
  );

  async function save(confirm = false) {
    const found: Record<string, string[]> = {};
    if (!v.name.trim()) found.name = ["Enter a name."];
    if (!v.source) found.source = ["Choose a record."];
    if (!v.target) found.target = ["Choose a record."];
    Object.assign(found, gate.errors());
    const cf = custom.build();
    Object.assign(found, cf.errors);
    setErrors(found);
    if (Object.keys(found).length) return;
    const body: Record<string, unknown> = {
      name: v.name.trim(), lifecycle: v.lifecycle, source: v.source!.id, target: v.target!.id,
      source_instance: v.sourceInstance || null, target_instance: v.targetInstance || null,
      data_purpose: v.data_purpose.trim(), protocol: v.protocol.trim(), schedule: v.schedule.trim(), documentation_url: v.documentation_url.trim(),
      direction: v.direction, auth_method: v.auth_method, monitoring_url: v.monitoring_url.trim(), data_classification: v.data_classification,
      // In the API's order, once each (it keeps them so).
      data_categories: DATA_CATEGORIES.map((c) => c.value).filter((c) => v.data_categories.includes(c)),
    };
    if (mode === "create") {
      Object.assign(body, { technical_owner: v.technical_owner || null, support_team: v.support_team || null, it_steward: v.it_steward || null });
    }
    if (cf.data) body.custom_field_data = cf.data;
    if (confirm) body.confirm_duplicate = true;
    setSaving(true); setFormError(""); setDuplicate("");
    try {
      const saved = await api<Integration>(mode === "create" ? "integrations" : `integrations/${record!.id}`, { method: mode === "create" ? "POST" : "PATCH", body, headers: mode === "edit" ? await write?.ifMatch() : undefined });
      onDone(saved);
    } catch (e) {
      setSaving(false);
      if (isStaleWrite(e) && write) write.onStale();
      else if (e instanceof Problem && e.type === "/problems/duplicate-name") setDuplicate(e.detail);
      else if (e instanceof Problem && Object.keys(e.errors).length) setErrors(e.errors);
      else setFormError(`${e instanceof Problem ? e.detail : "Something went wrong."} Your changes weren't saved. Nothing you typed was lost.`);
    }
  }
  if (people.error || teams.error || stewards.error) return <ErrorBanner message={people.error || teams.error || stewards.error} onRetry={() => { people.reload(); teams.reload(); stewards.reload(); }} />;
  if (!people.data || !teams.data || !stewards.data) return <Loading what="form" rows={4} />;
  const personOptions: ComboOption[] = people.data.map((p) => ({ id: p.id, label: p.name, hint: [p.department?.name, p.email].filter(Boolean).join(" · ") }));
  return (
    <form onSubmit={(e) => { e.preventDefault(); void save(); }} noValidate aria-label={mode === "create" ? "Add integration" : "Edit integration"} className="max-w-4xl rounded-lg border bg-card p-6">
      <fieldset disabled={saving} className="m-0 min-w-0 border-0 p-0">
        {formError && <ErrorBanner message={formError} />}
        <ErrorSummary errors={errors} focusOnShow="field" />
        {duplicate && (
          <div className="mb-3 flex flex-wrap items-center gap-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" role="status">
            <span aria-hidden="true">⚠</span>
            <p className="min-w-0 flex-1 font-semibold">{duplicate}</p>
            <Button type="button" size="sm" variant="outline" onClick={() => void save(true)}>Save anyway</Button>
          </div>
        )}
        <div className="grid gap-x-6 md:grid-cols-2">
          <Field id="name" label="Name (required)" errors={errors}><Input id="name" value={v.name} onChange={on("name")} autoComplete="off" /></Field>
          <Field id="lifecycle" label="Lifecycle (required)" errors={errors}>
            <Select id="lifecycle" value={v.lifecycle} onChange={on("lifecycle")}>{LIFECYCLES.map((l) => <option key={l} value={l}>{cap(l)}</option>)}</Select>
          </Field>

          <div className="mb-3 mt-3 border-t pt-4 md:col-span-2">
            <h2 className="font-display text-section font-semibold">Data flow</h2>
            <p className="mt-0.5 text-sm text-muted-foreground">Where the data comes from and where it goes. Both ends are applications or components.</p>
          </div>
          <div>
            <Field id="source" label="Source (required)" errors={errors} help="Search applications and components by name.">
              <RecordPicker id="source" value={v.source} kinds={["application", "component"]} listLabel="Source suggestions"
                onChange={(r) => { set("source", r); set("sourceInstance", ""); }} />
            </Field>
            <InstancePicker id="source_instance" label={`Instance of ${v.source?.name ?? "the source"} (optional)`} record={v.source} value={v.sourceInstance} onChange={(x) => set("sourceInstance", x)} errors={errors} onBlocked={gate.report} />
          </div>
          <div>
            <Field id="target" label="Target (required)" errors={errors} help="Search applications and components by name.">
              <RecordPicker id="target" value={v.target} kinds={["application", "component"]} listLabel="Target suggestions"
                onChange={(r) => { set("target", r); set("targetInstance", ""); }} />
            </Field>
            <InstancePicker id="target_instance" label={`Instance of ${v.target?.name ?? "the target"} (optional)`} record={v.target} value={v.targetInstance} onChange={(x) => set("targetInstance", x)} errors={errors} onBlocked={gate.report} />
          </div>
          <div className="md:col-span-2">
            <Field id="data_purpose" label="Data purpose" errors={errors} help="What data moves and why, in a sentence.">
              <Textarea id="data_purpose" value={v.data_purpose} onChange={on("data_purpose")} />
            </Field>
          </div>
          <Field id="protocol" label="Protocol" errors={errors} help="For example SFTP, REST API or manual export."><Input id="protocol" value={v.protocol} onChange={on("protocol")} /></Field>
          <Field id="schedule" label="Schedule" errors={errors} help="For example nightly at 02:00."><Input id="schedule" value={v.schedule} onChange={on("schedule")} /></Field>
          <Field id="documentation_url" label="Documentation URL" errors={errors}>
            <Input id="documentation_url" type="url" inputMode="url" placeholder="https://…" value={v.documentation_url} onChange={on("documentation_url")} />
          </Field>
          {pick("direction", "Direction", INTEGRATION_DIRECTIONS, "Source and target say where data starts; Both ways means data also comes back.")}
          {pick("auth_method", "Authentication", INTEGRATION_AUTH_METHODS, "How it signs in to the other end.")}
          {pick("data_classification", "Data classification", DATA_CLASSIFICATIONS, "The most sensitive data it moves.")}
          <div className="md:col-span-2">
            <CheckboxPicker id="data_categories" legend="Data categories" columns errors={errors.data_categories}
              options={DATA_CATEGORIES.map((c) => ({ id: c.value, label: c.label }))} value={v.data_categories}
              onChange={(ids) => set("data_categories", ids as DataCategory[])}
              help={<>What kinds of data it carries. Personal, special-category and health data mean it carries personal data{
                DATA_CATEGORIES.some((c) => ["personal", "special_category", "health"].includes(c.value) && v.data_categories.includes(c.value))
                  ? <strong className="font-semibold text-foreground"> (it does, as chosen)</strong> : ""}.</>} />
          </div>
          <Field id="monitoring_url" label="Monitoring URL" errors={errors} help="Where its runs or health are watched, for example a dashboard or run history.">
            <Input id="monitoring_url" type="url" inputMode="url" placeholder="https://…" value={v.monitoring_url} onChange={on("monitoring_url")} />
          </Field>

          <CustomFieldInputs form={custom} errors={errors} />

          {mode === "create" ? (
            <>
              <div className="mb-3 mt-3 border-t pt-4 md:col-span-2">
                <h2 className="font-display text-section font-semibold">Ownership</h2>
                <p className="mt-0.5 text-sm text-muted-foreground">Can be left blank; blanks show as gaps. Integrations have no business owner or criticality.</p>
              </div>
              <Field id="technical_owner" label="Technical owner" errors={errors}>
                <Combobox id="technical_owner" options={personOptions} value={v.technical_owner} onChange={(x) => set("technical_owner", x)} placeholder="Search people…" clearLabel="Not set (gap)" listLabel="Technical owner suggestions" />
              </Field>
              <Field id="support_team" label="Support team" errors={errors}>
                <Combobox id="support_team" options={teams.data.map((t) => ({ id: t.id, label: t.name }))} value={v.support_team} onChange={(x) => set("support_team", x)} placeholder="Search teams…" clearLabel="Not set (gap)" listLabel="Support team suggestions" />
              </Field>
              <Field id="it_steward" label="IT steward" errors={errors} help="With none, the integration routes to the IT coordinator.">
                <Select id="it_steward" value={v.it_steward} onChange={on("it_steward")}>
                  <option value="">None</option>
                  {stewards.data.map((u) => <option key={u.id} value={u.id}>{u.name}{u.id === me.user.id ? " (me)" : ""}</option>)}
                </Select>
              </Field>
            </>
          ) : (
            <p className="mb-3 text-sm text-muted-foreground md:col-span-2">Technical owner, support team and IT steward are changed with Edit ownership on the integration page.</p>
          )}
        </div>
        <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
          <Button asChild variant="outline"><Link to={cancelTo}>Cancel</Link></Button>
          <Button type="submit">{saving ? "Saving…" : "Save"}</Button>
        </div>
      </fieldset>
      {guard}
    </form>
  );
}

function NeedsWriteRole() {
  return (
    <>
      <PageHead title="You can't change integrations" desc="Editors and Admins can add and edit integrations. You can view them." />
      <Button asChild variant="outline"><Link to="/integrations">Back to Integrations</Link></Button>
    </>
  );
}

export function NewIntegration() {
  const me = useMe();
  const navigate = useNavigate();
  if (!canWrite(me)) return <NeedsWriteRole />;
  return (
    <>
      <Breadcrumb trail={[{ to: "/integrations", label: "Integrations" }]} current="Add integration" />
      <PageHead title="Add integration" desc="A name, a source and a target are required. Everything else can be left blank." />
      <IntegrationForm mode="create" cancelTo="/integrations" onDone={(saved) => navigate(`/integrations/${saved.id}`, { state: savedState(saved.name, `${saved.name} added.`) })} />
    </>
  );
}

export function EditIntegration() {
  const { id } = useParams();
  const me = useMe();
  const navigate = useNavigate();
  const rec = useLoad<Integration>(`integrations/${id}`);
  if (!canWrite(me)) return <NeedsWriteRole />;
  if (rec.status === 404) return <NotFound what="integration" back={{ to: "/integrations", label: "Integrations" }} />;
  if (rec.error) return <ErrorBanner message={rec.error} onRetry={rec.reload} />;
  const r = rec.data;
  return (
    <>
      <Breadcrumb trail={[{ to: "/integrations", label: "Integrations" }, ...(r ? [{ to: `/integrations/${id}`, label: r.name }] : [])]} current="Edit" />
      <PageHead docTitle={r ? `Edit ${r.name}` : "Edit"} title={<span className="[overflow-wrap:anywhere]">{r ? `Edit ${r.name}` : "Edit"}</span>} desc="A name, a source and a target are required." />
      {!r ? <Loading what="integration" rows={4} /> : (
        // The form starts from a read with the ETag, which the save sends back as If-Match.
        <OpenedEdit<Integration> key={r.id} path={`integrations/${r.id}`} what="integration">
          {(fresh, write) => <IntegrationForm mode="edit" record={fresh} write={write} cancelTo={`/integrations/${id}`} onDone={(saved) => navigate(`/integrations/${id}`, { state: savedState(saved.name, `${saved.name} saved.`) })} />}
        </OpenedEdit>
      )}
    </>
  );
}
