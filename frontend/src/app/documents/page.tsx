"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  ConflictResolutionCard,
  ConflictResolutionMode,
} from "@/components/ConflictResolutionCard";
import { InspectorTab, ReviewInspectorPanel } from "@/components/ReviewInspectorPanel";
import { ApiClientError, fetchCurrentUser, refreshSession } from "@/lib/auth";
import {
  approveDocument,
  computeDraftDirtySignature,
  DocumentDetailData,
  DocumentSourceData,
  DocumentSummaryData,
  DocumentVersionData,
  evaluateClientReviewReadiness,
  getDocument,
  listDocumentSources,
  listDocumentVersions,
  listWorkspaceDocuments,
  MeetingMinutesDraftContent,
  normalizeDraftForEditor,
  ReviewFieldStatus,
  updateDocument,
} from "@/lib/documents";
import {
  listWorkspaces,
  resolveActiveWorkspace,
  setStoredActiveWorkspaceId,
  WorkspaceItem,
} from "@/lib/workspaces";

const DOC_STATUS_BADGES: Record<string, { label: string; className: string }> = {
  draft: { label: "Draf", className: "bg-slate-100 text-slate-700" },
  processing: { label: "Sedang Diproses AI", className: "bg-sky-100 text-sky-800" },
  review_required: { label: "Perlu Ditinjau", className: "bg-amber-100 text-amber-800" },
  approved: { label: "Telah Disetujui", className: "bg-emerald-100 text-emerald-800" },
  rendering: { label: "Merender DOCX", className: "bg-sky-100 text-sky-800" },
  completed: { label: "Selesai", className: "bg-emerald-100 text-emerald-800" },
  failed: { label: "Gagal", className: "bg-red-100 text-red-700" },
  archived: { label: "Diarsipkan", className: "bg-slate-100 text-slate-500" },
};

const FIELD_STATUS_BADGES: Record<ReviewFieldStatus, { label: string; className: string }> = {
  grounded: { label: "Didukung Sumber", className: "bg-sky-50 text-sky-800 border-sky-200" },
  user_provided: {
    label: "Diisi Peninjau",
    className: "bg-indigo-50 text-indigo-800 border-indigo-200",
  },
  missing: { label: "Belum Tersedia", className: "bg-slate-50 text-slate-600 border-slate-200" },
  conflicting: { label: "Konflik", className: "bg-amber-50 text-amber-800 border-amber-300" },
};

function computeCanEditDocument(
  workspace: WorkspaceItem | null,
  doc: DocumentDetailData | null,
  currentUserId: string | null,
): boolean {
  if (!workspace || !doc) {
    return false;
  }
  if (workspace.type === "personal") {
    return (
      workspace.my_role === "owner" &&
      (currentUserId === null || doc.created_by_user_id === currentUserId)
    );
  }
  if (workspace.my_role === "owner" || workspace.my_role === "admin") {
    return true;
  }
  if (workspace.my_role === "editor" || workspace.my_role === "member") {
    return Boolean(currentUserId && doc.created_by_user_id === currentUserId);
  }
  return false;
}

function getScalarFieldValue(draft: MeetingMinutesDraftContent, fieldPath: string): string {
  switch (fieldPath) {
    case "meeting_title":
      return draft.meeting_title ?? "";
    case "meeting_date":
      return draft.meeting_date ?? "";
    case "meeting_time":
      return draft.meeting_time ?? "";
    case "location":
      return draft.location ?? "";
    case "organizer":
      return draft.organizer ?? "";
    case "chairperson":
      return draft.chairperson ?? "";
    case "discussion_summary":
      return draft.discussion_summary ?? "";
    default:
      return "";
  }
}

function setScalarFieldValue(
  draft: MeetingMinutesDraftContent,
  fieldPath: string,
  value: string,
): MeetingMinutesDraftContent {
  const nextVal = value === "" ? null : value;
  switch (fieldPath) {
    case "meeting_title":
      return { ...draft, meeting_title: nextVal };
    case "meeting_date":
      return { ...draft, meeting_date: nextVal };
    case "meeting_time":
      return { ...draft, meeting_time: nextVal };
    case "location":
      return { ...draft, location: nextVal };
    case "organizer":
      return { ...draft, organizer: nextVal };
    case "chairperson":
      return { ...draft, chairperson: nextVal };
    case "discussion_summary":
      return { ...draft, discussion_summary: nextVal };
    default:
      return draft;
  }
}

function moveArrayItem<T>(items: T[], index: number, direction: -1 | 1): T[] {
  const target = index + direction;
  if (target < 0 || target >= items.length) {
    return items;
  }
  const copy = [...items];
  const [removed] = copy.splice(index, 1);
  copy.splice(target, 0, removed);
  return copy;
}

function FieldStatusPill({
  status,
  hasProvenance,
  onInspect,
}: {
  status: ReviewFieldStatus;
  hasProvenance?: boolean;
  onInspect?: () => void;
}) {
  const badge = FIELD_STATUS_BADGES[status];
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`rounded border px-1.5 py-0.5 text-[11px] font-medium ${badge.className}`}>
        {badge.label}
      </span>
      {hasProvenance && onInspect ? (
        <button
          type="button"
          onClick={onInspect}
          className="text-[11px] font-medium text-slate-600 underline hover:text-slate-900"
        >
          Lihat Bukti
        </button>
      ) : null}
    </span>
  );
}

export default function DocumentsPage() {
  const [accessToken, setAccessToken] = useState("");
  const [currentUserId, setCurrentUserId] = useState<string | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceItem[]>([]);
  const [activeWorkspace, setActiveWorkspace] = useState<WorkspaceItem | null>(null);
  const [documents, setDocuments] = useState<DocumentSummaryData[]>([]);
  const [selectedDoc, setSelectedDoc] = useState<DocumentDetailData | null>(null);
  const [sources, setSources] = useState<DocumentSourceData[]>([]);
  const [versions, setVersions] = useState<DocumentVersionData[]>([]);

  const [editorTitle, setEditorTitle] = useState("");
  const [editorDraft, setEditorDraft] = useState<MeetingMinutesDraftContent | null>(null);
  const [conflictResolutions, setConflictResolutions] = useState<
    Record<string, ConflictResolutionMode>
  >({});
  const [loadedHistoricalVersionNumber, setLoadedHistoricalVersionNumber] = useState<
    number | null
  >(null);
  const [pendingSwitchDocId, setPendingSwitchDocId] = useState<string | null>(null);

  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("sources");
  const [focusedFieldPath, setFocusedFieldPath] = useState<string | null>(null);
  const [highlightedSourceId, setHighlightedSourceId] = useState<string | null>(null);
  const [confirmApproveOpen, setConfirmApproveOpen] = useState(false);

  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const persistedVersion = selectedDoc?.current_version ?? null;
  const persistedDraft = persistedVersion
    ? normalizeDraftForEditor(persistedVersion.structured_content)
    : null;
  const resolvedConflictFields = Object.keys(conflictResolutions);

  const isDirty = Boolean(
    selectedDoc &&
      editorDraft &&
      computeDraftDirtySignature(editorDraft, resolvedConflictFields, editorTitle) !==
        computeDraftDirtySignature(persistedDraft, [], selectedDoc.title),
  );

  useEffect(() => {
    if (!isDirty) {
      return;
    }
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [isDirty]);

  function applyDocumentState(
    detail: DocumentDetailData,
    srcItems: DocumentSourceData[],
    verItems: DocumentVersionData[],
  ) {
    setSelectedDoc(detail);
    setSources(srcItems);
    setVersions(verItems);
    setEditorTitle(detail.title);
    setEditorDraft(
      detail.current_version
        ? normalizeDraftForEditor(detail.current_version.structured_content)
        : null,
    );
    setConflictResolutions({});
    setLoadedHistoricalVersionNumber(null);
    setConfirmApproveOpen(false);
    setPendingSwitchDocId(null);
  }

  async function loadDocumentBundle(token: string, documentId: string) {
    const [detail, srcRes, verRes] = await Promise.all([
      getDocument(token, documentId),
      listDocumentSources(token, documentId),
      listDocumentVersions(token, documentId),
    ]);
    applyDocumentState(detail, srcRes.items, verRes.items);
  }

  async function syncWorkspaceAndDocuments(
    token: string,
    preferredWorkspaceId?: string,
    preferredDocId?: string | null,
  ) {
    const [me, wsList] = await Promise.all([fetchCurrentUser(token), listWorkspaces(token)]);
    setCurrentUserId(me.id);
    setWorkspaces(wsList.items);
    const resolvedWs = resolveActiveWorkspace(wsList.items, preferredWorkspaceId);
    setActiveWorkspace(resolvedWs);
    if (!resolvedWs) {
      setDocuments([]);
      setSelectedDoc(null);
      setEditorDraft(null);
      return;
    }

    const docsRes = await listWorkspaceDocuments(token, resolvedWs.id, 1, 50);
    setDocuments(docsRes.items);
    const queryDocId =
      preferredDocId ??
      (typeof window !== "undefined"
        ? new URLSearchParams(window.location.search).get("id")
        : null);
    const targetDocId =
      queryDocId && docsRes.items.some((d) => d.id === queryDocId)
        ? queryDocId
        : docsRes.items[0]?.id ?? null;

    if (targetDocId) {
      await loadDocumentBundle(token, targetDocId);
    } else {
      setSelectedDoc(null);
      setSources([]);
      setVersions([]);
      setEditorDraft(null);
    }
  }

  async function handleLoadFromSession() {
    setErrorMessage(null);
    setStatusMessage(null);
    setLoading(true);
    try {
      const session = await refreshSession();
      setAccessToken(session.access_token);
      setCurrentUserId(session.user.id);
      await syncWorkspaceAndDocuments(session.access_token);
      setStatusMessage("Sesi dan daftar dokumen workspace berhasil dimuat.");
    } catch (error) {
      setErrorMessage(
        error instanceof ApiClientError ? error.message : "Gagal memuat sesi pengguna.",
      );
    } finally {
      setLoading(false);
    }
  }

  async function handleLoadWithToken() {
    if (!accessToken.trim()) {
      setErrorMessage("Masukkan token akses atau muat dari sesi terlebih dahulu.");
      return;
    }
    setErrorMessage(null);
    setStatusMessage(null);
    setLoading(true);
    try {
      await syncWorkspaceAndDocuments(accessToken.trim());
      setStatusMessage("Daftar dokumen workspace berhasil dimuat.");
    } catch (error) {
      setErrorMessage(
        error instanceof ApiClientError ? error.message : "Gagal memuat dokumen workspace.",
      );
    } finally {
      setLoading(false);
    }
  }

  async function handleSwitchWorkspace(workspaceId: string) {
    if (!accessToken.trim()) {
      return;
    }
    setErrorMessage(null);
    setStatusMessage(null);
    setLoading(true);
    try {
      setStoredActiveWorkspaceId(workspaceId);
      await syncWorkspaceAndDocuments(accessToken.trim(), workspaceId);
    } catch (error) {
      setErrorMessage(
        error instanceof ApiClientError ? error.message : "Gagal mengganti workspace aktif.",
      );
    } finally {
      setLoading(false);
    }
  }

  async function handleSelectDocument(documentId: string, force = false) {
    if (!accessToken.trim()) {
      return;
    }
    if (isDirty && !force && selectedDoc?.id !== documentId) {
      setPendingSwitchDocId(documentId);
      return;
    }
    setErrorMessage(null);
    setStatusMessage(null);
    setLoading(true);
    try {
      await loadDocumentBundle(accessToken.trim(), documentId);
    } catch (error) {
      setErrorMessage(
        error instanceof ApiClientError ? error.message : "Gagal memuat detail dokumen.",
      );
    } finally {
      setLoading(false);
    }
  }

  function handleResetChanges() {
    if (!selectedDoc) {
      return;
    }
    setEditorTitle(selectedDoc.title);
    setEditorDraft(
      selectedDoc.current_version
        ? normalizeDraftForEditor(selectedDoc.current_version.structured_content)
        : null,
    );
    setConflictResolutions({});
    setLoadedHistoricalVersionNumber(null);
    setConfirmApproveOpen(false);
    setPendingSwitchDocId(null);
    setStatusMessage("Perubahan lokal dikembalikan ke versi aktif tersimpan.");
  }

  async function handleSaveNewVersion() {
    if (!accessToken.trim() || !selectedDoc || !editorDraft || !persistedVersion) {
      return;
    }
    setErrorMessage(null);
    setStatusMessage(null);
    setSaving(true);
    try {
      const updated = await updateDocument(accessToken.trim(), selectedDoc.id, {
        title: editorTitle.trim() || selectedDoc.title,
        structured_content: editorDraft as unknown as Record<string, unknown>,
        expected_version_id: persistedVersion.id,
        expected_content_hash: persistedVersion.content_hash,
        resolved_conflict_fields: resolvedConflictFields,
      });
      const [srcRes, verRes] = await Promise.all([
        listDocumentSources(accessToken.trim(), selectedDoc.id),
        listDocumentVersions(accessToken.trim(), selectedDoc.id),
      ]);
      applyDocumentState(updated, srcRes.items, verRes.items);
      setDocuments((prev) =>
        prev.map((item) =>
          item.id === updated.id
            ? {
                ...item,
                title: updated.title,
                status: updated.status,
                current_version_id: updated.current_version_id,
                approved_version_id: updated.approved_version_id,
                updated_at: updated.updated_at,
              }
            : item,
        ),
      );
      setStatusMessage(
        `Berhasil menyimpan Versi #${updated.current_version?.version_number ?? "-"}.`,
      );
    } catch (error) {
      setErrorMessage(
        error instanceof ApiClientError ? error.message : "Gagal menyimpan versi baru dokumen.",
      );
    } finally {
      setSaving(false);
    }
  }

  async function handleApproveDocument() {
    if (!accessToken.trim() || !selectedDoc || !persistedVersion) {
      return;
    }
    setErrorMessage(null);
    setStatusMessage(null);
    setSaving(true);
    try {
      const approved = await approveDocument(accessToken.trim(), selectedDoc.id, {
        version_id: persistedVersion.id,
        content_hash: persistedVersion.content_hash,
      });
      const [srcRes, verRes] = await Promise.all([
        listDocumentSources(accessToken.trim(), selectedDoc.id),
        listDocumentVersions(accessToken.trim(), selectedDoc.id),
      ]);
      applyDocumentState(approved, srcRes.items, verRes.items);
      setDocuments((prev) =>
        prev.map((item) =>
          item.id === approved.id
            ? {
                ...item,
                status: approved.status,
                approved_version_id: approved.approved_version_id,
                updated_at: approved.updated_at,
              }
            : item,
        ),
      );
      setStatusMessage(
        `Dokumen Versi #${approved.current_version?.version_number ?? "-"} telah disetujui.`,
      );
    } catch (error) {
      setErrorMessage(
        error instanceof ApiClientError ? error.message : "Gagal menyetujui dokumen.",
      );
    } finally {
      setSaving(false);
    }
  }

  function handleLoadHistoricalVersion(version: DocumentVersionData) {
    setEditorDraft(normalizeDraftForEditor(version.structured_content));
    setConflictResolutions({});
    setLoadedHistoricalVersionNumber(version.version_number);
    setConfirmApproveOpen(false);
    setStatusMessage(
      `Versi #${version.version_number} dimuat ke editor lokal. Klik "Simpan Versi Baru" untuk menyimpannya sebagai versi terbaru.`,
    );
  }

  function handleScalarInput(fieldPath: string, value: string) {
    if (!editorDraft) {
      return;
    }
    const nextDraft = setScalarFieldValue(editorDraft, fieldPath, value);
    setEditorDraft(nextDraft);
    const hasConflict = nextDraft.conflicts.some((c) => c.field_path === fieldPath);
    if (hasConflict) {
      setConflictResolutions((prev) => {
        const next = { ...prev };
        if (value.trim()) {
          next[fieldPath] = "manual";
        } else {
          delete next[fieldPath];
        }
        return next;
      });
    }
  }

  function handleSelectConflictCandidate(fieldPath: string, value: string) {
    if (!editorDraft) {
      return;
    }
    setEditorDraft(setScalarFieldValue(editorDraft, fieldPath, value));
    setConflictResolutions((prev) => ({ ...prev, [fieldPath]: "candidate" }));
  }

  function handleConfirmCurrentConflict(fieldPath: string) {
    setConflictResolutions((prev) => ({ ...prev, [fieldPath]: "confirmed_current" }));
  }

  function handleUndoConflictResolution(fieldPath: string) {
    setConflictResolutions((prev) => {
      const next = { ...prev };
      delete next[fieldPath];
      return next;
    });
  }

  function handleInspectField(fieldPath: string, sourceId?: string | null) {
    setFocusedFieldPath(fieldPath);
    if (sourceId) {
      setHighlightedSourceId(sourceId);
    }
    setInspectorTab("sources");
  }

  const canEditAndApprove = computeCanEditDocument(activeWorkspace, selectedDoc, currentUserId);
  const isDocumentLocked = Boolean(
    selectedDoc &&
      (selectedDoc.status === "processing" ||
        selectedDoc.status === "rendering" ||
        selectedDoc.status === "archived"),
  );
  const readOnly = !canEditAndApprove || isDocumentLocked;

  const liveReadiness =
    isDirty || !selectedDoc?.review_readiness
      ? evaluateClientReviewReadiness(editorDraft, resolvedConflictFields)
      : selectedDoc.review_readiness;

  const canApprove = Boolean(
    canEditAndApprove &&
      selectedDoc &&
      selectedDoc.status === "review_required" &&
      persistedVersion &&
      !isDirty &&
      liveReadiness.is_ready_for_approval &&
      !saving,
  );

  function getDisplayFieldStatus(fieldPath: string): ReviewFieldStatus {
    if (!editorDraft) {
      return "missing";
    }
    if (editorDraft.conflicts.some((c) => c.field_path === fieldPath)) {
      return conflictResolutions[fieldPath] ? "user_provided" : "conflicting";
    }
    const persistedStatus = editorDraft.field_statuses[fieldPath];
    if (persistedDraft) {
      const curVal = getScalarFieldValue(editorDraft, fieldPath).trim();
      const prevVal = getScalarFieldValue(persistedDraft, fieldPath).trim();
      if (curVal !== prevVal) {
        return curVal ? "user_provided" : "missing";
      }
    }
    if (persistedStatus) {
      return persistedStatus;
    }
    return getScalarFieldValue(editorDraft, fieldPath).trim() ? "user_provided" : "missing";
  }

  function getListFieldStatus(
    fieldPath: "attendees" | "absentees" | "agenda_items",
    items: string[],
  ): ReviewFieldStatus {
    if (!editorDraft) {
      return "missing";
    }
    if (items.filter((x) => x.trim()).length === 0) {
      return "missing";
    }
    if (persistedDraft) {
      const prev = persistedDraft[fieldPath];
      if (JSON.stringify(items) !== JSON.stringify(prev)) {
        return "user_provided";
      }
    }
    return editorDraft.field_statuses[fieldPath] ?? "user_provided";
  }

  const docBadge = selectedDoc ? DOC_STATUS_BADGES[selectedDoc.status] : null;

  return (
    <main className="mx-auto flex min-h-screen max-w-7xl flex-col gap-5 px-4 py-8 sm:px-6">
      <header className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              Milestone M7 — Tinjauan &amp; Persetujuan Dokumen
            </p>
            <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-900">
              Tinjauan Notulen Rapat
            </h1>
          </div>
          <div className="flex flex-wrap gap-2">
            <Link
              href="/workspaces"
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              Kelola Workspace
            </Link>
            <Link
              href="/"
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              Beranda
            </Link>
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-end gap-3 border-t border-slate-100 pt-4">
          <div className="min-w-[220px] flex-1">
            <label htmlFor="documents-access-token" className="block text-xs font-medium text-slate-700">
              Token Akses Bearer
            </label>
            <input
              id="documents-access-token"
              type="password"
              value={accessToken}
              onChange={(event) => setAccessToken(event.target.value)}
              placeholder="Muat dari sesi cookie atau tempel access_token"
              className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-900"
            />
          </div>
          <button
            type="button"
            disabled={loading}
            onClick={handleLoadFromSession}
            className="rounded-md bg-slate-900 px-3.5 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-60"
          >
            Muat dari Sesi
          </button>
          <button
            type="button"
            disabled={loading || !accessToken.trim()}
            onClick={handleLoadWithToken}
            className="rounded-md border border-slate-300 px-3.5 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
          >
            Muat Dokumen
          </button>

          {workspaces.length > 0 ? (
            <div className="min-w-[220px]">
              <label htmlFor="active-workspace-select" className="block text-xs font-medium text-slate-700">
                Workspace Aktif
              </label>
              <select
                id="active-workspace-select"
                value={activeWorkspace?.id ?? ""}
                onChange={(event) => handleSwitchWorkspace(event.target.value)}
                className="mt-1 block w-full rounded-md border border-slate-300 px-2.5 py-1.5 text-sm text-slate-900"
              >
                {workspaces.map((ws) => (
                  <option key={ws.id} value={ws.id}>
                    {ws.name} ({ws.type === "personal" ? "Pribadi" : ws.my_role})
                  </option>
                ))}
              </select>
            </div>
          ) : null}
        </div>

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
      </header>

      {documents.length > 0 ? (
        <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-slate-900">
              Dokumen Workspace ({documents.length})
            </h2>
            {activeWorkspace ? (
              <span className="text-xs text-slate-500">
                Peran Anda: <strong className="font-mono">{activeWorkspace.my_role}</strong>
              </span>
            ) : null}
          </div>
          <div className="mt-2.5 flex flex-wrap gap-2">
            {documents.map((doc) => {
              const isSelected = selectedDoc?.id === doc.id;
              const badge = DOC_STATUS_BADGES[doc.status] ?? {
                label: doc.status,
                className: "bg-slate-100 text-slate-700",
              };
              return (
                <button
                  key={doc.id}
                  type="button"
                  onClick={() => handleSelectDocument(doc.id)}
                  className={`flex items-center gap-2 rounded-md border px-3 py-1.5 text-left text-xs ${
                    isSelected
                      ? "border-slate-900 bg-slate-900 text-white"
                      : "border-slate-200 bg-white text-slate-800 hover:bg-slate-50"
                  }`}
                >
                  <span className="font-medium">{doc.title}</span>
                  <span
                    className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                      isSelected ? "bg-white/20 text-white" : badge.className
                    }`}
                  >
                    {badge.label}
                  </span>
                </button>
              );
            })}
          </div>
          {pendingSwitchDocId ? (
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              <span>
                Anda memiliki perubahan yang belum disimpan. Pindah dokumen akan membuang perubahan
                lokal tersebut.
              </span>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setPendingSwitchDocId(null)}
                  className="rounded border border-slate-300 bg-white px-2.5 py-1 font-medium text-slate-700"
                >
                  Tetap di Dokumen Ini
                </button>
                <button
                  type="button"
                  onClick={() => handleSelectDocument(pendingSwitchDocId, true)}
                  className="rounded bg-amber-900 px-2.5 py-1 font-medium text-white"
                >
                  Buang Perubahan &amp; Pindah
                </button>
              </div>
            </div>
          ) : null}
        </section>
      ) : null}

      {selectedDoc ? (
        <>
          <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-xl font-semibold text-slate-900">{selectedDoc.title}</h2>
                  {docBadge ? (
                    <span
                      data-testid="document-status-badge"
                      className={`rounded px-2 py-0.5 text-xs font-semibold ${docBadge.className}`}
                    >
                      {docBadge.label}
                    </span>
                  ) : null}
                  {persistedVersion ? (
                    <span className="rounded bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-700">
                      Versi #{persistedVersion.version_number}
                    </span>
                  ) : null}
                  {isDirty ? (
                    <span
                      data-testid="unsaved-indicator"
                      className="rounded border border-amber-300 bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-900"
                    >
                      Perubahan belum disimpan
                    </span>
                  ) : null}
                </div>
                {loadedHistoricalVersionNumber !== null ? (
                  <p className="text-xs font-medium text-indigo-800">
                    Memuat pratinjau dari Versi #{loadedHistoricalVersionNumber} ke editor lokal.
                    Klik &quot;Simpan Versi Baru&quot; untuk menjadikannya versi terbaru.
                  </p>
                ) : null}
                {selectedDoc.status === "approved" && persistedVersion?.approved_at ? (
                  <p className="text-xs text-emerald-800">
                    Disetujui pada {new Date(persistedVersion.approved_at).toLocaleString("id-ID")}{" "}
                    (Hash: <span className="font-mono">{persistedVersion.content_hash.slice(0, 12)}…</span>)
                  </p>
                ) : null}
              </div>

              <div className="flex flex-wrap items-center gap-2">
                {!readOnly ? (
                  <>
                    <button
                      type="button"
                      disabled={!isDirty || saving}
                      onClick={handleResetChanges}
                      className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                    >
                      Reset Perubahan
                    </button>
                    <button
                      type="button"
                      disabled={!isDirty || saving || !editorDraft}
                      onClick={handleSaveNewVersion}
                      className="rounded-md bg-slate-900 px-3.5 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
                    >
                      {saving ? "Menyimpan..." : "Simpan Versi Baru"}
                    </button>
                    <button
                      type="button"
                      disabled={!canApprove}
                      onClick={() => setConfirmApproveOpen(true)}
                      className="rounded-md bg-emerald-700 px-3.5 py-1.5 text-sm font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
                    >
                      Setujui Dokumen
                    </button>
                  </>
                ) : null}
              </div>
            </div>

            {confirmApproveOpen && persistedVersion ? (
              <div
                data-testid="approve-confirmation-panel"
                className="mt-4 rounded-md border border-emerald-300 bg-emerald-50 p-4 text-sm text-emerald-950"
              >
                <p className="font-semibold">Konfirmasi Persetujuan Dokumen</p>
                <p className="mt-1 text-xs">
                  Anda akan menyetujui <strong>Versi #{persistedVersion.version_number}</strong>{" "}
                  (Hash konten:{" "}
                  <code className="font-mono">{persistedVersion.content_hash}</code>). Pastikan
                  seluruh data rapat, peserta, keputusan, dan tindak lanjut telah diperiksa.
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    disabled={saving}
                    onClick={handleApproveDocument}
                    className="rounded-md bg-emerald-700 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-emerald-600 disabled:opacity-60"
                  >
                    Konfirmasi &amp; Setujui Dokumen
                  </button>
                  <button
                    type="button"
                    disabled={saving}
                    onClick={() => setConfirmApproveOpen(false)}
                    className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700"
                  >
                    Batal
                  </button>
                </div>
              </div>
            ) : null}

            <div className="mt-4 space-y-2">
              <div className="rounded-md border border-slate-200 bg-slate-50 px-3.5 py-2.5 text-xs text-slate-700">
                <strong>Perhatian Peninjau:</strong> Status &quot;Didukung Sumber&quot; hanya
                menandakan adanya kutipan teks pada dokumen sumber, bukan jaminan kebenaran faktual.
                Anda wajib memeriksa dan memvalidasi seluruh isi sebelum menyetujui dokumen.
              </div>

              {!canEditAndApprove ? (
                <div
                  data-testid="readonly-banner"
                  className="rounded-md border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs text-amber-900"
                >
                  <strong>Mode Baca-Saja:</strong> Anda tidak memiliki izin menyunting atau
                  menyetujui dokumen milik anggota lain ini. Anda tetap dapat meninjau isi draf,
                  kutipan sumber, dan riwayat versi.
                </div>
              ) : null}

              {canEditAndApprove &&
              (selectedDoc.status === "approved" || selectedDoc.status === "completed") ? (
                <div className="rounded-md border border-indigo-200 bg-indigo-50 px-3.5 py-2.5 text-xs text-indigo-900">
                  Dokumen ini sudah berstatus{" "}
                  <strong>{selectedDoc.status === "completed" ? "Selesai" : "Disetujui"}</strong>{" "}
                  (Versi #{persistedVersion?.version_number ?? "-"}). Menyimpan perubahan baru akan
                  menggantikan persetujuan sebelumnya dan mengembalikan dokumen ke status{" "}
                  <strong>Perlu Ditinjau</strong>.
                </div>
              ) : null}

              {liveReadiness.blocking_issues.length > 0 ? (
                <div
                  data-testid="blocking-issues-box"
                  className="rounded-md border border-red-200 bg-red-50 px-3.5 py-2.5 text-xs text-red-800"
                >
                  <p className="font-semibold">
                    Prasyarat Persetujuan Belum Terpenuhi ({liveReadiness.blocking_issues.length}):
                  </p>
                  <ul className="mt-1 list-disc pl-4">
                    {liveReadiness.blocking_issues.map((issue, idx) => (
                      <li key={idx}>{issue}</li>
                    ))}
                  </ul>
                </div>
              ) : (
                <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3.5 py-2 text-xs text-emerald-800">
                  Seluruh prasyarat wajib persetujuan telah terpenuhi.
                  {isDirty ? ' Klik "Simpan Versi Baru" terlebih dahulu sebelum menyetujui.' : ""}
                </div>
              )}

              {liveReadiness.optional_warnings.length > 0 ? (
                <div className="rounded-md border border-slate-200 bg-slate-50 px-3.5 py-2 text-xs text-slate-600">
                  <p className="font-medium text-slate-800">Catatan &amp; Bidang Opsional:</p>
                  <ul className="mt-1 list-disc pl-4">
                    {liveReadiness.optional_warnings.map((warning, idx) => (
                      <li key={idx}>{warning}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          </section>

          {editorDraft ? (
            <div className="grid gap-6 lg:grid-cols-12">
              <div className="space-y-6 lg:col-span-7">
                <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
                  <h3 className="text-base font-semibold text-slate-900">1. Informasi Rapat</h3>
                  <div className="mt-4 space-y-4">
                    <div>
                      <label htmlFor="doc-container-title" className="block text-xs font-medium text-slate-700">
                        Judul Kontainer Dokumen
                      </label>
                      <input
                        id="doc-container-title"
                        type="text"
                        disabled={readOnly}
                        value={editorTitle}
                        onChange={(event) => setEditorTitle(event.target.value)}
                        className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-900 disabled:bg-slate-50"
                      />
                    </div>

                    {(
                      [
                        { key: "meeting_title", label: "Judul Rapat", required: true },
                        { key: "meeting_date", label: "Tanggal Rapat", required: true },
                        { key: "meeting_time", label: "Waktu Rapat", required: false },
                        { key: "location", label: "Lokasi", required: false },
                        { key: "organizer", label: "Penyelenggara", required: false },
                        { key: "chairperson", label: "Pimpinan Rapat", required: false },
                      ] as const
                    ).map(({ key, label, required }) => {
                      const val = getScalarFieldValue(editorDraft, key);
                      const status = getDisplayFieldStatus(key);
                      const conflict = editorDraft.conflicts.find((c) => c.field_path === key);
                      const hasProv = editorDraft.provenance.some((p) => p.field_path === key);
                      return (
                        <div key={key}>
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <label
                              htmlFor={`field-${key}`}
                              className="text-xs font-medium text-slate-700"
                            >
                              {label}{" "}
                              <span className="text-slate-400">
                                ({required ? "Wajib" : "Opsional"})
                              </span>
                            </label>
                            <FieldStatusPill
                              status={status}
                              hasProvenance={hasProv}
                              onInspect={() => handleInspectField(key)}
                            />
                          </div>
                          <input
                            id={`field-${key}`}
                            type="text"
                            disabled={readOnly}
                            value={val}
                            onChange={(event) => handleScalarInput(key, event.target.value)}
                            className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-900 disabled:bg-slate-50"
                          />
                          {conflict ? (
                            <ConflictResolutionCard
                              conflict={conflict}
                              currentValue={val}
                              provenance={editorDraft.provenance}
                              sources={sources}
                              resolutionMode={conflictResolutions[key]}
                              readOnly={readOnly}
                              onSelectCandidate={handleSelectConflictCandidate}
                              onConfirmCurrent={handleConfirmCurrentConflict}
                              onUndoResolution={handleUndoConflictResolution}
                              onInspectSource={(sourceId, fp) => handleInspectField(fp, sourceId)}
                            />
                          ) : null}
                        </div>
                      );
                    })}
                  </div>
                </section>

                <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
                  <h3 className="text-base font-semibold text-slate-900">
                    2. Kehadiran &amp; Agenda Rapat
                  </h3>
                  <div className="mt-4 space-y-5">
                    {(
                      [
                        { key: "attendees", label: "Daftar Hadir", required: true },
                        { key: "absentees", label: "Tidak Hadir", required: false },
                        { key: "agenda_items", label: "Agenda Rapat", required: true },
                      ] as const
                    ).map(({ key, label, required }) => {
                      const list = editorDraft[key];
                      const status = getListFieldStatus(key, list);
                      const hasProv = editorDraft.provenance.some((p) => p.field_path === key);
                      return (
                        <div key={key} className="border-b border-slate-100 pb-4 last:border-b-0">
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <span className="text-xs font-medium text-slate-700">
                              {label}{" "}
                              <span className="text-slate-400">
                                ({required ? "Wajib min. 1" : "Opsional"})
                              </span>
                            </span>
                            <FieldStatusPill
                              status={status}
                              hasProvenance={hasProv}
                              onInspect={() => handleInspectField(key)}
                            />
                          </div>
                          <ul className="mt-2 space-y-2">
                            {list.map((entry, idx) => (
                              <li key={`${key}-${idx}`} className="flex items-center gap-2">
                                <input
                                  type="text"
                                  aria-label={`${label} ${idx + 1}`}
                                  disabled={readOnly}
                                  value={entry}
                                  onChange={(event) => {
                                    const updated = [...list];
                                    updated[idx] = event.target.value;
                                    setEditorDraft({ ...editorDraft, [key]: updated });
                                  }}
                                  className="block w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-900 disabled:bg-slate-50"
                                />
                                {!readOnly ? (
                                  <button
                                    type="button"
                                    onClick={() => {
                                      const updated = list.filter((_, i) => i !== idx);
                                      setEditorDraft({ ...editorDraft, [key]: updated });
                                    }}
                                    className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                                  >
                                    Hapus
                                  </button>
                                ) : null}
                              </li>
                            ))}
                          </ul>
                          {!readOnly ? (
                            <button
                              type="button"
                              onClick={() =>
                                setEditorDraft({ ...editorDraft, [key]: [...list, ""] })
                              }
                              className="mt-2 rounded border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                            >
                              + Tambah {label}
                            </button>
                          ) : null}
                        </div>
                      );
                    })}
                  </div>
                </section>

                <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h3 className="text-base font-semibold text-slate-900">
                      3. Ringkasan Pembahasan
                    </h3>
                    <FieldStatusPill
                      status={getDisplayFieldStatus("discussion_summary")}
                      hasProvenance={editorDraft.provenance.some(
                        (p) => p.field_path === "discussion_summary",
                      )}
                      onInspect={() => handleInspectField("discussion_summary")}
                    />
                  </div>
                  <textarea
                    aria-label="Ringkasan Pembahasan"
                    rows={4}
                    disabled={readOnly}
                    value={editorDraft.discussion_summary ?? ""}
                    onChange={(event) =>
                      handleScalarInput("discussion_summary", event.target.value)
                    }
                    className="mt-3 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 disabled:bg-slate-50"
                  />
                </section>

                <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
                  <h3 className="text-base font-semibold text-slate-900">
                    4. Pemisahan Keputusan, Usulan, dan Opini
                  </h3>
                  <p className="mt-1 text-xs text-slate-500">
                    Keputusan resmi dipisahkan secara tegas dari usulan maupun opini peserta rapat.
                  </p>

                  {/* Decisions */}
                  <div className="mt-4 border-t border-slate-100 pt-4">
                    <div className="flex items-center justify-between">
                      <h4 className="text-sm font-semibold text-slate-900">
                        Keputusan Rapat (Opsional jika tidak ada ketetapan formal)
                      </h4>
                      {!readOnly ? (
                        <button
                          type="button"
                          onClick={() =>
                            setEditorDraft({
                              ...editorDraft,
                              decisions: [
                                ...editorDraft.decisions,
                                {
                                  decision: "",
                                  origin: "user_provided",
                                  source_id: null,
                                  excerpt: null,
                                },
                              ],
                            })
                          }
                          className="rounded border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                        >
                          + Tambah Keputusan
                        </button>
                      ) : null}
                    </div>
                    <ul className="mt-2 space-y-2.5">
                      {editorDraft.decisions.map((item, idx) => (
                        <li
                          key={`dec-${idx}`}
                          className="rounded-md border border-slate-200 bg-slate-50/50 p-3"
                        >
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <FieldStatusPill
                              status={item.origin === "grounded" ? "grounded" : "user_provided"}
                              hasProvenance={Boolean(item.source_id && item.excerpt)}
                              onInspect={() => handleInspectField("decisions", item.source_id)}
                            />
                            {!readOnly ? (
                              <div className="flex gap-1">
                                <button
                                  type="button"
                                  disabled={idx === 0}
                                  onClick={() =>
                                    setEditorDraft({
                                      ...editorDraft,
                                      decisions: moveArrayItem(editorDraft.decisions, idx, -1),
                                    })
                                  }
                                  className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] disabled:opacity-40"
                                >
                                  Naik
                                </button>
                                <button
                                  type="button"
                                  disabled={idx === editorDraft.decisions.length - 1}
                                  onClick={() =>
                                    setEditorDraft({
                                      ...editorDraft,
                                      decisions: moveArrayItem(editorDraft.decisions, idx, 1),
                                    })
                                  }
                                  className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] disabled:opacity-40"
                                >
                                  Turun
                                </button>
                                <button
                                  type="button"
                                  onClick={() =>
                                    setEditorDraft({
                                      ...editorDraft,
                                      decisions: editorDraft.decisions.filter((_, i) => i !== idx),
                                    })
                                  }
                                  className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] text-red-700"
                                >
                                  Hapus
                                </button>
                              </div>
                            ) : null}
                          </div>
                          <input
                            type="text"
                            aria-label={`Keputusan ${idx + 1}`}
                            disabled={readOnly}
                            value={item.decision}
                            onChange={(event) => {
                              const next = [...editorDraft.decisions];
                              next[idx] = {
                                decision: event.target.value,
                                origin: "user_provided",
                                source_id: null,
                                excerpt: null,
                              };
                              setEditorDraft({ ...editorDraft, decisions: next });
                            }}
                            className="mt-2 block w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-900 disabled:bg-slate-50"
                          />
                          {item.excerpt ? (
                            <p className="mt-1.5 text-[11px] text-slate-500">
                              Kutipan: &quot;{item.excerpt}&quot;
                            </p>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  </div>

                  {/* Proposals */}
                  <div className="mt-4 border-t border-slate-100 pt-4">
                    <div className="flex items-center justify-between">
                      <h4 className="text-sm font-semibold text-slate-900">Usulan / Opsi</h4>
                      {!readOnly ? (
                        <button
                          type="button"
                          onClick={() =>
                            setEditorDraft({
                              ...editorDraft,
                              proposals: [
                                ...editorDraft.proposals,
                                {
                                  proposal: "",
                                  proposer: null,
                                  status: "proposed",
                                  origin: "user_provided",
                                  source_id: null,
                                  excerpt: null,
                                },
                              ],
                            })
                          }
                          className="rounded border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                        >
                          + Tambah Usulan
                        </button>
                      ) : null}
                    </div>
                    <ul className="mt-2 space-y-2.5">
                      {editorDraft.proposals.map((item, idx) => (
                        <li
                          key={`prop-${idx}`}
                          className="rounded-md border border-slate-200 bg-slate-50/50 p-3"
                        >
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <FieldStatusPill
                              status={item.origin === "grounded" ? "grounded" : "user_provided"}
                              hasProvenance={Boolean(item.source_id && item.excerpt)}
                              onInspect={() => handleInspectField("proposals", item.source_id)}
                            />
                            {!readOnly ? (
                              <button
                                type="button"
                                onClick={() =>
                                  setEditorDraft({
                                    ...editorDraft,
                                    proposals: editorDraft.proposals.filter((_, i) => i !== idx),
                                  })
                                }
                                className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] text-red-700"
                              >
                                Hapus
                              </button>
                            ) : null}
                          </div>
                          <div className="mt-2 grid gap-2 sm:grid-cols-3">
                            <input
                              type="text"
                              aria-label={`Usulan ${idx + 1}`}
                              disabled={readOnly}
                              value={item.proposal}
                              onChange={(event) => {
                                const next = [...editorDraft.proposals];
                                next[idx] = {
                                  ...item,
                                  proposal: event.target.value,
                                  origin: "user_provided",
                                  source_id: null,
                                  excerpt: null,
                                };
                                setEditorDraft({ ...editorDraft, proposals: next });
                              }}
                              className="rounded border border-slate-300 bg-white px-2.5 py-1.5 text-sm sm:col-span-2 disabled:bg-slate-50"
                            />
                            <div className="flex gap-2">
                              <input
                                type="text"
                                aria-label={`Pengusul ${idx + 1}`}
                                placeholder="Pengusul"
                                disabled={readOnly}
                                value={item.proposer ?? ""}
                                onChange={(event) => {
                                  const next = [...editorDraft.proposals];
                                  next[idx] = {
                                    ...item,
                                    proposer: event.target.value || null,
                                    origin: "user_provided",
                                    source_id: null,
                                    excerpt: null,
                                  };
                                  setEditorDraft({ ...editorDraft, proposals: next });
                                }}
                                className="w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-xs disabled:bg-slate-50"
                              />
                              <select
                                aria-label={`Status Usulan ${idx + 1}`}
                                disabled={readOnly}
                                value={item.status}
                                onChange={(event) => {
                                  const next = [...editorDraft.proposals];
                                  next[idx] = {
                                    ...item,
                                    status:
                                      event.target.value === "deferred" ? "deferred" : "proposed",
                                    origin: "user_provided",
                                    source_id: null,
                                    excerpt: null,
                                  };
                                  setEditorDraft({ ...editorDraft, proposals: next });
                                }}
                                className="rounded border border-slate-300 bg-white px-2 py-1.5 text-xs disabled:bg-slate-50"
                              >
                                <option value="proposed">Diusulkan</option>
                                <option value="deferred">Ditunda</option>
                              </select>
                            </div>
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>

                  {/* Opinions */}
                  <div className="mt-4 border-t border-slate-100 pt-4">
                    <div className="flex items-center justify-between">
                      <h4 className="text-sm font-semibold text-slate-900">Opini / Pandangan</h4>
                      {!readOnly ? (
                        <button
                          type="button"
                          onClick={() =>
                            setEditorDraft({
                              ...editorDraft,
                              opinions: [
                                ...editorDraft.opinions,
                                {
                                  statement: "",
                                  speaker: null,
                                  origin: "user_provided",
                                  source_id: null,
                                  excerpt: null,
                                },
                              ],
                            })
                          }
                          className="rounded border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                        >
                          + Tambah Opini
                        </button>
                      ) : null}
                    </div>
                    <ul className="mt-2 space-y-2.5">
                      {editorDraft.opinions.map((item, idx) => (
                        <li
                          key={`op-${idx}`}
                          className="rounded-md border border-slate-200 bg-slate-50/50 p-3"
                        >
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <FieldStatusPill
                              status={item.origin === "grounded" ? "grounded" : "user_provided"}
                              hasProvenance={Boolean(item.source_id && item.excerpt)}
                              onInspect={() => handleInspectField("opinions", item.source_id)}
                            />
                            {!readOnly ? (
                              <button
                                type="button"
                                onClick={() =>
                                  setEditorDraft({
                                    ...editorDraft,
                                    opinions: editorDraft.opinions.filter((_, i) => i !== idx),
                                  })
                                }
                                className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] text-red-700"
                              >
                                Hapus
                              </button>
                            ) : null}
                          </div>
                          <div className="mt-2 grid gap-2 sm:grid-cols-3">
                            <input
                              type="text"
                              aria-label={`Opini ${idx + 1}`}
                              disabled={readOnly}
                              value={item.statement}
                              onChange={(event) => {
                                const next = [...editorDraft.opinions];
                                next[idx] = {
                                  ...item,
                                  statement: event.target.value,
                                  origin: "user_provided",
                                  source_id: null,
                                  excerpt: null,
                                };
                                setEditorDraft({ ...editorDraft, opinions: next });
                              }}
                              className="rounded border border-slate-300 bg-white px-2.5 py-1.5 text-sm sm:col-span-2 disabled:bg-slate-50"
                            />
                            <input
                              type="text"
                              aria-label={`Pembicara Opini ${idx + 1}`}
                              placeholder="Pembicara"
                              disabled={readOnly}
                              value={item.speaker ?? ""}
                              onChange={(event) => {
                                const next = [...editorDraft.opinions];
                                next[idx] = {
                                  ...item,
                                  speaker: event.target.value || null,
                                  origin: "user_provided",
                                  source_id: null,
                                  excerpt: null,
                                };
                                setEditorDraft({ ...editorDraft, opinions: next });
                              }}
                              className="rounded border border-slate-300 bg-white px-2 py-1.5 text-xs disabled:bg-slate-50"
                            />
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                </section>

                <section className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
                  <div className="flex items-center justify-between">
                    <h3 className="text-base font-semibold text-slate-900">5. Tindak Lanjut</h3>
                    {!readOnly ? (
                      <button
                        type="button"
                        onClick={() =>
                          setEditorDraft({
                            ...editorDraft,
                            action_items: [
                              ...editorDraft.action_items,
                              {
                                task: "",
                                assignee: null,
                                due_date: null,
                                origin: "user_provided",
                                source_id: null,
                                excerpt: null,
                              },
                            ],
                          })
                        }
                        className="rounded border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                      >
                        + Tambah Tindak Lanjut
                      </button>
                    ) : null}
                  </div>
                  <ul className="mt-3 space-y-2.5">
                    {editorDraft.action_items.map((item, idx) => (
                      <li
                        key={`act-${idx}`}
                        className="rounded-md border border-slate-200 bg-slate-50/50 p-3"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <FieldStatusPill
                            status={item.origin === "grounded" ? "grounded" : "user_provided"}
                            hasProvenance={Boolean(item.source_id && item.excerpt)}
                            onInspect={() => handleInspectField("action_items", item.source_id)}
                          />
                          {!readOnly ? (
                            <button
                              type="button"
                              onClick={() =>
                                setEditorDraft({
                                  ...editorDraft,
                                  action_items: editorDraft.action_items.filter((_, i) => i !== idx),
                                })
                              }
                              className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] text-red-700"
                            >
                              Hapus
                            </button>
                          ) : null}
                        </div>
                        <div className="mt-2 grid gap-2 sm:grid-cols-4">
                          <input
                            type="text"
                            aria-label={`Tugas Tindak Lanjut ${idx + 1}`}
                            disabled={readOnly}
                            value={item.task}
                            onChange={(event) => {
                              const next = [...editorDraft.action_items];
                              next[idx] = {
                                ...item,
                                task: event.target.value,
                                origin: "user_provided",
                                source_id: null,
                                excerpt: null,
                              };
                              setEditorDraft({ ...editorDraft, action_items: next });
                            }}
                            className="rounded border border-slate-300 bg-white px-2.5 py-1.5 text-sm sm:col-span-2 disabled:bg-slate-50"
                          />
                          <input
                            type="text"
                            aria-label={`PIC Tindak Lanjut ${idx + 1}`}
                            placeholder="PIC (opsional)"
                            disabled={readOnly}
                            value={item.assignee ?? ""}
                            onChange={(event) => {
                              const next = [...editorDraft.action_items];
                              next[idx] = {
                                ...item,
                                assignee: event.target.value || null,
                                origin: "user_provided",
                                source_id: null,
                                excerpt: null,
                              };
                              setEditorDraft({ ...editorDraft, action_items: next });
                            }}
                            className="rounded border border-slate-300 bg-white px-2 py-1.5 text-xs disabled:bg-slate-50"
                          />
                          <input
                            type="text"
                            aria-label={`Tenggat Tindak Lanjut ${idx + 1}`}
                            placeholder="Tenggat (opsional)"
                            disabled={readOnly}
                            value={item.due_date ?? ""}
                            onChange={(event) => {
                              const next = [...editorDraft.action_items];
                              next[idx] = {
                                ...item,
                                due_date: event.target.value || null,
                                origin: "user_provided",
                                source_id: null,
                                excerpt: null,
                              };
                              setEditorDraft({ ...editorDraft, action_items: next });
                            }}
                            className="rounded border border-slate-300 bg-white px-2 py-1.5 text-xs disabled:bg-slate-50"
                          />
                        </div>
                      </li>
                    ))}
                  </ul>
                </section>
              </div>

              <div className="lg:col-span-5">
                <ReviewInspectorPanel
                  activeTab={inspectorTab}
                  onChangeTab={setInspectorTab}
                  draft={editorDraft}
                  sources={sources}
                  versions={versions}
                  currentVersionId={selectedDoc.current_version_id}
                  approvedVersionId={selectedDoc.approved_version_id}
                  focusedFieldPath={focusedFieldPath}
                  highlightedSourceId={highlightedSourceId}
                  readOnly={!canEditAndApprove}
                  isDocumentLocked={isDocumentLocked}
                  onClearFieldFilter={() => {
                    setFocusedFieldPath(null);
                    setHighlightedSourceId(null);
                  }}
                  onLoadHistoricalVersion={handleLoadHistoricalVersion}
                />
              </div>
            </div>
          ) : (
            <section className="rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-600 shadow-sm">
              Dokumen ini belum memiliki versi draf terstruktur. Jalankan pemrosesan AI terlebih
              dahulu agar draf Notulen Rapat siap ditinjau.
            </section>
          )}
        </>
      ) : (
        <section className="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-600 shadow-sm">
          Muat sesi atau token akses untuk melihat dan meninjau dokumen di workspace Anda.
        </section>
      )}
    </main>
  );
}
