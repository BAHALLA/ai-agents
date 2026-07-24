import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../chat/types";
import { storageKeys } from "../config";
import { NEW_CONVERSATION_TITLE, type Conversation } from "./types";
import { useConversations } from "./useConversations";

function msg(id: string): ChatMessage {
  return { id, role: "assistant", text: "reply", at: 1 };
}

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

  it("keeps a reply that arrives in the same tick as a New-chat click", () => {
    // The regression: newConversation wrote a whole new array built from the
    // render closure, so an update queued earlier in the same tick — the
    // assistant's reply landing via patchActive — was silently overwritten.
    const { result } = renderHook(() => useConversations());
    act(() => result.current.patchActive({ messages: [msg("user-1")] }));
    const originalId = result.current.activeId;

    act(() => {
      result.current.patchActive((c) => ({ messages: [...c.messages, msg("assistant-reply")] }));
      result.current.newConversation();
    });

    const original = result.current.conversations.find((c) => c.id === originalId);
    expect(original?.messages.map((m) => m.id)).toEqual(["user-1", "assistant-reply"]);
    expect(result.current.conversations).toHaveLength(2);
  });

  it("keeps a reply that arrives in the same tick as a delete", () => {
    const { result } = renderHook(() => useConversations());
    act(() => result.current.patchActive({ messages: [msg("user-1")] }));
    act(() => result.current.newConversation());
    const secondId = result.current.activeId;

    act(() => {
      result.current.patchActive((c) => ({ messages: [...c.messages, msg("late-reply")] }));
      result.current.deleteConversation("does-not-exist");
    });

    const second = result.current.conversations.find((c) => c.id === secondId);
    expect(second?.messages.map((m) => m.id)).toEqual(["late-reply"]);
  });

  it("evicts the least-recently-updated conversation, not the oldest-inserted", () => {
    const many = Array.from({ length: 50 }, (_, i) => ({
      ...makeConversation(`c${i}`),
      // c0 is the oldest insertion but the most recently used.
      updatedAt: i === 0 ? 10_000 : i,
    }));
    localStorage.setItem(storageKeys.conversations, JSON.stringify(many));
    const { result } = renderHook(() => useConversations());

    act(() => result.current.patchActive({ messages: [msg("m")] }));
    act(() => result.current.newConversation());

    const ids = result.current.conversations.map((c) => c.id);
    expect(ids).toHaveLength(50);
    expect(ids).toContain("c0"); // recently used — survives
    expect(ids).not.toContain("c1"); // least recently updated — evicted
  });

  it("trims a very long transcript when persisting but not in memory", () => {
    const { result } = renderHook(() => useConversations());
    const long = Array.from({ length: 250 }, (_, i) => msg(`m${i}`));
    act(() => result.current.patchActive({ messages: long }));

    expect(result.current.active.messages).toHaveLength(250);
    const stored = JSON.parse(localStorage.getItem(storageKeys.conversations)!) as Conversation[];
    expect(stored[0].messages).toHaveLength(200);
    expect(stored[0].messages.at(-1)?.id).toBe("m249"); // the most recent survive
  });

  it("sheds older conversations rather than losing the write when storage is full", () => {
    const { result } = renderHook(() => useConversations());
    act(() => result.current.patchActive({ messages: [msg("m")] }));
    act(() => result.current.newConversation());

    const original = Storage.prototype.setItem;
    let calls = 0;
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (
      this: Storage,
      key: string,
      value: string,
    ) {
      // Reject the first (full) write the way a quota-exceeded browser would.
      calls += 1;
      if (calls === 1) throw new DOMException("QuotaExceededError");
      original.call(this, key, value);
    });

    act(() => result.current.patchActive({ messages: [msg("m2")] }));

    expect(calls).toBeGreaterThan(1); // it retried instead of giving up
    setItem.mockRestore();
    const stored = localStorage.getItem(storageKeys.conversations);
    expect(stored).not.toBeNull();
    expect(JSON.parse(stored!)).toHaveLength(2);
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
