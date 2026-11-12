"use client";

import Link from "next/link";
import { useRef, useState } from "react";

import {
  saveOpportunity,
  SavedOpportunitiesApiError,
} from "../lib/saved-opportunities";

export type SaveOpportunityButtonState =
  | { type: "ready" }
  | { type: "saving" }
  | { type: "saved" }
  | { type: "sign-in" }
  | { type: "error"; message: string };

export async function performSaveOpportunity(
  apiBaseUrl: string,
  competitionId: number,
): Promise<Exclude<SaveOpportunityButtonState, { type: "ready" | "saving" }>> {
  try {
    await saveOpportunity(apiBaseUrl, competitionId);
    return { type: "saved" };
  } catch (error: unknown) {
    if (error instanceof SavedOpportunitiesApiError && error.status === 401) {
      return { type: "sign-in" };
    }
    return {
      type: "error",
      message:
        error instanceof Error
          ? error.message
          : "Could not save this opportunity. Please try again.",
    };
  }
}

export function SaveOpportunityButtonView({
  competitionId,
  state,
  onSave,
}: {
  competitionId: number;
  state: SaveOpportunityButtonState;
  onSave: () => void;
}) {
  if (state.type === "sign-in") {
    const returnTo = `/competitions/${competitionId}`;
    return (
      <div className="save-opportunity-control">
        <Link
          className="editorial-link"
          href={`/login?next=${encodeURIComponent(returnTo)}`}
        >
          Sign in to save
        </Link>
        <p className="save-opportunity-note">
          After signing in, return here and choose Save again.
        </p>
      </div>
    );
  }

  return (
    <div className="save-opportunity-control">
      <button
        aria-busy={state.type === "saving"}
        className="filter-submit save-opportunity-button"
        disabled={state.type === "saving" || state.type === "saved"}
        onClick={onSave}
        type="button"
      >
        {state.type === "saving"
          ? "Saving…"
          : state.type === "saved"
            ? "Saved"
            : "Save opportunity"}
      </button>
      {state.type === "saved" && (
        <p className="save-opportunity-note" role="status">
          Added to your saved opportunities.
        </p>
      )}
      {state.type === "error" && (
        <p className="save-opportunity-note field-error" role="alert">
          {state.message}
        </p>
      )}
    </div>
  );
}

export function SaveOpportunityButton({
  competitionId,
  apiBaseUrl,
}: {
  competitionId: number;
  apiBaseUrl: string;
}) {
  const [state, setState] = useState<SaveOpportunityButtonState>({ type: "ready" });
  const requestInFlight = useRef(false);

  async function handleSave() {
    if (requestInFlight.current || state.type === "saved") return;
    requestInFlight.current = true;
    setState({ type: "saving" });
    try {
      setState(await performSaveOpportunity(apiBaseUrl, competitionId));
    } finally {
      requestInFlight.current = false;
    }
  }

  return (
    <SaveOpportunityButtonView
      competitionId={competitionId}
      state={state}
      onSave={() => void handleSave()}
    />
  );
}
