/**
 * Single configuration boundary for environment-derived values.
 * All API base URL access in the app should go through `apiBaseUrl`
 * rather than reading `process.env` directly elsewhere.
 */
function requireEnv(name: string, fallback?: string): string {
  const value = process.env[name] ?? fallback;
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

export const apiBaseUrl = requireEnv(
  "NEXT_PUBLIC_API_URL",
  "http://localhost:8000",
);
