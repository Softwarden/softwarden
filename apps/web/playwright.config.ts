import { defineConfig, devices } from "@playwright/test";
import { SETUP_API_PORT, SETUP_TOKEN_FILE, SETUP_WEB, SETUP_WEB_PORT } from "./e2e/setup-server";
import { CLOUD, CLOUD_API_PORT, CLOUD_DB, CLOUD_ORG_SLUG, CLOUD_WEB, CLOUD_WEB_PORT, STAFF_EMAIL, TOTP_API_PORT, TOTP_DB, TOTP_WEB, TOTP_WEB_PORT } from "./e2e/cloud-server";

// Use the API's uv-managed virtualenv directly so the shell PATH doesn't matter.
const py = process.platform === "win32" ? ".venv\\Scripts\\python" : ".venv/bin/python";

// Starts its own Django API and Vite dev server on their own ports, against its own database, `softwarden_e2e`,
// dropped and recreated on every run (apps/api/e2e_db.py), so the dev database and dev servers are never touched.
// Postgres must be running first: `docker compose -f compose.yaml -f compose.dev.yaml up -d db` from the repo root
// (the base compose.yaml publishes no database port).
// Overridable so a second suite (or a stale one) doesn't collide: E2E_API_PORT, E2E_DB and E2E_SETUP_DB. Database
// names must end in `_e2e`, which the test-only routes require (api/urls.py).
const API_PORT = Number(process.env.E2E_API_PORT ?? 8011);
const E2E_DB = process.env.E2E_DB ?? "softwarden_e2e";
const SETUP_DB = process.env.E2E_SETUP_DB ?? "softwarden_setup_e2e";
// Each e2e API server keeps its database connections (one per server thread, kept while Vite's proxy keeps the HTTP
// connection alive) instead of opening one per request: three servers in parallel otherwise left ~10,000 sockets in
// TIME_WAIT and Windows ran out of ephemeral ports.
const DB_CONN_MAX_AGE = "60";
// Specs that call the test-only `e2e/run-digest` endpoint, which replaces today's digest run: they must never overlap,
// so they run in their own projects, one after the other, beside the rest of the suite.
const DIGEST_SPECS = ["**/a11y-renewals.spec.ts", "**/attention-renewals.spec.ts"];
// Every e2e API server requires If-Match from a signed-in session, whatever the
// installation default: a screen that writes without the ETag it read gets a 428 and its journey fails. Setup writes the
// specs make through the API send `If-Match: *` (e2e/helpers.ts ANY_REVISION_FOR).
const REQUIRE_IF_MATCH = "1";
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 5174); // override when a stale dev server holds 5174
const WEB = `http://localhost:${WEB_PORT}`;

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  // A stray test.only would quietly skip the rest of the suite in CI.
  forbidOnly: !!process.env.CI,
  // One retry in CI, and a flaky test still fails the job: the e2e job's next step,
  // ops/ci/test-report-check.py, reads the JSON report below and fails on any test that failed and then passed on
  // retry, naming it with its first error. The retry only tells a flake from a steady failure; it never makes a red
  // run green. Locally a failure is a failure (no retry).
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }], ["json", { outputFile: "test-results/results.json" }]],
  use: { baseURL: WEB, trace: "retain-on-failure" },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] }, testIgnore: DIGEST_SPECS },
    { name: "digest 1 of 2", use: { ...devices["Desktop Chrome"] }, testMatch: DIGEST_SPECS[0] },
    { name: "digest 2 of 2", use: { ...devices["Desktop Chrome"] }, testMatch: DIGEST_SPECS[1], dependencies: ["digest 1 of 2"] },
  ],
  webServer: [
    {
      command: `${py} e2e_db.py && ${py} manage.py migrate --noinput && ${py} manage.py seed_demo --accounts-only && ${py} e2e_server.py 127.0.0.1:${API_PORT}`,
      cwd: "../api",
      env: {
        // Development mode, off by default: the test servers run on the development secret key.
        SOFTWARDEN_DEBUG: "1",
        SOFTWARDEN_REQUIRE_IF_MATCH: REQUIRE_IF_MATCH,
        POSTGRES_DB: E2E_DB,
        SOFTWARDEN_DB_CONN_MAX_AGE: DB_CONN_MAX_AGE,
        SOFTWARDEN_CSRF_TRUSTED_ORIGINS: WEB,
        // The suite signs in many times per run; lift the login throttle for this test server only.
        LOGIN_THROTTLE_RATE: "1000/min",
        // Single sign-on and invitation links have their own scopes; lift them the same way.
        SOFTWARDEN_SSO_THROTTLE_RATE: "1000/min",
        SOFTWARDEN_INVITATION_THROTTLE_RATE: "1000/min",
        // The parallel suite sends thousands of reads a minute from a few shared users, past the per-user API rate
        // limits (core/throttling.py); lift them for this test server only (empty: no limit).
        SOFTWARDEN_API_READ_RATE: "",
        SOFTWARDEN_API_WRITE_RATE: "",
        // Digests go to Django's in-memory outbox, read back through the test-only e2e/run-digest endpoint.
        SOFTWARDEN_EMAIL_BACKEND: "django.core.mail.backends.locmem.EmailBackend",
        SOFTWARDEN_E2E: "1",
        // A synthetic credential key for connector secrets (core/credentials.py); the e2e connectors use the
        // simulated Entra tenant.
        SOFTWARDEN_CREDENTIAL_KEYS: "Hy0wJ7m3c5m8v1oNNpS7Ad0b2Qj0Zq3D3qPZfC0J0nA=",
        // Entra sign-in through the simulated identity provider; the callback comes back through Vite.
        SOFTWARDEN_BASE_URL: WEB,
        SOFTWARDEN_OIDC_TENANT: "simulated",
        SOFTWARDEN_OIDC_CLIENT_ID: "11111111-2222-4333-8444-555555555555",
        SOFTWARDEN_OIDC_CLIENT_SECRET: "synthetic-oidc-secret",
      },
      url: `http://127.0.0.1:${API_PORT}/api/v1/health`,
      reuseExistingServer: false, // a dev server would use the dev database and the default login throttle
      timeout: 120_000,
    },
    {
      command: "npm run dev",
      env: { SOFTWARDEN_WEB_PORT: String(WEB_PORT), SOFTWARDEN_API: `http://127.0.0.1:${API_PORT}` },
      // Vite's own output goes into the run's log ([WebServer] lines; stderr is piped by default), so a dev server
      // that stops or can't proxy shows why.
      stdout: "pipe",
      url: WEB,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    // A clean installation for e2e/setup.spec.ts: migrated, no accounts, setup pending with a token from
    // `bootstrap_admin --prepare`. Its own database, API and Vite (with its own dependency cache) on their own ports.
    {
      command: `${py} e2e_db.py && ${py} manage.py migrate --noinput && ${py} manage.py bootstrap_admin --prepare && ${py} e2e_server.py 127.0.0.1:${SETUP_API_PORT}`,
      cwd: "../api",
      env: {
        // Development mode, off by default: the test servers run on the development secret key.
        SOFTWARDEN_DEBUG: "1",
        SOFTWARDEN_REQUIRE_IF_MATCH: REQUIRE_IF_MATCH,
        POSTGRES_DB: SETUP_DB,
        SOFTWARDEN_DB_CONN_MAX_AGE: DB_CONN_MAX_AGE,
        SOFTWARDEN_CSRF_TRUSTED_ORIGINS: SETUP_WEB,
        SOFTWARDEN_BOOTSTRAP_TOKEN_FILE: SETUP_TOKEN_FILE,
        SOFTWARDEN_CREDENTIAL_KEYS: "Hy0wJ7m3c5m8v1oNNpS7Ad0b2Qj0Zq3D3qPZfC0J0nA=",
        SOFTWARDEN_BASE_URL: SETUP_WEB,
      },
      url: `http://127.0.0.1:${SETUP_API_PORT}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "npm run dev",
      env: { SOFTWARDEN_WEB_PORT: String(SETUP_WEB_PORT), SOFTWARDEN_API: `http://127.0.0.1:${SETUP_API_PORT}`, SOFTWARDEN_VITE_CACHE: "node_modules/.vite-setup-e2e" },
      stdout: "pipe",
      url: SETUP_WEB,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    // A hosted installation for e2e/cloud.spec.ts (e2e/cloud-server.ts): Contoso on the cloud edition,
    // platform@example.test platform staff with no membership (seed_demo makes it on an _e2e database) and
    // editor@example.test platform staff with one, public signup on, email to the in-memory outbox read back through
    // the test-only routes. Its own database, API and Vite.
    // Started only with E2E_CLOUD=1 (CI's Playwright job sets it); without it cloud.spec.ts is skipped, so a local run
    // of other specs doesn't pay for a third server pair.
    ...(CLOUD ? [{
      command: `${py} e2e_db.py && ${py} manage.py migrate --noinput && ${py} manage.py seed_demo --accounts-only && ${py} manage.py set_edition ${CLOUD_ORG_SLUG} cloud && ${py} manage.py platform_staff grant ${STAFF_EMAIL} && ${py} e2e_server.py 127.0.0.1:${CLOUD_API_PORT}`,
      cwd: "../api",
      env: {
        // Development mode, off by default: the test servers run on the development secret key.
        SOFTWARDEN_DEBUG: "1",
        SOFTWARDEN_REQUIRE_IF_MATCH: REQUIRE_IF_MATCH,
        POSTGRES_DB: CLOUD_DB,
        SOFTWARDEN_DB_CONN_MAX_AGE: DB_CONN_MAX_AGE,
        SOFTWARDEN_CSRF_TRUSTED_ORIGINS: CLOUD_WEB,
        LOGIN_THROTTLE_RATE: "1000/min",
        // Single sign-on and invitation links have their own scopes; lift them the same way.
        SOFTWARDEN_SSO_THROTTLE_RATE: "1000/min",
        SOFTWARDEN_INVITATION_THROTTLE_RATE: "1000/min",
        SOFTWARDEN_SIGNUP_THROTTLE_RATE: "1000/hour",
        SOFTWARDEN_API_READ_RATE: "",
        SOFTWARDEN_API_WRITE_RATE: "",
        SOFTWARDEN_EMAIL_BACKEND: "django.core.mail.backends.locmem.EmailBackend",
        SOFTWARDEN_SIGNUP_ENABLED: "1",
        // Sign-in domains need DNS proof, as in cloud mode; DEBUG's simulated resolver verifies
        // any *.dns-verified.test domain and no other, and never touches the network.
        SOFTWARDEN_SSO_DOMAIN_VERIFICATION: "1",
        // The test-only routes (e2e/outbox, e2e/expire-impersonation, e2e/run-export-jobs); its database ends in _e2e.
        SOFTWARDEN_E2E: "1",
        SOFTWARDEN_CREDENTIAL_KEYS: "Hy0wJ7m3c5m8v1oNNpS7Ad0b2Qj0Zq3D3qPZfC0J0nA=",
        SOFTWARDEN_BASE_URL: CLOUD_WEB,
      },
      url: `http://127.0.0.1:${CLOUD_API_PORT}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "npm run dev",
      env: { SOFTWARDEN_WEB_PORT: String(CLOUD_WEB_PORT), SOFTWARDEN_API: `http://127.0.0.1:${CLOUD_API_PORT}`, SOFTWARDEN_VITE_CACHE: "node_modules/.vite-cloud-e2e" },
      stdout: "pipe" as const,
      url: CLOUD_WEB,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    // A hosted installation with the platform second factor on, for e2e/platform-totp.spec.ts only (the setting is
    // installation-wide; e2e/cloud-server.ts says why it isn't the cloud server above). Also only with E2E_CLOUD=1.
    {
      command: `${py} e2e_db.py && ${py} manage.py migrate --noinput && ${py} manage.py seed_demo --accounts-only && ${py} e2e_server.py 127.0.0.1:${TOTP_API_PORT}`,
      cwd: "../api",
      env: {
        SOFTWARDEN_DEBUG: "1",
        SOFTWARDEN_REQUIRE_IF_MATCH: REQUIRE_IF_MATCH,
        POSTGRES_DB: TOTP_DB,
        SOFTWARDEN_DB_CONN_MAX_AGE: DB_CONN_MAX_AGE,
        SOFTWARDEN_CSRF_TRUSTED_ORIGINS: TOTP_WEB,
        SOFTWARDEN_PLATFORM_TOTP: "1",
        LOGIN_THROTTLE_RATE: "1000/min",
        SOFTWARDEN_API_READ_RATE: "",
        SOFTWARDEN_API_WRITE_RATE: "",
        SOFTWARDEN_EMAIL_BACKEND: "django.core.mail.backends.locmem.EmailBackend",
        SOFTWARDEN_E2E: "1",
        // The TOTP secret is kept encrypted under the credential keys.
        SOFTWARDEN_CREDENTIAL_KEYS: "Hy0wJ7m3c5m8v1oNNpS7Ad0b2Qj0Zq3D3qPZfC0J0nA=",
        SOFTWARDEN_BASE_URL: TOTP_WEB,
      },
      url: `http://127.0.0.1:${TOTP_API_PORT}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "npm run dev",
      env: { SOFTWARDEN_WEB_PORT: String(TOTP_WEB_PORT), SOFTWARDEN_API: `http://127.0.0.1:${TOTP_API_PORT}`, SOFTWARDEN_VITE_CACHE: "node_modules/.vite-totp-e2e" },
      stdout: "pipe" as const,
      url: TOTP_WEB,
      reuseExistingServer: false,
      timeout: 60_000,
    }] : []),
  ],
});
