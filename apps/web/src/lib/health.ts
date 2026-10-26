export type HealthResult =
  | { status: "ok" }
  | { status: "error"; message: string };

const DEFAULT_TIMEOUT_MS = 3000;

export async function checkHealth(
  baseUrl: string | undefined = process.env.API_BASE_URL,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<HealthResult> {
  if (!baseUrl) {
    return { status: "error", message: "API_BASE_URL is not set" };
  }
  try {
    const response = await fetch(new URL("/health", baseUrl), {
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!response.ok) {
      return { status: "error", message: `API returned HTTP ${response.status}` };
    }
    const data: unknown = await response.json();
    if (
      typeof data === "object" &&
      data !== null &&
      (data as { status?: unknown }).status === "ok"
    ) {
      return { status: "ok" };
    }
    return { status: "error", message: "Unexpected response from API" };
  } catch {
    return { status: "error", message: "Could not reach the API" };
  }
}
