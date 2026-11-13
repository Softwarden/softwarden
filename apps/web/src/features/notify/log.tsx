/** Administration › Notification log: every delivery (reminders, escalations, digests, tests) newest
 *  first, filtered in the URL by status, channel, kind, rule, baseline and the day it was queued; a delivery's page
 *  with its error and rendered message; and resend with a confirmation. A baseline row (recorded when a rule started,
 *  never sent) has nothing to resend, so it offers none. Admin only. Loaded on first use. */
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, Problem, when, type Page } from "@/api";
import { AdminOnly, Breadcrumb, EmptyState, ErrorBanner, isNotFound, Loading, NotFound, PageHead, Select, TableRegion, useReturnFocus } from "@/components/app";
import { DetailList, DetailRow, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { problemText, useApiQuery, useInvalidate } from "@/lib/query";
import {
  CHANNEL_LABEL, DELIVERY_KIND_LABEL, DELIVERY_STATUSES, DeliveryStatusChip, deliveryStatusLabel, stageText,
  type Channel, type Delivery, type DeliveryDetail, type DeliveryKind, type NotificationRule,
} from "@/features/notify/notify-ui";
import { useResetOn } from "@/features/notify/notify";

const WHAT = "every notice Softwarden sent or tried to send, with recipients' addresses and errors";
const LOG = "notification-log";
const FILTERS = ["status", "channel", "kind", "rule", "baseline", "from", "to"] as const;
type FilterKey = (typeof FILTERS)[number];

/** Whether a delivery can be resent: not a baseline (never written), not while it is still queued or being sent. */
export const canResend = (d: Delivery) => !d.baseline && d.status !== "queued" && d.status !== "sending";
const target = (d: Delivery) => (d.channel === "email" ? d.recipient : d.destination?.name ?? "");
const describe = (d: Delivery) => `${CHANNEL_LABEL[d.channel]} to ${target(d)}${d.subject ? `, ${d.subject}` : ""}`;

export function NotificationLogPage() {
  return <AdminOnly title="Notification log" what={WHAT}><LogList /></AdminOnly>;
}

function LogList() {
  const [params, setParams] = useSearchParams();
  const value = (k: FilterKey) => params.get(k) ?? "";
  const cursor = params.get("cursor") ?? "";
  const request = new URLSearchParams();
  for (const k of FILTERS) if (value(k)) request.set(k, value(k));
  if (cursor) request.set("cursor", cursor);
  const query = request.toString();
  const q = useApiQuery<Page<Delivery>>(`${LOG}${query ? `?${query}` : ""}`);
  const rules = useApiQuery<NotificationRule[]>("notification-rules");
  const [resending, setResending] = useState<Delivery | null>(null);
  const [notice, setNotice] = useState<{ text: string; id?: string } | null>(null);
  const filtered = FILTERS.some((k) => value(k));
  // Any filter change starts again at the first page: a cursor only works for the filters it was made with.
  // A discrete change (a select, Clear) pushes a history entry so Back undoes it; a typed one (a date box) replaces.
  const setFilter = (k: FilterKey, v: string, typed = false) => setParams((p) => {
    const n = new URLSearchParams(p);
    if (v) n.set(k, v); else n.delete(k);
    n.delete("cursor");
    return n;
  }, { replace: typed });
  const setCursor = (c: string | null) => setParams((p) => { const n = new URLSearchParams(p); if (c) n.set("cursor", c); else n.delete("cursor"); return n; });
  const clear = () => setParams({});
  const rows = q.data?.results;
  // A rule from the URL stays shown while the names load, if they can't, or once the rule is gone (its rows keep its
  // name), rather than the select reading "Any rule" while the list is filtered.
  const ruleOptions = (rules.data ?? []).map((r) => ({ value: r.id, label: r.name }));
  if (value("rule") && !ruleOptions.some((o) => o.value === value("rule"))) {
    ruleOptions.push({ value: value("rule"), label: rules.data ? "Unknown rule" : rules.isError ? "Rule (names didn't load)" : "Loading rule…" });
  }
  const select = (k: FilterKey, label: string, options: { value: string; label: string }[], all = "All") => (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={`log-${k}`}>{label}</Label>
      <Select id={`log-${k}`} className="w-48" value={value(k)} onChange={(e) => setFilter(k, e.target.value)}>
        <option value="">{all}</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </Select>
    </div>
  );
  return (
    <>
      <PageHead title="Notification log" desc="Every notice Softwarden sent or tried to send, newest first: reminders, escalations, digests and test sends. Failures are retried after about 1, 4 and 16 minutes." />
      <p className="mb-4 max-w-prose text-sm text-muted-foreground">
        Rows marked <span className="font-semibold">Baseline, not sent</span> were recorded when a rule started or widened after the notice was already due, so nobody got a burst of old notices.
        The per-person digest view is the <Link className="underline" to="/admin/digests">digest delivery log</Link>.
      </p>
      <form className="mb-4 flex flex-wrap items-end gap-3" aria-label="Filter the notification log" onSubmit={(e) => e.preventDefault()}>
        {select("status", "Status", DELIVERY_STATUSES.map((s) => ({ value: s, label: deliveryStatusLabel(s) })))}
        {select("channel", "Channel", (Object.keys(CHANNEL_LABEL) as Channel[]).map((c) => ({ value: c, label: CHANNEL_LABEL[c] })))}
        {select("kind", "Kind", (Object.keys(DELIVERY_KIND_LABEL) as DeliveryKind[]).map((k) => ({ value: k, label: DELIVERY_KIND_LABEL[k] })))}
        {select("rule", "Rule", ruleOptions, "Any rule")}
        {select("baseline", "Baselines", [{ value: "false", label: "Without baselines" }, { value: "true", label: "Only baselines" }], "Included")}
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="log-from">Queued from</Label>
          <Input id="log-from" type="date" className="w-44" value={value("from")} onChange={(e) => setFilter("from", e.target.value, true)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="log-to">Queued to</Label>
          <Input id="log-to" type="date" className="w-44" value={value("to")} onChange={(e) => setFilter("to", e.target.value, true)} />
        </div>
        {filtered && <Button type="button" variant="outline" onClick={clear}>Clear filters</Button>}
      </form>
      {rules.isError && (
        <p className="-mt-2 mb-4 flex flex-wrap items-center gap-2 text-sm text-error-foreground">
          <span><span aria-hidden="true">! </span>Couldn't load the rule names for the Rule filter.</span>
          <Button size="sm" variant="outline" onClick={() => void rules.refetch()}>Retry<span className="sr-only"> loading the rule names</span></Button>
        </p>
      )}
      {notice && <SuccessNote text={notice.text} />}
      {notice?.id && <p className="-mt-2 mb-4 text-sm"><Link to={`/admin/notification-log/${notice.id}`}>View the new delivery</Link></p>}
      {q.isError && (
        <div className="mb-4">
          <ErrorBanner message={problemText(q.error, "Couldn't load the notification log.")} onRetry={() => void q.refetch()} />
          {filtered && <Button variant="outline" onClick={clear}>Clear filters</Button>}
        </div>
      )}
      {!rows && !q.isError && <Loading what="the notification log" rows={5} />}
      {rows && rows.length === 0 && (filtered
        ? <EmptyState title="No deliveries match these filters" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>Try a wider date range or another status.</EmptyState>
        : <EmptyState title="Nothing sent yet" action={<Button asChild variant="outline"><Link to="/admin/notification-channels">Set up channels and send a test</Link></Button>}>Notices appear here when a rule, the daily digest or a test sends one.</EmptyState>)}
      {rows && rows.length > 0 && (
        <>
          <TableRegion label="Notification log table">
            <Table>
              <TableCaption className="sr-only">Deliveries, newest first{filtered ? ", filtered" : ""}</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Queued</TableHead><TableHead scope="col">Message</TableHead><TableHead scope="col">Status</TableHead>
                <TableHead scope="col">Channel</TableHead><TableHead scope="col">To</TableHead><TableHead scope="col">Rule</TableHead>
                <TableHead scope="col">Due date</TableHead><TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((d) => (
                  <TableRow key={d.id} className="align-top">
                    <TableCell className="tabular">{when(d.created_at)}</TableCell>
                    <TableHead scope="row" className="max-w-xs whitespace-normal font-normal text-foreground">
                      <Link to={`/admin/notification-log/${d.id}`} className="font-semibold underline">{d.subject || DELIVERY_KIND_LABEL[d.kind]}</Link>
                      <span className="block text-sm text-muted-foreground">{DELIVERY_KIND_LABEL[d.kind]}{d.resend_of ? ", resent" : ""}</span>
                    </TableHead>
                    <TableCell className="whitespace-normal">
                      <DeliveryStatusChip d={d} />
                      {d.status === "failed" && d.last_error && <span className="mt-1 block max-w-60 text-sm text-error-foreground [overflow-wrap:anywhere]">{d.last_error}</span>}
                      {d.next_attempt_at && <span className="block text-sm text-muted-foreground">Next try {when(d.next_attempt_at)}</span>}
                    </TableCell>
                    <TableCell>{CHANNEL_LABEL[d.channel]}</TableCell>
                    <TableCell className="max-w-48 whitespace-normal [overflow-wrap:anywhere]">{target(d) || "—"}</TableCell>
                    <TableCell className="max-w-48 whitespace-normal">
                      {d.rule_name || "—"}
                      {stageText(d) && <span className="block text-sm text-muted-foreground">{stageText(d)}</span>}
                    </TableCell>
                    <TableCell className="tabular">{d.due_date ?? "—"}</TableCell>
                    <TableCell className="text-right">
                      {canResend(d) && <Button size="sm" variant="outline" onClick={() => setResending(d)}>Resend<span className="sr-only"> {describe(d)}</span></Button>}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
          <nav aria-label="Pages" className="mt-3 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span className="mr-auto">{rows.length} {rows.length === 1 ? "delivery" : "deliveries"} on this page, newest first.</span>
            <Button size="sm" variant="outline" disabled={!q.data?.previous} onClick={() => setCursor(q.data?.previous ?? null)}>Previous</Button>
            <Button size="sm" variant="outline" disabled={!q.data?.next} onClick={() => setCursor(q.data?.next ?? null)}>Next</Button>
          </nav>
        </>
      )}
      <ResendDialog delivery={resending} onClose={() => setResending(null)}
        onDone={(copy) => { setResending(null); setNotice({ text: `Resent as a new delivery: ${describe(copy)}. The worker sends it within a minute.`, id: copy.id }); void q.refetch(); }} />
    </>
  );
}

/** Resend, confirmed. A 409 (still queued, destination deleted, or a baseline after all) shows what the API said. */
function ResendDialog({ delivery, onClose, onDone }: { delivery: Delivery | null; onClose: () => void; onDone: (copy: Delivery) => void }) {
  const open = delivery !== null;
  const returnFocus = useReturnFocus(open);
  const [error, setError] = useResetOn(delivery?.id ?? null, "");
  // Kept while the dialog animates closed, so its title and button don't go blank.
  const [last, setLast] = useState(delivery);
  if (delivery && delivery !== last) setLast(delivery);
  const shown = delivery ?? last;
  const [busy, setBusy] = useState(false);
  const invalidate = useInvalidate();
  async function resend() {
    if (!delivery) return;
    setBusy(true);
    try {
      const copy = await api<Delivery>(`${LOG}/${delivery.id}/resend`, { method: "POST" });
      setError("");
      void invalidate(LOG);
      onDone(copy);
    } catch (e) {
      setError(e instanceof Problem && e.status === 409 ? `Not resent: ${e.detail}` : problemText(e, "Couldn't resend it."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Resend this {shown ? CHANNEL_LABEL[shown.channel].toLowerCase() : ""} message?</DialogTitle>
          <DialogDescription>
            {shown && <>The same message goes again to {target(shown)}, as a new row in the log. Use it when the first one failed or didn't arrive.</>}
          </DialogDescription>
        </DialogHeader>
        {shown?.subject && <p className="text-sm"><span className="text-muted-foreground">Subject: </span>{shown.subject}</p>}
        {error && <p className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm" role="alert"><span aria-hidden="true">! </span>{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" disabled={busy} onClick={() => void resend()}>{busy ? "Resending…" : `Resend to ${shown ? target(shown) : ""}`}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---- one delivery -------------------------------------------------------------------------------------------------

export function DeliveryPage() {
  return <AdminOnly title="Delivery" what={WHAT}><DeliveryDetailView /></AdminOnly>;
}

function DeliveryDetailView() {
  const { id = "" } = useParams();
  const q = useApiQuery<DeliveryDetail>(`${LOG}/${id}`);
  const [resending, setResending] = useState(false);
  const navigate = useNavigate();
  const d = q.data;
  const trail = [{ to: "/admin/notification-log", label: "Notification log" }];
  if (q.isError && isNotFound(q.error)) return <NotFound what="delivery" back={{ to: "/admin/notification-log", label: "Notification log" }} trail={trail} />;
  if (q.isError) {
    return <>
      <Breadcrumb trail={trail} current="Delivery" />
      <PageHead title="Delivery" />
      <ErrorBanner message={problemText(q.error, "Couldn't load the delivery.")} onRetry={() => void q.refetch()} />
    </>;
  }
  if (!d) return <Loading what="the delivery" />;
  const content = d.content ?? {};
  const body = d.channel === "email" ? (typeof content.text === "string" ? content.text : "") : JSON.stringify(d.channel === "teams" ? content.card ?? content : content, null, 2);
  return (
    <>
      <Breadcrumb trail={trail} current={d.subject || DELIVERY_KIND_LABEL[d.kind]} />
      <PageHead title={d.subject || DELIVERY_KIND_LABEL[d.kind]} docTitle="Delivery · Notification log"
        desc={<span className="inline-flex flex-wrap items-center gap-2"><DeliveryStatusChip d={d} />{CHANNEL_LABEL[d.channel]} · {DELIVERY_KIND_LABEL[d.kind]}</span>}>
        {canResend(d) && <Button onClick={() => setResending(true)}>Resend</Button>}
      </PageHead>
      {d.baseline && (
        <p className="mb-4 max-w-prose rounded-md border border-l-4 border-info-border bg-info-bg px-4 py-3" role="note">
          <span aria-hidden="true">ⓘ </span>A baseline: recorded when the rule started or widened after this notice was already due, so it was never sent and can't be resent.
        </p>
      )}
      {d.last_error && !d.baseline && (
        <section aria-labelledby="error-heading" className="mb-4 max-w-3xl rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3">
          <h2 id="error-heading" className="font-semibold"><span aria-hidden="true">! </span>{d.status === "sent" ? "An earlier attempt failed" : "Why it wasn't sent"}</h2>
          <p className="mt-1 whitespace-pre-wrap [overflow-wrap:anywhere]" data-testid="delivery-error">{d.last_error}</p>
        </section>
      )}
      <section aria-labelledby="delivery-heading" className="mb-4 max-w-3xl rounded-lg border bg-card">
        <h2 id="delivery-heading" className="border-b px-4 py-2.5 font-display text-section font-semibold">Delivery</h2>
        <DetailList>
          <DetailRow label="To">{target(d) || "—"}</DetailRow>
          <DetailRow label="Rule">{d.rule ? <Link to={`/admin/notification-rules/${d.rule}`}>{d.rule_name}</Link> : d.rule_name || "—"}{stageText(d) && <span className="block text-sm text-muted-foreground">{stageText(d)}</span>}</DetailRow>
          <DetailRow label="Reminder">{d.reminder ? <>{d.reminder.title}<span className="block text-sm text-muted-foreground">{d.reminder.label}</span></> : "—"}</DetailRow>
          <DetailRow label="Due date"><span className="tabular">{d.due_date ?? "—"}</span></DetailRow>
          <DetailRow label="Queued"><span className="tabular">{when(d.created_at)}</span></DetailRow>
          <DetailRow label="Sent"><span className="tabular">{when(d.sent_at)}</span>{d.sent_at && <span className="block text-sm text-muted-foreground">Accepted by the channel; not proof that it was read.</span>}</DetailRow>
          <DetailRow label="Attempts"><span className="tabular">{d.attempts} of {d.max_attempts}</span>{d.next_attempt_at && <span className="block text-sm text-muted-foreground">Next try {when(d.next_attempt_at)}</span>}</DetailRow>
          {d.requested_by && <DetailRow label="Requested by">{d.requested_by.name}</DetailRow>}
          {d.client_request_id && <DetailRow label="Request ID"><span className="font-mono text-sm">{d.client_request_id}</span></DetailRow>}
          {d.resend_of && <DetailRow label="Resend of"><Link to={`/admin/notification-log/${d.resend_of}`}>The original delivery</Link></DetailRow>}
          {d.resends.length > 0 && <DetailRow label="Resent as">
            <ul className="m-0 list-none p-0">{d.resends.map((r, i) => <li key={r}><Link to={`/admin/notification-log/${r}`}>Resend {i + 1}</Link></li>)}</ul>
          </DetailRow>}
        </DetailList>
      </section>
      <section aria-labelledby="content-heading" className="max-w-3xl rounded-lg border bg-card">
        <h2 id="content-heading" className="border-b px-4 py-2.5 font-display text-section font-semibold">
          {d.channel === "email" ? "Message (plain text)" : d.channel === "teams" ? "Adaptive card" : "Webhook body"}
        </h2>
        {body
          ? <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap px-4 py-3 font-mono text-sm [overflow-wrap:anywhere]" tabIndex={0} aria-labelledby="content-heading">{body}</pre>
          : <p className="px-4 py-3 text-muted-foreground">No content was recorded.</p>}
      </section>
      <ResendDialog delivery={resending ? d : null} onClose={() => setResending(false)}
        onDone={(copy) => { setResending(false); navigate(`/admin/notification-log/${copy.id}`); }} />
    </>
  );
}
