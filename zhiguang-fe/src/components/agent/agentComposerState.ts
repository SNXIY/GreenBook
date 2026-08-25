export type ComposerState = "READY" | "SUBMITTING" | "UNAVAILABLE";

export const isComposerDisabled = (
  state: ComposerState,
  conversationAvailable: boolean
): boolean => !conversationAvailable || state !== "READY";

export const canSubmitNaturalLanguage = (
  prompt: string,
  state: ComposerState,
  conversationAvailable: boolean
): boolean => Boolean(prompt.trim()) && conversationAvailable && state === "READY";
