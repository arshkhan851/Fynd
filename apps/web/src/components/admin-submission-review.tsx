"use client";

import Link from "next/link";
import { type FormEvent, useEffect, useRef, useState } from "react";

import {
  AdminApiError,
  AdminSubmissionDetail,
  AdminSubmissionStatus,
  approveAdminSubmission,
  getAdminSubmission,
  getCurrentUser,
  rejectAdminSubmission,
  startAdminSubmissionReview,
  validateRejectionReason,
} from "@/lib/submissions";
import { resolveAdminAccess } from "@/lib/admin-access";

type ReviewState =
  | { type: "checking" }
  | { type: "sign-in" }
  | { type: "denied" }
  | { type: "error"; message: string }
  | { type: "loading" }
  | { type: "ready"; submission: AdminSubmissionDetail };

type ReviewAction = "start-review" | "reject" | "approve";

export function nextSubmissionStatus(status: AdminSubmissionStatus, action: ReviewAction): AdminSubmissionStatus | null {
  if (status === "pending" && action === "start-review") return "under_review";
  if (status === "under_review" && action === "reject") return "rejected";
  if (status === "under_review" && action === "approve") return "approved";
  return null;
}

function dateText(value: string | null): string {
  if (!value) return "Not provided";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function Detail({ label, value }: { label: string; value: string | null }) {
  return <div className="admin-detail-item"><dt>{label}</dt><dd>{value || "Not provided"}</dd></div>;
}

export function AdminReviewActions({
  status,
  busyAction,
  reason,
  reasonError,
  error,
  success,
  onReasonChange,
  onStartReview,
  onReject,
  onApprove,
}: {
  status: AdminSubmissionStatus;
  busyAction: ReviewAction | null;
  reason: string;
  reasonError: string | null;
  error: string | null;
  success: string | null;
  onReasonChange: (value: string) => void;
  onStartReview: () => void;
  onReject: (event: FormEvent<HTMLFormElement>) => void;
  onApprove: () => void;
}) {
  if (status !== "pending" && status !== "under_review") return null;
  const busy = busyAction !== null;
  return <section className="admin-detail-section" aria-labelledby="admin-actions-heading">
    <p className="eyebrow">Administrator actions</p>
    <h2 id="admin-actions-heading">Review decision</h2>
    {error && <p className="state-panel state-panel--error" role="alert">{error}</p>}
    {success && <p className="state-panel" role="status">{success}</p>}
    {status === "pending" && <button className="button-link" type="button" disabled={busy} onClick={onStartReview}>{busyAction === "start-review" ? "Starting review…" : "Start review"}</button>}
    {status === "under_review" && <div className="admin-review-actions">
      <form onSubmit={onReject}>
        <label htmlFor="rejection-reason">Rejection reason</label>
        <textarea id="rejection-reason" value={reason} maxLength={2000} onChange={(event) => onReasonChange(event.target.value)} aria-describedby={reasonError ? "rejection-reason-error" : undefined} />
        {reasonError && <p id="rejection-reason-error" className="field-error" role="alert">{reasonError}</p>}
        <button className="button-link" type="submit" disabled={busy}>{busyAction === "reject" ? "Rejecting…" : "Reject submission"}</button>
      </form>
      <div className="admin-approve-action">
        <p>Approving publishes this competition on Fynd.</p>
        <button className="button-link" type="button" disabled={busy} onClick={onApprove}>{busyAction === "approve" ? "Approving…" : "Approve and publish"}</button>
      </div>
    </div>}
  </section>;
}

export function ReviewView({
  state,
  onRetry,
  busyAction = null,
  reason = "",
  reasonError = null,
  actionError = null,
  actionSuccess = null,
  onReasonChange = () => undefined,
  onStartReview = () => undefined,
  onReject = () => undefined,
  onApprove = () => undefined,
}: {
  state: ReviewState;
  onRetry: () => void;
  busyAction?: ReviewAction | null;
  reason?: string;
  reasonError?: string | null;
  actionError?: string | null;
  actionSuccess?: string | null;
  onReasonChange?: (value: string) => void;
  onStartReview?: () => void;
  onReject?: (event: FormEvent<HTMLFormElement>) => void;
  onApprove?: () => void;
}) {
  if (state.type === "checking" || state.type === "loading") return <main className="page-shell submission-page"><div className="state-panel" role="status">{state.type === "checking" ? "Checking administrator access…" : "Loading submission…"}</div></main>;
  if (state.type === "sign-in") return <main className="page-shell submission-page"><div className="state-panel"><h1>Sign in to continue</h1><Link href="/login?next=%2Fadmin">Sign in</Link></div></main>;
  if (state.type === "denied") return <main className="page-shell submission-page"><div className="state-panel state-panel--error" role="alert"><h1>Access denied</h1><p>Administrator access is required.</p></div></main>;
  if (state.type === "error") return <main className="page-shell submission-page"><div className="state-panel state-panel--error" role="alert"><h1>Could not load submission</h1><p>{state.message}</p><button className="button-link" type="button" onClick={onRetry}>Try again</button></div></main>;

  const submission = state.submission;
  const dateRange = [submission.event_starts_at || submission.event_starts_on, submission.event_ends_at || submission.event_ends_on].map(dateText).join(" – ");
  return <main className="page-shell submission-page admin-review-page">
    <Link className="back-link" href="/admin">← Back to review queue</Link>
    <header className="admin-review-header"><div><p className="eyebrow">Submission #{submission.id}</p><h1 className="editorial-heading">{submission.title}</h1><p className="admin-review-subtitle">Submitted by {submission.submitter_email} on {dateText(submission.created_at)}</p></div><span className={`status-pill status-pill--${submission.status}`}>{submission.status.replaceAll("_", " ")}</span></header>
    {actionSuccess && submission.status !== "pending" && submission.status !== "under_review" && <p className="state-panel" role="status">{actionSuccess}</p>}
    <AdminReviewActions status={submission.status} busyAction={busyAction} reason={reason} reasonError={reasonError} error={actionError} success={actionSuccess} onReasonChange={onReasonChange} onStartReview={onStartReview} onReject={onReject} onApprove={onApprove} />
    <section className="admin-detail-section"><p className="eyebrow">Event</p><h2>Hackathon information</h2><p className="admin-description">{submission.description}</p><dl className="admin-detail-grid">
      <Detail label="Organizer" value={submission.organizer_name} /><Detail label="Organizer email" value={submission.organizer_email} /><Detail label="Format" value={submission.format} /><Detail label="Event dates" value={dateRange} /><Detail label="Registration deadline" value={dateText(submission.registration_deadline_at || submission.registration_deadline_on)} /><Detail label="Timezone" value={submission.event_timezone} />
      <Detail label="Official website" value={submission.official_url} /><Detail label="Registration URL" value={submission.registration_url} /><Detail label="Location" value={[submission.location_name, submission.location_city, submission.location_region, submission.location_country_code].filter(Boolean).join(", ") || null} /><Detail label="Registration price" value={submission.registration_price_status === "paid" ? `${submission.registration_fee_amount ?? ""} ${submission.registration_fee_currency ?? ""}`.trim() : submission.registration_price_status} />
      <Detail label="Contact name" value={submission.organizer_contact_name} /><Detail label="Contact email" value={submission.organizer_contact_email} /><Detail label="Contact phone" value={submission.organizer_contact_phone} />
    </dl></section>
    <section className="admin-detail-section"><p className="eyebrow">Categories</p><h2>Selected categories</h2>{submission.categories.length ? <ul className="admin-category-list">{submission.categories.map((category) => <li key={category.id}>{category.name}{category.is_primary && <span>Primary</span>}</li>)}</ul> : <p>No categories selected.</p>}</section>
    <section className="admin-detail-section"><p className="eyebrow">Permission</p><h2>Display consent</h2><p className="admin-description">{submission.consent.declaration_text}</p><p className="submission-hint">Version {submission.consent.declaration_version}; accepted {dateText(submission.consent.accepted_at)}.</p></section>
    <section className="admin-detail-section"><p className="eyebrow">History</p><h2>Review events</h2>{submission.review_events.length ? <ol className="admin-review-events">{submission.review_events.map((event) => <li key={event.id}><strong>{event.previous_status.replaceAll("_", " ")} → {event.new_status.replaceAll("_", " ")}</strong><span>{event.reviewer_email} · {dateText(event.created_at)}</span>{event.note && <p>{event.note}</p>}</li>)}</ol> : <p>No review actions have been recorded.</p>}</section>
  </main>;
}

export function AdminSubmissionReview({ apiBaseUrl, submissionId }: { apiBaseUrl: string; submissionId: number }) {
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState<ReviewState>({ type: "checking" });
  const [busyAction, setBusyAction] = useState<ReviewAction | null>(null);
  const [reason, setReason] = useState("");
  const [reasonError, setReasonError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);
  const actionLock = useRef(false);
  useEffect(() => {
    let active = true;
    getCurrentUser(apiBaseUrl).then((user) => {
      if (!active) return;
      const access = resolveAdminAccess(user);
      if (access !== "allowed") { setState({ type: access }); return; }
      setState({ type: "loading" });
      return getAdminSubmission(apiBaseUrl, submissionId).then((submission) => {
        if (active) setState({ type: "ready", submission });
      });
    }).catch((error: unknown) => {
      if (!active) return;
      if (error instanceof AdminApiError && error.status === 401) setState({ type: "sign-in" });
      else if (error instanceof AdminApiError && error.status === 403) setState({ type: "denied" });
      else setState({ type: "error", message: error instanceof Error ? error.message : "Please try again." });
    });
    return () => { active = false; };
  }, [apiBaseUrl, submissionId, retry]);

  async function performAction(action: ReviewAction, request: () => Promise<AdminSubmissionDetail>) {
    if (actionLock.current || state.type !== "ready" || !nextSubmissionStatus(state.submission.status, action)) return;
    actionLock.current = true;
    setBusyAction(action);
    setActionError(null);
    setActionSuccess(null);
    try {
      const updated = await request();
      setState({ type: "ready", submission: updated });
      setReason("");
      setReasonError(null);
      setActionSuccess(action === "approve" ? "Submission approved and competition published." : action === "reject" ? "Submission rejected." : "Submission moved to under review.");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not complete the review action. Please try again.");
    } finally {
      actionLock.current = false;
      setBusyAction(null);
    }
  }

  function handleReject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const validationError = validateRejectionReason(reason);
    if (validationError) { setReasonError(validationError); return; }
    setReasonError(null);
    void performAction("reject", () => rejectAdminSubmission(apiBaseUrl, submissionId, reason));
  }

  return <ReviewView
    state={state}
    onRetry={() => setRetry((value) => value + 1)}
    busyAction={busyAction}
    reason={reason}
    reasonError={reasonError}
    actionError={actionError}
    actionSuccess={actionSuccess}
    onReasonChange={(value) => { setReason(value); setReasonError(null); }}
    onStartReview={() => void performAction("start-review", () => startAdminSubmissionReview(apiBaseUrl, submissionId))}
    onReject={handleReject}
    onApprove={() => void performAction("approve", () => approveAdminSubmission(apiBaseUrl, submissionId))}
  />;
}
