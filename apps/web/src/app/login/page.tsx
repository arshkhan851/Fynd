import type { Metadata } from "next";

import { LoginForm } from "@/components/login-form";
import { SiteHeader } from "@/components/site-header";
import { APP_NAME } from "@/lib/config";

export const metadata: Metadata = { title: `Sign in — ${APP_NAME}` };

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string }> }) {
  const params = await searchParams;
  return <><SiteHeader /><LoginForm apiBaseUrl={process.env.API_BASE_URL ?? ""} next={params.next} /></>;
}
