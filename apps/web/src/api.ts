import type { components } from "@/generated/openapi";
import type { CatalogKind, RecordKind } from "@/lib/kinds";
import { createSessionGate } from "@/lib/session-gate";
import { assertSafePath } from "@/lib/safe-path";

export type Person = { id: string; name: string; email: string; department: { id: string; name: string } | null; active: boolean };
export type Team = { id: string; name: string };
export type UserRef = { id: number; name: string; email: string };
export type Role = "admin" | "editor" | "viewer";
/** One organization the account belongs to (`auth/me.organizations`): the organization's ID and name, and the role. */
export type Membership = { id: string; name: string; role: Role };
export type Me = {
  user: UserRef; role: Role; must_change_password: boolean; organization: { id: string; name: string; timezone: string; /** Set on creation; sign-in links name it (`/login?org=<slug>`). */ slug?: string };
  /** Every organization the account can switch to, sorted by name (one entry for a single-organization account). */
  organizations: Membership[];
  /** Capability names enabled for the current organization: gate on these, never on the edition. */
  capabilities: string[];
  /** Admins only: the latest digest run's failures, for the banner. */
  digest_problem?: { for_date: string; failed: number } | null;
  /** Admins only: webhook endpoints Softwarden turned off after a 410 Gone or too many failed deliveries. */
  webhook_problem?: components["schemas"]["WebhookProblem"] | null;
  /** The edition, shown only (gate on `capabilities`). */
  edition?: components["schemas"]["EditionEnum"];
  /** Set while the organization is scheduled for deletion (every member sees the banner); `final_export` is
   *  for Admins only. */
  organization_deletion?: components["schemas"]["OrganizationDeletion"] | null;
  /** Set while the service operator has suspended the organization. */
  organization_suspended?: components["schemas"]["OrganizationSuspended"] | null;
  /** The installation mode: "cloud" on the hosted service. For wording about who can act, not for gating. */
  deployment?: components["schemas"]["DeploymentEnum"];
  /** Whether the account is platform staff (the console at /platform). */
  platform_staff?: boolean;
  /** Set while platform staff act as this member in this browser session (the permanent banner). */
  impersonation?: components["schemas"]["ImpersonationBanner"] | null;
  /** Whether this server accepts the simulated tenant and `*.simulated.invalid` hosts. Show the
   *  hints that suggest them only when true. */
  simulation_allowed?: boolean;
};
/** `platform/session`: the staff account signed in, and its impersonation. A staff account with no membership
 *  gets this from `auth/login` instead of `me`, and its `auth/me` is 403. */
export type PlatformSession = components["schemas"]["PlatformSession"];
export type ReviewState = "unreviewed" | "needs_confirmation" | "overdue" | "confirmed";
export type Application = {
  id: string;
  name: string;
  slug: string;
  description: string;
  lifecycle: string;
  criticality: string | null;
  purpose: string;
  vendor: { id: string; name: string } | null;
  built_by: string;
  license_model: string;
  documentation_url: string;
  support_url: string;
  business_owner: Person | null;
  technical_owner: Person | null;
  support_team: Team | null;
  it_steward: UserRef | null;
  gaps: string[];
  review_state: ReviewState;
  review_why: string;
  next_review_date: string | null;
};
export type Instance = { id: string; application: string; name: string; environment: string; url: string; status: "operational" | "degraded" | "offline" | "unknown" };
/** A reference with an ID and a name (department, category, team, vendor). */
export type NamedRef = { id: string; name: string };
/** An AI model as an agent lists it (`models_used`). */
export type AIModelRef = { id: string; name: string; version: string; provider: NamedRef };
/** Compliance profile fields: present only while the organization has the profile on; absent otherwise. */
export type ComplianceFields = {
  is_gxp?: boolean | null; validation_status?: string | null; validation_doc_url?: string | null;
  last_periodic_review?: string | null; next_periodic_review?: string | null; risk_tier?: string | null;
};
/** An application, component or AI agent as returned by GET applications/:id, components/:id and ai-agents/:id. Each
 *  kind carries its own fields (the others are absent). */
export type CatalogRecord = Omit<Application, "vendor"> & ComplianceFields & {
  kind: RecordKind;
  archived: boolean;
  archived_at: string | null;
  archived_by: { id: number; name: string } | null;
  tags?: string[];
  /** The record's end-of-life tracking (applications and components; null when untracked or without `eol`). */
  end_of_life?: components["schemas"]["RecordEndOfLife"] | null;
  /** An application's SSO and MFA coverage (null without `discovery.entra`; absent for other kinds). */
  sso_coverage?: components["schemas"]["SsoCoverage"] | null;
  // shared
  department?: NamedRef | null; cost_center?: string; data_classification?: string;
  // applications
  vendor?: NamedRef | null;
  category?: NamedRef | null; deployment_model?: string; version?: string; primary_url?: string; admin_url?: string;
  authentication_method?: string; sso_enabled?: boolean | null; scim_provisioning?: boolean | null;
  entra_app_id?: string; service_principal_id?: string;
  instances?: Instance[];
  // components
  component_type?: string;
  host_runtime?: string;
  repository_url?: string;
  hosting?: string; runbook_url?: string; schedule_or_trigger?: string; environment?: string;
  azure_resource_id?: string; pp_environment_id?: string; external_id?: string;
  // AI agents
  platform?: string; models_used?: AIModelRef[]; channels?: string[]; knowledge_sources?: string; tools_connectors?: string;
  auth_mode?: string; run_as_identity?: string; data_access_scope?: string; human_in_loop?: boolean | null;
  actions_can_write?: boolean | null; risk_classification?: string; repo_url?: string;
  /** Custom field values: every field that applies to the kind, in weight order, null when unset. */
  custom_field_data?: Record<string, unknown>;
  /** Data quality: the completeness score and the checks it fails; absent without the data_quality capability. */
  quality?: RecordQuality;
};
/** A cursor-paginated list: pass `next` or `previous` back as `?cursor=`. */
export type Page<T> = { results: T[]; next: string | null; previous: string | null };
export type Review = {
  id: string;
  confirmed_by: Person;
  recorded_by: UserRef;
  confirmation_date: string;
  next_review_date: string;
  evidence: string;
  inactive_reason: string;
};
export type Steward = { id?: number; name: string; coordinator?: boolean };
export type OwnershipRow = { id: string; kind: RecordKind; name: string; criticality: string | null; gaps: string[]; why: string; steward: Steward; snooze?: Snooze | null };
export type ReviewRow = { id: string; kind: RecordKind; name: string; state: ReviewState; why: string; due: string | null; steward: Steward; snooze?: Snooze | null };
export type AttentionScope = "mine" | "all" | "unassigned";
export type AttentionLinkEnd = { id: string; kind: LinkKind; name: string; instance: string | null };
/** A relationship or integration whose derived link state is Needs verification; `record` is where Confirm link lives. */
export type LinkRow = {
  id: string; link_kind: "relationship" | "integration"; type: string;
  /** The relationship type's name ("Depends on"), or "Integration" (absent from an older API). */
  type_name?: string;
  record: { id: string; kind: LinkKind; name: string };
  source: AttentionLinkEnd; target: AttentionLinkEnd; why: string; last_confirmed_on: string; steward: Steward; snooze?: Snooze | null;
};
export type StaleAssessmentRow = {
  id: string; title: string; status: string; change_type: string; target: { id: string; kind: LinkKind; name: string };
  target_date: string | null; stale_since: string; why: string; steward: Steward; snooze?: Snooze | null;
};
export type Attention = {
  scope: AttentionScope; records: number; counts: Record<AttentionScope, number>;
  ownership_gaps: OwnershipRow[]; agent_gaps: OwnershipRow[]; reviews: ReviewRow[]; links_to_verify: LinkRow[]; stale_assessments: StaleAssessmentRow[];
  renewal_actions: RenewalActionRow[]; renewal_gaps: RenewalGapRow[]; discovery_candidates: DiscoveryQueueRow[];
  /** One row per data-quality rule and IT steward; absent from an older API. */
  data_quality?: DataQualityQueueRow[];
  /** Licence pools with proposed seat counts; absent from an older API. */
  seat_counts?: SeatCountQueueRow[];
  /** Live reminders not shown in another queue; absent from an older API. */
  reminders?: ReminderQueueRow[];
  /** Linked records a source no longer lists; absent from an older API. */
  discovery_missing?: MissingQueueRow[];
  /** Vendors whose risk file has a gap; absent from an older API. */
  vendor_risk?: VendorRiskQueueRow[];
  /** Tracked versions at or near end of life (capability `eol`); absent from an older API. */
  end_of_life?: EndOfLifeQueueRow[];
  /** Data flows with a risk (capability `data_flows`); absent from an older API. */
  data_flows?: DataFlowQueueRow[];
};

export type LinkState = "unconfirmed" | "needs_verification" | "confirmed";
export type LinkKind = "application" | "component" | "ai_agent" | "integration";
/** One end of a relationship or integration: the record and, optionally, one of its instances. */
export type LinkEnd = { id: string; kind: LinkKind; name: string; archived: boolean; instance: { id: string; name: string } | null };
/** Derived link state: the three members every relationship and integration response carries. */
export type LinkFields = { link_state: LinkState; link_why: string; last_confirmed_on: string | null };
/** A relationship type as a link carries it: the link reads "<source> <name> <target>" and, from the target,
 *  "<target> <inverse_name> <source>". `slug` is the link's `type`; core types are depends_on, implements and replaces,
 *  and Admins add more, so a slug is any string. */
export type RelationshipTypeBrief = { id: string; slug: string; name: string; inverse_name: string; is_dependency: boolean; is_core: boolean };
/** One row of GET relationship-types: `usage` counts the relationships of the type. */
export type RelationshipTypeDef = RelationshipTypeBrief & { description: string; usage: number; created_at: string; updated_at: string };
export type Relationship = LinkFields & {
  id: string; type: string; relationship_type: RelationshipTypeBrief; source: LinkEnd; target: LinkEnd; purpose: string; created_by: UserRef | null; created_at: string;
};
export type Integration = LinkFields & {
  id: string; kind: "integration"; name: string; slug: string; description: string; lifecycle: string;
  data_purpose: string; protocol: string; schedule: string; documentation_url: string;
  /** "" when not recorded. Labels in INTEGRATION_DIRECTIONS, INTEGRATION_AUTH_METHODS, DATA_CLASSIFICATIONS. */
  direction: string; auth_method: string; monitoring_url: string; data_classification: string;
  /** Data-flow map: what kinds of data it carries (DATA_CATEGORIES order), and whether any is personal. */
  data_categories?: DataCategory[]; contains_personal_data?: boolean;
  source: LinkEnd; target: LinkEnd;
  technical_owner: Person | null; support_team: Team | null; it_steward: UserRef | null;
  archived: boolean; archived_at?: string | null; archived_by?: { id: number; name: string } | null;
  vendor_native: boolean; implemented_by: { id: string; name: string; archived: boolean }[];
  custom_field_data?: Record<string, unknown>;
};
export type LinkConfirmation = {
  id: string; confirmed_by: Person; recorded_by: UserRef; confirmed_on: string; evidence: string; inactive_reason: string; created_at: string;
};

/** An RFC 9457 problem returned by the API. */
export class Problem extends Error {
  /** Seconds from a 429's Retry-After header, when the API sent one. */
  retryAfter?: number;
  constructor(public status: number, public detail: string, public errors: Record<string, string[]> = {}, public blockingReferences: Record<string, number> = {}, public type = "about:blank", public body: Record<string, unknown> = {}) {
    super(detail);
  }
}

// ---- problems the whole app handles the same way ------------------------------------------------------

export const LIMIT_REACHED = "/problems/limit-reached";
export const IMPERSONATION_RESTRICTED = "/problems/impersonation-restricted";
export const IMPERSONATION_ENDED = "/problems/impersonation-ended";
export const IMPERSONATION_STARTED = "/problems/impersonation-started";
export const ORGANIZATION_SUSPENDED = "/problems/organization-suspended";
export const REAUTHENTICATION_REQUIRED = "/problems/reauthentication-required";
/** The session ended (timed out, the server restarted, the password was reset): the shell asks to sign in again. */
export const NOT_SIGNED_IN = "/problems/not-signed-in";
/** A platform staff session that hasn't sent its second factor: the console shows its code page. */
export const SECOND_FACTOR_REQUIRED = "/problems/second-factor-required";
/** The window event every problem of a type in GLOBAL_PROBLEMS is also sent as, so the app shell can act on it whatever
 *  screen made the request: an impersonation that ended goes back to the console, one that started in another tab
 *  reloads this one, a suspension shows the suspended page. */
export const PROBLEM_EVENT = "softwarden:problem";
const GLOBAL_PROBLEMS = new Set([IMPERSONATION_ENDED, IMPERSONATION_STARTED, ORGANIZATION_SUSPENDED, NOT_SIGNED_IN, SECOND_FACTOR_REQUIRED]);

// ---- which impersonation this tab is in (docs/API.md "Which impersonation a tab is in") -----------------

/** The impersonation this tab believes in: the id of the impersonation this tab is showing, or null when it shows none.
 *  Only App sets it (setTabImpersonation), when this tab takes a session it will show: a sign-in, Start and Stop, and a
 *  reload whose impersonation is the one already believed. A reload that finds a different one (started, stopped or
 *  replaced in another tab) changes it only together with what the tab shows: App moves the tab (home, or the console)
 *  and says why, so a form filled for one identity is never saved as another. It is never read from the shared
 *  session, the cookie or another tab: the point is to tell the server what *this* tab is showing. Each tab has its
 *  own copy (module state is per page). */
let tabImpersonation: string | null = null;
export function setTabImpersonation(id: string | null) { tabImpersonation = id; }
/** Headers every non-safe request sends: the CSRF token, and `X-Softwarden-Impersonation` with this tab's impersonation
 *  id or `none`. A write from a tab that is behind (an earlier impersonation, one stopped since, or one started since)
 *  is refused with 409 impersonation-ended or impersonation-started and doesn't run. Uploads use it too. */
export function writeHeaders(): Record<string, string> {
  return { "X-CSRFToken": csrfToken(), "X-Softwarden-Impersonation": tabImpersonation ?? "none" };
}
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** Each limit's short name (GET organization/limits names them; `label` there says what is counted). Typed on the API's
 *  LimitNameEnum, so a limit the API adds fails the build until it has a name here. */
export type LimitName = components["schemas"]["LimitNameEnum"];
export const LIMIT_NAME: Record<LimitName, string> = {
  records: "Catalog records", other_records: "Other records", connectors: "Discovery connectors", members: "Active members",
  attachment_bytes: "Attachment storage", api_rate_read: "API reads per minute", api_rate_write: "API writes per minute",
  invitation_emails_hour: "Invitation emails per hour", invitation_emails_day: "Invitation emails per day",
};
/** A limit's short name; an unknown one (from an API newer than this page) as the API sent it. */
export const limitName = (name: string) => (LIMIT_NAME as Record<string, string>)[name] ?? name;
/** The send budgets count a sliding window, so their usage is what was sent in it. */
const LIMIT_WINDOW: Record<string, string> = { invitation_emails_hour: "in the last hour", invitation_emails_day: "in the last 24 hours" };
/** What a limit's usage did: "sent in the last hour" for a send budget, "used" for the others. */
export const limitVerb = (name: string) => (LIMIT_WINDOW[name] ? `sent ${LIMIT_WINDOW[name]}` : "used");
/** "3 of 5 sent in the last hour" for a send budget, "12 of 5,000 used" for the others. */
export const limitUsage = (name: string, usage: number, maximum: number) => `${limitAmount(name, usage)} of ${limitAmount(name, maximum)} ${limitVerb(name)}`;
/** Bytes as "512 bytes", "1.5 KB", "5 GB". */
export function byteSize(n: number) {
  if (n < 1024) return `${n} ${n === 1 ? "byte" : "bytes"}`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${Number.isInteger(value) ? value : value.toFixed(1)} ${units[i]}`;
}
/** A limit's amount: "1,234" in English digit grouping, or bytes for the attachment storage limit. */
export const limitAmount = (name: string, n: number) => (name === "attachment_bytes" ? byteSize(n) : n.toLocaleString("en-US"));

/** The server's clock minus the browser's, in milliseconds, from the newest response's Date header (to the second).
 *  Deadlines the server sets (an impersonation's `expires_at`) are counted down against `serverNow()`, so a browser
 *  clock that runs ahead or behind doesn't end them early or late. */
let serverOffset = 0;
export const serverNow = () => Date.now() + serverOffset;
function noteServerClock(response: Response) {
  const at = Date.parse(response.headers.get("Date") ?? "");
  // The header is truncated to the second: on average the server's time is half a second later than it says.
  if (!Number.isNaN(at)) serverOffset = at + 500 - Date.now();
}

/** The sentence for a 409 limit-reached: which limit, the usage of the maximum, then the API's own words (what to do). */
type LimitReachedProblem = components["schemas"]["LimitReachedProblem"];
/** Whether an error body is the API's LimitReachedProblem (its members are typed there; an older server may lack some). */
function isLimitReached(data: Record<string, unknown>): data is LimitReachedProblem & Record<string, unknown> {
  return data.type === LIMIT_REACHED && typeof data.limit === "string" && typeof data.maximum === "number" && typeof data.usage === "number";
}
function limitDetail(data: Record<string, unknown>, detail: string) {
  if (!isLimitReached(data)) return detail;
  return `Limit reached: ${limitName(data.limit)}, ${limitUsage(data.limit, data.usage, data.maximum)}. ${detail}`;
}

/** "Too many attempts. Try again in 3 minutes.", from a 429's Retry-After (seconds, when the API sent one). */
export function tooManyAttempts(retryAfter?: number) {
  if (!retryAfter) return "Too many attempts. Wait a few minutes, then try again.";
  if (retryAfter < 120) return `Too many attempts. Try again in ${retryAfter} ${retryAfter === 1 ? "second" : "seconds"}.`;
  return `Too many attempts. Try again in ${Math.ceil(retryAfter / 60)} minutes.`;
}

/** The Problem for an error answer, from every request path (api(), apiWithEtag(), uploads): a limit-reached problem's
 *  detail names the limit, usage and maximum, so every screen that shows `detail` says it; problems the shell acts on
 *  are sent as PROBLEM_EVENT too. */
export function problemFrom(status: number, data: Record<string, unknown>, retryAfter?: number | null, fallback = "Something went wrong.") {
  const type = typeof data.type === "string" ? data.type : "about:blank";
  let detail = typeof data.detail === "string" ? data.detail : fallback;
  if (type === LIMIT_REACHED) detail = limitDetail(data, detail);
  // The framework's "Authentication credentials were not provided." in words; the shell offers to sign in again.
  if (type === NOT_SIGNED_IN) detail = "Your session ended, so this couldn't be done. Sign in again to continue.";
  const problem = new Problem(status, detail, (data.errors as Record<string, string[]>) ?? {}, (data.blocking_references as Record<string, number>) ?? {}, type, data);
  if (retryAfter && retryAfter > 0) problem.retryAfter = retryAfter;
  if ((GLOBAL_PROBLEMS.has(type) || status === 401) && typeof window !== "undefined") window.dispatchEvent(new CustomEvent(PROBLEM_EVENT, { detail: problem }));
  return problem;
}

/** The 409 the API sends when a record (or a retirement assessment) would be retired while active records depend on it. */
export const RETIREMENT_ACK = "/problems/retirement-needs-acknowledgment";
export type RetirementDependent = { id: string; name: string; kind: LinkKind; lifecycle: string };
export type RetirementNotRequired = { id: string; name: string; kind: LinkKind; reason: string };
export type RetirementCheck = { detail: string; dependents: RetirementDependent[]; not_required: RetirementNotRequired[] };
/** The acknowledgment problem's body, or null for any other error. */
export function retirementCheck(e: unknown): RetirementCheck | null {
  if (!(e instanceof Problem) || e.status !== 409 || e.type !== RETIREMENT_ACK) return null;
  return { detail: e.detail, dependents: (e.body.dependents as RetirementDependent[]) ?? [], not_required: (e.body.not_required as RetirementNotRequired[]) ?? [] };
}

export type AssessmentStatus = "draft" | "in_review" | "ready" | "closed" | "cancelled";
export type ChangeType = "change" | "retirement";
export const ASSESSMENT_STATUSES: AssessmentStatus[] = ["draft", "in_review", "ready", "closed", "cancelled"];
export const ASSESSMENT_STATUS_LABEL: Record<AssessmentStatus, string> = {
  draft: "Draft", in_review: "In review", ready: "Ready for external execution", closed: "Closed", cancelled: "Canceled",
};
export type SnapshotCounts = { dependents: number; cycles: number; data_flows: number; implementers: number; links: number };
export type SnapshotSummary = {
  id: string; taken_at: string; taken_by: UserRef | null; is_current: boolean; stale: boolean; stale_since: string | null; stale_reasons: string[]; counts: SnapshotCounts;
};
export type SnapshotRef = { id: string; name: string; kind: LinkKind };
export type SnapshotOwner = { id: string; name: string; active: boolean } | null;
export type SnapshotOwners = { business_owner: SnapshotOwner; technical_owner: SnapshotOwner };
export type SnapshotNode = {
  id: string; resource: SnapshotRef & { archived: boolean }; role: "dependent" | "data_flow" | "implementer"; depth: number; path: SnapshotRef[]; in_cycle: boolean;
  lifecycle_then: string; lifecycle_now: string; owners_then: SnapshotOwners; owners_now: SnapshotOwners; changed: { lifecycle: boolean; owners: boolean };
};
export type SnapshotLinkRow = {
  kind: "relationship" | "integration"; id: string | null; removed: boolean; changed: boolean; type: string;
  /** The type's labels (current, else stored at capture, else the slug); null for an integration. */
  type_name: string | null; inverse_name: string | null;
  source: SnapshotRef; target: SnapshotRef;
  /** The confirmation state now, which is null once the link was removed. */
  link_state: LinkState | null; link_why: string | null; last_confirmed_on: string | null;
};
export type SnapshotFull = SnapshotSummary & { nodes: SnapshotNode[]; links: SnapshotLinkRow[] };
/** One row of GET assessments, and what every write returns. */
export type AssessmentRow = {
  id: string; title: string; description: string; findings: string; change_type: ChangeType; status: AssessmentStatus;
  target: { id: string; name: string; kind: LinkKind; lifecycle: string; archived: boolean };
  owner: UserRef | null; target_date: string | null; ticket_url: string; retirement_ack_reason: string;
  stale: boolean; snapshot: SnapshotSummary | null; created_at: string; updated_at: string;
};
export type AssessmentDetail = Omit<AssessmentRow, "snapshot"> & { snapshot: SnapshotFull | null; history: SnapshotSummary[] };

export function csrfToken(): string {
  const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

// ---- session changes (starting and stopping an impersonation) ----------------------------------------------------
// Both rotate the session key: a change runs alone, after this tab's requests in flight and before any asked for
// meanwhile (src/lib/session-gate.ts says why). api() and apiWithEtag() join it. Uploads (the attachment XHR), the
// import upload and downloads (lib/download.ts, plain links) don't: they neither wait for a change nor count as in
// flight, so one racing a Start or Stop can still meet a rotated key.
const gate = createSessionGate();

/** Sign out (`POST auth/logout`). A sign-out sent with a session key rotated a moment before (an organization switch
 *  or Stop in flight, perhaps in another tab) is refused with 403 not-signed-in while the newer, valid cookie stays: so
 *  that answer gets a fresh CSRF read and one more sign-out. A second not-signed-in means signed out. Any other
 *  failure is thrown, for the caller to say. */
export async function signOutRequest(): Promise<void> {
  const notSignedIn = (e: unknown) => e instanceof Problem && e.status === 403 && e.type === "/problems/not-signed-in";
  try {
    await api("auth/logout", { method: "POST" });
  } catch (e) {
    if (!notSignedIn(e)) throw e;
    await api("auth/csrf").catch(() => undefined);
    try {
      await api("auth/logout", { method: "POST" });
    } catch (again) {
      if (!notSignedIn(again)) throw again;
    }
  }
}

/** Run `change` (a request that rotates the session key, sent with `exclusive: true`) alone. */
export const sessionChange = <T>(change: () => Promise<T>) => gate.change(change);

/** `signal` cancels the request (TanStack Query passes one, so a query nobody shows any more is aborted). `exclusive`:
 *  this is the request of a sessionChange, which doesn't wait for itself. */
export function api<T>(path: string, init: { method?: string; body?: unknown; signal?: AbortSignal; headers?: Record<string, string>; exclusive?: boolean } = {}): Promise<T> {
  return gate.tracked(!!init.exclusive, () => send<T>(path, init));
}

/** A write through api() that also returns the ETag of the record it wrote: the next save's If-Match, taken
 *  from this answer rather than a second read, so a change someone else lands in between isn't absorbed. */
export function apiWriteWithEtag<T>(path: string, init: { method: string; body?: unknown; headers?: Record<string, string> }): Promise<{ data: T; etag: string | null }> {
  return gate.tracked(false, async () => {
    let etag: string | null = null;
    const data = await send<T>(path, init, (response) => { etag = response.headers.get("ETag"); });
    return { data, etag };
  });
}

async function send<T>(path: string, init: { method?: string; body?: unknown; signal?: AbortSignal; headers?: Record<string, string> }, onResponse?: (response: Response) => void): Promise<T> {
  assertSafePath(path);
  const method = init.method ?? "GET";
  // `headers` adds request headers, such as If-Match on an edit (412 when the record changed since it was read).
  const headers: Record<string, string> = { Accept: "application/json", ...init.headers };
  if (!SAFE_METHODS.has(method)) {
    headers["Content-Type"] = "application/json";
    Object.assign(headers, writeHeaders());
  }
  const response = await fetch(`/api/v1/${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
    signal: init.signal,
  });
  noteServerClock(response);
  onResponse?.(response);
  if (response.status === 204) return undefined as T;
  const data = await readBody(response);
  if (data === UNREADABLE) throw unreadable(response.status, method);
  if (!response.ok) throw problemFrom(response.status, data ?? {}, Number(response.headers.get("Retry-After")));
  return (data ?? {}) as T;
}

/** A 2xx body that isn't JSON: a reverse proxy, single sign-on gateway or captive portal answering /api/* with
 *  its own HTML page. Taking it as data would hand every screen something of the wrong shape. */
export const UNREADABLE_RESPONSE = "/problems/unreadable-response";
const UNREADABLE = Symbol("unreadable");
/** The body as JSON; null for an empty body (as before: `{}` to the caller); UNREADABLE when there is a body that isn't
 *  JSON, or the body couldn't be read. An error answer's unreadable body is read as `{}`, so it still gets its status's
 *  Problem. A canceled request (its signal aborted) still rejects with the AbortError. */
async function readBody(response: Response): Promise<Record<string, unknown> | null | typeof UNREADABLE> {
  let text: string;
  try {
    text = await response.text();
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    return response.ok ? UNREADABLE : {};
  }
  if (!text.trim()) return null;
  try {
    return JSON.parse(text);
  } catch {
    return response.ok ? UNREADABLE : {};
  }
}
/** The JSON of an answer fetched outside api() (an upload's multipart POST), read the way api() reads one: a 2xx
 *  body that isn't JSON throws the unreadable-response Problem instead of reaching the screen as `{}`; an error
 *  answer's unreadable body is `{}`, so it still gets its status's Problem from problemFrom. */
export async function readAnswer(response: Response, method = "GET"): Promise<Record<string, unknown>> {
  const data = await readBody(response);
  if (data === UNREADABLE) throw unreadable(response.status, method);
  return data ?? {};
}

/** A write's answer couldn't be read, so whether it was saved isn't known: say so. */
function unreadable(status: number, method = "GET") {
  const write = !SAFE_METHODS.has(method) ? " Your change may not have been saved: check before you try it again." : "";
  return new Problem(status, `The server's answer wasn't readable.${write} Reload the page to try again; if this keeps happening, something in front of Softwarden (a proxy or sign-in gateway) may be answering instead of it.`, {}, {}, UNREADABLE_RESPONSE);
}

export const STATE_LABEL: Record<ReviewState, string> = {
  unreviewed: "Unreviewed",
  needs_confirmation: "Needs confirmation",
  overdue: "Overdue",
  confirmed: "Confirmed",
};
export const STATE_GLYPH: Record<ReviewState, string> = { unreviewed: "○", needs_confirmation: "◐", overdue: "!", confirmed: "✓" };
export const LIFECYCLES = ["proposed", "evaluating", "active", "deprecated", "retired"];
export const CRITICALITIES = ["low", "medium", "high", "critical"];
/** Choice values from the catalog models (BuiltBy, LicenseModel, ComponentType). */
export const BUILT_BY = ["vendor", "internal", "contractor", "community"];
export const LICENSE_MODELS = ["commercial", "open_source", "free", "internal", "unknown"];
export const COMPONENT_TYPES = [
  "script", "scheduled_job", "runbook", "function", "logic_app", "automation_flow", "report", "ai_service", "middleware", "internal_api",
  "api", "service", "workflow", "infra_automation", "other",
];
export const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** Today in the organization's timezone as YYYY-MM-DD, plus `days`. Dates are judged in the organization's timezone,
 *  not the browser's: between 7 pm and midnight in New York a UTC organization is already on tomorrow, and a
 *  confirmation dated by the browser would land the day before the change it confirms. */
export function orgToday(timeZone: string, days = 0) {
  const today = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  if (!days) return today;
  const [y, m, d] = today.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}
const TYPE_LABEL: Record<string, string> = {
  logic_app: "Logic App", ai_service: "AI service", internal_api: "Internal API", api: "API", infra_automation: "Infrastructure automation",
};
export const typeLabel = (t?: string) => (t ? (TYPE_LABEL[t] ?? cap(t.replace(/_/g, " "))) : "—");

export const LINK_STATE_LABEL: Record<LinkState, string> = { unconfirmed: "Unconfirmed", needs_verification: "Needs verification", confirmed: "Confirmed" };

// ---- Renewals and notifications ------------------------------------------------------------------

export type AutoRenew = "yes" | "no" | "unknown";
export type PeriodUnit = "days" | "months" | "years";
export type Decision = "renew" | "cancel" | "renegotiate";
export type DeadlineLabel = "entered" | "calculated" | "assumed" | "renewal_action";
export type Timing = { days: number; overdue: boolean; text: string };
export type Deadline = { date: string | null; label: DeadlineLabel | null; label_text: string | null; explanation: string; timing: Timing | null };
export type Period = { value: number; unit: PeriodUnit };
export type CoveredApp = { id: string; name: string; lifecycle: string; archived: boolean };
export type RenewalCycle = {
  id: string; sequence: number; open: boolean; renewal_date: string | null; notice: Period | null; explicit_deadline: string | null;
  term: Period | null; applications: CoveredApp[]; deadline: Deadline; decision: Decision | null; decision_notes: string;
  decided_on: string | null; completed_on: string | null; completion_evidence: string; next_renewal_date: string | null;
  reminders: { days: number; on: string | null; status: "not_sent" | DigestStatus; sent_on: string | null }[];
  /** What a contract renew decision changed (money and dates before and after); null otherwise. */
  terms_change?: Record<string, string | null> | null;
};
export type Renewal = {
  id: string; name: string; vendor: { id: string; name: string } | null; responsible: Person | null; followup: UserRef | null;
  auto_renew: AutoRenew; archived: boolean; cycle: RenewalCycle; deadline: Deadline; gaps: string[]; cycle_count: number; updated_at: string;
  history?: RenewalCycle[];
  snooze?: (Snooze & { queue: "renewal_actions" | "renewal_gaps" }) | null;
  /** The linked contract the open cycle takes its renewal date, notice period and auto-renewal from (they
   *  can't be changed on the renewal while it is set). */
  contract?: { id: string; name: string } | null;
};
export const AUTO_RENEW_LABEL: Record<AutoRenew, string> = { yes: "Yes", no: "No", unknown: "Unknown" };
export const DECISION_LABEL: Record<Decision, string> = { renew: "Renew", cancel: "Cancel", renegotiate: "Renegotiate" };
export const periodText = (p: Period | null) => (p ? `${p.value} ${p.value === 1 ? p.unit.slice(0, -1) : p.unit}` : "—");

export type Snooze = { id: string; kind: "snooze" | "acknowledge"; until: string | null };
export type RenewalQueueRow = {
  id: string; kind: "renewal_action" | "renewal_gap"; name: string; vendor: string | null; cycle: string; deadline: string | null;
  label: DeadlineLabel | null; label_text: string | null; why: string; steward: Steward; snooze: Snooze | null;
};
export type RenewalActionRow = RenewalQueueRow & { timing: Timing; decision: Decision | null };
export type RenewalGapRow = RenewalQueueRow & { gaps: string[] };
export type AttentionQueue = "ownership_gaps" | "agent_gaps" | "reviews" | "links_to_verify" | "stale_assessments" | "renewal_actions" | "renewal_gaps" | "discovery_candidates" | "discovery_missing" | "data_quality" | "seat_counts" | "vendor_risk" | "end_of_life" | "data_flows";
/** A tracked version of a live record at end of life or within 180 days of it; `id` is the tracked version. */
export type EndOfLifeQueueRow = components["schemas"]["AttentionEndOfLife"];
/** A live data flow with a risk; `id` is the integration. */
export type DataFlowQueueRow = components["schemas"]["AttentionDataFlow"];
/** One row per vendor whose risk file has a gap: high or critical risk with no current SOC 2 or equivalent, or
 *  personal data with no signed DPA. `id` is the vendor; a vendor has no IT steward, so the coordinator catches it. */
export type VendorRiskQueueRow = {
  id: string; kind: "vendor_risk"; name: string; risk_tier: "low" | "medium" | "high" | "critical" | null;
  gaps: { code: "no_current_assurance" | "dpa_missing"; label: string }[]; why: string; steward: Steward; snooze: Snooze | null;
};
/** A linked record a source no longer lists (reconciliation, "No longer seen in source"); `id` is the observation. */
export type MissingQueueRow = {
  id: string; kind: "discovery_missing"; name: string; record: { id: string; kind: RecordKind; name: string };
  observation: { id: string; name: string; external_id: string }; connector: { id: string; name: string; source: DiscoverySourceId };
  since: string; why: string; steward: Steward; snooze: Snooze | null;
};
/** One row per licence pool whose seat counts a source disagrees with; `id` is the pool. */
export type SeatCountQueueRow = {
  id: string; kind: "seat_count"; name: string; application: { id: string; name: string }; proposals: number; why: string; steward: Steward; snooze: Snooze | null;
};
/** A live reminder (open, snoozed or acknowledged) due within the reminder window or overdue. Renewal-cycle
 *  and record-review reminders are folded into their Renewal actions and Reviews rows instead. `status` is as of today
 *  (a snooze whose date passed reads open); `snoozed_until` is set only while snoozed. Snooze, acknowledge and resolve
 *  apply to everyone it reaches, through `reminders/:id/…`. */
export type ReminderStatus = "open" | "snoozed" | "acknowledged" | "done" | "obsolete";
export type ReminderSourceType = "renewal_cycle" | "contract" | "periodic_review" | "record_review" | "custom_date" | "credential_expiry" | "vendor_risk" | "end_of_life";
export type ReminderQueueRow = {
  id: string; kind: "reminder"; source_type: ReminderSourceType; source_id: string; name: string; label: string; due: string; timing: Timing;
  status: ReminderStatus; snoozed_until: string | null; path: string | null; why: string; steward: Steward; snooze?: Snooze | null;
};
/** One row per data-quality rule and IT steward with failing records. `it_steward` is the user ID or null. */
export type DataQualityQueueRow = {
  id: string; kind: "data_quality"; rule: string; label: string; severity: Severity; count: number; it_steward: number | null;
  why: string; steward: Steward; snooze: Snooze | null;
};
/** One row per connector with candidates. */
export type DiscoveryQueueRow = { id: string; kind: "discovery"; name: string; new: number; changed: number; why: string; steward: Steward; snooze: Snooze | null };

export type DigestStatus = "pending" | "sent" | "failed" | "skipped";
export type DigestRow = {
  id: string; for_date: string; user: UserRef; status: DigestStatus; item_count: number; attempts: number;
  last_error: string; next_attempt_at: string | null; sent_at: string | null;
  delivery: string | null; // the delivery in the notification log
};
export type EmailStatus = {
  configured: boolean;
  last_run: { for_date: string; finished_at: string | null; sent: number; failed: number; pending: number; skipped: number } | null;
};
export type Preferences = {
  digest_enabled: boolean; reminders_enabled: boolean; digest_time: string; timezone: string; next_digest_at: string;
  /** Weekly saved view emails; absent from an older API (on). */
  subscriptions_enabled?: boolean;
};

/** A weekly email of a saved view's rows to one member (`saved-view-subscriptions`). `saved_view` is null once
 *  the view was deleted; its `name` is null when the view is private to someone else. `weekday` 0 is Monday. */
export type ViewSubscription = {
  id: string; saved_view: { id: string; name: string | null; kind: RecordKind; shared: boolean } | null;
  user: { id: number; name: string }; mine: boolean; weekday: number; weekday_name: string; active: boolean;
  /** The local date of the last weekly email queued. */
  last_sent: string | null;
  /** HH:MM in `timezone`: the organization's notification send time. */
  send_time: string; timezone: string; can_edit: boolean; created_at: string; updated_at: string;
};
export type SubscriptionTestResult = { ok: boolean; detail: string; status: string; delivery: string };
export const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] as const;

// ---- Discovery -----------------------------------------------------------------------------------

/** A connector run's status: queued until the worker starts it, then running, then finished. */
export type RunStatus = "queued" | "running" | "succeeded" | "failed";
export type DiscoverySourceId = "entra" | "entra_licenses" | "azure" | "powerplatform" | "halo" | "entra_shadow_it" | "entra_coverage" | "okta" | "google_workspace" | "entra_licence_usage";
/** One connector run as `connectors/{id}/runs`, `discovery/runs` and a connector's `last_run` / `active_run` give it.
 *  `skipped` is counts.skipped with each filter's label, largest first; `error` and `notes` are Softwarden's words. */
export type ConnectorRun = {
  id: string; connector: { id: string; name: string; source: DiscoverySourceId }; trigger: "manual" | "scheduled"; status: RunStatus;
  queued_at: string | null; started_at: string | null; finished_at: string | null; duration_seconds: number | null; requested_by: UserRef | null;
  counts: { seen?: number; kept?: number; skipped?: Record<string, number>; new?: number; changed?: number; unchanged?: number; gone?: number; out_of_scope?: number; not_read?: number; [key: string]: unknown };
  skipped: { filter: string; label: string; count: number }[]; notes: string[]; error: string;
  /** Why a failed run failed, when it has a code: `empty_listing`, the empty-listing guard. */
  failure_code: components["schemas"]["RunFailureCodeEnum"] | ""; job_id: string | null;
};
/** Each source reads only the keys its SCOPE_DEFAULTS declares; the API returns the defaults merged with what is set. */
export type ConnectorScope = Record<string, unknown> & { name_exclusions?: string[] };
/** A grant a source uses: a Graph permission, an Azure role, a Dataverse privilege, the Power Platform
 *  management app registration, a Halo scope, and an Okta scope or admin role, or a Google scope (through
 *  domain-wide delegation) or admin privilege. `role_id` is empty until confirmed on Microsoft Learn. */
export type SourcePermission = {
  name: string;
  kind: "graph" | "azure_rbac" | "dataverse_privilege" | "power_platform_registration" | "halo_scope" | "okta_scope" | "okta_admin_role" | "google_scope" | "google_admin_role";
  role_id: string; required: boolean; why: string; verified: boolean;
};
export type AccessStepState = "ok" | "failed" | "optional_missing" | "warning" | "not_used" | "not_checked" | "skipped";
export type AccessStep = {
  group: "credentials" | "api"; name: string; permission: string; required: boolean; state: AccessStepState;
  problem: string; problem_label: string; code: string; detail: string; next: string;
};
export type AccessCheck = { ok: boolean; missing: string[]; optional_missing: string[]; steps: AccessStep[]; checked_at: string; checked_by: UserRef | null };
export type ConnectionKind = "microsoft" | "halo" | "okta" | "google_workspace";
/** A connection's last test: when, passed or not, who, and what it found (never a secret). */
export type LastTest = ProbeResult & { at: string; by: UserRef | null };
/** The connection a connector signs in through, as the connector carries it (every role reads this much). `last_test`
 *  null means never tested; with `test_current` false and a passed last test, it changed since that test. */
export type ConnectionBrief = {
  id: string; kind: ConnectionKind; name: string; enabled: boolean; revision: number; tested_revision: number | null; test_current: boolean;
  /** (absent from an older API). */
  last_test?: LastTest | null;
};
/** GET discovery/sources: what each source needs before a connector of it exists. */
export type DiscoverySourceInfo = {
  id: DiscoverySourceId; label: string; capability: string; available: boolean; connection_kind: ConnectionKind;
  permissions: SourcePermission[]; scope_defaults: ConnectorScope; filters: Record<string, string>;
};
export type Connector = {
  id: string; source: DiscoverySourceId; source_label: string; name: string; enabled: boolean; connection: ConnectionBrief | null;
  /** The organization has the source's capability; when false (another edition, or a preview source the installation
   *  keeps off) the connector is kept but never runs, and only turning it off is allowed. */
  available: boolean;
  tenant_id: string; client_id: string; simulated: boolean;
  scope: ConnectorScope; filters: Record<string, string>; schedule: "manual" | "daily"; reviewer: UserRef | null; secret_set_on: string | null;
  run_requested: boolean;
  /** The newest finished run; `active_run` is the queued or running one, if any. */
  last_run: ConnectorRun | null; active_run: ConnectorRun | null;
  candidates: { new: number; changed: number }; permissions: SourcePermission[]; access_check: AccessCheck | null;
};
/** A connection test's outcome: Softwarden's words, the provider's code, and what to do next. */
export type ProbeResult = { ok: boolean; problem: string; problem_label: string; code: string; detail: string; next: string };
/** A Microsoft or Halo connection (Admin only). The secret is write-only: only `secret_set_on`. */
export type Connection = {
  id: string; name: string; enabled: boolean; notes: string; client_id: string; simulated: boolean; secret_set_on: string | null;
  revision: number; tested_revision: number | null; test_current: boolean;
  last_test: LastTest | null;
  connectors: { id: string; name: string }[]; created_at: string; updated_at: string;
  /** Microsoft */ tenant_id?: string;
  /** Halo, Okta */ base_url?: string; scope?: string;
  /** Okta */ key_id?: string;
  /** Google Workspace: `client_id` is the service account's, read from its key file */ admin_email?: string; service_account_email?: string;
};
export type ObservationStatus = "new" | "changed" | "linked" | "dismissed" | "gone" | "out_of_scope";
export type RecordRef = { id: string; kind: RecordKind; name: string };
/** A record a candidate may be: a live record of a compatible kind with a similar name, `score` 0 to 1. */
export type SuggestedMatch = RecordRef & { score: number };
/** A client secret or certificate seen on the observed app. Never key material. */
export type ObservedCredential = {
  id: string; key_id: string; credential_type: "secret" | "certificate"; display_name: string; origin: string;
  expires_on: string | null; removed_at: string | null;
};
/** One record field the source says differently (reconciliation, "Source differs"). Applying one is an
 *  ordinary audited record edit; ignoring one keeps the record's value until the source changes again. */
export type SourceDifference = {
  field: string; label: string; curated: string; source: string; since: string;
  ignored: boolean; ignored_at: string | null; ignored_by: UserRef | null;
};
export type ObservationDecision = {
  decision: "linked" | "created" | "dismissed"; reason: string; decided_by: UserRef | null; decided_at: string; record: RecordRef | null;
  /** No longer seen in source: kept or archived once someone resolved it; "" otherwise. */
  gone_resolution: "" | "kept" | "archived"; gone_acknowledged_at: string | null; gone_acknowledged_by: UserRef | null; gone_note: string;
};
export type Observation = {
  id: string; connector: { id: string; name: string }; kind: "application" | "component" | "ai_agent" | string; external_id: string; name: string; app_id: string; description: string;
  homepage: string; publisher: string; enabled: boolean; ai_agent: boolean; first_seen_at: string; last_seen_at: string; last_changed_at: string;
  gone_at: string | null; status: ObservationStatus;
  decision: ObservationDecision | null;
  suggested_owners: { name: string; email: string; external_id?: string; resolved?: boolean; person: { id: string; name: string; active: boolean } | null }[];
  suggested_matches: SuggestedMatch[];
  /** (absent from an older API). */
  source?: DiscoverySourceId; component_type?: string; details?: Record<string, unknown>; agent?: "" | "copilot_studio" | "agent_identity";
  registration?: boolean; enterprise_app?: boolean; credentials?: ObservedCredential[];
  /** The connector filter that leaves it out ("" when in scope), that filter's words, and since when. */
  out_of_scope?: string; out_of_scope_label?: string; out_of_scope_at?: string | null;
  differences?: SourceDifference[]; missing?: boolean;
  /** The assistant's newest suggestion (capability `assistant`); null without one or without the capability. */
  assistant_suggestion?: AssistantSuggestion | null;
};
export type CandidateCount = ObservationStatus | "candidates" | "missing" | "differs";
/** GET discovery/candidates: the generated type now that the schema describes it, with the
 *  rows as this file's Observation. The schema's row is the same object, but its decision's record is the shared
 *  RecordBrief, whose kind admits "integration", which discovery never links; the screens key record pages on
 *  RecordKind. */
export type CandidatePage = Omit<components["schemas"]["CandidatePage"], "results"> & { results: Observation[] };
export type DiscoverySource = {
  observation: string; connector: { id: string; name: string; source_label: string }; external_id: string; app_id: string;
  source_values: { name: string; description: string; homepage: string }; differs: string[]; enabled: boolean; last_seen_at: string;
  gone_at: string | null; decision: "linked" | "created"; decided_by: UserRef | null; decided_at: string;
  /** Every difference, ignored ones marked (absent from an older API). */
  differences?: SourceDifference[];
};
/** GET discovery/observations/{id}/drift: the record's differences from one source and the record's ETag for If-Match. */
export type Drift = {
  observation: string; name: string; connector: { id: string; name: string }; record: RecordRef; record_etag: string; differences: SourceDifference[];
};
/** "2026-11-03T14:02:11Z" as "2026-11-03 14:02", in the browser's time zone. */
export const when = (iso: string | null) => {
  if (!iso) return "—";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

// ---- catalog choices -------------------------------------------------------------------
// Values and labels from the API's TextChoices (catalog/models.py). Blank means "not recorded" everywhere.

export type Choice = { value: string; label: string };
const choices = (pairs: [string, string][]): Choice[] => pairs.map(([value, label]) => ({ value, label }));
export const DEPLOYMENT_MODELS = choices([["saas", "SaaS"], ["on_prem", "On-premises"], ["hybrid", "Hybrid"], ["desktop", "Desktop"]]);
export const AUTH_METHODS = choices([["saml", "SAML"], ["oidc", "OpenID Connect"], ["oauth", "OAuth"], ["ldap", "LDAP or Kerberos"], ["local", "Local accounts"], ["none", "No sign-in"], ["other", "Other"]]);
export const HOSTINGS = choices([["azure", "Azure"], ["on_prem", "On-premises"], ["power_platform", "Power Platform"], ["other", "Other"]]);
export const ENVIRONMENTS = choices([["dev", "Development"], ["test", "Test"], ["prod", "Production"]]);
export type DataCategory = components["schemas"]["DataCategoryEnum"];
/** An integration's data categories, in the API's order; personal, special-category and health make it
 *  carry personal data. Labels mirror catalog.models.DataCategory. */
export const DATA_CATEGORIES: { value: DataCategory; label: string }[] = [
  { value: "personal", label: "Personal data" }, { value: "special_category", label: "Special-category personal data" },
  { value: "financial", label: "Financial" }, { value: "credentials", label: "Credentials or secrets" }, { value: "health", label: "Health" },
  { value: "customer_content", label: "Customer content" }, { value: "other", label: "Other" },
];
export const DATA_CLASSIFICATIONS = choices([["public", "Public"], ["internal", "Internal"], ["confidential", "Confidential"], ["restricted", "Restricted"]]);
export const AGENT_PLATFORMS = choices([["copilot_studio", "Copilot Studio"], ["claude", "Claude"], ["azure_openai", "Azure OpenAI"], ["openai", "OpenAI"], ["m365_copilot_extension", "Microsoft 365 Copilot extension"], ["custom", "Custom"], ["other", "Other"]]);
export const AGENT_CHANNELS = choices([["teams", "Teams"], ["m365_copilot", "Microsoft 365 Copilot"], ["web", "Web"], ["api", "API"], ["email", "Email"], ["other", "Other"]]);
export const AGENT_AUTH_MODES = choices([["user", "Signed-in user"], ["service_principal", "Service principal"], ["api_key", "API key"], ["managed_identity", "Managed identity"]]);
export const AGENT_RISKS = choices([["minimal", "Minimal"], ["limited", "Limited"], ["high", "High"], ["unacceptable", "Unacceptable"]]);
export const MODEL_MODALITIES = choices([["text", "Text"], ["multimodal", "Multimodal"], ["image", "Image"], ["audio", "Audio"], ["embedding", "Embedding"], ["other", "Other"]]);
export const VALIDATION_STATUSES = choices([["not_assessed", "Not assessed"], ["not_required", "Not required"], ["in_validation", "In validation"], ["validated", "Validated"], ["retired", "Retired"]]);
export const RISK_TIERS = choices([["low", "Low"], ["medium", "Medium"], ["high", "High"]]);
/** Integration fields (IntegrationDirection and IntegrationAuthMethod). */
export const INTEGRATION_DIRECTIONS = choices([["one_way", "One way (source to target)"], ["two_way", "Both ways"]]);
export const INTEGRATION_AUTH_METHODS = choices([
  ["oauth", "OAuth 2.0"], ["api_key", "API key"], ["basic", "Username and password"], ["certificate", "Certificate"],
  ["managed_identity", "Managed identity"], ["ssh_key", "SSH key"], ["none", "No authentication"], ["other", "Other"],
]);
/** Tag colours: hue names the API stores, mapped to `tag-*` tokens by TagChip. Blank is the neutral default. */
export const TAG_COLORS = ["gray", "blue", "teal", "green", "amber", "orange", "red", "violet", "magenta"] as const;
export type TagColor = (typeof TAG_COLORS)[number];

/** The label of a choice value: "" for blank, the value itself for one this build doesn't know. */
export const labelOf = (list: Choice[], value?: string | null) => (value ? (list.find((c) => c.value === value)?.label ?? value) : "");


// ---- AI providers and models, categories, vendors, teams, tags -------------------------------

export type AIProvider = { id: string; name: string; website: string; usage: number };
export type AIModel = { id: string; name: string; version: string; provider: NamedRef; modality: string; hosting_region: string; usage: number };
export type ModelInUse = Omit<AIModel, "usage"> & { agents: number; live_agents: number };
export type SoftwareCategory = { id: string; name: string; description: string; usage: number };
export type Vendor = {
  id: string; name: string; usage: number; website: string; support_url: string; support_email: string; support_phone: string;
  portal_url: string; account_manager: Person | null; notes: string;
  custom_field_data?: Record<string, unknown>;
};
export type TeamDetail = { id: string; name: string; usage: number; description: string; members: Person[] };
export type TagInfo = { id: string; name: string; count: number; color: string; description: string };
/** GET organization (any role reads it; Admins change it). */
export type Organization = {
  id: string; name: string; slug: string; edition: string; timezone: string; review_interval_days: number; coordinator: UserRef | null;
  renewal_lead_days: number; reminder_thresholds: number[]; digest_time: string; email: EmailStatus;
  compliance_profile_available: boolean; compliance_profile_enabled: boolean;
  /** Attachment limits: the organization's (each within the installation's), the space used, and the installation's. */
  attachments?: AttachmentLimits;
  /** Data-quality rules an Admin turned off, by id. */
  data_quality_disabled_rules?: string[];
};
export type AttachmentLimitValues = { max_size_mb: number; allowed_extensions: string[]; quota_mb: number | null };
/** `retention_days`: how long a removed file is kept before the nightly sweep deletes it (absent from an older API). */
export type AttachmentLimits = AttachmentLimitValues & { used_bytes: number; retention_days?: number; installation: AttachmentLimitValues };

/** A refused delete (409): the API names what uses the item in `detail` and counts it in `blocking_references`. */
export const isInUse = (e: unknown): e is Problem => e instanceof Problem && e.status === 409;

/** GET one record with its ETag, so an edit can send it back as If-Match (412 when someone changed it since). */
export function apiWithEtag<T>(path: string): Promise<{ data: T; etag: string | null }> {
  return gate.tracked(false, () => readWithEtag<T>(path));
}
async function readWithEtag<T>(path: string): Promise<{ data: T; etag: string | null }> {
  assertSafePath(path);
  const response = await fetch(`/api/v1/${path}`, { headers: { Accept: "application/json" }, credentials: "same-origin" });
  const data = await readBody(response);
  if (data === UNREADABLE) throw unreadable(response.status);
  if (!response.ok) throw problemFrom(response.status, data ?? {});
  return { data: (data ?? {}) as T, etag: response.headers.get("ETag") };
}
/** The problem type of the 412 an If-Match write gets when the record changed since it was read. */
export const PRECONDITION_FAILED = "/problems/precondition-failed";


// ---- attachments -----------------------------------------------------------------------

export type AttachmentKind = "file" | "link";
export type AttachmentCategory = "contract" | "invoice" | "sop_runbook" | "architecture" | "validation" | "vendor_docs" | "security_review" | "other";
/** catalog.models.AttachmentCategory, in the API's order. */
export const ATTACHMENT_CATEGORIES: { value: AttachmentCategory; label: string }[] = [
  { value: "contract", label: "Contract" }, { value: "invoice", label: "Invoice" }, { value: "sop_runbook", label: "SOP or runbook" },
  { value: "architecture", label: "Architecture" }, { value: "validation", label: "Validation" }, { value: "vendor_docs", label: "Vendor documentation" },
  { value: "security_review", label: "Security review" }, { value: "other", label: "Other" },
];
/** The record kinds and vendors that hold attachments. */
export type AttachmentParentType = "application" | "component" | "integration" | "ai_agent" | "vendor" | "contract";
export type Attachment = {
  id: string; kind: AttachmentKind; title: string; category: AttachmentCategory; url: string; filename: string; content_type: string;
  size: number | null; sha256: string; download_url: string | null;
  attached_to: { type: AttachmentParentType; id: string; name: string };
  uploaded_by: UserRef | null; created_at: string; updated_at: string;
};

// ---- global search ---------------------------------------------------------------------

export type SearchKind = "application" | "component" | "integration" | "ai_agent" | "vendor" | "person" | "attachment" | "contract";
export type SearchSegment = { text: string; match: boolean };
export type SearchResult = {
  id: string; kind: SearchKind; name: string; path: string; score: number; archived: boolean; lifecycle: string | null;
  snippet: { field: string; label: string; segments: SearchSegment[] } | null;
  parent: { kind: string; id: string; name: string; path: string } | null;
};
export type SearchGroup = { kind: SearchKind; count: number; results: SearchResult[] };
export type SearchResponse = { query: string; ignored: string[]; fuzzy: boolean; groups: SearchGroup[] };

// ---- data quality ----------------------------------------------------------------------

export type Severity = "high" | "medium" | "low";
export type QualityRuleRef = { id: string; label: string; severity: Severity };
export type RecordQuality = { score: number; failing: QualityRuleRef[]; excepted: QualityRuleRef[] };
export type QualityCounts = { records: number; passing: number; failing: number; excepted: number; score: number | null };
export type QualityRule = {
  id: string; label: string; description: string; kinds: CatalogKind[]; severity: Severity; exceptable: boolean;
  covered_by: string | null; status: "on" | "off" | "unavailable"; status_reason: string; spec: Record<string, unknown>;
  counts: QualityCounts | null;
};
export type QualitySummary = QualityCounts & {
  as_of: string; complete: number;
  by_kind: (QualityCounts & { kind: RecordKind; complete: number })[];
  by_rule: (QualityCounts & QualityRuleRef)[];
  rules: { on: number; off: number; unavailable: number };
};
export type QualityFailure = {
  id: string; kind: CatalogKind; name: string; lifecycle: string; criticality: string | null; it_steward: UserRef | null; quality: RecordQuality;
};
export type QualityException = {
  id: string; record: { id: string; kind: RecordKind; name: string }; rule: string; rule_label: string; reason: string;
  owner: Person & { title?: string; anonymized?: boolean }; expires_on: string; active: boolean; created_by: UserRef | null;
  created_at: string; updated_at: string;
};
export const SEVERITY_LABEL: Record<Severity, string> = { high: "High", medium: "Medium", low: "Low" };

// ---- Imports (money kinds) -----------------------------------------------------------------------------------------

/** What an import run loads (imports.models.KINDS): records, and with the `contracts` capability contracts and licence
 *  pools. A closed union: a kind added in the API must be added here, with its words in the imports page. */
export type ImportKind = "application" | "component" | "contract" | "license_pool";
export const IMPORT_KINDS: ImportKind[] = ["application", "component", "contract", "license_pool"];
/** Kinds that need the `contracts` capability. */
export const MONEY_IMPORT_KINDS: ImportKind[] = ["contract", "license_pool"];
export const IMPORT_KIND_PLURAL: Record<ImportKind, string> = {
  application: "Applications", component: "Components", contract: "Contracts", license_pool: "License pools",
};


// ---- the assistant (capability `assistant`) ----------------------------------------------------------------

export const ASSISTANT_CAPABILITY = "assistant";
export type SuggestionAction = "link" | "create" | "dismiss";
export type SuggestionOutcome = "pending" | "accepted" | "overridden" | "ignored" | "rejected" | "superseded";
/** A candidate's newest assistant suggestion. `reason` and `detail` are model-written text: shown as plain text only. */
export type AssistantSuggestion = {
  id: string; state: "ready" | "refused" | "failed"; action: "" | SuggestionAction; record: RecordRef | null;
  confidence: number | null; reason: string; detail: string; model: string;
  /** Made for the candidate as it is now; false: it changed since, so Accept and Reject are refused. */
  current: boolean;
  outcome: SuggestionOutcome; outcome_at: string | null; outcome_by: UserRef | null; via_accept: boolean;
  decided_action: "" | SuggestionAction; note: string; created_at: string;
};
export type AssistantMonth = {
  start: string; end: string; to_date_usd: string; cap_usd: string; remaining_usd: string; cap_reached: boolean; cap_reached_at: string | null;
};
export type AssistantSettings = {
  saved: boolean; enabled: boolean; api_key_set: boolean; api_key_set_on: string | null;
  api_key_source: "organization" | "installation" | "none"; installation_key_available: boolean; installation_cap_usd: string | null;
  model: string; model_priced: boolean; prices: { input: string; output: string; cache_read: string; cache_write: string } | null;
  monthly_cap_usd: string; month: AssistantMonth; key_refused_at: string | null; key_refused_detail: string; updated_at: string | null;
};
export type AssistantOutcome =
  | "pending" | "ok" | "refusal" | "max_tokens" | "invalid" | "rate_limited" | "key_refused" | "bad_request" | "api_error" | "connection_error" | "abandoned";
export type AssistantUsageRow = {
  id: string; created_at: string; model: string; input_tokens: number; output_tokens: number; cache_read_input_tokens: number;
  cache_creation_input_tokens: number; estimated_cost_usd: string; cost_usd: string; outcome: AssistantOutcome; outcome_label: string;
  candidate_count: number; batch: string | null;
};
export type AssistantUsage = { month: AssistantMonth; results: AssistantUsageRow[]; truncated: boolean };
export type AssistantBatchStatus = "queued" | "running" | "succeeded" | "stopped" | "failed";
export type AssistantBatch = {
  id: string; trigger: "after_run" | "manual" | "follow_up"; status: AssistantBatchStatus; connector: { id: string; name: string } | null;
  requested_by: UserRef | null; queued_at: string; started_at: string | null; finished_at: string | null;
  counts: Partial<Record<"candidates" | "unchanged" | "suggested" | "refused" | "failed" | "calls" | "remaining" | "skipped", number>>;
  stop_reason: string; error: string; job_id: string | null;
};
/** The 403 a route gives when the organization's edition lacks a capability. */
export const CAPABILITY_REQUIRED = "/problems/capability-required";
