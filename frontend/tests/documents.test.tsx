import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createDocumentSourceUploadIntent,
  createWorkspaceDocument,
  deleteDocument,
  getDocument,
  getJobStatus,
  getWorkspaceUsage,
  getWorkspaceUsageSummary,
  listDocumentSources,
  listDocumentVersions,
  listWorkspaceDocuments,
  MeetingMinutesDraftContent,
  processDocument,
  submitDocumentSource,
  updateDocument,
} from "@/lib/documents";

function jsonOk(data: unknown, status = 200): Response {
  return new Response(JSON.stringify({ success: true, data }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Frontend M5 documents, sources, and jobs client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("creates, lists, updates, and soft-deletes workspace documents", async () => {
    const docSummary = {
      id: "doc-1",
      workspace_id: "ws-1",
      created_by_user_id: "user-1",
      document_type: "meeting_minutes",
      title: "Notulen Rapat Mingguan",
      status: "draft",
      current_version_id: null,
      selected_template_version_id: null,
      approved_version_id: null,
      created_at: "2026-11-18T00:00:00Z",
      updated_at: "2026-11-18T00:00:00Z",
    };
    const docDetail = {
      ...docSummary,
      current_version: null,
      sources_count: 0,
      jobs: [],
    };
    const updatedDetail = {
      ...docDetail,
      title: "Notulen Rapat Q4",
      current_version_id: "ver-1",
      current_version: {
        id: "ver-1",
        document_id: "doc-1",
        version_number: 1,
        structured_content: { ringkasan: "Draf awal" },
        content_hash: "abc123",
        created_by_user_id: "user-1",
        created_at: "2026-11-18T00:01:00Z",
        approval_status: "pending",
        approved_by_user_id: null,
        approved_at: null,
      },
    };

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonOk(docDetail, 201))
      .mockResolvedValueOnce(
        jsonOk({ items: [docSummary], total: 1, page: 1, page_size: 20 }),
      )
      .mockResolvedValueOnce(jsonOk(updatedDetail))
      .mockResolvedValueOnce(jsonOk(updatedDetail))
      .mockResolvedValueOnce(
        jsonOk({ id: "doc-1", status: "archived", deleted_at: "2026-11-18T00:02:00Z" }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const created = await createWorkspaceDocument("tok-1", "ws-1", {
      title: "Notulen Rapat Mingguan",
    });
    expect(created.status).toBe("draft");

    const listed = await listWorkspaceDocuments("tok-1", "ws-1", 1, 20);
    expect(listed.items).toHaveLength(1);
    expect(listed.items[0].title).toBe("Notulen Rapat Mingguan");

    const loaded = await getDocument("tok-1", "doc-1");
    expect(loaded.title).toBe("Notulen Rapat Q4");

    const patched = await updateDocument("tok-1", "doc-1", {
      title: "Notulen Rapat Q4",
      structured_content: { ringkasan: "Draf awal" },
    });
    expect(patched.current_version?.version_number).toBe(1);

    const archived = await deleteDocument("tok-1", "doc-1");
    expect(archived.status).toBe("archived");
  });

  it("handles upload intents, form/text/upload sources, processing, versions, and jobs", async () => {
    const intent = {
      object_key: "workspaces/ws-1/documents/doc-1/sources/rand/notes.docx",
      upload_url: "http://localhost:3000/storage/presigned-upload?key=...",
      method: "PUT",
      required_headers: {
        "Content-Type":
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      },
      expires_at: "2026-11-18T00:05:00Z",
      max_file_size_bytes: 10485760,
    };
    const formSource = {
      id: "src-1",
      workspace_id: "ws-1",
      document_id: "doc-1",
      document_version_id: null,
      source_bundle_id: null,
      source_type: "form",
      has_stored_object: false,
      source_name: "Rapat Evaluasi",
      content_hash: "h1",
      validation_status: "valid",
      metadata: { form_data: { meeting_title: "Rapat Evaluasi" } },
      extracted_text_preview: "Judul Rapat: Rapat Evaluasi",
      created_by_user_id: "user-1",
      created_at: "2026-11-18T00:00:00Z",
    };
    const uploadSource = {
      ...formSource,
      id: "src-2",
      source_type: "upload",
      has_stored_object: true,
      source_name: "notes.docx",
      content_hash: "h2",
    };
    const job = {
      id: "job-1",
      workspace_id: "ws-1",
      document_id: "doc-1",
      job_type: "ai_generation",
      status: "queued",
      source_bundle_id: "bundle-1",
      workflow_key: "meeting_minutes_v1",
      workflow_version: "v1",
      workflow_provider: "n8n",
      attempt_count: 0,
      started_at: null,
      completed_at: null,
      error_code: null,
      safe_error_message: null,
      created_at: "2026-11-18T00:01:00Z",
      updated_at: "2026-11-18T00:01:00Z",
    };
    const processRes = {
      document: {
        id: "doc-1",
        workspace_id: "ws-1",
        created_by_user_id: "user-1",
        document_type: "meeting_minutes",
        title: "Notulen Rapat Mingguan",
        status: "processing",
        current_version_id: null,
        selected_template_version_id: null,
        approved_version_id: null,
        created_at: "2026-11-18T00:00:00Z",
        updated_at: "2026-11-18T00:01:00Z",
      },
      source_bundle_id: "bundle-1",
      jobs: [job],
      reservation_id: null,
      reserved_credits: 0,
    };

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonOk(intent, 201))
      .mockResolvedValueOnce(jsonOk(formSource, 201))
      .mockResolvedValueOnce(jsonOk(uploadSource, 201))
      .mockResolvedValueOnce(jsonOk({ items: [formSource, uploadSource], total: 2 }))
      .mockResolvedValueOnce(jsonOk(processRes, 202))
      .mockResolvedValueOnce(jsonOk(job))
      .mockResolvedValueOnce(jsonOk({ items: [], total: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    const createdIntent = await createDocumentSourceUploadIntent("tok-1", "ws-1", "doc-1", {
      filename: "notes.docx",
      content_type:
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      size_bytes: 2048,
    });
    expect(createdIntent.object_key).toContain("workspaces/ws-1/documents/doc-1/sources/");

    const addedForm = await submitDocumentSource("tok-1", "ws-1", "doc-1", {
      source_type: "form",
      form_data: { meeting_title: "Rapat Evaluasi", attendees: ["Sakti", "Budi"] },
    });
    expect(addedForm.source_type).toBe("form");

    const confirmedUpload = await submitDocumentSource("tok-1", "ws-1", "doc-1", {
      source_type: "upload",
      object_key: createdIntent.object_key,
      filename: "notes.docx",
      content_type:
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    });
    expect(confirmedUpload.has_stored_object).toBe(true);

    const sources = await listDocumentSources("tok-1", "doc-1");
    expect(sources.total).toBe(2);

    const started = await processDocument("tok-1", "doc-1", { idempotency_key: "run-1" });
    expect(started.document.status).toBe("processing");
    expect(started.jobs[0].status).toBe("queued");

    const polled = await getJobStatus("tok-1", "job-1");
    expect(polled.job_type).toBe("ai_generation");

    const versions = await listDocumentVersions("tok-1", "doc-1");
    expect(versions.total).toBe(0);
  });

  it("fetches workspace AI usage history and summary alongside meeting_minutes draft types", async () => {
    const sampleDraft: MeetingMinutesDraftContent = {
      meeting_title: "Rapat M6",
      meeting_date: "2026-11-18",
      meeting_time: null,
      location: "Ruang Rapat 1",
      organizer: null,
      chairperson: null,
      attendees: ["Sakti"],
      absentees: [],
      agenda_items: ["Evaluasi M6"],
      discussion_summary: null,
      opinions: [{ statement: "Perlu uji tambahan", speaker: "Budi", source_id: "src-1", excerpt: "Opini: ..." }],
      proposals: [{ proposal: "Tambah tes", proposer: "Budi", status: "proposed", source_id: "src-1", excerpt: "Usulan: ..." }],
      decisions: [{ decision: "Lanjut rilis", source_id: "src-1", excerpt: "Keputusan: ..." }],
      action_items: [{ task: "Kirim laporan", assignee: "Sakti", due_date: "2026-11-20", source_id: "src-1", excerpt: "Tindak lanjut: ..." }],
      field_statuses: { meeting_title: "grounded", meeting_time: "missing" },
      conflicts: [],
      provenance: [],
      warnings: [],
    };
    expect(sampleDraft.decisions).toHaveLength(1);

    const usageList = {
      items: [
        {
          id: "u-1",
          workspace_id: "ws-1",
          user_id: "user-1",
          document_id: "doc-1",
          job_id: "job-1",
          provider: "gemini",
          model: "gemini-2.5-flash",
          workflow_key: "meeting_minutes_v1",
          input_tokens: 120,
          output_tokens: 80,
          estimated_cost_minor_units: 0,
          duration_ms: 420,
          status: "completed" as const,
          created_at: "2026-11-18T00:00:00Z",
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    };
    const usageSummary = {
      workspace_id: "ws-1",
      total_events: 1,
      completed_events: 1,
      failed_events: 0,
      total_input_tokens: 120,
      total_output_tokens: 80,
      total_estimated_cost_minor_units: 0,
      total_duration_ms: 420,
    };

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonOk(usageList))
      .mockResolvedValueOnce(jsonOk(usageSummary));
    vi.stubGlobal("fetch", fetchMock);

    const listRes = await getWorkspaceUsage("tok-1", "ws-1", 1, 20);
    expect(listRes.items[0].workflow_key).toBe("meeting_minutes_v1");

    const sumRes = await getWorkspaceUsageSummary("tok-1", "ws-1");
    expect(sumRes.completed_events).toBe(1);
    expect(sumRes.total_input_tokens).toBe(120);
  });
});

