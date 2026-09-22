import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { CompetitorsPage, EventCard, HomePage, OwnProductPage, ResearchPage } from './App'
import { documentStatus, sortCompetitionEvents } from './domain'
import type { CompetitionEvent, Overview, OwnProductDocument, ResearchState } from './types'

const mocks = vi.hoisted(() => ({
  getOverview: vi.fn(), scan: vi.fn(), getCompetitors: vi.fn(), addCompetitor: vi.fn(),
  updateCompetitor: vi.fn(), getOwnProduct: vi.fn(), updateOwnProduct: vi.fn(), addDocument: vi.fn(),
  replaceDocument: vi.fn(), getResearch: vi.fn(), startResearch: vi.fn(),
}))

vi.mock('./api/client', () => ({ api: mocks, workspaceId: 'default', productId: 'main' }))

function event(overrides: Partial<CompetitionEvent> = {}): CompetitionEvent {
  return {
    event_id: 'evt_1', competitor: 'OpenAI', event_type: 'capability',
    title: '发布企业能力', summary: '能力正式发布。', event_time: '2026-09-18',
    observation_time: '2026-09-19T08:00:00+08:00', discovery_origin: 'normal_monitoring',
    change_items: [{ item: '管理能力', previous_state: '未提供', current_state: '正式提供' }],
    evidence: [{ result_id: 'event:e1', title: '更新日志', content: '正式发布',
      credibility: 'official', status: 'confirmed', observed_at: '2026-09-19T08:00:00+08:00' }],
    triage: { decision: 'research', reason: '与当前重点直接相关。', focus_relevance: '相关',
      own_product_relevance: { status: 'insufficient_own_product_context', claims: [] },
      evidence_refs: ['event:e1'], uncertainty: [], missing_context: [] },
    research: { event_id: 'evt_1', recommended: true, status: 'not_started' },
    ...overrides,
  }
}

const overview = (events: CompetitionEvent[]): Overview => ({
  competitor_count: 1, pending_count: 0, last_scan: null, events,
})

beforeEach(() => {
  vi.clearAllMocks()
  mocks.getOverview.mockResolvedValue(overview([]))
  mocks.getCompetitors.mockResolvedValue([])
})

describe('竞争事件工作台', () => {
  it('按 research、monitor、no_action 分组，并在组内按时间倒序', () => {
    const rows = [
      event({ event_id: 'm', event_time: '2026-09-20', triage: { ...event().triage!, decision: 'monitor' } }),
      event({ event_id: 'r-old', event_time: '2026-09-18' }),
      event({ event_id: 'n', event_time: '2026-09-21', triage: { ...event().triage!, decision: 'no_action' } }),
      event({ event_id: 'r-new', event_time: '2026-09-19' }),
    ]
    expect(sortCompetitionEvents(rows).map(item => item.event_id)).toEqual(['r-new', 'r-old', 'm', 'n'])
  })

  it('research 事件显示深入研究', () => {
    render(<EventCard event={event()} onResearch={vi.fn()} />)
    expect(screen.getByRole('button', { name: '深入研究' })).toBeInTheDocument()
  })

  it('深入研究按钮传递正式 event_id 对应事件', async () => {
    const onResearch = vi.fn()
    render(<EventCard event={event({ event_id: 'evt_formal_123' })} onResearch={onResearch} />)
    await userEvent.click(screen.getByRole('button', { name: '深入研究' }))
    expect(onResearch.mock.calls[0][0].event_id).toBe('evt_formal_123')
  })

  it('monitor 事件不显示深入研究', () => {
    render(<EventCard event={event({ triage: { ...event().triage!, decision: 'monitor' } })} onResearch={vi.fn()} />)
    expect(screen.queryByRole('button', { name: '深入研究' })).not.toBeInTheDocument()
  })

  it('no_action 事件不显示深入研究', () => {
    render(<EventCard event={event({ triage: { ...event().triage!, decision: 'no_action' } })} onResearch={vi.fn()} />)
    expect(screen.queryByRole('button', { name: '深入研究' })).not.toBeInTheDocument()
  })

  it('历史回溯事件显示标签', () => {
    render(<EventCard event={event({ discovery_origin: 'historical_backfill' })} onResearch={vi.fn()} />)
    expect(screen.getByText('历史回溯')).toBeInTheDocument()
  })

  it('立即扫描期间显示加载状态', async () => {
    let resolveScan!: (value: unknown) => void
    mocks.getOverview.mockResolvedValue(overview([event()]))
    mocks.scan.mockReturnValue(new Promise(resolve => { resolveScan = resolve }))
    render(<HomePage onResearch={vi.fn()} />)
    await screen.findByText('发布企业能力')
    await userEvent.click(screen.getByRole('button', { name: '立即扫描' }))
    expect(screen.getByRole('button', { name: '扫描中…' })).toBeDisabled()
    resolveScan({ status: 'complete', completed_at: '2026-09-21', new_event_count: 0, competitors: [] })
  })

  it('扫描成功后刷新真实事件', async () => {
    mocks.getOverview.mockResolvedValueOnce(overview([])).mockResolvedValueOnce(overview([event()]))
    mocks.scan.mockResolvedValue({ status: 'complete', completed_at: '2026-09-21', new_event_count: 1, competitors: [] })
    render(<HomePage onResearch={vi.fn()} />)
    await screen.findByText('暂无竞争事件')
    await userEvent.click(screen.getByRole('button', { name: '立即扫描' }))
    expect(await screen.findByText('发布企业能力')).toBeInTheDocument()
    expect(screen.getByText('扫描完成 · 发现 1 条新事件')).toBeInTheDocument()
  })

  it('扫描来源失败时显示产品级来源提示', async () => {
    mocks.getOverview.mockResolvedValue(overview([]))
    mocks.scan.mockResolvedValue({ status: 'failed', completed_at: '2026-09-21',
      new_event_count: 0, competitors: [],
      error: { type: 'source_error', message: '部分来源无法访问，请稍后重试。' } })
    render(<HomePage onResearch={vi.fn()} />)
    await screen.findByText('暂无竞争事件')
    await userEvent.click(screen.getByRole('button', { name: '立即扫描' }))
    expect(await screen.findByText('部分来源无法访问，请稍后重试。')).toBeInTheDocument()
  })
})

describe('深入研究生命周期', () => {
  const completed: ResearchState = {
    event_id: 'evt_1', recommended: true, status: 'completed', citations: [],
    result: { event_summary: [{ text: '已确认变化', kind: 'fact', support: [] }],
      background: [], competitor_context: [],
      own_product_comparison: { status: 'insufficient_own_product_context', claims: [] },
      implications: [], watch_next: [], uncertainties: [], evidence_refs: [] },
  }

  it('completed Research 再次打开时不重新启动', async () => {
    mocks.getOverview.mockResolvedValue(overview([event({ research: completed })]))
    mocks.getResearch.mockResolvedValue(completed)
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    expect(await screen.findByText('已确认变化')).toBeInTheDocument()
    expect(mocks.startResearch).not.toHaveBeenCalled()
  })

  it('not_started Research 进入页面后使用同一个 event_id 启动', async () => {
    const waiting: ResearchState = { event_id: 'evt_1', recommended: true, status: 'not_started' }
    mocks.getOverview.mockResolvedValue(overview([event({ research: waiting })]))
    mocks.getResearch.mockResolvedValue(waiting)
    mocks.startResearch.mockResolvedValue({ ...waiting, status: 'running' })
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    await waitFor(() => expect(mocks.startResearch).toHaveBeenCalledWith('evt_1', false))
    expect(await screen.findByText('研究中')).toBeInTheDocument()
  })

  it('failed Research 可通过真实 retry 参数重试', async () => {
    const failed: ResearchState = { event_id: 'evt_1', recommended: true, status: 'failed',
      error: { type: 'research_error', message: '研究任务未完成，可显式重试。' } }
    mocks.getOverview.mockResolvedValue(overview([event({ research: failed })]))
    mocks.getResearch.mockResolvedValue(failed)
    mocks.startResearch.mockResolvedValue(completed)
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    await screen.findByText('本次深入研究未完成，事件及已有证据不受影响。')
    await userEvent.click(screen.getAllByRole('button', { name: '重新研究' })[0])
    expect(mocks.startResearch).toHaveBeenCalledWith('evt_1', true)
    expect(await screen.findByText('已确认变化')).toBeInTheDocument()
  })
})

describe('配置与文档显示', () => {
  it('编辑保存产品配置，并在重新打开页面后显示持久化值', async () => {
    const empty = { product_id: 'main', product_name: null, current_focus: null,
      editable: true, documents: [] }
    const saved = { ...empty, product_name: 'Acme Agent Platform',
      current_focus: '企业级 Agent 管理、权限体系、Agent 治理' }
    mocks.getOwnProduct.mockResolvedValueOnce(empty).mockResolvedValueOnce(saved)
    mocks.updateOwnProduct.mockResolvedValue(saved)

    const first = render(<OwnProductPage />)
    await screen.findAllByText('尚未配置')
    await userEvent.click(screen.getByRole('button', { name: '编辑' }))
    await userEvent.type(screen.getByLabelText('产品名称'), 'Acme Agent Platform')
    await userEvent.type(screen.getByLabelText('当前研究重点'), '企业级 Agent 管理、权限体系、Agent 治理')
    await userEvent.click(screen.getByRole('button', { name: '保存' }))
    expect(mocks.updateOwnProduct).toHaveBeenCalledWith({
      product_name: 'Acme Agent Platform',
      current_focus: '企业级 Agent 管理、权限体系、Agent 治理',
    })
    expect(await screen.findByText('Acme Agent Platform')).toBeInTheDocument()

    first.unmount()
    render(<OwnProductPage />)
    expect(await screen.findByText('企业级 Agent 管理、权限体系、Agent 治理')).toBeInTheDocument()
  })

  it('把真实解析与入库状态压缩为用户状态', () => {
    const base: OwnProductDocument = { document_id: 'doc_1', display_name: '产品.pdf', file_format: 'pdf',
      parsing_status: 'parsed', ingestion_status: 'completed', content_status: 'current',
      knowledge_status: 'available', unit_count: 1 }
    expect(documentStatus(base).label).toBe('已入库')
    expect(documentStatus({ ...base, parsing_status: 'parsing', ingestion_status: 'not_started' }).label).toBe('解析中')
    expect(documentStatus({ ...base, parsing_status: 'scanned_or_no_text', ingestion_status: 'failed' }).label).toBe('失败')
  })

  it('展示真实识别来源及可信度', async () => {
    mocks.getCompetitors.mockResolvedValue([{ name: 'OpenAI', official_url: 'https://openai.com', additional_urls: [],
      own_product_id: 'main', current_focus: '', product_profile: null, discovery_complete: true,
      remaining_gaps: [], last_scan_at: null,
      sources: [{ url: 'https://openai.com/changelog', title: 'OpenAI Changelog', source_type: 'changelog',
        monitoring_reason: '产品更新', credibility: 'official' }] }])
    render(<CompetitorsPage />)
    expect(await screen.findByText('OpenAI Changelog')).toBeInTheDocument()
    expect(screen.getByText('官方来源')).toBeInTheDocument()
  })
})
