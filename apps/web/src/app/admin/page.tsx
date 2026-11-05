import type { Metadata } from "next";

import { AdminDashboard } from "@/components/admin-dashboard";
import { SiteHeader } from "@/components/site-header";
import { APP_NAME } from "@/lib/config";

export const metadata: Metadata = { title: `Admin review queue — ${APP_NAME}` };

export default function AdminPage() {
  return <><SiteHeader /><AdminDashboard apiBaseUrl={process.env.API_BASE_URL ?? ""} /></>;
}
