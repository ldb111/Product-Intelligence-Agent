# Product Intelligence Agent

AI 产品研究与竞争信号监控项目。

## 当前功能：网页读取、页面快照与变化检测

输入一个 HTTP/HTTPS URL，程序会请求服务器返回的 HTML，删除确定性的结构噪声，提取标题和页面文本，为标准化内容计算 SHA-256 哈希并保存 JSON 页面快照，然后查找上一份快照并比较两份 `content_hash`。当前实现只判断标准化正文是否变化，不生成具体文本差异，也不判断变化是否具有竞争意义。

### 安装依赖

```powershell
python -m pip install -r requirements.txt
```

### 运行

```powershell
python backend/page_reader.py "https://example.com"
```

可用 `--timeout` 设置请求超时秒数：

```powershell
python backend/page_reader.py "https://example.com" --timeout 5
```

成功时，程序在 `data/snapshots/` 新建一份 UTF-8 JSON 快照，并向标准输出写入当前与上一份快照的查询结果：

```json
{
  "is_first_scan": true,
  "changed": null,
  "previous_content_hash": null,
  "current_content_hash": "...",
  "current_snapshot": {
    "url": "https://example.com",
    "status_code": 200,
    "title": "Example Domain",
    "content": "Example Domain\n...",
    "captured_at": "2026-09-02T14:35:20.123456+08:00",
    "content_hash": "..."
  },
  "previous_snapshot": null
}
```

失败时，程序向标准错误写入带有错误类型和说明的 JSON，并返回退出码 `1`。异常 HTTP 状态码对应的错误页面不会被作为正常页面输出。

### 测试

```powershell
python -m unittest discover -s tests -v
```

测试使用本机临时 HTTP 服务，不依赖外部网站。

### 当前限制

该实现使用 `requests + BeautifulSoup`，只能解析 HTTP 响应中已有的 HTML。对于依赖 JavaScript 才渲染主要内容的网页，`content` 可能不完整；本阶段未引入 Playwright 等浏览器自动化方案。

## 开发进度

- [Development Roadmap（开发路线图）](./docs/development-roadmap.md)
- [Current Sprint（当前迭代）](./docs/current-sprint.md)
