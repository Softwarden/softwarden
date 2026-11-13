/** Licence pools: the list with the organisation's seat totals, the pool page (utilisation as a bar plus
 *  words, the overuse flag, seat sync with its read-only last sync), the add and edit form, and the review of proposed
 *  seat counts: pools whose counts a discovery source (`license` observations) disagrees with, each applied (an
 *  ordinary audited edit) or dismissed with a reason, dismissed ones kept as history, and SKUs no pool tracks listed for
 *  information. Loaded on first use (App.tsx). */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, apiWithEtag, Problem, when } from "@/api";
import { attempt, Breadcrumb, EmptyState, ErrorBanner, ErrorSummary, Field, Loading, NotFound, PageHead, Select, TableRegion, TabStrip, Textarea, useReturnFocus } from "@/components/app";
import { FilterBar, type FilterField } from "@/components/filter-bar";
import { DeleteDialog } from "@/components/lifecycle-dialogs";
import { MoreMenu, type MenuAction } from "@/components/more-menu";
import { RecordPicker, type PickedRecord } from "@/components/record-picker";
import { RecordTable, useRecordTable, type RecordColumn } from "@/components/record-table";
import { chip, DetailList, DetailRow, GapChip, Panel, StatTile, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ActivityTab } from "@/features/admin/audit";
import { CustomFieldInputs, CustomFieldsPanel, customColumns, customFilterFields, unknownFilterFields, useCustomFieldForm, useCustomFields, type CustomField } from "@/features/catalog/custom-fields";
import { useUnsavedGuard } from "@/features/catalog/record-form";
import { recordPath } from "@/lib/kinds";
import { apiKey, problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { canWrite, savedState, useLandingNotice, useMe } from "@/lib/session";
import { SEAT_FIELD_LABEL, type LicensePool, type PoolTotals, type SeatField, type SeatProposal, type SeatReview } from "./contract-types";
import { ContractPicker, OverusedChip, StaleBanner, UtilisationMeter, useDownload } from "./contract-ui";
import { ifMatch, staleDeleteText, useOpenedRecord } from "@/lib/if-match";

// ---- list ---------------------------------------------------------------------------------------------------------------

const FILTER_KEYS = ["application", "contract", "seat_sync", "overused"];

function poolColumns(fields: CustomField[]): RecordColumn<LicensePool>[] {
  return [
    { id: "sku_name", header: "SKU", sortField: "sku_name", cell: () => null },
    { id: "sku_id", header: "SKU ID", defaultVisible: false, cell: (p) => (p.sku_id ? <span className="font-mono text-xs">{p.sku_id}</span> : <span className="text-muted-foreground">Not set</span>) },
    { id: "application", header: "Application", cell: (p) => <Link to={recordPath("application", p.application.id)!}>{p.application.name}</Link> },
    { id: "contract", header: "Contract", cell: (p) => (p.contract ? <Link to={`/contracts/${p.contract.id}`}>{p.contract.name}</Link> : <span className="text-muted-foreground">None</span>) },
    { id: "utilization", header: "Utilization", className: "whitespace-normal min-w-48", cell: (p) => <>
      <UtilisationMeter purchased={p.seats_purchased} assigned={p.seats_assigned} utilization={p.utilization} />
      {p.overused && <span className="mt-1 block"><OverusedChip by={-p.seats_available} /></span>}
    </> },
    { id: "seat_sync", header: "Seat sync", className: "whitespace-normal min-w-44", cell: (p) => <PoolSyncState p={p} /> },
    { id: "seat_proposals", header: "Proposed counts", cell: (p) => (p.seat_proposals
      ? <Link to={`/license-pools/${p.id}?tab=review`} className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>{p.seat_proposals} to review</Link>
      : <span className="text-muted-foreground">None</span>) },
    ...customColumns<LicensePool>(fields),
  ];
}

export function LicensePoolsList() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "review" ? "review" : "pools";
  const review = useApiQuery<SeatReview>("license-pools/seat-review?status=open", { staleTime: 10_000 });
  const openCount = review.data?.results.length;
  return (
    <>
      <PageHead title="License pools" desc="Seats bought per application and SKU, how many are assigned, and where a discovery source reports other counts." >
        <PoolsHeaderActions />
      </PageHead>
      <TabStrip tabs={[{ id: "pools" as const, label: "Pools" }, { id: "review" as const, label: `Proposed seat counts${openCount !== undefined ? ` (${openCount})` : ""}` }]} tab={tab} label="License pool views"
        onSelect={(t) => setParams(t === "pools" ? {} : { tab: t })} />
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "pools" ? <PoolsTable /> : <SeatReviewPanel />}
      </div>
    </>
  );
}

function PoolsHeaderActions() {
  const me = useMe();
  const [params] = useSearchParams();
  const exporter = useDownload();
  const [notice, setNotice] = useState("");
  const query = Object.fromEntries([...params.entries()].filter(([k]) => FILTER_KEYS.includes(k) || k === "q" || k.startsWith("cf_")));
  async function exportCsv() {
    const name = await exporter.download(`license-pools/export.csv?${new URLSearchParams(query)}`, "softwarden-license-pools.csv");
    if (name) setNotice(`Exported the license pools in this view to ${name}. The export is recorded in the audit log.`);
  }
  return (
    <>
      <Button variant="outline" disabled={exporter.busy} onClick={() => void exportCsv()}>Export CSV</Button>
      {canWrite(me) && <Button asChild><Link to="/license-pools/new">Add license pool</Link></Button>}
      <span className={exporter.error || notice ? "w-full" : "contents"}><SuccessNote text={notice} />{exporter.error && <ErrorBanner message={exporter.error} />}</span>
    </>
  );
}

function Totals() {
  const totals = useApiQuery<PoolTotals>("license-pools/totals");
  if (totals.error) return <ErrorBanner message={`Couldn't load the seat totals. ${problemText(totals.error)}`} onRetry={() => void totals.refetch()} />;
  if (!totals.data) return <Loading what="seat totals" />;
  const t = totals.data.total;
  return (
    <section aria-label="Seat totals" className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <StatTile level={2} id="stat-purchased" label="Seats purchased" value={t.seats_purchased} detail={`In ${t.pools} ${t.pools === 1 ? "pool" : "pools"}`} />
      <StatTile level={2} id="stat-assigned" label="Seats assigned" value={t.seats_assigned} detail={t.seats_available >= 0 ? `${t.seats_available} available` : `${-t.seats_available} more than purchased`} />
      <StatTile level={2} id="stat-utilisation" label="Utilization" value={t.utilization != null ? `${t.utilization}%` : "—"} detail={t.utilization == null ? "No seats purchased yet" : "Assigned of purchased"} />
      <StatTile level={2} id="stat-overused" label="Overused" value={t.overused_rows}
        detail={t.overused_rows ? <><span aria-hidden="true">! </span>{t.overused_rows === 1 ? "application and SKU has" : "applications and SKUs have"} more assigned than purchased</> : "Nothing assigned beyond what was bought"} />
    </section>
  );
}

function PoolsTable() {
  const [params, setParams] = useSearchParams();
  const custom = useCustomFields("license_pool").fields;
  const cfFilters = useMemo(() => customFilterFields(custom ?? []), [custom]);
  const cfUnknown = custom ? unknownFilterFields(params, cfFilters) : [];
  const keys = [...FILTER_KEYS, ...new Set([...cfFilters.map((f) => f.key), ...[...params.keys()].filter((k) => k.startsWith("cf_"))])];
  const q = (params.get("q") ?? "").trim();
  const values = Object.fromEntries(keys.filter((k) => params.get(k)).map((k) => [k, params.get(k)!]));
  const query = { ...values, ...(q ? { q } : {}) };
  const table = useRecordTable<LicensePool>({ id: "license-pools", path: "license-pools", query });
  const cols = useMemo(() => poolColumns(custom ?? []), [custom]);
  const me = useMe();
  function update(changes: Record<string, string>, push = !("q" in changes)) {
    setParams((cur) => {
      const next = new URLSearchParams(cur);
      next.delete("cursor");
      next.delete("page");
      for (const [k, v] of Object.entries(changes)) { if (v) next.set(k, v); else next.delete(k); }
      return next;
    }, { replace: !push });
  }
  const filtered = Object.keys(query).length > 0;
  const clear = () => update(Object.fromEntries([...keys, "q"].map((k) => [k, ""])), true);
  const appName = params.get("application") ? table.rows?.find((r) => r.application.id === params.get("application"))?.application.name : undefined;
  const fields: FilterField[] = [
    { key: "overused", label: "Overuse", options: [{ value: "true", label: "Overused" }, { value: "false", label: "Not overused" }] },
    { key: "seat_sync", label: "Seat sync", options: [{ value: "true", label: "Syncs from discovery" }, { value: "false", label: "Entered by hand" }] },
    // Searched as typed (contracts?q=), so every contract can be chosen however many there are.
    { key: "contract", label: "Contract", options: [], render: (id, value, onChange) => (
      <ContractPicker id={id} value={value} onChange={(c) => onChange(c?.id ?? "")} clearLabel="Any" className="h-7 text-[13px]" />
    ) },
    { key: "application", label: "Application", options: params.get("application") ? [{ value: params.get("application")!, label: appName ?? "The chosen application" }] : [] },
    ...cfFilters, ...cfUnknown,
  ];
  const empty = filtered ? (
    <EmptyState title="No license pools match these filters" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>
      {q ? `Nothing matches "${q}" with these filters.` : "No license pools are in this filter right now."}
    </EmptyState>
  ) : (
    <EmptyState title="No license pools yet" action={canWrite(me) ? <Button asChild variant="outline"><Link to="/license-pools/new">Add license pool</Link></Button> : undefined}>
      A pool is the seats of one SKU an application uses: how many were bought and how many are assigned. Add the SKU ID to compare it with what discovery reports.
      {!canWrite(me) && " Editors and Admins can add them."}
    </EmptyState>
  );
  return (
    <>
      <Totals />
      <FilterBar id="pools" searchLabel="Search license pools" searchPlaceholder="SKU name or ID, application" search={q} onSearch={(v) => update({ q: v })}
        fields={fields} values={values} onChange={(k, v, typed) => update({ [k]: v }, !typed)} filtered={filtered} onClear={clear} />
      <RecordTable table={table} caption="License pools" noun={["license pool", "license pools"]} columns={cols} rowHeader="sku_name" nameOf={(p) => p.sku_name}
        rowHref={(p) => `/license-pools/${p.id}`} selectable={false}
        defaultOrder={{ text: "SKU, A to Z", field: "sku_name" }} empty={empty} onClearFilters={clear} />
    </>
  );
}

// ---- the seat review ----------------------------------------------------------------------------------------------------

type ReviewStatus = "open" | "dismissed" | "all";
const REVIEW_STATUSES: { id: ReviewStatus; label: string }[] = [{ id: "open", label: "Open" }, { id: "dismissed", label: "Dismissed" }, { id: "all", label: "All" }];

/** Proposed seat counts, everywhere or for one pool. The status is in the URL (`seats`). */
export function SeatReviewPanel({ pool }: { pool?: LicensePool }) {
  const me = useMe();
  const write = canWrite(me);
  const [params, setParams] = useSearchParams();
  const asked = params.get("seats");
  const status: ReviewStatus = asked === "dismissed" || asked === "all" ? asked : "open";
  const load = useApiQuery<SeatReview>(`license-pools/seat-review?status=${status}${pool ? `&pool=${pool.id}` : ""}`);
  const invalidate = useInvalidate();
  const [notice, setNotice] = useState("");
  const [dialog, setDialog] = useState<{ mode: "apply" | "dismiss"; proposal: SeatProposal } | null>(null);
  const setStatus = (s: ReviewStatus) => setParams((cur) => { const next = new URLSearchParams(cur); if (s === "open") next.delete("seats"); else next.set("seats", s); return next; });
  const rows = load.data?.results;
  const done = (message: string) => { setDialog(null); setNotice(message); void invalidate("license-pools", "needs-attention", "contracts"); };
  return (
    <div className="grid gap-4">
      <p className="max-w-prose text-muted-foreground">
        A discovery source (Microsoft Entra licenses) reports each SKU's purchased and assigned seats. Where a pool with that SKU ID holds other counts, they are proposed here.
        Applying takes the source's counts into the pool as an ordinary, audited edit; dismissing keeps the pool's counts until the source reports different ones.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <div role="group" aria-label="Show proposals" className="inline-flex overflow-hidden rounded-md border border-border-strong bg-card">
          {REVIEW_STATUSES.map((s) => (
            <button key={s.id} type="button" aria-pressed={status === s.id} onClick={() => setStatus(s.id)}
              className="flex h-8 items-center px-3 text-[13px] text-muted-foreground aria-pressed:bg-accent aria-pressed:font-semibold aria-pressed:text-accent-foreground [&+&]:border-l [&+&]:border-border-strong">
              {s.label}
            </button>
          ))}
        </div>
      </div>
      <SuccessNote text={notice} />
      {load.error && <ErrorBanner message={`Couldn't load the proposed seat counts. ${problemText(load.error)}`} onRetry={() => void load.refetch()} />}
      {!rows && !load.error && <Loading what="proposed seat counts" rows={2} />}
      {rows && rows.length === 0 && (
        <EmptyState title={status === "dismissed" ? "No dismissed proposals" : status === "all" ? "No proposed seat counts" : "No proposed seat counts to review"}>
          {status === "dismissed" ? "Proposals someone dismissed, with their reason, are kept here until the source's counts change."
            : `Every ${pool ? "count of this pool matches" : "pool with a SKU ID matches"} what its sources report${status === "open" ? ", or the difference was dismissed" : ""}.`}
        </EmptyState>
      )}
      {rows && rows.length > 0 && (
        <TableRegion label="Proposed seat counts table">
          <Table>
            <TableCaption className="sr-only">Proposed seat counts{pool ? ` for ${pool.sku_name}` : ""}, by application, SKU and source</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">{pool ? "Source" : "License pool"}</TableHead>{!pool && <TableHead scope="col">Source</TableHead>}
              <TableHead scope="col">Differences</TableHead><TableHead scope="col">Status</TableHead>
              {write && <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>}
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((p) => {
                const source = (
                  <>
                    {p.observation.connector.name}
                    <span className="block text-xs font-normal text-muted-foreground">Reports {p.observation.display_name}, last seen {when(p.observation.last_seen_at)}</span>
                  </>
                );
                const label = `${p.pool.sku_name} from ${p.observation.connector.name}`;
                return (
                  <TableRow key={`${p.pool.id}-${p.observation.id}`} className="align-top" data-proposal={p.observation.id}>
                    <TableHead scope="row" className="whitespace-normal font-semibold text-foreground">
                      {pool ? source : <>
                        <Link to={`/license-pools/${p.pool.id}?tab=review`} className="underline">{p.pool.sku_name}</Link>
                        <span className="block text-xs font-normal text-muted-foreground">{p.pool.application.name}{p.pool.seat_sync ? " · syncs from discovery" : ""}</span>
                      </>}
                    </TableHead>
                    {!pool && <TableCell className="whitespace-normal">{source}</TableCell>}
                    <TableCell className="whitespace-normal">
                      <ul className="m-0 list-none p-0">
                        {p.differences.map((d) => <li key={d.field}><span className="font-medium">{SEAT_FIELD_LABEL[d.field]}:</span> <span className="tabular">{d.current}</span> in the pool, <span className="tabular font-semibold">{d.proposed}</span> reported</li>)}
                      </ul>
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      {p.status === "open" ? <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Open</span> : (
                        <>
                          <span className={chip({ state: "neutral" })}><span aria-hidden="true">⊘</span>Dismissed</span>
                          {p.dismissal && <span className="mt-1 block text-sm text-muted-foreground [overflow-wrap:anywhere]">"{p.dismissal.reason}"{p.dismissal.by ? ` (${p.dismissal.by.name}, ${when(p.dismissal.at)})` : ` (${when(p.dismissal.at)})`}</span>}
                        </>
                      )}
                    </TableCell>
                    {write && (
                      <TableCell className="text-right">
                        {p.status === "open" && (
                          <span className="inline-flex flex-wrap justify-end gap-2">
                            <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "apply", proposal: p })}>Apply…<span className="sr-only"> {label}</span></Button>
                            <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "dismiss", proposal: p })}>Dismiss…<span className="sr-only"> {label}</span></Button>
                          </span>
                        )}
                      </TableCell>
                    )}
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      {!pool && load.data && load.data.unbound.length > 0 && <UnboundProposals rows={load.data.unbound} write={write} onDone={done} />}
      <SeatDialog state={dialog} onClose={() => setDialog(null)} onDone={done} />
    </div>
  );
}

function SeatDialog({ state, onClose, onDone }: { state: { mode: "apply" | "dismiss"; proposal: SeatProposal } | null; onClose: () => void; onDone: (m: string) => void }) {
  const open = state !== null;
  const returnFocus = useReturnFocus(open);
  const p = state?.proposal;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>{state?.mode === "apply" ? `Apply the reported counts to ${p?.pool.sku_name}` : `Dismiss the counts for ${p?.pool.sku_name}`}</DialogTitle>
          <DialogDescription>{state?.mode === "apply"
            ? `The counts ${p?.observation.connector.name} reports replace the pool's, as an audited edit naming the source. The last sync date isn't changed.`
            : "The pool keeps its counts. The proposal comes back if the source reports different counts later."}</DialogDescription>
        </DialogHeader>
        {state && p && <SeatForm key={`${state.mode}-${p.pool.id}-${p.observation.id}`} mode={state.mode} p={p} onClose={onClose} onDone={onDone} />}
      </DialogContent>
    </Dialog>
  );
}

function SeatForm({ mode, p, onClose, onDone }: { mode: "apply" | "dismiss"; p: SeatProposal; onClose: () => void; onDone: (m: string) => void }) {
  const [counts, setCounts] = useState<SeatField[]>(p.differences.map((d) => d.field));
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const found: Record<string, string[]> = {};
    if (mode === "apply" && !counts.length) found.counts = ["Choose at least one count to apply, or cancel."];
    if (mode === "dismiss" && !reason.trim()) found.reason = ["Say why the pool's counts are right, for example a true-up already agreed."];
    setErrors(found);
    if (Object.keys(found).length) return;
    setBusy(true);
    const path = `license-pools/${p.pool.id}/seat-review/${p.observation.id}/${mode}`;
    const failed = await attempt(() => api(path, { method: "POST", body: mode === "apply" ? { counts } : { reason: reason.trim() } }));
    setBusy(false);
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { [mode === "apply" ? "counts" : "reason"]: [failed.detail] });
    onDone(mode === "apply"
      ? `${p.pool.sku_name} now has ${counts.map((f) => `${SEAT_FIELD_LABEL[f].toLowerCase()} ${p.differences.find((d) => d.field === f)!.proposed}`).join(" and ")}, from ${p.observation.connector.name}.`
      : `Dismissed: ${p.pool.sku_name} keeps its counts. The reason is in the pool's history.`);
  }
  return (
    <form onSubmit={submit} noValidate aria-label={mode === "apply" ? "Apply the reported counts" : "Dismiss the reported counts"}>
      <ErrorSummary errors={errors} />
      {mode === "apply" ? (
        <fieldset className="mb-3" aria-describedby={errors.counts ? "counts-error" : undefined}>
          <legend className="mb-1.5 text-sm font-medium">Counts to apply</legend>
          <ul className="flex flex-col gap-1.5">
            {p.differences.map((d, i) => (
              <li key={d.field}>
                <label className="flex min-h-6 items-center gap-2 text-sm">
                  <input type="checkbox" id={i === 0 ? "counts" : undefined} checked={counts.includes(d.field)} onChange={() => setCounts((cur) => (cur.includes(d.field) ? cur.filter((x) => x !== d.field) : [...cur, d.field]))} />
                  {SEAT_FIELD_LABEL[d.field]}: {d.current} to {d.proposed}
                </label>
              </li>
            ))}
          </ul>
          {errors.counts && <p id="counts-error" className="mt-1 text-sm text-error-foreground">{errors.counts.join(" ")}</p>}
        </fieldset>
      ) : (
        <Field id="reason" label="Reason (required)" errors={errors} help="Kept with the dismissal and in the pool's history.">
          <Textarea id="reason" rows={3} value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
        </Field>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? "Saving…" : mode === "apply" ? "Apply counts" : "Dismiss proposal"}</Button>
      </DialogFooter>
    </form>
  );
}

// ---- unbound SKUs: proposals to create a pool or bind one -------------------------------------------------

type Unbound = SeatReview["unbound"][number];
const skuLabel = (u: Unbound) => u.sku_part_number || u.sku_id;
const seatsText = (o: { seats_purchased: number | null; seats_assigned: number | null }) => `${o.seats_assigned ?? "?"} of ${o.seats_purchased ?? "?"} assigned`;

/** Where a pool's counts come from, as a chip with a glyph and words: synced from the licences connector the API names
 *  (`synced_from`), a sync held because two sources report the SKU or on with no source (named gaps), bound with the
 *  differences proposed, or not bound to a SKU (never matched to a source). */
export function PoolSyncState({ p }: { p: Pick<LicensePool, "sku_id" | "seat_sync" | "last_synced" | "seat_proposals" | "synced_from" | "sync_held"> }) {
  if (!p.sku_id.trim()) {
    return <span><span className={chip({ state: "neutral" })}><span aria-hidden="true">○</span>Not bound to a SKU</span><span className="block text-xs text-muted-foreground">No source is matched to it.</span></span>;
  }
  if (p.seat_sync) {
    const last = <span className="block text-xs text-muted-foreground">{p.last_synced ? `Last synced ${when(p.last_synced)}` : "Not synced yet: the next licenses run sets the counts."}</span>;
    if (p.sync_held) {
      return <span><GapChip label="Seat sync held: two license sources report this SKU" /><span className="block text-xs text-muted-foreground">No run changes the counts; each source's counts are proposed for review. Turn one connector off to sync again.</span></span>;
    }
    if (!p.synced_from) {
      return <span><GapChip label="Seat sync on, but no licenses source reports this SKU" />{last}</span>;
    }
    return <span><span className={chip({ state: "confirmed" })}><span aria-hidden="true">↻</span>Seats synced from {p.synced_from.name}</span>{last}</span>;
  }
  return p.seat_proposals > 0
    ? <span><span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>{p.seat_proposals === 1 ? "1 proposed count" : `${p.seat_proposals} proposed counts`}</span><span className="block text-xs text-muted-foreground">Bound to the SKU. Seat counts are entered by hand; review what the source reports.</span></span>
    : <span><span className={chip({ state: "neutral" })}><span aria-hidden="true">◇</span>Bound, counts by hand</span><span className="block text-xs text-muted-foreground">Seat counts are entered by hand; a source that disagrees is proposed for review.</span></span>;
}

function UnboundProposals({ rows, write, onDone }: { rows: Unbound[]; write: boolean; onDone: (message: string) => void }) {
  const [dialog, setDialog] = useState<{ mode: "create" | "bind"; u: Unbound } | null>(null);
  return (
    <section aria-labelledby="h-unbound" className="rounded-lg border bg-card">
      <h2 id="h-unbound" className="border-b px-4 py-2.5 font-display text-section font-semibold">Unbound SKUs ({rows.length})</h2>
      <p className="px-4 pt-3 text-sm text-muted-foreground">
        A source reports these SKUs, but no pool has their SKU ID, so nothing is synced or compared. Each is a proposal: create a pool for it, or bind an existing pool that has no SKU ID yet.
        {!write && " Editors and Admins can do either."}
      </p>
      <TableRegion label="Unbound SKUs table" className="m-4 mt-3">
        <Table>
          <TableCaption className="sr-only">SKUs a source reports that no license pool has as its SKU ID, each a proposal</TableCaption>
          <TableHeader><TableRow>
            <TableHead scope="col">SKU</TableHead><TableHead scope="col">Seats reported</TableHead><TableHead scope="col">Source</TableHead><TableHead scope="col">Status</TableHead>
            {write && <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>}
          </TableRow></TableHeader>
          <TableBody>
            {rows.map((u) => (
              <TableRow key={u.observation.id} data-unbound={u.sku_id}>
                <TableHead scope="row" className="whitespace-normal font-semibold text-foreground">{skuLabel(u)}<span className="block font-mono text-xs font-normal text-muted-foreground">{u.sku_id}</span></TableHead>
                <TableCell className="tabular">{seatsText(u.observed)}</TableCell>
                <TableCell className="whitespace-normal">{u.observation.connector.name}<span className="block text-xs text-muted-foreground">Last seen {when(u.observation.last_seen_at)}</span></TableCell>
                <TableCell><span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Proposal</span></TableCell>
                {write && (
                  <TableCell className="text-right">
                    <span className="inline-flex flex-wrap justify-end gap-2">
                      <Button size="sm" variant="queue" onClick={() => setDialog({ mode: "create", u })}>Create a pool…<span className="sr-only"> for {skuLabel(u)}</span></Button>
                      <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "bind", u })}>Bind to an existing pool…<span className="sr-only"> for {skuLabel(u)}</span></Button>
                    </span>
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableRegion>
      {dialog?.mode === "create" && <CreateFromUnbound u={dialog.u} onClose={() => setDialog(null)} onDone={(m) => { setDialog(null); onDone(m); }} />}
      {dialog?.mode === "bind" && <BindUnbound u={dialog.u} onClose={() => setDialog(null)} onDone={(m) => { setDialog(null); onDone(m); }} />}
    </section>
  );
}

/** A pool for an unbound SKU: the SKU ID, name and counts come from the source; the application is required. */
function CreateFromUnbound({ u, onClose, onDone }: { u: Unbound; onClose: () => void; onDone: (m: string) => void }) {
  const focus = useReturnFocus(true);
  const custom = useCustomFields("license_pool");
  const cf = useCustomFieldForm(custom.fields ?? [], "create");
  const [application, setApplication] = useState<PickedRecord | null>(null);
  const [contract, setContract] = useState("");
  const [skuName, setSkuName] = useState(skuLabel(u));
  const [seatSync, setSeatSync] = useState(false);
  const [notes, setNotes] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    const found: Record<string, string[]> = {};
    if (!application) found.application = ["Choose the application these seats are for."];
    if (!skuName.trim()) found.sku_name = ["Enter the SKU or plan name."];
    const built = cf.build();
    Object.assign(found, built.errors);
    setErrors(found);
    setDetail("");
    if (Object.keys(found).length) return;
    setBusy(true);
    try {
      const body: Record<string, unknown> = { application: application!.id, sku_name: skuName.trim(), seat_sync: seatSync };
      if (contract) body.contract = contract;
      if (notes.trim()) body.notes = notes.trim();
      if (built.data) body.custom_field_data = built.data;
      const made = await api<LicensePool>(`license-pools/unbound/${u.observation.id}/create`, { method: "POST", body });
      onDone(`Added the pool ${made.sku_name} for ${made.application.name}, with ${seatsText(made)} from ${u.observation.connector.name}.${seatSync ? " Its seats now sync on every licenses run." : ""}`);
    } catch (err) {
      if (err instanceof Problem && Object.keys(err.errors).length) setErrors(err.errors);
      else setDetail(err instanceof Problem ? err.detail : "Something went wrong. The pool wasn't added; try again.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...focus}>
        <DialogHeader>
          <DialogTitle className="[overflow-wrap:anywhere]">Create a pool for {skuLabel(u)}</DialogTitle>
          <DialogDescription>The SKU ID and the counts {u.observation.connector.name} reports ({seatsText(u.observed)}) start the pool, as an audited addition naming the source.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate aria-label={`Create a pool for ${skuLabel(u)}`}>
          <ErrorSummary errors={errors} />
          {detail && <p role="alert" className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2">{detail}</p>}
          <Field id="application" label="Application (required)" errors={errors}>
            <RecordPicker id="application" kinds={["application"]} value={application} onChange={setApplication} placeholder="Search applications…" listLabel="Application suggestions" />
          </Field>
          <Field id="sku_name" label="SKU name (required)" errors={errors} help="The source's part number unless you name it.">
            <Input id="sku_name" value={skuName} onChange={(e) => setSkuName(e.target.value)} autoComplete="off" />
          </Field>
          <Field id="contract" label="Contract" errors={errors} help="The contract the seats were bought under, if any.">
            <ContractPicker id="contract" value={contract} onChange={(c) => setContract(c?.id ?? "")} clearLabel="None" />
          </Field>
          <div className="mb-3">
            <label className="flex min-h-6 items-center gap-2 text-sm font-medium">
              <input type="checkbox" id="seat_sync" checked={seatSync} onChange={(e) => setSeatSync(e.target.checked)} aria-describedby="seat_sync-help" />
              Sync seat counts from {u.observation.connector.name}
            </label>
            <p id="seat_sync-help" className="pl-6 text-sm text-muted-foreground">Every licenses run then overwrites both counts. Off: a source that disagrees is proposed for review.</p>
          </div>
          <Field id="notes" label="Notes" errors={errors}><Textarea id="notes" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} /></Field>
          {custom.fields && custom.fields.length > 0 && <CustomFieldInputs form={cf} errors={errors} />}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={busy}>{busy ? "Adding…" : "Create pool"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Bind a pool without a SKU ID to the SKU, under the pool's If-Match; its own counts stay. The pools come from the
 *  server's `sku_id=none` filter, 50 at a time. Seat sync starts as the chosen pool has it and is sent only if changed. */
function BindUnbound({ u, onClose, onDone }: { u: Unbound; onClose: () => void; onDone: (m: string) => void }) {
  const focus = useReturnFocus(true);
  const [free, setFree] = useState<LicensePool[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [listError, setListError] = useState("");
  const [poolId, setPoolId] = useState("");
  const [seatSync, setSeatSync] = useState<boolean | null>(null); // null: as the chosen pool has it
  const [tag, setTag] = useState<{ id: string; etag: string | null } | null>(null);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [refusal, setRefusal] = useState<{ text: string; stale: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const chosenRef = useRef("");
  /** One page of pools without a SKU ID. `keep`: the pool chosen before a reload (Load the latest version), kept in the
   *  list even when it is past the first page, and still chosen; if it has a SKU ID by now, it can't be bound. */
  const loadPage = useCallback(async (cursor: string | null, keep = "") => {
    setListError("");
    try {
      const query = new URLSearchParams({ sku_id: "none", page_size: "50", ...(cursor ? { cursor } : {}) });
      const page = await api<{ results: LicensePool[]; next: string | null }>(`license-pools?${query}`);
      let results = page.results;
      if (keep && !results.some((p) => p.id === keep)) {
        const kept = await api<LicensePool>(`license-pools/${keep}`).catch(() => null);
        if (kept && !kept.sku_id.trim()) results = [kept, ...results];
        else {
          setPoolId("");
          setRefusal({ text: `${kept?.sku_name ?? "The pool you chose"} ${kept ? "has a SKU ID now" : "no longer exists"}, so it can't be bound. Choose another pool.`, stale: false });
        }
      }
      setFree((cur) => (cursor && cur ? [...cur, ...results] : results));
      setNext(page.next);
    } catch (e) {
      setListError(problemText(e));
    }
  }, []);
  useEffect(() => { void loadPage(null); }, [loadPage]);
  const chosen = free?.find((p) => p.id === poolId) ?? null;
  const sync = seatSync ?? chosen?.seat_sync ?? false;
  async function readTag(id: string) {
    chosenRef.current = id;
    setTag(null);
    if (!id) return;
    try {
      const got = await apiWithEtag<LicensePool>(`license-pools/${id}`);
      if (chosenRef.current === id) setTag({ id, etag: got.etag }); // a response for a pool no longer chosen is dropped
    } catch (e) {
      if (chosenRef.current === id) setRefusal({ text: problemText(e), stale: false });
    }
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setRefusal(null);
    if (!chosen) { setErrors({ pool: ["Choose the pool these seats are."] }); return; }
    setErrors({});
    setBusy(true);
    try {
      // The ETag read when the pool was chosen; read now if that hasn't finished. Never bound without one.
      const ifMatch = tag?.id === chosen.id && tag.etag ? tag.etag : (await apiWithEtag<LicensePool>(`license-pools/${chosen.id}`)).etag;
      if (!ifMatch) { setRefusal({ text: `Couldn't read ${chosen.sku_name}'s version, so it wasn't bound. Try again.`, stale: true }); return; }
      const body: Record<string, unknown> = { observation: u.observation.id };
      if (seatSync !== null && seatSync !== chosen.seat_sync) body.seat_sync = seatSync;
      await api(`license-pools/${chosen.id}/bind`, { method: "POST", body, headers: { "If-Match": ifMatch } });
      onDone(`Bound ${chosen.sku_name} to ${skuLabel(u)}. It keeps its counts (${seatsText(chosen)}); ${sync ? `${u.observation.connector.name} sets them on its next licenses run.` : "a difference from the source is proposed for review."}`);
    } catch (err) {
      if (!(err instanceof Problem)) setRefusal({ text: "Something went wrong. Nothing was bound; try again.", stale: false });
      else if (err.status === 412) setRefusal({ text: `Someone changed ${chosen.sku_name} after you chose it, so it wasn't bound. Load the latest version and bind again.`, stale: true });
      else if (Object.keys(err.errors).length && Object.keys(err.errors).every((k) => k === "pool" || k === "bind_seat_sync")) setErrors(err.errors);
      else setRefusal({ text: [err.detail, ...Object.values(err.errors).flat()].filter(Boolean).join(" "), stale: false });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...focus}>
        <DialogHeader>
          <DialogTitle className="[overflow-wrap:anywhere]">Bind {skuLabel(u)} to an existing pool</DialogTitle>
          <DialogDescription>The pool gets the SKU ID {u.sku_id}, as an audited edit naming {u.observation.connector.name}. Its counts stay as they are. Only pools without a SKU ID are listed.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate aria-label={`Bind ${skuLabel(u)} to a pool`}>
          <ErrorSummary errors={errors} />
          {refusal && (
            <div role="alert" className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2">
              <p>{refusal.text}</p>
              {refusal.stale && <Button type="button" size="sm" variant="outline" className="mt-2" onClick={() => { setRefusal(null); setSeatSync(null); void loadPage(null, poolId); void readTag(poolId); }}>Load the latest version</Button>}
            </div>
          )}
          {listError && <ErrorBanner message={`Couldn't load the pools. ${listError}`} onRetry={() => void loadPage(null)} />}
          {!free && !listError && <p className="mb-3 text-muted-foreground" aria-live="polite">Loading pools…</p>}
          {free && free.length === 0 && (
            <p className="mb-3 rounded-md border bg-muted px-3 py-2">Every pool already has a SKU ID. Create a pool for this SKU instead, or clear a pool's SKU ID on its own page.</p>
          )}
          {free && free.length > 0 && (
            <Field id="pool" label="Pool (required)" errors={errors} help={`Pools without a SKU ID, by SKU name${next ? `; the first ${free.length} shown` : ""}.`}>
              <Select id="pool" value={poolId} onChange={(e) => { setPoolId(e.target.value); setSeatSync(null); void readTag(e.target.value); }}>
                <option value="">Choose a pool…</option>
                {free.map((p) => <option key={p.id} value={p.id}>{p.sku_name} ({p.application.name}, {seatsText(p)})</option>)}
              </Select>
            </Field>
          )}
          {next && <div className="mb-3"><Button type="button" size="sm" variant="outline" onClick={() => void loadPage(next)}>Show more pools</Button></div>}
          <div className="mb-3">
            <label className="flex min-h-6 items-center gap-2 text-sm font-medium">
              <input type="checkbox" id="bind_seat_sync" checked={sync} onChange={(e) => setSeatSync(e.target.checked)} aria-describedby="bind_seat_sync-help" />
              Sync seat counts from {u.observation.connector.name}
            </label>
            <p id="bind_seat_sync-help" className="pl-6 text-sm text-muted-foreground">
              {chosen ? `As ${chosen.sku_name} has it now (${chosen.seat_sync ? "on" : "off"}) unless you change it. ` : ""}
              On: the next licenses run overwrites the pool's counts ({seatsText(u.observed)} now). Off: differences are proposed for review.
            </p>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={busy}>{busy ? "Binding…" : "Bind pool"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---- the pool page ------------------------------------------------------------------------------------------------------

type PoolTab = "overview" | "review" | "history";

export function LicensePoolDetail() {
  const { id } = useParams();
  return <PoolPage key={id} id={id!} />;
}

function PoolPage({ id }: { id: string }) {
  const me = useMe();
  const write = canWrite(me);
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab");
  const tab: PoolTab = asked === "review" || asked === "history" ? asked : "overview";
  const load = useApiQuery<LicensePool>(`license-pools/${id}`);
  const [notice, , noticeQuiet] = useLandingNotice();
  const [deleting, setDeleting] = useState(false);
  // The delete confirmation reads the pool with its ETag as it opens; the delete sends it as If-Match.
  const deleteRead = useOpenedRecord(deleting ? `license-pools/${id}` : null);
  const [pending, setPending] = useState(false);
  const [refusal, setRefusal] = useState("");
  const p = load.data;
  if (load.error) {
    const missing = load.error instanceof Problem && load.error.status === 404;
    if (missing) return <NotFound what="license pool" back={{ to: "/license-pools", label: "License pools" }} trail={[{ to: "/license-pools", label: "License pools" }]} />;
    return (
      <>
        <Breadcrumb trail={[{ to: "/license-pools", label: "License pools" }]} current="License pool" />
        <PageHead title={missing ? "License pool not found" : "Couldn't load this license pool"} />
        <ErrorBanner message={missing ? "This license pool doesn't exist or you don't have access." : `${problemText(load.error)} Nothing was changed.`} onRetry={missing ? undefined : () => void load.refetch()} />
      </>
    );
  }
  if (!p) return <Loading what="license pool" rows={3} />;
  const actions: MenuAction[] = me.role === "admin" ? [{ label: "Delete", danger: true, onSelect: () => { setRefusal(""); setDeleting(true); } }] : [];
  async function remove() {
    setPending(true);
    const failed = await attempt(async () => api(`license-pools/${id}`, { method: "DELETE", headers: ifMatch(await deleteRead.etag()) }));
    setPending(false);
    if (failed?.status === 412) return setRefusal(staleDeleteText("license pool"));
    if (failed) return setRefusal(failed.detail);
    navigate("/license-pools", { state: { notice: `${p!.sku_name} deleted.` } });
  }
  return (
    <>
      <Breadcrumb trail={[{ to: "/license-pools", label: "License pools" }]} current={p.sku_name} />
      <PageHead title={<span className="[overflow-wrap:anywhere]">{p.sku_name}</span>} docTitle={p.sku_name}
        desc={<>Seats of <Link to={recordPath("application", p.application.id)!} className="underline">{p.application.name}</Link>{p.contract && <> under <Link to={`/contracts/${p.contract.id}`} className="underline">{p.contract.name}</Link></>}</>}>
        {write && <Button asChild variant="outline"><Link to={`/license-pools/${id}/edit`}>Edit</Link></Button>}
        {actions.length > 0 && <MoreMenu actions={actions} about={p.sku_name} />}
      </PageHead>
      <SuccessNote text={notice} quiet={noticeQuiet} />
      {p.seat_proposals > 0 && tab !== "review" && (
        <p className="mb-4 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground" role="note">
          <span aria-hidden="true">◐ </span>{p.seat_proposals === 1 ? "A source reports" : `${p.seat_proposals} sources report`} other seat counts. <Link to={`/license-pools/${id}?tab=review`} className="font-semibold underline">Review the proposed counts</Link>
        </p>
      )}
      <TabStrip tabs={[{ id: "overview" as const, label: "Overview" }, { id: "review" as const, label: `Proposed seat counts (${p.seat_proposals})` }, { id: "history" as const, label: "History" }]}
        tab={tab} label="License pool sections" onSelect={(t) => setParams(t === "overview" ? {} : { tab: t })} />
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "overview" && (
          <div className="grid gap-5">
            <Panel id="h-seats" icon={<span aria-hidden="true">▤</span>} title="Seats">
              <div className="grid gap-2 px-4 py-3">
                <UtilisationMeter purchased={p.seats_purchased} assigned={p.seats_assigned} utilization={p.utilization} />
                {p.overused ? <span><OverusedChip by={-p.seats_available} /> <span className="text-sm text-muted-foreground">More seats are assigned than were bought. That's recorded, not refused.</span></span>
                  : <span className="text-sm">{p.seats_available} {p.seats_available === 1 ? "seat" : "seats"} available.</span>}
              </div>
            </Panel>
            <Panel id="h-pool" icon={<span aria-hidden="true">≡</span>} title="Pool">
              <DetailList>
                <DetailRow label="SKU name">{p.sku_name}</DetailRow>
                <DetailRow label="SKU ID">{p.sku_id ? <span className="font-mono text-[13px]">{p.sku_id}</span> : <span className="text-muted-foreground">Not set: sources can't be matched to this pool</span>}</DetailRow>
                <DetailRow label="Application"><Link to={recordPath("application", p.application.id)!}>{p.application.name}</Link></DetailRow>
                <DetailRow label="Contract">{p.contract ? <Link to={`/contracts/${p.contract.id}`}>{p.contract.name}</Link> : "None"}</DetailRow>
                <DetailRow label="Seat sync"><PoolSyncState p={p} /></DetailRow>
                <DetailRow label="Notes">{p.notes ? <span className="whitespace-pre-line">{p.notes}</span> : "—"}</DetailRow>
              </DetailList>
            </Panel>
            <CustomFieldsPanel kind="license_pool" values={p.custom_field_data} />
          </div>
        )}
        {tab === "review" && <SeatReviewPanel pool={p} />}
        {tab === "history" && <ActivityTab base="license-pools" id={p.id} withChildren={false} />}
      </div>
      <DeleteDialog name={p.sku_name} open={deleting} onOpenChange={(o) => !o && setDeleting(false)} pending={pending} refusal={refusal || undefined} onConfirm={remove} />
    </>
  );
}

// ---- add and edit -------------------------------------------------------------------------------------------------------

type PoolValues = { application: PickedRecord | null; contract: string; sku_name: string; sku_id: string; seats_purchased: string; seats_assigned: string; seat_sync: boolean; notes: string };

export function NewLicensePool() {
  const me = useMe();
  const [params] = useSearchParams();
  const custom = useCustomFields("license_pool");
  if (!canWrite(me)) return <PoolReadOnly />;
  if (custom.error) return <ErrorBanner message={problemText(custom.error, "Couldn't load the custom fields.")} onRetry={custom.refetch} />;
  if (!custom.fields) return <Loading what="the form" />;
  const initial: PoolValues = {
    application: null, contract: params.get("contract") ?? "", sku_name: params.get("sku_name") ?? "", sku_id: params.get("sku_id") ?? "",
    seats_purchased: "0", seats_assigned: "0", seat_sync: false, notes: "",
  };
  return <PoolForm fields={custom.fields} initial={initial} />;
}

export function EditLicensePool() {
  const { id } = useParams();
  return <EditPool key={id} id={id!} />;
}

function EditPool({ id }: { id: string }) {
  const me = useMe();
  const custom = useCustomFields("license_pool");
  const rec = useQuery({ queryKey: [...apiKey(`license-pools/${id}`), "etag"], queryFn: () => apiWithEtag<LicensePool>(`license-pools/${id}`), gcTime: 0, retry: false, staleTime: Infinity });
  const [loaded, setLoaded] = useState<{ data: LicensePool; etag: string | null } | null>(null);
  useEffect(() => { if (rec.data && !loaded) setLoaded(rec.data); }, [rec.data, loaded]);
  const [round, setRound] = useState(0);
  if (!canWrite(me)) return <PoolReadOnly />;
  if (rec.error instanceof Problem && rec.error.status === 404) return <NotFound what="license pool" back={{ to: "/license-pools", label: "License pools" }} />;
  if (rec.error) return <ErrorBanner message={problemText(rec.error, "Couldn't load this license pool.")} onRetry={() => void rec.refetch()} />;
  if (custom.error) return <ErrorBanner message={problemText(custom.error, "Couldn't load the custom fields.")} onRetry={custom.refetch} />;
  if (!loaded || !custom.fields) return <Loading what="the license pool" />;
  const p = loaded.data;
  const initial: PoolValues = {
    application: { id: p.application.id, name: p.application.name, kind: "application" }, contract: p.contract?.id ?? "", sku_name: p.sku_name, sku_id: p.sku_id,
    seats_purchased: String(p.seats_purchased), seats_assigned: String(p.seats_assigned), seat_sync: p.seat_sync, notes: p.notes,
  };
  async function loadLatest() {
    const latest = await rec.refetch();
    if (!latest.data) return;
    setLoaded(latest.data);
    setRound((n) => n + 1);
  }
  return <PoolForm key={round} fields={custom.fields} initial={initial} existing={p} etag={loaded.etag} onLoadLatest={() => void loadLatest()} />;
}

function PoolReadOnly() {
  return <><PageHead title="You can't change license pools" desc="Editors and Admins can add and change license pools. You can read them." /><Button asChild variant="outline"><Link to="/license-pools">Back to License pools</Link></Button></>;
}

const poolBody = (v: PoolValues): Record<string, unknown> => ({
  application: v.application?.id ?? null, contract: v.contract || null, sku_name: v.sku_name.trim(), sku_id: v.sku_id.trim(),
  seats_purchased: Number(v.seats_purchased || 0), seats_assigned: Number(v.seats_assigned || 0), seat_sync: v.seat_sync, notes: v.notes,
});

function PoolForm({ fields, initial, existing, etag, onLoadLatest }: {
  fields: CustomField[]; initial: PoolValues; existing?: LicensePool; etag?: string | null; onLoadLatest?: () => void;
}) {
  const navigate = useNavigate();
  const [v, setV] = useState<PoolValues>(initial);
  const cf = useCustomFieldForm(fields, existing ? "edit" : "create", existing?.custom_field_data);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [formError, setFormError] = useState("");
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState(false);
  const dirty = JSON.stringify(v) !== JSON.stringify(initial) || cf.dirty;
  const guard = useUnsavedGuard(dirty && !saving);
  const set = <K extends keyof PoolValues>(k: K) => (value: PoolValues[K]) => setV((cur) => ({ ...cur, [k]: value }));
  const back = existing ? `/license-pools/${existing.id}` : "/license-pools";
  const over = Number(v.seats_assigned || 0) > Number(v.seats_purchased || 0);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const found: Record<string, string[]> = {};
    if (!v.application) found.application = ["Choose the application these seats are for."];
    if (!v.sku_name.trim()) found.sku_name = ["Enter the SKU or plan name, such as Microsoft 365 E5."];
    if (v.seat_sync && !v.sku_id.trim()) found.seat_sync = ["Set the SKU ID to sync seat counts from Entra."];
    const custom = cf.build();
    Object.assign(found, custom.errors);
    setErrors(found);
    setFormError("");
    setStale(false);
    if (Object.keys(found).length) return;
    const all = poolBody(v);
    const before = existing ? poolBody(initial) : null;
    const body = before ? Object.fromEntries(Object.entries(all).filter(([k, x]) => JSON.stringify(x) !== JSON.stringify(before[k]))) : all;
    if (custom.data) body.custom_field_data = custom.data;
    if (existing && !Object.keys(body).length) { navigate(back); return; }
    setSaving(true);
    try {
      const saved = await api<LicensePool>(existing ? `license-pools/${existing.id}` : "license-pools", {
        method: existing ? "PATCH" : "POST", body, headers: existing && etag ? { "If-Match": etag } : undefined,
      });
      navigate(`/license-pools/${saved.id}`, { state: savedState(saved.sku_name, existing ? "License pool saved." : "License pool added.") });
    } catch (err) {
      setSaving(false);
      if (err instanceof Problem && err.status === 412) setStale(true);
      else if (err instanceof Problem && Object.keys(err.errors).length) setErrors(err.errors);
      else setFormError(`${problemText(err)} The license pool wasn't saved. Nothing you typed was lost.`);
    }
  }
  return (
    <>
      <Breadcrumb trail={[{ to: "/license-pools", label: "License pools" }, ...(existing ? [{ to: back, label: existing.sku_name }] : [])]} current={existing ? "Edit" : "Add license pool"} />
      <PageHead title={existing ? `Edit ${existing.sku_name}` : "Add license pool"} docTitle={existing ? "Edit license pool" : "Add license pool"}
        desc="The seats of one SKU an application uses. Assigned may exceed purchased: overuse is shown, not refused." />
      {stale && onLoadLatest && <StaleBanner what="license pool" onLoad={() => { setStale(false); onLoadLatest(); }} />}
      <Card className="max-w-3xl">
        <CardContent className="pt-6">
          <form onSubmit={submit} noValidate aria-label={existing ? "Edit license pool" : "Add license pool"}>
            <fieldset disabled={saving} className="m-0 min-w-0 border-0 p-0">
              {formError && <ErrorBanner message={formError} />}
              <ErrorSummary errors={errors} focusOnShow="field" />
              <div className="grid gap-x-6 md:grid-cols-2">
                <Field id="application" label="Application (required)" errors={errors}>
                  <RecordPicker id="application" kinds={["application"]} value={v.application} onChange={set("application")} placeholder="Search applications…" listLabel="Application suggestions" />
                </Field>
                <Field id="contract" label="Contract" errors={errors} help="The contract the seats were bought under, if any.">
                  <ContractPicker id="contract" value={v.contract} known={existing?.contract ?? null} onChange={(c) => set("contract")(c?.id ?? "")} clearLabel="None" />
                </Field>
                <Field id="sku_name" label="SKU name (required)" errors={errors} help="For example: Microsoft 365 E5.">
                  <Input id="sku_name" value={v.sku_name} onChange={(e) => set("sku_name")(e.target.value)} autoComplete="off" />
                </Field>
                <Field id="sku_id" label="SKU ID" errors={errors} help="Microsoft Graph's skuId. Discovery matches sources to the pool by it.">
                  <Input id="sku_id" value={v.sku_id} onChange={(e) => set("sku_id")(e.target.value)} autoComplete="off" className="font-mono" />
                </Field>
                <Field id="seats_purchased" label="Seats purchased" errors={errors}>
                  <Input id="seats_purchased" inputMode="numeric" value={v.seats_purchased} onChange={(e) => set("seats_purchased")(e.target.value.replace(/[^0-9]/g, ""))} className="w-32" />
                </Field>
                <Field id="seats_assigned" label="Seats assigned" errors={errors} help={over ? "More than purchased: the pool will show as overused." : undefined}>
                  <Input id="seats_assigned" inputMode="numeric" value={v.seats_assigned} onChange={(e) => set("seats_assigned")(e.target.value.replace(/[^0-9]/g, ""))} className="w-32" />
                </Field>
                <div className="md:col-span-2 mb-3">
                  <label className="flex min-h-6 items-center gap-2 text-sm font-medium">
                    <input type="checkbox" id="seat_sync" checked={v.seat_sync} onChange={(e) => set("seat_sync")(e.target.checked)}
                      aria-describedby={["seat_sync-help", errors.seat_sync && "seat_sync-error"].filter(Boolean).join(" ")} aria-invalid={errors.seat_sync ? true : undefined} />
                    Sync seat counts from Entra license discovery
                  </label>
                  <p id="seat_sync-help" className="pl-6 text-sm text-muted-foreground">Discovery then overwrites both counts and sets the last sync date. Needs the SKU ID; only one pool per SKU may sync.
                    {existing && ` Last synced: ${existing.last_synced ? when(existing.last_synced) : "never"} (set by discovery only).`}</p>
                  {errors.seat_sync && <p id="seat_sync-error" className="mt-1 pl-6 text-sm text-error-foreground">{errors.seat_sync.join(" ")}</p>}
                </div>
                <div className="md:col-span-2">
                  <Field id="notes" label="Notes" errors={errors}>
                    <Textarea id="notes" rows={2} value={v.notes} onChange={(e) => set("notes")(e.target.value)} />
                  </Field>
                </div>
                <CustomFieldInputs form={cf} errors={errors} />
              </div>
              <div className="flex flex-wrap justify-end gap-2 border-t pt-4">
                <Button asChild variant="outline"><Link to={back}>Cancel</Link></Button>
                <Button type="submit">{saving ? "Saving…" : existing ? "Save license pool" : "Add license pool"}</Button>
              </div>
            </fieldset>
          </form>
          {guard}
        </CardContent>
      </Card>
    </>
  );
}
