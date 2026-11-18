import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import LoginPage from "@/app/login/page";
import RegisterPage from "@/app/register/page";
import {
  ApiClientError,
  AuthTokenData,
  CSRF_COOKIE_NAME,
  loginUser,
  logoutUser,
  readCsrfCookie,
  refreshSession,
  resetAuthClientState,
  resolveCsrfToken,
} from "@/lib/auth";

function makeAuthPayload(overrides: Partial<AuthTokenData> = {}): AuthTokenData {
  return {
    access_token: "access-token-1",
    token_type: "bearer",
    expires_in: 900,
    csrf_token: "csrf-v1",
    user: {
      id: "user-123",
      email: "user@example.com",
      display_name: "Test User",
      email_verified: true,
      status: "active",
      created_at: "2026-01-01T00:00:00Z",
    },
    ...overrides,
  };
}

function clearBrowserCookies(): void {
  if (typeof document === "undefined" || !document.cookie) {
    return;
  }
  for (const part of document.cookie.split(";")) {
    const eqIndex = part.indexOf("=");
    const name = (eqIndex === -1 ? part : part.slice(0, eqIndex)).trim();
    if (name) {
      document.cookie = `${name}=; Max-Age=0; path=/`;
    }
  }
}

describe("Authentication pages", () => {
  it("renders accessible Indonesian-first login form", () => {
    render(<LoginPage />);

    expect(screen.getByRole("heading", { name: "Masuk ke Akun" })).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Kata Sandi")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Masuk" })).toBeInTheDocument();
  });

  it("renders accessible Indonesian-first registration form", () => {
    render(<RegisterPage />);

    expect(screen.getByRole("heading", { name: "Daftar Akun Baru" })).toBeInTheDocument();
    expect(screen.getByLabelText("Nama Tampilan")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Kata Sandi")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Daftar" })).toBeInTheDocument();
  });
});

describe("Frontend single-flight refresh and CSRF cookie handling", () => {
  beforeEach(() => {
    resetAuthClientState();
    clearBrowserCookies();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    resetAuthClientState();
    clearBrowserCookies();
  });

  it("reads CSRF cookie accurately across formatting and URI-encoding edge cases", () => {
    expect(readCsrfCookie("")).toBeNull();
    expect(readCsrfCookie("other=abc")).toBeNull();
    expect(readCsrfCookie(`${CSRF_COOKIE_NAME}=`)).toBeNull();
    expect(readCsrfCookie(`foo=1; ${CSRF_COOKIE_NAME}=tok%2B123%3D; bar=2`)).toBe("tok+123=");
    expect(readCsrfCookie(`${CSRF_COOKIE_NAME}=%E0%A4%A`)).toBe("%E0%A4%A");
  });

  it("coalesces concurrent refreshSession calls into a single network request", async () => {
    document.cookie = `${CSRF_COOKIE_NAME}=cookie-csrf-v1; path=/`;

    let resolveFetch!: (value: Response) => void;
    const pendingResponse = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });

    const fetchMock = vi.fn().mockReturnValue(pendingResponse);
    vi.stubGlobal("fetch", fetchMock);

    const p1 = refreshSession("cookie-csrf-v1");
    const p2 = refreshSession("stale-caller-csrf");
    const p3 = refreshSession();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/auth/refresh",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: { "X-CSRF-Token": "cookie-csrf-v1" },
      }),
    );

    const rotatedPayload = makeAuthPayload({
      access_token: "access-token-2",
      csrf_token: "cookie-csrf-v2",
    });
    resolveFetch(
      new Response(JSON.stringify({ success: true, data: rotatedPayload }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const [r1, r2, r3] = await Promise.all([p1, p2, p3]);
    expect(r1).toEqual(rotatedPayload);
    expect(r2).toEqual(rotatedPayload);
    expect(r3).toEqual(rotatedPayload);
  });

  it("starts a new request after flight completion and reads the latest CSRF cookie", async () => {
    document.cookie = `${CSRF_COOKIE_NAME}=cookie-csrf-v1; path=/`;

    const payload1 = makeAuthPayload({ access_token: "access-1", csrf_token: "cookie-csrf-v2" });
    const payload2 = makeAuthPayload({ access_token: "access-2", csrf_token: "cookie-csrf-v3" });
    const payload3 = makeAuthPayload({ access_token: "access-3", csrf_token: "cookie-csrf-v4" });

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: payload1 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: payload2 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: payload3 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const first = await refreshSession("stale-initial-arg");
    expect(first).toEqual(payload1);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8000/api/v1/auth/refresh",
      expect.objectContaining({
        headers: { "X-CSRF-Token": "cookie-csrf-v1" },
      }),
    );

    // Simulate browser updating csrf_token cookie from Set-Cookie on refresh #1
    document.cookie = `${CSRF_COOKIE_NAME}=cookie-csrf-v2; path=/`;
    const second = await refreshSession("stale-csrf-v1");
    expect(second).toEqual(payload2);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/api/v1/auth/refresh",
      expect.objectContaining({
        headers: { "X-CSRF-Token": "cookie-csrf-v2" },
      }),
    );

    // Even when document.cookie is absent (e.g. path-scoped or non-DOM),
    // passing the consumed token falls back to the latest rotated csrf_token.
    clearBrowserCookies();
    const third = await refreshSession("cookie-csrf-v2");
    expect(third).toEqual(payload3);
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://localhost:8000/api/v1/auth/refresh",
      expect.objectContaining({
        headers: { "X-CSRF-Token": "cookie-csrf-v3" },
      }),
    );

    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("propagates rejection to all concurrent callers and clears in-flight state for retries", async () => {
    document.cookie = `${CSRF_COOKIE_NAME}=cookie-csrf-v1; path=/`;

    let resolveFailedFetch!: (value: Response) => void;
    const failedResponsePromise = new Promise<Response>((resolve) => {
      resolveFailedFetch = resolve;
    });

    const retryPayload = makeAuthPayload({
      access_token: "access-recovered",
      csrf_token: "cookie-csrf-recovered",
    });

    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(failedResponsePromise)
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: retryPayload }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const c1 = refreshSession("cookie-csrf-v1");
    const c2 = refreshSession("cookie-csrf-v1");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    resolveFailedFetch(
      new Response(
        JSON.stringify({
          success: false,
          error: { code: "UNAUTHENTICATED", message: "Sesi refresh tidak valid." },
        }),
        { status: 401, headers: { "Content-Type": "application/json" } },
      ),
    );

    const results = await Promise.allSettled([c1, c2]);
    expect(results[0].status).toBe("rejected");
    expect(results[1].status).toBe("rejected");
    if (results[0].status === "rejected" && results[1].status === "rejected") {
      expect(results[0].reason).toBeInstanceOf(ApiClientError);
      expect((results[0].reason as ApiClientError).code).toBe("UNAUTHENTICATED");
      expect(results[1].reason).toBe(results[0].reason);
    }

    // Subsequent retry must trigger a fresh fetch after failure cleared the in-flight slot
    document.cookie = `${CSRF_COOKIE_NAME}=cookie-csrf-retry; path=/`;
    const recovered = await refreshSession();
    expect(recovered).toEqual(retryPayload);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/api/v1/auth/refresh",
      expect.objectContaining({
        headers: { "X-CSRF-Token": "cookie-csrf-retry" },
      }),
    );
  });

  it("uses latest CSRF token on logoutUser and clears client CSRF state", async () => {
    document.cookie = `${CSRF_COOKIE_NAME}=cookie-csrf-logout; path=/`;

    const fetchMock = vi.fn().mockResolvedValueOnce(
      new Response(
        JSON.stringify({ success: true, data: { message: "Berhasil keluar dari sesi." } }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const res = await logoutUser("stale-csrf");
    expect(res.message).toBe("Berhasil keluar dari sesi.");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/auth/logout",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: { "X-CSRF-Token": "cookie-csrf-logout" },
      }),
    );
  });

  it("coordinates logout with an in-flight refresh and rejects stale refresh restoration", async () => {
    let resolveRefresh!: (value: Response) => void;
    const pendingRefresh = new Promise<Response>((resolve) => {
      resolveRefresh = resolve;
    });

    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(pendingRefresh)
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ success: true, data: { message: "Berhasil keluar dari sesi." } }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const refreshPromise = refreshSession("csrf-v1");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Logout starts while refresh is still pending; /auth/logout must wait for refresh settlement
    const logoutPromise = logoutUser("csrf-v1");
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    const rotatedPayload = makeAuthPayload({
      access_token: "access-v2",
      csrf_token: "csrf-v2",
    });
    resolveRefresh(
      new Response(JSON.stringify({ success: true, data: rotatedPayload }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const [refreshOutcome, logoutOutcome] = await Promise.allSettled([
      refreshPromise,
      logoutPromise,
    ]);

    expect(refreshOutcome.status).toBe("rejected");
    if (refreshOutcome.status === "rejected") {
      expect(refreshOutcome.reason).toBeInstanceOf(ApiClientError);
      expect((refreshOutcome.reason as ApiClientError).code).toBe("UNAUTHENTICATED");
    }

    expect(logoutOutcome.status).toBe("fulfilled");
    if (logoutOutcome.status === "fulfilled") {
      expect(logoutOutcome.value.message).toBe("Berhasil keluar dari sesi.");
    }

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/api/v1/auth/logout",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: { "X-CSRF-Token": "csrf-v2" },
      }),
    );
    expect(resolveCsrfToken()).toBe("");
  });

  it("uses rotated CSRF token when refresh resolves before logout and clears state", async () => {
    const rotatedPayload = makeAuthPayload({
      access_token: "access-v2",
      csrf_token: "csrf-v2",
    });

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: rotatedPayload }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ success: true, data: { message: "Berhasil keluar dari sesi." } }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const refreshed = await refreshSession("csrf-v1");
    expect(refreshed.csrf_token).toBe("csrf-v2");

    const loggedOut = await logoutUser("csrf-v1");
    expect(loggedOut.message).toBe("Berhasil keluar dari sesi.");
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/api/v1/auth/logout",
      expect.objectContaining({
        headers: { "X-CSRF-Token": "csrf-v2" },
      }),
    );
    expect(resolveCsrfToken()).toBe("");
  });

  it("prevents an older in-flight refresh from overwriting a newer login session", async () => {
    let resolveStaleRefresh!: (value: Response) => void;
    const pendingRefresh = new Promise<Response>((resolve) => {
      resolveStaleRefresh = resolve;
    });

    const loginPayload = makeAuthPayload({
      access_token: "access-fresh-login",
      csrf_token: "csrf-fresh-login",
    });

    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(pendingRefresh)
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: loginPayload }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const staleRefreshPromise = refreshSession("csrf-old");
    const loginResult = await loginUser({
      email: "user@example.com",
      password: "StrongPassword123!",
    });
    expect(loginResult).toEqual(loginPayload);
    expect(resolveCsrfToken()).toBe("csrf-fresh-login");

    resolveStaleRefresh(
      new Response(
        JSON.stringify({
          success: true,
          data: makeAuthPayload({
            access_token: "access-stale-rotated",
            csrf_token: "csrf-stale-rotated",
          }),
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(staleRefreshPromise).rejects.toMatchObject({
      code: "UNAUTHENTICATED",
    });
    expect(resolveCsrfToken()).toBe("csrf-fresh-login");
  });

  it("tracks multi-step rotations when a caller repeatedly passes its initial CSRF token", async () => {
    const p1 = makeAuthPayload({ access_token: "access-1", csrf_token: "csrf-v2" });
    const p2 = makeAuthPayload({ access_token: "access-2", csrf_token: "csrf-v3" });
    const p3 = makeAuthPayload({ access_token: "access-3", csrf_token: "csrf-v4" });

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: p1 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: p2 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: p3 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await refreshSession("csrf-v1");
    await refreshSession("csrf-v1");
    await refreshSession("csrf-v1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8000/api/v1/auth/refresh",
      expect.objectContaining({ headers: { "X-CSRF-Token": "csrf-v1" } }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/api/v1/auth/refresh",
      expect.objectContaining({ headers: { "X-CSRF-Token": "csrf-v2" } }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://localhost:8000/api/v1/auth/refresh",
      expect.objectContaining({ headers: { "X-CSRF-Token": "csrf-v3" } }),
    );
  });
});

