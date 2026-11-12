import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getSavedOpportunities,
  removeSavedOpportunity,
  saveOpportunity,
  SavedOpportunitiesApiError,
} from "./saved-opportunities";

afterEach(() => vi.restoreAllMocks());

const savedItem = {
  competition_id: 42,
  title: "Campus Build Challenge",
  format: "online",
  event_starts_on: "2026-12-10",
  event_starts_at: null,
  registration_deadline_on: "2026-12-01",
  registration_deadline_at: null,
  saved_at: "2026-11-12T10:00:00Z",
};

describe("Saved Opportunities API client", () => {
  it("gets and parses a paginated response with the requested query parameters", async () => {
    const page = { items: [savedItem], total: 21, limit: 10, offset: 10 };
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json(page));

    expect(await getSavedOpportunities("http://api.test", 10, 10)).toEqual(page);
    expect(fetch).toHaveBeenCalledWith(
      new URL("http://api.test/api/v1/saved-opportunities?limit=10&offset=10"),
      expect.objectContaining({ credentials: "include", cache: "no-store" }),
    );
  });

  it("saves a competition with PUT and parses the mutation response", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({ competition_id: 42, saved: true }),
    );

    expect(await saveOpportunity("http://api.test", 42)).toEqual({
      competition_id: 42,
      saved: true,
    });
    expect(fetch).toHaveBeenCalledWith(
      new URL("http://api.test/api/v1/saved-opportunities/42"),
      expect.objectContaining({ method: "PUT", credentials: "include", cache: "no-store" }),
    );
  });

  it("removes a saved competition with DELETE", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({ competition_id: 42, saved: false }),
    );

    expect(await removeSavedOpportunity("http://api.test", 42)).toEqual({
      competition_id: 42,
      saved: false,
    });
    expect(fetch).toHaveBeenCalledWith(
      new URL("http://api.test/api/v1/saved-opportunities/42"),
      expect.objectContaining({ method: "DELETE", credentials: "include", cache: "no-store" }),
    );
  });

  it.each([
    ["list", () => getSavedOpportunities("http://api.test")],
    ["save", () => saveOpportunity("http://api.test", 42)],
    ["remove", () => removeSavedOpportunity("http://api.test", 42)],
  ])("reports HTTP 401 distinctly for %s", async (_operation, request) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({ detail: "Unauthorized" }, { status: 401 }),
    );

    await expect(request()).rejects.toMatchObject({
      name: "SavedOpportunitiesApiError",
      status: 401,
      message: "Please sign in to manage saved opportunities.",
    });
  });

  it("reports other HTTP failures without exposing response details", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({ detail: "internal database credentials" }, { status: 503 }),
    );

    await expect(getSavedOpportunities("http://api.test")).rejects.toMatchObject({
      name: "SavedOpportunitiesApiError",
      status: 503,
      message: "Saved opportunities request failed (HTTP 503).",
    });
  });

  it("rejects malformed successful responses", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({ items: [{ ...savedItem, title: null }], total: 1, limit: 20, offset: 0 }),
    );

    await expect(getSavedOpportunities("http://api.test")).rejects.toBeInstanceOf(
      SavedOpportunitiesApiError,
    );
  });
});
