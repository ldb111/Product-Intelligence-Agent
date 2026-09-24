import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { CompetitorsPage, EventCard, HomePage, OwnProductPage, ResearchPage } from './App'
import { documentStatus, scanSummary, sortCompetitionEvents } from './domain'
import type { CompetitionEvent, Overview, OwnProductDocument, ResearchState } from './types'

const mocks = vi.hoisted(() => ({
  getOverview: vi.fn(), scan: vi.fn(), getCompetitors: vi.fn(), addCompetitor: vi.fn(),
  updateCompetitor: vi.fn(), resetCompetitorTestData: vi.fn(), getOwnProduct: vi.fn(), updateOwnProduct: vi.fn(), addDocument: vi.fn(),
  replaceDocument: vi.fn(), getResearch: vi.fn(), startResearch: vi.fn(), getPreferences: vi.fn(),
  addPreference: vi.fn(), editPreference: vi.fn(), removePreference: vi.fn(), confirmPreference: vi.fn(), ignorePreference: vi.fn(),
  getFollowUps: vi.fn(), sendFollowUp: vi.fn(), retryFollowUp: vi.fn(),
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
  mocks.getPreferences.mockResolvedValue({ confirmed_items: [], awaiting_confirmation_items: [] })
  mocks.getFollowUps.mockResolvedValue({ available: true, turns: [] })
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
    const button = screen.getByRole('button', { name: '深入研究' })
    expect(button).toBeInTheDocument()
    expect(button).toHaveClass('research-action-button')
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

  it('固定状态和分诊枚举只显示中文产品文案', () => {
    render(<EventCard event={event()} onResearch={vi.fn()} />)
    expect(screen.getByText('建议深入研究')).toBeInTheDocument()
    expect(screen.getByText('1 条已确认')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('confirmed')
    expect(document.body).not.toHaveTextContent('research')
  })

  it('立即扫描期间显示加载状态', async () => {
    let resolveScan!: (value: unknown) => void
    mocks.getOverview.mockResolvedValue(overview([event()]))
    mocks.scan.mockReturnValue(new Promise(resolve => { resolveScan = resolve }))
    render(<HomePage onResearch={vi.fn()} />)
    await screen.findByText('发布企业能力')
    await userEvent.click(screen.getByRole('button', { name: '立即扫描' }))
    expect(screen.getByRole('button', { name: '扫描中…' })).toBeDisabled()
    resolveScan({ status: 'complete', completed_at: '2026-09-21', new_event_count: 0,
      historical_backfill_count: 0, competitors: [] })
  })

  it('扫描成功后刷新真实事件', async () => {
    mocks.getOverview.mockResolvedValueOnce(overview([])).mockResolvedValueOnce(overview([event()]))
    mocks.scan.mockResolvedValue({ status: 'complete', completed_at: '2026-09-21', new_event_count: 1,
      historical_backfill_count: 0, competitors: [] })
    render(<HomePage onResearch={vi.fn()} />)
    await screen.findByText('暂无竞争事件')
    await userEvent.click(screen.getByRole('button', { name: '立即扫描' }))
    expect(await screen.findByText('发布企业能力')).toBeInTheDocument()
    expect(screen.getByText('扫描完成 · 新增 1 条事件')).toBeInTheDocument()
  })

  it('扫描提示分别表达普通新增与历史回溯', () => {
    const base = { status: 'complete' as const, completed_at: '2026-09-21', competitors: [] }
    expect(scanSummary({ ...base, new_event_count: 2, historical_backfill_count: 0 })).toBe('扫描完成 · 新增 2 条事件')
    expect(scanSummary({ ...base, new_event_count: 0, historical_backfill_count: 7 })).toBe('扫描完成 · 发现 7 条历史事件，本次无新增事件')
    expect(scanSummary({ ...base, new_event_count: 2, historical_backfill_count: 7 })).toBe('扫描完成 · 新增 2 条 · 历史回溯 7 条')
    expect(scanSummary({ ...base, new_event_count: 0, historical_backfill_count: 0 })).toBe('扫描完成 · 本次未发现新的竞争事件')
  })

  it('扫描来源失败时显示产品级来源提示', async () => {
    mocks.getOverview.mockResolvedValue(overview([]))
    mocks.scan.mockResolvedValue({ status: 'failed', completed_at: '2026-09-21',
      new_event_count: 0, historical_backfill_count: 0, competitors: [],
      error: { type: 'source_error', message: '部分来源无法访问，请稍后重试。' } })
    render(<HomePage onResearch={vi.fn()} />)
    await screen.findByText('暂无竞争事件')
    await userEvent.click(screen.getByRole('button', { name: '立即扫描' }))
    expect(await screen.findByText('部分来源无法访问，请稍后重试。')).toBeInTheDocument()
  })
})

describe('深入研究生命周期', () => {
  const longEvidence = '官方更新日志正文。'.repeat(80)
  const completed: ResearchState = {
    event_id: 'evt_1', recommended: true, status: 'completed', citations: [{
      result_id: 'research:evt_1:page_001', title: '官方更新日志', url: 'https://example.com/changelog',
      credibility: 'official', observed_at: '2026-09-20T00:00:00Z', content: longEvidence,
    }],
    result: { event_summary: [{ text: '竞品已上线更轻量的监测向导，可能降低首次使用门槛。', kind: 'fact', support: [{
      result_id: 'research:evt_1:page_001', excerpt: '竞品已经正式上线该能力。',
      evidence_summary: '官方更新日志确认，竞品已经上线监测向导。',
    }] }],
      background: [{ text: '竞品已经正式上线监测向导', kind: 'fact', support: [{
        result_id: 'research:evt_1:page_001', excerpt: '| 功能 | 说明 |\n| --- | --- |\n| 监测向导 | 输入竞品名称 |',
        evidence_summary: '用户输入竞品名称后即可建立监测。',
      }] }, { text: '该变化可能降低使用门槛', kind: 'inference', support: [{
        result_id: 'research:evt_1:page_001', excerpt: '入口已简化为输入竞品名称。',
        evidence_summary: '官方页面显示，新的监测入口从输入竞品名称开始。',
      }] }], competitor_context: [],
      own_product_comparison: { status: 'insufficient_own_product_context', claims: [] },
      implications: [{ text: '考虑缩短首次配置流程', kind: 'recommendation', support: [{
        result_id: 'research:evt_1:page_001', excerpt: '入口已简化为输入竞品名称。',
        evidence_summary: '竞品已将监测入口简化为输入竞品名称。',
      }] }], watch_next: [{ text: '关注正式定价', kind: 'recommendation', support: [] }],
      uncertainties: ['source_group source_d388bf6d76f4e1b9 与 tool trace 仍需核对'], evidence_refs: ['research:evt_1:page_001'] },
  }

  it('completed Research 再次打开时不重新启动', async () => {
    mocks.getOverview.mockResolvedValue(overview([event({ research: completed })]))
    mocks.getResearch.mockResolvedValue(completed)
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    expect(await screen.findByText('竞品已上线更轻量的监测向导，可能降低首次使用门槛。')).toBeInTheDocument()
    expect(mocks.startResearch).not.toHaveBeenCalled()
  })

  it('not_started 在启动请求永远 pending 时持续显示研究中，完成后切换结果', async () => {
    const waiting: ResearchState = { event_id: 'evt_1', recommended: true, status: 'not_started' }
    mocks.getOverview.mockResolvedValue(overview([event({ research: waiting })]))
    mocks.getResearch.mockResolvedValue(waiting)
    let resolveResearch!: (value: ResearchState) => void
    mocks.startResearch.mockReturnValue(new Promise(resolve => { resolveResearch = resolve }))
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    expect(await screen.findByText('正在深入研究')).toBeInTheDocument()
    expect(screen.getByText('研究中')).toBeInTheDocument()
    expect(screen.getByText('研究对象')).toBeInTheDocument()
    expect(screen.getByText('1 条')).toBeInTheDocument()
    expect(screen.queryByText('研究摘要')).not.toBeInTheDocument()
    await waitFor(() => expect(mocks.startResearch).toHaveBeenCalledWith('evt_1', false))
    resolveResearch(completed)
    expect(await screen.findByText('研究摘要')).toBeInTheDocument()
  })

  it('StrictMode 重跑 effect 不会覆盖 starting 或重复启动 Research', async () => {
    const waiting: ResearchState = { event_id: 'evt_1', recommended: true, status: 'not_started' }
    mocks.getOverview.mockResolvedValue(overview([event({ research: waiting })]))
    mocks.getResearch.mockResolvedValue(waiting)
    mocks.startResearch.mockReturnValue(new Promise(() => undefined))
    render(<StrictMode><ResearchPage eventId="evt_1" onBack={vi.fn()} /></StrictMode>)
    expect(await screen.findByText('正在深入研究')).toBeInTheDocument()
    expect(screen.getByText('研究中')).toBeInTheDocument()
    await waitFor(() => expect(mocks.startResearch).toHaveBeenCalledTimes(1))
  })

  it('刷新时 running 状态持续显示明确 loading', async () => {
    const running: ResearchState = { event_id: 'evt_1', recommended: true, status: 'running' }
    mocks.getOverview.mockResolvedValue(overview([event({ research: running })]))
    mocks.getResearch.mockResolvedValue(running)
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    expect(await screen.findByText('正在深入研究')).toBeInTheDocument()
    expect(screen.getByText('研究中')).toBeInTheDocument()
    expect(mocks.startResearch).not.toHaveBeenCalled()
  })

  it('Claim 默认展示自然语言 evidence_summary，不展示摘录、Markdown 或网页正文', async () => {
    mocks.getOverview.mockResolvedValue(overview([event({ research: completed })]))
    mocks.getResearch.mockResolvedValue(completed)
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    expect(await screen.findByText('竞品已经正式上线监测向导')).toBeInTheDocument()
    const claim = screen.getByText('竞品已经正式上线监测向导').closest('.claim')
    expect(claim).toHaveTextContent('E1')
    expect(claim).toHaveTextContent('关键证据')
    expect(claim).toHaveTextContent('用户输入竞品名称后即可建立监测。')
    expect(document.body).not.toHaveTextContent('| 功能 | 说明 |')
    expect(document.body).not.toHaveTextContent('入口已简化为输入竞品名称。')
    expect(screen.queryByText(longEvidence)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /展开证据/ })).not.toBeInTheDocument()
  })

  it('推断和建议使用正确依据文案，待验证事项不伪造 Evidence', async () => {
    mocks.getOverview.mockResolvedValue(overview([event({ research: completed })]))
    mocks.getResearch.mockResolvedValue(completed)
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    const inference = (await screen.findByText('该变化可能降低使用门槛')).closest('.claim')
    expect(inference).toHaveTextContent('依据')
    expect(inference).toHaveTextContent('官方页面显示，新的监测入口从输入竞品名称开始。')
    const recommendation = screen.getByText('考虑缩短首次配置流程').closest('.claim')
    expect(recommendation).toHaveTextContent('产品建议')
    expect(recommendation).toHaveTextContent('参考依据')
    expect(screen.getByText('来源组与研究过程仍需核对').closest('li')?.querySelector('.research-evidence-card')).toBeNull()
  })

  it('completed 页面采用最终六段信息结构并合并待关注事项', async () => {
    mocks.getOverview.mockResolvedValue(overview([event({ research: completed })]))
    mocks.getResearch.mockResolvedValue(completed)
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    await screen.findByText('研究摘要')
    for (const title of ['关键发现', '对自身产品的影响', '产品建议', '下一步关注']) {
      expect(screen.getByRole('heading', { name: title })).toBeInTheDocument()
    }
    expect(screen.getByText('全部来源（1）')).toBeInTheDocument()
    expect(screen.getByText('当前自身产品资料不足，暂无法判断对应影响。')).toBeInTheDocument()
    for (const oldTitle of ['产品启示', '与自身产品的关系', '仍需确认']) {
      expect(screen.queryByText(oldTitle)).not.toBeInTheDocument()
    }
  })

  it('全部来源默认折叠且展开后只显示轻量索引', async () => {
    mocks.getOverview.mockResolvedValue(overview([event({ research: completed })]))
    mocks.getResearch.mockResolvedValue(completed)
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    const details = (await screen.findByText('全部来源（1）')).closest('details')!
    expect(details).not.toHaveAttribute('open')
    await userEvent.click(screen.getByText('全部来源（1）'))
    expect(details).toHaveTextContent('官方更新日志')
    expect(details).toHaveTextContent('官方来源 · 2026/09/20')
    expect(details).toHaveTextContent('查看来源')
    expect(details).not.toHaveTextContent('用户输入竞品名称后即可建立监测。')
    expect(details).not.toHaveTextContent(longEvidence)
  })

  it('旧 Artifact 缺少 evidence_summary 时安全显示纯文本原文摘录', async () => {
    const legacy: ResearchState = { ...completed, result: { ...completed.result!, background: [{
      text: '历史研究结论', kind: 'fact', support: [{
        result_id: 'research:evt_1:page_001',
        excerpt: '| 功能 | 说明 |\n| --- | --- |\n| 竞品监测 | 30 分钟收到周报 |',
      }],
    }] } }
    mocks.getOverview.mockResolvedValue(overview([event({ research: legacy })]))
    mocks.getResearch.mockResolvedValue(legacy)
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    const claim = (await screen.findByText('历史研究结论')).closest('.claim')
    expect(claim).toHaveTextContent('原文摘录')
    expect(claim).toHaveTextContent('功能；说明 竞品监测；30 分钟收到周报')
    expect(claim).not.toHaveTextContent('| --- |')
  })

  it('completed 页面不展示内部来源标识或工具轨迹', async () => {
    const withInternalFields = {
      ...completed,
      citations: completed.citations?.map(item => ({ ...item,
        source_group: 'internal-source-group', tool_trace: 'private-tool-trace',
      })),
    } as ResearchState
    mocks.getOverview.mockResolvedValue(overview([event({ research: withInternalFields })]))
    mocks.getResearch.mockResolvedValue(withInternalFields)
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    await screen.findByText('竞品已上线更轻量的监测向导，可能降低首次使用门槛。')
    expect(document.body).not.toHaveTextContent('research:evt_1:page_001')
    expect(document.body).not.toHaveTextContent('internal-source-group')
    expect(document.body).not.toHaveTextContent('private-tool-trace')
    expect(document.body).not.toHaveTextContent('source_group')
    expect(document.body).not.toHaveTextContent('source_d388bf6d76f4e1b9')
    expect(document.body).not.toHaveTextContent('tool trace')
    expect(document.body).toHaveTextContent('来源组与研究过程仍需核对')
    expect(screen.getByText('全部来源（1）').closest('details')).not.toHaveAttribute('open')
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
    expect(await screen.findByText('竞品已上线更轻量的监测向导，可能降低首次使用门槛。')).toBeInTheDocument()
  })

  it('初始研究完成后可进入继续追问，发送期间显示运行状态并保留事件标题', async () => {
    mocks.getOverview.mockResolvedValue(overview([event({ research: completed })]))
    mocks.getResearch.mockResolvedValue(completed)
    let resolve!: (value: unknown) => void
    mocks.sendFollowUp.mockReturnValue(new Promise(value => { resolve = value }))
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    await userEvent.click(await screen.findByRole('button', { name: '继续追问' }))
    expect(screen.getByRole('heading', { name: '发布企业能力' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '这次变化对我们当前产品影响最大的是什么？' }))
    await userEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(screen.getAllByText('正在继续研究…')).toHaveLength(2)
    expect(screen.getByRole('button', { name: '正在继续研究…' })).toBeDisabled()
    resolve({ turn_id: 'turn_1', user_message: '这次变化对我们当前产品影响最大的是什么？', status: 'completed',
      candidate_count: 0, created_at: '2026-09-24T00:00:00Z', updated_at: '2026-09-24T00:00:01Z',
      result: { answer: [{ text: '应优先验证管理入口的采用率。', kind: 'recommendation', support: [] }], uncertainties: [], evidence_refs: [] }, citations: [] })
    expect(await screen.findByText('应优先验证管理入口的采用率。')).toBeInTheDocument()
  })

  it('继续追问引用默认折叠，识别到待确认关注后提供产品入口', async () => {
    mocks.getOverview.mockResolvedValue(overview([event({ research: completed })]))
    mocks.getResearch.mockResolvedValue(completed)
    mocks.getFollowUps.mockResolvedValue({ available: true, turns: [{
      turn_id: 'turn_1', user_message: '我长期关注权限治理。', status: 'completed', candidate_count: 1,
      created_at: '2026-09-24T00:00:00Z', updated_at: '2026-09-24T00:00:01Z',
      result: { answer: [{ text: '建议持续核对权限覆盖范围。', kind: 'recommendation', support: [{ result_id: 'follow_up:turn_1:page_001', excerpt: '权限覆盖' }] }], uncertainties: [], evidence_refs: ['follow_up:turn_1:page_001'] },
      citations: [{ result_id: 'follow_up:turn_1:page_001', title: '官方说明', content: '权限覆盖' }],
    }] })
    render(<ResearchPage eventId="evt_1" onBack={vi.fn()} />)
    await userEvent.click(await screen.findByRole('button', { name: '继续追问' }))
    const source = await screen.findByText('参考来源（1）')
    expect(source.closest('details')).not.toHaveAttribute('open')
    expect(document.body).not.toHaveTextContent('follow_up:turn_1:page_001')
  })
})

describe('配置与文档显示', () => {
  const configuredCompetitor = { name: 'OpenAI', official_url: 'https://openai.com', additional_urls: [],
    own_product_id: 'main', current_focus: '', product_profile: null, discovery_complete: true,
    remaining_gaps: [], last_scan_at: '2026-09-23T00:00:00Z',
    sources: [{ url: 'https://openai.com/changelog', title: 'OpenAI Changelog', source_type: 'changelog',
      monitoring_reason: '产品更新', credibility: 'official' as const }] }

  it('开发环境的竞品卡片通过确认弹窗重置测试数据并保留配置视图', async () => {
    mocks.getCompetitors.mockResolvedValue([configuredCompetitor])
    mocks.resetCompetitorTestData.mockResolvedValue({ success: true, cleared: {
      events_cleared: 2, triage_results_cleared: 2, pending_candidates_cleared: 1,
      research_runs_cleared: 1, research_artifacts_cleared: 1, statement_states_cleared: 5,
      monitoring_states_cleared: 4, snapshots_cleared: 2, run_records_cleared: 1,
    } })
    render(<CompetitorsPage />)
    await userEvent.click(await screen.findByRole('button', { name: '重置测试数据' }))
    const dialog = screen.getByRole('dialog', { name: '重置测试数据？' })
    expect(dialog).toHaveTextContent('将清除该竞品已发现的事件、研究结果和扫描状态，但保留竞品及来源配置。')
    expect(dialog).toHaveTextContent('下次扫描将按首次扫描重新处理。')
    await userEvent.click(screen.getByRole('button', { name: '确认重置' }))
    await waitFor(() => expect(mocks.resetCompetitorTestData).toHaveBeenCalledWith('OpenAI'))
    expect(await screen.findByText('测试数据已重置，可重新扫描。')).toBeInTheDocument()
    expect(screen.getByText('OpenAI Changelog')).toBeInTheDocument()
  })

  it('重置失败时显示明确错误且不静默关闭确认弹窗', async () => {
    mocks.getCompetitors.mockResolvedValue([configuredCompetitor])
    mocks.resetCompetitorTestData.mockRejectedValue(new Error('private detail'))
    render(<CompetitorsPage />)
    await userEvent.click(await screen.findByRole('button', { name: '重置测试数据' }))
    await userEvent.click(screen.getByRole('button', { name: '确认重置' }))
    expect(await screen.findByText('重置失败，未能完整清除测试数据。')).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: '重置测试数据？' })).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('private detail')
  })

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
    await userEvent.type(screen.getByLabelText('当前关注重点'), '企业级 Agent 管理、权限体系、Agent 治理')
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

  it('关注与偏好显示中文类型，并可确认系统识别的待确认项', async () => {
    const product = { product_id: 'main', product_name: 'Acme', current_focus: '', editable: true, documents: [] }
    const awaiting = { item_id: 'item_1', type: 'hypothesis', content: '企业权限可能影响采购决策。',
      source: '从研究交流中识别', updated_at: '2026-09-24T00:00:00Z' }
    mocks.getOwnProduct.mockResolvedValue(product)
    mocks.getPreferences.mockResolvedValueOnce({ confirmed_items: [], awaiting_confirmation_items: [awaiting] })
      .mockResolvedValueOnce({ confirmed_items: [{ ...awaiting, source: '从研究交流中确认' }], awaiting_confirmation_items: [] })
    render(<OwnProductPage />)
    await userEvent.click(await screen.findByRole('button', { name: '待确认（1）' }))
    expect(screen.getByText('待验证判断')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('hypothesis')
    await userEvent.click(screen.getByRole('button', { name: '确认' }))
    expect(mocks.confirmPreference).toHaveBeenCalledWith('item_1')
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
    mocks.getCompetitors.mockResolvedValue([configuredCompetitor])
    render(<CompetitorsPage />)
    expect(await screen.findByText('OpenAI Changelog')).toBeInTheDocument()
    expect(screen.getByText('官方来源')).toBeInTheDocument()
  })
})
