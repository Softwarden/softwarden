/** Discovery building blocks shared by the Discovery page, the guided walkthrough, connector settings, connections and
 *  run history: the sources and what each needs, run and test states as chips (glyph plus words), the
 *  access check's steps, the permission list, and a run's counts with the skipped breakdown. */
import { Link } from "react-router-dom";
import {
  when, type AccessCheck, type AccessStep, type AccessStepState, type Connection, type ConnectionBrief, type ConnectionKind, type ConnectorRun,
  type DiscoverySourceId, type RunStatus, type SourcePermission,
} from "@/api";
import { TableRegion } from "@/components/app";
import { chip } from "@/components/sw";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

// ---- sources --------------------------------------------------------------------------------------------------------

/** An in-app page shows docs/PERMISSIONS.md; each source links to its section there. */
export const PERMISSIONS_DOC = "/discovery/permissions";
export const docLink = (anchor: string) => `${PERMISSIONS_DOC}#${anchor}`;
/** Why a discovery source or connection the organization lacks isn't available, never naming the capability: every
 *  edition has every source, so it was turned off for this organization. */
export const unavailableReason = (subject = "It") =>
  `${subject} is part of every edition, and has been turned off for this organization: ask the installation's operator to turn it back on.`;
/** The section anchor of a PERMISSIONS.md heading, as the reference page makes them. */
export const slug = (heading: string) => heading.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

export type SourceInfo = {
  id: DiscoverySourceId; label: string; capability: string; connection: ConnectionKind; summary: string;
  /** A preview feature (launch scope): listed only while the installation turns them on (`preview`). */
  preview?: true;
  /** What to prepare, in order, as on-screen instructions. */
  steps: React.ReactNode[];
  /** The last step where the server accepts the simulators (`auth/me.simulation_allowed`): how to try the source on
   *  synthetic data. Left out everywhere else. Use `stepsFor`. */
  simulated?: React.ReactNode;
  /** The PERMISSIONS.md section for this source. */
  doc: string;
};

const REGISTER = (
  <>In the Microsoft Entra admin center, open <strong>App registrations</strong> and choose <strong>New registration</strong>: accounts in this organizational directory only, no redirect URI. Use a registration for discovery only, not the one people sign in with.</>
);
const SECRET = (
  <>Under <strong>Certificates &amp; secrets</strong>, add a client secret and copy its <strong>Value</strong> (not the Secret ID). It is shown once; Softwarden stores it encrypted and never shows it again.</>
);
const IDS = <>From the registration's <strong>Overview</strong>, copy the <strong>Directory (tenant) ID</strong> and the <strong>Application (client) ID</strong>.</>;
const SIMULATED = <>Until you are ready to connect a real tenant, use the tenant ID <code>simulated</code> with any client secret: Softwarden then reads a synthetic tenant and never contacts Microsoft.</>;

export const SOURCES: SourceInfo[] = [
  {
    id: "entra", label: "Microsoft Entra ID", capability: "discovery.entra", connection: "microsoft",
    summary: "Enterprise applications and app registrations, with their owners and the end dates of their secrets and certificates.",
    doc: "discovery-source-entra",
    steps: [
      REGISTER,
      <>Under <strong>API permissions</strong>, add the Microsoft Graph <strong>application</strong> permission <code>Application.Read.All</code>. Optionally add <code>User.ReadBasic.All</code> (owners' names and emails) and <code>AgentIdentity.Read.All</code> (Entra Agent ID agent identities). Then choose <strong>Grant admin consent</strong>.</>,
      SECRET, IDS,
    ],
    simulated: SIMULATED,
  },
  {
    id: "entra_licenses", label: "Microsoft Entra licenses", capability: "discovery.entra", connection: "microsoft",
    summary: "The tenant's subscribed SKUs with purchased and assigned seats, for license pools.",
    doc: "discovery-source-entra-licenses",
    steps: [
      <>Use the app registration of the Entra connector, or create one as for Entra ID (the same connection can serve both).</>,
      <>Under <strong>API permissions</strong>, add the Microsoft Graph <strong>application</strong> permission <code>LicenseAssignment.Read.All</code> and choose <strong>Grant admin consent</strong>.</>,
      SECRET, IDS,
    ],
    simulated: SIMULATED,
  },
  {
    // Shadow IT (the overview is /discovery/shadow-it).
    id: "entra_shadow_it", label: "Microsoft Entra shadow IT", capability: "discovery.entra", connection: "microsoft",
    summary: "Third-party apps people sign in to or consented to that nobody registered, with usage counts and the scopes granted. Counts only: never who.",
    doc: "discovery-source-entra-shadow-it",
    steps: [
      <>Use the app registration of the Entra connector, or create one as for Entra ID (the same connection can serve both).</>,
      <>Under <strong>API permissions</strong>, add the Microsoft Graph <strong>application</strong> permissions <code>Directory.Read.All</code> (consent grants and enterprise apps) and <code>AuditLog.Read.All</code> (sign-in logs, which need a Microsoft Entra ID P1 or P2 license), then choose <strong>Grant admin consent</strong>. Without P1 or P2, turn off <strong>Read the sign-in logs</strong> in the Scope step and use consent grants only.</>,
      SECRET, IDS,
    ],
    simulated: SIMULATED,
  },
  {
    // A preview source until the license waste report has a screen.
    id: "entra_licence_usage", label: "Microsoft Entra license usage", capability: "discovery.entra", connection: "microsoft", preview: true,
    summary: "Which licenses each user holds and when they last signed in, for license waste. Stores no names or emails.",
    doc: "discovery-source-entra-licence-usage",
    steps: [
      <>Use the app registration of the Entra connector, or create one as for Entra ID (the same connection can serve both).</>,
      <>Under <strong>API permissions</strong>, add the Microsoft Graph <strong>application</strong> permission <code>User.Read.All</code> and, for the last sign-in, <code>AuditLog.Read.All</code> (the tenant also needs Microsoft Entra ID P1). Both are tenant-wide: a Privileged Role Administrator chooses <strong>Grant admin consent</strong>.</>,
      SECRET, IDS,
    ],
    simulated: SIMULATED,
  },
  {
    // SSO, SCIM and MFA coverage (the report is /discovery/sso-coverage).
    id: "entra_coverage", label: "Microsoft Entra SSO and MFA coverage", capability: "discovery.entra", connection: "microsoft",
    summary: "Whether each catalog application linked to an Entra app is behind single sign-on, has SCIM provisioning on, and needs MFA by conditional access.",
    doc: "discovery-source-entra-coverage",
    steps: [
      <>Use the app registration of the Entra connector (it already has <code>Application.Read.All</code>), or create one as for Entra ID with that permission.</>,
      <>Under <strong>API permissions</strong>, add the Microsoft Graph <strong>application</strong> permissions <code>Synchronization.Read.All</code> (provisioning jobs) and <code>Policy.Read.All</code> (conditional access policies, which need a Microsoft Entra ID P1 license), then choose <strong>Grant admin consent</strong>. Without P1, MFA shows as unknown; turn off <strong>Read conditional access policies</strong> in the Scope step.</>,
      SECRET, IDS,
    ],
    simulated: SIMULATED,
  },
  {
    id: "azure", label: "Azure", capability: "discovery.azure", connection: "microsoft",
    summary: "Logic Apps, Function Apps, App Services and Automation runbooks in your subscriptions.",
    doc: "discovery-source-azure",
    steps: [
      REGISTER,
      <>No Microsoft Graph permission is needed. Assign the Azure role <strong>Reader</strong> to the app on each subscription to read, or once on a management group above them: <code>az role assignment create --assignee &lt;client id&gt; --role Reader --scope /subscriptions/&lt;subscription id&gt;</code>. Role changes can take a few minutes to apply.</>,
      SECRET, IDS,
    ],
    simulated: SIMULATED,
  },
  {
    id: "powerplatform", label: "Power Platform", capability: "discovery.powerplatform", connection: "microsoft",
    summary: "Copilot Studio agents and, optionally, cloud flows in your Dataverse environments.",
    doc: "discovery-source-powerplatform",
    steps: [
      REGISTER,
      <>In each environment to read: Power Platform admin center, <strong>Environment</strong>, <strong>Settings</strong>, <strong>Users + permissions</strong>, <strong>Application users</strong>, <strong>New app user</strong>. Choose the app, the business unit and a read-only security role with read on <strong>Agent</strong> (bot) and, for cloud flows, <strong>Process</strong> (workflow). Avoid System Administrator.</>,
      <>Copy each environment's URL (<code>https://&lt;name&gt;.crm.dynamics.com</code>) from the environment's page. "All environments" is optional and needs the app registered for the Power Platform admin API, which Microsoft treats like a Power Platform Administrator.</>,
      SECRET, IDS,
    ],
    simulated: SIMULATED,
  },
  {
    id: "halo", label: "HaloITSM", capability: "discovery.halo", connection: "halo",
    summary: "Software assets from HaloITSM, with their Halo asset IDs.",
    doc: "discovery-source-halo",
    steps: [
      <>In HaloITSM (<strong>Configuration</strong>, <strong>Integrations</strong>, <strong>Halo API</strong>), create an API application that signs in with a client ID and secret (client credentials) and may read assets. The connection asks for the scope <code>read:assets</code>, the default and all this source reads.</>,
      <>Copy the application's client ID and secret, and your Halo address, such as <code>https://yourcompany.haloitsm.com</code>.</>,
    ],
    simulated: <>Before going live, use the address <code>https://halo.simulated.invalid</code> with any client ID and secret: Softwarden then reads a synthetic Halo and contacts nothing.</>,
  },
  {
    id: "okta", label: "Okta", capability: "discovery.okta", connection: "okta", preview: true,
    summary: "The applications your organization runs through Okta, with their sign-on mode and how many people are assigned.",
    doc: "discovery-source-okta",
    steps: [
      <>In the Okta Admin Console, open <strong>Applications</strong>, <strong>Create App Integration</strong>, and choose <strong>API Services</strong>. Under <strong>Client Credentials</strong>, choose <strong>Public key / Private key</strong> and add a key: Okta generates the pair; copy the private key (PEM or JWK) and its key ID. Leave <strong>Require Demonstrating Proof of Possession (DPoP)</strong> off: Softwarden doesn't send DPoP proofs.</>,
      <>On the app's <strong>Okta API Scopes</strong> tab, grant <code>okta.apps.read</code> only. On its <strong>Admin roles</strong> tab, assign <strong>Read-only Administrator</strong>: a scope works only within the app's admin role.</>,
      <>Copy the app's client ID and your Okta org address, such as <code>https://yourcompany.okta.com</code> (not the <code>-admin</code> address). The private key is stored encrypted and never shown again; only a short-lived signed assertion leaves Softwarden.</>,
    ],
    simulated: <>Before going live, use the address <code>https://okta.simulated.invalid</code> with any client ID and a private key you generate yourself: Softwarden then reads a synthetic Okta and contacts nothing.</>,
  },
  {
    id: "google_workspace", label: "Google Workspace", capability: "discovery.google", connection: "google_workspace", preview: true,
    summary: "Third-party apps people granted access to their Google Workspace data, with how many people and which scopes (never who).",
    doc: "discovery-source-google-workspace",
    steps: [
      <>In Google Cloud, create a project (or use one for Softwarden only), enable the <strong>Admin SDK API</strong>, create a <strong>service account</strong> and add a <strong>JSON key</strong>. Keep the key file: you paste it into the connection, and only its private key and key ID are stored, encrypted.</>,
      <><strong>Domain-wide delegation is a tenant-wide grant only a super administrator can make.</strong> In the Google Admin console, open <strong>Security</strong>, <strong>Access and data control</strong>, <strong>API controls</strong>, <strong>Domain-wide delegation</strong>, add the service account's numeric client ID (shown on the connection) with exactly one scope: <code>https://www.googleapis.com/auth/admin.reports.audit.readonly</code>. It can take from minutes to a day to apply.</>,
      <>Choose the administrator the service account acts as: a super administrator, or an admin role with the <strong>Reports</strong> privilege. Softwarden reads the OAuth token audit log only and keeps counts per app, never who.</>,
    ],
    simulated: <>Before going live, use an administrator address in <code>google.simulated.invalid</code> (such as <code>admin@google.simulated.invalid</code>) with any service account key file: Softwarden then reads a synthetic Workspace and contacts nothing.</>,
  },
];
export const sourceInfo = (id: string) => SOURCES.find((s) => s.id === id);
/** A source's steps, with the simulator step only where this server accepts the simulators. */
export const stepsFor = (info: SourceInfo, simulationAllowed: boolean) =>
  simulationAllowed && info.simulated ? [...info.steps, info.simulated] : info.steps;

/** Preview features (launch scope; docs/CAPABILITIES.md): the API gives every organization the `preview`
 *  capability only while the installation turns them on (SOFTWARDEN_PREVIEW_FEATURES). Without it they aren't offered
 *  at all, rather than shown as needing another edition. */
export const PREVIEW = "preview";
const PREVIEW_KINDS: ConnectionKind[] = ["okta", "google_workspace"];
const CONNECTION_KINDS: ConnectionKind[] = ["microsoft", "halo", ...PREVIEW_KINDS];
/** The sources this installation offers, in order (an edition may still lack one: `capability`). */
export const sourcesFor = (capabilities: string[]) => SOURCES.filter((s) => !s.preview || capabilities.includes(PREVIEW));
/** The connection kinds this installation offers, in order. */
export const connectionKindsFor = (capabilities: string[]) =>
  CONNECTION_KINDS.filter((k) => !PREVIEW_KINDS.includes(k) || capabilities.includes(PREVIEW));
export const CONNECTION_LABEL: Record<ConnectionKind, string> = {
  microsoft: "Microsoft connection", halo: "Halo connection", okta: "Okta connection", google_workspace: "Google Workspace connection",
};
/** The words each connection kind uses on the Connections page, the connection form and the walkthrough. */
export const CONNECTION_TEXT: Record<ConnectionKind, {
  tab: string; heading: string; about: string; empty: string; provider: string; secret: string; secretHelp: string; multiline: boolean;
  /** Appended to `about` only where the server accepts the simulators (`aboutFor`). */
  simulated: string;
}> = {
  microsoft: {
    tab: "Microsoft", heading: "Microsoft connections", provider: "Microsoft",
    about: "An Entra app registration with a client secret, used by the Entra, Entra licenses, Azure and Power Platform sources.",
    simulated: "Tenant simulated reads the synthetic tenant.",
    empty: "Add the tenant ID, the application (client) ID and a client secret of an app registration, then test it.",
    secret: "client secret", secretHelp: "The secret's Value, not its Secret ID. Stored encrypted and never shown again.", multiline: false,
  },
  halo: {
    tab: "HaloITSM", heading: "Halo connections", provider: "Halo",
    about: "A HaloITSM API application with a client ID and secret.",
    simulated: "The address https://halo.simulated.invalid reads the synthetic Halo.",
    empty: "Add the Halo address, client ID and secret of an API application, then test it.",
    secret: "client secret", secretHelp: "Stored encrypted and never shown again.", multiline: false,
  },
  okta: {
    tab: "Okta", heading: "Okta connections", provider: "Okta",
    about: "An Okta API service app with a private key and the read-only scope okta.apps.read.",
    simulated: "The address https://okta.simulated.invalid reads the synthetic Okta.",
    empty: "Add the Okta address, the service app's client ID and its private key, then test it.",
    secret: "private key", secretHelp: "The service app's private key, PEM or JWK. Stored encrypted and never shown again; only a short-lived signed assertion is sent to Okta.", multiline: true,
  },
  google_workspace: {
    tab: "Google Workspace", heading: "Google Workspace connections", provider: "Google",
    about: "A service account with domain-wide delegation, acting as a Workspace administrator, with the read-only Reports scope.",
    simulated: "An administrator in google.simulated.invalid reads the synthetic Workspace.",
    empty: "Add the administrator's address and the service account's key file, then test it.",
    secret: "service account key file", secretHelp: "Paste the whole JSON key file. Only its private key and key ID are stored, encrypted, and never shown again; its client email and client ID are shown.", multiline: true,
  },
};
/** A connection kind's description, naming its simulator only where this server accepts the simulators. */
export const aboutFor = (kind: ConnectionKind, simulationAllowed: boolean) =>
  simulationAllowed ? `${CONNECTION_TEXT[kind].about} ${CONNECTION_TEXT[kind].simulated}` : CONNECTION_TEXT[kind].about;
/** The Connections page, on the tab of this kind. */
export const connectionsLink = (kind: ConnectionKind) => `/discovery/connections${kind === "microsoft" ? "" : `?tab=${kind}`}`;

// ---- states ---------------------------------------------------------------------------------------------------------

type Tone = "confirmed" | "overdue" | "pending" | "unreviewed" | "neutral";
const RUN: Record<RunStatus, { tone: Tone; glyph: string; label: string }> = {
  queued: { tone: "neutral", glyph: "◔", label: "Queued" },
  running: { tone: "pending", glyph: "▸", label: "Running" },
  succeeded: { tone: "confirmed", glyph: "✓", label: "Succeeded" },
  failed: { tone: "overdue", glyph: "!", label: "Failed" },
};
export const RUN_STATUSES = Object.keys(RUN) as RunStatus[];
export const runStatusLabel = (s: RunStatus) => RUN[s].label;
export const TRIGGER_LABEL = { manual: "Run now", scheduled: "Daily schedule" } as const;

export function RunStatusChip({ status }: { status: RunStatus }) {
  const s = RUN[status];
  return <span className={chip({ state: s.tone })}><span aria-hidden="true">{s.glyph}</span>{s.label}</span>;
}

/** A connection's test state, glyph plus words. `test_current` false always says to test again before a run. */
export function testState(c: Pick<Connection, "test_current" | "tested_revision"> & { last_test?: Connection["last_test"] }) {
  if (c.test_current) return { tone: "confirmed" as Tone, glyph: "✓", label: "Tested", note: "" };
  if (c.last_test === null) return { tone: "unreviewed" as Tone, glyph: "○", label: "Not tested", note: "Test it before discovery runs." };
  if (c.last_test && !c.last_test.ok) return { tone: "overdue" as Tone, glyph: "!", label: "Last test failed", note: "Fix it and test again before discovery runs." };
  return { tone: "pending" as Tone, glyph: "◐", label: "Changed since last test", note: "Test again before discovery runs." };
}

export function TestStateChip({ c }: { c: Parameters<typeof testState>[0] }) {
  const s = testState(c);
  return <span className={chip({ state: s.tone })} data-testid="test-state"><span aria-hidden="true">{s.glyph}</span>{s.label}</span>;
}

/** A connector's connection's test state from its brief: not tested, last test failed, changed since its last test, or
 *  tested (an older API without `last_test` can't tell the first three apart). */
export function briefState(c: ConnectionBrief) {
  if (c.last_test === undefined && !c.test_current) return { tone: "pending" as Tone, glyph: "◐", label: "Changed since last test, or not tested", note: "Test it again before discovery runs." };
  return testState({ ...c, last_test: c.last_test ?? null });
}

/** Why a connector won't run on its connection, in one sentence. */
export function untestedText(c: ConnectionBrief) {
  if (c.last_test === null) return `The connection ${c.name} hasn't been tested yet: test it before discovery runs.`;
  if (c.last_test && !c.last_test.ok) return `The connection ${c.name} failed its last test: fix it and test it again before discovery runs.`;
  if (c.last_test) return `The connection ${c.name} changed since its last test: test it again before discovery runs.`;
  return `The connection ${c.name} changed since its last test, or hasn't passed one: test it again before discovery runs.`;
}

/** A connector's connection as every role sees it (the brief: name, on or off, its test state). */
export function ConnectionLine({ c, admin }: { c: ConnectionBrief | null; admin: boolean }) {
  if (!c) return <span className={chip({ state: "gap" })}><span aria-hidden="true">⚠</span>No connection</span>;
  const state = briefState(c);
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      {admin ? <Link className="underline" to={connectionsLink(c.kind)}>{c.name}</Link> : <span>{c.name}</span>}
      <span className={chip({ state: state.tone })}><span aria-hidden="true">{state.glyph}</span>{state.label}</span>
      {!c.enabled && <span className={chip({ state: "neutral" })}><span aria-hidden="true">–</span>Turned off</span>}
    </span>
  );
}

/** A connection test's outcome in plain words with what to do next. Render inside a status region. */
export function ProbeResultNote({ name, result }: { name: string; result: { ok: boolean; problem_label: string; code: string; detail: string; next: string } }) {
  return (
    <div className={`rounded-md border px-3 py-2 ${result.ok ? "border-success-border bg-success-bg text-success-foreground" : "border-l-4 border-error-border bg-error-bg"}`} data-testid="probe-result">
      <p className="font-semibold">
        <span aria-hidden="true">{result.ok ? "✓ " : "! "}</span>
        {result.ok ? `${name}: connection works.` : `${name}: ${result.problem_label || "test failed"}.`}
        {result.code && <span className="font-normal"> (code {result.code})</span>}
      </p>
      {result.detail && <p>{result.detail}</p>}
      {result.next && <p><span className="font-semibold">Next: </span>{result.next}</p>}
    </div>
  );
}

const STEP: Record<AccessStepState, { tone: Tone; glyph: string; label: string }> = {
  ok: { tone: "confirmed", glyph: "✓", label: "Works" },
  failed: { tone: "overdue", glyph: "!", label: "Failed" },
  optional_missing: { tone: "unreviewed", glyph: "○", label: "Optional, not granted" },
  warning: { tone: "pending", glyph: "△", label: "Warning" },
  not_used: { tone: "neutral", glyph: "–", label: "Not used by this scope" },
  not_checked: { tone: "neutral", glyph: "…", label: "Not checked" },
  skipped: { tone: "neutral", glyph: "–", label: "Skipped" },
};

export function AccessStepChip({ state }: { state: AccessStepState }) {
  const s = STEP[state];
  return <span className={chip({ state: s.tone })}><span aria-hidden="true">{s.glyph}</span>{s.label}</span>;
}

/** The access check's outcome: a summary (what is missing, by name), then each step with its state, the permission it
 *  proves and, for anything not working, what was found and the fix. Optional and warning steps never block. */
export function AccessCheckResult({ check }: { check: AccessCheck }) {
  const groups: { id: AccessStep["group"]; title: string }[] = [{ id: "credentials", title: "Sign-in" }, { id: "api", title: "Reads" }];
  return (
    <div data-testid="access-check">
      <div className={`mb-3 rounded-md border px-3 py-2 ${check.ok ? "border-success-border bg-success-bg text-success-foreground" : "border-l-4 border-error-border bg-error-bg"}`}>
        <p className="font-semibold">
          <span aria-hidden="true">{check.ok ? "✓ " : "! "}</span>
          {check.ok ? "Every required permission works." : check.missing.length ? `Missing: ${check.missing.join(", ")}.` : "The check didn't pass. See the failed step below."}
        </p>
        {check.optional_missing.length > 0 && <p>Optional, not granted (doesn't block runs): {check.optional_missing.join(", ")}.</p>}
        <p className="text-sm">Checked {when(check.checked_at)}{check.checked_by ? ` by ${check.checked_by.name}` : ""}.</p>
      </div>
      {groups.map((g) => {
        const steps = check.steps.filter((s) => s.group === g.id);
        if (!steps.length) return null;
        return (
          <section key={g.id} aria-labelledby={`check-${g.id}`} className="mb-3">
            <h3 id={`check-${g.id}`} className="mb-1.5 font-semibold">{g.title}</h3>
            <ol className="m-0 flex list-none flex-col gap-2 p-0">
              {steps.map((s, i) => (
                <li key={`${s.name}-${i}`} className={`rounded-md border bg-card px-3 py-2 ${s.state === "failed" ? "border-l-4 border-error-border" : s.state === "warning" ? "border-l-4 border-state-pending-border" : ""}`} data-state={s.state}>
                  <div className="flex flex-wrap items-center gap-2">
                    <AccessStepChip state={s.state} />
                    <span className="font-semibold">{s.name}</span>
                    {s.permission && <span className="text-sm text-muted-foreground">{s.permission}{s.required ? "" : " (optional)"}</span>}
                  </div>
                  {s.state === "failed" && s.permission && <p className="mt-1"><span className="font-semibold">Missing permission: </span><code>{s.permission}</code>{s.problem_label && s.problem !== "missing_permission" ? ` (${s.problem_label})` : ""}</p>}
                  {s.state === "failed" && !s.permission && s.problem_label && <p className="mt-1 font-semibold">{s.problem_label}</p>}
                  {s.detail && s.state !== "ok" && <p className="mt-1 text-sm">{s.detail}{s.code ? ` (code ${s.code})` : ""}</p>}
                  {s.next && s.state !== "ok" && <p className="mt-1 text-sm"><span className="font-semibold">Fix: </span>{s.next}</p>}
                </li>
              ))}
            </ol>
          </section>
        );
      })}
    </div>
  );
}

const KIND_TEXT: Record<SourcePermission["kind"], string> = {
  graph: "Microsoft Graph application permission", azure_rbac: "Azure role", dataverse_privilege: "Dataverse privilege",
  power_platform_registration: "Power Platform admin API registration", halo_scope: "Halo scope",
  okta_scope: "Okta scope", okta_admin_role: "Okta admin role", google_scope: "Google scope (domain-wide delegation)",
  google_admin_role: "Google admin privilege",
};
/** Role IDs exist only for Graph permissions and Azure roles; elsewhere there is none to show. */
const HAS_ROLE_ID: SourcePermission["kind"][] = ["graph", "azure_rbac"];

/** The grants a source uses, from the connector's `permissions`: name, why, required or optional, whether it was
 *  confirmed on Microsoft Learn, and its role ID when known. */
export function PermissionsTable({ permissions, caption }: { permissions: SourcePermission[]; caption: string }) {
  return (
    <TableRegion label="Permissions table">
      <Table>
        <TableCaption className="sr-only">{caption}</TableCaption>
        <TableHeader><TableRow>
          <TableHead scope="col">Permission</TableHead><TableHead scope="col">Needed</TableHead><TableHead scope="col">Why</TableHead>
          <TableHead scope="col">Microsoft Learn</TableHead><TableHead scope="col">Role ID</TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {permissions.map((p) => (
            <TableRow key={p.name} className="align-top">
              <TableHead scope="row" className="whitespace-normal font-semibold text-foreground"><code>{p.name}</code><span className="block text-sm font-normal text-muted-foreground">{KIND_TEXT[p.kind]}</span></TableHead>
              <TableCell>{p.required ? <span className={chip({ state: "neutral" })}><span aria-hidden="true">●</span>Required</span> : <span className={chip({ state: "unreviewed" })}><span aria-hidden="true">○</span>Optional</span>}</TableCell>
              <TableCell className="min-w-56 whitespace-normal">{p.why}</TableCell>
              <TableCell className="whitespace-normal">{p.verified ? <><span aria-hidden="true">✓ </span>Verified</> : <><span aria-hidden="true">? </span>Not verified yet</>}</TableCell>
              <TableCell className="whitespace-normal">{p.role_id ? <code className="text-xs [overflow-wrap:anywhere]">{p.role_id}</code> : HAS_ROLE_ID.includes(p.kind) ? <span className="text-muted-foreground">Not confirmed yet</span> : "—"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableRegion>
  );
}

// ---- runs -----------------------------------------------------------------------------------------------------------

const n = (v: unknown) => (typeof v === "number" ? v : 0);
const plural = (k: number, one: string, many = `${one}s`) => `${k} ${k === 1 ? one : many}`;

export function durationText(seconds: number | null) {
  if (seconds === null) return "—";
  if (seconds < 60) return `${seconds} s`;
  const m = Math.floor(seconds / 60);
  return `${m} min ${seconds % 60} s`;
}

/** A run in one sentence, as the Discovery page lists connectors. */
export function runSentence(run: ConnectorRun) {
  if (run.status === "queued") return `Run queued ${when(run.queued_at)}; the worker starts it within a minute.`;
  if (run.status === "running") return `Running since ${when(run.started_at)}.`;
  if (run.status === "failed") return `Last run ${when(run.finished_at ?? run.started_at ?? run.queued_at)} failed: ${run.error}`;
  const c = run.counts;
  const skipped = run.skipped.map((s) => `${s.count} ${s.label}`).join(", ");
  return `Last run ${when(run.finished_at)}: ${n(c.seen)} seen, ${n(c.kept)} kept${skipped ? ` (skipped: ${skipped})` : ""}; ${n(c.new)} new, ${n(c.changed)} changed, ${n(c.gone)} no longer in source.`;
}

/** A finished run's numbers: the counts, the skipped breakdown (by filter, in words), notes and the error. */
export function RunResult({ run }: { run: ConnectorRun }) {
  const c = run.counts;
  const rows: [string, unknown][] = [
    ["Seen in the source", c.seen], ["Kept (in scope)", c.kept], ["New candidates", c.new], ["Changed", c.changed],
    ["Unchanged", c.unchanged], ["No longer in source", c.gone], ["Out of scope", c.out_of_scope], ["Couldn't be read", c.not_read],
  ];
  const shown = rows.filter(([, v]) => typeof v === "number");
  return (
    <div className="flex flex-col gap-4" data-testid="run-result">
      {run.status === "failed" && (
        <p className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">
          <span className="font-semibold"><span aria-hidden="true">! </span>The run failed. </span>{run.error || "No reason was recorded."}
        </p>
      )}
      {shown.length > 0 && (
        <TableRegion label="Run counts table">
          <Table>
            <TableCaption className="sr-only">What the run counted</TableCaption>
            <TableHeader><TableRow><TableHead scope="col">Count</TableHead><TableHead scope="col" className="text-right">Number</TableHead></TableRow></TableHeader>
            <TableBody>
              {shown.map(([label, v]) => (
                <TableRow key={label}><TableHead scope="row" className="font-normal text-foreground">{label}</TableHead><TableCell className="tabular text-right">{n(v)}</TableCell></TableRow>
              ))}
            </TableBody>
          </Table>
        </TableRegion>
      )}
      <section aria-labelledby={`skipped-${run.id}`}>
        <h3 id={`skipped-${run.id}`} className="mb-1.5 font-semibold">Skipped by the scope</h3>
        {run.skipped.length === 0 ? <p className="text-muted-foreground">Nothing was skipped.</p> : (
          <TableRegion label="Skipped table">
            <Table>
              <TableCaption className="sr-only">Objects the scope skipped, by filter, largest first. Skipped objects are counted, not stored.</TableCaption>
              <TableHeader><TableRow><TableHead scope="col">Skipped because</TableHead><TableHead scope="col" className="text-right">Objects</TableHead></TableRow></TableHeader>
              <TableBody>
                {run.skipped.map((s) => (
                  <TableRow key={s.filter}><TableHead scope="row" className="font-normal text-foreground">{s.label}</TableHead><TableCell className="tabular text-right">{s.count}</TableCell></TableRow>
                ))}
              </TableBody>
            </Table>
          </TableRegion>
        )}
      </section>
      {run.notes.length > 0 && (
        <section aria-labelledby={`notes-${run.id}`}>
          <h3 id={`notes-${run.id}`} className="mb-1.5 font-semibold">Notes</h3>
          <ul className="ml-5 list-disc">{run.notes.map((t, i) => <li key={i}>{t}</li>)}</ul>
        </section>
      )}
    </div>
  );
}

/** "12 seen, 9 kept" for a table cell. */
export const seenKept = (run: ConnectorRun) => (run.status === "succeeded" || typeof run.counts.seen === "number" ? `${n(run.counts.seen)} seen, ${n(run.counts.kept)} kept` : "—");
export const changesText = (run: ConnectorRun) => (run.status === "succeeded" ? `${n(run.counts.new)} new, ${n(run.counts.changed)} changed, ${n(run.counts.gone)} gone` : "—");
export const skippedText = (run: ConnectorRun) => (run.skipped.length ? run.skipped.map((s) => `${s.count} ${s.label}`).join(", ") : "None");
export { plural };
