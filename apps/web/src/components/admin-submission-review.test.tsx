import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AdminReviewActions, ReviewView, nextSubmissionStatus } from "./admin-submission-review";
import type { AdminSubmissionDetail } from "@/lib/submissions";

const detail: AdminSubmissionDetail = {
  id: 31,
  status: "pending",
  title: "HackAWorld",
  organizer_name: "Campus Tech Club",
  submitter_email: "submitter@example.org",
  organizer_profile_id: null,
  created_at: "2026-06-01T10:00:00Z",
  updated_at: "2026-06-01T10:00:00Z",
  review_events: [],
  description: "A student hackathon.",
  official_url: "https://example.org",
  registration_url: "https://example.org/register",
  registration_deadline_at: null,
  registration_deadline_on: null,
  event_starts_at: null,
  event_starts_on: "2026-07-01",
  event_ends_at: null,
  event_ends_on: "2026-07-02",
  event_timezone: null,
  format: "online",
  location_name: null,
  location_city: null,
  location_region: null,
  location_country_code: null,
  registration_price_status: "free",
  registration_fee_amount: null,
  registration_fee_currency: null,
  categories: [],
  organizer_email: "organizer@example.org",
  organizer_contact_name: null,
  organizer_contact_email: null,
  organizer_contact_phone: null,
  approved_competition_id: null,
  consent: { declaration_version: "1", accepted_at: "2026-06-01T10:00:00Z", declaration_text: "Consent." },
};

const noop = () => undefined;

describe("admin submission review actions", () => {
  it("shows only Start review for a pending submission", () => {
    const html = renderToStaticMarkup(<AdminReviewActions status="pending" busyAction={null} reason="" reasonError={null} error={null} success={null} onReasonChange={noop} onStartReview={noop} onReject={noop} onApprove={noop} />);
    expect(html).toContain("Start review");
    expect(html).not.toContain("Reject submission");
    expect(html).not.toContain("Approve and publish");
  });

  it("shows approval and rejection for under-review submissions, and disables actions while busy", () => {
    const html = renderToStaticMarkup(<AdminReviewActions status="under_review" busyAction="approve" reason="" reasonError={null} error={null} success={null} onReasonChange={noop} onStartReview={noop} onReject={noop} onApprove={noop} />);
    expect(html).toContain("Reject submission");
    expect(html).toContain("Approving…");
    expect(html).toContain("Approving publishes this competition on Fynd.");
    expect((html.match(/disabled=""/g) ?? []).length).toBe(2);
  });

  it("does not show actions for terminal statuses", () => {
    const html = renderToStaticMarkup(<AdminReviewActions status="approved" busyAction={null} reason="" reasonError={null} error={null} success={null} onReasonChange={noop} onStartReview={noop} onReject={noop} onApprove={noop} />);
    expect(html).toBe("");
    expect(nextSubmissionStatus("pending", "start-review")).toBe("under_review");
    expect(nextSubmissionStatus("pending", "approve")).toBeNull();
    expect(nextSubmissionStatus("under_review", "reject")).toBe("rejected");
    expect(nextSubmissionStatus("rejected", "approve")).toBeNull();
  });

  it("renders action failures and confirmed updated status messages", () => {
    const failed = renderToStaticMarkup(<AdminReviewActions status="under_review" busyAction={null} reason="" reasonError={"Enter a rejection reason of at least 10 characters."} error="The server rejected this action." success={null} onReasonChange={noop} onStartReview={noop} onReject={noop} onApprove={noop} />);
    expect(failed).toContain("The server rejected this action.");
    expect(failed).toContain("Enter a rejection reason");
    const success = renderToStaticMarkup(<AdminReviewActions status="approved" busyAction={null} reason="" reasonError={null} error={null} success="Submission approved and competition published." onReasonChange={noop} onStartReview={noop} onReject={noop} onApprove={noop} />);
    expect(success).toBe("");
    const ready = renderToStaticMarkup(<ReviewView state={{ type: "ready", submission: { ...detail, status: "approved" } }} onRetry={noop} />);
    expect(ready).toContain("approved");
    const confirmed = renderToStaticMarkup(<ReviewView state={{ type: "ready", submission: { ...detail, status: "approved" } }} onRetry={noop} actionSuccess="Submission approved and competition published." />);
    expect(confirmed).toContain("Submission approved and competition published.");
  });

  it("does not render actions for signed-out or non-admin access states", () => {
    const signedOut = renderToStaticMarkup(<ReviewView state={{ type: "sign-in" }} onRetry={noop} />);
    const denied = renderToStaticMarkup(<ReviewView state={{ type: "denied" }} onRetry={noop} />);
    expect(signedOut).toContain("Sign in to continue");
    expect(signedOut).not.toContain("Start review");
    expect(denied).toContain("Access denied");
    expect(denied).not.toContain("Approve and publish");
  });

  it("shows action loading and server-confirmed success status", () => {
    const loading = renderToStaticMarkup(<AdminReviewActions status="pending" busyAction="start-review" reason="" reasonError={null} error={null} success={null} onReasonChange={noop} onStartReview={noop} onReject={noop} onApprove={noop} />);
    expect(loading).toContain("Starting review…");
    const updated = renderToStaticMarkup(<ReviewView state={{ type: "ready", submission: { ...detail, status: "under_review" } }} onRetry={noop} actionSuccess="Submission moved to under review." />);
    expect(updated).toContain("Submission moved to under review.");
    expect(updated).toContain("under review");
  });
});
