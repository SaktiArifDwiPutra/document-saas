import { parseApiResponse } from "@/lib/auth";
import { getApiBaseUrl } from "@/lib/config";
import { WorkspaceType } from "@/lib/workspaces";

export type CreditTransactionType =
  | "purchase"
  | "grant"
  | "commit"
  | "release"
  | "refund"
  | "adjustment"
  | "expiry";

export interface CreditWalletData {
  id: string;
  workspace_id: string;
  available_balance: number;
  reserved_balance: number;
  total_balance: number;
  updated_at: string;
}

export interface CreditTransactionItem {
  id: string;
  wallet_id: string;
  workspace_id: string;
  transaction_type: CreditTransactionType;
  amount: number;
  reference_type: string | null;
  reference_id: string | null;
  idempotency_key: string;
  description: string;
  created_by_user_id: string | null;
  created_at: string;
}

export interface CreditTransactionListData {
  items: CreditTransactionItem[];
  page: number;
  page_size: number;
  total: number;
}

export interface CreditPackageItem {
  id: string;
  name: string;
  credits: number;
  price_minor_units: number;
  currency: string;
  workspace_type: string;
  is_active: boolean;
}

export interface CreditPackageListData {
  items: CreditPackageItem[];
}

export async function getWorkspaceCredits(
  accessToken: string,
  workspaceId: string,
): Promise<CreditWalletData> {
  const res = await fetch(
    `${getApiBaseUrl()}/workspaces/${encodeURIComponent(workspaceId)}/credits`,
    {
      method: "GET",
      headers: { Authorization: `Bearer ${accessToken}` },
    },
  );
  return parseApiResponse<CreditWalletData>(res);
}

export async function listWorkspaceCreditTransactions(
  accessToken: string,
  workspaceId: string,
  page = 1,
  pageSize = 20,
): Promise<CreditTransactionListData> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  const res = await fetch(
    `${getApiBaseUrl()}/workspaces/${encodeURIComponent(workspaceId)}/credits/transactions?${params.toString()}`,
    {
      method: "GET",
      headers: { Authorization: `Bearer ${accessToken}` },
    },
  );
  return parseApiResponse<CreditTransactionListData>(res);
}

export async function listCreditPackages(
  workspaceType?: WorkspaceType,
): Promise<CreditPackageListData> {
  const query = workspaceType
    ? `?workspace_type=${encodeURIComponent(workspaceType)}`
    : "";
  const res = await fetch(`${getApiBaseUrl()}/credit-packages${query}`, {
    method: "GET",
  });
  return parseApiResponse<CreditPackageListData>(res);
}

