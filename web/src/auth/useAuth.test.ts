import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { storageKeys } from "../config";
import { useAuth } from "./useAuth";

describe("useAuth signOut", () => {
  beforeEach(() => localStorage.clear());

  it("clears the token, conversation history, and legacy keys", () => {
    const { result } = renderHook(() => useAuth());
    localStorage.setItem(storageKeys.token, "t");
    localStorage.setItem(storageKeys.conversations, "[]");
    localStorage.setItem(storageKeys.activeConversation, "c1");
    localStorage.setItem("orrery.console.sessionId", "legacy");

    act(() => result.current.signOut());

    expect(localStorage.getItem(storageKeys.token)).toBeNull();
    expect(localStorage.getItem(storageKeys.conversations)).toBeNull();
    expect(localStorage.getItem(storageKeys.activeConversation)).toBeNull();
    // Sign-out must not leave the prior user's history for the next sign-in.
    expect(localStorage.getItem("orrery.console.sessionId")).toBeNull();
  });
});
