export type TriageDecision = 'research' | 'monitor' | 'no_action'
export type ResearchStatus = 'not_started' | 'running' | 'completed' | 'failed'

export interface Evidence {
  result_id: string
  url?: string | null
  title: string
  source_type?: string | null
  credibility?: 'official' | 'unverified' | null
  status?: 'confirmed' | 'pending' | 'conflict' | null
  observed_at?: string | null
  content: string
}

export interface ClaimSupport {
  result_id: string
  excerpt: string
  evidence_summary?: string | null
}

export interface Claim {
  text: string
  kind: 'fact' | 'inference' | 'recommendation'
  support: ClaimSupport[]
}

export interface OwnComparison {
  status: 'supported' | 'insufficient_own_product_context'
  claims: Claim[]
}

export interface TriageResult {
  decision: TriageDecision
  reason: string
  focus_relevance: string
  own_product_relevance: OwnComparison
  evidence_refs: string[]
  uncertainty: string[]
  missing_context: string[]
}

export interface ResearchReport {
  event_summary: Claim[]
  background: Claim[]
  competitor_context: Claim[]
  own_product_comparison: OwnComparison
  implications: Claim[]
  watch_next: Claim[]
  uncertainties: string[]
  evidence_refs: string[]
}

export interface ResearchState {
  event_id: string
  recommended: boolean
  status: ResearchStatus
  result?: ResearchReport
  citations?: Evidence[]
  error?: { type: string; message: string }
}

export interface ChangeItem {
  item: string
  previous_state?: string | null
  current_state?: string | null
}

export interface CompetitionEvent {
  event_id: string
  competitor: string
  event_type: string
  title: string
  summary: string
  event_time?: string | null
  observation_time?: string | null
  effective_time?: string | null
  affected_scope?: string | null
  discovery_origin: string
  change_items: ChangeItem[]
  evidence: Evidence[]
  triage?: TriageResult | null
  research?: ResearchState | null
}

export interface Overview {
  competitor_count: number
  pending_count: number
  last_scan?: ScanResult | null
  events: CompetitionEvent[]
}

export interface MonitoringSource {
  url: string
  title: string
  source_type: string
  monitoring_reason: string
  credibility: 'official' | 'unverified'
}

export interface Competitor {
  name: string
  official_url?: string | null
  additional_urls: string[]
  own_product_id?: string | null
  product_profile?: string | null
  current_focus: string
  sources: MonitoringSource[]
  discovery_complete?: boolean | null
  remaining_gaps: string[]
  last_scan_at?: string | null
}

export interface TestResetResult {
  success: boolean
  cleared: {
    events_cleared: number
    triage_results_cleared: number
    pending_candidates_cleared: number
    research_runs_cleared: number
    research_artifacts_cleared: number
    statement_states_cleared: number
    monitoring_states_cleared: number
    snapshots_cleared: number
    run_records_cleared: number
  }
}

export interface OwnProductDocument {
  document_id?: string | null
  display_name: string
  file_format: string
  parsing_status: 'pending' | 'parsing' | 'parsed' | 'failed' | 'unsupported' | 'scanned_or_no_text'
  ingestion_status: 'not_started' | 'ingesting' | 'completed' | 'failed'
  content_status: 'current' | 'existing' | 'unchanged' | 'conflict' | 'unavailable' | 'current_preserved'
  knowledge_status: 'available' | 'unavailable'
  failure_reason?: string | null
  created_at?: string | null
  updated_at?: string | null
  unit_count: number
}

export interface OwnProduct {
  product_id: string
  product_name?: string | null
  current_focus?: string | null
  editable: boolean
  documents: OwnProductDocument[]
}

export interface ScanResult {
  status: 'complete' | 'partial' | 'failed'
  completed_at: string
  new_event_count: number
  historical_backfill_count: number
  error?: { type: 'configuration_error' | 'runtime_error' | 'source_error' | 'pipeline_error'; message: string }
  competitors: Array<{
    competitor: string
    status: string
    error?: { type: string; message: string }
  }>
}
