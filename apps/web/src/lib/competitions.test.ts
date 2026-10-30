import { afterEach, describe, expect, it, vi } from "vitest";

import { getCompetition, getCompetitions } from "./competitions";

const competition = {
  id: 12,
  title: "Reviewed competition",
  format: null,
  official_url: null,
  event_starts_on: null,
  event_starts_at: null,
  registration_deadline_on: null,
  registration_deadline_at: null,
};

const competitionDetail = {
  ...competition,
  description: null,
  eligibility_summary: null,
  registration_url: null,
  registration_opens_on: null,
  registration_opens_at: null,
  event_ends_on: null,
  event_ends_at: null,
  event_timezone: null,
  categories: [],
  organizers: [],
  prizes: [],
  sources: [],
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("getCompetitions", () => {
  it("loads a typed competition page with the default pagination", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        Response.json({ items: [competition], total: 1, limit: 20, offset: 0 }),
      );

    expect(await getCompetitions("http://api.test")).toEqual({
      status: "ok",
      data: { items: [competition], total: 1, limit: 20, offset: 0 },
    });
    expect(fetch).toHaveBeenCalledWith(
      new URL("http://api.test/competitions?limit=20&offset=0"),
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("returns an empty competition page", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({ items: [], total: 0, limit: 20, offset: 0 }),
    );

    expect(await getCompetitions("http://api.test")).toEqual({
      status: "ok",
      data: { items: [], total: 0, limit: 20, offset: 0 },
    });
  });

  it("returns API and network errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("unavailable", { status: 503 }),
    );
    expect(await getCompetitions("http://api.test")).toEqual({
      status: "error",
      message: "API returned HTTP 503",
    });

    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("fetch failed"));
    expect(await getCompetitions("http://api.test")).toEqual({
      status: "error",
      message: "Could not reach the API",
    });
  });

  it("rejects an unexpected response shape", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({ items: [{ ...competition, title: null }], total: 1, limit: 20, offset: 0 }),
    );

    expect(await getCompetitions("http://api.test")).toEqual({
      status: "error",
      message: "Unexpected response from API",
    });
  });

  it("returns an error when the API base URL is missing", async () => {
    expect(await getCompetitions("")).toEqual({
      status: "error",
      message: "API_BASE_URL is not set",
    });
  });
});

describe("getCompetition", () => {
  it("loads a typed detail response", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(Response.json(competitionDetail));

    expect(await getCompetition("12", "http://api.test")).toEqual({
      status: "ok",
      data: competitionDetail,
    });
    expect(fetch).toHaveBeenCalledWith(
      new URL("http://api.test/competitions/12"),
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("maps missing and invalid IDs to not found without fetching", async () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    fetch.mockResolvedValue(new Response("missing", { status: 404 }));

    expect(await getCompetition("0", "http://api.test")).toEqual({
      status: "not-found",
    });
    expect(await getCompetition("12", "http://api.test")).toEqual({
      status: "not-found",
    });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("rejects unsafe links in a detail response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({
        ...competitionDetail,
        official_url: "javascript:alert(1)",
      }),
    );

    expect(await getCompetition("12", "http://api.test")).toEqual({
      status: "error",
      message: "Unexpected response from API",
    });
  });

  it("returns an unavailable error", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("unavailable", { status: 503 }),
    );

    expect(await getCompetition("12", "http://api.test")).toEqual({
      status: "error",
      message: "API returned HTTP 503",
    });
  });
});
