import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";

/** A minimal unsigned JWT for the given claims (display decoding only). */
function makeToken(payload: Record<string, unknown>): string {
  const b64 = (obj: unknown) =>
    btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "HS256", typ: "JWT" })}.${b64(payload)}.sig`;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("App auth flow", () => {
  it("shows the token gate when unauthenticated", () => {
    render(<App />);
    expect(screen.getByRole("heading", { name: /orrery console/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/jwt bearer token/i)).toBeInTheDocument();
  });

  it("enters the console and shows the identity badge after connecting", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.type(
      screen.getByLabelText(/jwt bearer token/i),
      makeToken({ sub: "alice", roles: ["operator"] }),
    );
    await user.click(screen.getByRole("button", { name: /connect/i }));

    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText("operator")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /message/i })).toBeInTheDocument();
  });

  it("sends a message and renders the assistant reply", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ session_id: "s1", response: "Kafka is healthy." }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );
    localStorage.setItem("orrery.console.token", makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /message/i }), "kafka health?");
    await user.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText("Kafka is healthy.")).toBeInTheDocument();
    await waitFor(() => expect(localStorage.getItem("orrery.console.sessionId")).toBe("s1"));
  });

  it("renders the tool timeline and confirmation panel after a turn", async () => {
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    const fetchMock = vi.fn(async (url: RequestInfo | URL, _init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/activity")) {
        return json({
          session_id: "s1",
          entries: [
            {
              operation: "restart_deployment",
              details: "[k8s] name=payment-api → confirmation_required",
              timestamp: "2026-07-19T00:00:00+00:00",
            },
          ],
        });
      }
      if (u.includes("/confirmations/pending")) {
        return json({
          pending: {
            tool_name: "restart_deployment",
            level: "destructive",
            args: { name: "payment-api", namespace: "prod" },
            created_at: 1,
          },
        });
      }
      return json({ session_id: "s1", response: "This action needs your approval." });
    });
    vi.stubGlobal("fetch", fetchMock);
    localStorage.setItem("orrery.console.token", makeToken({ sub: "bob", roles: ["admin"] }));

    const user = userEvent.setup();
    render(<App />);

    await user.type(screen.getByRole("textbox", { name: /message/i }), "restart payment-api");
    await user.click(screen.getByRole("button", { name: /send/i }));

    // Timeline pane, collapsed by default — expand it to see the recorded call.
    const summary = await screen.findByText(/tool calls/i);
    await user.click(summary);
    expect(screen.getByText("restart_deployment", { selector: ".timeline__op" })).toBeVisible();

    // Confirmation panel for the pending guarded action.
    const panel = await screen.findByRole("alertdialog", { name: /pending confirmation/i });
    expect(panel).toHaveTextContent("restart_deployment");
    expect(panel).toHaveTextContent("payment-api");

    // Approve sends the literal decision word through the normal chat flow —
    // the server-side requester-verified gate stays the authority.
    await user.click(screen.getByRole("button", { name: /approve/i }));
    await waitFor(() => {
      const chatCalls = fetchMock.mock.calls.filter(([u]) => String(u).endsWith("/chat"));
      const last = chatCalls[chatCalls.length - 1];
      expect(JSON.parse(String(last[1]?.body))).toMatchObject({ message: "approve" });
    });
  });
});
