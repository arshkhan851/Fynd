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

export type CompetitionCategory = {
  name: string;
  slug: string;
  is_primary: boolean;
};

export type CompetitionOrganizer = {
  name: string;
  role: string | null;
  website_url: string | null;
};

export type CompetitionPrize = {
  award_name: string | null;
  place: string | null;
  amount: string | null;
  currency: string | null;
  details: string | null;
};

export type CompetitionSource = {
  source_kind: string;
  source_url: string;
  publisher_name: string | null;
  publisher_homepage_url: string | null;
};

export type CompetitionDetail = Competition & {
  description: string | null;
  eligibility_summary: string | null;
  registration_url: string | null;
  registration_opens_on: string | null;
  registration_opens_at: string | null;
  event_ends_on: string | null;
  event_ends_at: string | null;
  event_timezone: string | null;
  categories: CompetitionCategory[];
  organizers: CompetitionOrganizer[];
  prizes: CompetitionPrize[];
  sources: CompetitionSource[];
};

export type CompetitionResult =
  | { status: "ok"; data: CompetitionPage }
  | { status: "error"; message: string };

export type CompetitionDetailResult =
  | { status: "ok"; data: CompetitionDetail }
  | { status: "not-found" }
  | { status: "error"; message: string };

const DEFAULT_TIMEOUT_MS = 3000;
const DEFAULT_LIMIT = 20;

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isSafeHttpUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    return (
      (url.protocol === "http:" || url.protocol === "https:") &&
      url.hostname.length > 0 &&
      !url.username &&
      !url.password
    );
  } catch {
    return false;
  }
}

function isNullableHttpUrl(value: unknown): value is string | null {
  return value === null || isSafeHttpUrl(value);
}

function isCompetition(value: unknown): value is Competition {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.id === "number" &&
    typeof item.title === "string" &&
    isNullableString(item.format) &&
    isNullableHttpUrl(item.official_url) &&
    isNullableString(item.event_starts_on) &&
    isNullableString(item.event_starts_at) &&
    isNullableString(item.registration_deadline_on) &&
    isNullableString(item.registration_deadline_at)
  );
}

function isCompetitionDetail(value: unknown): value is CompetitionDetail {
  if (!isCompetition(value)) return false;
  const item = value as Record<string, unknown>;
  const isNullableArray = (key: string, validate: (entry: unknown) => boolean) =>
    Array.isArray(item[key]) && (item[key] as unknown[]).every(validate);
  return (
    isNullableString(item.description) &&
    isNullableString(item.eligibility_summary) &&
    isNullableHttpUrl(item.registration_url) &&
    isNullableString(item.registration_opens_on) &&
    isNullableString(item.registration_opens_at) &&
    isNullableString(item.event_ends_on) &&
    isNullableString(item.event_ends_at) &&
    isNullableString(item.event_timezone) &&
    isNullableArray("categories", (entry) => {
      if (typeof entry !== "object" || entry === null) return false;
      const category = entry as Record<string, unknown>;
      return (
        typeof category.name === "string" &&
        typeof category.slug === "string" &&
        typeof category.is_primary === "boolean"
      );
    }) &&
    isNullableArray("organizers", (entry) => {
      if (typeof entry !== "object" || entry === null) return false;
      const organizer = entry as Record<string, unknown>;
      return (
        typeof organizer.name === "string" &&
        isNullableString(organizer.role) &&
        isNullableHttpUrl(organizer.website_url)
      );
    }) &&
    isNullableArray("prizes", (entry) => {
      if (typeof entry !== "object" || entry === null) return false;
      const prize = entry as Record<string, unknown>;
      return (
        isNullableString(prize.award_name) &&
        isNullableString(prize.place) &&
        isNullableString(prize.amount) &&
        isNullableString(prize.currency) &&
        isNullableString(prize.details)
      );
    }) &&
    isNullableArray("sources", (entry) => {
      if (typeof entry !== "object" || entry === null) return false;
      const source = entry as Record<string, unknown>;
      return (
        typeof source.source_kind === "string" &&
        isSafeHttpUrl(source.source_url) &&
        isNullableString(source.publisher_name) &&
        isNullableHttpUrl(source.publisher_homepage_url)
      );
    })
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

export async function getCompetition(
  id: string,
  baseUrl: string | undefined = process.env.API_BASE_URL,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<CompetitionDetailResult> {
  if (!/^\d+$/.test(id) || !Number.isSafeInteger(Number(id)) || Number(id) < 1) {
    return { status: "not-found" };
  }
  if (!baseUrl) {
    return { status: "error", message: "API_BASE_URL is not set" };
  }

  try {
    const response = await fetch(new URL(`/competitions/${id}`, baseUrl), {
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (response.status === 404) return { status: "not-found" };
    if (!response.ok) {
      return {
        status: "error",
        message: `API returned HTTP ${response.status}`,
      };
    }

    const data: unknown = await response.json();
    if (!isCompetitionDetail(data)) {
      return { status: "error", message: "Unexpected response from API" };
    }
    return { status: "ok", data };
  } catch {
    return { status: "error", message: "Could not reach the API" };
  }
}
