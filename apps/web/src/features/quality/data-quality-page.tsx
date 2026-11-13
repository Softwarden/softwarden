/** Data quality: `/data-quality`, loaded on first use. Three tabs, all state in the URL:
 *
 *  - Failing records (default): live records failing a check that is on, after exceptions, filtered by `rule`, `kind`
 *    and `it_steward` (a user ID, or `none`); Needs attention's Data quality rows and the digest link here. Sorted by
 *    name or completeness, keyset pages (RecordTable).
 *  - Checks: every rule with its status (on, off, unavailable and why) and, while on, its counts.
 *  - Exceptions: "not applicable" marks, filterable by `status` (current / expired), `owner=left` (the accountable
 *    person has left: it can't be extended until reassigned) and `record`. Editors and Admins add, change (extend-only)
 *    and remove them. */
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { cap, SEVERITY_LABEL, type Page, type QualityException, type QualityFailure, type QualityRule, type UserRef } from "@/api";
import { AllClear, ChipGroup, EmptyState, ErrorBanner, Gap, Loading, PageHead, Select, TableRegion, TabStrip, useReturnFocus } from "@/components/app";
import { RecordLink } from "@/components/record-link";
import { RecordTable, useRecordTable, type RecordColumn } from "@/components/record-table";
import { chip, LifecycleChip } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api } from "@/api";
import { KIND_LABEL, KIND_PLURAL, recordPath, type CatalogKind } from "@/lib/kinds";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { ifMatch, isStaleWrite, staleDeleteText, useOpenedRecord } from "@/lib/if-match";
import { canWrite, useMe } from "@/lib/session";
import { CompletenessBar, ExceptedChip, hasQuality, RuleStatusChip, useQualityRules } from "./completeness";
import { ExceptionDialog } from "./exception-dialog";

type TabId = "failures" | "rules" | "exceptions";
const TABS: { id: TabId; label: string }[] = [
  { id: "failures", label: "Failing records" },
  { id: "rules", label: "Checks" },
  { id: "exceptions", label: "Exceptions" },
];
/** Each tab's own URL parameters; switching tabs drops the others'. */
const TAB_PARAMS: Record<TabId, string[]> = {
  failures: ["rule", "kind", "it_steward", "ordering", "cursor", "page", "page_size"],
  rules: [],
  exceptions: ["status", "owner", "record"],
};

export function DataQualityPage() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const tab = TABS.find((t) => t.id === params.get("tab"))?.id ?? "failures";
  const [adding, setAdding] = useState(false);
  const [notice, setNotice] = useState("");
  const setTab = (t: TabId) => setParams((cur) => {
    const next = new URLSearchParams();
    if (t !== "failures") next.set("tab", t);
    for (const k of TAB_PARAMS[t]) if (cur.get(k)) next.set(k, cur.get(k)!);
    return next;
  });
  if (!hasQuality(me)) {
    return (
      <>
        <PageHead title="Data quality" />
        <EmptyState title="Data quality isn't available in this organization">Its edition doesn't include data-quality checks.</EmptyState>
      </>
    );
  }
  const write = canWrite(me);
  return (
    <>
      {/* Each tab has its own document title, so tabs, history and screen readers tell them apart. */}
      <PageHead title="Data quality" docTitle={`${TABS.find((t) => t.id === tab)!.label} · Data quality`} desc="Checks on every live record (not archived, not retired). A check marked not applicable for a record doesn't count against it until its exception expires.">
        {write && <Button onClick={() => setAdding(true)}>Add exception</Button>}
      </PageHead>
      <p className="sr-only" role="status">{notice}</p>
      {notice && <p className="mb-4 flex items-center gap-2 rounded-md border border-success-border bg-success-bg px-3 py-2 text-success-foreground" aria-hidden="true"><span>✓</span>{notice}</p>}
      <TabStrip tabs={TABS} tab={tab} onSelect={setTab} label="Data quality sections" />
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "failures" && <FailuresTab />}
        {tab === "rules" && <RulesTab />}
        {tab === "exceptions" && <ExceptionsTab onNotice={setNotice} />}
      </div>
      <ExceptionDialog open={adding} onClose={() => setAdding(false)} onDone={(t) => { setAdding(false); setNotice(t); }} />
    </>
  );
}

// ---- failing records -----------------------------------------------------------------------------------------------

function FailuresTab() {
  const me = useMe();
  const [params, setParams] = useSearchParams();
  const rules = useQualityRules();
  const stewards = useApiQuery<UserRef[]>("stewards", { staleTime: 30_000 });
  const rule = params.get("rule") ?? "";
  const kind = params.get("kind") ?? "";
  const steward = params.get("it_steward") ?? "";
  const query: Record<string, string> = { ...(rule ? { rule } : {}), ...(kind ? { kind } : {}), ...(steward ? { it_steward: steward } : {}) };
  const table = useRecordTable<QualityFailure>({ id: "data-quality-failures", path: "data-quality/failures", query });
  const set = (k: string, v: string) => setParams((cur) => {
    const next = new URLSearchParams(cur);
    if (v) next.set(k, v); else next.delete(k);
    next.delete("cursor"); next.delete("page");
    return next;
  });
  const clear = () => setParams((cur) => {
    const next = new URLSearchParams(cur);
    for (const k of ["rule", "kind", "it_steward", "cursor", "page"]) next.delete(k);
    return next;
  });
  const filtered = !!(rule || kind || steward);
  const on = (rules.data ?? []).filter((r) => r.status === "on");
  // AI agents with `ai_agents`; integrations (checked by the data-flow rules only) with `data_flows`.
  const kinds: CatalogKind[] = [
    "application", "component",
    ...(me.capabilities.includes("ai_agents") ? ["ai_agent" as const] : []),
    ...(me.capabilities.includes("data_flows") ? ["integration" as const] : []),
  ];
  const ruleLabel = rules.data?.find((r) => r.id === rule)?.label;
  const stewardName = steward === "none" ? "no IT steward" : stewards.data?.find((u) => String(u.id) === steward)?.name;

  const columns: RecordColumn<QualityFailure>[] = [
    { id: "name", header: "Record", sortField: "name", cell: () => null },
    { id: "kind", header: "Kind", cell: (r) => KIND_LABEL[r.kind] ?? r.kind },
    { id: "lifecycle", header: "Lifecycle", cell: (r) => <LifecycleChip value={r.lifecycle} /> },
    { id: "completeness", header: "Completeness", sortField: "completeness", sortText: ["least complete first", "most complete first"], cell: (r) => <CompletenessBar score={r.quality.score} /> },
    {
      id: "failing", header: "Failing checks", className: "whitespace-normal min-w-64",
      cell: (r) => <ChipGroup>{r.quality.failing.map((f) => <Gap key={f.id} label={f.label} />)}{r.quality.excepted.map((x) => <ExceptedChip key={x.id} label={x.label} />)}</ChipGroup>,
    },
    { id: "it_steward", header: "IT steward", cell: (r) => r.it_steward?.name ?? <span className={chip({ state: "neutral" })}>Unassigned</span> },
  ];
  return (
    <>
      <div className="mb-4 flex flex-wrap items-end gap-3" role="group" aria-label="Filters">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="dq-rule">Check</Label>
          <Select id="dq-rule" className="w-64" value={rule} onChange={(e) => set("rule", e.target.value)}>
            <option value="">Any check that is on</option>
            {on.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
            {rule && !on.some((r) => r.id === rule) && <option value={rule}>{ruleLabel ?? rule}</option>}
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="dq-kind">Kind</Label>
          <Select id="dq-kind" className="w-44" value={kind} onChange={(e) => set("kind", e.target.value)}>
            <option value="">Every kind</option>
            {kinds.map((k) => <option key={k} value={k}>{KIND_PLURAL[k]}</option>)}
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="dq-steward">IT steward</Label>
          <Select id="dq-steward" className="w-56" value={steward} onChange={(e) => set("it_steward", e.target.value)}>
            <option value="">Anyone</option>
            <option value="none">No IT steward</option>
            {(stewards.data ?? []).map((u) => <option key={u.id} value={String(u.id)}>{u.name}{u.id === me.user.id ? " (you)" : ""}</option>)}
            {steward && steward !== "none" && !stewards.data?.some((u) => String(u.id) === steward) && <option value={steward}>{stewards.data ? "A former steward" : "Loading…"}</option>}
          </Select>
        </div>
        {filtered && <Button variant="outline" size="sm" onClick={clear}>Clear filters</Button>}
      </div>
      {filtered && (
        <p className="mb-3 text-sm text-muted-foreground">
          Records failing {ruleLabel ? <strong className="text-foreground">{ruleLabel}</strong> : "any check"}
          {kind && <> among {KIND_PLURAL[kind as CatalogKind]?.toLowerCase().replace(/^ai /, "AI ") ?? kind}</>}
          {stewardName && <> for {stewardName}</>}.
        </p>
      )}
      <RecordTable table={table} caption="Records failing data-quality checks" noun={["record", "records"]} columns={columns} rowHeader="name"
        rowHref={(r) => recordPath(r.kind, r.id) ?? "/data-quality"} selectable={false} defaultOrder={{ text: "name, A to Z", field: "name" }}
        onClearFilters={clear}
        empty={filtered
          ? <EmptyState title="No records fail this filter" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>Every live record matching these filters passes, or has an exception.</EmptyState>
          : <AllClear>Every live record passes every check that is on, or has an exception.</AllClear>} />
    </>
  );
}

// ---- checks ----------------------------------------------------------------------------------------------------------

function RulesTab() {
  const me = useMe();
  const rules = useQualityRules();
  if (rules.isError) return <ErrorBanner message={problemText(rules.error, "Couldn't load the checks.")} onRetry={() => void rules.refetch()} />;
  if (!rules.data) return <Loading what="checks" rows={4} />;
  const kindsText = (r: QualityRule) => r.kinds.map((k) => KIND_PLURAL[k] ?? k).join(", ");
  return (
    <>
      <p className="mb-3 max-w-prose text-sm text-muted-foreground">
        Checks that are also ownership, AI agent or review gaps are always on and listed in their own Needs attention queues.
        {me.role === "admin" ? <> Turn the others on or off in <Link to="/admin/organization#data-quality-rules" className="underline">Organization settings</Link>.</> : " Admins turn the others on or off in Organization settings."}
      </p>
      <TableRegion label="Data-quality checks table">
        <Table>
          <TableCaption className="sr-only">Data-quality checks, most severe first, with their status here and the live records each passes, fails and excepts</TableCaption>
          <TableHeader><TableRow>
            <TableHead scope="col">Check</TableHead><TableHead scope="col">Applies to</TableHead><TableHead scope="col">Severity</TableHead>
            <TableHead scope="col">Status</TableHead><TableHead scope="col">Failing</TableHead><TableHead scope="col">Not applicable</TableHead><TableHead scope="col">Passing</TableHead>
          </TableRow></TableHeader>
          <TableBody>
            {rules.data.map((r) => (
              <TableRow key={r.id} data-rule={r.id}>
                <TableHead scope="row" className="min-w-56 max-w-md whitespace-normal font-semibold text-foreground">
                  {r.label}
                  <span className="block font-normal text-muted-foreground">{r.description}</span>
                </TableHead>
                <TableCell className="whitespace-normal">{kindsText(r)}</TableCell>
                <TableCell>{SEVERITY_LABEL[r.severity]}</TableCell>
                <TableCell className="whitespace-normal">
                  <RuleStatusChip status={r.status} />
                  {r.covered_by && <span className="block text-xs text-muted-foreground">Always on: a {r.covered_by === "reviews" ? "review" : r.covered_by === "agent_gaps" ? "AI agent" : "ownership"} gap</span>}
                  {r.status_reason && <span className="block max-w-56 text-xs text-muted-foreground">{r.status_reason}</span>}
                </TableCell>
                <TableCell className="tabular">
                  {r.counts ? (r.counts.failing > 0 ? <Link to={`/data-quality?rule=${r.id}`}>{r.counts.failing}<span className="sr-only"> records failing {r.label}</span></Link> : "0") : "—"}
                </TableCell>
                <TableCell className="tabular">{r.counts ? r.counts.excepted : "—"}</TableCell>
                <TableCell className="tabular">{r.counts ? `${r.counts.passing}${r.counts.score !== null ? ` (${r.counts.score}%)` : ""}` : "—"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableRegion>
    </>
  );
}

// ---- exceptions --------------------------------------------------------------------------------------------------------

function ExceptionsTab({ onNotice }: { onNotice: (t: string) => void }) {
  const me = useMe();
  const write = canWrite(me);
  const invalidate = useInvalidate();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const owner = params.get("owner") ?? "";
  const record = params.get("record") ?? "";
  const query = new URLSearchParams({ page_size: "200" });
  if (status === "current") query.set("active", "true");
  if (status === "expired") query.set("active", "false");
  if (owner === "left") query.set("owner_active", "false");
  if (record) query.set("record", record);
  const list = useApiQuery<Page<QualityException>>(`data-quality/exceptions?${query.toString()}`);
  const [editing, setEditing] = useState<QualityException | null>(null);
  const [removing, setRemoving] = useState<QualityException | null>(null);
  const set = (k: string, v: string) => setParams((cur) => {
    const next = new URLSearchParams(cur);
    if (v) next.set(k, v); else next.delete(k);
    return next;
  });
  const filtered = !!(status || owner || record);
  const clear = () => setParams((cur) => {
    const next = new URLSearchParams(cur);
    for (const k of ["status", "owner", "record"]) next.delete(k);
    return next;
  });
  const rows = list.data?.results ?? [];
  const recordName = record ? rows[0]?.record.name : "";
  return (
    <>
      <div className="mb-4 flex flex-wrap items-end gap-3" role="group" aria-label="Filters">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="ex-status">Status</Label>
          <Select id="ex-status" className="w-44" value={status} onChange={(e) => set("status", e.target.value)}>
            <option value="">Current or expired</option>
            <option value="current">Current</option>
            <option value="expired">Expired</option>
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="ex-owner">Accountable person</Label>
          <Select id="ex-owner" className="w-52" value={owner} onChange={(e) => set("owner", e.target.value)}>
            <option value="">Anyone</option>
            <option value="left">Owner left</option>
          </Select>
        </div>
        {record && (
          <span className="inline-flex min-h-8 items-center gap-1 rounded-full border border-border-strong bg-card pl-3 pr-1 text-[13px]">
            <span className="font-semibold">Record:</span> {recordName || "one record"}
            <Button size="sm" variant="ghost" onClick={() => set("record", "")}>Show every record</Button>
          </span>
        )}
        {filtered && <Button variant="outline" size="sm" onClick={clear}>Clear filters</Button>}
      </div>
      {list.isError && <ErrorBanner message={`${problemText(list.error, "Couldn't load the exceptions.")} Nothing was changed.`} onRetry={() => void list.refetch()} />}
      {list.isPending && <Loading what="exceptions" rows={3} />}
      {list.data && rows.length === 0 && (filtered
        ? <EmptyState title="No exceptions match these filters" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>
            {owner === "left" ? "Every exception's accountable person is still active." : "Try another status, or clear the filters."}
          </EmptyState>
        : <EmptyState title="No exceptions yet">
            {write ? "When a check doesn't apply to a record (a free tool with no contract, a component documented elsewhere), add an exception with a reason and an accountable person. It lasts up to a year." : "Exceptions mark a check not applicable to one record, with a reason and an accountable person. Editors and Admins add them."}
          </EmptyState>)}
      {rows.length > 0 && (
        <TableRegion label="Exceptions table">
          <Table>
            <TableCaption className="sr-only">Data-quality exceptions, soonest expiry first</TableCaption>
            <TableHeader><TableRow>
              <TableHead scope="col">Record</TableHead><TableHead scope="col">Check</TableHead><TableHead scope="col">Reason</TableHead>
              <TableHead scope="col">Accountable person</TableHead><TableHead scope="col">Expires</TableHead>
              <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
            </TableRow></TableHeader>
            <TableBody>
              {rows.map((x) => (
                <TableRow key={x.id} data-exception={x.id}>
                  <TableHead scope="row" className="min-w-40 whitespace-normal font-semibold text-foreground">
                    <RecordLink record={x.record} />
                    <span className="block text-xs font-normal text-muted-foreground">{KIND_LABEL[x.record.kind] ?? cap(x.record.kind)}</span>
                  </TableHead>
                  <TableCell className="whitespace-normal">{x.rule_label}</TableCell>
                  <TableCell className="max-w-sm whitespace-pre-line">{x.reason}</TableCell>
                  <TableCell className="whitespace-normal">
                    {x.owner.name}
                    {!x.owner.active && <span className="mt-1 block"><Gap label="Owner left" /></span>}
                  </TableCell>
                  <TableCell className="tabular whitespace-normal">
                    <time dateTime={x.expires_on}>{x.expires_on}</time>
                    {!x.active && <span className={`${chip({ state: "pending" })} mt-1 flex w-fit`}><span aria-hidden="true">◐</span>Expired: the check counts again</span>}
                  </TableCell>
                  <TableCell className="space-x-2 text-right whitespace-nowrap">
                    {write && <>
                      <Button size="sm" variant="outline" onClick={() => setEditing(x)}>Change<span className="sr-only"> the exception for {x.rule_label} on {x.record.name}</span></Button>
                      <Button size="sm" variant="outline" onClick={() => setRemoving(x)}>Remove<span className="sr-only"> the exception for {x.rule_label} on {x.record.name}</span></Button>
                    </>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      {list.data?.next && <p className="mt-2 text-sm text-muted-foreground">Showing the first 200 exceptions, soonest expiry first. Filter to see others.</p>}
      <ExceptionDialog open={editing !== null} exception={editing} onClose={() => setEditing(null)} onDone={(t) => { setEditing(null); onNotice(t); }} />
      <RemoveException item={removing} onClose={() => setRemoving(null)}
        onDone={async (t) => { setRemoving(null); onNotice(t); await invalidate("data-quality", "needs-attention", "applications", "components", "ai-agents"); }} />
    </>
  );
}

function RemoveException({ item, onClose, onDone }: { item: QualityException | null; onClose: () => void; onDone: (t: string) => void }) {
  const open = item !== null;
  const returnFocus = useReturnFocus(open);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // Read with its ETag as the confirmation opens; the removal sends it as If-Match.
  const opened = useOpenedRecord(item ? `data-quality/exceptions/${item.id}` : null);
  async function remove() {
    if (!item) return;
    setBusy(true);
    setError("");
    try {
      await api(`data-quality/exceptions/${item.id}`, { method: "DELETE", headers: ifMatch(await opened.etag()) });
      onDone(`Exception removed: “${item.rule_label}” counts again for ${item.record.name}.`);
    } catch (e) {
      setError(isStaleWrite(e) ? staleDeleteText("exception", "remove") : `${problemText(e)} Nothing was removed.`);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) { setError(""); onClose(); } }}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Remove the exception for “{item?.rule_label}”?</DialogTitle>
          <DialogDescription>The check counts against {item?.record.name} again at once. The exception stays in the record's history.</DialogDescription>
        </DialogHeader>
        {error && <ErrorBanner message={error} />}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={busy} onClick={() => void remove()}>Remove exception</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
