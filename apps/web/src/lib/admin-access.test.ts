import { describe, expect, it } from "vitest";

import { resolveAdminAccess } from "./admin-access";

describe("admin access", () => {
  it("requires sign-in when there is no authenticated user", () => {
    expect(resolveAdminAccess(null)).toBe("sign-in");
  });

  it("denies authenticated non-admin users", () => {
    expect(resolveAdminAccess({ id: "user-1", email: "student@example.org", is_admin: false })).toBe("denied");
  });

  it("allows an authenticated administrator", () => {
    expect(resolveAdminAccess({ id: "admin-1", email: "admin@example.org", is_admin: true })).toBe("allowed");
  });
});
