import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { AdminDashboardView } from "./admin-dashboard-view";
import type { AdminSubmissionQueue } from "@/lib/submissions";

const emptyPage: AdminSubmissionQueue = { items: [], total: 0, limit: 20, offset: 0 };
const queuePage: AdminSubmissionQueue = {
  items: [
    { id: 31, status: "pending", title: "HackAWorld", organizer_name: "Campus Tech Club", submitter_email: "submitter@example.org", organizer_profile_id: null, created_at: "2026-06-01T10:00:00Z", updated_at: "2026-06-01T10:00:00Z", review_events: [] },
    { id: 32, status: "under_review", title: "Build for Bharat", organizer_name: "Student Founders", submitter_email: "another@example.org", organizer_profile_id: null, created_at: "2026-06-02T10:00:00Z", updated_at: "2026-06-02T10:00:00Z", review_events: [] },
  ],
  total: 22,
  limit: 2,
  offset: 0,
};

describe("AdminDashboardView", () => {
  it("shows loading and error states", () => {
    const loading = renderToStaticMarkup(<AdminDashboardView state={{ type: "loading" }} onRetry={vi.fn()} onPage={vi.fn()} />);
    const error = renderToStaticMarkup(<AdminDashboardView state={{ type: "error", message: "API unavailable" }} onRetry={vi.fn()} onPage={vi.fn()} />);
    expect(loading).toContain("Loading submissions");
    expect(error).toContain("API unavailable");
  });

  it("renders pending and under-review submissions with review links", () => {
    const html = renderToStaticMarkup(<AdminDashboardView state={{ type: "results", page: queuePage }} onRetry={vi.fn()} onPage={vi.fn()} />);
    expect(html).toContain("HackAWorld");
    expect(html).toContain("Campus Tech Club");
    expect(html).toContain("pending");
    expect(html).toContain("Build for Bharat");
    expect(html).toContain("under review");
    expect(html).toContain('href="/admin/submissions/31"');
    expect(html).toContain("Open for review");
    expect(html).toContain("Previous");
    expect(html).toContain("Next");
  });

  it("shows an empty queue state", () => {
    const html = renderToStaticMarkup(<AdminDashboardView state={{ type: "empty", page: emptyPage }} onRetry={vi.fn()} onPage={vi.fn()} />);
    expect(html).toContain("Queue is clear");
  });

  it("shows distinct sign-in and access-denied states", () => {
    const signIn = renderToStaticMarkup(<AdminDashboardView state={{ type: "sign-in" }} onRetry={vi.fn()} onPage={vi.fn()} />);
    const denied = renderToStaticMarkup(<AdminDashboardView state={{ type: "denied" }} onRetry={vi.fn()} onPage={vi.fn()} />);
    expect(signIn).toContain("Sign in to continue");
    expect(signIn).toContain('href="/login?next=%2Fadmin"');
    expect(denied).toContain("Access denied");
  });
});
