export type SavedOpportunity = {
  competition_id: number;
  title: string;
  format: string | null;
  event_starts_on: string | null;
  event_starts_at: string | null;
  registration_deadline_on: string | null;
  registration_deadline_at: string | null;
  saved_at: string;
};

export type SavedOpportunityPage = {
  items: SavedOpportunity[];
  total: number;
  limit: number;
  offset: number;
};

export type SavedOpportunityMutation = {
  competition_id: number;
  saved: boolean;
};

export class SavedOpportunitiesApiError extends Error {
  constructor(
    public readonly status: number | null,
    message: string,
  ) {
    super(message);
    this.name = "SavedOpportunitiesApiError";
  }
}

const API_PATH = "/api/v1/saved-opportunities";
const MAX_PAGE_SIZE = 50;

async function authenticatedFetch(
  apiBaseUrl: string,
  path: string,
  init?: RequestInit,
): Promise<Response> {
  if (!apiBaseUrl) {
    throw new SavedOpportunitiesApiError(null, "API_BASE_URL is not set");
  }

  let response: Response;
  try {
    response = await fetch(new URL(path, apiBaseUrl), {
      ...init,
      cache: "no-store",
      credentials: "include",
    });
  } catch {
    throw new SavedOpportunitiesApiError(null, "Could not reach the API");
  }

  if (!response.ok) {
    const message = response.status === 401
      ? "Please sign in to manage saved opportunities."
      : `Saved opportunities request failed (HTTP ${response.status}).`;
    throw new SavedOpportunitiesApiError(response.status, message);
  }

  return response;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isSavedOpportunity(value: unknown): value is SavedOpportunity {
  if (!isRecord(value)) return false;
  return (
    Number.isSafeInteger(value.competition_id) &&
    (value.competition_id as number) > 0 &&
    typeof value.title === "string" &&
    isNullableString(value.format) &&
    isNullableString(value.event_starts_on) &&
    isNullableString(value.event_starts_at) &&
    isNullableString(value.registration_deadline_on) &&
    isNullableString(value.registration_deadline_at) &&
    typeof value.saved_at === "string"
  );
}

function isSavedOpportunityPage(value: unknown): value is SavedOpportunityPage {
  if (!isRecord(value)) return false;
  return (
    Array.isArray(value.items) &&
    value.items.every(isSavedOpportunity) &&
    Number.isSafeInteger(value.total) &&
    Number.isSafeInteger(value.limit) &&
    Number.isSafeInteger(value.offset)
  );
}

function isMutationResponse(value: unknown): value is SavedOpportunityMutation {
  return (
    isRecord(value) &&
    Number.isSafeInteger(value.competition_id) &&
    (value.competition_id as number) > 0 &&
    typeof value.saved === "boolean"
  );
}

async function parseResponse<T>(
  response: Response,
  validate: (value: unknown) => value is T,
): Promise<T> {
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new SavedOpportunitiesApiError(
      response.status,
      "Unexpected response from the API",
    );
  }
  if (!validate(data)) {
    throw new SavedOpportunitiesApiError(
      response.status,
      "Unexpected response from the API",
    );
  }
  return data;
}

export async function getSavedOpportunities(
  apiBaseUrl: string,
  limit = 20,
  offset = 0,
): Promise<SavedOpportunityPage> {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_PAGE_SIZE) {
    throw new RangeError(`limit must be an integer between 1 and ${MAX_PAGE_SIZE}`);
  }
  if (!Number.isSafeInteger(offset) || offset < 0) {
    throw new RangeError("offset must be a non-negative integer");
  }

  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  const response = await authenticatedFetch(apiBaseUrl, `${API_PATH}?${query}`);
  return parseResponse(response, isSavedOpportunityPage);
}

async function mutateSavedOpportunity(
  apiBaseUrl: string,
  competitionId: number,
  method: "PUT" | "DELETE",
  expectedSaved: boolean,
): Promise<SavedOpportunityMutation> {
  if (!Number.isSafeInteger(competitionId) || competitionId < 1) {
    throw new RangeError("competitionId must be a positive safe integer");
  }

  const response = await authenticatedFetch(
    apiBaseUrl,
    `${API_PATH}/${competitionId}`,
    { method },
  );
  const result = await parseResponse(response, isMutationResponse);
  if (result.competition_id !== competitionId || result.saved !== expectedSaved) {
    throw new SavedOpportunitiesApiError(
      response.status,
      "Unexpected response from the API",
    );
  }
  return result;
}

export function saveOpportunity(
  apiBaseUrl: string,
  competitionId: number,
): Promise<SavedOpportunityMutation> {
  return mutateSavedOpportunity(apiBaseUrl, competitionId, "PUT", true);
}

export function removeSavedOpportunity(
  apiBaseUrl: string,
  competitionId: number,
): Promise<SavedOpportunityMutation> {
  return mutateSavedOpportunity(apiBaseUrl, competitionId, "DELETE", false);
}
