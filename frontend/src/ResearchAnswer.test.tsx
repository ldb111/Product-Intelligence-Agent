import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { AnswerText, FollowUpResult } from './ResearchAnswer'
import type { FollowUpTurn } from './types'

describe('追问正文与按需证据', () => {
  it('短回答保持段落，三点回答使用真实编号列表', () => {
    const view = render(<AnswerText text="这是简短解释。" />)
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
    view.rerender(<AnswerText text={'1. 第一项\n2. 第二项\n3. 第三项'} />)
    expect(screen.getAllByRole('listitem')).toHaveLength(3)
  })
  it('比较内容可以呈现表格，不固定章节数量', () => {
    render(<AnswerText text={'| 方面 | 现有证据 |\n| --- | --- |\n| 价格 | 尚未披露 |'} />)
    expect(screen.getByRole('table')).toHaveTextContent('尚未披露')
    expect(screen.queryByRole('article')).not.toBeInTheDocument()
  })
  it('不执行正文 HTML 或生成可点击的模型网址', () => {
    const view = render(<AnswerText text={'<script>alert(1)</script>\n[伪造链接](https://example.com)'} />)
    expect(view.container.querySelector('script')).toBeNull()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
  it.each([
    ['短段落。', 'p'],
    ['第一段。\n\n较长的第二段，完整说明支持范围。', 'p'],
    ['1. 第一项\n2. 最后一项', 'li'],
    ['- 第一项\n- 最后一项', 'li'],
    ['| 方面 | 说明 |\n| --- | --- |\n| 范围 | 已披露 |', 'td'],
    ['> 引用说明。', 'blockquote'],
  ])('引用嵌入正文末尾，保持 %s 的原有结构', (text, parent) => {
    const markers = <><a href="#source-1">[1]</a><a href="#source-2">[2]</a></>
    const view = render(<AnswerText text={text} citations={markers} />)
    const links = screen.getAllByRole('link')
    expect(links[0].parentElement?.tagName.toLowerCase()).toBe(parent)
    expect(links[1].parentElement).toBe(links[0].parentElement)
    expect(links[0].previousSibling?.textContent?.trim()).toBeTruthy()
    expect(links[0].parentElement?.textContent).toMatch(/\[1\]\[2\]$/)
    expect(view.container.querySelectorAll('p').length).toBe(text.startsWith('第一段') ? 2 : parent === 'p' ? 1 : 0)
  })
  it('引用默认折叠，点击编号显示对应真实来源和逐字证据', async () => {
    const turn: FollowUpTurn = { turn_id: 'turn-test', user_message: '为什么？', status: 'completed', candidate_count: 0,
      created_at: '2026-09-28', updated_at: '2026-09-28',
      result: { answer: [{text: '目前只能确认已公布的范围。', kind: 'fact', support: [{result_id: 'internal_01', excerpt: '面向所有用户'}]}], uncertainties: [], evidence_refs: ['internal_01'] },
      citations: [{result_id: 'internal_01', title: '官方发布', content: '面向所有用户', url: 'https://example.com/release'}] }
    const view = render(<FollowUpResult turn={turn} />)
    expect(screen.getByText('参考来源（1）').closest('details')).not.toHaveAttribute('open')
    expect(view.container.querySelector('.claim-kind')).toBeNull()
    expect(view.container).not.toHaveTextContent('internal_01')
    expect(screen.getByRole('link', {name:'查看来源 1'}).parentElement?.tagName).toBe('P')
    await userEvent.click(screen.getByRole('link', {name: '查看来源 1'}))
    expect(screen.getByText('参考来源（1）').closest('details')).toHaveAttribute('open')
    expect(screen.getByText('面向所有用户')).toBeVisible()
    expect(screen.getByRole('link',{name:'查看原文'})).toHaveAttribute('href','https://example.com/release')
  })
})
