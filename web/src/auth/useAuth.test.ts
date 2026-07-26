import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { legacyStorageKeys, storageKeys } from "../config";
import { useAuth } from "./useAuth";

describe("useAuth signOut", () => {
  beforeEach(() => localStorage.clear());

  it("clears the token and every key an earlier build wrote", () => {
    const { result } = renderHook(() => useAuth());
    localStorage.setItem(storageKeys.token, "t");
    legacyStorageKeys.forEach((key) => localStorage.setItem(key, "stale"));

    act(() => result.current.signOut());

    expect(localStorage.getItem(storageKeys.token)).toBeNull();
    // Transcripts live server-side now, but a browser upgraded from a build
    // that stored them must not leave the prior user's history behind.
    legacyStorageKeys.forEach((key) => expect(localStorage.getItem(key)).toBeNull());
  });
});
