/** The clean installation the first-run setup spec runs against (playwright.config.ts): its own API, database and Vite
 *  server, migrated but with no organization or account, so setup is pending. The main e2e server is seeded with the
 *  demo accounts, where setup is already closed. `bootstrap_admin --prepare` writes the one-time token to TOKEN_FILE
 *  (gitignored, under apps/api/.bootstrap/), which the spec reads as an operator on the host would. */
import path from "node:path";
import { fileURLToPath } from "node:url";

export const SETUP_API_PORT = Number(process.env.E2E_SETUP_API_PORT ?? 8021);
export const SETUP_WEB_PORT = Number(process.env.E2E_SETUP_WEB_PORT ?? 5181);
export const SETUP_WEB = `http://localhost:${SETUP_WEB_PORT}`;
/** apps/api/.bootstrap/e2e/setup-token, resolved from this file (the package is ESM, so there is no __dirname). */
const here = path.dirname(fileURLToPath(import.meta.url));
export const SETUP_TOKEN_FILE = path.resolve(here, "../../api/.bootstrap/e2e/setup-token");
