/** Administration › Single sign-on. One settings row
 *  per organization: the active configuration that members sign in with, and a draft an Admin edits, tests with a real
 *  sign-in of their own account and then activates. Saving the draft bumps its revision, which invalidates its test, so
 *  a broken draft can never be activated: Activate stays disabled, with the reason in words, until the current revision
 *  passed. The sign-in domains apply at once and sit outside the draft. The client secret is write-only: the page says
 *  since when one is stored, never what it is.
 *
 *  The test sign-in leaves the page (the provider's sign-in, then the API's callback) and comes back to
 *  `/admin/sign-in?sso_test=passed`, or `?sso_test=failed&reason=<code>`; the page says which, then takes the query out
 *  of the address. Admin only (others see AdminOnly), capability `sso.oidc`. Every call is typed from the schema
 *  (src/lib/openapi.ts). Loaded on first use.
 *
 *  The `saml` preset needs capability `sso.saml`, which this release doesn't offer: the SAML card isn't shown, or, for
 *  settings saved with it, is shown disabled with "Not available in this release." */
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { PRECONDITION_FAILED, Problem, when } from "@/api";
import { AdminOnly, ErrorBanner, ErrorSummary, Field, Loading, PageHead, useReturnFocus } from "@/components/app";
import { chip, CHIP_WRAP, DetailList, DetailRow, GapChip } from "@/components/sw";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { typedApi, typedGetWithEtag, type BodyOf, type Schemas } from "@/lib/openapi";
import { destinationMoved } from "@/lib/destination";
import { apiKey, problemText } from "@/lib/query";
import { useMe } from "@/lib/session";

type Settings = Schemas["SsoSettings"];
type Draft = Schemas["SsoDraft"];
type Provider = Schemas["ProviderEnum"];

const SSO_CAPABILITY = "sso.oidc";
const SAML_CAPABILITY = "sso.saml";
const DRAFT_CHANGED = "/problems/sso-draft-changed";
const TEST_REQUIRED = "/problems/sso-test-required";
const LOCAL_ONLY = "/problems/sso-local-only";

/** The presets, in the picker's order, with the provider's own name. */
const PROVIDERS: { value: Provider; label: string; hint: string }[] = [
  { value: "entra", label: "Microsoft Entra ID", hint: "Your directory's tenant ID fills in the rest." },
  { value: "okta", label: "Okta", hint: "Your Okta domain, and a custom authorization server if you use one." },
  { value: "google", label: "Google Workspace", hint: "Only accounts in the Workspace domains you list can sign in." },
  { value: "oidc", label: "Generic OpenID Connect", hint: "Any provider that publishes an OpenID Connect discovery document." },
  { value: "saml", label: "SAML 2.0", hint: "Any SAML 2.0 identity provider: import its metadata, or enter its entity ID, SSO URL and signing certificate." },
];
const providerName = (p: string) => PROVIDERS.find((x) => x.value === p)?.label ?? (p || "None");

/** A field the draft still needs, as a named gap (never "Missing"). */
const NEEDS: Record<string, string> = {
  provider: "No provider", tenant_id: "No tenant ID", okta_domain: "No Okta domain", hosted_domains: "No Workspace domain",
  discovery_url: "No discovery URL", client_id: "No client ID", client_secret: "No client secret",
  idp_entity_id: "No IdP entity ID", idp_sso_url: "No SSO URL", idp_certificates: "No signing certificate",
};

const WHAT = "the organization's single sign-on settings: the identity provider members sign in with, its client secret and the sign-in domains";

export function SsoSettingsPage() {
  return <AdminOnly title="Single sign-on" what={WHAT}><Gate /></AdminOnly>;
}

function Gate() {
  const me = useMe();
  if (!me.capabilities.includes(SSO_CAPABILITY)) {
    return (
      <>
        <PageHead title="Single sign-on" />
        <p className="max-w-prose rounded-md border border-dashed border-border-strong bg-muted px-3 py-2 text-sm" data-unavailable="">
          <span aria-hidden="true">⊘ </span><span className="font-semibold">Single sign-on isn't available.</span> This organization's edition doesn't include single sign-on. Members sign in with their passwords.
        </p>
      </>
    );
  }
  return <SsoSettingsScreen />;
}

/** The settings with their ETag, fetched afresh on every visit (never from the cache). */
function useSettings() {
  const q = useQuery({ queryKey: [...apiKey("sso/settings"), "etag"], queryFn: () => typedGetWithEtag("sso/settings"), gcTime: 0, staleTime: 0 });
  return q;
}

type Landing = { ok: boolean; reason: string } | null;

function SsoSettingsScreen() {
  const me = useMe();
  const q = useSettings();
  const [params, setParams] = useSearchParams();
  // The test sign-in's result, read from the address once and then taken out of it (a reload doesn't repeat it).
  const [landing] = useState<Landing>(() => {
    const result = params.get("sso_test");
    return result === "passed" || result === "failed" ? { ok: result === "passed", reason: params.get("reason") ?? "" } : null;
  });
  useEffect(() => {
    if (params.has("sso_test") || params.has("reason")) setParams({}, { replace: true });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const [notice, setNotice] = useState("");
  const [localOnly, setLocalOnly] = useState(false);
  const [disabling, setDisabling] = useState(false);
  // Kept here, not in DraftSection: a save before a test bumps the revision, which remounts that section.
  const [actionError, setActionError] = useState("");
  const [trouble, setTrouble] = useState<Trouble>(null);
  const reload = () => void q.refetch();

  const data = q.data?.data;
  const etag = q.data?.etag ?? null;
  return (
    <>
      <PageHead title="Single sign-on" desc={`How members of ${me.organization.name} sign in with their identity provider. Password sign-in is always kept.`} />
      <div role="status" data-testid="sso-status" className="empty:hidden">
        {notice ? <Note ok text={notice} /> : landing && data ? <LandingNote landing={landing} draft={data.draft} /> : null}
      </div>
      {(localOnly || data?.force_local_login) && <LocalOnlyBanner />}
      {q.isError && <ErrorBanner message={problemText(q.error, "Couldn't load the single sign-on settings.")} onRetry={reload} />}
      {!data && !q.isError && <Loading what="single sign-on settings" rows={3} />}
      {data && (
        <div className="grid max-w-4xl gap-5">
          <StatusCard settings={data} onDisable={() => setDisabling(true)} />
          <DraftSection key={data.draft.revision} settings={data} etag={etag} reload={reload} setNotice={setNotice} setLocalOnly={setLocalOnly}
            actionError={actionError} setActionError={setActionError} trouble={trouble} setTrouble={setTrouble} />
          <RedirectUri uri={data.redirect_uri} />
          <DomainsSection key={data.sign_in_domains.join(",")} settings={data} etag={etag} reload={reload} setNotice={setNotice} />
        </div>
      )}
      <DisableDialog open={disabling} org={me.organization.name} onClose={() => setDisabling(false)}
        onDone={(text) => { setDisabling(false); setNotice(text); reload(); }} />
    </>
  );
}

function Note({ ok, text }: { ok: boolean; text: string }) {
  return ok
    ? <p className="mb-4 rounded-md border border-success-border bg-success-bg px-3 py-2 text-success-foreground"><span aria-hidden="true">✓ </span>{text}</p>
    : <p className="mb-4 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2"><span aria-hidden="true">! </span>{text}</p>;
}

/** Codes the callback can send without recording them on the draft (core/oidc_views.py). */
const LANDING_REASONS: Record<string, string> = {
  test_session: "The test sign-in couldn't be matched to this browser's session: it took too long or was started elsewhere. Test again from this page.",
};

/** The callback records a failed test's detail as "Test sign-in failed: <why>"; the page says "failed" itself. */
function withoutPrefix(detail: string) {
  return detail.replace(/^Test sign-in failed: /, "");
}

/** What a save did, in words: with pasted metadata, only what the import filled in (a value typed in the same save wins
 *  over the import, and one the metadata repeats unchanged filled nothing). */
function savedNotice(saved: Draft, before: Draft, body: Record<string, unknown>) {
  const next = `Draft saved as revision ${saved.revision}. Test it before activating.`;
  if (!("idp_metadata_xml" in body)) return next;
  if (saved.revision === before.revision) return "The metadata matches the draft already; nothing changed.";
  const filled: string[] = [];
  if (!("idp_entity_id" in body) && saved.idp_entity_id !== before.idp_entity_id) filled.push(`entity ID ${saved.idp_entity_id}`);
  if (!("idp_sso_url" in body) && saved.idp_sso_url !== before.idp_sso_url) filled.push(`SSO URL ${saved.idp_sso_url}`);
  const prints = (d: Draft) => d.idp_certificates.map((c) => c.sha256).sort().join(",");
  if (!("idp_certificates" in body) && prints(saved) !== prints(before)) {
    const n = saved.idp_certificates.length;
    filled.push(`${n} signing ${n === 1 ? "certificate" : "certificates"}`);
  }
  if (filled.length === 0) return `The metadata matched the draft's values, so the import filled nothing. ${next}`;
  const list = filled.length === 1 ? filled[0] : `${filled.slice(0, -1).join(", ")} and ${filled[filled.length - 1]}`;
  return `Imported from the metadata: ${list}. ${next}`;
}

/** What the test sign-in that brought the browser back here found. */
function LandingNote({ landing, draft }: { landing: NonNullable<Landing>; draft: Draft }) {
  const t = draft.test;
  if (landing.ok) {
    const current = t.ok === true && t.revision === draft.revision;
    return <Note ok text={current ? `Test sign-in passed for revision ${t.revision}. You can activate this draft now.` : `Test sign-in passed for revision ${t.revision ?? "an earlier draft"}.`} />;
  }
  // Never the address's own words: a crafted link could put any text here. The recorded code, or a known landing code.
  const recorded = t.ok === false ? t.code : "";
  const code = recorded || (Object.hasOwn(LANDING_REASONS, landing.reason) ? landing.reason : "");
  const detail = recorded ? (t.detail ? ` ${withoutPrefix(t.detail)}` : "") : code ? ` ${LANDING_REASONS[code]}` : "";
  return <Note ok={false} text={`Test sign-in failed${code ? ` (${code})` : ""}.${detail} The draft can't be activated until a test passes.`} />;
}

function LocalOnlyBanner() {
  return (
    <div className="mb-4 max-w-4xl rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" role="note" data-testid="force-local">
      <p className="font-semibold"><span aria-hidden="true">◐ </span>Single sign-on is turned off on this installation.</p>
      <p className="mt-1">The server sets SOFTWARDEN_FORCE_LOCAL_LOGIN, so everyone signs in with a password and test sign-ins and activation are refused. You can still edit the draft. An operator removes the setting and restarts Softwarden to turn single sign-on back on.</p>
    </div>
  );
}

// ---- status -------------------------------------------------------------------------------------------------------

function SsoStatusChip({ status }: { status: Settings["status"] }) {
  if (status === "active") return <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Active</span>;
  if (status === "disabled") return <span className={chip({ state: "neutral" })}><span aria-hidden="true">⊘</span>Disabled</span>;
  return <span className={cn(chip({ state: "unreviewed" }), CHIP_WRAP)}><span aria-hidden="true">○</span>Legacy (not set up here)</span>;
}

function Section({ id, title, children, desc }: { id: string; title: string; desc?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section aria-labelledby={id} className="rounded-lg border bg-card px-4 py-4 sm:px-5">
      <h2 id={id} className="font-display text-section font-semibold">{title}</h2>
      {desc && <div className="mt-1 max-w-prose text-sm text-muted-foreground">{desc}</div>}
      <div className="mt-3">{children}</div>
    </section>
  );
}

function StatusCard({ settings, onDisable }: { settings: Settings; onDisable: () => void }) {
  const a = settings.active;
  return (
    <Section id="sso-current" title="Sign-in now">
      <DetailList className="rounded-md border px-3">
        <DetailRow label="Status"><span data-testid="sso-status-chip"><SsoStatusChip status={settings.status} /></span></DetailRow>
        <DetailRow label="How members sign in">{settings.effective.detail || "With their passwords."}</DetailRow>
        <DetailRow label="Active provider">
          {a?.provider ? <span data-testid="sso-active-provider">{providerName(a.provider)}</span> : <span className="text-muted-foreground">None activated</span>}
          {a?.provider === "saml"
            ? a.idp_entity_id && <span className="block text-sm text-muted-foreground [overflow-wrap:anywhere]">Entity ID {a.idp_entity_id}</span>
            : a?.issuer && <span className="block text-sm text-muted-foreground [overflow-wrap:anywhere]">Issuer {a.issuer}</span>}
        </DetailRow>
        <DetailRow label="Activated">
          {a?.activated_at
            ? <>{a.activated_by?.name ?? "Someone no longer here"} on <span className="tabular">{when(a.activated_at)}</span></>
            : <span className="text-muted-foreground">Never</span>}
        </DetailRow>
        <DetailRow label="Active revision">{a ? <span className="tabular">{a.revision}</span> : <span className="text-muted-foreground">None</span>}</DetailRow>
      </DetailList>
      {settings.status === "active" && (
        <div className="mt-3">
          <Button type="button" variant="outline" className="text-error-foreground" onClick={onDisable}>Turn off single sign-on…</Button>
        </div>
      )}
    </Section>
  );
}

function DisableDialog({ open, org, onClose, onDone }: { open: boolean; org: string; onClose: () => void; onDone: (text: string) => void }) {
  const returnFocus = useReturnFocus(open);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) setError(""); }, [open]);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await typedApi("post", "sso/settings/disable", {});
      onDone("Single sign-on is off. Members sign in with their passwords; the draft is kept.");
    } catch (err) {
      setError(problemText(err, "Couldn't turn single sign-on off."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg" {...returnFocus}>
        <DialogHeader>
          <DialogTitle>Turn off single sign-on?</DialogTitle>
          <DialogDescription>Members of {org} sign in with their passwords from now on. Anyone without a password (accounts approved from pending access) can't sign in until single sign-on is on again or an Admin resets their password.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate>
          {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{error}</p>}
          <p className="mb-4 text-sm">The draft and its test are kept: activate a tested draft to turn it on again.</p>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="destructive" disabled={busy}>{busy ? "Turning off…" : `Turn off single sign-on for ${org}`}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---- the draft, its test and activation ----------------------------------------------------------------------------

type Form = {
  provider: Provider | ""; tenant_id: string; okta_domain: string; okta_authorization_server: string; discovery_url: string;
  hosted_domains: string; client_id: string; client_secret: string;
};

const listOf = (text: string) => text.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);

function formOf(d: Draft): Form {
  return {
    provider: d.provider, tenant_id: d.tenant_id, okta_domain: d.okta_domain, okta_authorization_server: d.okta_authorization_server,
    discovery_url: d.provider === "oidc" ? d.discovery_url : "", hosted_domains: d.hosted_domains.join(", "), client_id: d.client_id, client_secret: "",
  };
}

/** Only what changed, so an untouched draft keeps its revision (and its test). */
function changes(form: Form, d: Draft, sendSecret = true): BodyOf<"sso/settings", "patch"> {
  const body: BodyOf<"sso/settings", "patch"> = {};
  const was = formOf(d);
  if (form.provider && form.provider !== d.provider) body.provider = form.provider;
  for (const k of ["tenant_id", "okta_domain", "okta_authorization_server", "client_id"] as const) {
    if (form[k].trim() !== was[k]) body[k] = form[k].trim();
  }
  if (form.provider === "oidc" && form.discovery_url.trim() !== was.discovery_url) body.discovery_url = form.discovery_url.trim();
  if (listOf(form.hosted_domains).join(",") !== listOf(was.hosted_domains).join(",")) body.hosted_domains = listOf(form.hosted_domains);
  if (sendSecret && form.client_secret) body.client_secret = form.client_secret;
  return body;
}

/** The draft matches the active configuration (same values, same stored secret). */
function draftIsActive(s: Settings) {
  const a = s.active, d = s.draft;
  if (s.status !== "active" || !a) return false;
  return a.provider === d.provider && a.tenant_id === d.tenant_id && a.okta_domain === d.okta_domain
    && a.okta_authorization_server === d.okta_authorization_server && a.discovery_url === d.discovery_url
    && a.hosted_domains.join(",") === d.hosted_domains.join(",") && a.client_id === d.client_id && a.secret_set_at === d.secret_set_at
    && a.idp_entity_id === d.idp_entity_id;
}

type Trouble = { kind: "stale" } | { kind: "changed"; revision: string } | null;

function DraftSection({ settings, etag, reload, setNotice, setLocalOnly, actionError, setActionError, trouble, setTrouble }: {
  settings: Settings; etag: string | null; reload: () => void; setNotice: (t: string) => void; setLocalOnly: (v: boolean) => void;
  actionError: string; setActionError: (t: string) => void; trouble: Trouble; setTrouble: (t: Trouble) => void;
}) {
  const d = settings.draft;
  const me = useMe();
  const samlAllowed = me.capabilities.includes(SAML_CAPABILITY);
  const [form, setForm] = useState<Form>(() => formOf(d));
  const [replacing, setReplacing] = useState(!d.secret_set);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState<"" | "save" | "test" | "activate">("");
  // A change that sends sign-in somewhere else (another provider, Okta domain or
  // discovery URL host) needs the client secret again in the same save, so its field opens, without "Keep".
  const moved = d.secret_set && !!form.provider && form.provider !== "saml" && (
    (!!d.provider && form.provider !== d.provider)
    || (form.provider === "okta" && form.okta_domain.trim().toLowerCase() !== d.okta_domain.toLowerCase())
    || (form.provider === "oidc" && destinationMoved(d.discovery_url, form.discovery_url)));
  const secretOpen = !d.secret_set || replacing || moved;
  // A secret typed while the field was open is sent only while it still shows (a domain changed and changed back hides
  // it again), as on the ticket connection form.
  const body = changes(form, d, secretOpen && !!form.provider && form.provider !== "saml");
  const dirty = Object.keys(body).length > 0;
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }));
  const local = settings.force_local_login;
  const loadLatest = () => { setTrouble(null); setActionError(""); reload(); };

  /** A refusal from save, test or activate, in words; field errors go beside their fields. */
  function refused(err: unknown, fallback: string) {
    if (err instanceof Problem && err.status === 412 && err.type === PRECONDITION_FAILED) return setTrouble({ kind: "stale" });
    if (err instanceof Problem && err.type === DRAFT_CHANGED) return setTrouble({ kind: "changed", revision: String(err.body.revision ?? "") });
    if (err instanceof Problem && err.type === LOCAL_ONLY) { setLocalOnly(true); return setActionError(err.detail); }
    if (err instanceof Problem && Object.keys(err.errors).length) {
      // A refusal on the secret opens its field, so the summary's link lands on it.
      if (err.errors.client_secret) setReplacing(true);
      return setErrors(err.errors);
    }
    setActionError(err instanceof Problem && err.type === TEST_REQUIRED ? err.detail : problemText(err, fallback));
  }
  function start(what: "save" | "test" | "activate") {
    setBusy(what); setErrors({}); setActionError(""); setTrouble(null); setNotice("");
  }
  /** Save the changed fields under the ETag; returns the saved settings (the draft's new revision). */
  async function save(): Promise<Settings> {
    const saved = await typedApi("patch", "sso/settings", { body, headers: etag ? { "If-Match": etag } : {} });
    return saved;
  }
  async function onSave(e: React.FormEvent) {
    e.preventDefault();
    if (!dirty) return setNotice("Nothing changed in the draft.");
    start("save");
    try {
      const saved = await save();
      setNotice(savedNotice(saved.draft, d, body));
      reload();
    } catch (err) {
      refused(err, "Couldn't save the draft.");
    } finally {
      setBusy("");
    }
  }
  /** Save first when the form has changes, then start the test sign-in and send the browser to the provider. */
  async function onTest() {
    start("test");
    try {
      const revision = dirty ? (await save()).draft.revision : d.revision;
      const started = await typedApi("post", "sso/settings/test", { body: { draft_revision: revision } });
      if (started.authorize_url && !safeAuthorizeUrl(started.authorize_url)) {
        setActionError("The test couldn't start: the identity provider's sign-in address isn't https.");
        setBusy("");
        return;
      }
      if (started.authorize_url) {
        window.location.assign(started.authorize_url);
        return; // the page is leaving; keep the button busy
      }
      // The provider's discovery document couldn't be used: the failure is recorded on the draft.
      setActionError(started.settings.draft.test.detail || "The test couldn't start.");
      setBusy("");
      reload();
    } catch (err) {
      refused(err, "Couldn't start the test sign-in.");
      setBusy("");
      if (dirty) reload();
    }
  }
  async function onActivate() {
    start("activate");
    try {
      const after = await typedApi("post", "sso/settings/activate", { body: { draft_revision: d.revision } });
      setNotice(`Single sign-on is active with ${providerName(after.active?.provider ?? d.provider)} (revision ${after.active?.revision ?? ""}). Sign-ins in progress start again.`);
      reload();
    } catch (err) {
      refused(err, "Couldn't activate the draft.");
    } finally {
      setBusy("");
    }
  }

  const p = "sso-";
  const shown = Object.fromEntries(Object.entries(errors).map(([k, v]) => [`${p}${k}`, v]));
  const missing = Object.keys(d.missing);
  const isActive = draftIsActive(settings);
  const why = activateReason(settings, dirty, isActive);
  return (
    <>
      <Section id="sso-draft" title={`Draft (revision ${d.revision})`}
        desc="Edit the configuration here. Nothing that signs people in changes until a draft whose test passed is activated; saving a change makes a new revision, which needs a new test.">
        <form onSubmit={onSave} noValidate aria-labelledby="sso-draft">
          {trouble?.kind === "stale" && (
            <div className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" role="alert">
              <p className="font-semibold">Someone changed these settings since you opened them. Nothing was saved.</p>
              <div className="mt-2"><Button type="button" size="sm" variant="outline" onClick={loadLatest}>Load the latest version</Button></div>
            </div>
          )}
          {trouble?.kind === "changed" && (
            <div className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" role="alert">
              <p className="font-semibold">The draft changed since you loaded it{trouble.revision ? ` (it is revision ${trouble.revision} now)` : ""}. Review it, then test again.</p>
              <div className="mt-2"><Button type="button" size="sm" variant="outline" onClick={loadLatest}>Load the latest version</Button></div>
            </div>
          )}
          <ErrorSummary errors={shown} />
          {missing.length > 0 && (
            <div className="mb-3">
              <p className="mb-1 text-sm">Before it can be tested, the saved draft needs:</p>
              <ul className="m-0 flex list-none flex-wrap gap-1.5 p-0" aria-label="What the draft needs">
                {missing.map((k) => <li key={k}><GapChip label={NEEDS[k] ?? `No ${k.replace(/_/g, " ")}`} /></li>)}
              </ul>
            </div>
          )}
          <fieldset className="mb-3" aria-describedby={shown[`${p}provider`] ? `${p}provider-error` : undefined}>
            <legend className="mb-1.5 text-sm font-medium">Provider</legend>
            <div className="grid gap-2 sm:grid-cols-2">
              {PROVIDERS.filter((o) => o.value !== "saml" || samlAllowed || form.provider === "saml").map((o) => {
                // Without sso.saml the SAML card is left out, unless the saved settings use it: then disabled, with the reason.
                const unavailable = o.value === "saml" && !samlAllowed;
                return (
                  <label key={o.value} className={`flex items-start gap-2 rounded-md border px-3 py-2 text-sm has-[:checked]:border-primary has-[:checked]:bg-accent ${unavailable ? "cursor-not-allowed border-dashed bg-muted" : "cursor-pointer"}`}>
                    <input type="radio" name="sso-provider" value={o.value} checked={form.provider === o.value} onChange={() => set("provider", o.value)} className="mt-0.5 h-4 w-4 accent-primary"
                      disabled={unavailable} aria-labelledby={unavailable ? "sso-saml-title" : undefined} aria-describedby={unavailable ? "sso-saml-unavailable" : undefined} />
                    <span>
                      <span className="font-medium" id={unavailable ? "sso-saml-title" : undefined}>{o.label}</span>
                      {unavailable
                        ? <span id="sso-saml-unavailable" className="block" data-unavailable=""><span aria-hidden="true">⊘ </span>Not available in this release.</span>
                        : <span className="block text-muted-foreground">{o.hint}</span>}
                    </span>
                  </label>
                );
              })}
            </div>
            {shown[`${p}provider`] && <p id={`${p}provider-error`} className="mt-1 text-sm text-error-foreground">{shown[`${p}provider`].join(" ")}</p>}
          </fieldset>
          {form.provider === "entra" && (
            <Field id={`${p}tenant_id`} label="Directory (tenant) ID (required)" errors={shown} help="A GUID, from the app registration's Overview in the Entra admin center. Not common or organizations.">
              <Input id={`${p}tenant_id`} value={form.tenant_id} onChange={(e) => set("tenant_id", e.target.value)} spellCheck={false} autoComplete="off" />
            </Field>
          )}
          {form.provider === "okta" && <>
            <Field id={`${p}okta_domain`} label="Okta domain (required)" errors={shown} help="Such as example.okta.com, without https://.">
              <Input id={`${p}okta_domain`} value={form.okta_domain} onChange={(e) => set("okta_domain", e.target.value)} spellCheck={false} autoComplete="off" />
            </Field>
            <Field id={`${p}okta_authorization_server`} label="Authorization server ID" errors={shown} help="Optional: a custom authorization server, such as default. Leave empty for the org authorization server.">
              <Input id={`${p}okta_authorization_server`} value={form.okta_authorization_server} onChange={(e) => set("okta_authorization_server", e.target.value)} spellCheck={false} autoComplete="off" />
            </Field>
          </>}
          {form.provider === "google" && (
            <Field id={`${p}hosted_domains`} label="Workspace domains (required)" errors={shown} help="Separated by commas, such as example.com. Accounts outside these domains are refused.">
              <Input id={`${p}hosted_domains`} value={form.hosted_domains} onChange={(e) => set("hosted_domains", e.target.value)} spellCheck={false} autoComplete="off" />
            </Field>
          )}
          {form.provider === "oidc" && (
            <Field id={`${p}discovery_url`} label="Discovery URL (required)" errors={shown} help="https://…/.well-known/openid-configuration, from your provider.">
              <Input id={`${p}discovery_url`} type="url" value={form.discovery_url} onChange={(e) => set("discovery_url", e.target.value)} spellCheck={false} autoComplete="off" />
            </Field>
          )}
          {form.provider && form.provider !== "oidc" && form.provider !== "saml" && form.provider === d.provider && d.discovery_url && (
            <p className="mb-3 text-sm text-muted-foreground [overflow-wrap:anywhere]">Discovery document: {d.discovery_url}{d.issuer ? `; issuer ${d.issuer}` : ""}</p>
          )}
          {form.provider && form.provider !== "saml" && <>
            <Field id={`${p}client_id`} label="Client ID (required)" errors={shown} help="The application (client) ID the provider gave this app.">
              <Input id={`${p}client_id`} value={form.client_id} onChange={(e) => set("client_id", e.target.value)} spellCheck={false} autoComplete="off" />
            </Field>
            {!secretOpen ? (
              <div className="mb-3">
                <p className="mb-1.5 text-sm font-medium">Client secret</p>
                <p className="flex flex-wrap items-center gap-2 text-sm">
                  <span className={chip({ state: "confirmed" })} data-testid="sso-secret"><span aria-hidden="true">✓</span>Set on {d.secret_set_at ? when(d.secret_set_at).slice(0, 10) : "an earlier date"}</span>
                  <span className="text-muted-foreground">Never shown.</span>
                  <Button type="button" size="sm" variant="outline" onClick={() => setReplacing(true)}>Replace<span className="sr-only"> the client secret</span></Button>
                </p>
              </div>
            ) : (
              <Field id={`${p}client_secret`} label={!d.secret_set || moved ? "Client secret (required)" : "New client secret"} errors={shown}
                help={!d.secret_set ? "Stored encrypted and never shown again."
                  : moved ? "Sign-in now goes to another address, so the stored secret isn't sent there: enter the client secret again."
                    : "Saving replaces the stored secret. Leave empty and choose Keep to keep it."}>
                <Input id={`${p}client_secret`} type="password" value={form.client_secret} onChange={(e) => set("client_secret", e.target.value)} autoComplete="new-password" spellCheck={false} />
              </Field>
            )}
            {d.secret_set && replacing && !moved && (
              <Button type="button" size="sm" variant="outline" className="-mt-1 mb-3" onClick={() => { setReplacing(false); setErrors((e) => { const next = { ...e }; delete next.client_secret; return next; }); set("client_secret", ""); }}>Keep the stored secret</Button>
            )}
          </>}
          <div className="flex flex-wrap items-center gap-2 border-t pt-3">
            <Button type="submit" disabled={busy !== ""}>{busy === "save" ? "Saving…" : "Save draft"}</Button>
            {dirty && <span className="text-sm text-muted-foreground" data-testid="sso-dirty">Unsaved changes</span>}
          </div>
        </form>
      </Section>
      <Section id="sso-test" title="Test and activate"
        desc="The test is a real sign-in with your own account against the draft. It signs nobody in and changes nothing; it records whether this revision works.">
        <TestResult draft={d} />
        {actionError && <p className="mt-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{actionError}</p>}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button type="button" variant="outline" disabled={busy !== "" || local} aria-describedby={local ? "sso-local-why" : undefined} onClick={() => void onTest()}>
            {busy === "test" ? "Starting the test…" : dirty ? "Save and test sign-in" : "Test sign-in"}
          </Button>
          <Button type="button" variant="outline" disabled={busy !== "" || why !== null} aria-describedby="sso-activate-why" onClick={() => void onActivate()}>
            {busy === "activate" ? "Activating…" : "Activate draft"}
          </Button>
        </div>
        {local && <p id="sso-local-why" className="mt-2 text-sm text-muted-foreground">Test sign-ins are refused while SOFTWARDEN_FORCE_LOCAL_LOGIN is set.</p>}
        <p id="sso-activate-why" className="mt-2 text-sm" data-testid="sso-activate-why">
          {why ?? `Activating makes revision ${d.revision} the configuration members sign in with.`}
        </p>
      </Section>
    </>
  );
}

/** Where a test sign-in may send the browser: an https address, or this site's own simulated provider (development and
 *  the e2e server only; the API refuses it elsewhere). Anything else (javascript:, http elsewhere) is never followed. */
function safeAuthorizeUrl(url: string) {
  try {
    const u = new URL(url, window.location.origin);
    const simulators = ["/api/v1/auth/oidc/simulator"];
    return u.protocol === "https:" || (u.origin === window.location.origin && simulators.includes(u.pathname));
  } catch {
    return false;
  }
}

/** Why Activate is disabled, in words; null when it may be pressed. */
function activateReason(s: Settings, dirty: boolean, isActive: boolean): string | null {
  const d = s.draft;
  if (s.force_local_login) return "Activation is refused while SOFTWARDEN_FORCE_LOCAL_LOGIN is set.";
  if (dirty) return "Save and test your changes before activating.";
  if (isActive) return `Revision ${d.revision} is already the active configuration.`;
  if (d.tested) return null;
  if (d.test.revision === null) return `Activate needs a passed test of revision ${d.revision}. It hasn't been tested.`;
  if (d.test.revision !== d.revision) return `Activate needs a passed test of revision ${d.revision}. The last test was of revision ${d.test.revision}.`;
  return `Activate needs a passed test of revision ${d.revision}. Its last test failed.`;
}

/** The last test, for the current revision, an earlier one, or none. */
function TestResult({ draft: d }: { draft: Draft }) {
  const t = d.test;
  const who = t.at ? <> by {t.by?.name ?? "someone no longer here"} on <span className="tabular">{when(t.at)}</span></> : null;
  if (t.revision === null) {
    return <p data-testid="sso-test-result"><span className={chip({ state: "unreviewed" })}><span aria-hidden="true">○</span>Not tested</span> <span className="text-sm text-muted-foreground">Revision {d.revision} hasn't been tested yet.</span></p>;
  }
  if (t.revision !== d.revision) {
    return (
      <div data-testid="sso-test-result">
        <p><span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Tested an earlier revision</span></p>
        <p className="mt-1 text-sm text-muted-foreground">Revision {t.revision} {t.ok ? "passed" : "failed"}{who}. The draft changed since (revision {d.revision}), so test it again.</p>
      </div>
    );
  }
  return (
    <div data-testid="sso-test-result">
      <p className="flex flex-wrap items-center gap-2">
        {t.ok
          ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Passed</span>
          : <span className={chip({ state: "overdue" })}><span aria-hidden="true">!</span>Failed</span>}
        {!t.ok && t.code && <code className="rounded-sm border bg-muted px-1.5 font-mono text-sm" data-testid="sso-test-code">{t.code}</code>}
        <span className="text-sm text-muted-foreground">Revision {t.revision}{who}</span>
      </p>
      {t.detail && <p className="mt-1 text-sm">{withoutPrefix(t.detail)}</p>}
    </div>
  );
}

// ---- the redirect URI and sign-in domains --------------------------------------------------------------------------

/** The organization's sign-in link (shown under the redirect URI, or under the SAML service provider details). */
function SignInLink() {
  const me = useMe();
  const signInLink = me.organization.slug ? `${window.location.origin}/login?${new URLSearchParams({ org: me.organization.slug })}` : "";
  if (!signInLink) return null;
  return (
    <p className="mt-2 text-sm">
      Members sign in from <code className="select-all break-all font-mono" data-testid="sso-sign-in-link">{signInLink}</code>, or by typing a work email in a sign-in domain below.
    </p>
  );
}

function RedirectUri({ uri }: { uri: string }) {
  const [status, setStatus] = useState("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(uri);
      setStatus("Redirect URI copied to the clipboard.");
    } catch {
      setStatus("Couldn't copy. Select the address and copy it by hand.");
    }
  }
  return (
    <Section id="sso-redirect" title="Redirect URI" desc="Register this address with the identity provider as the app's sign-in redirect (reply) URI. It is the same for every OpenID Connect provider.">
      <div className="flex flex-wrap items-center gap-2">
        <code className="min-w-0 flex-1 select-all break-all rounded-md border bg-muted px-3 py-2 font-mono text-sm" data-testid="sso-redirect-uri">{uri}</code>
        <Button type="button" variant="outline" onClick={() => void copy()}>Copy<span className="sr-only"> the redirect URI</span></Button>
      </div>
      <p className="mt-1 text-sm font-medium" role="status">{status}</p>
      <SignInLink />
    </Section>
  );
}

function DomainsSection({ settings, etag, reload, setNotice }: { settings: Settings; etag: string | null; reload: () => void; setNotice: (t: string) => void }) {
  const [text, setText] = useState(settings.sign_in_domains.join(", "));
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const domains = listOf(text.toLowerCase());
    if (domains.join(",") === settings.sign_in_domains.join(",")) return setNotice("The sign-in domains didn't change.");
    setBusy(true); setErrors({}); setStale(false); setNotice("");
    try {
      const saved = await typedApi("patch", "sso/settings", { body: { sign_in_domains: domains }, headers: etag ? { "If-Match": etag } : {} });
      const pending = saved.sign_in_domain_states.filter((d) => d.status === "pending").map((d) => d.domain);
      setNotice(!domains.length ? "Sign-in domains removed."
        : `Sign-in domains saved: ${domains.join(", ")}.${pending.length ? ` ${pending.join(", ")} ${pending.length === 1 ? "is" : "are"} pending: publish the TXT record shown below, then verify.` : ""}`);
      reload();
    } catch (err) {
      if (err instanceof Problem && err.status === 412 && err.type === PRECONDITION_FAILED) setStale(true);
      else setErrors({ "sso-sign_in_domains": err instanceof Problem ? (err.errors.sign_in_domains ?? [err.detail]) : [problemText(err, "Couldn't save the sign-in domains.")] });
      document.getElementById("sso-sign_in_domains")?.focus();
    } finally {
      setBusy(false);
    }
  }
  return (
    <Section id="sso-domains" title="Sign-in domains"
      desc={<>Work email domains that find this organization on the sign-in page, so members can type their email instead of using a link with <code className="font-mono">?org=</code>. They sit outside the draft, and a verified domain belongs to one organization on this installation. Where the installation asks for proof (a hosted service does), a new domain is pending until you publish a DNS TXT record and verify it; elsewhere it applies when you save.</>}>
      {settings.sign_in_domain_states.length > 0 && <DomainStates states={settings.sign_in_domain_states} reload={reload} setNotice={setNotice} />}
      <form onSubmit={submit} noValidate aria-labelledby="sso-domains">
        {stale && (
          <div className="mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" role="alert">
            <p className="font-semibold">Someone changed these settings since you opened them. Nothing was saved.</p>
            <div className="mt-2"><Button type="button" size="sm" variant="outline" onClick={() => { setStale(false); reload(); }}>Load the latest version</Button></div>
          </div>
        )}
        <Field id="sso-sign_in_domains" label="Domains" errors={errors} help="Separated by commas, such as example.com, example.org. Leave empty for none.">
          <Input id="sso-sign_in_domains" value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} autoComplete="off" />
        </Field>
        {settings.sign_in_domains.length === 0 && <p className="mb-3 text-sm text-muted-foreground">None yet: members use the organization's sign-in link.</p>}
        <Button type="submit" variant="outline" disabled={busy}>{busy ? "Saving…" : "Save domains"}</Button>
      </form>
    </Section>
  );
}

type DomainState = Settings["sign_in_domain_states"][number];

/** Each sign-in domain's state: ✓ Verified (it finds this organization on the sign-in page),
 *  or ◐ Pending with the DNS TXT record to publish, when the claim lapses, and Verify. Every refusal to verify is the
 *  API's one neutral sentence (no record yet, a DNS failure and a domain another organization holds read the same), so
 *  the page says only that and what to check. A fresh proof never takes a domain from another organization: support
 *  moves it. */
function DomainStates({ states, reload, setNotice }: { states: DomainState[]; reload: () => void; setNotice: (t: string) => void }) {
  const [busy, setBusy] = useState("");
  const [refusals, setRefusals] = useState<Record<string, string>>({});
  const [copied, setCopied] = useState("");
  // The Verify button is gone once its domain is verified: focus moves to that domain's new state instead.
  const [focusAfter, setFocusAfter] = useState("");
  useEffect(() => {
    if (!focusAfter || states.find((s) => s.domain === focusAfter)?.status !== "verified") return;
    document.getElementById(domainId(focusAfter))?.focus();
    setFocusAfter("");
  }, [states, focusAfter]);
  async function verify(domain: string) {
    setBusy(domain); setRefusals((r) => ({ ...r, [domain]: "" })); setNotice("");
    try {
      await typedApi("post", "sso/settings/domains/verify", { body: { domain } });
      setNotice(`${domain} is verified: members who type an address at ${domain} on the sign-in page now find this organization.`);
      setFocusAfter(domain);
      reload();
    } catch (err) {
      const said = err instanceof Problem ? (err.errors.domain ?? [err.detail]).join(" ") : problemText(err, "Couldn't verify the domain.");
      setRefusals((r) => ({ ...r, [domain]: said }));
    } finally {
      setBusy("");
    }
  }
  async function copy(what: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(`${what} copied to the clipboard.`);
    } catch {
      setCopied("Couldn't copy. Select the text and copy it by hand.");
    }
  }
  return (
    <>
      <ul className="m-0 mb-4 grid list-none gap-3 p-0" aria-label="Sign-in domain states" data-testid="sso-domain-states">
        {states.map((s) => (
          <li key={s.domain} className="rounded-md border px-3 py-2.5" data-domain={s.domain}>
            <p className="flex flex-wrap items-center gap-2">
              <span className="font-mono font-semibold">{s.domain}</span>
              <span id={domainId(s.domain)} tabIndex={-1} className="rounded-sm outline-none focus:ring-2 focus:ring-ring">
                {s.status === "verified"
                  ? <span className={chip({ state: "confirmed" })}><span aria-hidden="true">✓</span>Verified</span>
                  : <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Pending verification</span>}
              </span>
              <span className="text-sm text-muted-foreground">
                {s.status === "verified"
                  ? <>Finds this organization on the sign-in page{s.verified_at ? <>; verified <span className="tabular">{when(s.verified_at)}</span></> : null}.</>
                  : <>Finds nobody on the sign-in page until it is verified{s.lapses_at ? <>; forgotten on <span className="tabular">{when(s.lapses_at).slice(0, 10)}</span> if it isn't</> : null}.</>}
              </span>
            </p>
            {s.status === "pending" && s.record_name && s.record_value && (
              <div className="mt-2 grid gap-2 text-sm">
                <p>Publish this TXT record in the DNS of {s.domain}, then verify. It can take a few minutes to be seen.</p>
                <DnsValue label="Name" value={s.record_name} onCopy={() => void copy("The record's name", s.record_name!)} />
                <DnsValue label="Value" value={s.record_value} onCopy={() => void copy("The record's value", s.record_value!)} />
                <div>
                  <Button type="button" size="sm" variant="outline" disabled={busy !== ""} aria-describedby={refusals[s.domain] ? `${domainId(s.domain)}-refused` : undefined}
                    onClick={() => void verify(s.domain)}>{busy === s.domain ? "Checking DNS…" : <>Verify<span className="sr-only"> {s.domain}</span></>}</Button>
                </div>
                {refusals[s.domain] && (
                  <p id={`${domainId(s.domain)}-refused`} className="rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">
                    <span aria-hidden="true">! </span>{refusals[s.domain]}
                  </p>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
      <p className="sr-only" role="status">{copied}</p>
    </>
  );
}

const domainId = (domain: string) => `sso-domain-${domain.replace(/[^a-z0-9]+/gi, "-")}`;

function DnsValue({ label, value, onCopy }: { label: string; value: string; onCopy: () => void }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="w-12 font-medium">{label}</span>
      <code className="min-w-0 flex-1 select-all break-all rounded-md border bg-muted px-2 py-1 font-mono">{value}</code>
      <Button type="button" size="sm" variant="outline" onClick={onCopy}>Copy<span className="sr-only"> the record's {label.toLowerCase()}</span></Button>
    </div>
  );
}
