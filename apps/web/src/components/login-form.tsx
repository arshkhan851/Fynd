"use client";

import Link from "next/link";
import { type FormEvent, useState } from "react";

import { requestMagicLink } from "@/lib/submissions";

export function LoginForm({ apiBaseUrl, next }: { apiBaseUrl: string; next: string | undefined }) {
  const returnTo = next?.startsWith("/") && !next.startsWith("//") ? next : "/";
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(""); setLoading(true);
    try {
      await requestMagicLink(apiBaseUrl, email.trim());
      sessionStorage.setItem("fynd_return_to", returnTo);
      setSent(true);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not request a sign-in link."); }
    finally { setLoading(false); }
  }
  return <main className="page-shell auth-page"><p className="eyebrow">Your Fynd account</p><h1 className="editorial-heading">Sign in with a secure link.</h1>
    {sent ? <div className="state-panel" role="status"><h2>Check your email</h2><p>If the address is registered, a sign-in link has been sent. Open it on this device to continue.</p><button className="text-button" onClick={() => setSent(false)}>Use a different email</button></div> : <form className="auth-form" onSubmit={submit}><label className="submission-field"><span>Email address</span><input type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} /></label>{error && <p className="field-error" role="alert">{error}</p>}<button className="filter-submit" disabled={loading}>{loading ? "Sending…" : "Email me a sign-in link"}</button></form>}
    <Link className="back-link" href="/">Back to competitions</Link></main>;
}
