import assert from "node:assert/strict";
import {
  clearSelectedConversationId,
  conversationStorageKey,
  hasCustomConversationTitle,
  isConversationSelectionCurrent,
  readSelectedConversationId,
  titleFromFirstMessage,
  tokenTenantId,
  writeSelectedConversationId
} from "../src/components/agent/conversationLifecycle";

const values = new Map<string, string>();
Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: {
    atob: (value: string) => Buffer.from(value, "base64").toString("binary"),
    localStorage: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key)
    }
  }
});

const keyA = conversationStorageKey({
  userId: "user-a",
  tenantId: "tenant-a",
  surface: "HOME",
  contextPostId: null
});
const keyB = conversationStorageKey({
  userId: "user-b",
  tenantId: "tenant-a",
  surface: "HOME",
  contextPostId: null
});

// A: selected Conversation persistence is scoped by user/tenant/surface.
assert.notEqual(keyA, keyB);
writeSelectedConversationId(keyA, "conversation-a");
assert.equal(readSelectedConversationId(keyA), "conversation-a");
assert.equal(readSelectedConversationId(keyB), null);

// B: reload can read the same durable selection; clearing removes an invalid ID.
clearSelectedConversationId(keyA);
assert.equal(readSelectedConversationId(keyA), null);

// C: first-message titles are deterministic, single-line, and bounded.
assert.equal(titleFromFirstMessage("  A\n\nB  "), "A B");
assert.equal(titleFromFirstMessage("x".repeat(100)).length, 64);

// D: custom titles are preserved by the title projection.
assert.equal(hasCustomConversationTitle({ conversation_id: "a", title: null, updated_at: "" }), false);
assert.equal(hasCustomConversationTitle({ conversation_id: "a", title: "My title", updated_at: "" }), true);

// E: tenant claims partition local state without introducing server session truth.
const header = Buffer.from(JSON.stringify({ tenant_id: "tenant-a" })).toString("base64url");
assert.equal(tokenTenantId(`x.${header}.y`), "tenant-a");

// F/G: only the active generation may project a response; old A/B responses
// cannot overwrite the active C conversation.
assert.equal(isConversationSelectionCurrent("conversation-c", 3, "conversation-c", 3), true);
assert.equal(isConversationSelectionCurrent("conversation-c", 3, "conversation-a", 1), false);
assert.equal(isConversationSelectionCurrent("conversation-c", 3, "conversation-b", 2), false);

// H/I: simulate A→B→C responses arriving in reverse order.
const responses = [
  { id: "conversation-c", generation: 3 },
  { id: "conversation-b", generation: 2 },
  { id: "conversation-a", generation: 1 }
];
const applied = responses.filter(response =>
  isConversationSelectionCurrent("conversation-c", 3, response.id, response.generation)
);
assert.deepEqual(applied, [{ id: "conversation-c", generation: 3 }]);

console.log("conversation lifecycle tests: A-I PASS");
