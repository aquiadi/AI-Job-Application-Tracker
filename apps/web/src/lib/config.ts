/**
 * Browser configuration.
 *
 * Everything here is public by construction: `NEXT_PUBLIC_*` values are inlined into
 * the bundle at build time and are visible to anyone who opens the page. A Firebase
 * web API key belongs in that set — it identifies the project, it is not a credential,
 * and what actually protects data is the ID token the user signs in for, plus the
 * row-level security policies behind the API.
 */

function required(name: string, value: string | undefined, fallback: string): string {
  if (value && value.length > 0) return value;
  if (process.env.NODE_ENV === "production" && !fallback) {
    throw new Error(`${name} is not set`);
  }
  return fallback;
}

export const config = {
  apiUrl: required("NEXT_PUBLIC_API_URL", process.env.NEXT_PUBLIC_API_URL, "http://127.0.0.1:8080"),
  projectId: required(
    "NEXT_PUBLIC_FIREBASE_PROJECT_ID",
    process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
    "jobtrack-local",
  ),
  /**
   * The emulator does not check the key, so local development needs no real project.
   * In cloud this is the project's web API key.
   */
  apiKey: required(
    "NEXT_PUBLIC_FIREBASE_API_KEY",
    process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
    "demo-api-key",
  ),
  /**
   * Set only in local development. The emulator issues unsigned tokens, so a build
   * that reached production with this set would accept any token — the API refuses to
   * start in that configuration, and this is the browser half of the same guard.
   */
  authEmulatorHost: process.env.NEXT_PUBLIC_AUTH_EMULATOR_HOST ?? "",
} as const;

export const isEmulated = config.authEmulatorHost.length > 0;
