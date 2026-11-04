"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { verifyMagicLinkOnce } from "@/lib/submissions";

export function MagicLinkVerifier({ apiBaseUrl, token }: { apiBaseUrl: string; token: string }) {
  const router = useRouter();
  const [message, setMessage] = useState("Verifying your sign-in link…");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    verifyMagicLinkOnce(apiBaseUrl, token).then(() => {
      if (!active) return;
      const returnTo = sessionStorage.getItem("fynd_return_to");
      sessionStorage.removeItem("fynd_return_to");
      const safe = returnTo?.startsWith("/") && !returnTo.startsWith("//") ? returnTo : "/";
      setMessage("You’re signed in. Taking you back…");
      router.replace(safe);
    }).catch((error: unknown) => {
      if (active) { setMessage(error instanceof Error ? error.message : "Could not verify this link."); setFailed(true); }
    });
    return () => { active = false; };
  }, [apiBaseUrl, router, token]);
  return <main className="page-shell auth-page"><div className={`state-panel${failed ? " state-panel--error" : ""}`} role={failed ? "alert" : "status"}><h1>{failed ? "Sign-in link unavailable" : "Signing you in"}</h1><p>{message}</p>{failed && <Link className="button-link submission-inline-link" href="/login">Request a new link</Link>}</div></main>;
}
