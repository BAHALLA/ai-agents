import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "./client";

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
