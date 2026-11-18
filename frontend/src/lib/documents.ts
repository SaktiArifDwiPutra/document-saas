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
    source_id: string;
    excerpt: string;
  }>;
  proposals: Array<{
    proposal: string;
    proposer: string | null;
    status: "proposed" | "deferred";
    source_id: string;
    excerpt: string;
  }>;
  decisions: Array<{
    decision: string;
    source_id: string;
    excerpt: string;
  }>;
  action_items: Array<{
    task: string;
    assignee: string | null;
    due_date: string | null;
    source_id: string;
    excerpt: string;
  }>;
  field_statuses: Record<string, "grounded" | "missing" | "conflicting">;
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

