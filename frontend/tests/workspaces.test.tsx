import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import WorkspacesPage from "@/app/workspaces/page";
import {
  acceptWorkspaceInvitation,
  ACTIVE_WORKSPACE_STORAGE_KEY,
  clearStoredActiveWorkspaceId,
  createOrganizationWorkspace,
  getStoredActiveWorkspaceId,
  getWorkspaceDetails,
  listWorkspaces,
  resolveActiveWorkspace,
  setStoredActiveWorkspaceId,
  WorkspaceItem,
} from "@/lib/workspaces";

function makeWorkspace(overrides: Partial<WorkspaceItem> = {}): WorkspaceItem {
  return {
    id: "ws-personal-1",
    name: "Workspace Pribadi - Sakti",
    type: "personal",
    owner_user_id: "user-1",
    status: "active",
    my_role: "owner",
    created_at: "2026-11-18T00:00:00Z",
    updated_at: "2026-11-18T00:00:00Z",
    ...overrides,
  };
}

describe("Client-side active workspace selection and API helpers", () => {
  beforeEach(() => {
    clearStoredActiveWorkspaceId();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    clearStoredActiveWorkspaceId();
  });

  it("renders Indonesian-first workspace management page", () => {
    render(<WorkspacesPage />);

    expect(
      screen.getByRole("heading", { name: "Kelola & Pilih Workspace Aktif" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Token Akses Bearer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Muat dari Sesi" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Buat Organisasi" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Gabung Workspace" })).toBeInTheDocument();
  });

  it("persists active workspace and falls back to personal workspace when membership is lost", () => {
    const personal = makeWorkspace();
    const org = makeWorkspace({
      id: "ws-org-1",
      name: "OSIS SMA 1",
      type: "organization",
      my_role: "editor",
    });

    const selectedOrg = resolveActiveWorkspace([personal, org], org.id);
    expect(selectedOrg?.id).toBe("ws-org-1");
    expect(getStoredActiveWorkspaceId()).toBe("ws-org-1");
    expect(window.localStorage.getItem(ACTIVE_WORKSPACE_STORAGE_KEY)).toBe("ws-org-1");

    // Simulate user removed from org so GET /workspaces only returns personal workspace
    setStoredActiveWorkspaceId("ws-org-1");
    const fallback = resolveActiveWorkspace([personal]);
    expect(fallback?.id).toBe("ws-personal-1");
    expect(getStoredActiveWorkspaceId()).toBe("ws-personal-1");

    // Empty workspace list clears stored ID
    expect(resolveActiveWorkspace([])).toBeNull();
    expect(getStoredActiveWorkspaceId()).toBeNull();
  });

  it("calls workspace and invitation endpoints with Bearer authorization", async () => {
    const personal = makeWorkspace();
    const org = makeWorkspace({
      id: "ws-org-2",
      name: "BEM Fakultas",
      type: "organization",
      my_role: "owner",
    });

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            success: true,
            data: { items: [personal], page: 1, page_size: 20, total: 1 },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: org }), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: org }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: org }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const listed = await listWorkspaces("jwt-token-1");
    expect(listed.items).toEqual([personal]);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8000/api/v1/workspaces?page=1&page_size=20",
      expect.objectContaining({
        method: "GET",
        headers: { Authorization: "Bearer jwt-token-1" },
      }),
    );

    const created = await createOrganizationWorkspace("jwt-token-1", { name: "BEM Fakultas" });
    expect(created.id).toBe("ws-org-2");
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/api/v1/workspaces",
      expect.objectContaining({
        method: "POST",
        headers: {
          Authorization: "Bearer jwt-token-1",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ name: "BEM Fakultas" }),
      }),
    );

    const details = await getWorkspaceDetails("jwt-token-1", "ws-org-2");
    expect(details.name).toBe("BEM Fakultas");
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://localhost:8000/api/v1/workspaces/ws-org-2",
      expect.objectContaining({
        method: "GET",
        headers: { Authorization: "Bearer jwt-token-1" },
      }),
    );

    const joined = await acceptWorkspaceInvitation("jwt-token-1", "invite-secret-token");
    expect(joined.id).toBe("ws-org-2");
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://localhost:8000/api/v1/invitations/accept",
      expect.objectContaining({
        method: "POST",
        headers: {
          Authorization: "Bearer jwt-token-1",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ token: "invite-secret-token" }),
      }),
    );
  });
});

