import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  performSaveOpportunity,
  SaveOpportunityButton,
  SaveOpportunityButtonView,
} from "./save-opportunity-button";
import * as savedOpportunitiesApi from "../lib/saved-opportunities";
import { SavedOpportunitiesApiError } from "../lib/saved-opportunities";

afterEach(() => vi.restoreAllMocks());

describe("SaveOpportunityButton", () => {
  it("does not save merely by rendering", () => {
    const save = vi.spyOn(savedOpportunitiesApi, "saveOpportunity");

    const html = renderToStaticMarkup(
      createElement(SaveOpportunityButton, {
        competitionId: 42,
        apiBaseUrl: "http://api.test",
      }),
    );

    expect(html).toContain("Save opportunity");
    expect(html).not.toContain("Saved");
    expect(save).not.toHaveBeenCalled();
  });

  it("shows a disabled loading state", () => {
    const html = renderToStaticMarkup(
      createElement(SaveOpportunityButtonView, {
        competitionId: 42,
        state: { type: "saving" },
        onSave: vi.fn(),
      }),
    );

    expect(html).toContain("Saving…");
    expect(html).toContain('disabled=""');
    expect(html).toContain('aria-busy="true"');
  });

  it("shows success only after the save action confirms success", async () => {
    const save = vi
      .spyOn(savedOpportunitiesApi, "saveOpportunity")
      .mockResolvedValue({ competition_id: 42, saved: true });

    await expect(performSaveOpportunity("http://api.test", 42)).resolves.toEqual({
      type: "saved",
    });
    expect(save).toHaveBeenCalledWith("http://api.test", 42);

    const html = renderToStaticMarkup(
      createElement(SaveOpportunityButtonView, {
        competitionId: 42,
        state: { type: "saved" },
        onSave: vi.fn(),
      }),
    );
    expect(html).toContain("Added to your saved opportunities.");
    expect(html).toContain('role="status"');
  });

  it("shows a safe API error and allows a later retry", async () => {
    vi.spyOn(savedOpportunitiesApi, "saveOpportunity").mockRejectedValue(
      new SavedOpportunitiesApiError(503, "Saved opportunities request failed (HTTP 503)."),
    );

    const state = await performSaveOpportunity("http://api.test", 42);
    expect(state).toEqual({
      type: "error",
      message: "Saved opportunities request failed (HTTP 503).",
    });
    const html = renderToStaticMarkup(
      createElement(SaveOpportunityButtonView, {
        competitionId: 42,
        state,
        onSave: vi.fn(),
      }),
    );
    expect(html).toContain('role="alert"');
    expect(html).toContain("HTTP 503");
    expect(html).toContain("Save opportunity");
  });

  it("shows a sign-in link returning to the competition without retrying", async () => {
    const save = vi.spyOn(savedOpportunitiesApi, "saveOpportunity").mockRejectedValue(
      new SavedOpportunitiesApiError(401, "Please sign in to manage saved opportunities."),
    );

    const state = await performSaveOpportunity("http://api.test", 42);
    expect(state).toEqual({ type: "sign-in" });
    expect(save).toHaveBeenCalledTimes(1);

    const html = renderToStaticMarkup(
      createElement(SaveOpportunityButtonView, {
        competitionId: 42,
        state,
        onSave: vi.fn(),
      }),
    );
    expect(html).toContain('href="/login?next=%2Fcompetitions%2F42"');
    expect(html).toContain("After signing in, return here and choose Save again.");
    expect(save).toHaveBeenCalledTimes(1);
  });
});
