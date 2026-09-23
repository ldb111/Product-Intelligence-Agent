import type { CompetitionEvent, OwnProductDocument, ScanResult, TriageDecision } from './types'

const priority: Record<TriageDecision, number> = { research: 0, monitor: 1, no_action: 2 }

function timeValue(event: CompetitionEvent): number {
  const eventTime = event.event_time ? Date.parse(event.event_time) : Number.NaN
  if (!Number.isNaN(eventTime)) return eventTime
  const observed = event.observation_time ? Date.parse(event.observation_time) : Number.NaN
  return Number.isNaN(observed) ? 0 : observed
}

export function sortCompetitionEvents(events: CompetitionEvent[]): CompetitionEvent[] {
  return [...events].sort((left, right) => {
    const leftPriority = left.triage ? priority[left.triage.decision] : 3
    const rightPriority = right.triage ? priority[right.triage.decision] : 3
    return leftPriority - rightPriority || timeValue(right) - timeValue(left)
  })
}

export function isWithinDays(event: CompetitionEvent, days: number | null): boolean {
  if (days === null) return true
  const timestamp = timeValue(event)
  return timestamp > 0 && timestamp >= Date.now() - days * 86_400_000
}

export function scanSummary(result: ScanResult) {
  const normal = result.new_event_count
  const historical = result.historical_backfill_count ?? 0
  if (normal > 0 && historical > 0) return `扫描完成 · 新增 ${normal} 条 · 历史回溯 ${historical} 条`
  if (normal > 0) return `扫描完成 · 新增 ${normal} 条事件`
  if (historical > 0) return `扫描完成 · 发现 ${historical} 条历史事件，本次无新增事件`
  return '扫描完成 · 本次未发现新的竞争事件'
}

export function documentStatus(document: OwnProductDocument): {
  label: string
  tone: 'success' | 'progress' | 'danger'
} {
  if (document.parsing_status === 'parsed' && document.ingestion_status === 'completed') {
    return { label: '已入库', tone: 'success' }
  }
  if (['pending', 'parsing'].includes(document.parsing_status)
      || document.ingestion_status === 'ingesting') {
    return { label: '解析中', tone: 'progress' }
  }
  return { label: '失败', tone: 'danger' }
}

export const eventTypeLabels: Record<string, string> = {
  capability: '能力变化',
  availability: '可用范围',
  pricing: '定价变化',
  plan_packaging: '套餐变化',
  access_permission: '访问权限',
  commercialization: '商业化',
  deprecation: '下线调整',
  positioning: '产品定位',
}

export const sourceTypeLabels: Record<string, string> = {
  product_page: '产品页', pricing: '定价', changelog: '更新日志',
  release_notes: '发布说明', announcement: '公告', docs: '文档', blog: '博客',
  marketplace: '市场', model_catalog: '模型目录', status_page: '状态页', other: '其他',
}
