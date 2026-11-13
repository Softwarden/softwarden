/** The hosted (cloud) installation the spec (cloud.spec.ts) runs against (playwright.config.ts): its own API,
 *  database and Vite server, so nothing it does (a cloud edition, a deletion scheduled, a limit set to zero, an
 *  impersonation) reaches the shared Contoso the rest of the suite uses.
 *
 *  Started with the demo accounts (`seed_demo --accounts-only`, which on an _e2e database also makes
 *  platform@example.test, platform staff with no membership); Contoso is then moved to the cloud edition with the
 *  operator's `set_edition`, and editor@example.test is made platform staff too with `platform_staff grant` (staff with a
 *  membership). Public signup is on (SOFTWARDEN_SIGNUP_ENABLED), email goes to Django's in-memory outbox, and
 *  SOFTWARDEN_E2E=1 adds the test-only routes (e2e/outbox, e2e/expire-impersonation, e2e/run-export-jobs). */
/** E2E_CLOUD=1 starts this server pair (playwright.config.ts); without it cloud.spec.ts is skipped. */
export const CLOUD = process.env.E2E_CLOUD === "1";
export const CLOUD_API_PORT = Number(process.env.E2E_CLOUD_API_PORT ?? 8031);
export const CLOUD_WEB_PORT = Number(process.env.E2E_CLOUD_WEB_PORT ?? 5183);
export const CLOUD_WEB = `http://localhost:${CLOUD_WEB_PORT}`;
/** Must end in `_e2e` (apps/api/e2e_db.py refuses anything else). */
export const CLOUD_DB = process.env.E2E_CLOUD_DB ?? "softwarden_cloud_e2e";
/** The organization the cloud server moves to the cloud edition, and the account it makes platform staff. */
export const CLOUD_ORG_SLUG = "contoso-synthetic";
export const STAFF_EMAIL = "editor@example.test";
/** Platform staff with no membership, made by `seed_demo --accounts-only` on an _e2e database: signs in to the console only. */
export const PLATFORM_EMAIL = "platform@example.test";

/** A second hosted installation with the platform second factor on (SOFTWARDEN_PLATFORM_TOTP=1, cloud mode's
 *  default), for e2e/platform-totp.spec.ts only. The setting is installation-wide, so it can't be turned on for the cloud
 *  server above without every console spec there needing codes (and waiting out the 30-second steps between them).
 *  Started with E2E_CLOUD=1 as well; the same demo accounts (platform@example.test is staff, not yet enrolled). */
export const TOTP_API_PORT = Number(process.env.E2E_TOTP_API_PORT ?? 8041);
export const TOTP_WEB_PORT = Number(process.env.E2E_TOTP_WEB_PORT ?? 5193);
export const TOTP_WEB = `http://localhost:${TOTP_WEB_PORT}`;
/** Must end in `_e2e`. */
export const TOTP_DB = process.env.E2E_TOTP_DB ?? "softwarden_totp_e2e";
