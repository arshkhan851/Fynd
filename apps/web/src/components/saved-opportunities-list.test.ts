import { createElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SiteHeader } from "./site-header";
import {
  completeSavedOpportunityRemoval,
  nextSavedOffset,
  previousSavedOffset,
  removeOpportunityFromPage,
  requestSavedOpportunityRemoval,
  resolveRemovalFailure,
  SavedOpportunitiesList,
  SavedOpportunitiesListView,
  type SavedOpportunitiesListState,
} from "./saved-opportunities-list";
import * as savedApi from "../lib/saved-opportunities";
import type { SavedOpportunityPage } from "../lib/saved-opportunities";

afterEach(() => vi.restoreAllMocks());

const item = (competitionId: number) => ({
  competition_id: competitionId,
  title: `Competition ${competitionId}`,
  format: "online",
  event_starts_on: "2026-12-10",
  event_starts_at: null,
  registration_deadline_on: "2026-12-01",
  registration_deadline_at: null,
  saved_at: "2026-11-12T10:00:00Z",
});

function page(
  ids: number[],
  { total = ids.length, limit = 2, offset = 0 }: { total?: number; limit?: number; offset?: number } = {},
): SavedOpportunityPage {
  return { items: ids.map(item), total, limit, offset };
}

const initialPage = page([31, 32]);

type ButtonProps = {
  children?: ReactNode;
  "aria-label"?: string;
  disabled?: boolean;
  onClick?: () => void;
};

function findButtons(node: ReactNode): ReactElement<ButtonProps>[] {
  if (Array.isArray(node)) return node.flatMap(findButtons);
  if (!isValidElement(node)) return [];
  const element = node as ReactElement<ButtonProps>;
  const descendants = findButtons(element.props.children);
  return element.type === "button" ? [element, ...descendants] : descendants;
}

function textContent(node: ReactNode): string {
  if (Array.isArray(node)) return node.map(textContent).join("");
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (!isValidElement(node)) return "";
  return textContent((node as ReactElement<{ children?: ReactNode }>).props.children);
}

function findButton(tree: ReactNode, label: string): ReactElement<ButtonProps> {
  const button = findButtons(tree).find(
    (candidate) => candidate.props["aria-label"] === label || textContent(candidate.props.children).trim() === label,
  );
  if (!button) throw new Error(`Button not found: ${label}`);
  return button;
}

function click(button: ReactElement<ButtonProps>): void {
  if (button.props.disabled) throw new Error("Cannot click a disabled button");
  button.props.onClick?.();
}

function view(
  state: SavedOpportunitiesListState,
  options: {
    removingId?: number | null;
    actionError?: string | null;
    onRemove?: (competitionId: number) => void;
    onPage?: (offset: number) => void;
  } = {},
) {
  return SavedOpportunitiesListView({
    state,
    removingId: options.removingId ?? null,
    actionError: options.actionError ?? null,
    onRemove: options.onRemove ?? vi.fn(),
    onPage: options.onPage ?? vi.fn(),
  });
}

describe("Saved Opportunities page", () => {
  it("renders saved competitions and clicking Remove invokes the DELETE helper", async () => {
    const remove = vi.spyOn(savedApi, "removeSavedOpportunity").mockResolvedValue({
      competition_id: 31,
      saved: false,
    });
    let request: Promise<void> | undefined;
    const onRemove = vi.fn((competitionId: number) => {
      request = requestSavedOpportunityRemoval("http://api.test", competitionId);
    });
    const tree = view({ type: "results", page: initialPage }, { onRemove });

    expect(renderToStaticMarkup(createElement(SavedOpportunitiesListView, {
      state: { type: "results", page: initialPage },
      removingId: null,
      actionError: null,
      onRemove,
      onPage: vi.fn(),
    }))).toContain("Competition 32");
    click(findButton(tree, "Remove Competition 31 from saved opportunities"));

    expect(onRemove).toHaveBeenCalledWith(31);
    expect(request).toBeDefined();
    await request;
    expect(remove).toHaveBeenCalledWith("http://api.test", 31);
  });

  it("disables all remove and pagination controls while a removal is pending", () => {
    const state: SavedOpportunitiesListState = {
      type: "results",
      page: page([31, 32], { total: 45, limit: 20 }),
    };
    const tree = view(state, { removingId: 31 });

    expect(findButton(tree, "Remove Competition 31 from saved opportunities").props.disabled).toBe(true);
    expect(findButton(tree, "Remove Competition 32 from saved opportunities").props.disabled).toBe(true);
    expect(findButton(tree, "Previous").props.disabled).toBe(true);
    expect(findButton(tree, "Next").props.disabled).toBe(true);
  });

  it("ignores a removal response if the current page changed while it was pending", () => {
    const pageAfterNavigation = page([51, 52], { total: 45, limit: 20, offset: 40 });
    const currentState: SavedOpportunitiesListState = { type: "results", page: pageAfterNavigation };

    const staleCompletion = completeSavedOpportunityRemoval(currentState, 20, 31, 40);

    expect(staleCompletion).toBeNull();
    expect(currentState).toEqual({ type: "results", page: pageAfterNavigation });
  });

  it("moves to a valid previous page when removing the last item on a later page", () => {
    const lastPage: SavedOpportunitiesListState = {
      type: "results",
      page: page([31], { total: 41, limit: 20, offset: 40 }),
    };

    const completion = completeSavedOpportunityRemoval(lastPage, 40, 31, 40);

    expect(completion).not.toBeNull();
    expect(completion?.state).toEqual({ type: "empty", page: page([], { total: 40, limit: 20, offset: 40 }) });
    expect(completion?.nextOffset).toBe(20);
  });

  it("preserves the saved item and shows an error when removal fails", () => {
    const state: SavedOpportunitiesListState = { type: "results", page: initialPage };
    const failure = resolveRemovalFailure(state, new Error("Could not remove this opportunity."));
    const tree = view(failure.state, { actionError: failure.actionError });
    const html = renderToStaticMarkup(createElement(SavedOpportunitiesListView, {
      state: failure.state,
      removingId: null,
      actionError: failure.actionError,
      onRemove: vi.fn(),
      onPage: vi.fn(),
    }));

    expect(failure.state).toBe(state);
    expect(failure.actionError).toBe("Could not remove this opportunity.");
    expect(findButtons(tree)).toHaveLength(2);
    expect(html).toContain("Competition 31");
    expect(html).toContain('role="alert"');
    expect(html).toContain("Could not remove this opportunity.");
  });

  it("renders an empty state", () => {
    const html = renderToStaticMarkup(createElement(SavedOpportunitiesListView, {
      state: { type: "empty", page: page([], { total: 0 }) },
      removingId: null,
      actionError: null,
      onRemove: vi.fn(),
      onPage: vi.fn(),
    }));
    expect(html).toContain("No saved opportunities yet");
    expect(html).toContain('href="/#competitions"');
  });

  it("shows a sign-in link returning to the saved page", () => {
    const html = renderToStaticMarkup(createElement(SavedOpportunitiesListView, {
      state: { type: "signed-out" },
      removingId: null,
      actionError: null,
      onRemove: vi.fn(),
      onPage: vi.fn(),
    }));
    expect(html).toContain("Sign in to view your saved opportunities");
    expect(html).toContain('href="/login?next=%2Fsaved-opportunities"');
  });

  it("renders load failures accessibly", () => {
    const html = renderToStaticMarkup(createElement(SavedOpportunitiesListView, {
      state: { type: "error", message: "Could not reach the API" },
      removingId: null,
      actionError: null,
      onRemove: vi.fn(),
      onPage: vi.fn(),
    }));
    expect(html).toContain('role="alert"');
    expect(html).toContain("Could not reach the API");
  });

  it("navigates correctly at the first, middle, and last pagination boundaries", () => {
    const firstPage = page([1, 2], { total: 5, limit: 2, offset: 0 });
    const middlePage = page([3, 4], { total: 5, limit: 2, offset: 2 });
    const lastPage = page([5], { total: 5, limit: 2, offset: 4 });

    const firstOnPage = vi.fn();
    const first = view({ type: "results", page: firstPage }, { onPage: firstOnPage });
    expect(findButton(first, "Previous").props.disabled).toBe(true);
    click(findButton(first, "Next"));
    expect(firstOnPage).toHaveBeenCalledWith(2);

    const middleOnPage = vi.fn();
    const middle = view({ type: "results", page: middlePage }, { onPage: middleOnPage });
    expect(findButton(middle, "Previous").props.disabled).toBe(false);
    expect(findButton(middle, "Next").props.disabled).toBe(false);
    click(findButton(middle, "Previous"));
    click(findButton(middle, "Next"));
    expect(middleOnPage.mock.calls).toEqual([[0], [4]]);

    const lastOnPage = vi.fn();
    const last = view({ type: "results", page: lastPage }, { onPage: lastOnPage });
    expect(findButton(last, "Next").props.disabled).toBe(true);
    click(findButton(last, "Previous"));
    expect(lastOnPage).toHaveBeenCalledWith(2);
  });

  it("does not save anything while rendering the page", () => {
    const save = vi.spyOn(savedApi, "saveOpportunity");
    const html = renderToStaticMarkup(createElement(SavedOpportunitiesList, {
      apiBaseUrl: "http://api.test",
    }));

    expect(html).toContain("Loading saved opportunities");
    expect(save).not.toHaveBeenCalled();
  });

  it("removes an item from a page and decrements the total only when it exists", () => {
    expect(removeOpportunityFromPage(initialPage, 31)).toEqual({
      ...initialPage,
      items: [item(32)],
      total: 1,
    });
    expect(removeOpportunityFromPage(initialPage, 99)).toEqual(initialPage);
  });

  it("computes page offsets from API metadata", () => {
    const secondPage = page([31, 32], { total: 45, limit: 20, offset: 20 });
    expect(previousSavedOffset(secondPage)).toBe(0);
    expect(nextSavedOffset(secondPage)).toBe(40);
  });

  it("adds a plain text navigation link", () => {
    const html = renderToStaticMarkup(createElement(SiteHeader));
    expect(html).toContain('href="/saved-opportunities">Saved Opportunities</a>');
  });
});
