import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from './api/client'
import {
  documentStatus,
  eventTypeLabels,
  isWithinDays,
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
  ResearchReport,
  ResearchState,
  TriageDecision,
} from './types'

type Route = { page: 'home' | 'competitors' | 'product' } | { page: 'research'; eventId: string }
type LoadState = 'loading' | 'ready' | 'error'

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
          <div><span>智能分诊</span><p>{triage.reason}</p></div>
          {triage.decision === 'research' && (
            <button className={researchCompleted ? 'secondary-button' : 'primary-button'} onClick={() => onResearch(event)}>
              {researchCompleted ? '查看研究' : '深入研究'}
            </button>
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
      setScanMessage(`扫描完成 · 发现 ${result.new_event_count} 条新事件`)
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

  if (state === 'loading') return <PageLoading label="正在加载竞品…" />
  return <main className="page page-wide">
    <header className="page-header"><div><h1>竞品配置</h1><p>管理监控对象与信息来源</p></div><button className="primary-button" onClick={() => setAdding(true)}>添加竞品</button></header>
    {error && <ErrorNotice message={error} retry={state === 'error' ? load : undefined} />}
    {adding && <form className="form-card" onSubmit={add}>
      <div className="form-grid"><label>竞品名称<input required value={name} onChange={event => setName(event.target.value)} /></label>
        <label>官网 URL<input required type="url" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://example.com" /></label></div>
      <div className="form-actions"><button type="button" className="secondary-button" onClick={() => setAdding(false)}>取消</button><button className="primary-button" disabled={saving}>{saving ? '正在识别来源…' : '识别并添加'}</button></div>
    </form>}
    {!items.length && !adding ? <div className="empty-state"><h2>暂无竞品</h2><p>添加监控对象与官网后开始来源识别。</p></div> :
      <div className="competitor-grid">{items.map(item => <article key={item.name} className="competitor-card">
        <div className="card-heading"><div><h2>{item.name}</h2>{item.official_url && <a href={item.official_url} target="_blank" rel="noreferrer">{item.official_url}</a>}</div><button className="secondary-button small" onClick={() => setEditing(item)}>编辑</button></div>
        <div className="card-meta"><span>已识别来源 {item.sources.length}</span><span>最近扫描：{formatDate(item.last_scan_at, true)}</span></div>
        {item.sources.length ? <ul className="source-list">{item.sources.map(source => <SourceRow key={source.url} source={source} />)}</ul> : <p className="muted mt-4">尚未识别到可监控来源</p>}
      </article>)}</div>}
    {editing && <div className="modal-backdrop" role="presentation"><form className="modal" onSubmit={saveEdit}>
      <div><h2>编辑 {editing.name}</h2><p>更新官网会重新识别监控来源。</p></div>
      <label>官网 URL<input name="official_url" type="url" defaultValue={editing.official_url ?? ''} required /></label>
      <div className="form-actions"><button type="button" className="secondary-button" onClick={() => setEditing(null)}>取消</button><button className="primary-button" disabled={saving}>{saving ? '保存中…' : '保存'}</button></div>
    </form></div>}
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

  const load = useCallback(async () => {
    try { setProduct(await api.getOwnProduct()); setState('ready'); setError('') }
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
          <label>产品名称<input name="product_name" defaultValue={product?.product_name ?? ''} maxLength={200} placeholder="例如：Acme Agent Platform" /></label>
          <label>当前研究重点<textarea name="current_focus" defaultValue={product?.current_focus ?? ''} maxLength={2000} rows={4} placeholder="例如：企业级 Agent 管理、权限体系、Agent 治理" /></label>
        </div>
        <div className="form-actions"><button type="button" className="secondary-button" onClick={() => setEditing(false)} disabled={saving}>取消</button><button className="primary-button" disabled={saving}>{saving ? '保存中…' : '保存'}</button></div>
      </form> : <div className="product-identity">
        <div><span>产品名称</span><strong>{product?.product_name ?? '尚未配置'}</strong></div>
        <div><span>当前研究重点</span><strong>{product?.current_focus ?? '尚未配置'}</strong></div>
      </div>}
    </section>
    <section className="section-card">
      <div className="section-heading"><div><h2>产品资料</h2><p>支持 DOCX、文本型 PDF、XLSX、Markdown 和 TXT</p></div>
        <label className="primary-button file-button">{uploading ? '上传中…' : '上传资料'}<input type="file" accept=".docx,.pdf,.xlsx,.md,.txt" disabled={uploading}
          onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file); event.currentTarget.value = '' }} /></label></div>
      {product?.documents.length ? <div className="document-list">{product.documents.map(item => <DocumentRow key={item.document_id ?? item.display_name} document={item} replacing={replacing === item.document_id} onReplace={replace} />)}</div>
        : <div className="empty-state compact"><h2>暂无产品资料</h2><p>上传资料后可用于分诊与深入研究。</p></div>}
    </section>
  </main>
}

function ClaimList({ claims, citations }: { claims: Claim[]; citations: Map<string, Evidence> }) {
  return <div className="claim-list">{claims.map((claim, index) => <div className="claim" key={`${claim.text}-${index}`}>
    <span className={`claim-kind claim-${claim.kind}`}>{claim.kind === 'fact' ? '事实' : claim.kind === 'inference' ? '推断' : '建议'}</span>
    <div><p>{claim.text}</p>{claim.support.length > 0 && <div className="claim-sources">{claim.support.map(support => {
      const source = citations.get(support.result_id)
      return <span key={`${support.result_id}-${support.excerpt}`}>{source?.title ?? '已确认来源'}</span>
    })}</div>}</div>
  </div>)}</div>
}

function ResearchResult({ report, evidence }: { report: ResearchReport; evidence: Evidence[] }) {
  const citations = new Map(evidence.map(item => [item.result_id, item]))
  const sections: Array<[string, Claim[]]> = [
    ['研究摘要', report.event_summary], ['背景', report.background], ['竞品情况', report.competitor_context],
    ['产品启示', report.implications], ['后续关注', report.watch_next],
  ]
  return <div className="research-report">
    {sections.filter(([, claims]) => claims.length).map(([title, claims]) => <section key={title}><h2>{title}</h2><ClaimList claims={claims} citations={citations} /></section>)}
    <section><h2>与自身产品的关系</h2>{report.own_product_comparison.status === 'supported'
      ? <ClaimList claims={report.own_product_comparison.claims} citations={citations} />
      : <p className="muted">当前自身产品资料不足，暂无法判断对应能力情况。</p>}</section>
    {report.uncertainties.length > 0 && <section><h2>仍需确认</h2><ul className="bullet-list">{report.uncertainties.map(item => <li key={item}>{item}</li>)}</ul></section>}
    <section><h2>证据</h2><div className="research-evidence">{evidence.map(item => <article key={item.result_id}><a href={item.url ?? undefined} target="_blank" rel="noreferrer">{item.title}</a><p>{item.content}</p></article>)}</div></section>
  </div>
}

export function ResearchPage({ eventId, onBack }: { eventId: string; onBack: () => void }) {
  const [event, setEvent] = useState<CompetitionEvent | null>(null)
  const [research, setResearch] = useState<ResearchState | null>(null)
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')
  const started = useRef(false)

  const load = useCallback(async () => {
    setState('loading')
    try {
      const overview = await api.getOverview()
      const selected = overview.events.find(item => item.event_id === eventId)
      if (!selected) throw new Error('未找到该竞争事件。')
      setEvent(selected)
      const current = await api.getResearch(eventId)
      if (!current) throw new Error('该事件不支持深入研究。')
      setResearch(current)
      if (current.status === 'not_started' && !started.current) {
        started.current = true
        setResearch({ ...current, status: 'running' })
        const result = await api.startResearch(eventId, false)
        if (result) setResearch(result)
      }
      setState('ready')
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '深入研究未完成。')
      setState('error')
    }
  }, [eventId])
  useEffect(() => { void load() }, [load])

  const retry = async () => {
    setResearch(current => current ? { ...current, status: 'running' } : current)
    setError('')
    try { const result = await api.startResearch(eventId, true); if (result) setResearch(result) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '深入研究失败。') }
  }

  if (state === 'loading' && !event) return <PageLoading label="正在打开研究…" />
  if (state === 'error' && !event) return <main className="research-page"><button className="back-button" onClick={onBack}>← 返回首页</button><ErrorNotice message={error} retry={load} /></main>
  return <main className="research-page">
    <header className="research-header"><div className="research-width"><button className="back-button" onClick={onBack}>← 返回首页</button>
      <div className="flex items-start justify-between gap-6"><div><span className="eyebrow">深入研究事件</span><h1>{event?.title}</h1><div className="page-meta"><span>{event?.competitor}</span><span>{event ? eventTypeLabels[event.event_type] ?? event.event_type : ''}</span><span>{formatDate(event?.event_time ?? event?.observation_time)}</span><span className="confirmed">已确认</span></div></div>
        {research?.status === 'failed' && <button className="secondary-button" onClick={retry}>重新研究</button>}</div></div></header>
    <div className="research-body research-width">
      {error && <ErrorNotice message={error} />}
      {research?.status === 'running' && <div className="research-running"><span className="spinner" /><div><strong>研究中</strong><p>正在搜索、核验并整理相关信息…</p></div></div>}
      {research?.status === 'failed' && <div className="empty-state"><h2>研究失败</h2><p>本次深入研究未完成，事件及已有证据不受影响。</p><button className="primary-button" onClick={retry}>重新研究</button></div>}
      {research?.status === 'completed' && research.result && <ResearchResult report={research.result} evidence={research.citations ?? []} />}
    </div>
    {research?.status === 'completed' && <div className="followup"><div className="research-width"><input disabled placeholder="继续追问这个事件" aria-label="继续追问这个事件" /><button disabled>发送</button></div></div>}
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
