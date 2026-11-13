/** An application's SSO and MFA coverage panel (capability `discovery.entra`), on its Overview, loaded
 *  on first use. It shows the detail's `sso_coverage` block: the gap chips (as the ownership gaps are shown), single
 *  sign-on, SCIM and MFA each with the reason and when it was read, the Entra app it is matched to, the conditional
 *  access policies that cover or may cover it with their effect (report-only and blocking ones named), and "Source
 *  differs" where the record's own SSO or SCIM field says otherwise. Read-only for every role: coverage comes from the
 *  source's runs, and a difference is applied by editing the record. */
import { Link } from "react-router-dom";
import { chip, DetailList, DetailRow, GapChips, Panel } from "@/components/sw";
import { orgDate } from "@/components/org-banners";
import { editPath } from "@/lib/kinds";
import { useMe } from "@/lib/session";
import { CoverageChip, EffectChip, linkedText, lookOf, MFA, SCIM, SSO, yesNo, type Coverage } from "./coverage-ui";

const IconShieldCheck = (
  <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M8 1.5 2.5 3.5v4c0 3.2 2.3 5.9 5.5 7 3.2-1.1 5.5-3.8 5.5-7v-4L8 1.5Z" /><path d="m5.5 8 1.75 1.75L10.5 6.5" />
  </svg>
);

export function CoveragePanel({ id, name, block, write }: { id: string; name: string; block: Coverage; write: boolean }) {
  const me = useMe();
  const tz = me.organization.timezone;
  const read = (iso: string) => <span className="tabular">{orgDate(iso, tz)}</span>;
  const checked = (iso: string | null) => (iso ? <>Checked {read(iso)}</> : "Not checked yet");
  // Nothing read for this app yet (not linked to an Entra app, no coverage source, or not seen by a run): the three
  // dimensions would each say "unknown" with the same reason, so the panel says it once.
  if (!block.connector && !block.seen_at) {
    const linked = block.linked_by === "entra_app_id" || block.linked_by === "candidate";
    return (
      <Panel id="h-coverage" icon={IconShieldCheck} title="Single sign-on and MFA" note="From Microsoft Entra ID; read-only">
        <div className="px-4 py-3 text-sm" data-testid="coverage-empty">
          <p className="font-medium">{linked ? "No coverage read yet" : "Not linked to an Entra application"}</p>
          <p className="mt-1 text-muted-foreground">
            {linked
              ? block.sso_detail || "Single sign-on, SCIM and MFA show here after the next coverage run."
              : "Single sign-on, SCIM and MFA coverage is read from Microsoft Entra ID once this application is matched to its Entra app: set its Entra application ID, or link its Entra candidate in Discovery."}
          </p>
          {!linked && (
            <p className="mt-2">
              {write
                ? <Link to={`${editPath("application", id)}#entra_app_id`} className="inline-block min-h-6 leading-6">Link an Entra application<span className="sr-only"> to {name}</span></Link>
                : <span className="text-muted-foreground">Editors and Admins can link an Entra application.</span>}
            </p>
          )}
        </div>
      </Panel>
    );
  }
  return (
    <Panel id="h-coverage" icon={IconShieldCheck} title="Single sign-on and MFA" note="From Microsoft Entra ID; read-only">
      <div className="flex flex-wrap items-center gap-1.5 border-b px-4 py-3" data-testid="coverage-gaps">
        <span className="mr-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Coverage</span>
        {block.gaps.length
          ? <><span className="mr-1 text-sm">{block.gaps.length} {block.gaps.length === 1 ? "gap" : "gaps"}:</span><GapChips gaps={block.gaps} /></>
          : <span className="text-sm text-state-confirmed-foreground"><span aria-hidden="true">✓ </span>No coverage gaps</span>}
      </div>
      <DetailList>
        <DetailRow label="Single sign-on">
          <span data-testid="panel-sso"><CoverageChip look={lookOf(SSO, block.sso)} /></span>
          {block.sso_detail && <span className="mt-1 block text-sm text-muted-foreground">{block.sso_detail}</span>}
          {block.sso_mode && <span className="block text-sm text-muted-foreground">Mode in Entra: <code className="font-mono text-xs">{block.sso_mode}</code></span>}
          {block.seen_at && <span className="block text-sm text-muted-foreground">Read {read(block.seen_at)}</span>}
        </DetailRow>
        <DetailRow label="SCIM provisioning">
          <span data-testid="panel-scim"><CoverageChip look={lookOf(SCIM, block.scim)} /></span>
          {block.scim_detail && <span className="mt-1 block text-sm text-muted-foreground">{block.scim_detail}</span>}
          <span className="block text-sm text-muted-foreground">{checked(block.scim_checked_at)}</span>
        </DetailRow>
        <DetailRow label="MFA">
          <span data-testid="panel-mfa"><CoverageChip look={lookOf(MFA, block.mfa)} /></span>
          {block.mfa_detail && <span className="mt-1 block text-sm text-muted-foreground">{block.mfa_detail}</span>}
          <span className="block text-sm text-muted-foreground">{checked(block.mfa_checked_at)}</span>
        </DetailRow>
        <DetailRow label="Entra application">
          {block.entra_app_id ? <code className="font-mono text-[13px] [overflow-wrap:anywhere]">{block.entra_app_id}</code> : <span className="text-muted-foreground">None</span>}
          <span className="block text-sm text-muted-foreground">{linkedText(block)}{block.connector ? `, read by ${block.connector.name}` : ""}</span>
        </DetailRow>
      </DetailList>
      <div className="border-t px-4 py-3">
        <h3 className="mb-1 text-sm font-semibold">Conditional access policies{block.policies.length ? ` (${block.policies.length})` : ""}</h3>
        {block.policies.length === 0
          ? <p className="text-sm text-muted-foreground">No policy covers this app{block.mfa === "unknown" ? " that could be read" : ""}.</p>
          : (
            <ul className="grid gap-2" data-testid="coverage-policies">
              {block.policies.map((p) => (
                <li key={p.id} className="text-sm" data-policy={p.name}>
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold">{p.name}</span>
                    <EffectChip effect={p.effect} />
                  </span>
                  <span className="block text-muted-foreground">
                    {p.state === "enabledForReportingButNotEnforced" ? "Report-only" : "On"}
                    {"; "}{p.applies === "maybe" ? "may apply to this app (an application filter or app group)" : "applies to this app"}
                    {"; "}{p.users === "all" ? "all users" : "some users"}
                    {p.exclusions > 0 && `, ${p.exclusions} ${p.exclusions === 1 ? "exclusion" : "exclusions"}`}
                  </span>
                  {p.why && <span className="block text-muted-foreground">{p.why}</span>}
                </li>
              ))}
            </ul>
          )}
      </div>
      {block.differs.length > 0 && (
        <div className="border-t px-4 py-3" data-testid="coverage-differs">
          <p className="text-sm">
            <span className={chip({ state: "pending" })}><span aria-hidden="true">◐</span>Source differs</span>{" "}
            Entra says {block.differs.length === 1 ? "one field" : `${block.differs.length} fields`} differently. This record keeps its values unless you edit it.
          </p>
          <dl className="mt-1 grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[max-content_1fr]">
            {block.differs.map((d) => (
              <div key={d.field} className="contents">
                <dt className="font-medium">{d.label}</dt>
                <dd>“{yesNo(d.source)}” in Entra; {d.curated === null ? "not recorded" : `“${yesNo(d.curated)}”`} here</dd>
              </div>
            ))}
          </dl>
          <p className="mt-1 text-sm text-muted-foreground">
            {write ? <Link className="underline" to={editPath("application", id)}>Edit {name} to apply a difference</Link> : "Editors and Admins can edit the record to apply a difference."}
          </p>
        </div>
      )}
    </Panel>
  );
}
