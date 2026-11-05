"use client";

import { useEffect, useState } from "react";

import { getAdminSubmissions } from "@/lib/submissions";
import { getCurrentUser } from "@/lib/submissions";
import { resolveAdminAccess } from "@/lib/admin-access";
import { AdminApiError } from "@/lib/submissions";
import { AdminDashboardState, AdminDashboardView } from "@/components/admin-dashboard-view";

export function AdminDashboard({ apiBaseUrl }: { apiBaseUrl: string }) {
  const [offset, setOffset] = useState(0);
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState<AdminDashboardState>({ type: "checking" });

  useEffect(() => {
    let active = true;
    getCurrentUser(apiBaseUrl).then((user) => {
      if (!active) return;
      const access = resolveAdminAccess(user);
      if (access !== "allowed") {
        setState({ type: access });
        return;
      }
      setState({ type: "loading" });
      return getAdminSubmissions(apiBaseUrl, 20, offset).then((page) => {
        if (active) setState({ type: page.items.length === 0 ? "empty" : "results", page });
      });
    }).catch((error: unknown) => {
      if (!active) return;
      if (error instanceof AdminApiError && error.status === 401) {
        setState({ type: "sign-in" });
      } else if (error instanceof AdminApiError && error.status === 403) {
        setState({ type: "denied" });
      } else {
        setState({ type: "error", message: error instanceof Error ? error.message : "Please try again." });
      }
    });
    return () => { active = false; };
  }, [apiBaseUrl, offset, retry]);

  const visibleState =
    (state.type === "empty" || state.type === "results") && state.page.offset !== offset
      ? { type: "loading" as const }
      : state;
  return <AdminDashboardView state={visibleState} onPage={setOffset} onRetry={() => setRetry((value) => value + 1)} />;
}
