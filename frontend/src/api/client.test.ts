import { afterEach, expect, it, vi } from 'vitest'
import { api } from './client'

afterEach(() => vi.unstubAllGlobals())

it('拒绝没有正式 data envelope 的成功响应，避免页面收到 undefined', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 200 })))
  await expect(api.getOverview()).rejects.toThrow('服务返回的数据不完整，请重试。')
})
