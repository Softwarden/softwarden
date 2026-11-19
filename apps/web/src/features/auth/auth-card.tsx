/** The card shown outside the shell (sign-in, setup, invitation, signup) and the forced password change: a small module
 *  of its own, because the sign-in page and the app shell load it up front while the Admin pages are lazy. */
import { useEffect, useState } from "react";
import { api, Me, tooManyAttempts } from "@/api";
import { attempt, ErrorSummary, Field } from "@/components/app";
import { BrandMark, Wordmark } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

/** Centered card with the brand mark, for screens shown outside the shell. */
export function AuthCard({ title, children }: { title: string; children: React.ReactNode }) {
  useEffect(() => { document.title = `${title} · Softwarden`; }, [title]);
  return (
    <main id="main" className="grid min-h-screen place-items-center bg-background p-4">
      <div className="w-full max-w-sm">
        <div className="mb-5 flex items-center justify-center gap-2.5">
          <BrandMark className="h-8 w-8" />
          <Wordmark className="text-lg" />
        </div>
        <Card className="py-0"><CardContent className="p-6">
          <h1 tabIndex={-1} className="mb-1 font-display text-title font-bold text-text-strong outline-none">{title}</h1>
          {children}
        </CardContent></Card>
      </div>
    </main>
  );
}

// ---- Forced password change ----------------------------------------------------------------

export function ChangePassword({ me, onChanged }: { me: Me; onChanged: () => void }) {
  const [form, setForm] = useState({ current_password: "", new_password: "", confirm: "" });
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (form.new_password !== form.confirm) return setErrors({ confirm: ["The two new passwords don't match."] });
    const failed = await attempt(() => api("auth/password", { method: "POST", body: { current_password: form.current_password, new_password: form.new_password } }));
    if (failed?.status === 429) return setErrors({ current_password: [tooManyAttempts(failed.retryAfter)] });
    if (failed) return setErrors(Object.keys(failed.errors).length ? failed.errors : { current_password: [failed.detail] });
    await api("auth/csrf");
    onChanged();
  }
  return (
    <AuthCard title="Choose a new password">
      <p className="mb-4 text-sm text-muted-foreground">{me.user.email}: your temporary password must be replaced before you continue. Use at least 12 characters.</p>
      <form onSubmit={submit} noValidate>
        <ErrorSummary errors={errors} />
        <Field id="current_password" label="Temporary password" errors={errors}><Input id="current_password" type="password" autoComplete="current-password" value={form.current_password} onChange={(e) => setForm({ ...form, current_password: e.target.value })} /></Field>
        <Field id="new_password" label="New password" errors={errors}><Input id="new_password" type="password" autoComplete="new-password" value={form.new_password} onChange={(e) => setForm({ ...form, new_password: e.target.value })} /></Field>
        <Field id="confirm" label="Confirm new password" errors={errors}><Input id="confirm" type="password" autoComplete="new-password" value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} /></Field>
        <Button type="submit" className="w-full">Save password</Button>
      </form>
    </AuthCard>
  );
}
