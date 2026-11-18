import { parseApiResponse } from "@/lib/auth";
import { getApiBaseUrl } from "@/lib/config";

export type DocumentType = "meeting_minutes";
export type DocumentStatus =
  | "draft"
  | "processing"
  | "review_required"
  | "approved"
  | "rendering"
  | "completed"
  | "failed"
  | "archived";

export type ApprovalStatus = "pending" | "approved" | "superseded" | "rejected";
export type SourceType = "form" | "text" | "upload" | "workspace_reference";
export type SourceValidationStatus = "pending" | "valid" | "rejected";
export type JobType = "normalize" | "ai_generation" | "docx_render";
export type JobStatus = "queued" | "running" | "validating" | "completed" | "failed" | "cancelled";
export type ReviewFieldStatus = "grounded" | "user_provided" | "missing" | "conflicting";
export type ReviewItemOrigin = "grounded" | "user_provided";

export interface DocumentReviewReadiness {
  is_ready_for_approval: boolean;
  missing_required_fields: string[];
  unresolved_conflict_fields: string[];
  blocking_issues: string[];
  optional_warnings: string[];
}

export interface DocumentVersionData {
  id: string;
  document_id: string;
  version_number: number;
  structured_content: Record<string, unknown>;
  content_hash: string;
  created_by_user_id: string | null;
  created_at: string;
  approval_status: ApprovalStatus;
  approved_by_user_id: string | null;
  approved_at: string | null;
}

export interface DocumentSourceData {
  id: string;
  workspace_id: string;
  document_id: string;
  document_version_id: string | null;
  source_bundle_id: string | null;
  source_type: SourceType;
  has_stored_object: boolean;
  source_name: string;
  content_hash: string;
  validation_status: SourceValidationStatus;
  metadata: Record<string, unknown>;
  extracted_text_preview: string | null;
  created_by_user_id: string | null;
  created_at: string;
}

export interface JobData {
  id: string;
  workspace_id: string;
  document_id: string;
  job_type: JobType;
  status: JobStatus;
  source_bundle_id: string | null;
  workflow_key: string;
  workflow_version: string;
  workflow_provider: string;
  attempt_count: number;
  started_at: string | null;
  completed_at: string | null;
  error_code: string | null;
  safe_error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentSummaryData {
  id: string;
  workspace_id: string;
  created_by_user_id: string | null;
  document_type: string;
  title: string;
  status: DocumentStatus;
  current_version_id: string | null;
  selected_template_version_id: string | null;
  approved_version_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentDetailData extends DocumentSummaryData {
  current_version: DocumentVersionData | null;
  sources_count: number;
  jobs: JobData[];
  review_readiness?: DocumentReviewReadiness | null;
}

export interface DocumentListData {
  items: DocumentSummaryData[];
  total: number;
  page: number;
  page_size: number;
}

export interface DocumentVersionListData {
  items: DocumentVersionData[];
  total: number;
}

export interface DocumentSourceListData {
  items: DocumentSourceData[];
  total: number;
}

export interface UploadIntentData {
  object_key: string;
  upload_url: string;
  method: string;
  required_headers: Record<string, string>;
  expires_at: string;
  max_file_size_bytes: number;
}

export interface ProcessDocumentData {
  document: DocumentSummaryData;
  source_bundle_id: string;
  jobs: JobData[];
  reservation_id: string | null;
  reserved_credits: number;
}

export interface DeleteDocumentData {
  id: string;
  status: "archived";
  deleted_at: string;
}

export interface MeetingMinutesFormInput {
  meeting_title: string;
  meeting_date?: string | null;
  meeting_time?: string | null;
  location?: string | null;
  organizer?: string | null;
  attendees?: string[];
  agenda_items?: string[];
  notes?: string | null;
  decisions_raw?: string[];
  action_items_raw?: string[];
}

export type SubmitDocumentSourcePayload =
  | {
      source_type: "upload";
      source_name?: string | null;
      object_key: string;
      filename: string;
      content_type: string;
    }
  | {
      source_type: "form";
      source_name?: string | null;
      form_data: MeetingMinutesFormInput;
    }
  | {
      source_type: "text";
      source_name?: string | null;
      raw_text: string;
    };

export async function listWorkspaceDocuments(
  accessToken: string,
  workspaceId: string,
  page = 1,
  pageSize = 20,
): Promise<DocumentListData> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  const res = await fetch(
    `${getApiBaseUrl()}/workspaces/${encodeURIComponent(workspaceId)}/documents?${params.toString()}`,
    {
      method: "GET",
      headers: { Authorization: `Bearer ${accessToken}` },
    },
  );
  return parseApiResponse<DocumentListData>(res);
}

export async function createWorkspaceDocument(
  accessToken: string,
  workspaceId: string,
  payload: {
    title: string;
    document_type?: DocumentType;
    selected_template_version_id?: string | null;
    initial_content?: Record<string, unknown> | null;
  },
): Promise<DocumentDetailData> {
  const res = await fetch(
    `${getApiBaseUrl()}/workspaces/${encodeURIComponent(workspaceId)}/documents`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        document_type: "meeting_minutes",
        ...payload,
      }),
    },
  );
  return parseApiResponse<DocumentDetailData>(res);
}

export async function getDocument(
  accessToken: string,
  documentId: string,
): Promise<DocumentDetailData> {
  const res = await fetch(`${getApiBaseUrl()}/documents/${encodeURIComponent(documentId)}`, {
    method: "GET",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  return parseApiResponse<DocumentDetailData>(res);
}

export async function updateDocument(
  accessToken: string,
  documentId: string,
  payload: {
    title?: string | null;
    structured_content?: Record<string, unknown> | null;
    selected_template_version_id?: string | null;
    expected_version_id?: string | null;
    expected_content_hash?: string | null;
    resolved_conflict_fields?: string[];
  },
): Promise<DocumentDetailData> {
  const res = await fetch(`${getApiBaseUrl()}/documents/${encodeURIComponent(documentId)}`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return parseApiResponse<DocumentDetailData>(res);
}

export async function approveDocument(
  accessToken: string,
  documentId: string,
  payload: {
    version_id: string;
    content_hash: string;
  },
): Promise<DocumentDetailData> {
  const res = await fetch(
    `${getApiBaseUrl()}/documents/${encodeURIComponent(documentId)}/approve`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    },
  );
  return parseApiResponse<DocumentDetailData>(res);
}

export async function deleteDocument(
  accessToken: string,
  documentId: string,
): Promise<DeleteDocumentData> {
  const res = await fetch(`${getApiBaseUrl()}/documents/${encodeURIComponent(documentId)}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  return parseApiResponse<DeleteDocumentData>(res);
}

export async function createDocumentSourceUploadIntent(
  accessToken: string,
  workspaceId: string,
  documentId: string,
  payload: {
    filename: string;
    content_type: string;
    size_bytes: number;
  },
): Promise<UploadIntentData> {
  const res = await fetch(
    `${getApiBaseUrl()}/workspaces/${encodeURIComponent(workspaceId)}/documents/${encodeURIComponent(documentId)}/sources/upload-intents`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    },
  );
  return parseApiResponse<UploadIntentData>(res);
}

export async function submitDocumentSource(
  accessToken: string,
  workspaceId: string,
  documentId: string,
  payload: SubmitDocumentSourcePayload,
): Promise<DocumentSourceData> {
  const res = await fetch(
    `${getApiBaseUrl()}/workspaces/${encodeURIComponent(workspaceId)}/documents/${encodeURIComponent(documentId)}/sources`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    },
  );
  return parseApiResponse<DocumentSourceData>(res);
}

export async function listDocumentSources(
  accessToken: string,
  documentId: string,
): Promise<DocumentSourceListData> {
  const res = await fetch(
    `${getApiBaseUrl()}/documents/${encodeURIComponent(documentId)}/sources`,
    {
      method: "GET",
      headers: { Authorization: `Bearer ${accessToken}` },
    },
  );
  return parseApiResponse<DocumentSourceListData>(res);
}

export async function processDocument(
  accessToken: string,
  documentId: string,
  payload: { idempotency_key?: string } = {},
): Promise<ProcessDocumentData> {
  const res = await fetch(
    `${getApiBaseUrl()}/documents/${encodeURIComponent(documentId)}/process`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    },
  );
  return parseApiResponse<ProcessDocumentData>(res);
}

export async function listDocumentVersions(
  accessToken: string,
  documentId: string,
): Promise<DocumentVersionListData> {
  const res = await fetch(
    `${getApiBaseUrl()}/documents/${encodeURIComponent(documentId)}/versions`,
    {
      method: "GET",
      headers: { Authorization: `Bearer ${accessToken}` },
    },
  );
  return parseApiResponse<DocumentVersionListData>(res);
}

export interface ConflictItem {
  field_path: string;
  values: string[];
  source_ids: string[];
  description: string;
}

export interface MeetingMinutesDraftContent {
  meeting_title: string | null;
  meeting_date: string | null;
  meeting_time: string | null;
  location: string | null;
  organizer: string | null;
  chairperson: string | null;
  attendees: string[];
  absentees: string[];
  agenda_items: string[];
  discussion_summary: string | null;
  opinions: Array<{
    statement: string;
    speaker: string | null;
    origin?: ReviewItemOrigin;
    source_id: string | null;
    excerpt: string | null;
  }>;
  proposals: Array<{
    proposal: string;
    proposer: string | null;
    status: "proposed" | "deferred";
    origin?: ReviewItemOrigin;
    source_id: string | null;
    excerpt: string | null;
  }>;
  decisions: Array<{
    decision: string;
    origin?: ReviewItemOrigin;
    source_id: string | null;
    excerpt: string | null;
  }>;
  action_items: Array<{
    task: string;
    assignee: string | null;
    due_date: string | null;
    origin?: ReviewItemOrigin;
    source_id: string | null;
    excerpt: string | null;
  }>;
  field_statuses: Record<string, ReviewFieldStatus>;
  conflicts: ConflictItem[];
  provenance: Array<{
    field_path: string;
    source_id: string;
    source_name: string;
    source_type: SourceType;
    excerpt: string;
  }>;
  warnings: string[];
}

function asNullableString(val: unknown): string | null {
  return typeof val === "string" ? val : null;
}

function asStringArray(val: unknown): string[] {
  if (!Array.isArray(val)) {
    return [];
  }
  return val.filter((item): item is string => typeof item === "string");
}

function resolveItemOrigin(
  rawOrigin: unknown,
  sourceId: string | null,
  excerpt: string | null,
): ReviewItemOrigin {
  if (rawOrigin === "grounded" || rawOrigin === "user_provided") {
    return rawOrigin;
  }
  return sourceId && excerpt ? "grounded" : "user_provided";
}

export function normalizeDraftForEditor(
  raw: Record<string, unknown> | null | undefined,
): MeetingMinutesDraftContent {
  const data = raw ?? {};
  const rawOpinions = Array.isArray(data.opinions) ? data.opinions : [];
  const rawProposals = Array.isArray(data.proposals) ? data.proposals : [];
  const rawDecisions = Array.isArray(data.decisions) ? data.decisions : [];
  const rawActions = Array.isArray(data.action_items) ? data.action_items : [];
  const rawConflicts = Array.isArray(data.conflicts) ? data.conflicts : [];
  const rawProvenance = Array.isArray(data.provenance) ? data.provenance : [];
  const rawStatuses =
    data.field_statuses && typeof data.field_statuses === "object"
      ? (data.field_statuses as Record<string, unknown>)
      : {};

  const field_statuses: Record<string, ReviewFieldStatus> = {};
  for (const [k, v] of Object.entries(rawStatuses)) {
    if (v === "grounded" || v === "user_provided" || v === "missing" || v === "conflicting") {
      field_statuses[k] = v;
    }
  }

  return {
    meeting_title: asNullableString(data.meeting_title),
    meeting_date: asNullableString(data.meeting_date),
    meeting_time: asNullableString(data.meeting_time),
    location: asNullableString(data.location),
    organizer: asNullableString(data.organizer),
    chairperson: asNullableString(data.chairperson),
    attendees: asStringArray(data.attendees),
    absentees: asStringArray(data.absentees),
    agenda_items: asStringArray(data.agenda_items),
    discussion_summary: asNullableString(data.discussion_summary),
    opinions: rawOpinions
      .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
      .map((item) => {
        const sourceId = asNullableString(item.source_id);
        const excerpt = asNullableString(item.excerpt);
        return {
          statement: typeof item.statement === "string" ? item.statement : "",
          speaker: asNullableString(item.speaker),
          origin: resolveItemOrigin(item.origin, sourceId, excerpt),
          source_id: sourceId,
          excerpt,
        };
      }),
    proposals: rawProposals
      .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
      .map((item) => {
        const sourceId = asNullableString(item.source_id);
        const excerpt = asNullableString(item.excerpt);
        return {
          proposal: typeof item.proposal === "string" ? item.proposal : "",
          proposer: asNullableString(item.proposer),
          status: item.status === "deferred" ? "deferred" : "proposed",
          origin: resolveItemOrigin(item.origin, sourceId, excerpt),
          source_id: sourceId,
          excerpt,
        };
      }),
    decisions: rawDecisions
      .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
      .map((item) => {
        const sourceId = asNullableString(item.source_id);
        const excerpt = asNullableString(item.excerpt);
        return {
          decision: typeof item.decision === "string" ? item.decision : "",
          origin: resolveItemOrigin(item.origin, sourceId, excerpt),
          source_id: sourceId,
          excerpt,
        };
      }),
    action_items: rawActions
      .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
      .map((item) => {
        const sourceId = asNullableString(item.source_id);
        const excerpt = asNullableString(item.excerpt);
        return {
          task: typeof item.task === "string" ? item.task : "",
          assignee: asNullableString(item.assignee),
          due_date: asNullableString(item.due_date),
          origin: resolveItemOrigin(item.origin, sourceId, excerpt),
          source_id: sourceId,
          excerpt,
        };
      }),
    field_statuses,
    conflicts: rawConflicts.filter(
      (item): item is ConflictItem =>
        Boolean(
          item &&
            typeof item === "object" &&
            typeof (item as ConflictItem).field_path === "string" &&
            Array.isArray((item as ConflictItem).values),
        ),
    ),
    provenance: rawProvenance.filter(
      (item): item is MeetingMinutesDraftContent["provenance"][number] =>
        Boolean(
          item &&
            typeof item === "object" &&
            typeof item.field_path === "string" &&
            typeof item.source_id === "string" &&
            typeof item.excerpt === "string",
        ),
    ),
    warnings: asStringArray(data.warnings),
  };
}

function isFieldNonEmptyInDraft(draft: MeetingMinutesDraftContent, fieldPath: string): boolean {
  switch (fieldPath) {
    case "meeting_title":
      return Boolean(draft.meeting_title?.trim());
    case "meeting_date":
      return Boolean(draft.meeting_date?.trim());
    case "meeting_time":
      return Boolean(draft.meeting_time?.trim());
    case "location":
      return Boolean(draft.location?.trim());
    case "organizer":
      return Boolean(draft.organizer?.trim());
    case "chairperson":
      return Boolean(draft.chairperson?.trim());
    case "discussion_summary":
      return Boolean(draft.discussion_summary?.trim());
    case "attendees":
      return draft.attendees.some((x) => x.trim().length > 0);
    case "absentees":
      return draft.absentees.some((x) => x.trim().length > 0);
    case "agenda_items":
      return draft.agenda_items.some((x) => x.trim().length > 0);
    case "opinions":
      return draft.opinions.length > 0;
    case "proposals":
      return draft.proposals.length > 0;
    case "decisions":
      return draft.decisions.length > 0;
    case "action_items":
      return draft.action_items.length > 0;
    default:
      return false;
  }
}

export function evaluateClientReviewReadiness(
  draft: MeetingMinutesDraftContent | null,
  resolvedConflictFields: string[] = [],
): DocumentReviewReadiness {
  if (!draft) {
    return {
      is_ready_for_approval: false,
      missing_required_fields: [
        "meeting_title",
        "meeting_date",
        "attendees",
        "agenda_items",
        "content_sections",
      ],
      unresolved_conflict_fields: [],
      blocking_issues: ["Draf dokumen belum tersedia untuk ditinjau."],
      optional_warnings: [],
    };
  }

  const missing_required_fields: string[] = [];
  const blocking_issues: string[] = [];

  if (!draft.meeting_title?.trim()) {
    missing_required_fields.push("meeting_title");
    blocking_issues.push("Judul rapat wajib diisi sebelum persetujuan.");
  }
  if (!draft.meeting_date?.trim()) {
    missing_required_fields.push("meeting_date");
    blocking_issues.push("Tanggal rapat wajib diisi sebelum persetujuan.");
  }
  if (!draft.attendees.some((x) => x.trim().length > 0)) {
    missing_required_fields.push("attendees");
    blocking_issues.push("Daftar peserta hadir wajib memiliki minimal satu peserta.");
  }
  if (!draft.agenda_items.some((x) => x.trim().length > 0)) {
    missing_required_fields.push("agenda_items");
    blocking_issues.push("Agenda rapat wajib memiliki minimal satu topik.");
  }

  const hasSummary = Boolean(draft.discussion_summary?.trim());
  const hasDecisions = draft.decisions.some((d) => d.decision.trim().length > 0);
  const hasActions = draft.action_items.some((a) => a.task.trim().length > 0);
  if (!hasSummary && !hasDecisions && !hasActions) {
    missing_required_fields.push("content_sections");
    blocking_issues.push(
      "Minimal satu bagian isi rapat (ringkasan pembahasan, keputusan, atau tindak lanjut) wajib diisi.",
    );
  }

  const resolvedSet = new Set(resolvedConflictFields);
  const unresolved_conflict_fields: string[] = [];
  const seenConflicts = new Set<string>();

  const maybeAddConflict = (fieldPath: string) => {
    if (seenConflicts.has(fieldPath)) {
      return;
    }
    const isResolved = resolvedSet.has(fieldPath) && isFieldNonEmptyInDraft(draft, fieldPath);
    if (!isResolved) {
      seenConflicts.add(fieldPath);
      unresolved_conflict_fields.push(fieldPath);
      blocking_issues.push(`Konflik pada bidang '${fieldPath}' wajib diselesaikan terlebih dahulu.`);
    }
  };

  for (const conflict of draft.conflicts) {
    maybeAddConflict(conflict.field_path);
  }
  for (const [fieldName, status] of Object.entries(draft.field_statuses)) {
    if (status === "conflicting") {
      maybeAddConflict(fieldName);
    }
  }

  const optional_warnings: string[] = [];
  const seenWarnings = new Set<string>();
  for (const w of draft.warnings) {
    const cleaned = w.trim();
    if (cleaned && !seenWarnings.has(cleaned)) {
      seenWarnings.add(cleaned);
      optional_warnings.push(cleaned);
    }
  }
  if (!draft.meeting_time?.trim()) {
    optional_warnings.push("Waktu rapat belum diisi (opsional).");
  }
  if (!draft.location?.trim()) {
    optional_warnings.push("Lokasi rapat belum diisi (opsional).");
  }
  if (!draft.organizer?.trim()) {
    optional_warnings.push("Penyelenggara rapat belum diisi (opsional).");
  }
  if (!draft.chairperson?.trim()) {
    optional_warnings.push("Pimpinan rapat belum diisi (opsional).");
  }
  if (!hasDecisions) {
    optional_warnings.push("Tidak ada keputusan formal tercatat pada rapat ini (opsional).");
  }
  if (draft.action_items.some((a) => !a.assignee?.trim() || !a.due_date?.trim())) {
    optional_warnings.push("Beberapa tindak lanjut belum memiliki PIC atau tenggat waktu (opsional).");
  }

  return {
    is_ready_for_approval:
      missing_required_fields.length === 0 && unresolved_conflict_fields.length === 0,
    missing_required_fields,
    unresolved_conflict_fields,
    blocking_issues,
    optional_warnings,
  };
}

export function computeDraftDirtySignature(
  draft: MeetingMinutesDraftContent | null,
  resolvedConflictFields: string[],
  title: string,
): string {
  if (!draft) {
    return JSON.stringify({ title: title.trim(), draft: null });
  }
  return JSON.stringify({
    title: title.trim(),
    resolved: [...new Set(resolvedConflictFields)].sort(),
    meeting_title: draft.meeting_title ?? null,
    meeting_date: draft.meeting_date ?? null,
    meeting_time: draft.meeting_time ?? null,
    location: draft.location ?? null,
    organizer: draft.organizer ?? null,
    chairperson: draft.chairperson ?? null,
    attendees: draft.attendees,
    absentees: draft.absentees,
    agenda_items: draft.agenda_items,
    discussion_summary: draft.discussion_summary ?? null,
    opinions: draft.opinions.map((o) => ({ statement: o.statement, speaker: o.speaker })),
    proposals: draft.proposals.map((p) => ({
      proposal: p.proposal,
      proposer: p.proposer,
      status: p.status,
    })),
    decisions: draft.decisions.map((d) => ({ decision: d.decision })),
    action_items: draft.action_items.map((a) => ({
      task: a.task,
      assignee: a.assignee,
      due_date: a.due_date,
    })),
  });
}

export interface UsageEventItem {
  id: string;
  workspace_id: string;
  user_id: string | null;
  document_id: string | null;
  job_id: string | null;
  provider: string;
  model: string;
  workflow_key: string;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_minor_units: number;
  duration_ms: number;
  status: "completed" | "failed";
  created_at: string;
}

export interface UsageListData {
  items: UsageEventItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface UsageSummaryData {
  workspace_id: string;
  total_events: number;
  completed_events: number;
  failed_events: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_estimated_cost_minor_units: number;
  total_duration_ms: number;
}

export async function getJobStatus(accessToken: string, jobId: string): Promise<JobData> {
  const res = await fetch(`${getApiBaseUrl()}/jobs/${encodeURIComponent(jobId)}`, {
    method: "GET",
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  return parseApiResponse<JobData>(res);
}

export async function getWorkspaceUsage(
  accessToken: string,
  workspaceId: string,
  page = 1,
  pageSize = 20,
): Promise<UsageListData> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  const res = await fetch(
    `${getApiBaseUrl()}/workspaces/${encodeURIComponent(workspaceId)}/usage?${params.toString()}`,
    {
      method: "GET",
      headers: { Authorization: `Bearer ${accessToken}` },
    },
  );
  return parseApiResponse<UsageListData>(res);
}

export async function getWorkspaceUsageSummary(
  accessToken: string,
  workspaceId: string,
): Promise<UsageSummaryData> {
  const res = await fetch(
    `${getApiBaseUrl()}/workspaces/${encodeURIComponent(workspaceId)}/usage/summary`,
    {
      method: "GET",
      headers: { Authorization: `Bearer ${accessToken}` },
    },
  );
  return parseApiResponse<UsageSummaryData>(res);
}

