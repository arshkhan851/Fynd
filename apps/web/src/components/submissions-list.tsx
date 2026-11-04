"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { getMySubmissions, SubmissionListItem } from "@/lib/submissions";

export function SubmissionsList({ apiBaseUrl, created }: { apiBaseUrl: string; created?: string }) {
  const [items, setItems] = useState<SubmissionListItem[]>([]);
  const [state, setState] = useState<"loading" | "ok" | "error">("loading");
  const [error, setError] = useState("");
  const [createdConfirmed, setCreatedConfirmed] = useState(false);
  useEffect(() => {
    let active = true;
    getMySubmissions(apiBaseUrl).then((result) => { if (active) { setItems(result.items); setCreatedConfirmed(Boolean(created && result.items.some((item) => String(item.id) === created && item.status === "pending"))); setState("ok"); } }).catch((caught: unknown) => { if (active) { setError(caught instanceof Error ? caught.message : "Could not load submissions."); setState("error"); } });
    return () => { active = false; };
  }, [apiBaseUrl, created]);
  return <main className="page-shell submission-page"><p className="eyebrow">Your account</p><h1 className="editorial-heading">My submissions</h1>
    {createdConfirmed && <div className="state-panel submission-success" role="status"><h2>Submission received</h2><p>Your event is pending review. It will only appear publicly after Fynd approves it.</p></div>}
    {state === "loading" ? <div className="state-panel" role="status">Loading your submissions…</div> : state === "error" ? <div className="state-panel state-panel--error" role="alert"><p>{error}</p><Link href="/login?next=%2Fsubmissions">Sign in</Link></div> : items.length === 0 ? <div className="state-panel"><h2>No submissions yet</h2><p>Share a hackathon with the Fynd community.</p><Link className="button-link submission-inline-link" href="/submit">Submit an event</Link></div> : <ul className="submission-list">{items.map((item) => <li key={item.id}><div><h2>{item.title}</h2><p>Submitted {new Date(item.created_at).toLocaleDateString()}</p></div><span className={`status-pill status-pill--${item.status}`}>{item.status.replaceAll("_", " ")}</span></li>)}</ul>}
  </main>;
}
