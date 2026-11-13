/** Public signup (cloud): `/signup` creates an account and a new organization once the email is confirmed, and
 *  `/signup/verify#<token>` confirms it. Loaded on first visit, outside the first bundle.
 *
 *  - Signup asks for the email, a name, the organization's name, a password and (optionally) the organization's
 *    timezone, and answers "check your email" (202, the same for any address). A new link can be sent from there; the
 *    API allows one message a minute per address, so the button waits a minute between sends.
 *  - The confirmation link's token is in the fragment, which browsers never send, so it is in no server or proxy log.
 *    The page reads it into memory, takes it out of the address bar at once and sends it in the body only; it is never
 *    stored, logged or put in a query string. The password chosen at signup is asked again (the API requires it, so a
 *    stranger who signed up with your address can't hand you an account with their password).
 *  - Every refusal gets a plain sentence: signup off (404), no email on this installation (503), the message refused
 *    just now (503), too many attempts (429), an unknown, used, replaced or expired link (404), and an address that got
 *    an account meanwhile (409). */
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, Problem, type Me } from "@/api";
import { ErrorSummary, Field } from "@/components/app";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AuthCard } from "@/features/auth/auth-card";
import { HOME } from "@/features/auth/login";
import { typedApi, type Schemas } from "@/lib/openapi";

const DISABLED = "/problems/signup-disabled";
const UNAVAILABLE = "/problems/signup-unavailable";
const EMAIL_FAILED = "/problems/email-failed";
const LINK_UNAVAILABLE = "/problems/signup-link-unavailable";
const PAUSED = "/problems/signup-paused";
/** The API sends at most one message a minute per address. */
const RESEND_WAIT_S = 60;

/** "in 3 minutes", from a 429's Retry-After (seconds). */
function waitText(seconds?: number) {
  if (!seconds) return "a few minutes";
  if (seconds < 120) return `${seconds} ${seconds === 1 ? "second" : "seconds"}`;
  const m = Math.ceil(seconds / 60);
  return `${m} ${m === 1 ? "minute" : "minutes"}`;
}

/** The sentence for a refused signup, resend or confirmation that isn't a field error. */
function refusalText(e: unknown, what: "signup" | "resend" | "verify") {
  if (!(e instanceof Problem)) return "Softwarden didn't answer. Check your connection and try again; nothing was saved.";
  if (e.type === DISABLED) return "Signup is turned off on this installation. Ask the person who runs it for an invitation instead.";
  if (e.type === UNAVAILABLE) return "Signup isn't available right now: this installation can't send email to confirm addresses. Try again later.";
  if (e.type === EMAIL_FAILED) return `The confirmation email couldn't be sent just now, so nothing was saved. Try again in a few minutes.`;
  // The installation's daily ceiling on new organizations (SOFTWARDEN_SIGNUP_DAILY_LIMIT). A pending signup keeps
  // its link, so a confirmation can simply be tried again later.
  if (e.type === PAUSED) {
    return what === "verify"
      ? "New organizations are paused for a few hours: this installation has created as many as it allows in a day. Your link keeps working until it expires, so open it again later. Nothing was created."
      : "New organizations are paused for a few hours: this installation has created as many as it allows in a day. Try again later; nothing was saved.";
  }
  if (e.status === 429) return `Too many attempts from this network. Try again in ${waitText(e.retryAfter)}.`;
  if (e.status === 403) return "This page's security check expired. Reload the page and try again.";
  return what === "verify" ? `${e.detail} Nothing was created.` : `${e.detail} Nothing was saved.`;
}

async function withCsrf<T>(call: () => Promise<T>) {
  await api("auth/csrf"); // the signup routes are CSRF-checked even with nobody signed in
  return call();
}

function browserTimezone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return ""; }
}

/** Shown when signup is off: where to go instead. */
function SignupOff() {
  return (
    <AuthCard title="Signup is turned off">
      <p className="mb-4 text-sm text-muted-foreground">This installation doesn't take new signups. Ask the person who runs it for an invitation.</p>
      <Button asChild variant="outline" className="w-full"><Link to="/login">Go to sign in</Link></Button>
    </AuthCard>
  );
}

export function SignupPage() {
  // Whether signup is on (auth/options); nothing shows until it answers, and a failure shows the form (the API decides).
  const options = useQuery({ queryKey: ["auth-options"], queryFn: () => api<Schemas["AuthOptions"]>("auth/options"), gcTime: 0, retry: false });
  const [form, setForm] = useState({ email: "", name: "", organization_name: "", password: "", timezone: browserTimezone() });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [banner, setBanner] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState<{ email: string; detail: string } | null>(null);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    const missing: Record<string, string[]> = {};
    if (!form.email.trim()) missing.email = ["Enter your work email."];
    if (!form.name.trim()) missing.name = ["Enter your name."];
    if (!form.organization_name.trim()) missing.organization_name = ["Enter your organization's name."];
    if (!form.password) missing.password = ["Choose a password of 12 characters or more."];
    setBanner("");
    if (Object.keys(missing).length) return setErrors(missing);
    setBusy(true);
    try {
      const body = { email: form.email.trim(), name: form.name.trim(), organization_name: form.organization_name.trim(), password: form.password, ...(form.timezone.trim() ? { timezone: form.timezone.trim() } : {}) };
      const answer = await withCsrf(() => typedApi("post", "auth/signup", { body }));
      setErrors({});
      setSent({ email: body.email, detail: answer.detail });
    } catch (err) {
      if (err instanceof Problem && Object.keys(err.errors).length) setErrors(err.errors);
      else { setErrors({}); setBanner(refusalText(err, "signup")); }
    } finally {
      setBusy(false);
    }
  }

  if (options.isPending) return <AuthCard title="Create your Softwarden account"><p className="text-muted-foreground" role="status">Loading…</p></AuthCard>;
  if (options.data && !options.data.signup_enabled) return <SignupOff />;
  if (sent) return <CheckEmail email={sent.email} detail={sent.detail} onRestart={() => setSent(null)} />;
  return (
    <AuthCard title="Create your Softwarden account">
      <p className="mb-4 text-sm text-muted-foreground">You'll be the Admin of a new organization. We email you a link to confirm your address first; nothing is created until you open it.</p>
      <form onSubmit={submit} noValidate aria-label="Create an account">
        <ErrorSummary errors={errors} focusOnShow="field" />
        {banner && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{banner}</p>}
        <Field id="email" label="Work email (required)" errors={errors} help="You sign in with it once it's confirmed.">
          <Input id="email" type="email" autoComplete="email" value={form.email} onChange={set("email")} />
        </Field>
        <Field id="name" label="Your name (required)" errors={errors}><Input id="name" autoComplete="name" value={form.name} onChange={set("name")} /></Field>
        <Field id="organization_name" label="Organization name (required)" errors={errors} help="Usually your company or IT team. You can change it later.">
          <Input id="organization_name" autoComplete="organization" value={form.organization_name} onChange={set("organization_name")} />
        </Field>
        <Field id="password" label="Password (required)" errors={errors} help="12 characters or more, not a common password. You'll enter it again to confirm.">
          <Input id="password" type="password" autoComplete="new-password" value={form.password} onChange={set("password")} />
        </Field>
        <Field id="timezone" label="Timezone" errors={errors} help="Dates and deadlines are judged in this timezone, such as Europe/London. Leave it blank for UTC.">
          <Input id="timezone" value={form.timezone} onChange={set("timezone")} />
        </Field>
        <Button type="submit" className="mt-1 w-full" aria-busy={busy || undefined}>{busy ? "Creating…" : "Create account"}</Button>
      </form>
      <p className="mt-4 border-t pt-4 text-sm">Already have an account? <Link to="/login" className="font-semibold">Sign in</Link></p>
    </AuthCard>
  );
}

/** After the 202: what to do next, and a new link once a minute. */
function CheckEmail({ email, detail, onRestart }: { email: string; detail: string; onRestart: () => void }) {
  const [wait, setWait] = useState(RESEND_WAIT_S);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const heading = useRef<HTMLParagraphElement>(null);
  useEffect(() => { heading.current?.focus(); }, []);
  useEffect(() => {
    if (wait <= 0) return;
    const t = setTimeout(() => setWait((w) => w - 1), 1000);
    return () => clearTimeout(t);
  }, [wait]);
  async function resend() {
    if (busy) return;
    if (wait > 0) return setStatus(`You can send a new link in ${waitText(wait)}.`);
    setBusy(true); setError(""); setStatus("");
    try {
      const answer = await withCsrf(() => typedApi("post", "auth/signup/resend", { body: { email } }));
      setStatus(answer.detail);
      setWait(RESEND_WAIT_S);
    } catch (err) {
      if (err instanceof Problem && err.status === 429) setWait(err.retryAfter ?? RESEND_WAIT_S);
      setError(refusalText(err, "resend"));
    } finally {
      setBusy(false);
    }
  }
  return (
    <AuthCard title="Check your email">
      <p ref={heading} tabIndex={-1} className="mb-3 rounded-md border border-l-4 border-info-border bg-info-bg px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" data-testid="signup-sent">
        <span aria-hidden="true">ⓘ </span>{detail}
      </p>
      <p className="mb-4 text-sm">We sent it to <span className="font-semibold [overflow-wrap:anywhere]">{email}</span>. Open the link in this browser and enter your password again to finish.</p>
      {error && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm" role="alert">{error}</p>}
      <p className="mb-3 text-sm font-medium" role="status">{status}</p>
      <div className="flex flex-wrap gap-2">
        {/* Not disabled while it waits: pressing it says how long is left. */}
        <Button type="button" variant="outline" aria-busy={busy || undefined} aria-describedby="resend-help" onClick={() => void resend()}>{busy ? "Sending…" : "Send a new link"}</Button>
        <Button type="button" variant="outline" onClick={onRestart}>Use a different email</Button>
      </div>
      <p id="resend-help" className="mt-2 text-sm text-muted-foreground">{wait > 0 ? `A new link can be sent in ${wait} ${wait === 1 ? "second" : "seconds"}. It replaces the one we sent.` : "A new link replaces the one we sent."}</p>
    </AuthCard>
  );
}

/** `/signup/verify#<token>`: the password again, then signed in on the new organization's dashboard. App's VerifyRoute
 *  reads the token from the fragment and clears the address bar before this page loads; here it is in memory only, never
 *  stored, logged, or sent anywhere but this page's request body. */
export function VerifyPage({ token, onSignedIn }: { token: string; onSignedIn: (me: Me) => void }) {
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [refusal, setRefusal] = useState<{ kind: "link" | "taken" | "other"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (!password) return setErrors({ password: ["Enter the password you chose when you signed up."] });
    setBusy(true); setRefusal(null);
    try {
      await withCsrf(() => typedApi("post", "auth/signup/verify", { body: { token, password } }));
      await api("auth/csrf").catch(() => undefined); // Django rotates the CSRF token when it signs someone in
      const me = await api<Me>("auth/me").catch(() => null);
      // Signed in either way; if `me` couldn't be read just now, a full load of the dashboard reads it.
      if (!me) return window.location.assign(HOME);
      onSignedIn(me);
      navigate(HOME, { replace: true });
    } catch (err) {
      setBusy(false);
      if (err instanceof Problem && err.type === LINK_UNAVAILABLE) return setRefusal({ kind: "link", text: "" });
      if (err instanceof Problem && err.status === 409) return setRefusal({ kind: "taken", text: "" });
      if (err instanceof Problem && Object.keys(err.errors).length) return setErrors(err.errors);
      setErrors({});
      setRefusal({ kind: "other", text: refusalText(err, "verify") });
    }
  }

  if (!token || refusal?.kind === "link") {
    return (
      <AuthCard title="This link can't be used">
        <p className="mb-2 text-sm" role={refusal ? "alert" : undefined}>
          {token ? "This confirmation link isn't valid any more." : "This page needs the link from your confirmation email."}
          {" "}Links work once, for 24 hours, and a newer link replaces an older one.
        </p>
        <p className="mb-4 text-sm text-muted-foreground">Open the newest link from your email, or sign up again to get a new one. Nothing was created.</p>
        <div className="flex flex-col gap-2">
          <Button asChild variant="outline" className="w-full"><Link to="/signup">Sign up again</Link></Button>
          <Button asChild variant="outline" className="w-full"><Link to="/login">Go to sign in</Link></Button>
        </div>
      </AuthCard>
    );
  }
  if (refusal?.kind === "taken") {
    return (
      <AuthCard title="This email already has an account">
        <p className="mb-4 text-sm" role="alert">Someone confirmed an account for this address since you signed up, so no new organization was created. Sign in with that account instead.</p>
        <Button asChild variant="outline" className="w-full"><Link to="/login">Go to sign in</Link></Button>
      </AuthCard>
    );
  }
  return (
    <AuthCard title="Confirm your email">
      <p className="mb-4 text-sm text-muted-foreground">Enter the password you chose when you signed up. Your account and organization are created, and you're signed in.</p>
      <form onSubmit={submit} noValidate aria-label="Confirm your email">
        <ErrorSummary errors={errors} focusOnShow="field" />
        {refusal && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2" role="alert">{refusal.text}</p>}
        <Field id="password" label="Password (required)" errors={errors}>
          <Input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Button type="submit" className="mt-1 w-full" aria-busy={busy || undefined}>{busy ? "Confirming…" : "Confirm and sign in"}</Button>
      </form>
    </AuthCard>
  );
}
