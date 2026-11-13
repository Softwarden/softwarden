/** Every IANA zone the browser knows, plus UTC and `current` (the browser's own zone, which can be a legacy alias not in
 *  the list, or an organization's saved zone), as Combobox options. First-run setup and Organization settings share it. */
export function zoneOptions(current: string) {
  let zones: string[] = [];
  try {
    zones = Intl.supportedValuesOf("timeZone");
  } catch {
    zones = [];
  }
  return [...new Set(["UTC", current, ...zones])].filter(Boolean).sort().map((z) => ({ id: z, label: z }));
}
