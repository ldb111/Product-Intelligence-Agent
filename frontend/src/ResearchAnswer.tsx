import { useState } from 'react'
import type { ReactNode } from 'react'
import type { FollowUpTurn } from './types'

function inline(text: string): ReactNode[] {
  // 只支持展示性标记；React 转义正文，不执行模型给出的 HTML、链接或脚本。
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, index) =>
    part.startsWith('**') ? <strong key={index}>{part.slice(2, -2)}</strong>
      : part.startsWith('`') ? <code key={index}>{part.slice(1, -1)}</code> : part)
}

export function AnswerText({ text, citations }: { text: string; citations?: ReactNode }) {
  // 回答按其实际内容展示段落、列表、引用或表格，不预设固定章节和卡片数量。
  const blocks: ReactNode[] = []
  const lines = text.trim().split('\n')
  const cells = (line: string) => line.trim().replace(/^\||\|$/g, '').split('|').map(item => item.trim())
  for (let i = 0; i < lines.length;) {
    const line = lines[i].trim()
    if (!line) { i++; continue }
    if (line.includes('|') && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] ?? '')) {
      const headings = cells(line)
      i += 2
      const rows: string[][] = []
      while (i < lines.length && lines[i].includes('|')) rows.push(cells(lines[i++]))
      const tail = i === lines.length ? citations : null
      blocks.push(<div className="answer-table" key={i}><table><thead><tr>{headings.map((cell, n) => <th key={n}>{inline(cell)}{!rows.length && n === headings.length - 1 && tail}</th>)}</tr></thead>
        <tbody>{rows.map((row, n) => <tr key={n}>{headings.map((_, c) => <td key={c}>{inline(row[c] ?? '')}{n === rows.length - 1 && c === headings.length - 1 && tail}</td>)}</tr>)}</tbody></table></div>)
    } else if (/^(?:[-*+] |\d+[.)] )/.test(line)) {
      const ordered = /^\d/.test(line)
      const pattern = ordered ? /^\d+[.)] / : /^[-*+] /
      const items = []
      const start = ordered ? parseInt(line) : undefined
      while (i < lines.length && pattern.test(lines[i].trim())) items.push(lines[i++].trim().replace(pattern, ''))
      const children = items.map((item, n) => <li key={n}>{inline(item)}{i === lines.length && n === items.length - 1 && citations}</li>)
      blocks.push(ordered ? <ol start={start} key={i}>{children}</ol> : <ul key={i}>{children}</ul>)
    } else if (/^#{1,6} /.test(line)) {
      blocks.push(<h3 key={i}>{inline(line.replace(/^#{1,6} /, ''))}{i === lines.length - 1 && citations}</h3>)
      i++
    } else if (line.startsWith('> ')) {
      blocks.push(<blockquote key={i}>{inline(line.slice(2))}{i === lines.length - 1 && citations}</blockquote>)
      i++
    } else {
      blocks.push(<p key={i}>{inline(line)}{i === lines.length - 1 && citations}</p>)
      i++
    }
  }
  return <div className="answer-prose">{blocks}</div>
}

export function FollowUpResult({ turn }: { turn: FollowUpTurn }) {
  const [sourcesOpen, setSourcesOpen] = useState(false)
  const evidence = turn.citations ?? []
  const labels = new Map(evidence.map((item, index) => [item.result_id, index + 1]))
  return <div className="follow-up-answer">
    {turn.result?.answer.map((claim, index) => <div className="answer-part" key={index}>
      {/* 引用属于整个已校验 Claim，附在其最后正文单元内，不造独立引用段或逐句绑定。 */}
      <AnswerText text={claim.text} citations={Array.from(new Set(claim.support.map(item => item.result_id))).map(id => {
        const number = labels.get(id)
        return number ? <a className="inline-citation" key={id} href={`#${turn.turn_id}-source-${number}`}
          aria-label={`查看来源 ${number}`} onClick={() => setSourcesOpen(true)}>[{number}]</a>
          : <span className="answer-source-missing" key={id}>引用暂不可用</span>
      })} />
    </div>)}
    {!!turn.result?.uncertainties.length && <div className="follow-up-uncertainties"><strong>仍需确认</strong><ul>{turn.result.uncertainties.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
    {evidence.length > 0 && <details className="answer-sources" open={sourcesOpen} onToggle={event => setSourcesOpen(event.currentTarget.open)}>
      <summary>参考来源（{evidence.length}）</summary>
      <ol>{evidence.map((source, index) => {
        const quotes = turn.result?.answer.flatMap(claim => claim.support).filter(item => item.result_id === source.result_id) ?? []
        return <li id={`${turn.turn_id}-source-${index + 1}`} key={source.result_id}>
          <strong>[{index + 1}] {source.title}</strong>{source.url && <> · <a href={source.url} target="_blank" rel="noreferrer">查看原文</a></>}
          {Array.from(new Set(quotes.map(item => item.excerpt))).map((excerpt, n) => <blockquote key={n}>{excerpt}</blockquote>)}
        </li>
      })}</ol>
    </details>}
  </div>
}
