/**
 * Public client-side runtime configuration for the static-export frontend.
 * Never store secrets or provider API keys in NEXT_PUBLIC_* variables.
 */
export function getApiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  if (configured && configured.length > 0) {
    return configured.replace(/\/+$/, "");
  }
  return "http://localhost:8000/api/v1";
}
