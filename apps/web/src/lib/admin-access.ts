import type { AuthenticatedUser } from "./submissions";

export type AdminAccess = "sign-in" | "denied" | "allowed";

export function resolveAdminAccess(user: AuthenticatedUser | null): AdminAccess {
  if (!user) return "sign-in";
  return user.is_admin === true ? "allowed" : "denied";
}
