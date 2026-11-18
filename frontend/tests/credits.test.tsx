import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import WorkspacesPage from "@/app/workspaces/page";
import {
  getWorkspaceCredits,
  listCreditPackages,
  listWorkspaceCreditTransactions,
} from "@/lib/credits";

describe("Frontend credit wallet and ledger helpers", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("fetches workspace wallet summary, transactions, and credit packages", async () => {
    const walletPayload = {
      id: "wallet-1",
      workspace_id: "ws-1",
      available_balance: 25,
      reserved_balance: 5,
      total_balance: 30,
      updated_at: "2026-11-18T00:00:00Z",
    };
    const txPayload = {
      items: [
        {
          id: "tx-1",
          wallet_id: "wallet-1",
          workspace_id: "ws-1",
          transaction_type: "grant",
          amount: 30,
          reference_type: "beta_complimentary_grant",
          reference_id: "ws-1",
          idempotency_key: "initial_beta_grant:ws-1",
          description: "Alokasi kredit beta awal",
          created_by_user_id: "user-1",
          created_at: "2026-11-18T00:00:00Z",
        },
      ],
      page: 1,
      page_size: 10,
      total: 1,
    };
    const packagesPayload = {
      items: [
        {
          id: "pkg-1",
          name: "Paket Beta",
          credits: 100,
          price_minor_units: 0,
          currency: "IDR",
          workspace_type: "all",
          is_active: true,
        },
      ],
    };

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: walletPayload }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: txPayload }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true, data: packagesPayload }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const wallet = await getWorkspaceCredits("jwt-1", "ws-1");
    expect(wallet.available_balance).toBe(25);
    expect(wallet.reserved_balance).toBe(5);
    expect(wallet.total_balance).toBe(30);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8000/api/v1/workspaces/ws-1/credits",
      expect.objectContaining({
        method: "GET",
        headers: { Authorization: "Bearer jwt-1" },
      }),
    );

    const txs = await listWorkspaceCreditTransactions("jwt-1", "ws-1", 1, 10);
    expect(txs.items).toHaveLength(1);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/api/v1/workspaces/ws-1/credits/transactions?page=1&page_size=10",
      expect.objectContaining({
        method: "GET",
        headers: { Authorization: "Bearer jwt-1" },
      }),
    );

    const packages = await listCreditPackages("organization");
    expect(packages.items[0]?.name).toBe("Paket Beta");
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://localhost:8000/api/v1/credit-packages?workspace_type=organization",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("renders credit wallet summary and ledger entries on /workspaces after loading", async () => {
    const ws = {
      id: "ws-personal-1",
      name: "Workspace Pribadi - Sakti",
      type: "personal",
      owner_user_id: "user-1",
      status: "active",
      my_role: "owner",
      created_at: "2026-11-18T00:00:00Z",
      updated_at: "2026-11-18T00:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            success: true,
            data: { items: [ws], page: 1, page_size: 20, total: 1 },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            success: true,
            data: {
              items: [
                {
                  user_id: "user-1",
                  email: "sakti@example.com",
                  display_name: "Sakti",
                  role: "owner",
                  status: "active",
                  joined_at: "2026-11-18T00:00:00Z",
                },
              ],
              page: 1,
              page_size: 20,
              total: 1,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            success: true,
            data: {
              id: "wallet-1",
              workspace_id: "ws-personal-1",
              available_balance: 20,
              reserved_balance: 5,
              total_balance: 25,
              updated_at: "2026-11-18T00:00:00Z",
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            success: true,
            data: {
              items: [
                {
                  id: "tx-1",
                  wallet_id: "wallet-1",
                  workspace_id: "ws-personal-1",
                  transaction_type: "grant",
                  amount: 25,
                  reference_type: "beta_complimentary_grant",
                  reference_id: "ws-personal-1",
                  idempotency_key: "initial_beta_grant:ws-personal-1",
                  description: "Alokasi kredit beta awal",
                  created_by_user_id: "user-1",
                  created_at: "2026-11-18T00:00:00Z",
                },
              ],
              page: 1,
              page_size: 10,
              total: 1,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    render(<WorkspacesPage />);
    fireEvent.change(screen.getByLabelText("Token Akses Bearer"), {
      target: { value: "jwt-token-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Muat Workspace" }));

    await waitFor(() => {
      expect(
        screen.getByText("Ringkasan Kredit (Workspace Pribadi - Sakti)"),
      ).toBeInTheDocument();
    });
    expect(screen.getByText("Tersedia")).toBeInTheDocument();
    expect(screen.getByText("Dicadangkan")).toBeInTheDocument();
    expect(screen.getByText("Total")).toBeInTheDocument();
    expect(screen.getByText("Alokasi kredit beta awal")).toBeInTheDocument();
    expect(screen.getByText("+25 (grant)")).toBeInTheDocument();
  });
});

