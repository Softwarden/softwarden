/** Webhooks: the shapes, words and chips shared by the endpoints page and the webhook
 *  delivery log. Every shape is the generated OpenAPI type (src/generated/openapi.d.ts through src/lib/openapi.ts), so
 *  a field the API doesn't return can't be read here. Imported only by the lazily loaded webhook pages. */
import { Link } from "react-router-dom";
import { when } from "@/api";
import { chip } from "@/components/sw";
import type { Schemas } from "@/lib/openapi";
import { useMe } from "@/lib/session";

export type Webhook = Schemas["Webhook"];
export type WebhookDelivery = Schemas["WebhookDelivery"];
export type WebhookDeliveryDetail = Schemas["WebhookDeliveryDetail"];
export type DeliveryAttempt = Schemas["DeliveryAttempt"];
export type EventCatalog = Schemas["EventCatalog"];
export type Subscription = Schemas["EventsEnum"];
export type WebhookDeliveryKind = Schemas["DeliveryKindEnum"];
export type WebhookDeliveryStatus = Schemas["DeliveryStatusEnum"];

export const CAPABILITY = "webhooks";
export const WEBHOOKS_PAGE = "/admin/webhooks";
export const LOG_PAGE = "/admin/webhooks/deliveries";
export const WHAT_ENDPOINTS = "where Softwarden posts signed events: each endpoint's host, its events, its last delivery and why it was turned off";
export const WHAT_LOG = "every post to a webhook endpoint, with the payload sent and each attempt's answer";

/** Whether this organization's edition has webhooks (gate on the capability, never the edition). */
export function useHasWebhooks() {
  return useMe().capabilities.includes(CAPABILITY);
}

/** Shown instead of a webhook page the edition lacks; the page calls nothing. */
export function WebhooksUnavailable() {
  return (
    <p className="max-w-prose rounded-md border border-dashed border-border-strong bg-muted px-3 py-2 text-sm" data-unavailable="">
      <span aria-hidden="true">⊘ </span><span className="font-semibold">Webhooks aren't available.</span> This organization's edition doesn't include signed webhook events.
    </p>
  );
}

/** The log's kinds, in the words of this page (a test delivery is a ping here). */
export const KIND_LABEL: Partial<Record<WebhookDeliveryKind, string>> = {
  event: "Event", test: "Ping", reminder: "Reminder notice", escalation: "Overdue escalation",
};
export const KIND_CHOICES = (Object.keys(KIND_LABEL) as WebhookDeliveryKind[]).map((k) => ({ value: k, label: KIND_LABEL[k] ?? k }));
export const kindLabel = (k: WebhookDeliveryKind) => KIND_LABEL[k] ?? k;

type Tone = "confirmed" | "pending" | "overdue" | "unreviewed" | "neutral";
const STATUS: Record<WebhookDeliveryStatus, { tone: Tone; glyph: string; label: string }> = {
  queued: { tone: "neutral", glyph: "◔", label: "Queued" },
  sending: { tone: "neutral", glyph: "▸", label: "Sending" },
  sent: { tone: "confirmed", glyph: "✓", label: "Sent" },
  failed: { tone: "overdue", glyph: "!", label: "Failed" },
  skipped: { tone: "neutral", glyph: "–", label: "Skipped" },
};
export const STATUS_CHOICES = (Object.keys(STATUS) as WebhookDeliveryStatus[]).map((s) => ({ value: s, label: STATUS[s].label }));

export function DeliveryStatusChip({ status }: { status: WebhookDeliveryStatus }) {
  const s = STATUS[status];
  return <span className={chip({ state: s.tone })}><span aria-hidden="true">{s.glyph}</span>{s.label}</span>;
}

const OUTCOME: Record<Schemas["DeliveryAttemptOutcomeEnum"], { tone: Tone; glyph: string; label: string }> = {
  sent: { tone: "confirmed", glyph: "✓", label: "Sent" },
  retrying: { tone: "pending", glyph: "◐", label: "Failed, will retry" },
  failed: { tone: "overdue", glyph: "!", label: "Failed" },
  skipped: { tone: "neutral", glyph: "–", label: "Skipped" },
};

export function OutcomeChip({ outcome }: { outcome: Schemas["DeliveryAttemptOutcomeEnum"] | Schemas["LastDeliveryOutcomeEnum"] }) {
  const o = OUTCOME[outcome];
  return <span className={chip({ state: o.tone })}><span aria-hidden="true">{o.glyph}</span>{o.label}</span>;
}

/** On, off, or turned off by Softwarden (a 410 Gone or too many failed event deliveries), glyph plus words. */
export function EndpointStateChip({ w }: { w: Pick<Webhook, "enabled" | "disabled_at"> }) {
  if (w.enabled) return <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>On</span>;
  if (w.disabled_at) return <span className={chip({ state: "overdue" })}><span aria-hidden="true">!</span>Turned off by Softwarden</span>;
  return <span className={chip({ state: "neutral" })}><span aria-hidden="true">–</span>Off</span>;
}

/** The HTTP answer in words: "HTTP 202", or "No answer" when none arrived. */
export const answerText = (code: number | null | undefined) => (code === null || code === undefined ? "No answer" : `HTTP ${code}`);

/** "record.*, renewal.decided": the subscriptions as the API stores them, family wildcards first. */
export function eventsText(events: readonly string[]) {
  const wild = events.filter((e) => e.endsWith(".*"));
  const types = events.filter((e) => !e.endsWith(".*"));
  return [...wild, ...types].join(", ");
}

/** An endpoint's last delivery: its outcome, the answer and when, linked to the log; "Nothing sent yet" before any. */
export function LastDeliveryCell({ w }: { w: Webhook }) {
  const last = w.last_delivery;
  if (!last) return <span className="text-muted-foreground">Nothing sent yet</span>;
  return (
    <span className="inline-flex flex-col items-start gap-0.5">
      <OutcomeChip outcome={last.outcome} />
      <span className="text-sm text-muted-foreground"><span className="tabular">{when(last.at)}</span>, {answerText(last.status_code)}</span>
      <Link to={`${LOG_PAGE}/${last.delivery}`} className="text-sm">View delivery<span className="sr-only"> to {w.name}</span></Link>
    </span>
  );
}

/** 24 random bytes as URL-safe base64 (32 characters): a signing secret the receiver can paste. */
export function generateSecret() {
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_");
}
