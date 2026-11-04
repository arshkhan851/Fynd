import { CURRENT_CONSENT_DECLARATION_TEXT, CURRENT_CONSENT_DECLARATION_VERSION } from "./submission-consent";

export type AuthenticatedUser = { id: string; email: string; is_admin: boolean };

export type SubmissionCreatePayload = {
  title: string;
  description: string;
  organizer_name: string;
  organizer_email: string;
  organizer_contact_name?: string;
  organizer_contact_email?: string;
  organizer_contact_phone?: string;
  official_url: string;
  registration_url: string;
  registration_deadline_on: string;
  event_starts_on: string;
  event_ends_on: string;
  format: "online" | "offline" | "hybrid";
  location_name?: string;
  location_city?: string;
  location_region?: string;
  location_country_code?: string;
  registration_price_status: "free" | "paid" | "unknown";
  registration_fee_amount?: string;
  registration_fee_currency?: string;
  category_ids: number[];
  primary_category_id: number | null;
  consent: { declaration_version: string; declaration_text: string };
};

export type CreatedSubmission = { id: number; status: string; title: string };
export type SubmissionListItem = CreatedSubmission & { created_at: string };
const verificationRequests = new Map<string, Promise<void>>();
async function apiFetch(baseUrl: string, path: string, init?: RequestInit): Promise<Response> {
  if (!baseUrl) throw new Error("API_BASE_URL is not set");
  return fetch(new URL(path, baseUrl), {
    ...init,
    cache: "no-store",
    credentials: "include",
  });
}

export async function getCurrentUser(baseUrl: string): Promise<AuthenticatedUser | null> {
  try {
    const response = await apiFetch(baseUrl, "/api/v1/auth/me");
    if (response.status === 401) return null;
    if (!response.ok) throw new Error(`API returned HTTP ${response.status}`);
    return (await response.json()) as AuthenticatedUser;
  } catch (error) {
    if (error instanceof Error) throw error;
    throw new Error("Could not reach the API");
  }
}

export async function requestMagicLink(baseUrl: string, email: string): Promise<void> {
  const response = await apiFetch(baseUrl, "/api/v1/auth/request-link", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  if (!response.ok) throw new Error(response.status === 422 ? "Enter a valid email address." : "Could not request a sign-in link. Please try again.");
}

export async function verifyMagicLink(baseUrl: string, token: string): Promise<void> {
  const response = await apiFetch(baseUrl, "/api/v1/auth/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!response.ok) throw new Error("This sign-in link is invalid or has expired. Request a new one.");
}

export function verifyMagicLinkOnce(baseUrl: string, token: string): Promise<void> {
  const existing = verificationRequests.get(token);
  if (existing) return existing;
  const request = verifyMagicLink(baseUrl, token);
  verificationRequests.set(token, request);
  void request.then(
    () => verificationRequests.delete(token),
    () => verificationRequests.delete(token),
  );
  return request;
}

export async function getMySubmissions(baseUrl: string): Promise<{ items: SubmissionListItem[]; total: number }> {
  const response = await apiFetch(baseUrl, "/api/v1/submissions/me?limit=50&offset=0");
  if (!response.ok) throw new Error(response.status === 401 ? "Please sign in to view your submissions." : "Could not load your submissions.");
  return (await response.json()) as { items: SubmissionListItem[]; total: number };
}

export async function createSubmission(baseUrl: string, payload: Omit<SubmissionCreatePayload, "consent"> & { consentAccepted: true }): Promise<CreatedSubmission> {
  const { consentAccepted, ...event } = payload;
  if (!consentAccepted) throw new Error("Permission declaration must be accepted.");
  const response = await apiFetch(baseUrl, "/api/v1/submissions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...event,
      consent: {
        declaration_version: CURRENT_CONSENT_DECLARATION_VERSION,
        declaration_text: CURRENT_CONSENT_DECLARATION_TEXT,
      },
    } satisfies SubmissionCreatePayload),
  });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail = typeof body === "object" && body !== null && "detail" in body && typeof body.detail === "string"
      ? body.detail
      : `Submission failed (HTTP ${response.status}). Please review your details and try again.`;
    throw new Error(detail);
  }
  return (await response.json()) as CreatedSubmission;
}
