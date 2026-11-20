"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { AUTH_LOGOUT_EVENT, getCurrentUser, logoutCurrentUser, type AuthenticatedUser } from "../lib/submissions";

type AccountState =
  | { type: "loading" }
  | { type: "signed-out" }
  | { type: "unavailable"; message: string }
  | { type: "signed-in"; user: AuthenticatedUser; logoutError?: string; loggingOut?: boolean };

export function AccountControl({ apiBaseUrl }: { apiBaseUrl: string }) {
  const [state, setState] = useState<AccountState>({ type: "loading" });
  const [retry, setRetry] = useState(0);
  const requestId = useRef(0);
  const logoutInFlight = useRef(false);

  useEffect(() => {
    const id = ++requestId.current;
    let active = true;
    getCurrentUser(apiBaseUrl).then(
      (user) => {
        if (active && requestId.current === id) {
          setState(user ? { type: "signed-in", user } : { type: "signed-out" });
        }
      },
      () => {
        if (active && requestId.current === id) {
          setState({ type: "unavailable", message: "Account status is unavailable." });
        }
      },
    );
    return () => {
      active = false;
    };
  }, [apiBaseUrl, retry]);

  async function logout() {
    if (state.type !== "signed-in" || logoutInFlight.current) return;
    logoutInFlight.current = true;
    const user = state.user;
    setState({ type: "signed-in", user, loggingOut: true });
    try {
      await logoutCurrentUser(apiBaseUrl);
      setState({ type: "signed-out" });
      window.dispatchEvent(new Event(AUTH_LOGOUT_EVENT));
    } catch {
      setState({
        type: "signed-in",
        user,
        logoutError: "Could not log out. Please try again.",
      });
    } finally {
      logoutInFlight.current = false;
    }
  }

  if (state.type === "loading") {
    return <span className="account-control__placeholder" aria-live="polite">Checking account…</span>;
  }
  if (state.type === "signed-out") {
    return <Link className="account-control__link" href="/login">Sign in</Link>;
  }
  if (state.type === "unavailable") {
    return <span className="account-control__unavailable" role="status">
      {state.message} <button className="text-button" onClick={() => {
        setState({ type: "loading" });
        setRetry((value) => value + 1);
      }}>Retry</button>
    </span>;
  }

  return (
    <details className="account-control">
      <summary aria-label="Account menu">My Account</summary>
      <div className="account-control__menu">
        <p className="account-control__email">{state.user.email}</p>
        <Link href="/saved-opportunities">Saved Opportunities</Link>
        <Link href="/submissions">My Submissions</Link>
        <button type="button" onClick={logout} disabled={state.loggingOut}>
          {state.loggingOut ? "Logging out…" : "Log out"}
        </button>
        {state.logoutError && <p className="account-control__error" role="alert">{state.logoutError}</p>}
      </div>
    </details>
  );
}
