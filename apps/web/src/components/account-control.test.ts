import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AccountControl } from "./account-control";

afterEach(() => vi.restoreAllMocks());

describe("AccountControl", () => {
  it("renders a neutral initial state without flashing a sign-in action", () => {
    const html = renderToStaticMarkup(createElement(AccountControl, { apiBaseUrl: "http://api.test" }));
    expect(html).toContain("Checking account");
    expect(html).not.toContain("Sign in");
  });
});
