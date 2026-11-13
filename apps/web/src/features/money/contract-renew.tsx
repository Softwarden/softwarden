/** Renew, renegotiate or cancel a contract: `/contracts/:id/renew`. The form starts
 *  from `GET /contracts/{id}/renew` (whether it can be decided now and why not, the renewal cycle the decision is
 *  recorded on, and the next term's defaults), so the screen never re-implements them. `POST` sends the contract's ETag
 *  as If-Match; renewing creates the next term and opens it, cancelling stays here. Refusals (409) are shown in the
 *  API's words; field errors (an overlap to confirm, a pool that can't be copied, a newly required custom field) are on
 *  their fields, so they can be fixed in place. */
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, apiWithEtag, AUTO_RENEW_LABEL, DECISION_LABEL, Problem, type AutoRenew, type Decision, type PeriodUnit } from "@/api";
import { Breadcrumb, ErrorBanner, ErrorSummary, Field, Loading, PageHead, Select, Textarea } from "@/components/app";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { CustomFieldInputs, useCustomFieldForm, useCustomFields, type CustomField } from "@/features/catalog/custom-fields";
import { PeriodField } from "@/features/money/renewals";
import { apiKey, problemText, useApiQuery, useInvalidate } from "@/lib/query";
import { canWrite, useMe } from "@/lib/session";
import { AMOUNT_HELP, AMOUNT_PATTERN, BILLING_FREQUENCIES, moneyText, percentText, type BillingFrequency, type ContractDetail, type RenewTerms } from "./contract-types";
import { Money, StaleBanner } from "./contract-ui";

export function RenewContract() {
  const { id } = useParams();
  return <RenewPage key={id} id={id!} />;
}

function RenewPage({ id }: { id: string }) {
  const me = useMe();
  // The contract with its ETag (If-Match on the POST) and the renew terms; neither cached, so the form starts fresh.
  const rec = useQuery({ queryKey: [...apiKey(`contracts/${id}`), "etag"], queryFn: () => apiWithEtag<ContractDetail>(`contracts/${id}`), gcTime: 0, retry: false, staleTime: Infinity });
  const terms = useApiQuery<RenewTerms>(`contracts/${id}/renew`, { gcTime: 0 });
  const custom = useCustomFields("contract");
  const [round, setRound] = useState(0);
  const back = `/contracts/${id}`;
  if (rec.error instanceof Problem && rec.error.status === 404) {
    return <><PageHead title="Contract not found" desc="This contract doesn't exist or you don't have access." /><Button asChild variant="outline"><Link to="/contracts">Back to Contracts</Link></Button></>;
  }
  const error = rec.error || terms.error || custom.error;
  if (error) return <ErrorBanner message={problemText(error, "Couldn't load the renew form.")} onRetry={() => { void rec.refetch(); void terms.refetch(); custom.refetch(); }} />;
  if (!rec.data || !terms.data || !custom.fields) return <Loading what="the renew form" rows={3} />;
  const c = rec.data.data;
  const trail = [{ to: "/contracts", label: "Contracts" }, { to: back, label: c.name }];
  if (!canWrite(me)) {
    return (
      <>
        <Breadcrumb trail={trail} current="Renew" />
        <PageHead title={`Renew ${c.name}`} docTitle="Renew contract" desc="Editors and Admins record renewal decisions. You can read the contract and its renewal." />
        <Button asChild variant="outline"><Link to={back}>Back to {c.name}</Link></Button>
      </>
    );
  }
  async function loadLatest() {
    await Promise.all([rec.refetch(), terms.refetch()]);
    setRound((n) => n + 1);
  }
  return (
    <>
      <Breadcrumb trail={trail} current="Renew" />
      <PageHead title={`Renew or cancel ${c.name}`} docTitle="Renew contract"
        desc={terms.data.renewal && terms.data.open_cycle
          ? `The decision is recorded on cycle ${terms.data.open_cycle.sequence} of the renewal ${terms.data.renewal.name} and completes it.`
          : "Not linked to a renewal: the decision is recorded on the contracts only."} />
      <RenewForm key={round} c={c} etag={rec.data.etag} terms={terms.data} fields={custom.fields} onLoadLatest={() => void loadLatest()} />
    </>
  );
}

type Next = {
  name: string; contract_number: string; po_number: string; amount: string; currency: string; billing_frequency: BillingFrequency;
  start_date: string; end_date: string; auto_renew: AutoRenew; notice_value: string; notice_unit: PeriodUnit; notes: string;
};

function RenewForm({ c, etag, terms, fields, onLoadLatest }: { c: ContractDetail; etag: string | null; terms: RenewTerms; fields: CustomField[]; onLoadLatest: () => void }) {
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const d = terms.defaults;
  const [decision, setDecision] = useState<Decision>(terms.can_renew ? "renew" : "cancel");
  const [decidedOn, setDecidedOn] = useState("");
  const [notes, setNotes] = useState("");
  const [evidence, setEvidence] = useState("");
  const [next, setNext] = useState<Next>({
    name: d.name, contract_number: d.contract_number, po_number: d.po_number, amount: d.amount ?? "", currency: d.currency,
    billing_frequency: d.billing_frequency, start_date: d.start_date ?? "", end_date: d.end_date ?? "", auto_renew: d.auto_renew,
    notice_value: d.notice_value != null ? String(d.notice_value) : "", notice_unit: (d.notice_unit || "days") as PeriodUnit, notes: d.notes,
  });
  const [lines, setLines] = useState<string[]>(d.lines);
  const [pools, setPools] = useState<string[]>(d.pools);
  const [poolAction, setPoolAction] = useState<"move" | "copy">(d.pool_action);
  const [overlap, setOverlap] = useState(d.overlap_confirmed);
  const cf = useCustomFieldForm(fields, "edit", c.custom_field_data);
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [refusal, setRefusal] = useState("");
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState(false);
  const renewing = decision !== "cancel";
  const set = <K extends keyof Next>(k: K) => (v: Next[K]) => setNext((cur) => ({ ...cur, [k]: v }));
  // The next term starts on or before this one's end: the API wants that confirmed (overlap_confirmed).
  const overlaps = renewing && !!next.start_date && !!c.end_date && next.start_date <= c.end_date;
  useEffect(() => { if (!overlaps && !errors.overlap_confirmed) setOverlap(false); }, [overlaps]); // eslint-disable-line react-hooks/exhaustive-deps

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const found: Record<string, string[]> = {};
    if (decision === "cancel" && !notes.trim()) found.decision_notes = ["Say why it is canceled: notes are required to cancel."];
    if (renewing) {
      if (!next.name.trim()) found.name = ["Enter a name for the next term."];
      if (next.amount.trim() && !AMOUNT_PATTERN.test(next.amount.trim())) found.amount = ["Enter the amount as a number with up to two decimal places, such as 12000 or 999.50."];
      if (!/^[A-Za-z]{3}$/.test(next.currency.trim())) found.currency = ["Enter a three-letter currency code, such as USD or EUR."];
      if (!next.end_date) found.end_date = ["Enter the next term's end date."];
    }
    const custom = renewing ? cf.build() : { data: undefined, errors: {} };
    Object.assign(found, custom.errors);
    setErrors(found);
    setRefusal("");
    setStale(false);
    if (Object.keys(found).length) return;
    const body: Record<string, unknown> = {
      decision, ...(notes.trim() ? { decision_notes: notes.trim() } : {}), ...(decidedOn ? { decided_on: decidedOn } : {}),
      ...(evidence.trim() ? { completion_evidence: evidence.trim() } : {}),
    };
    if (renewing) {
      Object.assign(body, {
        name: next.name.trim(), contract_number: next.contract_number.trim(), po_number: next.po_number.trim(), amount: next.amount.trim() || null,
        currency: next.currency.trim().toUpperCase(), billing_frequency: next.billing_frequency, auto_renew: next.auto_renew,
        notice_value: next.notice_value ? Number(next.notice_value) : null, notice_unit: next.notice_value ? next.notice_unit : "", notes: next.notes,
        ...(next.start_date ? { start_date: next.start_date } : {}), end_date: next.end_date,
        lines, pools, pool_action: poolAction, overlap_confirmed: overlap,
        ...(custom.data ? { custom_field_data: custom.data } : {}),
      });
    }
    setSaving(true);
    try {
      const saved = await api<ContractDetail>(`contracts/${c.id}/renew`, { method: "POST", body, headers: etag ? { "If-Match": etag } : undefined });
      await invalidate("contracts", "renewals", "license-pools");
      if (renewing) {
        navigate(`/contracts/${saved.id}`, { state: { notice: `${DECISION_LABEL[decision]}: the next term ${saved.name} was created${terms.renewal ? ` and the renewal ${terms.renewal.name} now takes its dates from it` : ""}. ${c.name} is kept as the previous term.` } });
      } else {
        navigate(`/contracts/${c.id}`, { state: { notice: `Canceled. The decision is recorded${terms.renewal ? ` on the renewal ${terms.renewal.name}, whose cycle is complete` : ""}.` } });
      }
    } catch (err) {
      setSaving(false);
      if (err instanceof Problem && err.status === 412) setStale(true);
      else if (err instanceof Problem && err.status === 409) setRefusal(err.detail);
      else if (err instanceof Problem && Object.keys(err.errors).length) setErrors(err.errors);
      else setRefusal(`${problemText(err)} Nothing was saved.`);
    }
  }

  const toggle = (list: string[], set: (v: string[]) => void, id: string) => set(list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);
  return (
    <>
      {stale && <StaleBanner what="contract" onLoad={() => { setStale(false); onLoadLatest(); }} />}
      {!terms.can_renew && (
        <div id="renew-refusal" className="mb-4 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-4 py-3 text-state-pending-foreground" role="note" data-testid="renew-refusal">
          <p className="font-semibold"><span aria-hidden="true">! </span>This contract can't be renewed now.</p>
          <p>{terms.refusal || "Renew and Renegotiate aren't available for it."} Renew and Renegotiate are turned off.</p>
        </div>
      )}
      {refusal && <ErrorBanner message={refusal} />}
      <Card className="max-w-4xl">
        <CardContent className="pt-6">
          <form onSubmit={submit} noValidate aria-label="Renew or cancel">
            <fieldset disabled={saving} className="m-0 min-w-0 border-0 p-0">
              <ErrorSummary errors={errors} focusOnShow="field" />
              <fieldset className="mb-4">
                <legend className="mb-1.5 font-display text-section font-semibold">Decision</legend>
                <div className="flex flex-col gap-2">
                  {([["renew", "Renew: the next term repeats this one (change anything below)"], ["renegotiate", "Renegotiate: a next term with new terms"], ["cancel", "Cancel: no next term; this contract becomes Canceled"]] as const).map(([value, label], i) => {
                    // A contract that can't be renewed now (already renewed, cancelled, archived, no end date) offers only
                    // Cancel; the refusal note above says why, and the disabled choices point to it.
                    const off = value !== "cancel" && !terms.can_renew;
                    return (
                      <label key={value} className={`flex min-h-6 items-center gap-2 text-sm ${off ? "text-muted-foreground" : ""}`}>
                        <input type="radio" id={i === 0 ? "decision" : undefined} name="decision" value={value} checked={decision === value} onChange={() => setDecision(value)}
                          disabled={off} aria-describedby={off ? "renew-refusal" : undefined} className="h-4 w-4 accent-primary" />
                        {label}{off && <span className="sr-only"> (not available now)</span>}
                      </label>
                    );
                  })}
                </div>
              </fieldset>
              <div className="grid gap-x-6 md:grid-cols-[200px_1fr]">
                <Field id="decided_on" label="Decided on" errors={errors} help="Defaults to today; not in the future.">
                  <Input id="decided_on" type="date" value={decidedOn} onChange={(e) => setDecidedOn(e.target.value)} />
                </Field>
                <Field id="decision_notes" label={decision === "cancel" ? "Notes (required to cancel)" : "Notes"} errors={errors} help="Why, and anything agreed with the vendor.">
                  <Textarea id="decision_notes" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
                </Field>
                <div className="md:col-span-2">
                  <Field id="completion_evidence" label="Completion evidence" errors={errors}
                    help={terms.renewal ? `Completes cycle ${terms.open_cycle?.sequence ?? ""} of ${terms.renewal.name}. Leave blank to name the ${renewing ? "next term" : "cancellation"}.` : "Optional: a PO number or the email confirming it."}>
                    <Input id="completion_evidence" value={evidence} onChange={(e) => setEvidence(e.target.value)} autoComplete="off" />
                  </Field>
                </div>
              </div>

              {renewing && terms.can_renew && (
                <>
                  <fieldset className="mt-2 border-t pt-4">
                    <legend className="mb-1 font-display text-section font-semibold">Next term</legend>
                    <p className="mb-3 text-sm text-muted-foreground">Filled in from this contract: it starts the day after {c.end_date ?? "this one ends"} and runs as long. It is Signed until it starts.</p>
                    <div className="grid gap-x-6 md:grid-cols-2">
                      <div className="md:col-span-2">
                        <Field id="name" label="Name (required)" errors={errors}>
                          <Input id="name" value={next.name} onChange={(e) => set("name")(e.target.value)} autoComplete="off" />
                        </Field>
                      </div>
                      <Field id="contract_number" label="Contract number" errors={errors} help="Not copied: a new term usually has its own.">
                        <Input id="contract_number" value={next.contract_number} onChange={(e) => set("contract_number")(e.target.value)} autoComplete="off" />
                      </Field>
                      <Field id="po_number" label="PO number" errors={errors}>
                        <Input id="po_number" value={next.po_number} onChange={(e) => set("po_number")(e.target.value)} autoComplete="off" />
                      </Field>
                      <Field id="amount" label="Amount" errors={errors} help={`${AMOUNT_HELP[next.billing_frequency]} This term: ${moneyText(c.amount, c.currency) ?? "no amount"}.`}>
                        <Input id="amount" inputMode="decimal" value={next.amount} onChange={(e) => set("amount")(e.target.value.replace(/[^0-9.]/g, ""))} autoComplete="off" />
                      </Field>
                      <div className="grid grid-cols-[6rem_1fr] gap-x-4">
                        <Field id="currency" label="Currency" errors={errors}>
                          <Input id="currency" value={next.currency} maxLength={3} onChange={(e) => set("currency")(e.target.value.toUpperCase())} autoComplete="off" className="uppercase" />
                        </Field>
                        <Field id="billing_frequency" label="Billing frequency" errors={errors}>
                          <Select id="billing_frequency" value={next.billing_frequency} onChange={(e) => set("billing_frequency")(e.target.value as BillingFrequency)}>
                            {BILLING_FREQUENCIES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
                          </Select>
                        </Field>
                      </div>
                      <Field id="start_date" label="Start date" errors={errors}>
                        <Input id="start_date" type="date" value={next.start_date} onChange={(e) => set("start_date")(e.target.value)} />
                      </Field>
                      <Field id="end_date" label="End date (required)" errors={errors} help={`After this term's end date (${c.end_date ?? "not set"}).`}>
                        <Input id="end_date" type="date" value={next.end_date} onChange={(e) => set("end_date")(e.target.value)} />
                      </Field>
                      {(overlaps || errors.overlap_confirmed) && (
                        <div className="md:col-span-2 mb-3 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-state-pending-foreground">
                          <label className="flex min-h-6 items-start gap-2 text-sm">
                            <input type="checkbox" id="overlap_confirmed" checked={overlap} onChange={(e) => setOverlap(e.target.checked)} className="mt-0.5"
                              aria-describedby={errors.overlap_confirmed ? "overlap_confirmed-error" : undefined} aria-invalid={errors.overlap_confirmed ? true : undefined} />
                            <span>The next term starts on or before {c.end_date}, while this one still runs. Both terms will overlap; I confirm that's intended.</span>
                          </label>
                          {errors.overlap_confirmed && <p id="overlap_confirmed-error" className="mt-1 text-sm text-error-foreground">{errors.overlap_confirmed.join(" ")}</p>}
                        </div>
                      )}
                      <fieldset className="mb-3 flex min-w-0 flex-col gap-1.5">
                        <legend className="mb-1.5 text-sm font-medium leading-none">Auto-renews</legend>
                        <div className="flex flex-wrap items-center gap-5">
                          {(["yes", "no", "unknown"] as const).map((x, i) => (
                            <label key={x} className="flex min-h-6 items-center gap-2 text-sm">
                              <input type="radio" id={i === 0 ? "auto_renew" : undefined} name="auto_renew" value={x} checked={next.auto_renew === x} onChange={() => set("auto_renew")(x)} className="h-4 w-4 accent-primary" />
                              {AUTO_RENEW_LABEL[x]}
                            </label>
                          ))}
                        </div>
                        {errors.auto_renew && <span className="text-sm text-error-foreground">{errors.auto_renew.join(" ")}</span>}
                      </fieldset>
                      <PeriodField id="notice" label="Notice period" errors={errors} value={next.notice_value} unit={next.notice_unit} onValue={set("notice_value")} onUnit={set("notice_unit")} />
                      <div className="md:col-span-2">
                        <Field id="notes" label="Notes on the next term" errors={errors}>
                          <Textarea id="notes" rows={2} value={next.notes} onChange={(e) => set("notes")(e.target.value)} />
                        </Field>
                      </div>
                    </div>
                  </fieldset>

                  <fieldset className="mt-2 border-t pt-4" aria-describedby={errors.lines ? "lines-error" : undefined}>
                    <legend className="mb-1 font-display text-section font-semibold">Lines to carry</legend>
                    {c.lines.length === 0 ? <p className="text-sm text-muted-foreground">This contract has no lines.</p> : (
                      <ul className="flex flex-col gap-1.5">
                        {c.lines.map((l, i) => (
                          <li key={l.id}>
                            <label className="flex min-h-6 items-center gap-2 text-sm">
                              <input type="checkbox" id={i === 0 ? "lines" : undefined} checked={lines.includes(l.id)} onChange={() => toggle(lines, setLines, l.id)} />
                              {l.record.name}<span className="text-muted-foreground">· {l.allocation_percent != null ? `fixed ${percentText(l.allocation_percent)}%` : "shares the rest"}</span>
                            </label>
                          </li>
                        ))}
                      </ul>
                    )}
                    {errors.lines && <p id="lines-error" className="mt-1 text-sm text-error-foreground">{errors.lines.join(" ")}</p>}
                  </fieldset>

                  <fieldset className="mt-4 border-t pt-4" aria-describedby={errors.pools ? "pools-error" : undefined}>
                    <legend className="mb-1 font-display text-section font-semibold">License pools</legend>
                    {c.license_pools.length === 0 ? <p className="text-sm text-muted-foreground">No license pools were bought under this contract.</p> : (
                      <>
                        <ul className="mb-3 flex flex-col gap-1.5">
                          {c.license_pools.map((p, i) => (
                            <li key={p.id}>
                              <label className="flex min-h-6 items-center gap-2 text-sm">
                                <input type="checkbox" id={i === 0 ? "pools" : undefined} checked={pools.includes(p.id)} onChange={() => toggle(pools, setPools, p.id)} />
                                {p.sku_name}<span className="text-muted-foreground">· {p.application.name} · {p.seats_purchased} seats</span>
                              </label>
                            </li>
                          ))}
                        </ul>
                        <fieldset>
                          <legend className="mb-1.5 text-sm font-medium">The chosen pools</legend>
                          <div className="flex flex-col gap-1.5">
                            <label className="flex min-h-6 items-center gap-2 text-sm">
                              <input type="radio" id="pool_action" name="pool_action" checked={poolAction === "move"} onChange={() => setPoolAction("move")} className="h-4 w-4 accent-primary" />
                              Move to the next term (seats are counted once)
                            </label>
                            <label className="flex min-h-6 items-center gap-2 text-sm">
                              <input type="radio" name="pool_action" checked={poolAction === "copy"} onChange={() => setPoolAction("copy")} className="h-4 w-4 accent-primary" />
                              Copy to the next term (copies don't sync seat counts)
                            </label>
                          </div>
                        </fieldset>
                      </>
                    )}
                    {errors.pools && <p id="pools-error" className="mt-1 text-sm text-error-foreground">{errors.pools.join(" ")}</p>}
                  </fieldset>

                  <div className="grid gap-x-6 md:grid-cols-2">
                    <CustomFieldInputs form={cf} errors={errors} />
                  </div>
                </>
              )}
              {!renewing && (
                <p className="mt-2 border-t pt-4 text-sm">
                  Canceling sets {c.name} to Canceled{c.annualized_cost ? <> (<Money amount={c.annualized_cost} currency={c.currency} /> a year stops counting in spend)</> : ""}. No next term is created; license pools stay where they are.
                </p>
              )}
              <div className="mt-4 flex flex-wrap justify-end gap-2 border-t pt-4">
                <Button asChild variant="outline"><Link to={`/contracts/${c.id}`}>Back without deciding</Link></Button>
                <Button type="submit" id="renew-submit" variant={decision === "cancel" ? "destructive" : "default"}>
                  {saving ? "Saving…" : decision === "cancel" ? `Cancel ${c.name}` : `${DECISION_LABEL[decision]} and create the next term`}
                </Button>
              </div>
            </fieldset>
          </form>
        </CardContent>
      </Card>
    </>
  );
}
