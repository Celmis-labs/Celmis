import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import { problemsOf } from "./processes";

describe("problemsOf", () => {
  it("names every row the server refused, one per line", () => {
    const err = new ApiError(400, { detail: { problems: ["#2: name is required", "#3: id is not usable"] } }, "bad");
    expect(problemsOf(err)).toBe("#2: name is required\n#3: id is not usable");
  });

  it("falls back to the message when the answer carries no problems", () => {
    expect(problemsOf(new ApiError(500, {}, "boom"))).toBe("boom");
    expect(problemsOf(new Error("offline"))).toBe("offline");
  });
});
