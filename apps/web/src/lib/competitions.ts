export type Competition = {
  id: number;
  title: string;
  format: string | null;
  official_url: string | null;
  event_starts_on: string | null;
  event_starts_at: string | null;
  registration_deadline_on: string | null;
  registration_deadline_at: string | null;
};

export type CompetitionPage = {
  items: Competition[];
  total: number;
  limit: number;
  offset: number;
};

export type CompetitionResult =
  | { status: "ok"; data: CompetitionPage }
  | { status: "error"; message: string };

const DEFAULT_TIMEOUT_MS = 3000;
const DEFAULT_LIMIT = 20;

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isCompetition(value: unknown): value is Competition {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.id === "number" &&
    typeof item.title === "string" &&
    isNullableString(item.format) &&
    isNullableString(item.official_url) &&
    isNullableString(item.event_starts_on) &&
    isNullableString(item.event_starts_at) &&
    isNullableString(item.registration_deadline_on) &&
    isNullableString(item.registration_deadline_at)
  );
}

function isCompetitionPage(value: unknown): value is CompetitionPage {
  if (typeof value !== "object" || value === null) return false;
  const page = value as Record<string, unknown>;
  return (
    Array.isArray(page.items) &&
    page.items.every(isCompetition) &&
    typeof page.total === "number" &&
    typeof page.limit === "number" &&
    typeof page.offset === "number"
  );
}

export async function getCompetitions(
  baseUrl: string | undefined = process.env.API_BASE_URL,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<CompetitionResult> {
  if (!baseUrl) {
    return { status: "error", message: "API_BASE_URL is not set" };
  }

  try {
    const url = new URL("/competitions", baseUrl);
    url.searchParams.set("limit", String(DEFAULT_LIMIT));
    url.searchParams.set("offset", "0");
    const response = await fetch(url, {
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!response.ok) {
      return {
        status: "error",
        message: `API returned HTTP ${response.status}`,
      };
    }

    const data: unknown = await response.json();
    if (!isCompetitionPage(data)) {
      return { status: "error", message: "Unexpected response from API" };
    }
    return { status: "ok", data };
  } catch {
    return { status: "error", message: "Could not reach the API" };
  }
}
