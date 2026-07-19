import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { storageKeys } from "../config";
import { NEW_CONVERSATION_TITLE, type Conversation } from "./types";
import { useConversations } from "./useConversations";

function makeConversation(id: string, withMessage = true): Conversation {
  return {
    id,
    sessionId: null,
    title: withMessage ? id : NEW_CONVERSATION_TITLE,
    messages: withMessage ? [{ id: "m", role: "user", text: "hi", at: 1 }] : [],
    updatedAt: 1,
  };
}

describe("useConversations", () => {
  beforeEach(() => localStorage.clear());

  it("starts with one empty conversation when storage is empty", () => {
    const { result } = renderHook(() => useConversations());
    expect(result.current.conversations).toHaveLength(1);
    expect(result.current.active.title).toBe(NEW_CONVERSATION_TITLE);
  });

  it("newConversation reuses an existing blank instead of piling up", () => {
    const { result } = renderHook(() => useConversations());
    const firstId = result.current.activeId;
    act(() => result.current.newConversation());
    expect(result.current.conversations).toHaveLength(1);
    expect(result.current.activeId).toBe(firstId);
  });

  it("newConversation adds a fresh one once the current has messages", () => {
    const { result } = renderHook(() => useConversations());
    act(() => result.current.patchActive({ messages: makeConversation("x").messages }));
    act(() => result.current.newConversation());
    expect(result.current.conversations).toHaveLength(2);
    expect(result.current.active.messages).toHaveLength(0);
  });

  it("deleting the active conversation reassigns activeId", () => {
    localStorage.setItem(
      storageKeys.conversations,
      JSON.stringify([makeConversation("a"), makeConversation("b")]),
    );
    localStorage.setItem(storageKeys.activeConversation, "a");
    const { result } = renderHook(() => useConversations());
    act(() => result.current.deleteConversation("a"));
    expect(result.current.conversations.map((c) => c.id)).toEqual(["b"]);
    expect(result.current.activeId).toBe("b");
  });

  it("deleting the last conversation creates a fresh empty one", () => {
    localStorage.setItem(storageKeys.conversations, JSON.stringify([makeConversation("only")]));
    const { result } = renderHook(() => useConversations());
    act(() => result.current.deleteConversation("only"));
    expect(result.current.conversations).toHaveLength(1);
    expect(result.current.active.messages).toHaveLength(0);
  });

  it("caps a large stored history at load", () => {
    const many = Array.from({ length: 70 }, (_, i) => makeConversation(`c${i}`));
    localStorage.setItem(storageKeys.conversations, JSON.stringify(many));
    const { result } = renderHook(() => useConversations());
    expect(result.current.conversations.length).toBeLessThanOrEqual(50);
  });

  it("drops malformed stored entries instead of crashing", () => {
    localStorage.setItem(
      storageKeys.conversations,
      JSON.stringify([makeConversation("ok"), { id: "bad", messages: "not-an-array" }]),
    );
    const { result } = renderHook(() => useConversations());
    expect(result.current.conversations.map((c) => c.id)).toEqual(["ok"]);
  });
});
