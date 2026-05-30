# 运维 Agent (Operator Agent)

基于 **LangGraph 1.2.2** 的运维智能体:能通过 SSH 操作服务器、通过云厂商 MCP(阿里云 / Cloudflare,可多账号)操作云资源,完成复杂运维任务。带**命令分级护栏 + 人工审批门 + 全量审计 + 后台配置**。

## 核心设计(2026 Agent 模式)

| 模式 | 落地位置 |
|---|---|
| Human-in-the-loop 审批 | `app/agent/graph.py` 的 `guardrail` 节点 `interrupt()` |
| Durable execution / 续跑 | LangGraph checkpointer(`app/agent/runtime.py`) |
| Guardrails 命令分级 | `app/agent/guardrails.py`(只读/变更/危险) |
| Plan-and-Execute + 反思 | `app/agent/state.py` 显式 plan + 系统提示 |
| MCP-native 工具接入 | `app/tools/mcp_manager.py` 动态加载多云账号 |
| Provider-agnostic 模型层 | `app/llm/registry.py`(5 家供应商) |
| 凭证加密 | `app/db/crypto.py`(Fernet) |
| 审计可追溯 | `app/db/models.py` AuditLog + 每次执行落库 |

## 快速开始

```bash
cd operator_agent
uv venv && source .venv/bin/activate
uv pip install -e .

cp .env.example .env
# 生成加密主密钥并填进 .env 的 SECRET_ENCRYPTION_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# 把 ADMIN_TOKEN 改成你自己的

python -m app.main          # 启动,默认 http://localhost:8000
```

打开 `http://localhost:8000`,填入 Admin Token,然后:
1. **模型** 标签:加一个供应商(如 anthropic / claude-opus-4-8 + API Key),勾选「设为默认」。
2. **服务器** 标签:登记要操作的服务器(SSH 凭证)。
3. **云账号** 标签(可选):登记阿里云 / Cloudflare 的 MCP server。
4. **对话** 标签:下达运维任务。高危操作会弹出审批框,点「批准」才执行。

## 云账号(MCP)配置示例

**阿里云(stdio)**
- command: `npx`,args: `-y,@alicloud/alibabacloud-mcp-server`
- secrets: `{"ALIBABA_CLOUD_ACCESS_KEY_ID":"xxx","ALIBABA_CLOUD_ACCESS_KEY_SECRET":"yyy"}`

**Cloudflare(streamable_http)**
- transport: `streamable_http`,url: `https://<你的-cf-mcp-endpoint>`
- secrets(作为 headers): `{"Authorization":"Bearer <token>"}`

> 具体 MCP server 的包名 / 端点以各云厂商官方文档为准,这里只决定"怎么接",不绑定具体实现。

## API 速览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chat` | 发起/继续任务 `{message, thread_id?}` |
| POST | `/chat/approve` | 审批续跑 `{thread_id, approved, by}` |
| POST/GET/DELETE | `/admin/models` `/admin/servers` `/admin/cloud-accounts` | 配置 CRUD |
| GET | `/admin/audits` | 审计日志 |

所有接口需 header `X-Admin-Token`。

## 生产化 TODO(脚手架未做)

- [ ] checkpointer 换 `PostgresSaver`(目前进程内 MemorySaver)
- [ ] 正式鉴权 / RBAC / 多用户隔离,替换单一 admin token
- [ ] SSH `known_hosts` 主机指纹校验(当前为 `None`)
- [ ] 审批粒度细化(按单条 tool_call 而非整批)、审批超时
- [ ] 流式输出(SSE)、对话历史接口
- [ ] guardrail 规则可在后台配置 / 用小模型打风险分
- [ ] 密钥改用 KMS / Vault 托管
```
