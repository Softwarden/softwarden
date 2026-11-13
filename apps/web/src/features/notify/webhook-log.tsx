/** Administration › Webhooks › Delivery log: every post to a webhook endpoint (events, pings and the reminder
 *  notices notification rules post), newest first, filtered in the URL by endpoint, status, kind, event and the day it
 *  was queued; one delivery's page with every attempt (the answer's start, secrets removed by the API) and the JSON
 *  payload as text in a <pre>, never markup; and resend, which queues the same payload as a new delivery. Admin only,
 *  capability `webhooks`. Typed from the generated OpenAPI client (src/lib/openapi.ts). Loaded on first use. */
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Problem, when } from "@/api";
import { AdminOnly, Breadcrumb, EmptyState, ErrorBanner, isNotFound, Loading, NotFound, PageHead, Select, TableRegion, useReturnFocus } from "@/components/app";
import { DetailList, DetailRow, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { typedApi, useTypedQuery, type QueryOf } from "@/lib/openapi";
import { problemText, useInvalidate } from "@/lib/query";
import { useResetOn } from "@/features/notify/notify";
import {
  answerText, DeliveryStatusChip, KIND_CHOICES, kindLabel, LOG_PAGE, OutcomeChip, STATUS_CHOICES, useHasWebhooks,
  WEBHOOKS_PAGE, WebhooksUnavailable, WHAT_LOG, type WebhookDelivery,
} from "@/features/notify/webhooks-ui";

const FILTERS = ["webhook", "status", "kind", "event", "from", "to"] as const;
type FilterKey = (typeof FILTERS)[number];
type LogQuery = QueryOf<"webhook-deliveries", "get">;

/** Not while it is still queued or being sent (the API answers 409), and not once its endpoint is gone. */
const canResend = (d: WebhookDelivery) => d.status !== "queued" && d.status !== "sending" && d.webhook.id !== null;
const describe = (d: WebhookDelivery) => `${d.event_type} to ${d.webhook.name}`;

export function WebhookLogPage() {
  return <AdminOnly title="Webhook delivery log" what={WHAT_LOG}><Gate><LogList /></Gate></AdminOnly>;
}

export function WebhookDeliveryPage() {
  return <AdminOnly title="Webhook delivery" what={WHAT_LOG}><Gate><DeliveryDetailView /></Gate></AdminOnly>;
}

function Gate({ children }: { children: React.ReactNode }) {
  if (!useHasWebhooks()) return <><PageHead title="Webhook delivery log" /><WebhooksUnavailable /></>;
  return <>{children}</>;
}

/** Event families and the types people filter by most; any other type can be typed. */
const EVENT_CHOICES = ["record.*", "relationship.*", "renewal.*", "discovery.*", "reminder.*", "ping", "reminder.due", "reminder.overdue"];

function LogList() {
  const [params, setParams] = useSearchParams();
  const value = (k: FilterKey) => params.get(k) ?? "";
  const cursor = params.get("cursor") ?? "";
  const query: Partial<LogQuery> = { cursor: cursor || undefined };
  for (const k of FILTERS) if (value(k)) (query as Record<string, string>)[k] = value(k);
  const q = useTypedQuery("webhook-deliveries", { query });
  const endpoints = useTypedQuery("webhooks");
  const [resending, setResending] = useState<WebhookDelivery | null>(null);
  const [notice, setNotice] = useState<{ text: string; id: string } | null>(null);
  const filtered = FILTERS.some((k) => value(k));
  // A cursor only works with the filters it came from, so any filter change starts again at the first page.
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
  const endpointOptions = (endpoints.data ?? []).map((w) => ({ value: w.id, label: w.name }));
  if (value("webhook") && !endpointOptions.some((o) => o.value === value("webhook"))) {
    endpointOptions.push({ value: value("webhook"), label: endpoints.data ? "Deleted endpoint" : endpoints.isError ? "Endpoint (names didn't load)" : "Loading endpoint…" });
  }
  const eventOptions = EVENT_CHOICES.map((e) => ({ value: e, label: e.endsWith(".*") ? `${e} (every ${e.slice(0, -2)} event)` : e }));
  if (value("event") && !EVENT_CHOICES.includes(value("event"))) eventOptions.push({ value: value("event"), label: value("event") });
  const select = (k: FilterKey, label: string, options: { value: string; label: string }[], all = "All") => (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={`whlog-${k}`}>{label}</Label>
      <Select id={`whlog-${k}`} className="w-52" value={value(k)} onChange={(e) => setFilter(k, e.target.value)}>
        <option value="">{all}</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </Select>
    </div>
  );
  return (
    <>
      <Breadcrumb trail={[{ to: WEBHOOKS_PAGE, label: "Webhooks" }]} current="Delivery log" />
      <PageHead title="Webhook delivery log" desc="Every post to a webhook endpoint, newest first: events, pings and notification rules' reminder notices, with each attempt. By default, sent rows are kept 30 days and failed ones 90." />
      <form className="mb-4 flex flex-wrap items-end gap-3" aria-label="Filter the webhook delivery log" onSubmit={(e) => e.preventDefault()}>
        {select("webhook", "Endpoint", endpointOptions, "Every endpoint")}
        {select("status", "Status", STATUS_CHOICES)}
        {select("kind", "Kind", KIND_CHOICES)}
        {select("event", "Event", eventOptions, "Any event")}
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="whlog-from">Queued from</Label>
          <Input id="whlog-from" type="date" className="w-44" value={value("from")} onChange={(e) => setFilter("from", e.target.value, true)} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="whlog-to">Queued to</Label>
          <Input id="whlog-to" type="date" className="w-44" value={value("to")} onChange={(e) => setFilter("to", e.target.value, true)} />
        </div>
        {filtered && <Button type="button" variant="outline" onClick={clear}>Clear filters</Button>}
      </form>
      {endpoints.isError && (
        <p className="-mt-2 mb-4 flex flex-wrap items-center gap-2 text-sm text-error-foreground">
          <span><span aria-hidden="true">! </span>Couldn't load the endpoint names for the Endpoint filter.</span>
          <Button size="sm" variant="outline" onClick={() => void endpoints.refetch()}>Retry<span className="sr-only"> loading the endpoint names</span></Button>
        </p>
      )}
      <SuccessNote text={notice?.text ?? ""} />
      {notice && <p className="-mt-2 mb-4 text-sm"><Link to={`${LOG_PAGE}/${notice.id}`}>View the new delivery</Link></p>}
      {q.isError && (
        <div className="mb-4">
          <ErrorBanner message={problemText(q.error, "Couldn't load the webhook delivery log.")} onRetry={() => void q.refetch()} />
          {filtered && <Button variant="outline" onClick={clear}>Clear filters</Button>}
        </div>
      )}
      {!rows && !q.isError && <Loading what="the webhook delivery log" rows={5} />}
      {rows && rows.length === 0 && (filtered
        ? <EmptyState title="No deliveries match these filters" action={<Button variant="outline" onClick={clear}>Clear filters</Button>}>Try a wider date range, another status or every endpoint.</EmptyState>
        : <EmptyState title="Nothing posted yet" action={<Button asChild variant="outline"><Link to={WEBHOOKS_PAGE}>Add a webhook and ping it</Link></Button>}>Deliveries appear here when an endpoint is pinged, or when a change it subscribes to happens.</EmptyState>)}
      {rows && rows.length > 0 && (
        <>
          <TableRegion label="Webhook delivery log table">
            <Table>
              <TableCaption className="sr-only">Webhook deliveries, newest first{filtered ? ", filtered" : ""}</TableCaption>
              <TableHeader><TableRow>
                <TableHead scope="col">Queued</TableHead><TableHead scope="col">Event</TableHead><TableHead scope="col">Endpoint</TableHead>
                <TableHead scope="col">Status</TableHead><TableHead scope="col">Attempts</TableHead><TableHead scope="col">Last answer</TableHead>
                <TableHead scope="col"><span className="sr-only">Actions</span></TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {rows.map((d) => (
                  <TableRow key={d.id} className="align-top">
                    <TableCell className="tabular">{when(d.created_at)}</TableCell>
                    <TableHead scope="row" className="max-w-xs whitespace-normal font-normal text-foreground">
                      <Link to={`${LOG_PAGE}/${d.id}`} className="font-mono font-semibold underline">{d.event_type}</Link>
                      <span className="block text-sm text-muted-foreground">{kindLabel(d.kind)}{d.resend_of ? ", resent" : ""}</span>
                    </TableHead>
                    <TableCell className="max-w-48 whitespace-normal [overflow-wrap:anywhere]">
                      {d.webhook.name}{d.webhook.id === null && <span className="block text-sm text-muted-foreground">Endpoint deleted</span>}
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      <DeliveryStatusChip status={d.status} />
                      {d.status === "failed" && d.last_error && <span className="mt-1 block max-w-60 text-sm text-error-foreground [overflow-wrap:anywhere]">{d.last_error}</span>}
                      {d.next_attempt_at && <span className="block text-sm text-muted-foreground">Next try {when(d.next_attempt_at)}</span>}
                    </TableCell>
                    <TableCell className="tabular">{d.attempts} of {d.max_attempts}</TableCell>
                    <TableCell className="tabular">{d.attempts ? answerText(d.last_status_code) : "—"}</TableCell>
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
        onDone={(copy) => { setResending(null); setNotice({ text: `Resent as a new delivery: ${describe(copy)}. The worker sends it within a minute.`, id: copy.id }); }} />
    </>
  );
}

/** Resend, confirmed. A 409 (still queued or sending, endpoint deleted) says what the API said. */
function ResendDialog({ delivery, onClose, onDone }: { delivery: WebhookDelivery | null; onClose: () => void; onDone: (copy: WebhookDelivery) => void }) {
  const open = delivery !== null;
  const returnFocus = useReturnFocus(open);
  const [error, setError] = useResetOn(delivery?.id ?? null, "");
  const [last, setLast] = useState(delivery);
  if (delivery && delivery !== last) setLast(delivery);
  const shown = delivery ?? last;
  const [busy, setBusy] = useState(false);
  const invalidate = useInvalidate();
  async function resend() {
    if (!delivery) return;
    setBusy(true);
    try {
      const copy = await typedApi("post", "webhook-deliveries/{id}/resend", { path: { id: delivery.id } });
      setError("");
      void invalidate("webhook-deliveries", "webhooks");
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
          <DialogTitle>Resend {shown?.event_type} to {shown?.webhook.name}?</DialogTitle>
          <DialogDescription>
            The same payload, with the same event id, is queued again as a new delivery with its own delivery id, so the
            receiver can tell it from the first. The worker sends it within a minute.
          </DialogDescription>
        </DialogHeader>
        {error && <p className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm" role="alert"><span aria-hidden="true">! </span>{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="button" disabled={busy} onClick={() => void resend()}>{busy ? "Resending…" : `Resend to ${shown?.webhook.name ?? ""}`}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---- one delivery -------------------------------------------------------------------------------------------------

function DeliveryDetailView() {
  const { id = "" } = useParams();
  const q = useTypedQuery("webhook-deliveries/{id}", { path: { id } });
  const [resending, setResending] = useState(false);
  const navigate = useNavigate();
  const d = q.data;
  const trail = [{ to: WEBHOOKS_PAGE, label: "Webhooks" }, { to: LOG_PAGE, label: "Delivery log" }];
  if (q.isError && isNotFound(q.error)) return <NotFound what="webhook delivery" back={{ to: LOG_PAGE, label: "Delivery log" }} trail={trail} />;
  if (q.isError) {
    return <>
      <Breadcrumb trail={trail} current="Delivery" />
      <PageHead title="Webhook delivery" />
      <ErrorBanner message={problemText(q.error, "Couldn't load the delivery.")} onRetry={() => void q.refetch()} />
    </>;
  }
  if (!d) return <Loading what="the delivery" />;
  // The payload is shown as text: JSON.stringify output in a <pre>, never parsed as markup.
  const payload = JSON.stringify(d.payload, null, 2);
  return (
    <>
      <Breadcrumb trail={trail} current={d.event_type} />
      <PageHead title={`${d.event_type} to ${d.webhook.name}`} docTitle="Webhook delivery"
        desc={<span className="inline-flex flex-wrap items-center gap-2"><DeliveryStatusChip status={d.status} />{kindLabel(d.kind)}</span>}>
        {canResend(d) && <Button onClick={() => setResending(true)}>Resend</Button>}
      </PageHead>
      {d.last_error && (
        <section aria-labelledby="error-heading" className="mb-4 max-w-3xl rounded-md border border-l-4 border-error-border bg-error-bg px-4 py-3">
          <h2 id="error-heading" className="font-semibold"><span aria-hidden="true">! </span>{d.status === "sent" ? "An earlier attempt failed" : "Why it wasn't sent"}</h2>
          <p className="mt-1 whitespace-pre-wrap [overflow-wrap:anywhere]" data-testid="delivery-error">{d.last_error}</p>
        </section>
      )}
      <section aria-labelledby="delivery-heading" className="mb-4 max-w-3xl rounded-lg border bg-card">
        <h2 id="delivery-heading" className="border-b px-4 py-2.5 font-display text-section font-semibold">Delivery</h2>
        <DetailList>
          <DetailRow label="Endpoint">
            {d.webhook.id ? <Link to={`${LOG_PAGE}?webhook=${d.webhook.id}`}>{d.webhook.name}<span className="sr-only">: its deliveries</span></Link> : <>{d.webhook.name}<span className="block text-sm text-muted-foreground">Endpoint deleted</span></>}
          </DetailRow>
          <DetailRow label="Delivery ID"><span className="font-mono text-sm [overflow-wrap:anywhere]">{d.id}</span><span className="block text-sm text-muted-foreground">The X-Softwarden-Delivery header: the same on every retry, new on a resend.</span></DetailRow>
          <DetailRow label="Event ID"><span className="font-mono text-sm [overflow-wrap:anywhere]">{d.event_id ?? "—"}</span></DetailRow>
          <DetailRow label="Queued"><span className="tabular">{when(d.created_at)}</span></DetailRow>
          <DetailRow label="Sent"><span className="tabular">{when(d.sent_at)}</span>{d.sent_at && <span className="block text-sm text-muted-foreground">The endpoint answered 2xx.</span>}</DetailRow>
          <DetailRow label="Attempts"><span className="tabular">{d.attempts} of {d.max_attempts}</span>{d.next_attempt_at && <span className="block text-sm text-muted-foreground">Next try {when(d.next_attempt_at)}</span>}</DetailRow>
          {d.requested_by && <DetailRow label="Requested by">{d.requested_by.name}</DetailRow>}
          {d.resend_of && <DetailRow label="Resend of"><Link to={`${LOG_PAGE}/${d.resend_of}`}>The original delivery</Link></DetailRow>}
          {d.resends.length > 0 && <DetailRow label="Resent as">
            <ul className="m-0 list-none p-0">{d.resends.map((r, i) => <li key={r}><Link to={`${LOG_PAGE}/${r}`}>Resend {i + 1}</Link></li>)}</ul>
          </DetailRow>}
        </DetailList>
      </section>
      <section aria-labelledby="attempts-heading" className="mb-4">
        <h2 id="attempts-heading" className="mb-2 font-display text-section font-semibold">Attempts</h2>
        {d.attempt_log.length === 0
          ? <p className="max-w-3xl rounded-lg border bg-card px-4 py-3 text-muted-foreground">No attempt yet. The worker posts queued deliveries within a minute.</p>
          : (
            <TableRegion label="Attempts table">
              <Table>
                <TableCaption className="sr-only">Attempts, oldest first</TableCaption>
                <TableHeader><TableRow>
                  <TableHead scope="col">Attempt</TableHead><TableHead scope="col">Started</TableHead><TableHead scope="col">Outcome</TableHead>
                  <TableHead scope="col">Answer</TableHead><TableHead scope="col">Took</TableHead><TableHead scope="col">Response or error</TableHead>
                </TableRow></TableHeader>
                <TableBody>
                  {d.attempt_log.map((a) => (
                    <TableRow key={a.number} className="align-top">
                      <TableHead scope="row" className="tabular font-semibold text-foreground">{a.number}</TableHead>
                      <TableCell className="tabular">{when(a.started_at)}</TableCell>
                      <TableCell className="whitespace-normal">
                        <OutcomeChip outcome={a.outcome} />
                        {a.next_attempt_at && <span className="block text-sm text-muted-foreground">Retry at {when(a.next_attempt_at)}</span>}
                      </TableCell>
                      <TableCell className="tabular">{answerText(a.status_code)}</TableCell>
                      <TableCell className="tabular">{a.duration_ms === null ? "—" : `${a.duration_ms} ms`}</TableCell>
                      <TableCell className="min-w-64 max-w-md whitespace-normal">
                        {a.error && <p className="text-sm text-error-foreground [overflow-wrap:anywhere]">{a.error}</p>}
                        {a.response_excerpt
                          ? <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded-sm bg-muted px-2 py-1 font-mono text-xs [overflow-wrap:anywhere]" tabIndex={0} aria-label={`Response to attempt ${a.number}`}>{a.response_excerpt}</pre>
                          : !a.error && <span className="text-sm text-muted-foreground">Empty answer</span>}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableRegion>
          )}
      </section>
      <section aria-labelledby="payload-heading" className="max-w-3xl rounded-lg border bg-card">
        <h2 id="payload-heading" className="border-b px-4 py-2.5 font-display text-section font-semibold">Payload (JSON)</h2>
        <p className="border-b px-4 py-2 text-sm text-muted-foreground">The body as posted. The signature header differs on each attempt and isn't shown.</p>
        <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap px-4 py-3 font-mono text-sm [overflow-wrap:anywhere]" tabIndex={0} aria-labelledby="payload-heading" data-testid="webhook-payload">{payload}</pre>
      </section>
      <ResendDialog delivery={resending ? d : null} onClose={() => setResending(false)}
        onDone={(copy) => { setResending(false); navigate(`${LOG_PAGE}/${copy.id}`); }} />
    </>
  );
}
