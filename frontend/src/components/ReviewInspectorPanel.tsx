"use client";

import { useState } from "react";

import {
  DocumentSourceData,
  DocumentVersionData,
  MeetingMinutesDraftContent,
  normalizeDraftForEditor,
} from "@/lib/documents";

export type InspectorTab = "sources" | "versions";

interface ReviewInspectorPanelProps {
  activeTab: InspectorTab;
  onChangeTab: (tab: InspectorTab) => void;
  draft: MeetingMinutesDraftContent | null;
  sources: DocumentSourceData[];
  versions: DocumentVersionData[];
  currentVersionId: string | null;
  approvedVersionId: string | null;
  focusedFieldPath: string | null;
  highlightedSourceId: string | null;
  readOnly: boolean;
  isDocumentLocked: boolean;
  onClearFieldFilter: () => void;
  onLoadHistoricalVersion: (version: DocumentVersionData) => void;
}

const APPROVAL_LABELS: Record<string, { label: string; className: string }> = {
  pending: { label: "Menunggu", className: "bg-amber-100 text-amber-800" },
  approved: { label: "Disetujui", className: "bg-emerald-100 text-emerald-800" },
  superseded: { label: "Digantikan", className: "bg-slate-100 text-slate-600" },
  rejected: { label: "Ditolak", className: "bg-red-100 text-red-700" },
};

export function ReviewInspectorPanel({
  activeTab,
  onChangeTab,
  draft,
  sources,
  versions,
  currentVersionId,
  approvedVersionId,
  focusedFieldPath,
  highlightedSourceId,
  readOnly,
  isDocumentLocked,
  onClearFieldFilter,
  onLoadHistoricalVersion,
}: ReviewInspectorPanelProps) {
  const [expandedVersionId, setExpandedVersionId] = useState<string | null>(null);

  const provenanceList = draft?.provenance ?? [];
  const filteredProvenance = focusedFieldPath
    ? provenanceList.filter(
        (entry) =>
          entry.field_path === focusedFieldPath ||
          entry.field_path.startsWith(`${focusedFieldPath}[`) ||
          entry.field_path.startsWith(`${focusedFieldPath}.`),
      )
    : provenanceList;

  return (
    <aside className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex border-b border-slate-200" role="tablist" aria-label="Panel Inspeksi">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "sources"}
          onClick={() => onChangeTab("sources")}
          className={`border-b-2 px-3 py-2 text-sm font-medium ${
            activeTab === "sources"
              ? "border-slate-900 text-slate-900"
              : "border-transparent text-slate-500 hover:text-slate-700"
          }`}
        >
          Sumber &amp; Bukti ({sources.length})
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "versions"}
          onClick={() => onChangeTab("versions")}
          className={`border-b-2 px-3 py-2 text-sm font-medium ${
            activeTab === "versions"
              ? "border-slate-900 text-slate-900"
              : "border-transparent text-slate-500 hover:text-slate-700"
          }`}
        >
          Riwayat Versi ({versions.length})
        </button>
      </div>

      {activeTab === "sources" ? (
        <div className="mt-4 space-y-5">
          <div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold text-slate-900">Kutipan Provenance</h3>
              {focusedFieldPath ? (
                <button
                  type="button"
                  onClick={onClearFieldFilter}
                  className="rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-700 hover:bg-slate-50"
                >
                  Filter: {focusedFieldPath} ×
                </button>
              ) : null}
            </div>
            <p className="mt-1 text-xs text-slate-500">
              Menampilkan kutipan sumber (maks. 500 karakter) untuk bidang yang masih didukung
              sumber AI.
            </p>
            {filteredProvenance.length === 0 ? (
              <p className="mt-2 rounded border border-dashed border-slate-200 p-3 text-xs text-slate-500">
                {focusedFieldPath
                  ? `Tidak ada kutipan provenance tersimpan untuk bidang "${focusedFieldPath}".`
                  : "Belum ada kutipan provenance pada versi ini."}
              </p>
            ) : (
              <ul className="mt-2 space-y-2">
                {filteredProvenance.map((entry, idx) => (
                  <li
                    key={`${entry.field_path}-${entry.source_id}-${idx}`}
                    className="rounded-md border border-slate-200 bg-slate-50 p-2.5 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono font-semibold text-slate-800">
                        {entry.field_path}
                      </span>
                      <span className="text-[11px] text-slate-500">
                        {entry.source_name} ({entry.source_type})
                      </span>
                    </div>
                    <p className="mt-1 text-slate-700">&quot;{entry.excerpt}&quot;</p>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="border-t border-slate-100 pt-4">
            <h3 className="text-sm font-semibold text-slate-900">
              Pratinjau Sumber Dokumen (Maks. 500 Karakter)
            </h3>
            {sources.length === 0 ? (
              <p className="mt-2 text-xs text-slate-500">Belum ada sumber dokumen tercatat.</p>
            ) : (
              <ul className="mt-2 space-y-2.5">
                {sources.map((src) => {
                  const isHighlighted = highlightedSourceId === src.id;
                  return (
                    <li
                      key={src.id}
                      data-testid={`source-card-${src.id}`}
                      className={`rounded-md border p-3 text-xs ${
                        isHighlighted
                          ? "border-slate-900 bg-slate-50 ring-1 ring-slate-900"
                          : "border-slate-200 bg-white"
                      }`}
                    >
                      <div className="flex flex-wrap items-center justify-between gap-1">
                        <span className="font-semibold text-slate-900">{src.source_name}</span>
                        <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600">
                          {src.source_type}
                        </span>
                      </div>
                      {src.extracted_text_preview ? (
                        <pre className="mt-2 whitespace-pre-wrap break-words rounded bg-slate-50 p-2 font-mono text-[11px] text-slate-700">
                          {src.extracted_text_preview}
                        </pre>
                      ) : (
                        <p className="mt-1 text-[11px] italic text-slate-500">
                          Pratinjau teks tidak tersedia.
                        </p>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>
      ) : (
        <div className="mt-4 space-y-3">
          <p className="text-xs text-slate-500">
            Versi historis bersifat tetap (immutable). Memuat versi lama ke editor hanya menyalin ke
            formulir lokal dan baru membuat versi baru saat Anda klik &quot;Simpan Versi Baru&quot;.
          </p>
          {versions.length === 0 ? (
            <p className="rounded border border-dashed border-slate-200 p-3 text-xs text-slate-500">
              Belum ada riwayat versi tersimpan.
            </p>
          ) : (
            <ul className="space-y-2.5">
              {versions.map((ver) => {
                const isCurrent = ver.id === currentVersionId;
                const isApproved = ver.id === approvedVersionId;
                const badge = APPROVAL_LABELS[ver.approval_status] ?? {
                  label: ver.approval_status,
                  className: "bg-slate-100 text-slate-700",
                };
                const isExpanded = expandedVersionId === ver.id;
                const parsedVer = isExpanded
                  ? normalizeDraftForEditor(ver.structured_content)
                  : null;

                return (
                  <li
                    key={ver.id}
                    data-testid={`version-item-v${ver.version_number}`}
                    className={`rounded-md border p-3 text-xs ${
                      isCurrent ? "border-slate-900 bg-slate-50/70" : "border-slate-200 bg-white"
                    }`}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-semibold text-slate-900">
                          Versi #{ver.version_number}
                        </span>
                        {isCurrent ? (
                          <span className="rounded bg-slate-900 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                            Aktif
                          </span>
                        ) : null}
                        {isApproved ? (
                          <span className="rounded bg-emerald-700 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                            Disetujui Saat Ini
                          </span>
                        ) : null}
                        <span
                          className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${badge.className}`}
                        >
                          {badge.label}
                        </span>
                      </div>
                      <span className="text-[11px] text-slate-500">
                        {ver.created_by_user_id === null ? "Draf AI" : "Diedit Peninjau"}
                      </span>
                    </div>

                    <p className="mt-1 font-mono text-[11px] text-slate-500">
                      Hash: {ver.content_hash.slice(0, 12)}… ·{" "}
                      {new Date(ver.created_at).toLocaleString("id-ID")}
                    </p>
                    {ver.approved_at ? (
                      <p className="mt-0.5 text-[11px] text-emerald-700">
                        Pernah disetujui pada {new Date(ver.approved_at).toLocaleString("id-ID")}
                      </p>
                    ) : null}

                    <div className="mt-2 flex flex-wrap gap-2">
                      <button
                        type="button"
                        onClick={() =>
                          setExpandedVersionId(isExpanded ? null : ver.id)
                        }
                        className="rounded border border-slate-300 px-2 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-50"
                      >
                        {isExpanded ? "Tutup Ringkasan" : "Lihat Ringkasan"}
                      </button>
                      {!readOnly && !isDocumentLocked ? (
                        <button
                          type="button"
                          onClick={() => onLoadHistoricalVersion(ver)}
                          className="rounded border border-slate-400 bg-white px-2 py-1 text-[11px] font-medium text-slate-900 hover:bg-slate-50"
                        >
                          Muat ke Editor
                        </button>
                      ) : null}
                    </div>

                    {isExpanded && parsedVer ? (
                      <div className="mt-2.5 space-y-1 rounded border border-slate-200 bg-white p-2.5 text-[11px] text-slate-700">
                        <p>
                          <strong>Judul:</strong> {parsedVer.meeting_title ?? "(kosong)"}
                        </p>
                        <p>
                          <strong>Tanggal:</strong> {parsedVer.meeting_date ?? "(kosong)"}
                        </p>
                        <p>
                          <strong>Peserta ({parsedVer.attendees.length}):</strong>{" "}
                          {parsedVer.attendees.join(", ") || "(kosong)"}
                        </p>
                        <p>
                          <strong>Agenda ({parsedVer.agenda_items.length}):</strong>{" "}
                          {parsedVer.agenda_items.join("; ") || "(kosong)"}
                        </p>
                        <p>
                          <strong>Keputusan:</strong> {parsedVer.decisions.length} item ·{" "}
                          <strong>Tindak Lanjut:</strong> {parsedVer.action_items.length} item
                        </p>
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </aside>
  );
}
