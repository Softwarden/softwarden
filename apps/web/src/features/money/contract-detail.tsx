/** The contract page: its cost and cancellation deadline with how they were worked out, the renewal it
 *  drives ("Dates for renewal X come from this contract"), its lines with the allocation editor (fixed shares and
 *  shared lines, live totals, the API's allocation errors on the field), the renewal's cycles with what each renew
 *  decision changed, the chain of terms (renews / renewed by), its licence pools, attachments and History with its
 *  lines' and attachments' events. Archive, restore and (Admins) delete. The renew page is contract-renew.tsx. */
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, AUTO_RENEW_LABEL, periodText, Problem } from "@/api";
import { attempt, Breadcrumb, Chip, EmptyState, ErrorBanner, ErrorSummary, Field, Gap, Loading, NotFound, PageHead, TableRegion, TabStrip, useReturnFocus } from "@/components/app";
import { ArchiveDialog, DeleteDialog } from "@/components/lifecycle-dialogs";
import { MoreMenu, type MenuAction } from "@/components/more-menu";
import { RecordPicker, type PickedRecord } from "@/components/record-picker";
import { DetailList, DetailRow, LifecycleChip, Panel, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ActivityTab } from "@/features/admin/audit";
import { AttachmentsTab, hasAttachments, useAttachmentsLabel } from "@/features/attachments/tab";
import { CustomFieldsPanel } from "@/features/catalog/custom-fields";
import { DeadlineCell, decisionText } from "@/features/money/renewals";
import { termsChangeText } from "@/features/money/renewal-detail";
import { KIND_LABEL, isCatalogKind, recordPath } from "@/lib/kinds";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { canWrite, useMe, useLandingNotice } from "@/lib/session";
import {
  FREQUENCY_LABEL, fromHundredths, hundredths, percentText, type ContractDetail, type ContractLine, type ContractRenewalCycle,
} from "./contract-types";
import { ContractStatusChip, Money, OverusedChip, SeatSyncText, UtilisationMeter } from "./contract-ui";
import { OpenedEdit, type OpenedWrite } from "@/components/opened-edit";
import { ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";

type Tab = "overview" | "lines" | "renewal" | "pools" | "attachments" | "history";
const TABS: Tab[] = ["overview", "lines", "renewal", "pools", "attachments", "history"];

export function ContractDetailPage() {
  const { id } = useParams();
  return <ContractPage key={id} id={id!} />;
}

function ContractPage({ id }: { id: string }) {
  const me = useMe();
  const write = canWrite(me);
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab") as Tab | null;
  const load = useApiQuery<ContractDetail>(`contracts/${id}`);
  const invalidate = useInvalidate();
  const attachmentsLabel = useAttachmentsLabel(me, "contract", id);
  const tab: Tab = asked && TABS.includes(asked) && (asked !== "attachments" || attachmentsLabel) ? asked : "overview";
  const [notice, setNotice, noticeQuiet] = useLandingNotice();
  const [dialog, setDialog] = useState<null | "archive" | "delete">(null);
  // The delete confirmation reads the contract with its ETag as it opens; the delete sends it as If-Match.
  const deleteRead = useOpenedRecord(dialog === "delete" ? `contracts/${id}` : null);
  const [pending, setPending] = useState(false);
  const [refusal, setRefusal] = useState("");
  const c = load.data;
  if (load.error) {
    const missing = load.error instanceof Problem && load.error.status === 404;
    if (missing) return <NotFound what="contract" back={{ to: "/contracts", label: "Contracts" }} trail={[{ to: "/contracts", label: "Contracts" }]} />;
    return (
      <>
        <Breadcrumb trail={[{ to: "/contracts", label: "Contracts" }]} current="Contract" />
        <PageHead title={missing ? "Contract not found" : "Couldn't load this contract"} />
        <ErrorBanner message={missing ? "This contract doesn't exist or you don't have access." : `${problemText(load.error)} Nothing was changed.`}
          onRetry={missing ? undefined : () => void load.refetch()} />
      </>
    );
  }
  if (!c) return <Loading what="contract" rows={4} />;
  const reload = () => void invalidate("contracts", "renewals", "license-pools");
  const done = (message: string) => { setDialog(null); setRefusal(""); setNotice(message); reload(); };
  async function lifecycle(path: string, method: string, message: string, after?: () => void) {
    setPending(true);
    setRefusal("");
    const failed = await attempt(async () => api(path, { method, headers: method === "DELETE" ? ifMatch(await deleteRead.etag()) : undefined }));
    setPending(false);
    if (failed?.status === 412) return setRefusal(staleDeleteText("contract"));
    if (failed) return setRefusal(failed.detail);
    if (after) return after();
    done(message);
  }
  const actions: MenuAction[] = write ? [
    c.archived ? { label: "Restore", onSelect: () => void lifecycle(`contracts/${id}/restore`, "POST", "Restored.") }
      : { label: "Archive", onSelect: () => { setRefusal(""); setDialog("archive"); } },
    ...(me.role === "admin" ? [{ label: "Delete", danger: true, onSelect: () => { setRefusal(""); setDialog("delete"); } }] : []),
  ] : [];
  const canRenew = write && !c.archived && !c.successor && ["signed", "active", "pending_renewal"].includes(c.status);
  const tabs: { id: Tab; label: string }[] = [
    { id: "overview", label: "Overview" },
    { id: "lines", label: `Lines (${c.lines.length})` },
    { id: "renewal", label: c.renewal ? `Renewal (${(c.renewal.history?.length ?? 0) + 1} ${(c.renewal.history?.length ?? 0) === 0 ? "cycle" : "cycles"})` : "Renewal" },
    { id: "pools", label: `License pools (${c.license_pools.length})` },
    ...(attachmentsLabel ? [{ id: "attachments" as const, label: attachmentsLabel }] : []),
    { id: "history", label: "History" },
  ];
  return (
    <>
      <Breadcrumb trail={[{ to: "/contracts", label: "Contracts" }]} current={c.name} />
      <PageHead title={<span className="[overflow-wrap:anywhere]">{c.name}</span>} docTitle={c.name}
        desc={<span className="inline-flex flex-wrap items-center gap-2">
          <ContractStatusChip status={c.status} />
          <span>{c.vendor ? c.vendor.name : <Gap label="No vendor" />}</span>
          {c.annualized_cost && <span>· <Money amount={c.annualized_cost} currency={c.currency} /> a year</span>}
          {c.archived && <Chip>Archived</Chip>}
        </span>}>
        {write && <Button asChild variant="outline"><Link to={`/contracts/${id}/edit`}>Edit</Link></Button>}
        {canRenew && <Button asChild><Link to={`/contracts/${id}/renew`}>Renew or cancel…</Link></Button>}
        <MoreMenu actions={actions} about={c.name} />
      </PageHead>
      <SuccessNote text={notice} quiet={noticeQuiet} />
      {refusal && !dialog && <ErrorBanner message={refusal} />}
      {c.archived && <p className="mb-4 rounded-md border bg-muted px-3 py-2">Archived. It no longer drives a renewal or counts in spend, and its history is kept. {write && "Restore it from More."}</p>}
      <ChainNote c={c} />
      <TabStrip tabs={tabs} tab={tab} label="Contract sections" onSelect={(t) => setParams(t === "overview" ? {} : { tab: t })} />
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "overview" && <Overview c={c} />}
        {tab === "lines" && <Lines c={c} write={write && !c.archived} onChanged={(m) => { setNotice(m); reload(); }} />}
        {tab === "renewal" && <RenewalPanel c={c} canRenew={canRenew} />}
        {tab === "pools" && <Pools c={c} write={write} />}
        {tab === "attachments" && hasAttachments(me) && <AttachmentsTab parent={{ type: "contract", id: c.id, name: c.name }} />}
        {tab === "history" && <ActivityTab base="contracts" id={c.id} />}
      </div>
      <ArchiveDialog name={c.name} open={dialog === "archive"} onOpenChange={(o) => !o && setDialog(null)} pending={pending} refusal={refusal || undefined}
        onConfirm={() => lifecycle(`contracts/${id}/archive`, "POST", "Archived. It no longer drives its renewal.")} />
      <DeleteDialog name={c.name} open={dialog === "delete"} onOpenChange={(o) => !o && setDialog(null)} pending={pending} refusal={refusal || undefined}
        onConfirm={() => lifecycle(`contracts/${id}`, "DELETE", "", () => navigate("/contracts", { state: { notice: `${c.name} deleted.` } }))}
        onArchiveInstead={() => { setRefusal(""); setDialog("archive"); }} />
    </>
  );
}

/** The chain of terms: the one this renews, and the one that renews it. */
function ChainNote({ c }: { c: ContractDetail }) {
  if (!c.predecessor && !c.successor) return null;
  return (
    <p className="mb-4 flex flex-wrap gap-x-6 gap-y-1 rounded-md border bg-card px-3 py-2 text-sm" data-testid="contract-chain">
      {c.predecessor && <span>Renews <Link to={`/contracts/${c.predecessor.id}`} className="underline">{c.predecessor.name}</Link> (the previous term)</span>}
      {c.successor && <span><span aria-hidden="true">↻ </span>Renewed by <Link to={`/contracts/${c.successor.id}`} className="underline">{c.successor.name}</Link> (the next term)</span>}
    </p>
  );
}

const IconMoney = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="1.5" y="4" width="13" height="8" rx="1.5" /><circle cx="8" cy="8" r="1.8" /></svg>;
const IconClock = <span aria-hidden="true">◷</span>;
const IconLines = <span aria-hidden="true">≡</span>;

function Overview({ c }: { c: ContractDetail }) {
  return (
    <div className="grid gap-5">
      <Panel id="h-deadline" icon={IconClock} title="Cancellation deadline">
        <div className="grid gap-2 px-4 py-3">
          <DeadlineCell deadline={c.deadline} closed={["cancelled", "expired", "renewed"].includes(c.status)} />
          <p>{c.deadline.explanation}</p>
          <RenewalLinkNote c={c} />
        </div>
      </Panel>
      <div className="grid gap-5 lg:grid-cols-2">
        <Panel id="h-money" icon={IconMoney} title="Cost">
          <DetailList>
            <DetailRow label="Amount">{c.amount ? <><Money amount={c.amount} currency={c.currency} /> <span className="text-muted-foreground">{FREQUENCY_LABEL[c.billing_frequency].toLowerCase()}</span></> : <Gap label="No amount" />}</DetailRow>
            <DetailRow label="Annualized cost">{c.annualized_cost ? <Money amount={c.annualized_cost} currency={c.currency} /> : "—"}<span className="block text-sm text-muted-foreground">{c.annualization_note}</span></DetailRow>
            <DetailRow label="Cost center">{c.cost_center || "—"}</DetailRow>
            <DetailRow label="Contract number">{c.contract_number || "—"}</DetailRow>
            <DetailRow label="PO number">{c.po_number || "—"}</DetailRow>
          </DetailList>
        </Panel>
        <Panel id="h-term" icon={IconLines} title="Term">
          <DetailList>
            <DetailRow label="Start date">{c.start_date ?? "—"}</DetailRow>
            <DetailRow label="End date">{c.end_date ?? <Gap label="No end date" />}</DetailRow>
            <DetailRow label="Term">{c.term_months != null ? `${c.term_months} ${c.term_months === 1 ? "month" : "months"}` : "—"}</DetailRow>
            <DetailRow label="Auto-renews">{c.auto_renew === "unknown" ? <Gap label="Auto-renewal Unknown" /> : AUTO_RENEW_LABEL[c.auto_renew]}</DetailRow>
            <DetailRow label="Notice period">{periodText(c.notice)}</DetailRow>
            <DetailRow label="Renewal owner">{c.renewal_owner ? <>{c.renewal_owner.name}{!c.renewal_owner.active && <span className="text-muted-foreground"> (inactive)</span>}</> : <Gap label="No renewal owner" />}</DetailRow>
          </DetailList>
        </Panel>
      </div>
      {c.notes && (
        <Panel id="h-notes" icon={IconLines} title="Notes">
          <p className="whitespace-pre-line px-4 py-3 [overflow-wrap:anywhere]">{c.notes}</p>
        </Panel>
      )}
      <CustomFieldsPanel kind="contract" values={c.custom_field_data} />
    </div>
  );
}

/** Whether the linked renewal takes its dates from this contract, in words. */
function RenewalLinkNote({ c }: { c: ContractDetail }) {
  const r = c.renewal_obligation;
  if (!r) return <p className="text-sm text-muted-foreground">Not linked to a renewal, so no reminders are sent for this deadline. Edit the contract to link or create one.</p>;
  const link = <Link to={`/renewals/${r.id}`} className="underline">{r.name}</Link>;
  if (c.drives_renewal) {
    return (
      <p className="rounded-md border border-l-4 border-info-border bg-info-bg px-3 py-2 text-sm text-info-foreground" data-testid="drives-renewal">
        <span aria-hidden="true">ⓘ </span>Dates for renewal {link} come from this contract: its open cycle takes this end date, notice period and auto-renewal, so its reminders and this deadline are the same date.
      </p>
    );
  }
  const driver = c.renewal?.contract;
  return (
    <p className="text-sm">
      Linked to renewal {link}, but its dates {driver ? <>come from the contract <Link to={`/contracts/${driver.id}`} className="underline">{driver.name}</Link></> : "aren't taken from any contract now"}
      {c.status === "renewed" ? ": this term was renewed." : c.archived ? ": this contract is archived." : ["cancelled", "expired"].includes(c.status) ? `: this contract is ${c.status}.` : "."}
    </p>
  );
}

// ---- lines --------------------------------------------------------------------------------------------------------------

function Lines({ c, write, onChanged }: { c: ContractDetail; write: boolean; onChanged: (message: string) => void }) {
  const [dialog, setDialog] = useState<{ mode: "add" } | { mode: "edit" | "remove"; line: ContractLine } | null>(null);
  const fixedTotal = hundredths(c.allocated_percent) ?? 0;
  const close = () => setDialog(null);
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-md border bg-card px-4 py-3" role="status" aria-live="polite" data-testid="allocation-totals">
        <span><span className="font-semibold">Fixed shares: </span><span className="tabular">{percentText(c.allocated_percent)}%</span> of 100%</span>
        <span>
          <span className="font-semibold">Not allocated: </span>
          {c.unallocated ? <><span className="tabular">{percentText(c.unallocated.share_percent)}%</span>{c.unallocated.annual_amount && <> (<Money amount={c.unallocated.annual_amount} currency={c.currency} /> a year)</>}</> : "nothing"}
        </span>
        {write && <Button size="sm" variant="outline" className="ml-auto" onClick={() => setDialog({ mode: "add" })}>Add line</Button>}
      </div>
      {c.lines.length === 0 ? (
        <EmptyState title="No lines yet">
          Lines say which applications, components or AI agents this contract pays for. Give a line a fixed share, or leave it to share what the fixed ones leave, evenly.
          {!write && " Editors and Admins can add them."}
        </EmptyState>
      ) : (
        <TableRegion label="Contract lines table">
          <Table>
            <TableCaption className="sr-only">Lines of {c.name}, oldest first, with each one's share of the annualized cost</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Record</TableHead><TableHead scope="col">Allocation</TableHead><TableHead scope="col" className="text-right">Share</TableHead>
              <TableHead scope="col" className="text-right">A year</TableHead>{write && <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>}
            </TableRow></TableHeader>
            <TableBody>
              {c.lines.map((l) => {
                const href = isCatalogKind(l.record.kind) ? recordPath(l.record.kind, l.record.id) : null;
                return (
                  <TableRow key={l.id} className="align-top">
                    <TableHead scope="row" className="whitespace-normal font-semibold text-foreground [overflow-wrap:anywhere]">
                      {href ? <Link to={href} className="underline">{l.record.name}</Link> : l.record.name}
                      <span className="block text-xs font-normal text-muted-foreground">{isCatalogKind(l.record.kind) ? KIND_LABEL[l.record.kind] : l.record.kind}</span>
                      {l.record.lifecycle === "retired" && <LifecycleChip value="retired" className="mt-1" />}{l.record.archived && <Chip className="mt-1">Archived</Chip>}
                    </TableHead>
                    <TableCell>{l.allocation_percent != null ? `Fixed ${percentText(l.allocation_percent)}%` : "Shares the rest"}</TableCell>
                    <TableCell className="tabular text-right">{l.share_percent != null ? `${percentText(l.share_percent)}%` : "—"}</TableCell>
                    <TableCell className="text-right">{l.annual_amount ? <Money amount={l.annual_amount} currency={c.currency} /> : "—"}</TableCell>
                    {write && (
                      <TableCell className="text-right">
                        <span className="inline-flex flex-wrap justify-end gap-2">
                          <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "edit", line: l })}>Change share<span className="sr-only"> of {l.record.name}</span></Button>
                          <Button size="sm" variant="outline" onClick={() => setDialog({ mode: "remove", line: l })}>Remove<span className="sr-only"> {l.record.name}</span></Button>
                        </span>
                      </TableCell>
                    )}
                  </TableRow>
                );
              })}
              {c.unallocated && (
                <TableRow className="align-top text-muted-foreground">
                  <TableHead scope="row" className="font-normal">Not allocated to a record</TableHead>
                  <TableCell>—</TableCell>
                  <TableCell className="tabular text-right">{percentText(c.unallocated.share_percent)}%</TableCell>
                  <TableCell className="text-right">{c.unallocated.annual_amount ? <Money amount={c.unallocated.annual_amount} currency={c.currency} /> : "—"}</TableCell>
                  {write && <TableCell />}
                </TableRow>
              )}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <LineDialog c={c} state={dialog} fixedTotal={fixedTotal} onClose={close} onDone={(m) => { close(); onChanged(m); }} />
    </div>
  );
}

function LineDialog({ c, state, fixedTotal, onClose, onDone }: {
  c: ContractDetail; state: { mode: "add" } | { mode: "edit" | "remove"; line: ContractLine } | null; fixedTotal: number;
  onClose: () => void; onDone: (message: string) => void;
}) {
  const open = state !== null;
  const returnFocus = useReturnFocus(open);
  const line = state && state.mode !== "add" ? state.line : null;
  const title = !state ? "" : state.mode === "add" ? "Add a line" : state.mode === "edit" ? `Change the share of ${line!.record.name}` : `Remove ${line!.record.name} from this contract?`;
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{state?.mode === "remove"
            ? "Its share goes back to the other lines (or to Not allocated). The record itself isn't changed, and both Histories keep the line."
            : "A fixed share takes that percentage of the annualized cost; lines without one share what the fixed ones leave, evenly. Fixed shares can total 100% at most."}</DialogDescription>
        </DialogHeader>
        {state && state.mode !== "edit" && <LineForm key={`${state.mode}-${line?.id ?? "new"}`} c={c} mode={state.mode} line={line} fixedTotal={fixedTotal} onClose={onClose} onDone={onDone} />}
        {state?.mode === "edit" && (
          // The share starts from the line read with its ETag, which the save sends back as If-Match.
          <OpenedEdit<ContractLine> key={`edit-${line!.id}`} path={`contracts/${c.id}/lines/${line!.id}`} what="line">
            {(fresh, write) => <LineForm c={c} mode="edit" line={fresh} write={write} fixedTotal={fixedTotal} onClose={onClose} onDone={onDone} />}
          </OpenedEdit>
        )}
      </DialogContent>
    </Dialog>
  );
}

function LineForm({ c, mode, line, write, fixedTotal, onClose, onDone }: {
  c: ContractDetail; mode: "add" | "edit" | "remove"; line: ContractLine | null; write?: OpenedWrite; fixedTotal: number; onClose: () => void; onDone: (message: string) => void;
}) {
  // A removal reads the line with its ETag as the confirmation opens and sends it as If-Match.
  const removing = useOpenedRecord(mode === "remove" && line ? `contracts/${c.id}/lines/${line.id}` : null);
  const [record, setRecord] = useState<PickedRecord | null>(null);
  const [shared, setShared] = useState(line ? line.allocation_percent == null : false);
  const [percent, setPercent] = useState(line?.allocation_percent ? percentText(line.allocation_percent) : "");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  // A refusal that isn't about a field shown here (a 409, or an error on a hidden field) is said in a banner, not filed
  // under a field that may be hidden.
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const own = line?.allocation_percent ? hundredths(line.allocation_percent) ?? 0 : 0;
  const typed = shared ? 0 : hundredths(percent);
  const after = typed == null ? null : fixedTotal - own + typed;
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (mode === "remove") {
      setBusy(true);
      const failed = await attempt(async () => api(`contracts/${c.id}/lines/${line!.id}`, { method: "DELETE", headers: ifMatch(await removing.etag()) }));
      setBusy(false);
      if (failed?.status === 412) return setErrors({ remove: [staleDeleteText("line", "remove")] });
      if (failed) return setErrors({ remove: [failed.detail] });
      return onDone(`${line!.record.name} removed from the contract.`);
    }
    const found: Record<string, string[]> = {};
    if (mode === "add" && !record) found.record = ["Choose an application, component or AI agent."];
    if (!shared && (percent.trim() === "" || hundredths(percent) == null)) found.allocation_percent = ["Enter a percentage from 0 to 100 with up to two decimal places, or choose Shares the rest."];
    setErrors(found);
    setProblem("");
    if (Object.keys(found).length) return;
    const allocation = shared ? null : percent.trim();
    setBusy(true);
    try {
      if (mode === "add") await api(`contracts/${c.id}/lines`, { method: "POST", body: { record: record!.id, allocation_percent: allocation } });
      else await api(`contracts/${c.id}/lines/${line!.id}`, { method: "PATCH", body: { allocation_percent: allocation }, headers: await write?.ifMatch() });
      onDone(mode === "add" ? `${record!.name} added${allocation ? ` with a fixed ${allocation}%` : ", sharing the rest"}.` : `Share of ${line!.record.name} saved.`);
    } catch (err) {
      setBusy(false);
      if (isStaleWrite(err) && write) return write.onStale();
      const shown = new Set([...(mode === "add" ? ["record"] : []), ...(shared ? [] : ["allocation_percent"])]);
      const fieldErrors = err instanceof Problem ? Object.fromEntries(Object.entries(err.errors).filter(([k]) => shown.has(k))) : {};
      const other = err instanceof Problem ? Object.entries(err.errors).filter(([k]) => !shown.has(k)).flatMap(([, m]) => m) : [];
      setErrors(fieldErrors);
      setProblem(Object.keys(fieldErrors).length && !other.length ? "" : `${other.join(" ") || problemText(err)} Nothing was saved.`);
    }
  }
  if (mode === "remove") {
    return (
      <form onSubmit={submit} noValidate>
        {errors.remove && <ErrorBanner message={errors.remove.join(" ")} />}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="destructive" disabled={busy}>{busy ? "Removing…" : `Remove ${line!.record.name}`}</Button>
        </DialogFooter>
      </form>
    );
  }
  return (
    <form onSubmit={submit} noValidate aria-label={mode === "add" ? "Add a line" : "Change the share"}>
      {problem && <ErrorBanner message={problem} />}
      <ErrorSummary errors={errors} />
      {mode === "add" && (
        <Field id="record" label="Record (required)" errors={errors} help="An application, component or AI agent. Each can be on a contract once.">
          <RecordPicker id="record" kinds={["application", "component", "ai_agent"]} value={record} onChange={setRecord} placeholder="Search records…" listLabel="Record suggestions" />
        </Field>
      )}
      <fieldset className="mb-3">
        <legend className="mb-1.5 text-sm font-medium">Allocation</legend>
        <div className="flex flex-col gap-2">
          <label className="flex min-h-6 items-center gap-2 text-sm">
            <input type="radio" name="allocation" checked={!shared} onChange={() => setShared(false)} className="h-4 w-4 accent-primary" />Fixed share
          </label>
          <label className="flex min-h-6 items-center gap-2 text-sm">
            <input type="radio" name="allocation" checked={shared} onChange={() => setShared(true)} className="h-4 w-4 accent-primary" />Shares the rest (evenly with other such lines)
          </label>
        </div>
      </fieldset>
      {!shared && (
        <Field id="allocation_percent" label="Fixed share (%)" errors={errors} help="0 to 100, up to two decimal places.">
          <Input id="allocation_percent" inputMode="decimal" value={percent} onChange={(e) => setPercent(e.target.value.replace(/[^0-9.]/g, ""))} className="w-32" autoComplete="off" />
        </Field>
      )}
      <p className="mb-3 text-sm" role="status" aria-live="polite" data-testid="allocation-after">
        {after == null ? "Fixed shares now total " + percentText(c.allocated_percent) + "%."
          : after > 10000 ? <><span aria-hidden="true">! </span>Fixed shares would total {fromHundredths(after)}%, over 100%. The contract won't accept it.</>
            : `Fixed shares would total ${fromHundredths(after)}%, leaving ${fromHundredths(10000 - after)}% for shared lines (or Not allocated).`}
      </p>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? "Saving…" : mode === "add" ? "Add line" : "Save share"}</Button>
      </DialogFooter>
    </form>
  );
}

// ---- renewal ------------------------------------------------------------------------------------------------------------

function RenewalPanel({ c, canRenew }: { c: ContractDetail; canRenew: boolean }) {
  const r = c.renewal;
  if (!r) {
    return (
      <EmptyState title="Not linked to a renewal" action={canRenew ? <Button asChild variant="outline"><Link to={`/contracts/${c.id}/renew`}>Renew or cancel…</Link></Button> : undefined}>
        Link a renewal (or create one) from Edit so reminders follow this contract's cancellation deadline. A contract without one can still be renewed or canceled here.
      </EmptyState>
    );
  }
  const cycles: ContractRenewalCycle[] = [r.cycle, ...(r.history ?? [])];
  return (
    <div className="grid gap-4">
      <div className="rounded-md border bg-card px-4 py-3">
        <p><span className="font-semibold">Renewal: </span><Link to={`/renewals/${r.id}`} className="underline">{r.name}</Link></p>
        <RenewalLinkNote c={c} />
        {r.cycle.open && <p className="mt-2 text-sm">Open cycle {r.cycle.sequence}: {decisionText(r.cycle)}{r.cycle.decision ? ", not completed" : ""}. {canRenew && "Record the decision with Renew or cancel: it completes this cycle and, renewing, creates the next term."}</p>}
      </div>
      <TableRegion label="Renewal cycles table">
        <Table>
          <TableCaption className="sr-only">Cycles of {r.name}, newest first, with each decision and what it changed</TableCaption>
          <TableHeader><TableRow>
            <TableHead scope="col">#</TableHead><TableHead scope="col">Renewal date</TableHead><TableHead scope="col">Action deadline</TableHead>
            <TableHead scope="col">Decision</TableHead><TableHead scope="col">Completed</TableHead><TableHead scope="col">Terms change</TableHead>
          </TableRow></TableHeader>
          <TableBody>
            {cycles.map((cy) => (
              <TableRow key={cy.id} className="align-top">
                <TableHead scope="row" className="tabular">{cy.sequence}{cy.open && <span className="block text-xs font-normal text-muted-foreground">Open</span>}</TableHead>
                <TableCell className="tabular">{cy.renewal_date ?? "—"}</TableCell>
                <TableCell className="whitespace-normal">{cy.deadline.date ? <DeadlineCell deadline={cy.deadline} closed={!cy.open} /> : "—"}</TableCell>
                <TableCell className="whitespace-normal">{decisionText(cy)}{cy.decision_notes && <span className="block text-sm text-muted-foreground [overflow-wrap:anywhere]">{cy.decision_notes}</span>}</TableCell>
                <TableCell className="whitespace-normal">{cy.completed_on ?? "—"}{cy.completion_evidence && <span className="block text-sm text-muted-foreground [overflow-wrap:anywhere]">{cy.completion_evidence}</span>}</TableCell>
                <TableCell className="whitespace-normal">{termsChangeText(cy.terms_change) ?? "—"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableRegion>
    </div>
  );
}

// ---- licence pools ------------------------------------------------------------------------------------------------------

function Pools({ c, write }: { c: ContractDetail; write: boolean }) {
  const add = write && <Button asChild variant="outline" size="sm"><Link to={`/license-pools/new?contract=${c.id}`}>Add license pool</Link></Button>;
  if (c.license_pools.length === 0) {
    return <EmptyState title="No license pools" action={add || undefined}>License pools count the seats bought under this contract and how many are assigned.{!write && " Editors and Admins can add them."}</EmptyState>;
  }
  return (
    <div className="grid gap-3">
      {add && <div className="flex justify-end">{add}</div>}
      <TableRegion label="License pools of this contract table">
        <Table>
          <TableCaption className="sr-only">License pools bought under {c.name}, by SKU name</TableCaption>
          <TableHeader><TableRow>
            <TableHead scope="col">SKU</TableHead><TableHead scope="col">Application</TableHead><TableHead scope="col">Utilization</TableHead><TableHead scope="col">Seat sync</TableHead>
          </TableRow></TableHeader>
          <TableBody>
            {c.license_pools.map((p) => (
              <TableRow key={p.id} className="align-top">
                <TableHead scope="row" className="whitespace-normal font-semibold text-foreground"><Link to={`/license-pools/${p.id}`} className="underline">{p.sku_name}</Link>{p.sku_id && <span className="block font-mono text-xs font-normal text-muted-foreground">{p.sku_id}</span>}</TableHead>
                <TableCell className="whitespace-normal"><Link to={recordPath("application", p.application.id)!}>{p.application.name}</Link></TableCell>
                <TableCell className="whitespace-normal">
                  <UtilisationMeter purchased={p.seats_purchased} assigned={p.seats_assigned} utilization={p.utilization} />
                  {p.overused && <span className="mt-1 block"><OverusedChip by={-p.seats_available} /></span>}
                </TableCell>
                <TableCell className="whitespace-normal"><SeatSyncText sync={p.seat_sync} lastSynced={p.last_synced} /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableRegion>
    </div>
  );
}
