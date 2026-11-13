/** The platform console's second factor (docs/API.md "Second factor"). With
 *  `SOFTWARDEN_PLATFORM_TOTP` on (the default in cloud mode) a staff session that signed in with its password reaches
 *  only `platform/session` and `platform/second-factor/*` until it sends a current code from an authenticator app:
 *  - not enrolled yet (the first console sign-in): show the setup key once (base32, and the otpauth:// address an
 *    authenticator app takes), then confirm it with the first code;
 *  - enrolled: ask for the current code.
 *  A wrong or used code is a field error; five wrong ones in a row wait five minutes (429). A lost device is the
 *  operator's to reset (`manage.py platform_staff reset-second-factor`). The secret is shown here once, kept in this
 *  component only, and never stored, logged or sent anywhere. */
import { useEffect, useRef, useState } from "react";
import { Problem } from "@/api";
import { ErrorSummary, Field, PageHead } from "@/components/app";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { typedApi, type Schemas } from "@/lib/openapi";

export type SecondFactorState = Schemas["SecondFactorState"];

/** Whether the console is closed until a code is sent in this session. */
export const needsCode = (state: SecondFactorState | undefined) => !!state && state.required && !state.verified;

/** "5 minutes", from a 429's Retry-After (seconds). */
function waitText(seconds?: number) {
  if (!seconds) return "a few minutes";
  if (seconds < 120) return `${seconds} ${seconds === 1 ? "second" : "seconds"}`;
  const m = Math.ceil(seconds / 60);
  return `${m} ${m === 1 ? "minute" : "minutes"}`;
}

/** A refused code: the field error, or the sentence for a refusal that isn't one. */
export function codeErrors(err: unknown): Record<string, string[]> {
  if (err instanceof Problem && err.status === 429) return { code: [`Too many wrong codes. Wait ${waitText(err.retryAfter)}, then enter the current code.`] };
  if (err instanceof Problem && err.errors.code) return { code: err.errors.code };
  if (err instanceof Problem) return { code: [err.detail] };
  return { code: ["Softwarden didn't answer. Check your connection and try again."] };
}

/** The base32 secret in groups of four, as authenticator apps show it. */
const grouped = (secret: string) => secret.replace(/(.{4})(?=.)/g, "$1 ");

/** Six digits, as the app shows them (spaces allowed while typing). */
const cleanCode = (code: string) => code.replace(/\s+/g, "");

export function SecondFactorGate({ state, onVerified }: { state: SecondFactorState; onVerified: () => Promise<unknown> }) {
  const [enrollment, setEnrollment] = useState<Schemas["SecondFactorEnrollment"] | null>(null);
  const [code, setCode] = useState("");
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [banner, setBanner] = useState("");
  const [busy, setBusy] = useState(false);
  const keyBox = useRef<HTMLDivElement>(null);
  useEffect(() => { if (enrollment) keyBox.current?.focus(); }, [enrollment]);
  const enrolling = !state.enrolled;

  async function showKey() {
    if (busy) return;
    setBusy(true);
    setBanner("");
    try {
      setEnrollment(await typedApi("post", "platform/second-factor/enrollment", {}));
    } catch (err) {
      // 409: set up meanwhile (another tab, or the operator from the host): ask for a code instead.
      if (err instanceof Problem && err.status === 409) await onVerified();
      else setBanner(err instanceof Problem ? err.detail : "Softwarden didn't answer. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  async function verify(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    const value = cleanCode(code);
    if (!/^\d{6}$/.test(value)) return setErrors({ code: ["Enter the 6-digit code your authenticator app shows now."] });
    setBusy(true);
    setErrors({});
    try {
      await typedApi("post", "platform/second-factor/verify", { body: { code: value } });
      await onVerified();
    } catch (err) {
      setBusy(false);
      setErrors(codeErrors(err));
    }
  }

  const form = (
    <form onSubmit={verify} noValidate aria-label={enrolling ? "Confirm your second factor" : "Enter your code"} className="max-w-sm">
      <ErrorSummary errors={errors} focusOnShow="field" />
      <Field id="code" label="6-digit code (required)" errors={errors} help={enrolling ? "The code your authenticator app shows for Softwarden now." : "From your authenticator app. Each code works once."}>
        <Input id="code" inputMode="numeric" autoComplete="one-time-code" maxLength={8} className="w-40 font-mono tabular" value={code} onChange={(e) => setCode(e.target.value)} />
      </Field>
      <Button type="submit" aria-busy={busy || undefined}>{busy ? "Checking…" : enrolling ? "Confirm and open the console" : "Verify"}</Button>
    </form>
  );

  return (
    <>
      <PageHead title={enrolling ? "Set up your second factor" : "Enter your authenticator code"}
        desc={enrolling
          ? "The platform console needs a code from an authenticator app every time you sign in, and again to impersonate a member. Set it up once, now."
          : "The platform console needs a current code from your authenticator app for this session."} />
      <div className="max-w-prose rounded-lg border bg-card px-4 py-5">
        {banner && <p className="mb-3 rounded-md border border-l-4 border-error-border bg-error-bg px-3 py-2 text-sm" role="alert">{banner}</p>}
        {enrolling && !enrollment && (
          <>
            <p className="mb-3">You need an authenticator app (on a phone or a password manager) that supports time-based codes. Softwarden shows a setup key once; add it to the app, then enter the code the app shows.</p>
            <Button type="button" aria-busy={busy || undefined} onClick={() => void showKey()}>{busy ? "Preparing…" : "Show the setup key"}</Button>
          </>
        )}
        {enrolling && enrollment && (
          <>
            <div ref={keyBox} tabIndex={-1} className="mb-4 rounded-md border border-l-4 border-info-border bg-info-bg px-3 py-2 outline-none focus-visible:ring-2 focus-visible:ring-ring" data-testid="setup-key">
              <p className="mb-1 font-semibold"><span aria-hidden="true">ⓘ </span>Shown this once. Add it to your app now.</p>
              <p className="mb-1 text-sm">If you leave before confirming, a new key replaces this one: remove this one from your app then.</p>
              <p className="text-sm">Setup key (choose time-based, 6 digits, 30 seconds):</p>
              <p className="my-1 font-mono text-lg [overflow-wrap:anywhere]" data-testid="setup-secret">{grouped(enrollment.secret)}</p>
              <p className="text-sm">Or, on the device with the app, open this address: <a href={enrollment.otpauth_uri} className="font-mono text-xs [overflow-wrap:anywhere]">{enrollment.otpauth_uri}</a></p>
            </div>
            {form}
          </>
        )}
        {!enrolling && form}
        <p className="mt-4 text-sm text-muted-foreground">Lost the device? Ask the operator to reset your second factor (<code className="font-mono">manage.py platform_staff reset-second-factor</code>); you set it up again at your next sign-in.</p>
      </div>
    </>
  );
}
