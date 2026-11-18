import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import DocumentsPage from "@/app/documents/page";
import {
  approveDocument,
  createDocumentSourceUploadIntent,
  createWorkspaceDocument,
  deleteDocument,
  evaluateClientReviewReadiness,
  getDocument,
  getJobStatus,
  getWorkspaceUsage,
  getWorkspaceUsageSummary,
  listDocumentSources,
  listDocumentVersions,
  listWorkspaceDocuments,
  MeetingMinutesDraftContent,
  normalizeDraftForEditor,
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

  it("evaluates client review readiness, normalizes M6 drafts, and calls approveDocument", async () => {
    const rawM6 = {
      meeting_title: "Rapat M7",
      meeting_date: "2026-11-18",
      attendees: ["Sakti"],
      agenda_items: ["Review"],
      discussion_summary: "Diskusi selesai.",
      decisions: [{ decision: "Lanjut", source_id: "src-1", excerpt: "Keputusan: Lanjut" }],
      field_statuses: { meeting_title: "grounded", meeting_date: "conflicting" },
      conflicts: [
        {
          field_path: "meeting_date",
          values: ["2026-11-18", "2026-11-20"],
          source_ids: ["src-1", "src-2"],
          description: "Tanggal berbeda",
        },
      ],
      provenance: [],
      warnings: [],
    };
    const normalized = normalizeDraftForEditor(rawM6);
    expect(normalized.decisions[0].origin).toBe("grounded");

    const blocked = evaluateClientReviewReadiness(normalized, []);
    expect(blocked.is_ready_for_approval).toBe(false);
    expect(blocked.unresolved_conflict_fields).toEqual(["meeting_date"]);

    const resolved = evaluateClientReviewReadiness(
      { ...normalized, decisions: [] },
      ["meeting_date"],
    );
    expect(resolved.is_ready_for_approval).toBe(true);
    expect(resolved.optional_warnings).toContain(
      "Tidak ada keputusan formal tercatat pada rapat ini (opsional).",
    );

    const approvedDoc = {
      id: "doc-1",
      workspace_id: "ws-1",
      created_by_user_id: "user-1",
      document_type: "meeting_minutes",
      title: "Notulen",
      status: "approved",
      current_version_id: "ver-2",
      selected_template_version_id: null,
      approved_version_id: "ver-2",
      created_at: "2026-11-18T00:00:00Z",
      updated_at: "2026-11-18T00:02:00Z",
      current_version: null,
      sources_count: 1,
      jobs: [],
      review_readiness: resolved,
    };
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonOk(approvedDoc));
    vi.stubGlobal("fetch", fetchMock);

    const res = await approveDocument("tok-1", "doc-1", {
      version_id: "ver-2",
      content_hash: "a".repeat(64),
    });
    expect(res.status).toBe("approved");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/documents/doc-1/approve",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ version_id: "ver-2", content_hash: "a".repeat(64) }),
      }),
    );
  });

  it("supports M7 review UX: conflict resolution, dirty tracking, historical load, and approval", async () => {
    const hashV1 = "1".repeat(64);
    const hashV2 = "2".repeat(64);
    const hashV3 = "3".repeat(64);

    const v1Content: MeetingMinutesDraftContent = {
      meeting_title: "Rapat M7",
      meeting_date: "2026-11-18",
      meeting_time: null,
      location: "Ruang Lama",
      organizer: null,
      chairperson: null,
      attendees: ["Sakti"],
      absentees: [],
      agenda_items: ["Review"],
      discussion_summary: "Ringkasan awal",
      opinions: [],
      proposals: [],
      decisions: [
        { decision: "Gunakan skema M7", origin: "grounded", source_id: "src-1", excerpt: "Keputusan: Gunakan skema M7" },
      ],
      action_items: [],
      field_statuses: {
        meeting_title: "grounded",
        meeting_date: "conflicting",
        location: "grounded",
        attendees: "grounded",
        agenda_items: "grounded",
        decisions: "grounded",
      },
      conflicts: [
        {
          field_path: "meeting_date",
          values: ["2026-11-18", "2026-11-20"],
          source_ids: ["src-1", "src-2"],
          description: "Tanggal rapat berbeda antar sumber.",
        },
      ],
      provenance: [
        {
          field_path: "meeting_date",
          source_id: "src-1",
          source_name: "Formulir",
          source_type: "form",
          excerpt: "Tanggal: 2026-11-18",
        },
        {
          field_path: "meeting_date",
          source_id: "src-2",
          source_name: "Catatan",
          source_type: "text",
          excerpt: "Tanggal: 2026-11-20",
        },
      ],
      warnings: ["Konflik terdeteksi pada bidang 'meeting_date'."],
    };

    const v2Content: MeetingMinutesDraftContent = {
      ...v1Content,
      location: "Aula Utama",
      field_statuses: { ...v1Content.field_statuses, location: "user_provided" },
    };

    const v3Content: MeetingMinutesDraftContent = {
      ...v2Content,
      meeting_date: "2026-11-20",
      field_statuses: { ...v2Content.field_statuses, meeting_date: "user_provided" },
      conflicts: [],
      warnings: [],
    };

    const v1 = {
      id: "ver-1",
      document_id: "doc-1",
      version_number: 1,
      structured_content: v1Content as unknown as Record<string, unknown>,
      content_hash: hashV1,
      created_by_user_id: null,
      created_at: "2026-11-18T00:01:00Z",
      approval_status: "superseded",
      approved_by_user_id: null,
      approved_at: null,
    };
    const v2 = {
      ...v1,
      id: "ver-2",
      version_number: 2,
      structured_content: v2Content as unknown as Record<string, unknown>,
      content_hash: hashV2,
      created_by_user_id: "user-1",
      approval_status: "pending",
    };
    const v3 = {
      ...v2,
      id: "ver-3",
      version_number: 3,
      structured_content: v3Content as unknown as Record<string, unknown>,
      content_hash: hashV3,
    };
    const v3Approved = {
      ...v3,
      approval_status: "approved",
      approved_by_user_id: "user-1",
      approved_at: "2026-11-18T00:05:00Z",
    };

    const docBase = {
      id: "doc-1",
      workspace_id: "ws-1",
      created_by_user_id: "user-1",
      document_type: "meeting_minutes",
      title: "Notulen M7",
      status: "review_required",
      current_version_id: "ver-2",
      selected_template_version_id: null,
      approved_version_id: null,
      created_at: "2026-11-18T00:00:00Z",
      updated_at: "2026-11-18T00:02:00Z",
    };
    const docV2 = {
      ...docBase,
      current_version: v2,
      sources_count: 2,
      jobs: [],
      review_readiness: evaluateClientReviewReadiness(v2Content, []),
    };
    const docV3 = {
      ...docBase,
      current_version_id: "ver-3",
      current_version: v3,
      sources_count: 2,
      jobs: [],
      review_readiness: evaluateClientReviewReadiness(v3Content, []),
    };
    const docV3Approved = {
      ...docV3,
      status: "approved",
      approved_version_id: "ver-3",
      current_version: v3Approved,
    };
    const sourcesList = {
      items: [
        {
          id: "src-1",
          workspace_id: "ws-1",
          document_id: "doc-1",
          document_version_id: null,
          source_bundle_id: null,
          source_type: "form",
          has_stored_object: false,
          source_name: "Formulir",
          content_hash: "h1",
          validation_status: "valid",
          metadata: {},
          extracted_text_preview: "Judul Rapat: Rapat M7\nTanggal: 2026-11-18",
          created_by_user_id: "user-1",
          created_at: "2026-11-18T00:00:00Z",
        },
      ],
      total: 1,
    };

    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/auth/me")) {
        return Promise.resolve(
          jsonOk({
            id: "user-1",
            email: "u1@example.com",
            display_name: "Sakti",
            email_verified: true,
            status: "active",
            created_at: "2026-11-18T00:00:00Z",
          }),
        );
      }
      if (url.includes("/workspaces?") && method === "GET") {
        return Promise.resolve(
          jsonOk({
            items: [
              {
                id: "ws-1",
                name: "Org OSIS",
                type: "organization",
                owner_user_id: "user-1",
                status: "active",
                my_role: "member",
                created_at: "2026-11-18T00:00:00Z",
                updated_at: "2026-11-18T00:00:00Z",
              },
            ],
            page: 1,
            page_size: 20,
            total: 1,
          }),
        );
      }
      if (url.includes("/workspaces/ws-1/documents?")) {
        return Promise.resolve(jsonOk({ items: [docBase], total: 1, page: 1, page_size: 50 }));
      }
      if (url.endsWith("/documents/doc-1") && method === "GET") {
        return Promise.resolve(jsonOk(docV2));
      }
      if (url.endsWith("/documents/doc-1/sources")) {
        return Promise.resolve(jsonOk(sourcesList));
      }
      if (url.endsWith("/documents/doc-1/versions")) {
        return Promise.resolve(jsonOk({ items: [v2, v1], total: 2 }));
      }
      if (url.endsWith("/documents/doc-1") && method === "PATCH") {
        return Promise.resolve(jsonOk(docV3));
      }
      if (url.endsWith("/documents/doc-1/approve") && method === "POST") {
        return Promise.resolve(jsonOk(docV3Approved));
      }
      return Promise.reject(new Error(`Unexpected fetch ${method} ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DocumentsPage />);
    fireEvent.change(screen.getByLabelText("Token Akses Bearer"), {
      target: { value: "tok-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Muat Dokumen" }));

    await waitFor(() => {
      expect(screen.getByText(/Perhatian Peninjau:/i)).toBeInTheDocument();
    });
    expect(screen.getByTestId("conflict-card-meeting_date")).toBeInTheDocument();
    expect(screen.getByText(/Judul Rapat: Rapat M7/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Setujui Dokumen" })).toBeDisabled();

    // Select conflict candidate -> marks dirty & resolved locally
    fireEvent.click(screen.getByRole("button", { name: 'Gunakan Kandidat Ini ("2026-11-20")' }));
    expect(screen.getByTestId("unsaved-indicator")).toBeInTheDocument();

    // Save new version -> PATCH called with expected_version_id, expected_content_hash, resolved_conflict_fields
    fireEvent.click(screen.getByRole("button", { name: "Simpan Versi Baru" }));
    await waitFor(() => {
      expect(screen.getByText("Versi #3")).toBeInTheDocument();
    });

    const patchCall = fetchMock.mock.calls.find(
      ([url, init]) => String(url).endsWith("/documents/doc-1") && init?.method === "PATCH",
    );
    expect(patchCall).toBeDefined();
    const patchBody = JSON.parse(String(patchCall?.[1]?.body));
    expect(patchBody.expected_version_id).toBe("ver-2");
    expect(patchBody.expected_content_hash).toBe(hashV2);
    expect(patchBody.resolved_conflict_fields).toEqual(["meeting_date"]);

    // Load historical v1 into editor -> marks dirty without mutating v3, then reset
    fireEvent.click(screen.getByRole("tab", { name: /Riwayat Versi/i }));
    const v1Card = screen.getByTestId("version-item-v1");
    const loadBtn = v1Card.querySelector("button:nth-of-type(2)") as HTMLButtonElement;
    fireEvent.click(loadBtn);
    expect(screen.getByTestId("unsaved-indicator")).toBeInTheDocument();
    expect(screen.getByText(/Memuat pratinjau dari Versi #1/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reset Perubahan" }));
    expect(screen.queryByTestId("unsaved-indicator")).not.toBeInTheDocument();

    // Approve v3 -> confirmation -> POST /approve
    fireEvent.click(screen.getByRole("button", { name: "Setujui Dokumen" }));
    expect(screen.getByTestId("approve-confirmation-panel")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Konfirmasi & Setujui Dokumen" }));

    await waitFor(() => {
      expect(screen.getByTestId("document-status-badge")).toHaveTextContent("Telah Disetujui");
    });
  });

  it("enforces read-only inspector mode on /documents when an editor views another member's document", async () => {
    const docOther = {
      id: "doc-other",
      workspace_id: "ws-org",
      created_by_user_id: "user-owner",
      document_type: "meeting_minutes",
      title: "Notulen Milik Anggota Lain",
      status: "review_required",
      current_version_id: "ver-1",
      selected_template_version_id: null,
      approved_version_id: null,
      created_at: "2026-11-18T00:00:00Z",
      updated_at: "2026-11-18T00:00:00Z",
      current_version: {
        id: "ver-1",
        document_id: "doc-other",
        version_number: 1,
        structured_content: {
          meeting_title: "Rapat Pengurus",
          meeting_date: "2026-11-18",
          attendees: ["Rudi"],
          agenda_items: ["Evaluasi"],
          discussion_summary: "Ringkasan",
          decisions: [],
          action_items: [],
          opinions: [],
          proposals: [],
          field_statuses: { meeting_title: "grounded" },
          conflicts: [],
          provenance: [],
          warnings: [],
        },
        content_hash: "a".repeat(64),
        created_by_user_id: null,
        created_at: "2026-11-18T00:00:00Z",
        approval_status: "pending",
        approved_by_user_id: null,
        approved_at: null,
      },
      sources_count: 0,
      jobs: [],
    };

    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) {
        return Promise.resolve(
          jsonOk({
            id: "user-editor",
            email: "ed@example.com",
            display_name: "Editor",
            email_verified: true,
            status: "active",
            created_at: "2026-11-18T00:00:00Z",
          }),
        );
      }
      if (url.includes("/workspaces?")) {
        return Promise.resolve(
          jsonOk({
            items: [
              {
                id: "ws-org",
                name: "Org Sekolah",
                type: "organization",
                owner_user_id: "user-owner",
                status: "active",
                my_role: "editor",
                created_at: "2026-11-18T00:00:00Z",
                updated_at: "2026-11-18T00:00:00Z",
              },
            ],
            page: 1,
            page_size: 20,
            total: 1,
          }),
        );
      }
      if (url.includes("/workspaces/ws-org/documents?")) {
        return Promise.resolve(jsonOk({ items: [docOther], total: 1, page: 1, page_size: 50 }));
      }
      if (url.endsWith("/documents/doc-other")) {
        return Promise.resolve(jsonOk(docOther));
      }
      if (url.endsWith("/documents/doc-other/sources")) {
        return Promise.resolve(jsonOk({ items: [], total: 0 }));
      }
      if (url.endsWith("/documents/doc-other/versions")) {
        return Promise.resolve(jsonOk({ items: [docOther.current_version], total: 1 }));
      }
      return Promise.reject(new Error(`Unexpected fetch ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DocumentsPage />);
    fireEvent.change(screen.getByLabelText("Token Akses Bearer"), {
      target: { value: "tok-ed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Muat Dokumen" }));

    await waitFor(() => {
      expect(screen.getByTestId("readonly-banner")).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: "Simpan Versi Baru" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Setujui Dokumen" })).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Judul Rapat/i)).toBeDisabled();
  });
});

