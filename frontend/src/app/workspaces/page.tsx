"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";

import { ApiClientError, refreshSession } from "@/lib/auth";
import {
  CreditTransactionItem,
  CreditWalletData,
  getWorkspaceCredits,
  listWorkspaceCreditTransactions,
} from "@/lib/credits";
import {
  acceptWorkspaceInvitation,
  AssignableMemberRole,
  createOrganizationWorkspace,
  createWorkspaceInvitation,
  getWorkspaceDetails,
  listWorkspaceMembers,
  listWorkspaces,
  resolveActiveWorkspace,
  setStoredActiveWorkspaceId,
  WorkspaceItem,
  WorkspaceMemberItem,
} from "@/lib/workspaces";

export default function WorkspacesPage() {
  const [accessToken, setAccessToken] = useState("");
  const [workspaces, setWorkspaces] = useState<WorkspaceItem[]>([]);
  const [activeWorkspace, setActiveWorkspace] = useState<WorkspaceItem | null>(null);
  const [members, setMembers] = useState<WorkspaceMemberItem[]>([]);
  const [creditWallet, setCreditWallet] = useState<CreditWalletData | null>(null);
  const [creditTransactions, setCreditTransactions] = useState<CreditTransactionItem[]>([]);
  const [newOrgName, setNewOrgName] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<AssignableMemberRole>("member");
  const [inviteToken, setInviteToken] = useState("");
  const [loading, setLoading] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  async function loadWorkspaceContext(token: string, workspaceId: string) {
    const [membersData, walletData, txData] = await Promise.all([
      listWorkspaceMembers(token, workspaceId),
      getWorkspaceCredits(token, workspaceId),
      listWorkspaceCreditTransactions(token, workspaceId, 1, 10),
    ]);
    setMembers(membersData.items);
    setCreditWallet(walletData);
    setCreditTransactions(txData.items);
  }

  async function syncWorkspaces(token: string, preferredWorkspaceId?: string) {
    const data = await listWorkspaces(token);
    setWorkspaces(data.items);
    const resolved = resolveActiveWorkspace(data.items, preferredWorkspaceId);
    setActiveWorkspace(resolved);
    if (resolved) {
      await loadWorkspaceContext(token, resolved.id);
    } else {
      setMembers([]);
      setCreditWallet(null);
      setCreditTransactions([]);
    }
  }

  async function handleLoadFromSession() {
    setErrorMessage(null);
    setStatusMessage(null);
    setLoading(true);
    try {
      const session = await refreshSession();
      setAccessToken(session.access_token);
      await syncWorkspaces(session.access_token);
      setStatusMessage("Sesi diperbarui dan daftar workspace berhasil dimuat.");
    } catch (error) {
      if (error instanceof ApiClientError) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("Gagal memuat sesi atau daftar workspace.");
      }
    } finally {
      setLoading(false);
    }
  }

  async function handleSelectWorkspace(workspaceId: string) {
    if (!accessToken.trim()) {
      setErrorMessage("Muat sesi atau masukkan token akses terlebih dahulu.");
      return;
    }
    setErrorMessage(null);
    setStatusMessage(null);
    setLoading(true);
    try {
      const details = await getWorkspaceDetails(accessToken.trim(), workspaceId);
      setStoredActiveWorkspaceId(details.id);
      setActiveWorkspace(details);
      await loadWorkspaceContext(accessToken.trim(), details.id);
      setStatusMessage(`Workspace aktif beralih ke ${details.name}.`);
    } catch (error) {
      if (error instanceof ApiClientError) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("Gagal mengganti workspace aktif.");
      }
    } finally {
      setLoading(false);
    }
  }

  async function handleCreateOrganization(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!accessToken.trim()) {
      setErrorMessage("Muat sesi atau masukkan token akses terlebih dahulu.");
      return;
    }
    setErrorMessage(null);
    setStatusMessage(null);
    setLoading(true);
    try {
      const created = await createOrganizationWorkspace(accessToken.trim(), { name: newOrgName });
      setNewOrgName("");
      await syncWorkspaces(accessToken.trim(), created.id);
      setStatusMessage(`Organisasi "${created.name}" berhasil dibuat.`);
    } catch (error) {
      if (error instanceof ApiClientError) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("Gagal membuat workspace organisasi.");
      }
    } finally {
      setLoading(false);
    }
  }

  async function handleAcceptInvitation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!accessToken.trim()) {
      setErrorMessage("Muat sesi atau masukkan token akses terlebih dahulu.");
      return;
    }
    setErrorMessage(null);
    setStatusMessage(null);
    setLoading(true);
    try {
      const joined = await acceptWorkspaceInvitation(accessToken.trim(), inviteToken);
      setInviteToken("");
      await syncWorkspaces(accessToken.trim(), joined.id);
      setStatusMessage(`Berhasil bergabung ke workspace "${joined.name}".`);
    } catch (error) {
      if (error instanceof ApiClientError) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("Gagal menerima undangan workspace.");
      }
    } finally {
      setLoading(false);
    }
  }

  async function handleInviteMember(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!accessToken.trim() || !activeWorkspace) {
      return;
    }
    setErrorMessage(null);
    setStatusMessage(null);
    setLoading(true);
    try {
      const invited = await createWorkspaceInvitation(accessToken.trim(), activeWorkspace.id, {
        email: inviteEmail,
        role: inviteRole,
      });
      setInviteEmail("");
      setStatusMessage(`Undangan dikirim untuk ${invited.email_normalized} sebagai ${invited.role}.`);
    } catch (error) {
      if (error instanceof ApiClientError) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("Gagal mengirim undangan anggota.");
      }
    } finally {
      setLoading(false);
    }
  }

  const canInviteInActiveWorkspace =
    activeWorkspace?.type === "organization" &&
    (activeWorkspace.my_role === "owner" || activeWorkspace.my_role === "admin");

  return (
    <main className="mx-auto flex min-h-screen max-w-4xl flex-col gap-6 px-6 py-12">
      <section
        aria-labelledby="workspaces-heading"
        className="rounded-lg border border-slate-200 bg-white p-8 shadow-sm"
      >
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              Milestone M3 — Workspace &amp; RBAC
            </p>
            <h1
              id="workspaces-heading"
              className="mt-1 text-2xl font-semibold tracking-tight text-slate-900"
            >
              Kelola &amp; Pilih Workspace Aktif
            </h1>
          </div>
          <div className="flex flex-wrap gap-2">
            <Link
              href="/documents"
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              Tinjau Dokumen
            </Link>
            <Link
              href="/"
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              Kembali ke Beranda
            </Link>
          </div>
        </div>

        <p className="mt-2 text-sm text-slate-600">
          Pemilihan workspace aktif berjalan di sisi klien dan selalu divalidasi ulang oleh FastAPI
          pada setiap permintaan.
        </p>

        {errorMessage ? (
          <div
            role="alert"
            className="mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
          >
            {errorMessage}
          </div>
        ) : null}

        {statusMessage ? (
          <div
            role="status"
            className="mt-4 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800"
          >
            {statusMessage}
          </div>
        ) : null}

        <div className="mt-6 flex flex-wrap items-end gap-3 border-t border-slate-100 pt-6">
          <div className="min-w-[240px] flex-1">
            <label htmlFor="workspace-access-token" className="block text-sm font-medium text-slate-700">
              Token Akses Bearer
            </label>
            <input
              id="workspace-access-token"
              type="password"
              value={accessToken}
              onChange={(event) => setAccessToken(event.target.value)}
              placeholder="Tempel access_token atau muat dari sesi cookie"
              className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:border-slate-900 focus:outline-none"
            />
          </div>
          <button
            type="button"
            disabled={loading}
            onClick={handleLoadFromSession}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-60"
          >
            Muat dari Sesi
          </button>
          <button
            type="button"
            disabled={loading || !accessToken.trim()}
            onClick={() => syncWorkspaces(accessToken.trim())}
            className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
          >
            Muat Workspace
          </button>
        </div>
      </section>

      <section className="grid gap-6 md:grid-cols-2">
        <div className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="text-lg font-semibold text-slate-900">Daftar Workspace</h2>
          {workspaces.length === 0 ? (
            <p className="mt-3 text-sm text-slate-500">
              Belum ada workspace dimuat. Gunakan sesi aktif atau masukkan token akses.
            </p>
          ) : (
            <ul className="mt-4 space-y-2">
              {workspaces.map((ws) => {
                const isActive = activeWorkspace?.id === ws.id;
                return (
                  <li key={ws.id}>
                    <button
                      type="button"
                      disabled={loading}
                      onClick={() => handleSelectWorkspace(ws.id)}
                      className={`flex w-full items-center justify-between rounded-md border px-3 py-2 text-left text-sm ${
                        isActive
                          ? "border-slate-900 bg-slate-900 text-white"
                          : "border-slate-200 bg-white text-slate-800 hover:bg-slate-50"
                      }`}
                    >
                      <span>
                        <span className="font-medium">{ws.name}</span>
                        <span className="ml-2 text-xs opacity-80">
                          ({ws.type === "personal" ? "Pribadi" : "Organisasi"} · {ws.my_role})
                        </span>
                      </span>
                      {isActive ? <span className="text-xs font-semibold">Aktif</span> : null}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          {activeWorkspace && creditWallet ? (
            <div className="mt-6 border-t border-slate-100 pt-4">
              <h3 className="text-sm font-semibold text-slate-900">
                Ringkasan Kredit ({activeWorkspace.name})
              </h3>
              <dl className="mt-2 grid grid-cols-3 gap-2 rounded-md border border-slate-200 bg-slate-50 p-3 text-center text-xs">
                <div>
                  <dt className="text-slate-500">Tersedia</dt>
                  <dd className="mt-0.5 font-mono text-sm font-semibold text-slate-900">
                    {creditWallet.available_balance}
                  </dd>
                </div>
                <div>
                  <dt className="text-slate-500">Dicadangkan</dt>
                  <dd className="mt-0.5 font-mono text-sm font-semibold text-slate-900">
                    {creditWallet.reserved_balance}
                  </dd>
                </div>
                <div>
                  <dt className="text-slate-500">Total</dt>
                  <dd className="mt-0.5 font-mono text-sm font-semibold text-slate-900">
                    {creditWallet.total_balance}
                  </dd>
                </div>
              </dl>
              {creditTransactions.length > 0 ? (
                <ul className="mt-3 space-y-1 text-xs text-slate-600">
                  {creditTransactions.map((tx) => (
                    <li key={tx.id} className="flex justify-between gap-2">
                      <span className="truncate">{tx.description}</span>
                      <span className="font-mono font-medium text-slate-800">
                        {tx.amount >= 0 ? `+${tx.amount}` : tx.amount} ({tx.transaction_type})
                      </span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}

          {activeWorkspace && members.length > 0 ? (
            <div className="mt-6 border-t border-slate-100 pt-4">
              <h3 className="text-sm font-semibold text-slate-900">
                Anggota ({activeWorkspace.name})
              </h3>
              <ul className="mt-2 space-y-1 text-sm text-slate-600">
                {members.map((member) => (
                  <li key={member.user_id} className="flex justify-between">
                    <span>{member.display_name}</span>
                    <span className="font-mono text-xs text-slate-500">{member.role}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>

        <div className="space-y-6">
          <form
            onSubmit={handleCreateOrganization}
            className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
          >
            <h2 className="text-lg font-semibold text-slate-900">Buat Workspace Organisasi</h2>
            <label htmlFor="org-workspace-name" className="mt-3 block text-sm font-medium text-slate-700">
              Nama Organisasi
            </label>
            <input
              id="org-workspace-name"
              type="text"
              required
              value={newOrgName}
              onChange={(event) => setNewOrgName(event.target.value)}
              className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900"
            />
            <button
              type="submit"
              disabled={loading}
              className="mt-4 w-full rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-60"
            >
              Buat Organisasi
            </button>
          </form>

          <form
            onSubmit={handleAcceptInvitation}
            className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
          >
            <h2 className="text-lg font-semibold text-slate-900">Terima Undangan Workspace</h2>
            <label htmlFor="invitation-token" className="mt-3 block text-sm font-medium text-slate-700">
              Token Undangan
            </label>
            <input
              id="invitation-token"
              type="text"
              required
              value={inviteToken}
              onChange={(event) => setInviteToken(event.target.value)}
              className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900"
            />
            <button
              type="submit"
              disabled={loading}
              className="mt-4 w-full rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50 disabled:opacity-60"
            >
              Gabung Workspace
            </button>
          </form>

          {canInviteInActiveWorkspace ? (
            <form
              onSubmit={handleInviteMember}
              className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
            >
              <h2 className="text-lg font-semibold text-slate-900">Undang Anggota Baru</h2>
              <label htmlFor="invite-member-email" className="mt-3 block text-sm font-medium text-slate-700">
                Email Anggota
              </label>
              <input
                id="invite-member-email"
                type="email"
                required
                value={inviteEmail}
                onChange={(event) => setInviteEmail(event.target.value)}
                className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900"
              />
              <label htmlFor="invite-member-role" className="mt-3 block text-sm font-medium text-slate-700">
                Peran
              </label>
              <select
                id="invite-member-role"
                value={inviteRole}
                onChange={(event) => setInviteRole(event.target.value as AssignableMemberRole)}
                className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900"
              >
                <option value="admin">Admin</option>
                <option value="editor">Editor</option>
                <option value="member">Member</option>
              </select>
              <button
                type="submit"
                disabled={loading}
                className="mt-4 w-full rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-60"
              >
                Kirim Undangan
              </button>
            </form>
          ) : null}
        </div>
      </section>
    </main>
  );
}

