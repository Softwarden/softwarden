/** A connector's scope, per source: the keys each source's SCOPE_DEFAULTS declares, as checkboxes and
 *  one-per-line lists. The API validates them; its errors come back as `scope.<key>` and show on the field. Shared by
 *  the walkthrough's Scope step and the connector settings page; viewers get ScopeSummary. */
import type { ConnectorScope, DiscoverySourceId } from "@/api";
import { Field, Textarea } from "@/components/app";
import { DetailList, DetailRow } from "@/components/sw";

type Spec = { key: string; kind: "bool" | "lines"; label: string; help?: string; warning?: string; empty?: string };

const NAMES: Spec = { key: "name_exclusions", kind: "lines", label: "Skip names starting with", help: "One name or prefix per line, for example: Test. Skipped objects are counted in each run, not stored.", empty: "None" };

export const SCOPE_FIELDS: Record<DiscoverySourceId, Spec[]> = {
  entra: [
    { key: "exclude_microsoft", kind: "bool", label: "Skip apps Microsoft publishes into every tenant (first-party)" },
    { key: "exclude_disabled", kind: "bool", label: "Skip disabled applications" },
    { key: "include_agent_identities", kind: "bool", label: "Also list Entra Agent ID agent identities", help: "Needs the optional permission AgentIdentity.Read.All." },
    { key: "first_party_tenants", kind: "lines", label: "Tenants whose apps count as Microsoft first-party", help: "One tenant ID (a GUID) per line. Apps whose publisher tenant is listed are skipped while the first box is ticked.", empty: "None" },
    NAMES,
  ],
  entra_licenses: [
    { key: "sku_exclusions", kind: "lines", label: "Skip SKUs whose part number starts with", help: "One SKU part number or prefix per line, for example: FLOW_FREE.", empty: "None" },
  ],
  azure: [
    { key: "include_logic_apps", kind: "bool", label: "Logic Apps" },
    { key: "include_function_apps", kind: "bool", label: "Function Apps" },
    { key: "include_app_services", kind: "bool", label: "App Services (web apps that aren't function apps)" },
    { key: "include_runbooks", kind: "bool", label: "Automation runbooks" },
    { key: "subscriptions", kind: "lines", label: "Subscriptions", help: "One subscription ID (a GUID) per line. Leave empty to read every subscription the app has Reader on; Reader on a management group covers the subscriptions under it.", empty: "Every subscription the app can read" },
    NAMES,
  ],
  powerplatform: [
    { key: "include_cloud_flows", kind: "bool", label: "Also read cloud flows", help: "Needs read on Process (workflow) in the application user's role." },
    { key: "all_environments", kind: "bool", label: "Also list every environment through the Power Platform admin API",
      warning: "Opt-in. The app's registration for the admin API is admin-equivalent: Microsoft treats it like a Power Platform Administrator and roles can't limit it. Listing environments by URL is the least-privilege way." },
    { key: "environment_urls", kind: "lines", label: "Dataverse environment URLs", help: "One per line, such as https://contoso.crm.dynamics.com (Power Platform admin center, Environment, Environment URL).", empty: "None" },
    NAMES,
  ],
  halo: [
    { key: "exclude_inactive", kind: "bool", label: "Skip inactive assets" },
    { key: "asset_types", kind: "lines", label: "Asset types", help: "One per line, not case-sensitive. Leave empty for every type.", empty: "Every type" },
    NAMES,
  ],
  // Shadow IT from Entra sign-in logs and consent grants (the full screens come with its SPA package).
  entra_shadow_it: [
    { key: "sign_ins", kind: "bool", label: "Read the sign-in logs", help: "Counts each app's users and sign-ins over 30 days. Needs AuditLog.Read.All and a Microsoft Entra ID P1 or P2 license in the tenant; untick it to use consent grants only." },
    { key: "exclude_microsoft", kind: "bool", label: "Skip apps Microsoft publishes into every tenant (first-party)" },
    { key: "first_party_tenants", kind: "lines", label: "Tenants whose apps count as Microsoft first-party", help: "One tenant ID (a GUID) per line. Apps whose publisher tenant is listed are skipped while the box above is ticked.", empty: "None" },
    NAMES,
  ],
  // SSO, SCIM and conditional access coverage of the catalog's Entra apps (the full screens come with its SPA package).
  entra_coverage: [
    { key: "provisioning", kind: "bool", label: "Read provisioning (SCIM) jobs", help: "Whether each linked app's provisioning is on. Needs Synchronization.Read.All; one read per app, at most 100 a run." },
    { key: "conditional_access", kind: "bool", label: "Read conditional access policies", help: "Which apps require MFA, for all users or only some. Needs Policy.Read.All and a Microsoft Entra ID P1 license; without P1, MFA shows as unknown." },
  ],
  // Okta applications.
  okta: [
    { key: "exclude_inactive", kind: "bool", label: "Skip inactive apps" },
    { key: "exclude_okta_apps", kind: "bool", label: "Skip Okta's own apps (Admin Console, Dashboard, Browser Plugin)" },
    { key: "exclude_bookmarks", kind: "bool", label: "Skip bookmark apps (a link, no sign-in)" },
    { key: "assignment_counts", kind: "bool", label: "Count each app's assigned users", help: "One read per app (more for large apps), within Okta's rate limits. Only the number is kept." },
    NAMES,
  ],
  // Third-party apps from the Google Workspace OAuth token audit log. The reporting window (`days`, default 30)
  // and the minimum number of people (`min_users`, default 1) are numbers this form can't edit yet: the defaults apply.
  google_workspace: [NAMES],
  // Licensed users and their last sign-in, for licence waste (the full screens come with its SPA package).
  entra_licence_usage: [
    { key: "sign_in_activity", kind: "bool", label: "Read last sign-in", help: "Tells active from inactive licenses. Needs AuditLog.Read.All and a Microsoft Entra ID P1 license; without them, licenses are counted and the last sign-in shows as unknown." },
    { key: "service_account_prefixes", kind: "lines", label: "Service and shared accounts: user names starting with", help: "One prefix per line, for example: svc-. These accounts are counted apart and never as waste. Entra has no flag for them.", empty: "None" },
    { key: "excluded_users", kind: "lines", label: "Service and shared accounts: object IDs", help: "One user object ID (a GUID) per line, counted the same way.", empty: "None" },
  ],
};

/** The scope fields of a source; none for a source this screen doesn't know yet (an older SPA against a newer API),
 *  so such a connector still opens instead of throwing. */
export const scopeFieldsOf = (source: string): Spec[] => (SCOPE_FIELDS as Record<string, Spec[] | undefined>)[source] ?? [];

export type ScopeValues = Record<string, string | boolean>;
export const fieldId = (key: string) => `scope-${key}`;

/** The form's values from the connector's scope (lists as one item per line). */
export function scopeValues(source: DiscoverySourceId, scope: ConnectorScope): ScopeValues {
  return Object.fromEntries(scopeFieldsOf(source).map((f) => [f.key, f.kind === "bool" ? scope[f.key] === true : (Array.isArray(scope[f.key]) ? (scope[f.key] as string[]).join("\n") : "")]));
}

/** The PATCH body's scope: only this source's keys, lists split into lines (blank lines dropped). */
export function scopeBody(source: DiscoverySourceId, values: ScopeValues): ConnectorScope {
  return Object.fromEntries(scopeFieldsOf(source).map((f) => [f.key, f.kind === "bool" ? values[f.key] === true : String(values[f.key] ?? "").split("\n").map((s) => s.trim()).filter(Boolean)]));
}

/** The API's errors for a connector write, keyed for these fields: `scope.x` becomes `scope-x`; an error on `scope`
 *  itself (an unknown key) goes on the first field. */
export function scopeErrors(source: DiscoverySourceId, errors: Record<string, string[]>) {
  const out: Record<string, string[]> = {};
  for (const [k, v] of Object.entries(errors)) {
    if (k.startsWith("scope.")) out[fieldId(k.slice(6))] = v;
    else if (k === "scope") out[fieldId(scopeFieldsOf(source)[0]?.key ?? "scope")] = v;
    else out[k] = v;
  }
  return out;
}

export function ScopeFields({ source, values, onChange, errors }: {
  source: DiscoverySourceId; values: ScopeValues; onChange: (v: ScopeValues) => void; errors: Record<string, string[]>;
}) {
  const specs = scopeFieldsOf(source);
  const bools = specs.filter((f) => f.kind === "bool");
  const lines = specs.filter((f) => f.kind === "lines");
  const set = (k: string, v: string | boolean) => onChange({ ...values, [k]: v });
  return (
    <>
      {bools.length > 0 && (
        <fieldset className="mb-3">
          <legend className="mb-1.5 font-semibold">{source === "azure" ? "Resource types to read" : "What to read"}</legend>
          {bools.map((f) => {
            const id = fieldId(f.key);
            const describedBy = [f.help && `${id}-help`, f.warning && `${id}-warning`, errors[id] && `${id}-error`].filter(Boolean).join(" ") || undefined;
            return (
              <div key={f.key} className="mb-2">
                <label className="flex min-h-8 items-center gap-2 text-sm">
                  <input id={id} type="checkbox" checked={values[f.key] === true} onChange={(e) => set(f.key, e.target.checked)} aria-describedby={describedBy} aria-invalid={errors[id] ? true : undefined} className="h-4 w-4 accent-primary" />
                  {f.label}
                </label>
                {f.help && <p id={`${id}-help`} className="ml-6 text-sm text-muted-foreground">{f.help}</p>}
                {f.warning && (
                  <p id={`${id}-warning`} className="ml-6 mt-1 rounded-md border border-l-4 border-state-pending-border bg-state-pending px-3 py-2 text-sm text-state-pending-foreground">
                    <span aria-hidden="true">! </span>{f.warning}
                  </p>
                )}
                {errors[id] && <p id={`${id}-error`} className="ml-6 text-sm text-error-foreground">{errors[id].join(" ")}</p>}
              </div>
            );
          })}
        </fieldset>
      )}
      {lines.map((f) => (
        <Field key={f.key} id={fieldId(f.key)} label={f.label} errors={errors} help={f.help}>
          <Textarea id={fieldId(f.key)} value={String(values[f.key] ?? "")} onChange={(e) => set(f.key, e.target.value)} spellCheck={false} />
        </Field>
      ))}
    </>
  );
}

/** The scope in words, for Viewers and Editors (who can't change it). */
export function ScopeSummary({ source, scope }: { source: DiscoverySourceId; scope: ConnectorScope }) {
  return (
    <DetailList>
      {scopeFieldsOf(source).map((f) => {
        const v = scope[f.key];
        const list = Array.isArray(v) ? (v as string[]) : [];
        return (
          <DetailRow key={f.key} label={f.label}>
            {f.kind === "bool" ? (v === true ? "Yes" : "No") : list.length ? <ul>{list.map((x) => <li key={x} className="[overflow-wrap:anywhere]">{x}</li>)}</ul> : <span className="text-muted-foreground">{f.empty ?? "None"}</span>}
          </DetailRow>
        );
      })}
    </DetailList>
  );
}
