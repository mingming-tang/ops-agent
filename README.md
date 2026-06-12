# Ops Agent · 运维智能体

> 基于 **LangGraph** 的运维 Agent:用自然语言下达任务,它通过 SSH 登录服务器执行运维操作,**每条变更命令都要你点「批准」才会执行**。带命令分级护栏、人工审批门、全量审计与内置 Web 控制台(含交互式 SSH 终端)。

把"让模型帮我运维"这件事做得**可控**:模型负责规划与生成命令,真正的执行权始终握在你手里。

> [!WARNING]
> **请勿用于生产环境。** 本项目是一个演示/学习性质的脚手架,鉴权、权限隔离、主机指纹校验等尚不完备(见文末「生产化 TODO」)。它能直接操作真实服务器,误用风险较高——仅建议在测试机、个人环境或受控内网中体验。

![Ops Agent 控制台截图](docs/screenshot.png)

```
你: 检查 web-prod-1 的磁盘使用率,>80% 的分区列出最大的 5 个目录
Agent: 计划 → 生成命令 `df -h`(只读,自动执行)
       → 生成命令 `du -sh /var/* | sort -rh | head -5`(等待你审批)
你: [批准]
Agent: 执行 → 汇总结论
```

---

## 💡 项目背景

> 每次都要查完 ChatGPT,完了把命令一条一条地复制到服务器,完了等结果,再验证,挺麻烦的。于是自己花了点时间做了一个运维 Agent,感觉还不错,很多自己不想做的运维工作都能交给它了,分享给大家。

## 🎬 视频演示

- 🎥 **功能演示**:https://www.bilibili.com/video/BV16VJ56bEo5/
- 🛠️ **部署教程**:https://www.bilibili.com/video/BV1LpJ56pERU/

---

## ✨ 特性

- **🤖 自然语言运维** —— LangGraph 编排的 Plan-and-Execute Agent,自动拆解任务、生成命令、反思结果。
- **🛡️ 命令分级护栏** —— 命令自动分级为 *只读 / 变更 / 危险*,只读可放行,变更与危险默认逐条人工审批。
- **🙋 人工审批门 (Human-in-the-loop)** —— 基于 LangGraph `interrupt()`,审批前任务挂起、审批后续跑,可按会话开启「本会话免确认」。
- **🖥️ 内置 SSH 终端** —— Web 控制台自带多 Tab 交互式终端,Agent 操作之余也能自己上手敲。
- **🧩 多模型供应商** —— 内置 OpenAI / Anthropic / 通义千问(Qwen) / MiniMax / DeepSeek,后台即可切换默认模型。
- **🔐 凭证加密落库** —— SSH 私钥/密码等敏感凭证用 Fernet 加密存储。
- **📋 全量审计** —— 每次命令执行、每次审批都落库可追溯。
- **🔑 单用户登录认证** + 多会话并行隔离 + 刷新保持会话 + 移动端适配。

---

## 🚀 快速开始(Docker,推荐)

镜像已发布到 Docker Hub:[`mingmingtang/ops-agent`](https://hub.docker.com/r/mingmingtang/ops-agent)。

### 1. 生成凭证加密主密钥

所有入库的敏感凭证都用这个 Fernet 密钥加密,**必须设置且妥善保管**(丢了就解不开已存的凭证)。直接用镜像生成一个:

```bash
docker run --rm mingmingtang/ops-agent \
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 2. 启动容器

```bash
docker run -d --name ops-agent \
  -p 8000:8000 \
  -e SECRET_ENCRYPTION_KEY="把上一步生成的密钥粘贴到这里" \
  -e AUTH_USERNAME=admin \
  -e AUTH_PASSWORD="换成你自己的强密码" \
  -v ops-agent-data:/app/data \
  --restart unless-stopped \
  mingmingtang/ops-agent:latest
```

打开 **http://localhost:8000**,用上面的用户名/密码登录即可。数据(SQLite)持久化在 `ops-agent-data` 卷里,容器重建不丢。

> ⚠️ **不设 `AUTH_PASSWORD` 就等于不开认证**——任何能访问该端口的人都能操作你的服务器。除非只在本机/内网且自己清楚,否则务必设置密码,并不要把 8000 端口直接暴露到公网。

### 3. 用 docker compose(可选)

仓库根目录已带 `docker-compose.yml`(默认拉取已发布镜像):

```bash
# 先把密钥写进环境变量或 .env 文件
echo "SECRET_ENCRYPTION_KEY=$(docker run --rm mingmingtang/ops-agent python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" > .env
echo "AUTH_USERNAME=admin"            >> .env
echo "AUTH_PASSWORD=换成你自己的强密码" >> .env

docker compose up -d
```

---

## 🧭 使用教程

登录控制台后,顶部有几个标签页,按下面顺序配置一遍即可开始干活。

### ① 模型 —— 接一个大模型

进入 **模型** 标签 → 新增一个供应商,例如:

| 字段 | 示例 |
|---|---|
| 供应商 | `anthropic` |
| 模型名 | `claude-opus-4-8` |
| API Key | `sk-...` |

保存后**勾选「设为默认」**。支持 `openai` / `anthropic` / `qwen` / `minimax` / `deepseek`,后四类走 OpenAI 兼容协议,必要时可自定义 `base_url`。

> 💬 **模型怎么选?** 运维场景对"听懂指令、稳定生成正确命令、按规划逐步执行"要求较高,建议选各家**旗舰级**模型。本人日常用的是 **`qwen3.7-max`**(供应商选 `qwen`),效果不错;**其它模型暂未测试**,理论上 `claude-opus-4-8`、`gpt` 等旗舰模型也可用,欢迎试用后反馈。

### ② 服务器 —— 登记要操作的机器

进入 **服务器** 标签,填写 host / port / 用户名,以及认证方式:

- **SSH 私钥**:先到 **SSH秘钥** 标签上传/粘贴私钥,再在服务器里选用;
- **密码**:直接填密码。

凭证保存时会用 Fernet 加密。填好后点「**测试连通性**」确认能连上。

### ③ 对话 —— 下达运维任务

回到 **对话** 标签:

1. (可选)在「操作对象」里勾选本次允许操作的服务器,不勾选=允许全部。
2. 输入任务,例如「*检查 web-prod-1 的磁盘使用率,>80% 的分区列出最大的 5 个目录*」。
3. Agent 会规划并生成命令:
   - **只读命令** 自动执行;
   - **变更/危险命令** 弹出审批框,展示将要执行的命令,点「**批准**」才执行,「**拒绝**」则跳过。
4. 信任当前任务、想少点几次确认?勾选「**⚡ 本会话所有命令无需确认**」即可在本会话内放行(立即生效)。
5. 需要自己动手时,右侧 **终端** 面板可直接开一个该服务器的交互式 SSH 终端。

### ④ 审计 —— 回看做过什么

**审计** 标签按时间线记录了每条执行过的命令、风险等级与审批结果,便于复盘和合规追溯。

---

## 🛠️ 配置项(环境变量)

| 变量 | 默认 | 说明 |
|---|---|---|
| `SECRET_ENCRYPTION_KEY` | — | **必填**。Fernet 主密钥,加密入库凭证。 |
| `AUTH_USERNAME` | `admin` | 控制台登录用户名。 |
| `AUTH_PASSWORD` | 空 | 控制台登录密码。**留空则不启用认证。** |
| `DATABASE_URL` | 容器内为 `sqlite:////app/data/ops_agent.db` | 业务数据库;可改 Postgres,如 `postgresql+psycopg://user:pass@host:5432/ops_agent`。 |
| `CHECKPOINT_DB_URL` | 空 | LangGraph checkpointer(续跑/持久化);留空用内存版,重启丢失。生产建议指向 Postgres。 |
| `REQUIRE_APPROVAL_FOR_DANGEROUS` | `true` | 危险命令是否强制人工审批。 |
| `REQUIRE_COMMAND_APPROVAL` | `true` | 所有可执行命令(SSH/云)执行前是否逐条审批。 |
| `DEFAULT_DRY_RUN` | `false` | 是否默认只展示不执行。 |
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8000` | 监听地址/端口。 |

---

## 💻 从源码运行(开发)

需要 Python 3.12+ 与 [uv](https://github.com/astral-sh/uv);如需 stdio 方式的云 MCP 还需 Node.js(`npx`)。

```bash
git clone https://github.com/<your-org>/ops-agent.git
cd ops-agent
uv venv && source .venv/bin/activate
uv pip install -e .

cp .env.example .env
# 编辑 .env:生成并填入 SECRET_ENCRYPTION_KEY,设置 AUTH_PASSWORD
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

python -m app.main          # 默认 http://localhost:8000(开发模式带热重载)
```

---

## 🏗️ 架构一览

| 模块 | 位置 |
|---|---|
| Agent 编排(LangGraph 图) | `app/agent/graph.py` |
| 审批节点 / Human-in-the-loop | `graph.py` 的 `guardrail` 节点 `interrupt()` |
| 命令分级护栏(只读/变更/危险) | `app/agent/guardrails.py` |
| Plan-and-Execute + 系统提示 | `app/agent/state.py` · `app/agent/prompts.py` |
| 续跑 / Checkpointer | `app/agent/runtime.py` |
| 多模型供应商层 | `app/llm/registry.py` |
| SSH 工具 / 交互式终端 | `app/tools/ssh.py` · `app/api/terminal.py` |
| 云资源 MCP 接入 | `app/tools/mcp_manager.py` |
| 凭证加密(Fernet) | `app/db/crypto.py` |
| 审计与数据模型 | `app/db/models.py` |
| Web 控制台(单文件前端) | `app/web/index.html` |

> 💡 项目还内置了**多云 MCP 接入**能力(阿里云 / Cloudflare 等,通过 `mcp_manager.py` 动态加载),可让 Agent 操作云资源。该入口在默认控制台中已隐藏,如需启用可通过 `/admin/cloud-accounts` 接口配置。

---

## 🔌 API 速览

控制台所有操作都是这些 HTTP 接口的封装(启用认证后需先登录拿到会话 Cookie):

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/login` · `/auth/logout` | 登录 / 登出 |
| POST | `/chat/stream` | 发起/继续任务(SSE 流式) |
| POST | `/chat/approve` | 审批续跑 |
| GET/POST/DELETE | `/chat/conversations...` | 会话历史 CRUD |
| POST/GET/PUT/DELETE | `/admin/models` · `/admin/servers` · `/admin/ssh-keys` · `/admin/cloud-accounts` | 配置 CRUD |
| GET | `/admin/audits` | 审计日志 |
| POST/GET/DELETE | `/chat/terminal...` | 交互式终端会话 |
| GET | `/health` | 健康检查 |

---

## ⚠️ 安全须知

再次强调:**不建议用于生产环境**。即便只是在测试/内网环境体验,也请务必:

- 设置强 `AUTH_PASSWORD`,不要把端口裸暴露到公网(建议放在反向代理 + HTTPS 之后,或仅限内网/VPN)。
- 妥善保管 `SECRET_ENCRYPTION_KEY`,丢失将无法解密已存凭证。
- 给 Agent 使用的 SSH 账号按最小权限配置。
- 生产环境保持「危险命令强制审批」开启,谨慎使用「本会话免确认」。

## 🧱 生产化 TODO(脚手架未做)

**工程化 / 安全硬化**

- [ ] checkpointer 换 `PostgresSaver`(当前默认进程内 MemorySaver)
- [ ] 正式鉴权 / RBAC / 多用户隔离(当前为单用户登录)
- [ ] SSH `known_hosts` 主机指纹校验(当前为 `None`)
- [ ] 审批超时、guardrail 规则后台可配 / 用小模型打风险分
- [ ] 密钥改用 KMS / Vault 托管

**能力扩展**

- [ ] 云资源管理(基于多云 MCP,纳管 ECS / OSS / 安全组等资源)
- [ ] 监控管理(对接 Prometheus / Zabbix 等,告警查询与处置闭环)
- [ ] 路由交换管理(网络设备配置/排障,如交换机、路由器、防火墙)

---

## 📄 License

本项目基于 [MIT License](LICENSE) 开源。欢迎 Issue / PR。
