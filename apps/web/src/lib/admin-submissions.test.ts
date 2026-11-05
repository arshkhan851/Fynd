import { afterEach, describe, expect, it, vi } from "vitest";

import {
  approveAdminSubmission,
  getAdminSubmission,
  getAdminSubmissions,
  rejectAdminSubmission,
  startAdminSubmissionReview,
  validateRejectionReason,
} from "./submissions";

afterEach(() => vi.restoreAllMocks());

describe("admin submissions API client", () => {
  it("requests the protected pending and under-review queue with pagination and credentials", async () => {
    const page = { items: [], total: 0, limit: 10, offset: 20 };
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json(page));
    expect(await getAdminSubmissions("http://api.test", 10, 20)).toEqual(page);
    const url = fetch.mock.calls[0][0] as URL;
    expect(url.pathname).toBe("/api/v1/admin/submissions");
    expect(url.searchParams.get("limit")).toBe("10");
    expect(url.searchParams.get("offset")).toBe("20");
    expect(url.searchParams.getAll("status")).toEqual(["pending", "under_review"]);
    expect(fetch.mock.calls[0][1]).toMatchObject({ credentials: "include", cache: "no-store" });
  });

  it("surfaces backend authorization responses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 403 }));
    await expect(getAdminSubmissions("http://api.test")).rejects.toMatchObject({
      name: "AdminApiError",
      status: 403,
    });
  });

  it("loads the admin detail endpoint for the selected submission", async () => {
    const detail = { id: 31, status: "pending", title: "HackAWorld" };
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json(detail));
    expect(await getAdminSubmission("http://api.test", 31)).toEqual(detail);
    expect(fetch.mock.calls[0][0]).toEqual(new URL("http://api.test/api/v1/admin/submissions/31"));
  });

  it("posts start-review and returns the server's updated detail", async () => {
    const updated = { id: 31, status: "under_review", title: "HackAWorld" };
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json(updated));
    expect(await startAdminSubmissionReview("http://api.test", 31)).toEqual(updated);
    expect(fetch.mock.calls[0][0]).toEqual(new URL("http://api.test/api/v1/admin/submissions/31/start-review"));
    expect(fetch.mock.calls[0][1]).toMatchObject({ method: "POST", credentials: "include" });
  });

  it("posts the trimmed rejection reason and rejects reasons below the backend minimum", async () => {
    expect(validateRejectionReason("  Too short ")).toContain("at least 10");
    expect(validateRejectionReason("          ")).toContain("at least 10");
    expect(validateRejectionReason("A sufficiently meaningful reason.")).toBeNull();
    const updated = { id: 31, status: "rejected", title: "HackAWorld" };
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json(updated));
    expect(await rejectAdminSubmission("http://api.test", 31, "  This event does not meet the criteria.  ")).toEqual(updated);
    expect(fetch.mock.calls[0][0]).toEqual(new URL("http://api.test/api/v1/admin/submissions/31/reject"));
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({ reason: "This event does not meet the criteria." });
    await expect(rejectAdminSubmission("http://api.test", 31, "short")).rejects.toThrow("at least 10");
  });

  it("approves without selecting an existing competition or organizer", async () => {
    const updated = { id: 31, status: "approved", title: "HackAWorld" };
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json(updated));
    expect(await approveAdminSubmission("http://api.test", 31)).toEqual(updated);
    expect(fetch.mock.calls[0][0]).toEqual(new URL("http://api.test/api/v1/admin/submissions/31/approve"));
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({});
  });

  it("surfaces backend review-action errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json({ detail: "Submission must be under review." }, { status: 409 }));
    await expect(approveAdminSubmission("http://api.test", 31)).rejects.toMatchObject({
      name: "AdminApiError",
      status: 409,
      message: "Submission must be under review.",
    });
  });
});
