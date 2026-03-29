// Common types shared across domains

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface APIErrorResponse {
  detail?: string;
  message?: string;
}

export interface BulkUpdateRequest {
  action: string;
  [key: string]: unknown;
}

export interface BulkJobsRequest extends BulkUpdateRequest {
  job_ids: number[];
}

export interface BulkPrintersRequest extends BulkUpdateRequest {
  printer_ids: number[];
}

export interface BulkSpoolsRequest extends BulkUpdateRequest {
  spool_ids: number[];
}
