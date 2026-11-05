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
export type AdminSubmissionStatus = "pending" | "under_review" | "approved" | "rejected" | "withdrawn";
export type AdminReviewEvent = {
  id: number;
  reviewer_user_id: string;
  reviewer_email: string;
  previous_status: string;
  new_status: string;
  note: string | null;
  created_at: string;
};
export type AdminSubmissionQueueItem = {
  id: number;
  status: AdminSubmissionStatus;
  title: string;
  organizer_name: string;
  submitter_email: string;
  organizer_profile_id: string | null;
  created_at: string;
  updated_at: string;
  review_events: AdminReviewEvent[];
};
export type AdminSubmissionQueue = {
  items: AdminSubmissionQueueItem[];
  total: number;
  limit: number;
  offset: number;
};
export type AdminSubmissionDetail = AdminSubmissionQueueItem & {
  description: string;
  official_url: string;
  registration_url: string;
  registration_deadline_at: string | null;
  registration_deadline_on: string | null;
  event_starts_at: string | null;
  event_starts_on: string | null;
  event_ends_at: string | null;
  event_ends_on: string | null;
  event_timezone: string | null;
  format: string;
  location_name: string | null;
  location_city: string | null;
  location_region: string | null;
  location_country_code: string | null;
  registration_price_status: string;
  registration_fee_amount: string | null;
  registration_fee_currency: string | null;
  categories: { id: number; name: string; slug: string; is_primary: boolean }[];
  submitter_email: string;
  organizer_email: string;
  organizer_contact_name: string | null;
  organizer_contact_email: string | null;
  organizer_contact_phone: string | null;
  approved_competition_id: number | null;
  consent: { declaration_version: string; accepted_at: string; declaration_text: string };
};

export class AdminApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = "AdminApiError";
  }
}

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

export async function getAdminSubmissions(
  baseUrl: string,
  limit = 20,
  offset = 0,
): Promise<AdminSubmissionQueue> {
  const url = new URL("/api/v1/admin/submissions", baseUrl);
  url.searchParams.set("limit", String(limit));
  url.searchParams.set("offset", String(offset));
  url.searchParams.append("status", "pending");
  url.searchParams.append("status", "under_review");
  const response = await apiFetch(baseUrl, `${url.pathname}${url.search}`);
  if (!response.ok) {
    throw new AdminApiError(
      response.status,
      response.status === 401
        ? "Please sign in to access the admin dashboard."
        : response.status === 403
          ? "Administrator access is required."
          : `Could not load submissions (HTTP ${response.status}).`,
    );
  }
  return (await response.json()) as AdminSubmissionQueue;
}

export async function getAdminSubmission(
  baseUrl: string,
  submissionId: number,
): Promise<AdminSubmissionDetail> {
  if (!Number.isSafeInteger(submissionId) || submissionId < 1) {
    throw new AdminApiError(404, "Submission not found.");
  }
  const response = await apiFetch(
    baseUrl,
    `/api/v1/admin/submissions/${submissionId}`,
  );
  if (!response.ok) {
    throw new AdminApiError(
      response.status,
      response.status === 401
        ? "Please sign in to access this submission."
        : response.status === 403
          ? "Administrator access is required."
          : response.status === 404
            ? "Submission not found."
            : `Could not load submission (HTTP ${response.status}).`,
    );
  }
  return (await response.json()) as AdminSubmissionDetail;
}

export function validateRejectionReason(reason: string): string | null {
  const trimmed = reason.trim();
  if (trimmed.length < 10) return "Enter a rejection reason of at least 10 characters.";
  if (trimmed.length > 2000) return "Rejection reason must be 2000 characters or fewer.";
  return null;
}

async function submitAdminReviewAction(
  baseUrl: string,
  submissionId: number,
  action: "start-review" | "reject" | "approve",
  body?: { reason: string } | Record<string, never>,
): Promise<AdminSubmissionDetail> {
  if (!Number.isSafeInteger(submissionId) || submissionId < 1) {
    throw new AdminApiError(404, "Submission not found.");
  }
  const response = await apiFetch(baseUrl, `/api/v1/admin/submissions/${submissionId}/${action}`, {
    method: "POST",
    ...(body === undefined ? {} : {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  });
  if (!response.ok) {
    const data: unknown = await response.json().catch(() => null);
    const detail = typeof data === "object" && data !== null && "detail" in data
      ? data.detail
      : null;
    const message = typeof detail === "string"
      ? detail
      : response.status === 401
        ? "Please sign in to access this submission."
        : response.status === 403
          ? "Administrator access is required."
          : response.status === 404
            ? "Submission not found."
            : `Could not ${action === "start-review" ? "start review" : action === "reject" ? "reject" : "approve"} submission (HTTP ${response.status}).`;
    throw new AdminApiError(response.status, message);
  }
  return (await response.json()) as AdminSubmissionDetail;
}

export function startAdminSubmissionReview(baseUrl: string, submissionId: number): Promise<AdminSubmissionDetail> {
  return submitAdminReviewAction(baseUrl, submissionId, "start-review");
}

export function rejectAdminSubmission(baseUrl: string, submissionId: number, reason: string): Promise<AdminSubmissionDetail> {
  const validationError = validateRejectionReason(reason);
  if (validationError) return Promise.reject(new Error(validationError));
  return submitAdminReviewAction(baseUrl, submissionId, "reject", { reason: reason.trim() });
}

export function approveAdminSubmission(baseUrl: string, submissionId: number): Promise<AdminSubmissionDetail> {
  // The backend permits omitted selection IDs; an empty request explicitly leaves both unset.
  return submitAdminReviewAction(baseUrl, submissionId, "approve", {});
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
