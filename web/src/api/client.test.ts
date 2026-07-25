import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "./client";
import { API_PREFIXES } from "./paths";

function mockFetch(impl: typeof fetch): void {
  vi.stubGlobal("fetch", vi.fn(impl));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ApiClient.chat", () => {
  it("sends the message and returns the parsed response", async () => {
    mockFetch(
      async () =>
        new Response(JSON.stringify({ session_id: "s1", response: "hi" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );

    const client = new ApiClient("tok");
    const res = await client.chat({ message: "hello", session_id: null });

    expect(res).toEqual({ session_id: "s1", response: "hi" });
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.headers.Authorization).toBe("Bearer tok");
    expect(JSON.parse(init.body)).toEqual({ message: "hello", session_id: null });
  });

  it("omits the Authorization header when no token is set", async () => {
    mockFetch(
      async () => new Response(JSON.stringify({ session_id: "s", response: "" }), { status: 200 }),
    );
    await new ApiClient(null).chat({ message: "x" });
    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.headers.Authorization).toBeUndefined();
  });

  it("raises an auth ApiError on 401", async () => {
    mockFetch(
      async () =>
        new Response(JSON.stringify({ detail: "Invalid token" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }),
    );

    const client = new ApiClient("bad");
    const err = await client.chat({ message: "hi" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
    expect((err as ApiError).isAuth).toBe(true);
    expect((err as ApiError).message).toBe("Invalid token");
  });

  it("wraps network failures as a status-0 ApiError", async () => {
    mockFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    const err = await new ApiClient("t").chat({ message: "hi" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(0);
  });

  it("propagates AbortError without wrapping", async () => {
    mockFetch(async () => {
      throw new DOMException("aborted", "AbortError");
    });
    const err = await new ApiClient("t").chat({ message: "hi" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(DOMException);
    expect((err as DOMException).name).toBe("AbortError");
  });
});

describe("ApiClient.activity", () => {
  it("GETs the session timeline with the bearer token and no body", async () => {
    const payload = {
      session_id: "s1",
      entries: [{ operation: "check_cluster_health", details: "[kafka] → ok", timestamp: "t" }],
    };
    mockFetch(
      async () =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );

    const res = await new ApiClient("tok").activity("s1");

    expect(res).toEqual(payload);
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/session/s1/activity");
    expect(init.method).toBe("GET");
    expect(init.body).toBeNull();
    expect(init.headers.Authorization).toBe("Bearer tok");
    expect(init.headers["Content-Type"]).toBeUndefined();
  });

  it("URL-encodes the session id", async () => {
    mockFetch(
      async () => new Response(JSON.stringify({ session_id: "x", entries: [] }), { status: 200 }),
    );
    await new ApiClient("t").activity("a/b c");
    const [url] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/session/a%2Fb%20c/activity");
  });
});

describe("ApiClient.pendingConfirmation", () => {
  it("returns the caller's pending action", async () => {
    const payload = {
      pending: {
        tool_name: "restart_deployment",
        level: "destructive",
        args: { name: "payment-api" },
        created_at: 1,
      },
    };
    mockFetch(
      async () =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );

    const res = await new ApiClient("tok").pendingConfirmation();
    expect(res).toEqual(payload);
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/confirmations/pending");
    expect(init.method).toBe("GET");
  });

  it("returns null when nothing is pending", async () => {
    mockFetch(async () => new Response(JSON.stringify({ pending: null }), { status: 200 }));
    const res = await new ApiClient("tok").pendingConfirmation();
    expect(res.pending).toBeNull();
  });
});

describe("ApiClient.triage", () => {
  it("GETs the session triage verdict", async () => {
    const payload = { session_id: "s1", severity: "critical", report: "## Down" };
    mockFetch(
      async () =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    const res = await new ApiClient("tok").triage("s1");
    expect(res).toEqual(payload);
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/session/s1/triage");
    expect(init.method).toBe("GET");
  });
});

describe("API path coverage", () => {
  // Regression guard for the dev-proxy drift that made the System pane's
  // checks 404 on :5173 while working fine on :8000. Every path the client can
  // request must be covered by API_PREFIXES, which is what vite.config.ts
  // proxies — so a new endpoint cannot be added without also proxying it.
  it("every ApiClient request starts with a proxied prefix", async () => {
    mockFetch(
      async () =>
        new Response(JSON.stringify({}), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );

    const client = new ApiClient("tok");
    await Promise.all([
      client.chat({ message: "x" }),
      client.activity("s1"),
      client.pendingConfirmation(),
      client.triage("s1"),
      client.me(),
      client.selfTest(),
    ]);

    const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls;
    // Guards against the assertion below passing vacuously if a method stops
    // calling fetch, and against methods being added without a call here.
    const methodCount = Object.getOwnPropertyNames(ApiClient.prototype).filter(
      (n) => n !== "constructor" && n !== "setToken" && n !== "request",
    ).length;
    expect(calls).toHaveLength(methodCount);

    for (const [url] of calls) {
      const path = new URL(String(url), "http://localhost").pathname;
      expect(
        API_PREFIXES.some((prefix) => path === prefix || path.startsWith(`${prefix}/`)),
        `${path} is not covered by API_PREFIXES — add it there so the Vite dev proxy forwards it`,
      ).toBe(true);
    }
  });
});

describe("non-JSON responses", () => {
  // The failure that made the System pane render "—" with no explanation: the
  // Vite dev server answered an unproxied GET with the SPA shell, so the
  // response was a 200 full of HTML and res.json() died with a bare
  // SyntaxError. A 200 is not success if it didn't come from the API.
  it("rejects a 200 that returns HTML instead of JSON", async () => {
    mockFetch(
      async () =>
        new Response("<!doctype html><html></html>", {
          status: 200,
          headers: { "Content-Type": "text/html" },
        }),
    );

    const err = await new ApiClient("tok").me().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toContain("text/html");
    expect((err as ApiError).message).toContain("didn't reach the API");
  });

  it("reports malformed JSON rather than leaking a SyntaxError", async () => {
    mockFetch(
      async () =>
        new Response("{not json", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );

    const err = await new ApiClient("tok").me().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toContain("Malformed JSON");
  });

  it("still accepts a valid JSON body sent without a JSON content type", async () => {
    mockFetch(async () => new Response(JSON.stringify({ role: "admin" }), { status: 200 }));
    await expect(new ApiClient("tok").me()).resolves.toEqual({ role: "admin" });
  });
});
