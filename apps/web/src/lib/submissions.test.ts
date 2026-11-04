import { afterEach, describe, expect, it, vi } from "vitest";

import { createSubmission, getCurrentUser, requestMagicLink, verifyMagicLink, verifyMagicLinkOnce } from "./submissions";
import { CURRENT_CONSENT_DECLARATION_TEXT, CURRENT_CONSENT_DECLARATION_VERSION } from "./submission-consent";

afterEach(() => vi.restoreAllMocks());

describe("submission API client", () => {
  it("checks the authenticated user with cookie credentials", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json({ id: "u1", email: "student@example.org", is_admin: false }));
    expect(await getCurrentUser("http://api.test")).toMatchObject({ id: "u1" });
    expect(fetch.mock.calls[0][0]).toEqual(new URL("http://api.test/api/v1/auth/me"));
    expect(fetch.mock.calls[0][1]).toMatchObject({ credentials: "include", cache: "no-store" });
  });

  it("returns signed out for an unauthorized session", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 401 }));
    expect(await getCurrentUser("http://api.test")).toBeNull();
  });

  it("requests and verifies magic links against existing auth endpoints", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 202 }));
    await requestMagicLink("http://api.test", "student@example.org");
    expect(fetch.mock.calls[0][0]).toEqual(new URL("http://api.test/api/v1/auth/request-link"));
    fetch.mockResolvedValueOnce(Response.json({ message: "Successfully authenticated." }));
    await verifyMagicLink("http://api.test", "opaque-token");
    expect(fetch.mock.calls[1][0]).toEqual(new URL("http://api.test/api/v1/auth/verify"));
  });

  it("coalesces duplicate verification attempts for one magic token", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json({ message: "ok" }));
    await Promise.all([
      verifyMagicLinkOnce("http://api.test", "single-use-token-unique"),
      verifyMagicLinkOnce("http://api.test", "single-use-token-unique"),
    ]);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("sends the exact current consent declaration and event schema", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json({ id: 8, status: "pending", title: "Campus Hack" }, { status: 201 }));
    await createSubmission("http://api.test", {
      title: "Campus Hack", description: "Build things", organizer_name: "Club",
      organizer_email: "club@example.org", official_url: "https://club.example.org",
      registration_url: "https://club.example.org/register", format: "online",
      registration_deadline_on: "2026-12-01", event_starts_on: "2026-12-05",
      event_ends_on: "2026-12-06", registration_price_status: "free",
      category_ids: [], primary_category_id: null, consentAccepted: true,
    });
    const request = fetch.mock.calls[0][1];
    const body = JSON.parse(String(request?.body));
    expect(fetch.mock.calls[0][0]).toEqual(new URL("http://api.test/api/v1/submissions"));
    expect(body).toMatchObject({
      category_ids: [], primary_category_id: null,
      consent: { declaration_version: CURRENT_CONSENT_DECLARATION_VERSION, declaration_text: CURRENT_CONSENT_DECLARATION_TEXT },
    });
    expect(body.submitter_user_id).toBeUndefined();
  });
});
