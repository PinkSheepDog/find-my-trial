import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, MATCH_TIMEOUT_MS } from "../src/api.js";

function jsonResponse(body, { ok = true, status = 200 } = {}) {
  return { ok, status, text: async () => JSON.stringify(body) };
}

function textResponse(body, { ok = false, status = 502 } = {}) {
  return { ok, status, text: async () => body };
}

// fetch that never settles until the request signal aborts.
function hangingFetch() {
  return vi.fn((_path, init) =>
    new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => {
        const err = new Error("The operation was aborted.");
        err.name = "AbortError";
        reject(err);
      });
    })
  );
}

beforeEach(() => {
  document.cookie = "fmt_csrf=csrf-token-value";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("request timeouts", () => {
  it("aborts a hung request and reports a timeout", async () => {
    vi.useFakeTimers();
    global.fetch = hangingFetch();

    const promise = api.health();
    const assertion = expect(promise).rejects.toThrow(/timed out after 30s/i);
    await vi.advanceTimersByTimeAsync(30_000);
    await assertion;
  });

  it("gives matching a longer budget than session calls", async () => {
    vi.useFakeTimers();
    global.fetch = hangingFetch();

    const promise = api.match({ deidentified_text: "x" });
    const assertion = expect(promise).rejects.toThrow(/timed out after 120s/i);
    await vi.advanceTimersByTimeAsync(MATCH_TIMEOUT_MS);
    await assertion;
  });

  it("clears the timer once a response arrives", async () => {
    vi.useFakeTimers();
    global.fetch = vi.fn(async () => jsonResponse({ ok: true }));
    const clearSpy = vi.spyOn(global, "clearTimeout");

    await api.health();
    expect(clearSpy).toHaveBeenCalled();
  });
});

describe("cancellation", () => {
  it("surfaces a caller-initiated cancel as an AbortError, not a failure", async () => {
    global.fetch = hangingFetch();
    const controller = new AbortController();

    const promise = api.health({ signal: controller.signal });
    controller.abort();

    await expect(promise).rejects.toMatchObject({ name: "AbortError", isAbort: true });
  });

  it("does not fire fetch at all when the signal is already aborted", async () => {
    global.fetch = hangingFetch();
    const controller = new AbortController();
    controller.abort();

    await expect(api.health({ signal: controller.signal })).rejects.toMatchObject({ isAbort: true });
  });
});

describe("error handling", () => {
  it("prefers the server-issued error_id", async () => {
    global.fetch = vi.fn(async () => jsonResponse({ error: "Not authenticated", error_id: "a1b2c3" }, { ok: false, status: 401 }));

    await expect(api.me()).rejects.toMatchObject({ status: 401, errorId: "a1b2c3", message: "Not authenticated" });
  });

  it("mints a client-side ID when the server sent none", async () => {
    global.fetch = vi.fn(async () => jsonResponse({ error: "Boom" }, { ok: false, status: 500 }));

    await expect(api.me()).rejects.toMatchObject({ errorId: expect.stringMatching(/^FMT-[0-9A-F]{6}$/) });
  });

  it("never surfaces an unmodelled response body", async () => {
    // A proxy's HTML error page could echo anything; only the modelled `error`
    // field is known not to carry request content.
    global.fetch = vi.fn(async () =>
      textResponse("<html><body>62yo F with metastatic breast cancer</body></html>")
    );

    await expect(api.me()).rejects.toThrow(/Request failed \(502\)/);
    await expect(api.me()).rejects.not.toThrow(/breast cancer/);
  });

  it("reports an unreachable server without echoing the request", async () => {
    global.fetch = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });

    await expect(api.deidentify("secret chart text")).rejects.toThrow(/Could not reach the server/);
    await expect(api.deidentify("secret chart text")).rejects.not.toThrow(/secret chart text/);
  });

  it("collapses multi-line messages to a single capped line", async () => {
    const long = `line one\nline two\n${"x".repeat(500)}`;
    global.fetch = vi.fn(async () => jsonResponse({ error: long }, { ok: false, status: 400 }));

    const err = await api.me().catch((e) => e);
    expect(err.message).not.toContain("\n");
    expect(err.message.length).toBeLessThanOrEqual(301);
  });
});

describe("headers", () => {
  it("sends the CSRF token on state-changing requests only", async () => {
    global.fetch = vi.fn(async () => jsonResponse({}));

    await api.health();
    expect(global.fetch.mock.calls[0][1].headers["x-csrf-token"]).toBeUndefined();

    await api.deidentify("text");
    expect(global.fetch.mock.calls[1][1].headers["x-csrf-token"]).toBe("csrf-token-value");
  });

  it("sends the de-identification approval token when one was issued", async () => {
    global.fetch = vi.fn(async () => jsonResponse({}));

    await api.match({ deidentified_text: "x" }, { approvalToken: "tok-abc" });
    expect(global.fetch.mock.calls[0][1].headers["X-Deid-Approval"]).toBe("tok-abc");
  });

  it("omits the approval header when there is no token", async () => {
    global.fetch = vi.fn(async () => jsonResponse({}));

    await api.match({ deidentified_text: "x" });
    expect(global.fetch.mock.calls[0][1].headers["X-Deid-Approval"]).toBeUndefined();
  });

  it("posts the reviewed text to the approval endpoint", async () => {
    global.fetch = vi.fn(async () => jsonResponse({ approval_token: "t", expires_in_minutes: 30 }));

    await api.approveDeid("reviewed text");
    expect(global.fetch.mock.calls[0][0]).toBe("/api/approve-deid");
    expect(JSON.parse(global.fetch.mock.calls[0][1].body)).toEqual({ text: "reviewed text" });
  });
});
