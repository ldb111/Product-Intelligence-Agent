# 模型可见面审查 v0.1

> 历史记录：本文保留原阶段计划、验收数字、代码字段与输出，不代表当前待办。当前产品能力见[实现事实索引](../PIA_MVP_Implementation_Fact_Index.md)，面试阅读入口见[产品决策](../product_decisions.md)。

<details>
<summary>历史记录阅读术语（代码字段与原始输出保留原样）</summary>

LLM（大语言模型）；Prompt（提示词）；Tool（工具）。

</details>

## 问题

`web_search` 的 MCP Tool Description（MCP 工具描述）曾包含密钥名称和搜索供应商信息。这些内容会进入 LLM Context（大语言模型上下文），但不能帮助模型选择工具，还破坏了工具的供应商无关边界。

## 根因

开发说明与模型输入没有被明确区分。函数 Docstring（文档字符串）既被当作代码说明，又被 MCP 自动暴露为 Tool Description，导致底层实现细节进入模型上下文。

## 修正

- `web_search` 只描述搜索用途、输入、返回结果，以及与 `read_page` 的边界。
- `read_page` 只描述指定页面读取能力和返回内容。
- 增加 Tool Metadata Contract Test（工具元数据契约测试），阻止供应商、密钥、环境变量和内部模块名称重新进入描述。

## 后续审查规则

1. 将 Tool Description、参数描述、返回字段描述、Prompt（提示词）和返回给模型的错误统一视为模型输入。
2. 只保留模型完成决策所需的业务语义、使用时机和能力边界。
3. Provider（供应商）、密钥、环境变量、认证方式、SDK 和 Endpoint（接口地址）留在代码或开发日志中。
4. 程序能够确定的事实由程序处理，不转交模型猜测。
5. 新增或修改模型可见内容时，同步检查元数据契约测试。
