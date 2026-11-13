/** Self-service password reset (docs/API.md "Password reset by email"), loaded on first
 *  visit, outside the first bundle.
 *
 *  - `/forgot-password` asks for the email and always answers the same "check your email", whether or not the address
 *    has an account (the API's 202 is the same for every address; the link goes out on the worker's next tick).
 *  - `/reset-password#<token>`: App's route reads the token from the fragment and takes it out of the address bar before
 *    this page loads; here it is in memory only and sent in the request body. A new password and its confirmation; the
 *    204 ends every session of the account, so the page goes to sign in with a notice.
 *  - Every refusal gets a sentence: no email on this installation (503), too many requests (429), a link that can't be
 *    used (404), a password the validators refuse (400, inline; the link stays usable), and a platform staff session
 *    acting as a member (403 impersonation-restricted: an impersonation never asks for or uses a reset link). */
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, IMPERSONATION_RESTRICTED, Problem } from "@/api";
import { ErrorSummary, Field } from "@/components/app";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AuthCard } from "@/features/auth/auth-card";
import { typedApi } from "@/lib/openapi";

const UNAVAILABLE = "/problems/password-reset-unavailable";
const LINK_UNAVAILABLE = "/problems/password-reset-link-unavailable";

/** "3 minutes", from a 429's Retry-After (seconds). */
function waitText(seconds?: number) {
  if (!seconds) return "a few minutes";
  if (seconds < 120) return `${seconds} ${seconds === 1 ? "second" : "seconds"}`;
  const m = Math.ceil(seconds / 60);
  return `${m} ${m === 1 ? "minute" : "minutes"}`;
}

/** The sentence for a refusal that isn't a field error. */
function refusalText(e: unknown) {
  if (!(e instanceof Problem)) return "Softwarden didn't answer. Check your connection and try again.";
  if (e.type === UNAVAILABLE) return "Password reset by email isn't available on this installation, because it can't send email. Ask the person who runs Softwarden to reset your password.";
  if (e.type === IMPERSONATION_RESTRICTED) return `${e.detail} A password reset is the member's own: nothing was sent or changed.`;
  if (e.status === 429) return `Too many requests from this network. Try again in ${waitText(e.retryAfter)}.`;
  if (e.status === 403) return "This page's security check expired. Reload the page and try again.";
  return e.detail;
}

async function withCsrf<T>(call: () => Promise<T>) {
  await api("auth/csrf"); // the reset routes are CSRF-checked even with nobody signed in
  return call();
}

const alertBox = "mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm";

export function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [banner, setBanner] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState<{ email: string; detail: string } | null>(null);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBanner("");
    if (!email.trim()) return setErrors({ email: ["Enter the email you sign in with."] });
    setBusy(true);
    try {
      const answer = await withCsrf(() => typedApi("post", "auth/password-reset", { body: { email: email.trim() } }));
      setErrors({});
      setSent({ email: email.trim(), detail: answer.detail });
    } catch (err) {
      if (err instanceof Problem && Object.keys(err.errors).length) setErrors(err.errors);
      else { setErrors({}); setBanner(refusalText(err)); }
    } finally {
      setBusy(false);
    }
  }
  if (sent) return <CheckEmail {...sent} onRestart={() => setSent(null)} />;
  return (
    <AuthCard title="Reset your password">
      <p className="mb-4 text-sm text-muted-foreground">Enter the email you sign in with. If it has a password here, we email a link to choose a new one; the link works once, for an hour.</p>
      <form onSubmit={submit} noValidate aria-label="Reset your password">
        <ErrorSummary errors={errors} focusOnShow="field" />
        {banner && <p className={alertBox} role="alert">{banner}</p>}
        <Field id="email" label="Email (required)" errors={errors}>
          <Input id="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Button type="submit" className="mt-1 w-full" aria-busy={busy || undefined}>{busy ? "Sending…" : "Email me a link"}</Button>
      </form>
      <p className="mt-4 border-t pt-4 text-sm">Remembered it? <Link to="/login" className="font-semibold">Sign in</Link></p>
    </AuthCard>
  );
}

/** After the 202: the same words for every address. */
function CheckEmail({ email, detail, onRestart }: { email: string; detail: string; onRestart: () => void }) {
  const said = useRef<HTMLParagraphElement>(null);
  useEffect(() => { said.current?.focus(); }, []);
  return (
    <AuthCard title="Check your email">
      <p ref={said} tabIndex={-1} className="mb-3 rounded-md border border-l-4 border-info-border bg-info-bg px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" data-testid="reset-sent">
        <span aria-hidden="true">ⓘ </span>{detail}
      </p>
      <p className="mb-4 text-sm">
        If <span className="font-semibold [overflow-wrap:anywhere]">{email}</span> signs in to Softwarden with a password, a link is on its way (within a minute or two).
        Accounts that sign in only through single sign-on have no password here: sign in through your identity provider instead.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button asChild variant="outline"><Link to="/login">Back to sign in</Link></Button>
        <Button type="button" variant="outline" onClick={onRestart}>Use a different email</Button>
      </div>
    </AuthCard>
  );
}

/** `/reset-password#<token>`: a new password; then sign in with it. */
export function ResetPasswordPage({ token, onReset }: { token: string; onReset: () => Promise<void> }) {
  const navigate = useNavigate();
  const [form, setForm] = useState({ password: "", confirm: "" });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [banner, setBanner] = useState("");
  const [gone, setGone] = useState(false);
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBanner("");
    const found: Record<string, string[]> = {};
    if (!form.password) found.password = ["Choose a new password of 12 characters or more."];
    else if (form.confirm !== form.password) found.confirm = ["The two passwords don't match."];
    if (Object.keys(found).length) return setErrors(found);
    setBusy(true);
    try {
      await withCsrf(() => typedApi("post", "auth/password-reset/confirm", { body: { token, password: form.password } }));
      // Every session of the account ended: this tab's too, if it was that account's.
      await onReset();
      navigate("/login", { replace: true, state: { notice: "Your password is changed and every session of your account was signed out. Sign in with your new password." } });
    } catch (err) {
      setBusy(false);
      if (err instanceof Problem && err.type === LINK_UNAVAILABLE) return setGone(true);
      if (err instanceof Problem && Object.keys(err.errors).length) return setErrors(err.errors);
      setErrors({});
      setBanner(refusalText(err));
    }
  }
  if (!token || gone) {
    return (
      <AuthCard title="This link can't be used">
        <p className="mb-2 text-sm" role={gone ? "alert" : undefined}>
          {token ? "This password reset link isn't valid any more." : "This page needs the link from your password reset email."}
          {" "}Links work once, for an hour, and a newer link or a password change replaces it.
        </p>
        <p className="mb-4 text-sm text-muted-foreground">Ask for a new link. Your password wasn't changed.</p>
        <div className="flex flex-col gap-2">
          <Button asChild variant="outline" className="w-full"><Link to="/forgot-password">Ask for a new link</Link></Button>
          <Button asChild variant="outline" className="w-full"><Link to="/login">Go to sign in</Link></Button>
        </div>
      </AuthCard>
    );
  }
  return (
    <AuthCard title="Choose a new password">
      <p className="mb-4 text-sm text-muted-foreground">Use at least 12 characters, not a common password. Every session of your account is signed out, and you sign in again with the new password.</p>
      <form onSubmit={submit} noValidate aria-label="Choose a new password">
        <ErrorSummary errors={errors} focusOnShow="field" />
        {banner && <p className={alertBox} role="alert">{banner}</p>}
        <Field id="password" label="New password (required)" errors={errors}>
          <Input id="password" type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
        </Field>
        <Field id="confirm" label="Confirm new password (required)" errors={errors}>
          <Input id="confirm" type="password" autoComplete="new-password" value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} />
        </Field>
        <Button type="submit" className="mt-1 w-full" aria-busy={busy || undefined}>{busy ? "Saving…" : "Save the new password"}</Button>
      </form>
    </AuthCard>
  );
}
