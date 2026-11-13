/** Imported before Scalar: puts the zod bundled with Scalar (zod 4) in its `jitless` mode, so it never probes
 *  `new Function("")` to see whether it may compile parsers. The probe is caught and harmless, but under the
 *  installation's CSP (`script-src 'self'`, no 'unsafe-eval') the browser reports it as a violation in the console and
 *  to any report endpoint. zod reads its options from this global, shared by
 *  every copy of zod 4 on the page, and decides whether to compile when each schema is made, so this must run before
 *  Scalar's modules are evaluated: keep it the first import of scalar-reference.tsx. Parsing is the same without the
 *  compiled fast path, only slower, and Scalar parses one configuration. */
type ZodGlobal = { __zod_globalConfig?: { jitless?: boolean } };
const holder = globalThis as ZodGlobal;
holder.__zod_globalConfig ??= {};
holder.__zod_globalConfig.jitless = true;

export {};
