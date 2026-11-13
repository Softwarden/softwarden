/** The vendor page, `/vendors/:id`: what reminders about a vendor (custom dates, risk documents) and the
 *  Vendor risk queue link to. Tabs in `?tab=`: Overview (the vendor's details, custom fields and the risk summary from
 *  GET vendors/{id}), Risk (the vendor risk file, capability `vendor_risk`) and Attachments (capability `attachments`).
 *  Editors and Admins edit the details with the Vendors tab's form. Loaded on first use. */
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { Problem } from "@/api";
import { Breadcrumb, ErrorBanner, Loading, NotFound, PageHead, TabStrip } from "@/components/app";
import { DetailList, DetailRow, Panel, SuccessNote } from "@/components/sw";
import { Button } from "@/components/ui/button";
import { Modal, NamedForm, type Named } from "@/features/admin/admin";
import { AttachmentsTab, hasAttachments, useAttachmentsLabel } from "@/features/attachments/tab";
import { CustomFieldsPanel, useCustomFields } from "@/features/catalog/custom-fields";
import { useTypedQuery, type Schemas } from "@/lib/openapi";
import { problemText, useInvalidate } from "@/lib/query";
import { canWrite, useMe } from "@/lib/session";
import { AssuranceStatusChip, assuranceTypeLabel, RiskGaps, TierMark } from "./risk-ui";
import { RiskTab } from "./vendor-risk";

type Tab = "overview" | "risk" | "attachments";
type VendorDetail = Schemas["VendorDetail"];
const VENDORS = "/vendors";

const IconVendor = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round"><path d="M2.5 14V4.5l5-2.5v12M7.5 14V6.5l6 2V14" /><path d="M1.5 14h13M4.5 6.5h1M4.5 9h1M4.5 11.5h1M10 10.5h1M10 12.5h1" strokeLinecap="round" /></svg>;
const IconRisk = <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round"><path d="M8 1.8 13 3.6v3.7c0 3-2 5.4-5 6.3-3-.9-5-3.3-5-6.3V3.6z" /><path d="M8 5.5v3M8 10.5h.01" strokeLinecap="round" /></svg>;

export function VendorDetailPage() {
  const { id } = useParams();
  return <VendorPage key={id} id={id!} />;
}

function VendorPage({ id }: { id: string }) {
  const me = useMe();
  const write = canWrite(me);
  const [params, setParams] = useSearchParams();
  const load = useTypedQuery("vendors/{id}", { path: { id } });
  const attachmentsLabel = useAttachmentsLabel(me, "vendor", id);
  const riskOn = me.capabilities.includes("vendor_risk");
  const custom = useCustomFields("vendor");
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState(false);
  const [notice, setNotice] = useState("");
  const asked = params.get("tab") as Tab | null;
  const tab: Tab = asked === "risk" && riskOn ? "risk" : asked === "attachments" && attachmentsLabel ? "attachments" : "overview";
  // Other parameters (the Risk tab's ?replaced=hide) belong to their tab and go when it changes.
  const select = (t: Tab) => setParams(t === "overview" ? {} : { tab: t });
  const v = load.data;
  if (load.isError) {
    const missing = load.error instanceof Problem && load.error.status === 404;
    if (missing) return <NotFound what="vendor" back={{ to: VENDORS, label: "Vendors" }} trail={[{ to: VENDORS, label: "Vendors" }]} />;
    return (
      <>
        <Breadcrumb trail={[{ to: VENDORS, label: "Vendors" }]} current="Vendor" />
        <PageHead title={missing ? "Vendor not found" : "Couldn't load this vendor"} />
        <ErrorBanner message={missing ? "This vendor doesn't exist or you don't have access." : `${problemText(load.error)} Nothing was changed.`}
          onRetry={missing ? undefined : () => void load.refetch()} />
      </>
    );
  }
  if (!v) return <><Breadcrumb trail={[{ to: VENDORS, label: "Vendors" }]} current="Vendor" /><Loading what="the vendor" rows={3} /></>;
  const tabs: { id: Tab; label: string }[] = [
    { id: "overview", label: "Overview" },
    ...(riskOn ? [{ id: "risk" as const, label: v.risk?.gaps.length ? `Risk (${v.risk.gaps.length} ${v.risk.gaps.length === 1 ? "gap" : "gaps"})` : "Risk" }] : []),
    ...(attachmentsLabel ? [{ id: "attachments" as const, label: attachmentsLabel }] : []),
  ];
  const item: Named = v;
  return (
    <>
      <Breadcrumb trail={[{ to: VENDORS, label: "Vendors" }]} current={v.name} />
      <PageHead title={<span className="[overflow-wrap:anywhere]">{v.name}</span>} docTitle={v.name}
        desc={<span className="inline-flex flex-wrap items-center gap-2">
          <span>Vendor{v.usage ? ` · used by ${v.usage} ${v.usage === 1 ? "application or renewal" : "applications and renewals"}` : ""}</span>
          {v.risk && <TierMark tier={v.risk.risk_tier} />}
          {v.risk && v.risk.gaps.length > 0 && <RiskGaps gaps={v.risk.gaps} />}
        </span>}>
        {write && <Button variant="outline" onClick={() => setEditing(true)}>Edit vendor</Button>}
      </PageHead>
      <SuccessNote text={notice} />
      <TabStrip tabs={tabs} tab={tab} label="Vendor sections" onSelect={select} />
      <div role="tabpanel" id="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === "overview" && <Overview v={v} onOpenRisk={riskOn ? () => select("risk") : undefined} />}
        {tab === "risk" && <RiskTab vendor={{ id: v.id, name: v.name }} />}
        {tab === "attachments" && hasAttachments(me) && <AttachmentsTab parent={{ type: "vendor", id: v.id, name: v.name }} />}
      </div>
      <Modal title={`Edit ${v.name}`} open={editing} onClose={() => setEditing(false)}>
        {custom.fields
          ? <NamedForm path="vendors" kind="Vendors" item={item} customFields={custom.fields} onCancel={() => setEditing(false)}
            onDone={(text) => { setEditing(false); setNotice(text); void invalidate("vendors"); }} />
          : custom.error ? <ErrorBanner message={problemText(custom.error, "Couldn't load the custom fields.")} onRetry={custom.refetch} /> : <Loading what="the form" />}
      </Modal>
    </>
  );
}

const none = (text = "Not recorded") => <span className="text-muted-foreground">{text}</span>;
const external = (href: string, text: string, about: string) => (
  <a href={href} target="_blank" rel="noreferrer noopener" className="underline [overflow-wrap:anywhere]">{text}<span className="sr-only"> ({about}, opens in a new tab)</span></a>
);

function Overview({ v, onOpenRisk }: { v: VendorDetail; onOpenRisk?: () => void }) {
  const r = v.risk;
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Panel id="h-vendor-details" icon={IconVendor} title="Vendor details">
        <DetailList>
          <DetailRow label="Website">{v.website ? external(v.website, v.website, `website of ${v.name}`) : none()}</DetailRow>
          <DetailRow label="Customer portal">{v.portal_url ? external(v.portal_url, v.portal_url, `portal of ${v.name}`) : none()}</DetailRow>
          <DetailRow label="Support">
            {[v.support_email, v.support_phone].filter(Boolean).join(" · ") || (v.support_url ? null : none())}
            {v.support_url && <span className="block">{external(v.support_url, "Support site", `support site of ${v.name}`)}</span>}
          </DetailRow>
          <DetailRow label="Account manager">{v.account_manager ? <>{v.account_manager.name}{!v.account_manager.active && <span className="text-muted-foreground"> (inactive)</span>}</> : none("No account manager")}</DetailRow>
          <DetailRow label="Used by">{v.usage ? `${v.usage} ${v.usage === 1 ? "application or renewal" : "applications and renewals"}` : none("Nothing uses it yet")}</DetailRow>
          <DetailRow label="Notes">{v.notes ? <span className="whitespace-pre-wrap">{v.notes}</span> : none("None")}</DetailRow>
        </DetailList>
      </Panel>
      {r && (
        <Panel id="h-vendor-risk-summary" icon={IconRisk} title="Risk at a glance"
          action={onOpenRisk ? <Button size="sm" variant="outline" onClick={onOpenRisk}>Open the risk file</Button> : undefined}>
          <DetailList>
            <DetailRow label="Risk tier"><TierMark tier={r.risk_tier} /></DetailRow>
            <DetailRow label="Assurance report">
              <span className="inline-flex flex-wrap items-center gap-2">
                {r.assurance.type && assuranceTypeLabel(r.assurance.type)}
                <AssuranceStatusChip status={r.assurance.status} />
              </span>
              {r.assurance.expires_on && <span className="block text-sm text-muted-foreground">Expires {r.assurance.expires_on}</span>}
            </DetailRow>
            <DetailRow label="Documents in force">{r.documents.current} current · {r.documents.expiring} expiring · {r.documents.expired} expired</DetailRow>
            <DetailRow label="Next expiry">{r.next_expiry ? <span className="tabular">{r.next_expiry}</span> : none("No dated documents")}</DetailRow>
            <DetailRow label="Gaps">{r.gaps.length ? <RiskGaps gaps={r.gaps} /> : <span className="text-success-foreground"><span aria-hidden="true">✓ </span>No risk gaps</span>}</DetailRow>
          </DetailList>
          {!r.has_profile && <p className="border-t px-4 py-3 text-sm text-muted-foreground">No risk file yet. <Link to="?tab=risk" replace className="underline">Start it on the Risk tab</Link>.</p>}
        </Panel>
      )}
      <div className="lg:col-span-2"><CustomFieldsPanel kind="vendor" values={v.custom_field_data} /></div>
    </div>
  );
}
