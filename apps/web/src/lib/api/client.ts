import { apiBaseUrl } from "@/lib/env";

/**
 * The single place the frontend talks HTTP to the DevLens API: base URL,
 * request construction, JSON parsing, and error normalization. Components
 * never call `fetch` directly; they call the typed functions in
 * `endpoints.ts`, which call `request` here.
 */

export type ApiErrorKind =
  /** The API could not be reached at all (server down, CORS, DNS). */
  | "network"
  /** 404: the repository/revision/session/artifact does not exist. */
  | "not_found"
  /** 409: the entity exists but conflicts (wrong repository, ambiguity). */
  | "conflict"
  /** 400/422: the request was rejected as invalid. */
  | "invalid_request"
  /** 502/503: the backend reached a failing dependency (provider, scanner). */
  | "upstream"
  /** Any other non-2xx status. */
  | "http"
  /** 2xx, but the body was not the JSON shape this client expects. */
  | "malformed_response";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  /** The parsed FastAPI `detail` value, when one was returned. */
  readonly detail: unknown;

  constructor(
    kind: ApiErrorKind,
    message: string,
    status: number | null,
    detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }
}

function kindForStatus(status: number): ApiErrorKind {
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 400 || status === 422) return "invalid_request";
  if (status === 502 || status === 503) return "upstream";
  return "http";
}

/**
 * Turns a FastAPI `detail` (a string, a structured object, or a list of
 * validation issues) into one short human-readable sentence. Never echoes
 * a stack trace: FastAPI does not send one, and anything unrecognized
 * falls back to a generic status message.
 */
export function describeDetail(detail: unknown, status: number): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((issue) =>
        issue && typeof issue === "object" && "msg" in issue
          ? String((issue as { msg: unknown }).msg)
          : null,
      )
      .filter((m): m is string => Boolean(m));
    if (messages.length) return messages.slice(0, 3).join("; ");
  }
  if (detail && typeof detail === "object" && "message" in detail) {
    const message = (detail as { message: unknown }).message;
    if (typeof message === "string") return message;
  }
  return `Request failed with status ${status}.`;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | undefined | null>;
  signal?: AbortSignal;
}

export function buildUrl(
  path: string,
  query?: RequestOptions["query"],
): string {
  const url = new URL(path.replace(/^\//, ""), apiBaseUrl.replace(/\/?$/, "/"));
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

/**
 * Performs one request and returns the parsed JSON body, validated by
 * `parse`. Every failure surfaces as an `ApiError`; abort errors are
 * re-thrown untouched so callers can ignore them.
 */
export async function request<T>(
  path: string,
  parse: (body: unknown) => T,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body, query, signal } = options;
  let response: Response;
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      signal,
      headers:
        body === undefined
          ? { Accept: "application/json" }
          : {
              Accept: "application/json",
              "Content-Type": "application/json",
            },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApiError(
      "network",
      "The DevLens API could not be reached. Check that the backend is running and NEXT_PUBLIC_API_URL is correct.",
      null,
    );
  }

  if (response.status === 204) return parse(null);

  let payload: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      if (response.ok) {
        throw new ApiError(
          "malformed_response",
          "The API returned a response that is not valid JSON.",
          response.status,
        );
      }
    }
  }

  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? (payload as { detail: unknown }).detail
        : undefined;
    throw new ApiError(
      kindForStatus(response.status),
      describeDetail(detail, response.status),
      response.status,
      detail,
    );
  }

  try {
    return parse(payload);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(
      "malformed_response",
      "The API response did not have the expected shape.",
      response.status,
    );
  }
}

// ---------- Minimal structural validation ----------

/**
 * Checks that `value` is an object carrying every listed key, and that the
 * keys listed in `arrays` hold arrays. This is deliberately shallow: it
 * protects views from rendering a wrong-endpoint or truncated body, not a
 * full schema validator duplicating the backend's Pydantic models.
 */
export function expectShape<T>(
  value: unknown,
  keys: readonly string[],
  arrays: readonly string[] = [],
): T {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ApiError(
      "malformed_response",
      "The API response was not a JSON object.",
      null,
    );
  }
  const record = value as Record<string, unknown>;
  for (const key of keys) {
    if (!(key in record)) {
      throw new ApiError(
        "malformed_response",
        `The API response is missing "${key}".`,
        null,
      );
    }
  }
  for (const key of arrays) {
    if (!Array.isArray(record[key])) {
      throw new ApiError(
        "malformed_response",
        `The API response field "${key}" is not a list.`,
        null,
      );
    }
  }
  return value as T;
}
