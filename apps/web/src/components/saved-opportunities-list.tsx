"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  getSavedOpportunities,
  removeSavedOpportunity,
  SavedOpportunitiesApiError,
  type SavedOpportunityPage,
} from "../lib/saved-opportunities";

const PAGE_SIZE = 20;

export type SavedOpportunitiesListState =
  | { type: "loading" }
  | { type: "signed-out" }
  | { type: "error"; message: string }
  | { type: "empty"; page: SavedOpportunityPage }
  | { type: "results"; page: SavedOpportunityPage };

export function removeOpportunityFromPage(
  page: SavedOpportunityPage,
  competitionId: number,
): SavedOpportunityPage {
  const items = page.items.filter((item) => item.competition_id !== competitionId);
  return { ...page, items, total: Math.max(0, page.total - (items.length === page.items.length ? 0 : 1)) };
}

export async function requestSavedOpportunityRemoval(
  apiBaseUrl: string,
  competitionId: number,
): Promise<void> {
  await removeSavedOpportunity(apiBaseUrl, competitionId);
}

export function completeSavedOpportunityRemoval(
  currentState: SavedOpportunitiesListState,
  requestedOffset: number,
  competitionId: number,
  currentOffset: number = requestedOffset,
): { state: SavedOpportunitiesListState; nextOffset: number | null } | null {
  if (
    currentOffset !== requestedOffset ||
    (currentState.type !== "results" && currentState.type !== "empty") ||
    currentState.page.offset !== requestedOffset
  ) {
    return null;
  }

  const page = removeOpportunityFromPage(currentState.page, competitionId);
  const nextOffset =
    currentState.type === "results" &&
    currentState.page.items.length === 1 &&
    requestedOffset > 0
      ? Math.max(0, requestedOffset - currentState.page.limit)
      : null;
  return {
    state: { type: page.items.length === 0 ? "empty" : "results", page },
    nextOffset,
  };
}

export function resolveRemovalFailure(
  currentState: SavedOpportunitiesListState,
  error: unknown,
): { state: SavedOpportunitiesListState; actionError: string | null } {
  if (error instanceof SavedOpportunitiesApiError && error.status === 401) {
    return { state: { type: "signed-out" }, actionError: null };
  }
  return {
    state: currentState,
    actionError:
      error instanceof Error ? error.message : "Could not remove this opportunity.",
  };
}

export function previousSavedOffset(page: SavedOpportunityPage): number {
  return Math.max(0, page.offset - page.limit);
}

export function nextSavedOffset(page: SavedOpportunityPage): number {
  return page.offset + page.limit;
}

export function SavedOpportunitiesListView({
  state,
  removingId,
  actionError,
  onRemove,
  onPage,
}: {
  state: SavedOpportunitiesListState;
  removingId: number | null;
  actionError: string | null;
  onRemove: (competitionId: number) => void;
  onPage: (offset: number) => void;
}) {
  return (
    <main className="page-shell submission-page saved-opportunities-page">
      <p className="eyebrow">Your account</p>
      <h1 className="editorial-heading">Saved Opportunities</h1>
      {state.type === "loading" && (
        <div className="state-panel" role="status">Loading saved opportunities…</div>
      )}
      {state.type === "signed-out" && (
        <div className="state-panel" role="status">
          <h2>Sign in to view your saved opportunities</h2>
          <Link className="button-link submission-inline-link" href="/login?next=%2Fsaved-opportunities">
            Sign in
          </Link>
        </div>
      )}
      {state.type === "error" && (
        <div className="state-panel state-panel--error" role="alert">
          <p>{state.message}</p>
        </div>
      )}
      {(state.type === "empty" || state.type === "results") && (
        <>
          {actionError && <p className="field-error" role="alert">{actionError}</p>}
          {state.type === "empty" ? (
            <div className="state-panel" role="status">
              <h2>No saved opportunities yet</h2>
              <p>Save a competition to keep it here for later.</p>
              <Link className="button-link submission-inline-link" href="/#competitions">
                Browse competitions
              </Link>
            </div>
          ) : (
            <>
              <p className="saved-opportunities-count" aria-live="polite">
                {state.page.total} {state.page.total === 1 ? "saved opportunity" : "saved opportunities"}
              </p>
              <ul className="submission-list saved-opportunities-list">
                {state.page.items.map((item) => (
                  <li className="saved-opportunity-row" key={item.competition_id}>
                    <div>
                      <h2>
                        <Link href={`/competitions/${item.competition_id}`}>{item.title}</Link>
                      </h2>
                      <p>
                        {[item.format, item.event_starts_on ?? item.event_starts_at
                          ? `Starts ${item.event_starts_on ?? item.event_starts_at}`
                          : null,
                        item.registration_deadline_on ?? item.registration_deadline_at
                          ? `Registration closes ${item.registration_deadline_on ?? item.registration_deadline_at}`
                          : null]
                          .filter(Boolean)
                          .join(" · ") || "Competition details"}
                      </p>
                    </div>
                    <button
                      aria-label={`Remove ${item.title} from saved opportunities`}
                      className="text-button saved-opportunity-remove"
                      disabled={removingId !== null}
                      onClick={() => onRemove(item.competition_id)}
                      type="button"
                    >
                      {removingId === item.competition_id ? "Removing…" : "Remove"}
                    </button>
                  </li>
                ))}
              </ul>
              {state.page.total > state.page.limit && (
                <nav className="competition-pagination" aria-label="Saved opportunity pages">
                  <button
                    disabled={removingId !== null || state.page.offset === 0}
                    onClick={() => onPage(previousSavedOffset(state.page))}
                    type="button"
                  >Previous</button>
                  <span>
                    Showing {state.page.offset + 1}–{state.page.offset + state.page.items.length} of {state.page.total}
                  </span>
                  <button
                    disabled={removingId !== null || state.page.offset + state.page.limit >= state.page.total}
                    onClick={() => onPage(nextSavedOffset(state.page))}
                    type="button"
                  >Next</button>
                </nav>
              )}
            </>
          )}
        </>
      )}
    </main>
  );
}

export function SavedOpportunitiesList({ apiBaseUrl }: { apiBaseUrl: string }) {
  const [offset, setOffset] = useState(0);
  const [state, setState] = useState<SavedOpportunitiesListState>({ type: "loading" });
  const [removingId, setRemovingId] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const removeInFlight = useRef(false);
  const offsetRef = useRef(offset);
  const stateRef = useRef(state);

  const updateState = useCallback((nextState: SavedOpportunitiesListState) => {
    stateRef.current = nextState;
    setState(nextState);
  }, []);

  useEffect(() => {
    let active = true;
    getSavedOpportunities(apiBaseUrl, PAGE_SIZE, offset)
      .then((page) => {
        if (active) updateState({ type: page.items.length === 0 ? "empty" : "results", page });
      })
      .catch((error: unknown) => {
        if (!active) return;
        if (error instanceof SavedOpportunitiesApiError && error.status === 401) {
          updateState({ type: "signed-out" });
        } else {
          updateState({
            type: "error",
            message: error instanceof Error ? error.message : "Could not load saved opportunities.",
          });
        }
      });
    return () => { active = false; };
  }, [apiBaseUrl, offset, updateState]);

  const visibleState =
    (state.type === "empty" || state.type === "results") && state.page.offset !== offset
      ? { type: "loading" as const }
      : state;

  function changePage(nextOffset: number) {
    if (removeInFlight.current) return;
    offsetRef.current = nextOffset;
    setOffset(nextOffset);
  }

  async function handleRemove(competitionId: number) {
    if (removeInFlight.current) return;
    const initialState = stateRef.current;
    if (initialState.type !== "results") return;
    const requestedOffset = initialState.page.offset;
    removeInFlight.current = true;
    setRemovingId(competitionId);
    setActionError(null);
    try {
      await requestSavedOpportunityRemoval(apiBaseUrl, competitionId);
      if (offsetRef.current !== requestedOffset) return;
      const completion = completeSavedOpportunityRemoval(
        stateRef.current,
        requestedOffset,
        competitionId,
        offsetRef.current,
      );
      if (!completion) return;
      updateState(completion.state);
      if (completion.nextOffset !== null) {
        offsetRef.current = completion.nextOffset;
        setOffset(completion.nextOffset);
      }
    } catch (error: unknown) {
      if (offsetRef.current !== requestedOffset) return;
      const failure = resolveRemovalFailure(stateRef.current, error);
      updateState(failure.state);
      setActionError(failure.actionError);
    } finally {
      removeInFlight.current = false;
      setRemovingId(null);
    }
  }

  return (
    <SavedOpportunitiesListView
      actionError={actionError}
      onPage={changePage}
      onRemove={(competitionId) => void handleRemove(competitionId)}
      removingId={removingId}
      state={visibleState}
    />
  );
}
