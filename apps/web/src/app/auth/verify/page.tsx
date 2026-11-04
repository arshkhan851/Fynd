import Link from "next/link";
import { SiteHeader } from "@/components/site-header";
import { MagicLinkVerifier } from "@/components/magic-link-verifier";

export default async function VerifyPage({ searchParams }: { searchParams: Promise<{ token?: string }> }) {
  const params = await searchParams;
  const token = params.token ?? "";
  return <><SiteHeader />{token ? <MagicLinkVerifier apiBaseUrl={process.env.API_BASE_URL ?? ""} token={token} /> : <main className="page-shell auth-page"><div className="state-panel state-panel--error"><h1>Sign-in link unavailable</h1><p>This sign-in link is missing its token.</p><Link className="button-link submission-inline-link" href="/login">Request a new link</Link></div></main>}</>;
}
