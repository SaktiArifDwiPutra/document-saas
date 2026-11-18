import { getApiBaseUrl } from "@/lib/config";

export const CSRF_COOKIE_NAME = "csrf_token";

let inFlightRefreshPromise: Promise<AuthTokenData> | null = null;
let inFlightRefreshRawPromise: Promise<AuthTokenData | null> | null = null;
let authGeneration = 0;
let latestCsrfToken: string | null = null;
const consumedCsrfTokens = new Set<string>();

export interface UserProfile {
  id: string;
  email: string;
  display_name: string;
  email_verified: boolean;
  status: string;
  created_at: string;
}

export interface AuthTokenData {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  csrf_token: string;
  user: UserProfile;
}

interface ApiSuccess<T> {
  success: true;
  data: T;
}

interface ApiFailure {
  success: false;
  error: {
    code: string;
    message: string;
  };
}

export class ApiClientError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

export async function parseApiResponse<T>(res: Response): Promise<T> {
  const json = (await res.json()) as ApiSuccess<T> | ApiFailure;
  if (!res.ok || !json.success) {
    const code = !json.success ? json.error.code : "INTERNAL_ERROR";
    const message = !json.success ? json.error.message : "Terjadi kesalahan permintaan.";
    throw new ApiClientError(code, message);
  }
  return json.data;
}

const parseAuthResponse = parseApiResponse;

export function readCsrfCookie(cookieSource?: string): string | null {
  const source = cookieSource ?? (typeof document !== "undefined" ? document.cookie : "");
  if (!source) {
    return null;
  }

  for (const segment of source.split(";")) {
    const trimmed = segment.trim();
    if (!trimmed) {
      continue;
    }
    const separatorIndex = trimmed.indexOf("=");
    if (separatorIndex === -1) {
      continue;
    }
    const name = trimmed.slice(0, separatorIndex).trim();
    if (name !== CSRF_COOKIE_NAME) {
      continue;
    }
    const rawValue = trimmed.slice(separatorIndex + 1).trim();
    if (!rawValue) {
      return null;
    }
    try {
      const decoded = decodeURIComponent(rawValue).trim();
      return decoded || null;
    } catch {
      return rawValue;
    }
  }

  return null;
}

export function resolveCsrfToken(fallbackToken?: string): string {
  const cookieToken = readCsrfCookie();
  if (cookieToken) {
    return cookieToken;
  }

  const trimmedFallback = fallbackToken?.trim() ?? "";
  if (!trimmedFallback) {
    return latestCsrfToken ?? "";
  }
  if (consumedCsrfTokens.has(trimmedFallback) && latestCsrfToken) {
    return latestCsrfToken;
  }
  return trimmedFallback;
}

export function resetAuthClientState(): void {
  inFlightRefreshPromise = null;
  inFlightRefreshRawPromise = null;
  authGeneration = 0;
  latestCsrfToken = null;
  consumedCsrfTokens.clear();
}

export async function registerUser(input: {
  email: string;
  password: string;
  display_name: string;
}): Promise<AuthTokenData> {
  authGeneration += 1;
  inFlightRefreshPromise = null;
  inFlightRefreshRawPromise = null;

  const res = await fetch(`${getApiBaseUrl()}/auth/register`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  const data = await parseAuthResponse<AuthTokenData>(res);
  authGeneration += 1;
  latestCsrfToken = data.csrf_token;
  consumedCsrfTokens.clear();
  return data;
}

export async function loginUser(input: {
  email: string;
  password: string;
}): Promise<AuthTokenData> {
  authGeneration += 1;
  inFlightRefreshPromise = null;
  inFlightRefreshRawPromise = null;

  const res = await fetch(`${getApiBaseUrl()}/auth/login`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  const data = await parseAuthResponse<AuthTokenData>(res);
  authGeneration += 1;
  latestCsrfToken = data.csrf_token;
  consumedCsrfTokens.clear();
  return data;
}

export async function refreshSession(csrfToken?: string): Promise<AuthTokenData> {
  if (inFlightRefreshPromise !== null) {
    return inFlightRefreshPromise;
  }

  const requestGeneration = authGeneration;
  const resolvedCsrfToken = resolveCsrfToken(csrfToken);

  const rawPromise = (async (): Promise<AuthTokenData> => {
    const res = await fetch(`${getApiBaseUrl()}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: { "X-CSRF-Token": resolvedCsrfToken },
    });
    try {
      return await parseAuthResponse<AuthTokenData>(res);
    } catch (error) {
      if (
        error instanceof ApiClientError &&
        error.code === "UNAUTHENTICATED" &&
        authGeneration === requestGeneration
      ) {
        latestCsrfToken = null;
        consumedCsrfTokens.clear();
      }
      throw error;
    }
  })();

  const settledRawPromise = rawPromise.then(
    (data) => data,
    () => null,
  );
  inFlightRefreshRawPromise = settledRawPromise;

  const trackedPromise = rawPromise
    .then((data) => {
      if (authGeneration !== requestGeneration) {
        throw new ApiClientError(
          "UNAUTHENTICATED",
          "Sesi autentikasi telah berubah atau berakhir.",
        );
      }
      if (resolvedCsrfToken) {
        consumedCsrfTokens.add(resolvedCsrfToken);
      }
      latestCsrfToken = data.csrf_token;
      return data;
    })
    .finally(() => {
      if (inFlightRefreshPromise === trackedPromise) {
        inFlightRefreshPromise = null;
      }
      if (inFlightRefreshRawPromise === settledRawPromise) {
        inFlightRefreshRawPromise = null;
      }
    });

  inFlightRefreshPromise = trackedPromise;
  return trackedPromise;
}

export async function logoutUser(csrfToken?: string): Promise<{ message: string }> {
  const preLogoutCsrfToken = resolveCsrfToken(csrfToken);
  const pendingRefresh = inFlightRefreshRawPromise;

  authGeneration += 1;
  const logoutGeneration = authGeneration;
  inFlightRefreshPromise = null;
  inFlightRefreshRawPromise = null;
  latestCsrfToken = null;
  consumedCsrfTokens.clear();

  const refreshResult = pendingRefresh !== null ? await pendingRefresh : null;
  const logoutCsrfToken =
    readCsrfCookie() ?? refreshResult?.csrf_token ?? preLogoutCsrfToken;

  try {
    const res = await fetch(`${getApiBaseUrl()}/auth/logout`, {
      method: "POST",
      credentials: "include",
      headers: { "X-CSRF-Token": logoutCsrfToken },
    });
    return await parseAuthResponse<{ message: string }>(res);
  } finally {
    if (authGeneration === logoutGeneration) {
      latestCsrfToken = null;
      consumedCsrfTokens.clear();
    }
  }
}

export async function fetchCurrentUser(accessToken: string): Promise<UserProfile> {
  const res = await fetch(`${getApiBaseUrl()}/auth/me`, {
    method: "GET",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  return parseAuthResponse<UserProfile>(res);
}

