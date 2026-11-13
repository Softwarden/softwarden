/** SSO and MFA coverage vocabulary (capability `discovery.entra`), shared by the coverage report
 *  (`sso-coverage.tsx`) and an application's Coverage panel (`coverage-panel.tsx`); both are loaded on first use, so
 *  this module never reaches the first bundle.
 *
 *  Each dimension is a chip with a glyph and words. Covered is the confirmed tone (✓ SAML, ✓ SCIM on, ✓ MFA for all
 *  users); a gap is the dashed gap chip with the API's own gap words ("No SSO", "No SCIM", "No MFA policy", "MFA for
 *  some users only"); unknown is a named gap too ("SSO unknown"), always shown with its reason (`*_detail`), never as
 *  "no SSO"; sign-in blocked for everyone is neutral (⊘), since nobody can sign in at all. */
import { chip, GapChip } from "@/components/sw";
import type { Schemas } from "@/lib/openapi";

export type Coverage = Schemas["SsoCoverage"];
export type CoverageRow = Schemas["SsoCoverageRow"];
export type CoveragePolicy = Schemas["CoveragePolicy"];
type Tone = "confirmed" | "pending" | "unreviewed" | "neutral";
/** A value's chip: its glyph and words in a tone, or a named gap (`gap`). */
type Look = { glyph: string; text: string; tone: Tone } | { gap: string };

export const SSO: Record<Coverage["sso"], Look> = {
  saml: { glyph: "✓", text: "SAML", tone: "confirmed" },
  oidc: { glyph: "✓", text: "OpenID Connect", tone: "confirmed" },
  password: { glyph: "✓", text: "Password-based SSO", tone: "confirmed" },
  none: { gap: "No SSO" },
  unknown: { gap: "SSO unknown" },
};
export const SCIM: Record<Coverage["scim"], Look> = {
  on: { glyph: "✓", text: "SCIM on", tone: "confirmed" },
  off: { gap: "No SCIM" },
  unknown: { gap: "SCIM unknown" },
};
export const MFA: Record<Coverage["mfa"], Look> = {
  all_users: { glyph: "✓", text: "MFA for all users", tone: "confirmed" },
  some_users: { gap: "MFA for some users only" },
  none: { gap: "No MFA policy" },
  unknown: { gap: "MFA unknown" },
  blocked: { glyph: "⊘", text: "Sign-in blocked for all users", tone: "neutral" },
};

/** The filters' words for each value (the report's selects). */
export const SSO_CHOICES = [
  { value: "saml", label: "SAML" }, { value: "oidc", label: "OpenID Connect" }, { value: "password", label: "Password-based SSO" },
  { value: "none", label: "No SSO" }, { value: "unknown", label: "Unknown" },
];
export const SCIM_CHOICES = [{ value: "on", label: "On" }, { value: "off", label: "Off (no SCIM)" }, { value: "unknown", label: "Unknown" }];
export const MFA_CHOICES = [
  { value: "all_users", label: "MFA for all users" }, { value: "some_users", label: "MFA for some users only" },
  { value: "none", label: "No MFA policy" }, { value: "unknown", label: "Unknown" }, { value: "blocked", label: "Sign-in blocked for all users" },
];
export const GAP_CHOICES = [
  { value: "no_sso", label: "No SSO" }, { value: "no_scim", label: "No SCIM" }, { value: "no_mfa", label: "No MFA policy" },
  { value: "mfa_partial", label: "MFA for some users only" }, { value: "unknown", label: "Coverage unknown" },
];

/** A value's chip, or for a value this build doesn't know (a newer API) a neutral `?` chip with the raw value, never a
 *  crash. */
export function lookOf(map: Record<string, Look>, value: string): Look {
  return map[value] ?? { glyph: "?", text: value || "Not given", tone: "neutral" };
}

export function CoverageChip({ look, testId }: { look: Look; testId?: string }) {
  if ("gap" in look) return <span data-testid={testId}><GapChip label={look.gap} /></span>;
  return <span className={chip({ state: look.tone })} data-testid={testId}><span aria-hidden="true">{look.glyph}</span>{look.text}</span>;
}

/** What a conditional access policy does to the app, as a chip (glyph plus words). Report-only and blocked are named. */
const EFFECT: Record<CoveragePolicy["effect"], { glyph: string; text: string; tone: Tone }> = {
  mfa_all_users: { glyph: "✓", text: "Requires MFA for all users", tone: "confirmed" },
  mfa_some_users: { glyph: "◐", text: "Requires MFA for some users", tone: "pending" },
  may_require_mfa: { glyph: "?", text: "May require MFA", tone: "unreviewed" },
  mfa_optional: { glyph: "◐", text: "MFA is one option", tone: "pending" },
  no_mfa: { glyph: "○", text: "Doesn't require MFA", tone: "unreviewed" },
  report_only: { glyph: "○", text: "Report-only, not enforced", tone: "neutral" },
  blocked: { glyph: "⊘", text: "Blocks sign-in", tone: "neutral" },
};

export function EffectChip({ effect }: { effect: CoveragePolicy["effect"] }) {
  const e = EFFECT[effect] ?? { glyph: "?", text: effect, tone: "neutral" as const };
  return <span className={chip({ state: e.tone })} data-effect={effect}><span aria-hidden="true">{e.glyph}</span>{e.text}</span>;
}

/** The record's value of a field Entra contradicts, in words ("Yes", "No", "Not recorded"). */
export const yesNo = (v: boolean | null) => (v === null ? "Not recorded" : v ? "Yes" : "No");

export const linkedText = (c: Pick<Coverage, "linked_by" | "entra_app_id">) =>
  c.linked_by === "entra_app_id" ? "Matched by its Entra application ID"
    : c.linked_by === "candidate" ? "Matched through its linked discovery candidate"
      : "Not linked to an Entra application";
