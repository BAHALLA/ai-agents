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
