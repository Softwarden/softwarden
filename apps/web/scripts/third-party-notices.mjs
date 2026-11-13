// Lists the SPA's production npm packages with their licenses and license texts, for the image's THIRD_PARTY_NOTICES.
// The Dockerfile's web stage runs it after `npm ci`; docker/third_party_notices.py
// adds the Python packages and writes the file the image ships. Standard library only.
//
//   node scripts/third-party-notices.mjs [app dir] > notices.txt
//
// "Production" is every package in package-lock.json that isn't marked dev or devOptional: what `npm ci --omit=dev`
// would install, so everything the built bundle can contain. An optional package that isn't installed on this
// platform (a native binary for another OS) is left out. A package whose license can't be read is listed as UNKNOWN
// and counted on stderr, so the build log shows it.
import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const LICENSE_FILE = /^(licen[cs]e|copying|notice|copyright)([-_.].*)?$/i;

/** The license an npm package.json declares: an SPDX expression, or the older object and array forms. */
export function declaredLicense(pkg) {
  const one = (l) => (typeof l === "string" ? l : l && typeof l === "object" ? l.type : undefined);
  if (pkg.license) return one(pkg.license) ?? "UNKNOWN";
  if (Array.isArray(pkg.licenses) && pkg.licenses.length) return pkg.licenses.map(one).filter(Boolean).join(" OR ");
  return "UNKNOWN";
}

/** The production packages of a package-lock.json (lockfile v2 or v3), as [path in node_modules, lock entry]. */
export function productionPackages(lock) {
  return Object.entries(lock.packages ?? {}).filter(
    ([key, entry]) => key.startsWith("node_modules/") && !entry.dev && !entry.devOptional && !entry.link,
  );
}

/** One package's section: name, version, license, homepage, then each license file's text. */
export function section(dir, lockEntry) {
  const pkg = JSON.parse(readFileSync(path.join(dir, "package.json"), "utf8"));
  const name = pkg.name ?? lockEntry.name;
  const version = pkg.version ?? lockEntry.version;
  const license = declaredLicense(pkg);
  const home = pkg.homepage ?? (typeof pkg.repository === "string" ? pkg.repository : pkg.repository?.url) ?? "";
  const files = readdirSync(dir)
    .filter((f) => LICENSE_FILE.test(f))
    .sort();
  const texts = files.map((f) => `--- ${f} ---\n${readFileSync(path.join(dir, f), "utf8").trimEnd()}\n`);
  const head = `${name} ${version}\nLicense: ${license}${home ? `\nSource: ${home}` : ""}\n`;
  return { name, license, text: `${head}${texts.length ? `\n${texts.join("\n")}` : "(no license file in the package)\n"}` };
}

export function notices(appDir) {
  const lock = JSON.parse(readFileSync(path.join(appDir, "package-lock.json"), "utf8"));
  const out = [];
  const unknown = [];
  for (const [key, entry] of productionPackages(lock)) {
    const dir = path.join(appDir, key);
    if (!existsSync(path.join(dir, "package.json"))) {
      if (entry.optional) continue; // a platform binary for another OS
      throw new Error(`${key} is a production dependency but isn't installed: run npm ci first`);
    }
    const s = section(dir, entry);
    if (s.license === "UNKNOWN") unknown.push(s.name);
    out.push(s);
  }
  out.sort((a, b) => a.name.localeCompare(b.name));
  return { sections: out, unknown };
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const appDir = path.resolve(process.argv[2] ?? path.join(path.dirname(fileURLToPath(import.meta.url)), ".."));
  const { sections, unknown } = notices(appDir);
  process.stdout.write(sections.map((s) => s.text).join(`\n${"=".repeat(78)}\n\n`));
  console.error(`third-party notices: ${sections.length} npm packages${unknown.length ? `, license unknown: ${unknown.join(", ")}` : ""}`);
}
