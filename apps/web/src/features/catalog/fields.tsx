/** Catalog fields shared by the record forms, detail pages and lists: the compliance profile switch, the
 *  three-state yes / no / not recorded control, and how blank values read. */
import type { Organization } from "@/api";
import { Select } from "@/components/app";
import { useApiQuery } from "@/lib/query";

/** Whether this organization's records carry the compliance fields now (GET organization, any role). While the answer
 *  loads, or if it can't be read, the fields stay hidden: the API refuses them while the profile is off. */
export function useCompliance() {
  const org = useApiQuery<Organization>("organization", { staleTime: 60_000 });
  return { enabled: org.data?.compliance_profile_enabled === true, available: org.data?.compliance_profile_available === true, org };
}

/** A three-state answer for a form: "" (not recorded, null), "true" or "false". */
export type TriState = "" | "true" | "false";
export const triOf = (v: boolean | null | undefined): TriState => (v === true ? "true" : v === false ? "false" : "");
export const triValue = (v: TriState): boolean | null => (v === "true" ? true : v === "false" ? false : null);

/** Yes / No / Not recorded as a select: "No" is a decision, "Not recorded" means nobody has said (null). */
export function TriSelect({ id, value, onChange, yes = "Yes", no = "No", ...rest }: {
  id: string; value: TriState; onChange: (v: TriState) => void; yes?: string; no?: string;
  "aria-invalid"?: boolean; "aria-describedby"?: string;
}) {
  return (
    <Select id={id} value={value} onChange={(e) => onChange(e.target.value as TriState)} {...rest}>
      <option value="">Not recorded</option>
      <option value="true">{yes}</option>
      <option value="false">{no}</option>
    </Select>
  );
}

/** A three-state value as text: "Yes", "No" or "Not recorded" (muted). */
export function TriText({ value, yes = "Yes", no = "No" }: { value: boolean | null | undefined; yes?: string; no?: string }) {
  if (value === true) return <>{yes}</>;
  if (value === false) return <>{no}</>;
  return <span className="text-muted-foreground">Not recorded</span>;
}

/** Filter options for a three-state field (`true`, `false`, `unknown`). */
export const triFilter = (yes = "Yes", no = "No") => [
  { value: "true", label: yes }, { value: "false", label: no }, { value: "unknown", label: "Not recorded" },
];
