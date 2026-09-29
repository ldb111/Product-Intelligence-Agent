import type {
  Competitor,
  Overview,
  OwnProduct,
  OwnProductDocument,
  FollowUpConversation,
  FollowUpTurn,
  PreferenceItem,
  Preferences,
  PreferenceType,
  ResearchState,
  ScanResult,
  TestResetResult,
} from '../types'

const baseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
export const workspaceId = import.meta.env.VITE_PIA_WORKSPACE_ID ?? 'default'
export const productId = import.meta.env.VITE_PIA_PRODUCT_ID ?? 'main'

interface Envelope<T> { data: T }

export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: init?.body instanceof FormData
      ? init.headers
      : { 'Content-Type': 'application/json', ...init?.headers },
  })
  const body = await response.json().catch(() => null) as Envelope<T> | { code?: string; message?: string } | null
  if (!response.ok) {
    const error = body && 'message' in body ? body : null
    throw new ApiError(error?.code ?? 'request_failed', error?.message ?? '请求未完成，请重试。', response.status)
  }
  if (!body || typeof body !== 'object' || !('data' in body)) {
    throw new ApiError('invalid_response', '服务返回的数据不完整，请重试。', response.status)
  }
  return body.data
}

const root = (workspace = workspaceId) => `/api/workspaces/${encodeURIComponent(workspace)}`

export const api = {
  getOverview: () => request<Overview>(`${root()}/overview`),
  scan: () => request<ScanResult>(`${root()}/scan`, { method: 'POST' }),
  getCompetitors: () => request<Competitor[]>(`${root()}/competitors`),
  addCompetitor: (input: { name: string; official_url: string }) =>
    request<Competitor>(`${root()}/competitors`, {
      method: 'POST',
      body: JSON.stringify({ ...input, own_product_id: productId }),
    }),
  updateCompetitor: (name: string, input: Partial<Competitor>) =>
    request<Competitor>(`${root()}/competitors/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify(input),
    }),
  resetCompetitorTestData: (name: string) =>
    request<TestResetResult>(`${root()}/competitors/${encodeURIComponent(name)}/reset-test-data`, {
      method: 'POST',
    }),
  getOwnProduct: () => request<OwnProduct>(`${root()}/own-product?product_id=${encodeURIComponent(productId)}`),
  updateOwnProduct: (input: { product_name: string; current_focus: string }) =>
    request<OwnProduct>(`${root()}/own-product?product_id=${encodeURIComponent(productId)}`, {
      method: 'PUT', body: JSON.stringify(input),
    }),
  addDocument: (file: File) => {
    const form = new FormData()
    form.set('product_id', productId)
    form.set('display_name', file.name)
    form.set('file', file)
    return request<OwnProductDocument>(`${root()}/own-product/documents`, { method: 'POST', body: form })
  },
  replaceDocument: (documentId: string, file: File) => {
    const form = new FormData()
    form.set('product_id', productId)
    form.set('file', file)
    return request<OwnProductDocument>(
      `${root()}/own-product/documents/${encodeURIComponent(documentId)}`,
      { method: 'PUT', body: form },
    )
  },
  getPreferences: () => request<Preferences>(
    `${root()}/own-product/preferences?product_id=${encodeURIComponent(productId)}`,
  ),
  addPreference: (input: { type: PreferenceType; content: string }) => request<PreferenceItem>(
    `${root()}/own-product/preferences?product_id=${encodeURIComponent(productId)}`,
    { method: 'POST', body: JSON.stringify(input) },
  ),
  editPreference: (itemId: string, input: { type: PreferenceType; content: string }) => request<PreferenceItem>(
    `${root()}/own-product/preferences/${encodeURIComponent(itemId)}?product_id=${encodeURIComponent(productId)}`,
    { method: 'PUT', body: JSON.stringify(input) },
  ),
  removePreference: (itemId: string) => request<{ removed: boolean }>(
    `${root()}/own-product/preferences/${encodeURIComponent(itemId)}?product_id=${encodeURIComponent(productId)}`,
    { method: 'DELETE' },
  ),
  confirmPreference: (itemId: string) => request<PreferenceItem>(
    `${root()}/own-product/preferences/${encodeURIComponent(itemId)}/confirm?product_id=${encodeURIComponent(productId)}`,
    { method: 'POST' },
  ),
  ignorePreference: (itemId: string) => request<{ ignored: boolean }>(
    `${root()}/own-product/preferences/${encodeURIComponent(itemId)}/ignore?product_id=${encodeURIComponent(productId)}`,
    { method: 'POST' },
  ),
  getResearch: async (eventId: string) => {
    const value = await request<{ accepted: boolean; research?: ResearchState }>(
      `${root()}/events/${encodeURIComponent(eventId)}/research`,
    )
    return value.research ?? null
  },
  startResearch: async (eventId: string, retry = false) => {
    const value = await request<{ accepted: boolean; research?: ResearchState }>(
      `${root()}/events/${encodeURIComponent(eventId)}/research`,
      { method: 'POST', body: JSON.stringify({ retry }) },
    )
    return value.research ?? null
  },
  getFollowUps: (eventId: string) => request<FollowUpConversation>(
    `${root()}/events/${encodeURIComponent(eventId)}/research/follow-ups`,
  ),
  sendFollowUp: (eventId: string, input: { request_id: string; question: string }) => request<FollowUpTurn>(
    `${root()}/events/${encodeURIComponent(eventId)}/research/follow-ups`,
    { method: 'POST', body: JSON.stringify(input) },
  ),
  retryFollowUp: (eventId: string, turnId: string) => request<FollowUpTurn>(
    `${root()}/events/${encodeURIComponent(eventId)}/research/follow-ups/${encodeURIComponent(turnId)}/retry`,
    { method: 'POST' },
  ),
}
