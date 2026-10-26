import { describe, expect, it } from "vitest";

import { canRetryNow, isDeferred, reasonSentence } from "./deferred-review";

const waiting = { job_id: "j1", reason: "the model provider is unavailable", since: null, next_check_at: null };

describe("a review that waits for its model", () => {
  it("is deferred only when the server says so", () => {
    expect(isDeferred("deferred")).toBe(true);
    expect(isDeferred("queued")).toBe(false);
    expect(isDeferred(undefined)).toBe(false);
  });

  it("offers retry-now to a workspace manager when a job is waiting", () => {
    expect(canRetryNow(waiting, true)).toBe(true);
  });

  it("offers nothing to a member, while the role loads, or with no job", () => {
    expect(canRetryNow(waiting, false)).toBe(false);
    expect(canRetryNow(waiting, undefined)).toBe(false);
    expect(canRetryNow({ ...waiting, job_id: "" }, true)).toBe(false);
    expect(canRetryNow(null, true)).toBe(false);
  });

  it("writes the curated reason as one sentence", () => {
    expect(reasonSentence("the model provider is unavailable")).toBe("The model provider is unavailable.");
    expect(reasonSentence("already ends with a stop.")).toBe("Already ends with a stop.");
    expect(reasonSentence(null)).toBe("");
  });
});
