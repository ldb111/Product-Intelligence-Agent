import type {
  Competitor,
  Overview,
  OwnProduct,
  OwnProductDocument,
  ResearchState,
  ScanResult,
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
  return (body as Envelope<T>).data
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
}
