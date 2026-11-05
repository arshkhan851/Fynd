import Link from "next/link";

import type { AdminSubmissionQueue } from "@/lib/submissions";

export type AdminDashboardState =
  | { type: "checking" }
  | { type: "sign-in" }
  | { type: "denied" }
  | { type: "error"; message: string }
  | { type: "loading" }
  | { type: "empty"; page: AdminSubmissionQueue }
  | { type: "results"; page: AdminSubmissionQueue };

function dateLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Date unavailable" : date.toLocaleDateString();
}

export function AdminDashboardView({
  state,
  onRetry,
  onPage,
}: {
  state: AdminDashboardState;
  onRetry: () => void;
  onPage: (offset: number) => void;
}) {
  return (
    <main className="page-shell submission-page admin-page">
      <p className="eyebrow">Fynd administration</p>
      <h1 className="editorial-heading">Submission review</h1>
      {state.type === "checking" && <div className="state-panel" role="status">Checking administrator access…</div>}
      {state.type === "sign-in" && <div className="state-panel" role="status"><h2>Sign in to continue</h2><p>This dashboard is available to Fynd administrators.</p><Link className="button-link submission-inline-link" href="/login?next=%2Fadmin">Sign in</Link></div>}
      {state.type === "denied" && <div className="state-panel state-panel--error" role="alert"><h2>Access denied</h2><p>Your account does not have administrator access.</p><Link href="/">Return to Fynd</Link></div>}
      {state.type === "error" && <div className="state-panel state-panel--error" role="alert"><h2>Could not load the review queue</h2><p>{state.message}</p><button className="button-link" type="button" onClick={onRetry}>Try again</button></div>}
      {state.type === "loading" && <div className="state-panel" role="status">Loading submissions…</div>}
      {(state.type === "empty" || state.type === "results") && <>
        <div className="admin-queue-heading"><div><p className="eyebrow">Pending and under review</p><h2>{state.page.total} {state.page.total === 1 ? "submission" : "submissions"}</h2></div></div>
        {state.type === "empty" ? <div className="state-panel" role="status"><h2>Queue is clear</h2><p>There are no pending or under-review submissions.</p></div> : <ul className="admin-queue">{state.page.items.map((item) => <li key={item.id}>
          <div className="admin-queue__main"><h3>{item.title}</h3><p>Organized by {item.organizer_name}</p><p>Submitted {dateLabel(item.created_at)}</p></div>
          <span className={`status-pill status-pill--${item.status}`}>{item.status.replaceAll("_", " ")}</span>
          <Link className="admin-review-link" href={`/admin/submissions/${item.id}`}>Open for review <span aria-hidden="true">→</span></Link>
        </li>)}</ul>}
        {state.page.total > state.page.limit && <nav className="admin-pagination" aria-label="Submission queue pages">
          <button type="button" disabled={state.page.offset === 0} onClick={() => onPage(Math.max(0, state.page.offset - state.page.limit))}>← Previous</button>
          <span>Showing {state.page.offset + 1}–{Math.min(state.page.offset + state.page.items.length, state.page.total)} of {state.page.total}</span>
          <button type="button" disabled={state.page.offset + state.page.limit >= state.page.total} onClick={() => onPage(state.page.offset + state.page.limit)}>Next →</button>
        </nav>}
      </>}
    </main>
  );
}
