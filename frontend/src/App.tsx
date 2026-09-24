import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api/client'
import {
  documentStatus,
  eventTypeLabels,
  isWithinDays,
  scanSummary,
  sortCompetitionEvents,
  sourceTypeLabels,
} from './domain'
import type {
  Claim,
  CompetitionEvent,
  Competitor,
  Evidence,
  MonitoringSource,
  Overview,
  OwnProduct,
  OwnProductDocument,
  FollowUpTurn,
  PreferenceItem,
  Preferences,
  PreferenceType,
  ResearchReport,
  ResearchState,
  TriageDecision,
} from './types'

type Route = { page: 'home' | 'competitors' | 'product' } | { page: 'research'; eventId: string }
type LoadState = 'loading' | 'ready' | 'error'
type ResearchExecutionState = 'idle' | 'starting' | 'running' | 'completed' | 'failed'

const preferenceTypeLabels: Record<PreferenceType, string> = {
  focus: '关注点', preference: '偏好', constraint: '限制条件', goal: '目标',
  decision: '决策', hypothesis: '待验证判断', open_question: '待确认问题',
}

const triageMeta: Record<TriageDecision, { label: string; className: string }> = {
  research: { label: '建议深入研究', className: 'badge-research' },
  monitor: { label: '继续监控', className: 'badge-monitor' },
  no_action: { label: '暂无需行动', className: 'badge-muted' },
}

function routeFromPath(pathname: string): Route {
  const match = pathname.match(/^\/research\/([^/]+)$/)
  if (match) return { page: 'research', eventId: decodeURIComponent(match[1]) }
  if (pathname === '/competitors') return { page: 'competitors' }
  if (pathname === '/product') return { page: 'product' }
  return { page: 'home' }
}

function formatDate(value?: string | null, withTime = false): string {
  if (!value) return '未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    ...(withTime ? { hour: '2-digit', minute: '2-digit' } : {}),
  }).format(date)
}

function ErrorNotice({ message, retry }: { message: string; retry?: () => void }) {
  return (
    <div role="alert" className="notice notice-error">
      <span>{message}</span>
      {retry && <button className="text-button" onClick={retry}>重试</button>}
    </div>
  )
}

function PageLoading({ label = '正在加载…' }: { label?: string }) {
  return <div className="page-state"><span className="spinner" />{label}</div>
}

function TriageBadge({ decision }: { decision: TriageDecision }) {
  const meta = triageMeta[decision]
  return <span className={`badge ${meta.className}`}>{meta.label}</span>
}

function EvidenceList({ evidence }: { evidence: Evidence[] }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="mt-4">
      <button className="evidence-toggle" onClick={() => setOpen(value => !value)} aria-expanded={open}>
        <span>证据</span>
        <span>{evidence.length} 条已确认</span>
        <span aria-hidden="true">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <div className="evidence-list">
          {evidence.map(item => (
            <article key={item.result_id} className="evidence-item">
              <div className="flex flex-wrap items-center gap-2">
                {item.url ? (
                  <a href={item.url} target="_blank" rel="noreferrer" className="source-link">{item.title}</a>
                ) : <strong>{item.title}</strong>}
                <span className="muted">{formatDate(item.observed_at)}</span>
                {item.credibility && (
                  <span className="mini-badge">{item.credibility === 'official' ? '官方来源' : '待确认来源'}</span>
                )}
              </div>
              <p>{item.content}</p>
            </article>
          ))}
        </div>
      )}
    </div>
  )
}

export function EventCard({ event, onResearch }: {
  event: CompetitionEvent
  onResearch: (event: CompetitionEvent) => void
}) {
  const triage = event.triage
  const researchCompleted = event.research?.status === 'completed'
  const pairedChanges = event.change_items.filter(item => item.previous_state && item.current_state)
  const explicitChanges = event.change_items.filter(item => !item.previous_state && item.current_state)
  return (
    <article className="event-card">
      <div className="flex items-start justify-between gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <strong className="text-sm text-zinc-950">{event.competitor}</strong>
          <span className="event-type">{eventTypeLabels[event.event_type] ?? event.event_type}</span>
          <span className="muted">{formatDate(event.event_time ?? event.observation_time)}</span>
          {event.discovery_origin === 'historical_backfill' && <span className="badge badge-muted">历史回溯</span>}
        </div>
        {triage && <TriageBadge decision={triage.decision} />}
      </div>
      <h2 className="event-title">{event.title}</h2>
      <p className="event-summary">{event.summary}</p>
      {pairedChanges.length > 0 && (
        <div className="state-grid">
          <div className="state-box"><span>之前</span><p>{pairedChanges.map(item => item.previous_state).join('；')}</p></div>
          <span className="state-arrow">→</span>
          <div className="state-box state-current"><span>现在</span><p>{pairedChanges.map(item => item.current_state).join('；')}</p></div>
        </div>
      )}
      {pairedChanges.length === 0 && explicitChanges.length > 0 && (
        <div className="explicit-change"><span>明确变化</span><p>{explicitChanges.map(item => item.current_state).join('；')}</p></div>
      )}
      <EvidenceList evidence={event.evidence} />
      {triage && (
        <div className="triage-row">
          <div className="triage-copy"><span>智能分诊</span><p>{triage.reason}</p></div>
          {triage.decision === 'research' && (
            <div className="triage-action"><button
              className={`${researchCompleted ? 'secondary-button' : 'primary-button'} research-action-button`}
              onClick={() => onResearch(event)}
            >{researchCompleted ? '查看研究' : '深入研究'}</button></div>
          )}
        </div>
      )}
    </article>
  )
}

export function HomePage({ onResearch }: { onResearch: (event: CompetitionEvent) => void }) {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')
  const [scanning, setScanning] = useState(false)
  const [scanMessage, setScanMessage] = useState('')
  const [triageFilter, setTriageFilter] = useState<'all' | TriageDecision>('all')
  const [competitor, setCompetitor] = useState('all')
  const [eventType, setEventType] = useState('all')
  const [days, setDays] = useState<number | null>(null)
  const [page, setPage] = useState(1)

  const load = useCallback(async () => {
    setState('loading')
    try {
      setOverview(await api.getOverview())
      setState('ready')
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '事件加载失败，请重试。')
      setState('error')
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const scan = async () => {
    if (scanning) return
    setScanning(true)
    setScanMessage('')
    try {
      const result = await api.scan()
      if (result.status !== 'complete') throw new Error(result.error?.message ?? '扫描失败，请重试。')
      setScanMessage(scanSummary(result))
      await load()
    } catch (reason) {
      setScanMessage(reason instanceof Error ? reason.message : '扫描失败，请重试。')
    } finally {
      setScanning(false)
    }
  }

  const allEvents = useMemo(() => sortCompetitionEvents(overview?.events ?? []), [overview])
  const competitors = useMemo(() => [...new Set(allEvents.map(item => item.competitor))], [allEvents])
  const eventTypes = useMemo(() => [...new Set(allEvents.map(item => item.event_type))], [allEvents])
  const filtered = allEvents.filter(item =>
    (triageFilter === 'all' || item.triage?.decision === triageFilter)
    && (competitor === 'all' || item.competitor === competitor)
    && (eventType === 'all' || item.event_type === eventType)
    && isWithinDays(item, days))
  const totalPages = Math.max(1, Math.ceil(filtered.length / 8))
  const visible = filtered.slice((page - 1) * 8, page * 8)

  if (state === 'loading' && !overview) return <PageLoading label="正在加载竞争事件…" />
  if (state === 'error' && !overview) return <main className="page"><ErrorNotice message={error} retry={load} /></main>

  return (
    <main className="page page-wide">
      <header className="page-header">
        <div>
          <h1>竞争事件</h1>
          <div className="page-meta">
            <span>已配置 {overview?.competitor_count ?? 0} 个竞品</span>
            <span>最近扫描：{formatDate(overview?.last_scan?.completed_at, true)}</span>
            {(overview?.pending_count ?? 0) > 0 && <span>待确认候选 {overview?.pending_count}</span>}
          </div>
        </div>
        <button className="primary-button scan-button" onClick={scan} disabled={scanning || !overview?.competitor_count}>
          {scanning && <span className="spinner spinner-light" />}{scanning ? '扫描中…' : '立即扫描'}
        </button>
      </header>
      {scanMessage && <div className={scanMessage.startsWith('扫描完成') ? 'notice notice-success' : 'notice notice-error'}>{scanMessage}</div>}
      {overview?.competitor_count === 0 ? (
        <div className="empty-state"><h2>请先添加竞品</h2><p>添加监控对象后即可开始扫描。</p></div>
      ) : (
        <>
          <div className="filters">
            <div className="segmented" aria-label="分诊筛选">
              {([
                ['all', '全部'], ['research', '建议深入研究'], ['monitor', '继续监控'], ['no_action', '暂无需行动'],
              ] as const).map(([value, label]) => (
                <button key={value} className={triageFilter === value ? 'active' : ''}
                  onClick={() => { setTriageFilter(value); setPage(1) }}>{label}</button>
              ))}
            </div>
            <div className="selects">
              <select aria-label="竞品" value={competitor} onChange={event => { setCompetitor(event.target.value); setPage(1) }}>
                <option value="all">全部竞品</option>{competitors.map(name => <option key={name}>{name}</option>)}
              </select>
              <select aria-label="事件类型" value={eventType} onChange={event => { setEventType(event.target.value); setPage(1) }}>
                <option value="all">全部类型</option>{eventTypes.map(type => <option key={type} value={type}>{eventTypeLabels[type] ?? type}</option>)}
              </select>
              <select aria-label="时间范围" value={days ?? 'all'} onChange={event => { setDays(event.target.value === 'all' ? null : Number(event.target.value)); setPage(1) }}>
                <option value="all">全部时间</option><option value="7">近 7 天</option><option value="30">近 30 天</option><option value="90">近 90 天</option>
              </select>
            </div>
          </div>
          <p className="list-count">共 {filtered.length} 条事件</p>
          {visible.length ? <div className="event-list">{visible.map(item => <EventCard key={item.event_id} event={item} onResearch={onResearch} />)}</div>
            : <div className="empty-state"><h2>暂无竞争事件</h2><p>可以调整筛选条件或发起扫描。</p></div>}
          {totalPages > 1 && <div className="pagination">
            <button disabled={page === 1} onClick={() => setPage(value => value - 1)}>上一页</button>
            <span>{page} / {totalPages}</span>
            <button disabled={page === totalPages} onClick={() => setPage(value => value + 1)}>下一页</button>
          </div>}
        </>
      )}
    </main>
  )
}

function SourceRow({ source }: { source: MonitoringSource }) {
  return <li><div><a href={source.url} target="_blank" rel="noreferrer">{source.title}</a><p>{sourceTypeLabels[source.source_type] ?? source.source_type}</p></div>
    <span className="mini-badge">{source.credibility === 'official' ? '官方来源' : '待确认来源'}</span></li>
}

export function CompetitorsPage() {
  const [items, setItems] = useState<Competitor[]>([])
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')
  const [adding, setAdding] = useState(false)
  const [saving, setSaving] = useState(false)
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [editing, setEditing] = useState<Competitor | null>(null)
  const [resetTarget, setResetTarget] = useState<Competitor | null>(null)
  const [resetting, setResetting] = useState(false)
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    try { setItems(await api.getCompetitors()); setState('ready'); setError('') }
    catch (reason) { setError(reason instanceof Error ? reason.message : '竞品加载失败。'); setState('error') }
  }, [])
  useEffect(() => { void load() }, [load])

  const add = async (event: React.FormEvent) => {
    event.preventDefault(); setSaving(true); setError('')
    try { await api.addCompetitor({ name: name.trim(), official_url: url.trim() }); setAdding(false); setName(''); setUrl(''); await load() }
    catch (reason) { setError(reason instanceof Error ? reason.message : '来源识别失败。') }
    finally { setSaving(false) }
  }

  const saveEdit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (!editing) return; setSaving(true)
    const form = new FormData(event.currentTarget)
    try {
      await api.updateCompetitor(editing.name, {
        official_url: String(form.get('official_url') ?? ''),
      })
      setEditing(null); await load()
    } catch (reason) { setError(reason instanceof Error ? reason.message : '竞品更新失败。') }
    finally { setSaving(false) }
  }

  const resetTestData = async () => {
    if (!resetTarget) return
    setResetting(true); setError(''); setNotice('')
    try {
      await api.resetCompetitorTestData(resetTarget.name)
      setResetTarget(null)
      setNotice('测试数据已重置，可重新扫描。')
      await load()
    } catch {
      setError('重置失败，未能完整清除测试数据。')
    } finally {
      setResetting(false)
    }
  }

  if (state === 'loading') return <PageLoading label="正在加载竞品…" />
  return <main className="page page-wide">
    <header className="page-header"><div><h1>竞品配置</h1><p>管理监控对象与信息来源</p></div><button className="primary-button" onClick={() => setAdding(true)}>添加竞品</button></header>
    {error && <ErrorNotice message={error} retry={state === 'error' ? load : undefined} />}
    {notice && <div role="status" className="notice notice-success">{notice}</div>}
    {adding && <form className="form-card" onSubmit={add}>
      <div className="form-grid"><label>竞品名称<input required value={name} onChange={event => setName(event.target.value)} /></label>
        <label>官网 URL<input required type="url" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://example.com" /></label></div>
      <div className="form-actions"><button type="button" className="secondary-button" onClick={() => setAdding(false)}>取消</button><button className="primary-button" disabled={saving}>{saving ? '正在识别来源…' : '识别并添加'}</button></div>
    </form>}
    {!items.length && !adding ? <div className="empty-state"><h2>暂无竞品</h2><p>添加监控对象与官网后开始来源识别。</p></div> :
      <div className="competitor-grid">{items.map(item => <article key={item.name} className="competitor-card">
        <div className="card-heading"><div><h2>{item.name}</h2>{item.official_url && <a href={item.official_url} target="_blank" rel="noreferrer">{item.official_url}</a>}</div><div className="card-actions">
          <button className="secondary-button small" onClick={() => setEditing(item)}>编辑</button>
          {import.meta.env.DEV && <button className="danger-secondary small" onClick={() => { setResetTarget(item); setNotice('') }}>重置测试数据</button>}
        </div></div>
        <div className="card-meta"><span>已识别来源 {item.sources.length}</span><span>最近扫描：{formatDate(item.last_scan_at, true)}</span></div>
        {item.sources.length ? <ul className="source-list">{item.sources.map(source => <SourceRow key={source.url} source={source} />)}</ul> : <p className="muted mt-4">尚未识别到可监控来源</p>}
      </article>)}</div>}
    {editing && <div className="modal-backdrop" role="presentation"><form className="modal" onSubmit={saveEdit}>
      <div><h2>编辑 {editing.name}</h2><p>更新官网会重新识别监控来源。</p></div>
      <label>官网 URL<input name="official_url" type="url" defaultValue={editing.official_url ?? ''} required /></label>
      <div className="form-actions"><button type="button" className="secondary-button" onClick={() => setEditing(null)}>取消</button><button className="primary-button" disabled={saving}>{saving ? '保存中…' : '保存'}</button></div>
    </form></div>}
    {resetTarget && <div className="modal-backdrop" role="presentation"><div className="modal" role="dialog" aria-modal="true" aria-labelledby="reset-title">
      <div><h2 id="reset-title">重置测试数据？</h2><p>将清除该竞品已发现的事件、研究结果和扫描状态，但保留竞品及来源配置。</p><p>下次扫描将按首次扫描重新处理。</p></div>
      <div className="form-actions"><button type="button" className="secondary-button" disabled={resetting} onClick={() => setResetTarget(null)}>取消</button>
        <button type="button" className="danger-button" disabled={resetting} onClick={() => void resetTestData()}>{resetting ? '正在重置…' : '确认重置'}</button></div>
    </div></div>}
  </main>
}

function DocumentRow({ document, replacing, onReplace }: {
  document: OwnProductDocument
  replacing: boolean
  onReplace: (document: OwnProductDocument, file: File) => void
}) {
  const status = documentStatus(document)
  return <div className="document-row">
    <div className="document-icon">{document.file_format.toUpperCase()}</div>
    <div className="document-main"><strong>{document.display_name}</strong><span>更新时间：{formatDate(document.updated_at, true)}</span>{document.failure_reason && <p>{document.failure_reason}</p>}</div>
    <span className={`status status-${status.tone}`}>{status.label}</span>
    {document.document_id && <label className="secondary-button small file-button">{replacing ? '更新中…' : '更新'}<input type="file" accept=".docx,.pdf,.xlsx,.md,.txt" disabled={replacing}
      onChange={event => { const file = event.target.files?.[0]; if (file) onReplace(document, file); event.currentTarget.value = '' }} /></label>}
  </div>
}

export function OwnProductPage() {
  const [product, setProduct] = useState<OwnProduct | null>(null)
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')
  const [uploading, setUploading] = useState(false)
  const [replacing, setReplacing] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [preferences, setPreferences] = useState<Preferences>({ confirmed_items: [], awaiting_confirmation_items: [] })
  const [preferenceTab, setPreferenceTab] = useState<'confirmed' | 'awaiting'>(() =>
    new URLSearchParams(window.location.search).get('preferences') === 'awaiting' ? 'awaiting' : 'confirmed')
  const [preferenceForm, setPreferenceForm] = useState<PreferenceItem | 'new' | null>(null)
  const [preferenceBusy, setPreferenceBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const [nextProduct, nextPreferences] = await Promise.all([api.getOwnProduct(), api.getPreferences()])
      setProduct(nextProduct); setPreferences(nextPreferences); setState('ready'); setError('')
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : '产品资料加载失败。'); setState('error') }
  }, [])
  useEffect(() => { void load() }, [load])

  const upload = async (file: File) => {
    setUploading(true); setError('')
    try { const result = await api.addDocument(file); if (result.failure_reason) setError(result.failure_reason); await load() }
    catch (reason) { setError(reason instanceof Error ? reason.message : '资料上传失败。') }
    finally { setUploading(false) }
  }
  const replace = async (document: OwnProductDocument, file: File) => {
    if (!document.document_id) return
    setReplacing(document.document_id); setError('')
    try { const result = await api.replaceDocument(document.document_id, file); if (result.failure_reason) setError(result.failure_reason); await load() }
    catch (reason) { setError(reason instanceof Error ? reason.message : '资料更新失败。') }
    finally { setReplacing(null) }
  }
  const saveConfiguration = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setSaving(true); setError('')
    try {
      const saved = await api.updateOwnProduct({
        product_name: String(form.get('product_name') ?? ''),
        current_focus: String(form.get('current_focus') ?? ''),
      })
      setProduct(saved)
      setEditing(false)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '产品信息保存失败。')
    } finally {
      setSaving(false)
    }
  }

  const savePreference = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const input = { type: String(form.get('type')) as PreferenceType, content: String(form.get('content') ?? '').trim() }
    if (!input.content) return
    setPreferenceBusy(true); setError('')
    try {
      if (preferenceForm === 'new') await api.addPreference(input)
      else if (preferenceForm) await api.editPreference(preferenceForm.item_id, input)
      setPreferences(await api.getPreferences()); setPreferenceForm(null)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '关注信息保存失败。') }
    finally { setPreferenceBusy(false) }
  }
  const preferenceAction = async (action: 'remove' | 'confirm' | 'ignore', item: PreferenceItem) => {
    setPreferenceBusy(true); setError('')
    try {
      if (action === 'remove') await api.removePreference(item.item_id)
      if (action === 'confirm') await api.confirmPreference(item.item_id)
      if (action === 'ignore') await api.ignorePreference(item.item_id)
      setPreferences(await api.getPreferences())
    } catch (reason) { setError(reason instanceof Error ? reason.message : '操作未完成。') }
    finally { setPreferenceBusy(false) }
  }

  if (state === 'loading') return <PageLoading label="正在加载产品资料…" />
  if (state === 'error' && !product) return <main className="page"><ErrorNotice message={error} retry={load} /></main>
  return <main className="page">
    <header className="page-header"><div><h1>自身产品</h1><p>管理产品信息与参考资料</p></div></header>
    {error && <ErrorNotice message={error} />}
    <section className="product-identity-card">
      <div className="section-heading"><div><h2>产品信息</h2><p>用于后续竞争事件的相关性判断</p></div>
        {!editing && <button className="secondary-button small" onClick={() => setEditing(true)}>编辑</button>}
      </div>
      {editing ? <form onSubmit={saveConfiguration}>
        <div className="product-config-form">
          <label>产品名称<input name="product_name" defaultValue={product?.product_name ?? ''} maxLength={200} placeholder="例如：Acme 智能协作平台" /></label>
          <label>当前关注重点<textarea name="current_focus" defaultValue={product?.current_focus ?? ''} maxLength={2000} rows={4} placeholder="例如：企业级智能体管理、权限体系与治理" /></label>
        </div>
        <div className="form-actions"><button type="button" className="secondary-button" onClick={() => setEditing(false)} disabled={saving}>取消</button><button className="primary-button" disabled={saving}>{saving ? '保存中…' : '保存'}</button></div>
      </form> : <div className="product-identity">
        <div><span>产品名称</span><strong>{product?.product_name ?? '尚未配置'}</strong></div>
        <div><span>当前关注重点</span><strong>{product?.current_focus ?? '尚未配置'}</strong></div>
      </div>}
    </section>
    <section className="section-card">
      <div className="section-heading"><div><h2>产品资料</h2><p>支持 DOCX、文本型 PDF、XLSX、Markdown 和 TXT</p></div>
        <label className="primary-button file-button">{uploading ? '上传中…' : '上传资料'}<input type="file" accept=".docx,.pdf,.xlsx,.md,.txt" disabled={uploading}
          onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file); event.currentTarget.value = '' }} /></label></div>
      {product?.documents.length ? <div className="document-list">{product.documents.map(item => <DocumentRow key={item.document_id ?? item.display_name} document={item} replacing={replacing === item.document_id} onReplace={replace} />)}</div>
        : <div className="empty-state compact"><h2>暂无产品资料</h2><p>上传资料后可用于分诊与深入研究。</p></div>}
    </section>
    <section className="section-card" id="preferences">
      <div className="section-heading"><div><h2>关注与偏好</h2><p>用于记录长期关注、工作偏好和判断原则，分诊与深入研究会参考这些信息。</p></div>
        <button className="primary-button" onClick={() => setPreferenceForm('new')}>＋ 添加关注与偏好</button></div>
      <div className="preference-tabs" role="tablist">
        <button className={preferenceTab === 'confirmed' ? 'active' : ''} onClick={() => setPreferenceTab('confirmed')}>已确认（{preferences.confirmed_items.length}）</button>
        <button className={preferenceTab === 'awaiting' ? 'active' : ''} onClick={() => setPreferenceTab('awaiting')}>待确认（{preferences.awaiting_confirmation_items.length}）</button>
      </div>
      <div className="preference-list">{(preferenceTab === 'confirmed' ? preferences.confirmed_items : preferences.awaiting_confirmation_items).map(item =>
        <article className="preference-row" key={item.item_id}><div><span className="preference-type">{preferenceTypeLabels[item.type]}</span><p>{item.content}</p><small>来源：{item.source} · 更新于 {formatDate(item.updated_at)}</small></div>
          <div className="preference-actions">{preferenceTab === 'confirmed' ? <><button className="secondary-button small" onClick={() => setPreferenceForm(item)}>编辑</button><button className="danger-secondary" disabled={preferenceBusy} onClick={() => void preferenceAction('remove', item)}>移除</button></>
            : <><button className="primary-button small" disabled={preferenceBusy} onClick={() => void preferenceAction('confirm', item)}>确认</button><button className="secondary-button small" disabled={preferenceBusy} onClick={() => void preferenceAction('ignore', item)}>忽略</button></>}</div>
        </article>)}
        {(preferenceTab === 'confirmed' ? preferences.confirmed_items : preferences.awaiting_confirmation_items).length === 0 && <div className="empty-state compact"><h2>{preferenceTab === 'confirmed' ? '暂无关注与偏好' : '暂无待确认内容'}</h2><p>{preferenceTab === 'confirmed' ? '你可以添加希望持续关注的方向。' : '研究交流中识别到的候选会出现在这里。'}</p></div>}
      </div>
    </section>
    {preferenceForm && <div className="modal-backdrop" role="presentation"><form className="modal" onSubmit={savePreference}><div><h2>{preferenceForm === 'new' ? '添加关注' : '编辑关注'}</h2><p>保存后会用于相关事件的研究优先级判断。</p></div>
      <label>类型<select name="type" defaultValue={preferenceForm === 'new' ? 'focus' : preferenceForm.type}>{Object.entries(preferenceTypeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>内容<textarea name="content" required maxLength={2000} rows={4} defaultValue={preferenceForm === 'new' ? '' : preferenceForm.content} placeholder="例如：持续关注企业级权限治理能力" /></label>
      <div className="form-actions"><button type="button" className="secondary-button" disabled={preferenceBusy} onClick={() => setPreferenceForm(null)}>取消</button><button className="primary-button" disabled={preferenceBusy}>{preferenceBusy ? '保存中…' : '保存'}</button></div>
    </form></div>}
  </main>
}

function credibilityLabel(value: Evidence['credibility']) {
  if (value === 'official') return '官方来源'
  if (value === 'unverified') return '待确认来源'
  return null
}

function researchCopy(value: string) {
  return value
    .replace(/\bsource_group\b(?:\s+source_[a-f0-9]{8,})?\s*/gi, '来源组')
    .replace(/\bsource_[a-f0-9]{8,}\b/gi, '')
    .replace(/\bresult_id\b/gi, '来源引用')
    .replace(/\bartifact[_ ]?id\b/gi, '研究产物')
    .replace(/\bchunk_id\b/gi, '文本片段')
    .replace(/\s*\btool[_ ]?trace\b\s*/gi, '研究过程')
    .replace(/\bown_product_comparison\b\s*/gi, '自身产品对照')
    .replace(/\s*\binsufficient_own_product_context\b/gi, '自身产品资料不足')
    .replace(/\bprevious_state\b/gi, '先前状态')
    .replace(/\bcurrent_state\b/gi, '当前状态')
    .replace(/\beffective_time\b/gi, '实际生效时间')
    .replace(/\b(?:research_evidence|event_evidence|knowledge|candidate)_\d+\b/gi, '证据')
    .replace(/\bknowledge\b/gi, '知识资料')
    .replace(/\bEvent\b/g, '事件证据')
    .replace(/\bnull\b/gi, '未记录')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

function plainEvidence(value: string) {
  return value
    .replace(/```[\s\S]*?```/g, block => block.replace(/```[^\n]*\n?|```/g, ' '))
    .replace(/<[^>]+>/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/gm, ' ')
    .replace(/^\s*#{1,6}\s+/gm, '')
    .replace(/^\s*(?:[-*+]\s+|>\s*)/gm, '')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^\s*\|(.+)\|\s*$/gm, (_, row: string) => row.split('|').map(cell => cell.trim()).filter(Boolean).join('；'))
    .replace(/[ \t]*\n[ \t]*/g, ' ')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

function EvidenceCard({ evidence, label, support, kind }: {
  evidence: Evidence
  label: string
  support: Claim['support'][number]
  kind: Claim['kind']
}) {
  const credibility = credibilityLabel(evidence.credibility)
  const historical = !support.evidence_summary
  const summary = plainEvidence(support.evidence_summary ?? support.excerpt)
  const proofLabel = historical ? '原文摘录' : kind === 'fact' ? '关键证据' : kind === 'inference' ? '依据' : '参考依据'
  return <article className="research-evidence-card">
    <div className="evidence-card-heading">
      <span className="evidence-id">{label}</span>
      {evidence.url ? <a href={evidence.url} target="_blank" rel="noreferrer">{evidence.title}</a> : <strong>{evidence.title}</strong>}
      {credibility && <span className="mini-badge">{credibility}</span>}
      {evidence.observed_at && <span className="evidence-date">{formatDate(evidence.observed_at)}</span>}
    </div>
    <p className="evidence-proof"><strong>{proofLabel}</strong><span>{summary}</span></p>
    {evidence.url && <div className="evidence-actions"><a href={evidence.url} target="_blank" rel="noreferrer">查看来源</a></div>}
  </article>
}

function ClaimList({ claims, citations, labels }: {
  claims: Claim[]
  citations: Map<string, Evidence>
  labels: Map<string, string>
}) {
  return <div className="claim-list">{claims.map((claim, index) => <article className="claim" key={`${claim.text}-${index}`}>
    <div className="claim-heading">
      <span className={`claim-kind claim-${claim.kind}`}>{claim.kind === 'fact' ? '事实' : claim.kind === 'inference' ? '推断' : '产品建议'}</span>
      <p>{researchCopy(claim.text)}</p>
    </div>
    {claim.support.length > 0 ? <div className="claim-evidence">{claim.support.map((support, supportIndex) => {
      const source = citations.get(support.result_id)
      if (!source) return <p className="evidence-missing" key={`${support.result_id}-${supportIndex}`}>当前证据不足，仍需验证。</p>
      return <EvidenceCard
        key={`${support.result_id}-${support.excerpt}-${supportIndex}`}
        evidence={source}
        label={labels.get(support.result_id) ?? `E${supportIndex + 1}`}
        support={support}
        kind={claim.kind}
      />
    })}</div> : claim.kind === 'inference' && <p className="evidence-missing">当前事实依据不足，仍需验证。</p>}
  </article>)}</div>
}

function WatchList({ claims, labels, prefix }: { claims: Claim[]; labels: Map<string, string>; prefix: string }) {
  return <ul className="watch-list">{claims.map((claim, index) => <li key={`${claim.text}-${index}`}>
    <span className="claim-kind claim-pending">{prefix}</span><span>{researchCopy(claim.text)}</span>
    {claim.support.map(item => labels.get(item.result_id)).filter(Boolean).map(label => <span className="citation-label" key={label}>{label}</span>)}
  </li>)}</ul>
}

function ResearchSummary({ claims }: { claims: Claim[] }) {
  return <div className="research-summary">{claims.map((claim, index) =>
    <p key={`${claim.text}-${index}`}>{researchCopy(claim.text)}</p>)}</div>
}

function ResearchResult({ report, evidence }: { report: ResearchReport; evidence: Evidence[] }) {
  const citations = new Map(evidence.map(item => [item.result_id, item]))
  const labels = new Map(evidence.map((item, index) => [item.result_id, `E${index + 1}`]))
  const findings = [...report.background, ...report.competitor_context]
  const impact = report.implications.filter(claim => claim.kind === 'inference')
  const recommendations = report.implications.filter(claim => claim.kind === 'recommendation')
  return <div className="research-report">
    <section><h2>研究摘要</h2><ResearchSummary claims={report.event_summary} /></section>
    {findings.length > 0 && <section><h2>关键发现</h2><ClaimList claims={findings} citations={citations} labels={labels} /></section>}
    <section className={report.own_product_comparison.status === 'supported' ? '' : 'research-callout'}><h2>对自身产品的影响</h2>{report.own_product_comparison.status === 'supported'
      ? <><ClaimList claims={report.own_product_comparison.claims} citations={citations} labels={labels} />
        {impact.length > 0 && <ClaimList claims={impact} citations={citations} labels={labels} />}</>
      : <p>当前自身产品资料不足，暂无法判断对应影响。</p>}</section>
    {recommendations.length > 0 && <section><h2>产品建议</h2><ClaimList claims={recommendations} citations={citations} labels={labels} /></section>}
    {(report.watch_next.length > 0 || report.uncertainties.length > 0) && <section><h2>下一步关注</h2>
      {report.watch_next.length > 0 && <WatchList claims={report.watch_next} labels={labels} prefix="重点跟踪" />}
      {report.uncertainties.length > 0 && <ul className="watch-list">{report.uncertainties.map(item => <li key={item}><span className="claim-kind claim-pending">待验证</span><span>{researchCopy(item)}</span></li>)}</ul>}
    </section>}
    {evidence.length > 0 && <details className="source-index"><summary>全部来源（{evidence.length}）</summary><ol>{evidence.map(item => <li key={item.result_id}>
      <span className="evidence-id">{labels.get(item.result_id)}</span><span className="source-index-copy"><strong>{item.title}</strong><span>{credibilityLabel(item.credibility) ?? '已核验来源'}{item.observed_at ? ` · ${formatDate(item.observed_at)}` : ''}</span></span>
      {item.url && <a href={item.url} target="_blank" rel="noreferrer">查看来源</a>}
    </li>)}</ol></details>}
  </div>
}

function FollowUpResult({ turn }: { turn: FollowUpTurn }) {
  const evidence = turn.citations ?? []
  const citations = new Map(evidence.map(item => [item.result_id, item]))
  const labels = new Map(evidence.map((item, index) => [item.result_id, `E${index + 1}`]))
  return <div className="follow-up-answer">
    {turn.result && <ClaimList claims={turn.result.answer} citations={citations} labels={labels} />}
    {!!turn.result?.uncertainties.length && <div className="follow-up-uncertainties"><strong>仍需确认</strong><ul>{turn.result.uncertainties.map(item => <li key={item}>{researchCopy(item)}</li>)}</ul></div>}
    {evidence.length > 0 && <details className="source-index"><summary>参考来源（{evidence.length}）</summary><ol>{evidence.map((item, index) => <li key={item.result_id}>
      <span className="evidence-id">E{index + 1}</span><span className="source-index-copy"><strong>{item.title}</strong><span>{credibilityLabel(item.credibility) ?? '已核验来源'}</span></span>
      {item.url && <a href={item.url} target="_blank" rel="noreferrer">查看来源</a>}
    </li>)}</ol></details>}
  </div>
}

export function ResearchPage({ eventId, onBack }: { eventId: string; onBack: () => void }) {
  const [event, setEvent] = useState<CompetitionEvent | null>(null)
  const [research, setResearch] = useState<ResearchState | null>(null)
  const [executionState, setExecutionState] = useState<ResearchExecutionState>('idle')
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')
  const [researchTab, setResearchTab] = useState<'result' | 'follow-up'>('result')
  const [turns, setTurns] = useState<FollowUpTurn[]>([])
  const [question, setQuestion] = useState('')
  const [followUpError, setFollowUpError] = useState('')
  const [sending, setSending] = useState(false)
  const [candidateNotice, setCandidateNotice] = useState(0)
  const loadVersion = useRef(0)
  const activeEventId = useRef(eventId)
  const startRequest = useRef<{ eventId: string; promise: Promise<ResearchState | null> } | null>(null)
  activeEventId.current = eventId

  const applyResearchState = useCallback((value: ResearchState) => {
    setResearch(value)
    if (value.status === 'completed') setExecutionState('completed')
    else if (value.status === 'failed') setExecutionState('failed')
    else if (value.status === 'running') setExecutionState('running')
    else setExecutionState('idle')
  }, [])

  const start = useCallback((current: ResearchState, retry: boolean) => {
    // starting 是前端等待 HTTP 的状态，不改变 Backend Research lifecycle。
    // 它先于请求发出而设置，因此真实深度研究返回前 Body 也始终可见。
    setExecutionState('starting')
    setError('')
    if (startRequest.current?.eventId === eventId) return

    const promise = api.startResearch(eventId, retry)
    startRequest.current = { eventId, promise }
    void promise.then(result => {
      if (activeEventId.current === eventId && result) applyResearchState(result)
    }).catch(reason => {
      if (activeEventId.current !== eventId) return
      setError(reason instanceof Error ? reason.message : '深入研究失败。')
      setResearch({ ...current, status: 'failed' })
      setExecutionState('failed')
    }).finally(() => {
      if (startRequest.current?.promise === promise) startRequest.current = null
    })
  }, [applyResearchState, eventId])

  const load = useCallback(async () => {
    const version = ++loadVersion.current
    setState('loading')
    try {
      const overview = await api.getOverview()
      if (version !== loadVersion.current) return
      const selected = overview.events.find(item => item.event_id === eventId)
      if (!selected) throw new Error('未找到该竞争事件。')
      setEvent(selected)
      const current = await api.getResearch(eventId)
      if (version !== loadVersion.current) return
      if (!current) throw new Error('该事件不支持深入研究。')
      setError('')
      setResearch(current)
      if (current.status === 'not_started') {
        setState('ready')
        start(current, false)
        return
      }
      applyResearchState(current)
      setState('ready')
    } catch (reason) {
      if (version !== loadVersion.current) return
      setError(reason instanceof Error ? reason.message : '深入研究未完成。')
      setState('error')
    }
  }, [applyResearchState, eventId, start])
  useEffect(() => { void load() }, [load])

  const loadFollowUps = useCallback(async () => {
    try {
      const conversation = await api.getFollowUps(eventId)
      setTurns(conversation.turns)
    } catch (reason) {
      setFollowUpError(reason instanceof Error ? reason.message : '对话记录加载失败。')
    }
  }, [eventId])
  useEffect(() => {
    if (research?.status === 'completed') void loadFollowUps()
  }, [loadFollowUps, research?.status])

  const sendFollowUp = async () => {
    const text = question.trim()
    if (!text || sending) return
    const requestId = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
    const optimistic: FollowUpTurn = {
      turn_id: `local-${requestId}`, user_message: text, status: 'running', candidate_count: 0,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    }
    setTurns(current => [...current, optimistic]); setQuestion(''); setSending(true); setFollowUpError('')
    try {
      const saved = await api.sendFollowUp(eventId, { request_id: requestId, question: text })
      setTurns(current => current.map(item => item.turn_id === optimistic.turn_id ? saved : item))
      if (saved.candidate_count > 0) setCandidateNotice(saved.candidate_count)
    } catch (reason) {
      setTurns(current => current.filter(item => item.turn_id !== optimistic.turn_id))
      setQuestion(text)
      setFollowUpError(reason instanceof Error ? reason.message : '继续研究未完成。')
    } finally { setSending(false) }
  }
  const retryFollowUp = async (turnId: string) => {
    setTurns(current => current.map(item => item.turn_id === turnId ? { ...item, status: 'running' } : item))
    setSending(true); setFollowUpError('')
    try {
      const saved = await api.retryFollowUp(eventId, turnId)
      setTurns(current => current.map(item => item.turn_id === turnId ? saved : item))
    } catch (reason) {
      setTurns(current => current.map(item => item.turn_id === turnId ? { ...item, status: 'failed' } : item))
      setFollowUpError(reason instanceof Error ? reason.message : '重试未完成。')
    } finally { setSending(false) }
  }
  const openPreferences = () => {
    window.history.pushState({}, '', '/product?preferences=awaiting')
    window.dispatchEvent(new PopStateEvent('popstate'))
  }

  const retry = () => {
    setResearch(current => current ? { ...current, status: 'running' } : current)
    if (research) start(research, true)
  }

  const showRunning = executionState === 'starting' || executionState === 'running'

  if (state === 'loading' && !event) return <PageLoading label="正在打开研究…" />
  if (state === 'error' && !event) return <main className="research-page"><button className="back-button" onClick={onBack}>← 返回首页</button><ErrorNotice message={error} retry={load} /></main>
  return <main className="research-page">
    <header className="research-header"><div className="research-width"><button className="back-button" onClick={onBack}>← 返回首页</button>
      <div className="flex items-start justify-between gap-6"><div><span className="eyebrow">深入研究事件</span><h1>{event?.title}</h1><div className="page-meta"><span>{event?.competitor}</span><span>{event ? eventTypeLabels[event.event_type] ?? event.event_type : ''}</span><span>{formatDate(event?.event_time ?? event?.observation_time)}</span><span className="confirmed">已确认</span></div></div>
        {research?.status === 'failed' && <button className="secondary-button" onClick={retry}>重新研究</button>}</div></div></header>
    {research?.status === 'completed' && <div className="research-subnav"><div className="research-width"><button className={researchTab === 'result' ? 'active' : ''} onClick={() => setResearchTab('result')}>研究结果</button><button className={researchTab === 'follow-up' ? 'active' : ''} onClick={() => setResearchTab('follow-up')}>继续追问</button></div></div>}
    <div className="research-body research-width">
      {error && <ErrorNotice message={error} />}
      {showRunning && <div className="research-running"><span className="spinner" /><div className="research-running-copy"><div><h2>正在深入研究</h2><span className="badge badge-research">研究中</span></div><p>正在搜索、核验并整理与该事件相关的信息，研究完成后结果会自动显示在这里。</p><dl><div><dt>研究对象</dt><dd>{event?.title}</dd></div><div><dt>已有证据</dt><dd>{event?.evidence.length ?? 0} 条</dd></div></dl></div></div>}
      {research?.status === 'failed' && <div className="empty-state"><h2>研究失败</h2><p>本次深入研究未完成，事件及已有证据不受影响。</p><button className="primary-button" onClick={retry}>重新研究</button></div>}
      {research?.status === 'completed' && research.result && researchTab === 'result' && <ResearchResult report={research.result} evidence={research.citations ?? []} />}
      {research?.status === 'completed' && researchTab === 'follow-up' && <div className="follow-up-conversation">
        {candidateNotice > 0 && <div className="notice notice-success"><span>识别到 {candidateNotice} 条待确认的关注信息。</span><button className="text-button" onClick={openPreferences}>去确认</button></div>}
        {followUpError && <ErrorNotice message={followUpError} />}
        {turns.length === 0 && <div className="follow-up-empty"><h2>继续追问本次研究</h2><p>围绕本次事件继续提问。如现有证据不足，系统会继续查证相关资料。</p><div className="suggested-questions">{['这次变化对我们当前产品影响最大的是什么？', '还有哪些关键信息没有确认？', '如果继续跟踪，最值得关注什么？'].map(item => <button key={item} onClick={() => setQuestion(item)}>{item}</button>)}</div></div>}
        {turns.map(turn => <div className="conversation-turn" key={turn.turn_id}><div className="user-message">{turn.user_message}</div><div className="assistant-message">
          {turn.status === 'running' && <div className="follow-up-running"><span className="spinner" />正在继续研究…</div>}
          {turn.status === 'completed' && <FollowUpResult turn={turn} />}
          {turn.status === 'failed' && <div className="follow-up-failed"><p>{turn.error?.message ?? '本次继续研究未完成。'}</p><button className="secondary-button small" disabled={sending} onClick={() => void retryFollowUp(turn.turn_id)}>重试</button></div>}
        </div></div>)}
      </div>}
    </div>
    {research?.status === 'completed' && researchTab === 'follow-up' && <div className="followup"><div className="research-width"><textarea value={question} rows={2} placeholder="围绕本次事件继续提问…" aria-label="围绕本次事件继续提问" disabled={sending} onChange={event => setQuestion(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void sendFollowUp() } }} /><button className="primary-button" disabled={sending || !question.trim()} onClick={() => void sendFollowUp()}>{sending ? '正在继续研究…' : '发送'}</button></div></div>}
  </main>
}

function Sidebar({ route, navigate }: { route: Route; navigate: (path: string) => void }) {
  const items = [
    { page: 'home', path: '/', label: '首页', icon: '⌂' },
    { page: 'competitors', path: '/competitors', label: '竞品配置', icon: '◇' },
    { page: 'product', path: '/product', label: '自身产品', icon: '□' },
  ] as const
  return <aside className="sidebar"><div className="brand"><span className="brand-mark">P</span><strong>产品情报助手</strong></div>
    <nav>{items.map(item => <button key={item.page} onClick={() => navigate(item.path)} className={route.page === item.page ? 'active' : ''}><span>{item.icon}</span>{item.label}</button>)}</nav>
  </aside>
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => routeFromPath(window.location.pathname))
  useEffect(() => {
    const listener = () => setRoute(routeFromPath(window.location.pathname))
    window.addEventListener('popstate', listener)
    return () => window.removeEventListener('popstate', listener)
  }, [])
  const navigate = (path: string) => { window.history.pushState({}, '', path); setRoute(routeFromPath(path)) }
  const openResearch = (event: CompetitionEvent) => navigate(`/research/${encodeURIComponent(event.event_id)}`)
  return <div className="app-shell"><Sidebar route={route} navigate={navigate} /><div className="content">
    {route.page === 'home' && <HomePage onResearch={openResearch} />}
    {route.page === 'competitors' && <CompetitorsPage />}
    {route.page === 'product' && <OwnProductPage />}
    {route.page === 'research' && <ResearchPage eventId={route.eventId} onBack={() => navigate('/')} />}
  </div></div>
}
