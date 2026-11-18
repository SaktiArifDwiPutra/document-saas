import { parseApiResponse } from "@/lib/auth";
import { getApiBaseUrl } from "@/lib/config";

export type WorkspaceType = "personal" | "organization";
export type WorkspaceRole = "owner" | "admin" | "editor" | "member";
export type AssignableMemberRole = "admin" | "editor" | "member";

export interface WorkspaceItem {
  id: string;
  name: string;
  type: WorkspaceType;
  owner_user_id: string;
  status: string;
  my_role: WorkspaceRole;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceListData {
  items: WorkspaceItem[];
  page: number;
  page_size: number;
  total: number;
}

export interface WorkspaceMemberItem {
  user_id: string;
  email: string;
  display_name: string;
  role: WorkspaceRole;
  status: string;
  joined_at: string;
}

export interface WorkspaceMemberListData {
  items: WorkspaceMemberItem[];
  page: number;
  page_size: number;
  total: number;
}

export interface InvitationItem {
  id: string;
  workspace_id: string;
  email_normalized: string;
  role: AssignableMemberRole;
  expires_at: string;
  accepted_at: string | null;
  cancelled_at: string | null;
  created_by_user_id: string;
  created_at: string;
}

export interface InvitationListData {
  items: InvitationItem[];
  page: number;
  page_size: number;
  total: number;
}

export const ACTIVE_WORKSPACE_STORAGE_KEY = "document_saas.active_workspace_id";

export function getStoredActiveWorkspaceId(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const value = window.localStorage.getItem(ACTIVE_WORKSPACE_STORAGE_KEY);
    return value && value.trim() ? value.trim() : null;
  } catch {
    return null;
  }
}

export function setStoredActiveWorkspaceId(workspaceId: string): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(ACTIVE_WORKSPACE_STORAGE_KEY, workspaceId);
  } catch {
    // Ignore storage unavailability in restricted browser contexts.
  }
}

export function clearStoredActiveWorkspaceId(): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.removeItem(ACTIVE_WORKSPACE_STORAGE_KEY);
  } catch {
    // Ignore storage unavailability in restricted browser contexts.
  }
}

export function resolveActiveWorkspace(
  workspaces: WorkspaceItem[],
  preferredId?: string | null,
): WorkspaceItem | null {
  if (workspaces.length === 0) {
    clearStoredActiveWorkspaceId();
    return null;
  }

  const candidateId = preferredId ?? getStoredActiveWorkspaceId();
  const matched = candidateId ? workspaces.find((ws) => ws.id === candidateId) : undefined;
  const resolved = matched ?? workspaces.find((ws) => ws.type === "personal") ?? workspaces[0];
  setStoredActiveWorkspaceId(resolved.id);
  return resolved;
}

export async function listWorkspaces(
  accessToken: string,
  page = 1,
  pageSize = 20,
): Promise<WorkspaceListData> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  const res = await fetch(`${getApiBaseUrl()}/workspaces?${params.toString()}`, {
    method: "GET",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  return parseApiResponse<WorkspaceListData>(res);
}

export async function createOrganizationWorkspace(
  accessToken: string,
  input: { name: string },
): Promise<WorkspaceItem> {
  const res = await fetch(`${getApiBaseUrl()}/workspaces`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(input),
  });
  return parseApiResponse<WorkspaceItem>(res);
}

export async function getWorkspaceDetails(
  accessToken: string,
  workspaceId: string,
): Promise<WorkspaceItem> {
  const res = await fetch(`${getApiBaseUrl()}/workspaces/${encodeURIComponent(workspaceId)}`, {
    method: "GET",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  return parseApiResponse<WorkspaceItem>(res);
}

export async function listWorkspaceMembers(
  accessToken: string,
  workspaceId: string,
  page = 1,
  pageSize = 20,
): Promise<WorkspaceMemberListData> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  const res = await fetch(
    `${getApiBaseUrl()}/workspaces/${encodeURIComponent(workspaceId)}/members?${params.toString()}`,
    {
      method: "GET",
      headers: { Authorization: `Bearer ${accessToken}` },
    },
  );
  return parseApiResponse<WorkspaceMemberListData>(res);
}

export async function createWorkspaceInvitation(
  accessToken: string,
  workspaceId: string,
  input: { email: string; role: AssignableMemberRole },
): Promise<InvitationItem> {
  const res = await fetch(
    `${getApiBaseUrl()}/workspaces/${encodeURIComponent(workspaceId)}/invitations`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(input),
    },
  );
  return parseApiResponse<InvitationItem>(res);
}

export async function acceptWorkspaceInvitation(
  accessToken: string,
  token: string,
): Promise<WorkspaceItem> {
  const res = await fetch(`${getApiBaseUrl()}/invitations/accept`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ token }),
  });
  return parseApiResponse<WorkspaceItem>(res);
}

