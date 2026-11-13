/** Theme choice in the top bar: System (follow the operating system, the default), Light or Dark. Stored in this
 *  browser only (src/lib/theme.ts). */
import { Monitor, Moon, Sun } from "lucide-react";
import { MoreMenu } from "@/components/more-menu";
import { THEME_PREFS, useTheme } from "@/lib/theme";

export function ThemeMenu() {
  const { pref, setPref } = useTheme();
  const current = THEME_PREFS.find((p) => p.value === pref)!;
  const Icon = pref === "dark" ? Moon : pref === "light" ? Sun : Monitor;
  return (
    <MoreMenu label={`Theme: ${current.label}`} menuLabel="Theme" size="sm"
      trigger={<><Icon aria-hidden="true" className="text-icon" /><span className="hidden sm:inline">Theme</span></>}
      actions={THEME_PREFS.map((p) => ({ label: p.label, checked: p.value === pref, onSelect: () => setPref(p.value) }))} />
  );
}
