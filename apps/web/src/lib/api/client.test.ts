import { describe, expect, it } from "vitest";
import { jsonResponse, stubFetch } from "@/test/fixtures";
import { ApiError, buildUrl, expectShape, request } from "./client";
import { analyzeImpact, getOverview } from "./endpoints";

const parseAny = (body: unknown) => body;

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof ApiError) return error;
    throw error;
  }
  throw new Error("expected the request to fail");
}

describe("buildUrl", () => {
  it("joins the configured base URL and drops empty query values", () => {
    expect(
      buildUrl("/repositories/x/overview", {
        revision_id: null,
        limit: 5,
        q: "",
      }),
    ).toBe("http://localhost:8000/repositories/x/overview?limit=5");
  });
});

describe("request", () => {
  it("returns the parsed body on success and sends JSON bodies", async () => {
    const fetchMock = stubFetch(() => jsonResponse({ ok: true }));
    await expect(
      request("/x", parseAny, { method: "POST", body: { a: 1 } }),
    ).resolves.toEqual({ ok: true });
    const [, init] = fetchMock.mock.calls[0];
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify({ a: 1 }));
  });

  it("normalizes an unreachable API to a network error", async () => {
    stubFetch(() => {
      throw new TypeError("Failed to fetch");
    });
    const error = await failure(request("/x", parseAny));
    expect(error.kind).toBe("network");
    expect(error.status).toBeNull();
  });

  it("maps 404 with a string detail to not_found and keeps the message", async () => {
    stubFetch(() => jsonResponse({ detail: "no repository abc" }, 404));
    const error = await failure(request("/x", parseAny));
    expect(error.kind).toBe("not_found");
    expect(error.message).toBe("no repository abc");
  });

  it("summarizes FastAPI validation lists without leaking structure", async () => {
    stubFetch(() =>
      jsonResponse(
        {
          detail: [
            { loc: ["body", "url"], msg: "Field required", type: "missing" },
          ],
        },
        422,
      ),
    );
    const error = await failure(request("/x", parseAny));
    expect(error.kind).toBe("invalid_request");
    expect(error.message).toBe("Field required");
  });

  it("keeps a structured 409 detail available to the caller", async () => {
    const detail = {
      message: "qualified name is ambiguous",
      candidates: [
        { symbol_id: "s1", file_path: "a.py", symbol_kind: "function" },
      ],
    };
    stubFetch(() => jsonResponse({ detail }, 409));
    const error = await failure(request("/x", parseAny));
    expect(error.kind).toBe("conflict");
    expect(error.message).toBe("qualified name is ambiguous");
    expect(error.detail).toEqual(detail);
  });

  it("maps 502 to upstream and non-JSON error bodies to a generic message", async () => {
    stubFetch(() => new Response("<html>Bad gateway</html>", { status: 502 }));
    const error = await failure(request("/x", parseAny));
    expect(error.kind).toBe("upstream");
    expect(error.message).toBe("Request failed with status 502.");
  });

  it("rejects a 200 response that is not JSON", async () => {
    stubFetch(() => new Response("not json", { status: 200 }));
    expect((await failure(request("/x", parseAny))).kind).toBe(
      "malformed_response",
    );
  });
});

describe("response shape validation", () => {
  it("rejects an overview body missing required sections", async () => {
    stubFetch(() => jsonResponse({ repository: {} }));
    const error = await failure(getOverview("r", null));
    expect(error.kind).toBe("malformed_response");
  });

  it("rejects an impact body whose list fields are not lists", async () => {
    stubFetch(() =>
      jsonResponse({
        target_symbol_id: "t",
        target_qualified_name: "a.b",
        max_depth: 0,
        analysis_status: "deterministic",
        direct_impacts: null,
        transitive_impacts: [],
        impacted_files: [],
      }),
    );
    const error = await failure(
      analyzeImpact("r", {
        revision_id: "v",
        qualified_name: "a.b",
        include_explanation: false,
      }),
    );
    expect(error.kind).toBe("malformed_response");
    expect(error.message).toContain("direct_impacts");
  });

  it("expectShape rejects arrays and primitives", () => {
    expect(() => expectShape([], ["a"])).toThrow(ApiError);
    expect(() => expectShape("x", [])).toThrow(ApiError);
  });

  it("sends the explicit revision as a query parameter", async () => {
    const fetchMock = stubFetch(() => jsonResponse({}));
    await failure(getOverview("repo-1", "rev-9"));
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "http://localhost:8000/repositories/repo-1/overview?revision_id=rev-9",
    );
  });
});
