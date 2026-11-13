/** Sign-in (password, or the organization's single sign-on: Microsoft Entra ID, Okta, Google, OpenID Connect or SAML) and the redirect that sends signed-out visitors to it. */
import { useEffect, useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { api, Me, Problem, type PlatformSession } from "@/api";
import { AuthCard } from "@/features/auth/auth-card";
import { Field } from "@/components/app";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { Schemas } from "@/lib/openapi";

/** Where everyone lands after signing in (password or Entra), first-run setup or switching organization with no page in
 *  mind: the dashboard, for every role (superseding "home for Admins"). `/` itself stays
 *  Needs attention. The API's Entra callback uses the same default. */
export const HOME = "/dashboard";

export function RedirectToLogin() {
  const location = useLocation();
  // The whole address, so a shared link's filters, tab and fragment survive sign-in.
  return <Navigate to="/login" replace state={{ from: location.pathname + location.search + location.hash }} />;
}

/** Only a path on this site: "/x?y#z" yes; "//host", "https://…" or anything else counts as no page asked for ("/").
 *  A control character or backslash anywhere is refused too: browsers strip tabs and newlines and read "\" as "/", so
 *  "/\t/evil.example" would become "//evil.example". The API's Entra callback applies the same rule. */
function localPath(from?: string) {
  const unsafe = (v: string) => [...v].some((c) => c.charCodeAt(0) <= 0x1f || c === "\\");
  return from && from.startsWith("/") && !from.startsWith("//") && !unsafe(from) ? from : "/";
}

/** Why a single sign-on came back refused (the callback's ?error=), with the provider's name ("Microsoft Entra ID",
 *  "Okta", or "single sign-on" before the options answer). No identity details are shown. A code not listed here gets
 *  the generic message (`provider_error`); own keys only, so "?error=constructor" can't reach Object.prototype. */
const SAML_REFUSED = "Your identity provider's response couldn't be accepted. Ask your administrator to check the SAML settings.";

const SSO_ERRORS: Record<string, (provider: string) => string> = {
  no_account: (p) => `Your ${p} account doesn't match a Softwarden account. Ask a Softwarden administrator to invite you with your work email.`,
  inactive: () => "Your Softwarden account is deactivated. Ask a Softwarden administrator.",
  other_identity: (p) => `Your Softwarden account already signs in with a different ${p} account. Ask an administrator to unlink it.`,
  expired: () => "The sign-in took too long or was started in another tab. Try again.",
  cancelled: (p) => `Sign-in with ${p} was canceled.`,
  provider_error: (p) => `Sign-in with ${p} didn't complete. Try again, or sign in with your password.`,
  not_configured: (p) => `Sign-in with ${p} isn't set up. Sign in with your password.`,
  issuer: (p) => `The sign-in came back from an identity provider this organization doesn't use. Try again, or sign in with your password. If it keeps happening, ask an administrator to check the ${p} settings.`,
  audience: (p) => `The sign-in was meant for a different application. Try again, or ask an administrator to check the ${p} client ID.`,
  nonce: () => "The sign-in couldn't be matched to the one this page started. Try again from this page.",
  token_refused: (p) => `${p} didn't accept Softwarden's sign-in request. Try again, or ask an administrator to check the client secret.`,
  hosted_domain: () => "Your account isn't in a domain this organization accepts. Sign in with your work account.",
  email_unverified: (p) => `${p} hasn't verified your email address, so it can't be matched to a Softwarden account. Verify it, then try again.`,
  tenant: (p) => `Your ${p} account belongs to a different directory than this organization's. Sign in with your work account.`,
  signature: () => "The sign-in couldn't be verified, so it was refused. Try again, or sign in with your password.",
  unknown_key: (p) => `The sign-in was signed with a key ${p} doesn't publish. Try again in a few minutes, or sign in with your password.`,
  changed: () => "This organization's sign-in settings changed while you were signing in. Try again.",
  // Platform staff accounts never sign in through an organization's single sign-on.
  platform_staff: () => "This account is a platform staff account, so it can't sign in through an organization's single sign-on. Sign in with your password.",
  // A sign-in that matched no member, with auto-provision on, is recorded for an Admin to approve.
  access_pending: (p) => `Your ${p} account isn't a member yet. Your request for access is waiting for a Softwarden administrator; sign in again once they approve it.`,
  access_pending_full: () => "Your request for access couldn't be recorded: this organization has too many requests waiting. Ask a Softwarden administrator to invite you.",
  // SAML (from the optional `ee` package). Most say the response was refused and who can fix it; the ones a person
  // can act on (start again, a clock) say so. The Admin sees the technical reason on a test sign-in.
  unsolicited: () => "This sign-in wasn't started from this page, so it was refused. Start again from this page.",
  in_response_to: () => "The answer from your identity provider doesn't belong to the sign-in this page started. Start again from this page.",
  wrong_organization: () => "The sign-in came back to a different organization's address. Start again from your organization's sign-in link.",
  replay: () => "That sign-in was already used once, so it was refused. Start a new sign-in from this page.",
  not_yet_valid: () => "Your identity provider's response isn't valid yet, which usually means a clock is wrong. Try again in a minute; if it keeps happening, ask your administrator.",
  wrapping: () => SAML_REFUSED,
  destination: () => SAML_REFUSED,
  recipient: () => SAML_REFUSED,
  subject_confirmation: () => SAML_REFUSED,
  no_name_id: () => SAML_REFUSED,
  name_id_format: () => SAML_REFUSED,
  encrypted: () => SAML_REFUSED,
  unsigned: () => SAML_REFUSED,
  weak_algorithm: () => SAML_REFUSED,
  invalid_response: () => SAML_REFUSED,
};

/** For an organization that signs in with SAML, the codes OpenID Connect shares get SAML's wording (no client ID or
 *  secret to blame); every other code reads as in SSO_ERRORS. */
const SAML_ERRORS: Record<string, (provider: string) => string> = {
  cancelled: () => "Sign-in at your identity provider was canceled or didn't complete. Try again, or sign in with your password.",
  provider_error: () => "Your identity provider answered with an error, so you weren't signed in. Try again, or sign in with your password.",
  issuer: () => SAML_REFUSED,
  audience: () => SAML_REFUSED,
  signature: () => SAML_REFUSED,
};

/** The sentence for a refusal code: SAML's own wording first for a SAML organization, then the shared one, then the
 *  generic one. Own keys only. */
function refusal(code: string, saml: boolean) {
  if (saml && Object.hasOwn(SAML_ERRORS, code)) return SAML_ERRORS[code];
  if (Object.hasOwn(SSO_ERRORS, code)) return SSO_ERRORS[code];
  return saml ? SAML_ERRORS.provider_error : SSO_ERRORS.provider_error;
}

type AuthOptions = Schemas["AuthOptions"];
/** When the options can't be read: the password form only. */
const PASSWORD_ONLY: AuthOptions = { entra: false, setup_pending: false, password: true, signup_enabled: false, password_reset_enabled: false, sso: null };

/** Where the single sign-on button goes: the API's start route (with the org or email it was given) plus the page to
 *  come back to. An older API without `sso` but with `entra` gets the Entra start route, as before. */
function ssoButton(options: AuthOptions, landing: string): { text: string; href: string } | null {
  const sso = options.sso ?? (options.entra ? { provider: "entra" as const, label: "Microsoft Entra ID", start_url: "/api/v1/auth/oidc/start" } : null);
  if (!sso) return null;
  const join = sso.start_url.includes("?") ? "&" : "?";
  const href = `${sso.start_url}${join}${new URLSearchParams({ next: landing })}`;
  // Entra keeps its long-standing name (visible text and accessible name alike); any other is "Sign in with <label>"
  // (for SAML the Admin's button label, or "SAML single sign-on").
  return { text: sso.provider === "entra" ? "Sign in with Microsoft Entra ID" : `Sign in with ${sso.label}`, href };
}

/** `auth/login` answers `me`, or for a platform staff account with no membership `platform/session`'s body (no
 *  `organization`), which signs in for the platform console only. */
export function isPlatformSession(body: Me | PlatformSession): body is PlatformSession {
  return !("organization" in body);
}

export function Login({ onSignedIn, onStaffSignedIn, notice = "" }: {
  onSignedIn: (me: Me) => void; onStaffSignedIn?: (session: PlatformSession) => void;
  /** Why the visitor is here (the session ended). A page that sent them here may say so in `state.notice` too. */
  notice?: string;
}) {
  const location = useLocation();
  const said = notice || ((location.state as { notice?: unknown } | null)?.notice === undefined ? "" : String((location.state as { notice?: unknown }).notice));
  const params = new URLSearchParams(location.search);
  // A sign-in link names the organization (?org=acme) or a work email (?email=); with neither, the API answers for the
  // installation's only organization, and with several, password sign-in only.
  const org = params.get("org") ?? "";
  const hint = params.get("email") ?? "";
  const [email, setEmail] = useState(hint);
  const [password, setPassword] = useState("");
  // The refusal the callback sent back (?error=), shown until the first password attempt replaces it.
  const [ssoError, setSsoError] = useState(params.get("error"));
  const [error, setError] = useState("");
  // One request says which sign-in methods are offered and whether first-run setup is still open. Nothing shows until
  // it answers, so a fresh installation goes straight to setup instead of flashing the form; a failure shows the form.
  const [options, setOptions] = useState<AuthOptions | null>(null);
  useEffect(() => {
    const query = org ? `?${new URLSearchParams({ org })}` : hint ? `?${new URLSearchParams({ email: hint })}` : "";
    let live = true;
    api<AuthOptions>(`auth/options${query}`).then(
      (o) => live && setOptions(o),
      () => live && setOptions(PASSWORD_ONLY),
    );
    return () => {
      live = false;
    };
  }, [org, hint]);
  const navigate = useNavigate();
  // After a refused single sign-on the page they were going to comes back as ?next=.
  const from = localPath((location.state as { from?: string } | null)?.from ?? params.get("next") ?? undefined);
  // No page asked for ("/") means the dashboard, for the password form and the single sign-on button alike.
  const landing = from === "/" ? HOME : from;
  // One sign-in at a time: a second Enter while one is on its way would count against the sign-in limit.
  const [submitting, setSubmitting] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setError("");
    setSsoError(null);
    setSubmitting(true);
    try {
      const body = await api<Me | PlatformSession>("auth/login", { method: "POST", body: { email, password } });
      await api("auth/csrf"); // Django rotates the CSRF token at sign-in
      if (isPlatformSession(body)) {
        // Platform staff with no organization: the console is the only place this session can go.
        onStaffSignedIn?.(body);
        navigate("/platform", { replace: true });
        return;
      }
      onSignedIn(body);
      // Everyone's home is the dashboard; a page they were sent from wins.
      navigate(landing, { replace: true });
    } catch (err) {
      if (err instanceof Problem) setError(err.detail);
      // fetch rejects with a TypeError when the request never got an answer: say so rather than "failed".
      else if (err instanceof TypeError) setError("Couldn't reach Softwarden, so you aren't signed in. Check your connection and try again.");
      else {
        // Anything else went wrong after the server answered, so the sign-in may have gone through: ask it.
        const me = await api<Me>("auth/me").catch(() => null);
        if (me?.user) {
          onSignedIn(me);
          navigate(landing, { replace: true });
          return;
        }
        setError("Sign-in didn't complete. Try again.");
      }
      setSubmitting(false);
    }
  }
  if (options === null) {
    return <AuthCard title="Sign in to Softwarden"><p className="text-muted-foreground" role="status">Loading sign-in…</p></AuthCard>;
  }
  // A fresh installation has no accounts to sign in with: send the visitor to first-run setup instead.
  if (options.setup_pending) return <Navigate to="/setup" replace />;
  const button = ssoButton(options, landing);
  const saml = options.sso?.provider === "saml";
  // A SAML label is the Admin's button text, not a provider's name, so its messages say "single sign-on".
  const providerName = button && !saml ? (options.sso?.label ?? "Microsoft Entra ID") : "single sign-on";
  const shown = error || (ssoError ? refusal(ssoError, saml)(providerName) : "");
  return (
    <AuthCard title="Sign in to Softwarden">
      <p className="mb-4 text-sm text-muted-foreground">For everyone with a Softwarden account, including anyone an Admin invited. You don't need an account to be named as a record's owner.</p>
      {said && !shown && <p className="mb-3 rounded-md border border-l-4 border-info-border bg-info-bg px-3 py-2" role="status"><span aria-hidden="true">ⓘ </span>{said}</p>}
      <form onSubmit={submit} aria-label="Sign in">
        {shown && <p id="login-error" className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{shown}</p>}
        <Field id="email" label="Email"><Input id="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required aria-invalid={shown ? true : undefined} aria-describedby={shown ? "login-error" : undefined} /></Field>
        <Field id="password" label="Password"><Input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required aria-invalid={shown ? true : undefined} aria-describedby={shown ? "login-error" : undefined} /></Field>
        <Button type="submit" className="mt-1 w-full" aria-disabled={submitting || undefined}>{submitting ? "Signing in…" : "Sign in"}</Button>
        {/* Offered while the installation can send email. */}
        {options.password_reset_enabled && <p className="mt-3 text-center text-sm"><Link to="/forgot-password">Forgot your password?</Link></p>}
      </form>
      {button && (
        <div className="mt-4 border-t pt-4">
          {/* A full-page navigation: the browser goes to the identity provider and comes back through the API's callback. */}
          <Button asChild variant="outline" className="w-full">
            <a href={button.href}>{button.text}</a>
          </Button>
        </div>
      )}
      {options.signup_enabled && (
        // Public signup is on for this installation.
        <p className="mt-4 border-t pt-4 text-sm">New to Softwarden? <Link to="/signup" className="font-semibold">Create an account</Link></p>
      )}
    </AuthCard>
  );
}
