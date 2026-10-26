import { afterEach, describe, expect, it, vi } from "vitest";

import { checkHealth } from "./health";

function mockFetch(impl: () => Promise<Response>) {
  vi.spyOn(globalThis, "fetch").mockImplementation(impl);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("checkHealth", () => {
  it("returns ok when the API reports ok", async () => {
    mockFetch(async () => Response.json({ status: "ok" }));
    expect(await checkHealth("http://api.test")).toEqual({ status: "ok" });
  });

  it("returns an error for a non-200 response", async () => {
    mockFetch(async () => new Response("fail", { status: 500 }));
    const result = await checkHealth("http://api.test");
    expect(result.status).toBe("error");
  });

  it("returns an error for an unexpected body", async () => {
    mockFetch(async () => Response.json({ status: "down" }));
    const result = await checkHealth("http://api.test");
    expect(result.status).toBe("error");
  });

  it("returns an error when the API is unreachable", async () => {
    mockFetch(async () => {
      throw new TypeError("fetch failed");
    });
    const result = await checkHealth("http://api.test");
    expect(result.status).toBe("error");
  });

  it("returns an error when the base URL is missing", async () => {
    const result = await checkHealth("");
    expect(result.status).toBe("error");
  });
});
