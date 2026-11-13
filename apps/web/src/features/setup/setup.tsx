/** First-run setup: create the first organization and Admin with the one-time token that
 *  `manage.py bootstrap_admin` prints on the host. Public, outside the signed-in shell (API: core/setup_views.py). */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, Me, Problem } from "@/api";
import { HOME } from "@/features/auth/login";
import { AuthCard } from "@/features/auth/auth-card";
import { ErrorBanner, ErrorSummary, Field } from "@/components/app";
import { Combobox } from "@/components/combobox";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { zoneOptions } from "@/lib/timezones";

const BOOTSTRAP_COMMAND = "docker compose exec app python manage.py bootstrap_admin";
const MIN_PASSWORD = 12; // AUTH_PASSWORD_VALIDATORS in settings.py; the server also refuses common passwords
/** Form order: the first of these with an error gets focus after a failed submit. */
const FIELDS = ["token", "organization_name", "timezone", "name", "email", "password", "password_confirm"] as const;
type FieldName = (typeof FIELDS)[number];
type Form = Record<FieldName, string>;
type Errors = Record<string, string[]>;

/** The browser's IANA zone, or UTC when it has none. */
function browserZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}


function waitText(seconds: number) {
  if (seconds < 60) return `${seconds} ${seconds === 1 ? "second" : "seconds"}`;
  const minutes = Math.ceil(seconds / 60);
  return `${minutes} ${minutes === 1 ? "minute" : "minutes"}`;
}

/** Checks the browser can make before sending; the server's rules and messages still decide. */
function check(form: Form): Errors {
  const errors: Errors = {};
  if (!form.token) errors.token = ["Enter the setup token."];
  if (!form.organization_name.trim()) errors.organization_name = ["Enter the organization name."];
  if (!form.timezone) errors.timezone = ["Choose a timezone such as America/New_York."];
  if (!form.name.trim()) errors.name = ["Enter a display name."];
  if (!form.email.includes("@")) errors.email = ["Enter an email address."];
  if (form.password.length < MIN_PASSWORD) errors.password = [`Use at least ${MIN_PASSWORD} characters.`];
  if (form.password_confirm !== form.password) errors.password_confirm = ["The two passwords don't match."];
  return errors;
}

type Phase =
  | { kind: "loading" }
  | { kind: "load_error" }
  | { kind: "done"; fromSubmit?: boolean } // setup closed: not pending on load, or a 409 on submit
  | { kind: "form" };

export function Setup({ signedIn, onSignedIn }: { signedIn: boolean; onSignedIn: (me: Me) => void }) {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const load = () => {
    setPhase({ kind: "loading" });
    // The GET also sets the CSRF cookie the POST needs.
    api<{ pending: boolean }>("setup").then(
      (s) => setPhase({ kind: s.pending ? "form" : "done" }),
      () => setPhase({ kind: "load_error" }),
    );
  };
  useEffect(load, []);

  if (phase.kind === "loading") {
    return <AuthCard title="Set up Softwarden"><p className="text-muted-foreground" role="status">Checking whether setup is open…</p></AuthCard>;
  }
  if (phase.kind === "load_error") {
    return (
      <AuthCard title="Set up Softwarden">
        <ErrorBanner message="Couldn't reach the Softwarden server. Check that it is running." onRetry={load} />
      </AuthCard>
    );
  }
  if (phase.kind === "done") return <SetupDone signedIn={signedIn} focusHeading={!!phase.fromSubmit} />;
  return <SetupForm onSignedIn={onSignedIn} onClosed={() => setPhase({ kind: "done", fromSubmit: true })} />;
}

function SetupDone({ signedIn, focusHeading }: { signedIn: boolean; focusHeading: boolean }) {
  // After a 409 the form (and the focused button) is gone: put focus on the new heading rather than <body>.
  useEffect(() => { if (focusHeading) document.querySelector<HTMLElement>("#main h1")?.focus(); }, [focusHeading]);
  return (
    <AuthCard title="Setup is done">
      <p className="mb-4" role="status"><span aria-hidden="true">✓ </span>This Softwarden installation already has an organization and an Admin.</p>
      <p className="mb-4 text-sm text-muted-foreground">
        {signedIn ? "You are signed in." : "Sign in with your account."} An Admin who is locked out can be recovered on the server with{" "}
        <code className="font-mono">manage.py reset_admin</code>.
      </p>
      <Button asChild className="w-full">
        {signedIn ? <Link to="/">Go to Needs attention</Link> : <Link to="/login">Sign in</Link>}
      </Button>
    </AuthCard>
  );
}

/** A problem that isn't about one field: shown above the form. */
const NOTICE_ID = "setup-notice";
type Notice = { kind: "token" | "csrf" | "rate_limited" | "network" | "general"; text: string };

function SetupForm({ onSignedIn, onClosed }: { onSignedIn: (me: Me) => void; onClosed: () => void }) {
  const zone = useMemo(browserZone, []);
  const zones = useMemo(() => zoneOptions(zone), [zone]);
  const [form, setForm] = useState<Form>({ token: "", organization_name: "", timezone: zone, name: "", email: "", password: "", password_confirm: "" });
  const [errors, setErrors] = useState<Errors>({});
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0); // bumps on every failed submit so focus moves even when errors repeat
  const navigate = useNavigate();
  const set = (k: FieldName) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });

  // After a failed submit, focus the first field with an error (the token field for a refused token), or else the
  // notice, so focus never drops to <body>.
  const focusAfter = useRef<string | null>(null);
  useEffect(() => {
    const id = focusAfter.current;
    focusAfter.current = null;
    if (id) document.getElementById(id)?.focus();
  }, [attempt]);
  function fail(next: Errors, nextNotice: Notice | null = null) {
    setErrors(next);
    setNotice(nextNotice);
    focusAfter.current = FIELDS.find((f) => next[f]) ?? (nextNotice?.kind === "token" ? "token" : nextNotice ? NOTICE_ID : null);
    setAttempt((n) => n + 1);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    const local = check(form);
    if (Object.keys(local).length) return fail(local);
    setBusy(true);
    setNotice(null);
    try {
      const body = { token: form.token, organization_name: form.organization_name, timezone: form.timezone, name: form.name, email: form.email, password: form.password };
      const me = await api<Me>("setup", { method: "POST", body });
      // Django rotates the CSRF token at sign-in. Setup has already succeeded, so a failed refresh must not report an error.
      await api("auth/csrf").catch(() => undefined);
      onSignedIn(me);
      // Everyone's home is the dashboard.
      navigate(HOME, { replace: true });
      return;
    } catch (err) {
      if (!(err instanceof Problem) || err.status >= 500) {
        fail({}, { kind: "network", text: "Couldn't reach the Softwarden server. The setup may not have finished; check that it is running and try again." });
      } else if (err.status === 409) {
        onClosed();
      } else if (err.status === 429) {
        const wait = err.retryAfter ? `Try again in ${waitText(err.retryAfter)}.` : "Wait a few minutes and try again.";
        fail({}, { kind: "rate_limited", text: `Too many wrong setup tokens from this address. ${wait}` });
      } else if (err.status === 403 && /csrf/i.test(err.detail)) {
        await api("setup").catch(() => undefined); // sets a fresh CSRF cookie
        fail({}, { kind: "csrf", text: "The page's security check expired. Submit the form again." });
      } else if (err.status === 403) {
        fail({}, { kind: "token", text: "This setup token is wrong or has expired." });
      } else {
        const fieldErrors = { ...err.errors };
        const general = fieldErrors.non_field;
        delete fieldErrors.non_field;
        const hasFields = Object.keys(fieldErrors).length > 0;
        // A 400 with no field errors at all: its detail is the general notice.
        const text = general ? general.join(" ") : hasFields ? "" : err.detail;
        fail(fieldErrors, text ? { kind: "general", text } : null);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Set up Softwarden">
      <p className="mb-4 text-sm text-muted-foreground">Create your organization and its first Admin account. You can invite everyone else afterwards.</p>
      <form onSubmit={submit} noValidate aria-label="First-run setup" aria-busy={busy}>
        {notice && (
          <div id={NOTICE_ID} tabIndex={-1} className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 outline-none focus-visible:ring-2 focus-visible:ring-ring" role="alert">
            <p><span aria-hidden="true">! </span>{notice.text}</p>
            {notice.kind === "token" && (
              <p className="mt-1 text-sm">On the server, run <code className="font-mono">{BOOTSTRAP_COMMAND}</code> for the current token.</p>
            )}
          </div>
        )}
        <ErrorSummary errors={errors} />
        <Field id="token" label="Setup token" errors={errors}
          help={<>Run <code className="font-mono">{BOOTSTRAP_COMMAND}</code> on the server to print it. It expires one hour after it was made.</>}>
          <Input id="token" className="font-mono" autoComplete="off" autoCapitalize="off" spellCheck={false}
            value={form.token} onChange={(e) => setForm({ ...form, token: e.target.value.trim() })} />
        </Field>
        <Field id="organization_name" label="Organization name" errors={errors}>
          <Input id="organization_name" autoComplete="organization" value={form.organization_name} onChange={set("organization_name")} />
        </Field>
        <Field id="timezone" label="Timezone" errors={errors} help="Dates and deadlines are judged in this timezone.">
          <Combobox id="timezone" listLabel="Timezones" options={zones} value={form.timezone} onChange={(z) => setForm({ ...form, timezone: z })} />
        </Field>
        <Field id="name" label="Your name" errors={errors}>
          <Input id="name" autoComplete="name" value={form.name} onChange={set("name")} />
        </Field>
        <Field id="email" label="Your email" errors={errors} help="You sign in with it.">
          <Input id="email" type="email" autoComplete="username" value={form.email} onChange={set("email")} />
        </Field>
        <Field id="password" label="Password" errors={errors} help={`At least ${MIN_PASSWORD} characters, and not a common password.`}>
          <Input id="password" type="password" autoComplete="new-password" value={form.password} onChange={set("password")} />
        </Field>
        <Field id="password_confirm" label="Confirm password" errors={errors}>
          <Input id="password_confirm" type="password" autoComplete="new-password" value={form.password_confirm} onChange={set("password_confirm")} />
        </Field>
        <Button type="submit" className="mt-1 w-full aria-disabled:opacity-60" aria-disabled={busy || undefined}>{busy ? "Creating…" : "Create organization and Admin"}</Button>
      </form>
    </AuthCard>
  );
}
