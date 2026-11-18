"use client";

import { ConflictItem, DocumentSourceData, MeetingMinutesDraftContent } from "@/lib/documents";

export type ConflictResolutionMode = "candidate" | "manual" | "confirmed_current";

interface ConflictResolutionCardProps {
  conflict: ConflictItem;
  currentValue: string;
  provenance: MeetingMinutesDraftContent["provenance"];
  sources: DocumentSourceData[];
  resolutionMode: ConflictResolutionMode | undefined;
  readOnly: boolean;
  onSelectCandidate: (fieldPath: string, value: string) => void;
  onConfirmCurrent: (fieldPath: string) => void;
  onUndoResolution: (fieldPath: string) => void;
  onInspectSource: (sourceId: string, fieldPath: string) => void;
}

const SOURCE_TYPE_LABELS: Record<string, string> = {
  form: "Formulir",
  text: "Teks",
  upload: "Unggahan",
  workspace_reference: "Referensi",
};

export function ConflictResolutionCard({
  conflict,
  currentValue,
  provenance,
  sources,
  resolutionMode,
  readOnly,
  onSelectCandidate,
  onConfirmCurrent,
  onUndoResolution,
  onInspectSource,
}: ConflictResolutionCardProps) {
  const isResolved = resolutionMode !== undefined;
  const fieldProvenance = provenance.filter((entry) => entry.field_path === conflict.field_path);
  const sourcesById = new Map(sources.map((src) => [src.id, src]));

  function findContextForCandidate(candidateValue: string, index: number): {
    sourceId: string | null;
    sourceName: string;
    sourceTypeLabel: string;
    excerpt: string | null;
  } {
    const normalizedCandidate = candidateValue.trim().toLowerCase();
    const matchedProv =
      fieldProvenance.find((entry) =>
        entry.excerpt.toLowerCase().includes(normalizedCandidate),
      ) ?? fieldProvenance[index];

    if (matchedProv) {
      return {
        sourceId: matchedProv.source_id,
        sourceName: matchedProv.source_name,
        sourceTypeLabel: SOURCE_TYPE_LABELS[matchedProv.source_type] ?? matchedProv.source_type,
        excerpt: matchedProv.excerpt,
      };
    }

    const fallbackSourceId = conflict.source_ids[index] ?? conflict.source_ids[0] ?? null;
    const matchedSource = fallbackSourceId ? sourcesById.get(fallbackSourceId) : undefined;
    if (matchedSource) {
      return {
        sourceId: matchedSource.id,
        sourceName: matchedSource.source_name,
        sourceTypeLabel: SOURCE_TYPE_LABELS[matchedSource.source_type] ?? matchedSource.source_type,
        excerpt: matchedSource.extracted_text_preview,
      };
    }

    return {
      sourceId: fallbackSourceId,
      sourceName: "Sumber Dokumen",
      sourceTypeLabel: "Sumber",
      excerpt: null,
    };
  }

  return (
    <div
      data-testid={`conflict-card-${conflict.field_path}`}
      className={`mt-2 rounded-md border p-3 text-xs ${
        isResolved
          ? "border-emerald-200 bg-emerald-50/60 text-slate-800"
          : "border-amber-300 bg-amber-50 text-slate-900"
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold">
          {isResolved
            ? "Konflik ditandai selesai (menunggu simpan)"
            : "Konflik terdeteksi antar sumber"}
        </span>
        {isResolved && !readOnly ? (
          <button
            type="button"
            onClick={() => onUndoResolution(conflict.field_path)}
            className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-700 hover:bg-slate-50"
          >
            Urungkan Resolusi Konflik
          </button>
        ) : null}
      </div>

      <p className="mt-1 text-slate-700">{conflict.description}</p>
      <p className="mt-1 text-[11px] font-medium text-slate-600">
        Kandidat Tercatat di Sumber (Perlu Verifikasi Peninjau) — pilih kandidat, ubah nilai secara
        manual di kolom atas, atau konfirmasi nilai saat ini:
      </p>

      {isResolved ? (
        <p className="mt-1.5 rounded bg-white/80 px-2 py-1 text-[11px] font-medium text-emerald-900">
          {resolutionMode === "candidate"
            ? `Konflik diselesaikan: Memilih kandidat sumber ("${currentValue}") — akan disimpan sebagai Diisi Peninjau.`
            : resolutionMode === "manual"
              ? "Konflik diselesaikan: Nilai diedit manual oleh peninjau — akan disimpan sebagai Diisi Peninjau."
              : `Konflik diselesaikan: Nilai saat ini dikonfirmasi peninjau ("${currentValue}") — akan disimpan sebagai Diisi Peninjau.`}
        </p>
      ) : null}

      <ul className="mt-2 space-y-2">
        {conflict.values.map((candidateValue, idx) => {
          const ctx = findContextForCandidate(candidateValue, idx);
          return (
            <li
              key={`${conflict.field_path}-cand-${idx}`}
              className="rounded border border-slate-200 bg-white p-2.5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <span className="font-semibold text-slate-900">{candidateValue}</span>
                  <span className="ml-2 text-[11px] text-slate-500">
                    ({ctx.sourceName} · {ctx.sourceTypeLabel})
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {ctx.sourceId ? (
                    <button
                      type="button"
                      onClick={() => onInspectSource(ctx.sourceId as string, conflict.field_path)}
                      className="rounded border border-slate-300 px-2 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-50"
                    >
                      Lihat di Panel Sumber
                    </button>
                  ) : null}
                  {!readOnly ? (
                    <button
                      type="button"
                      onClick={() => onSelectCandidate(conflict.field_path, candidateValue)}
                      className="rounded bg-slate-900 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-slate-800"
                    >
                      Gunakan Kandidat Ini (&quot;{candidateValue}&quot;)
                    </button>
                  ) : null}
                </div>
              </div>
              {ctx.excerpt ? (
                <p className="mt-1.5 rounded bg-slate-50 px-2 py-1 font-mono text-[11px] text-slate-600">
                  Kutipan sumber (maks. 500 karakter): &quot;{ctx.excerpt}&quot;
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>

      {!readOnly && currentValue.trim() ? (
        <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2 border-t border-amber-200/70 pt-2">
          <span className="text-[11px] text-slate-600">
            Nilai saat ini di kolom: <strong className="text-slate-900">{currentValue}</strong>
          </span>
          <button
            type="button"
            onClick={() => onConfirmCurrent(conflict.field_path)}
            className="rounded border border-slate-400 bg-white px-2.5 py-1 text-[11px] font-medium text-slate-800 hover:bg-slate-50"
          >
            Konfirmasi Nilai Saat Ini (&quot;{currentValue}&quot;)
          </button>
        </div>
      ) : null}
    </div>
  );
}
