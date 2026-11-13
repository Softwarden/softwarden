/** Notification rules, channels and the notification log: the API's shapes (notify/rule_serializers.py,
 *  notify/channel_serializers.py) and their words. Imported only by the lazily loaded admin pages. */
import type { ReminderSourceType, ReminderStatus, Role, UserRef } from "@/api";
import { Link } from "react-router-dom";
import { chip } from "@/components/sw";

export type Ref = { id: string; name: string };
export type DestinationRef = Ref & { enabled: boolean };

export type NotificationRule = {
  id: string; name: string; kind: "reminders" | "digest"; builtin: boolean; builtin_key: string; enabled: boolean;
  source_types: ReminderSourceType[]; lead_times_days: number[]; escalation_days: number[]; repeat_every_days: number | null; escalate_when_acknowledged: boolean;
  send_email: boolean; teams_destinations: DestinationRef[]; webhook_destinations: DestinationRef[];
  notify_routed: boolean; people: Ref[]; team: Ref | null; roles: Role[];
  escalation_people: Ref[]; escalation_team: Ref | null; escalation_roles: Role[];
  fallback_team: Ref | null; fallback_person: Ref | null;
  criticalities: string[]; record_kinds: string[]; vendor: Ref | null; min_annual_value: string | null;
  /** The daily digest only: HH:MM in the organization's timezone. */
  digest_time: string | null;
  created_at: string; updated_at: string;
};

export type PreviewEvent = {
  date: string; kind: "reminder" | "escalation"; lead_time_days: number | null; overdue_step: number | null;
  reminder: { id: string; source_type: ReminderSourceType; title: string; label: string; due_date: string; status: ReminderStatus; snoozed_until: string | null };
  recipients: { name: string; email: string; user: number | null; person: string | null }[];
  destinations: { channel: "teams" | "webhook"; id: string; name: string }[];
  opted_out: string[];
  fallback: "" | "rule" | "coordinator";
};
export type RulePreview = { rule: string; start: string; days: number; events: PreviewEvent[] };

export type Channel = "email" | "teams" | "webhook";
export type DeliveryKind = "reminder" | "escalation" | "digest" | "subscription" | "test" | "event";
export type DeliveryStatus = "queued" | "sending" | "sent" | "failed" | "skipped";
export type Delivery = {
  id: string; channel: Channel; kind: DeliveryKind; status: DeliveryStatus; recipient: string;
  destination: { id: string | null; name: string } | null; subject: string;
  reminder: { id: string; title: string; label: string; due_date: string } | null;
  rule: string | null; rule_name: string; lead_time_days: number | null; overdue_step: number | null;
  due_date: string | null; baseline: boolean; attempts: number; max_attempts: number; next_attempt_at: string | null;
  last_error: string; sent_at: string | null; client_request_id: string | null; resend_of: string | null;
  requested_by: UserRef | null; created_at: string; updated_at: string;
};
export type DeliveryDetail = Delivery & { content: Record<string, unknown>; resends: string[] };
export type TestSendResult = { ok: boolean; detail: string; delivery: Delivery };
export type RuleTestSendResult = { ok: boolean; results: TestSendResult[] };
export type TestResult = { ok: boolean; detail: string };

export type EmailTransport = "installation_default" | "graph" | "smtp";
export type SmtpSecurity = "starttls" | "tls" | "none";
export type EmailSettings = {
  saved: boolean; transport: EmailTransport;
  graph_tenant_id: string; graph_client_id: string; graph_secret_set: boolean; graph_secret_set_on: string | null; sender_mailbox: string;
  smtp_host: string; smtp_port: number | null; smtp_security: SmtpSecurity; smtp_username: string;
  smtp_password_set: boolean; smtp_password_set_on: string | null; from_address: string;
  installation_default: { configured: boolean; from_address: string };
  last_test_at: string | null; last_test_ok: boolean | null; last_test_detail: string; updated_at: string | null;
};
export type Destination = {
  id: string; name: string; enabled: boolean; url_host: string; url_set_on: string | null;
  last_test_at: string | null; last_test_ok: boolean | null; last_test_detail: string; created_at: string; updated_at: string;
  /** Webhooks only. */
  signing_secret_set_on?: string | null;
};

// ---- words ----------------------------------------------------------------------------------------------------------

export const SOURCE_TYPES: { value: ReminderSourceType; label: string }[] = [
  { value: "renewal_cycle", label: "Renewal action deadlines" },
  { value: "contract", label: "Contract cancellation deadlines" },
  { value: "periodic_review", label: "Periodic reviews" },
  { value: "record_review", label: "Record reviews" },
  { value: "custom_date", label: "Custom dates" },
  { value: "credential_expiry", label: "Credential expiry" },
  { value: "vendor_risk", label: "Vendor risk documents" },
  { value: "end_of_life", label: "Version end of life" },
];
export const sourceLabel = (v: string) => SOURCE_TYPES.find((s) => s.value === v)?.label ?? v;
export const ROLES: { value: Role; label: string }[] = [{ value: "admin", label: "Admins" }, { value: "editor", label: "Editors" }, { value: "viewer", label: "Viewers" }];
export const CRITICALITY_CHOICES = [
  { value: "critical", label: "Critical" }, { value: "high", label: "High" }, { value: "medium", label: "Medium" }, { value: "low", label: "Low" },
];
export const RECORD_KIND_CHOICES = [
  { value: "application", label: "Applications" }, { value: "component", label: "Components" }, { value: "ai_agent", label: "AI agents" },
];
export const CHANNEL_LABEL: Record<Channel, string> = { email: "Email", teams: "Teams", webhook: "Webhook" };
export const DELIVERY_KIND_LABEL: Record<DeliveryKind, string> = {
  reminder: "Reminder", escalation: "Overdue escalation", digest: "Digest", subscription: "Saved view subscription", test: "Test",
  event: "Webhook event",
};
export const TRANSPORT_LABEL: Record<EmailTransport, string> = {
  installation_default: "Installation default (the server's SMTP settings)", graph: "Microsoft Graph (sendMail)", smtp: "SMTP server",
};
export const SECURITY_LABEL: Record<SmtpSecurity, string> = {
  starttls: "STARTTLS", tls: "TLS (implicit, usually port 465)", none: "None (local relays and Mailpit only)",
};

const joinWords = (list: string[]) => (list.length <= 1 ? list.join("") : `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`);
const dayWord = (days: number[]) => (days.length === 1 && days[0] === 1 ? "day" : "days");

/** "90, 60, 30 and 7 days before" from [90, 60, 30, 7]; 0 reads "on the due date". */
export function leadText(days: number[]) {
  const before = days.filter((d) => d > 0);
  const onTheDay = days.includes(0);
  if (!before.length) return onTheDay ? "On the due date" : "";
  const text = `${joinWords(before.map(String))} ${dayWord(before)} before`;
  return onTheDay ? `${text}, and on the due date` : text;
}
/** "1, 3 and 7 days after" from [1, 3, 7]. */
export const stepText = (days: number[]) => (days.length ? `${joinWords(days.map(String))} ${dayWord(days)} after` : "");
/** A delivery's stage in words: "30 days before", "On the due date", "3 days overdue". */
export function stageText(d: { lead_time_days: number | null; overdue_step: number | null }) {
  if (d.overdue_step !== null) return `${d.overdue_step} ${d.overdue_step === 1 ? "day" : "days"} overdue`;
  if (d.lead_time_days === null) return "";
  if (d.lead_time_days === 0) return "On the due date";
  return `${d.lead_time_days} ${d.lead_time_days === 1 ? "day" : "days"} before`;
}

type Tone = "confirmed" | "pending" | "overdue" | "unreviewed" | "neutral";
const STATUS: Record<DeliveryStatus, { tone: Tone; glyph: string; label: string }> = {
  queued: { tone: "neutral", glyph: "◔", label: "Queued" },
  sending: { tone: "neutral", glyph: "▸", label: "Sending" },
  sent: { tone: "confirmed", glyph: "✓", label: "Sent" },
  failed: { tone: "overdue", glyph: "!", label: "Failed" },
  skipped: { tone: "neutral", glyph: "–", label: "Skipped" },
};
export const DELIVERY_STATUSES = Object.keys(STATUS) as DeliveryStatus[];
export const deliveryStatusLabel = (s: DeliveryStatus) => STATUS[s].label;

/** A delivery's status as a chip with a glyph and words; a baseline row says it was recorded, never sent. */
export function DeliveryStatusChip({ d }: { d: Pick<Delivery, "status" | "baseline"> }) {
  if (d.baseline) return <span className={chip({ state: "unreviewed" })}><span aria-hidden="true">○</span>Baseline, not sent</span>;
  const s = STATUS[d.status];
  return <span className={chip({ state: s.tone })}><span aria-hidden="true">{s.glyph}</span>{s.label}</span>;
}

/** A channel test's last outcome (email settings, a destination): never tested, passed or failed, with when. */
export function LastTest({ at, ok, detail }: { at: string | null; ok: boolean | null; detail: string }) {
  if (!at || ok === null) return <span className={chip({ state: "unreviewed" })}><span aria-hidden="true">○</span>Never tested</span>;
  return (
    <span className="inline-flex flex-col gap-0.5">
      <span className={chip({ state: ok ? "confirmed" : "overdue" })}><span aria-hidden="true">{ok ? "✓" : "!"}</span>{ok ? "Last test passed" : "Last test failed"}</span>
      <span className="text-sm text-muted-foreground">{at.slice(0, 16).replace("T", " ")} UTC{detail ? `: ${detail}` : ""}</span>
    </span>
  );
}

/** On or off as a chip with a glyph and words. */
export function OnOffChip({ on, onText = "On", offText = "Off" }: { on: boolean; onText?: string; offText?: string }) {
  return <span className={chip({ state: on ? "confirmed" : "neutral" })}><span aria-hidden="true">{on ? "✓" : "–"}</span>{on ? onText : offText}</span>;
}

/** A write-only secret: set (since when) or a named gap, never the value. */
export function SecretState({ setOn, what }: { setOn: string | null | undefined; what: string }) {
  return setOn
    ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>{what} set on {setOn}</span>
    : <span className={chip({ state: "gap" })}><span aria-hidden="true">⚠</span>No {what.toLowerCase()}</span>;
}

/** Where a test went: "Email to you@example.test", "Teams channel IT alerts". */
export function sentTo(d: Delivery) {
  if (d.channel === "email") return `Email to ${d.recipient}`;
  return `${d.channel === "teams" ? "Teams channel" : "Webhook"} ${d.destination?.name ?? ""}`.trim();
}

/** A test send's outcome per channel, glyph plus words, each linked to its row in the notification log. Render inside a
 *  status region so it is announced. */
export function TestResultList({ results }: { results: TestSendResult[] }) {
  return (
    <ul className="m-0 flex list-none flex-col gap-2 p-0" data-testid="test-results">
      {results.map((r) => (
        <li key={r.delivery.id} className={`rounded-md border px-3 py-2 ${r.ok ? "border-success-border bg-success-bg text-success-foreground" : "border-l-4 border-error-border bg-error-bg"}`}>
          <span className="font-semibold"><span aria-hidden="true">{r.ok ? "✓ " : "! "}</span>{sentTo(r.delivery)}: {r.ok ? "sent" : "not sent"}.</span>
          {!r.ok && <span className="block [overflow-wrap:anywhere]">{r.detail}</span>}
          <Link to={`/admin/notification-log/${r.delivery.id}`} className="ml-0 block text-sm">View in the notification log<span className="sr-only">: {sentTo(r.delivery)}</span></Link>
        </li>
      ))}
    </ul>
  );
}
