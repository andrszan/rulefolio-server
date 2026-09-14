# 好玩实验室后端

本仓库是好玩实验室（`rulefolio`）的 API 服务，使用 FastAPI、同步 SQLAlchemy 2.x、Psycopg 3、PostgreSQL、Alembic、Pydantic v2、pytest 和 Ruff。

当前已提供应用配置、数据库连接、请求日志、错误响应、CORS、`/health`、`/ready`，以及账户开通、Argon2id 密码、opaque session、恢复凭据、Outbox 和 SMTP 派发能力；还包括私有工作空间、成员、邮件邀请及邀请兑换，以及默认私有的作品、基础资料更新和作品级访问控制。版本、场次、反馈、问题和文件能力尚未实现。

## 环境要求

- Python 3.12 或更高版本
- uv
- 项目已绑定的 PostgreSQL；执行完整测试时使用隔离测试库

## 快速开始

本机开发 `.env` 需要同时配置 API/邮件运行身份 `DB_*` 和仅供 Alembic 使用的 schema owner `MIGRATOR_DB_*`；两者不得复用。新环境才需要从公开示例创建配置，并填写该环境已经分配的真实资源绑定：

```bash
uv sync --locked
cp .env.example .env
```

运行迁移并在项目分配端口启动服务：

```bash
uv run --locked alembic upgrade head
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8105 --reload
```

检查服务：

```bash
curl http://127.0.0.1:8105/health
curl http://127.0.0.1:8105/ready
```

两者就绪时返回 `{"status":"ok"}`。`/health` 只检查进程存活，不访问数据库；`/ready` 只执行最小 PostgreSQL 连通性检查，不迁移、建表或查询业务数据。

API 文档默认位于：

```text
/api/v1/docs
/api/v1/redoc
/api/v1/openapi.json
```

## 配置边界

`.env` 保存受保护的本机开发配置，公开键合同见 `.env.example`：

- `DB_*` 与 `TEST_DB_NAME`：API 与邮件派发的非 owner 开发库和隔离测试库身份；当前代码已经消费 `DB_*`。
- `MIGRATOR_DB_*`：仅 Alembic 使用的 schema owner 身份；应用运行配置不得使用它。
- `APP_*`、`API_PREFIX`、`ENABLE_API_DOCS`、`LOG_LEVEL`、`CORS_ORIGINS`：应用、文档、日志和前端联调设置。
- `PASSWORD_*`、`ARGON2_*`、`SESSION_TTL_HOURS`、`ONE_TIME_TOKEN_TTL_MINUTES`、`*_MAX_ATTEMPTS`、`AUTH_ATTEMPT_*`、`RECOVERY_RESPONSE_MIN_DURATION_MS`、`RECOVERY_JOB_STALE_MINUTES`：服务端认证安全参数。
- `TOKEN_ENCRYPTION_KEY`、`AUTH_ATTEMPT_PEPPER`：恢复 token 信封和尝试主体摘要的受保护密钥，不能进入客户端或版本库。
- `S3_*`：私有版本材料和作品图片的对象存储绑定。
- `SMTP_*` 与 `MAIL_*`：开发邮件发送和测试收件人绑定。

S3 和 SMTP 资源已经准备并验证，但当前业务代码尚未消费这些配置；后续只能在对应文件或邮件业务实现中接入，不得以基础工程存在配置键为由宣称能力已经可用。

`CORS_ORIGINS` 使用 JSON 字符串数组。本地配置只允许 `http://127.0.0.1:3105`。真实密码、访问密钥和邮件凭据不得进入源码、公开示例、日志或提交。

## 受控账户与邮件派发

完成独立 migrator 配置并执行 migration 后，受控运维才可开通待激活账户；命令不接收或输出密码与一次性 token：

```bash
uv run --locked python -m app.manage_identity provision \
  --email <account-email> \
  --operator <operator-id> \
  --reason <approved-reason>
```

工作空间的受控诊断必须带明确空间、操作者和理由；它只返回成员和邀请的最小标识/状态并写入安全审计：

```bash
uv run --locked python -m app.manage_workspaces \
  --workspace-id <workspace-id> \
  --operator <operator-id> \
  --reason <approved-reason>
```

由独立进程先处理已加密的恢复请求队列、再派发已提交的 Outbox；工作空间邀请在派发前会再次复核邀请和凭据，SMTP 已接受只表示邮件服务接受请求，不表示已送达或已阅读：

```bash
uv run --locked python -m app.mail_dispatcher --once
```

受控诊断只能按 Outbox 标识读取，并且必须写入操作者和理由审计：

```bash
uv run --locked python -m app.manage_identity outbox \
  --id <outbox-id> \
  --operator <operator-id> \
  --reason <approved-reason>
```

## 代码入口

```text
src/app/
├── core/       # 配置、数据库、响应、错误、日志与 middleware
├── api.py                 # 业务 API 聚合
├── identity/              # 账户、密码、session 与一次性凭据
├── notifications/         # Outbox、SMTP adapter 与派发状态
├── workspaces/            # 工作空间、成员、邀请、兑换与作品访问关系
├── works/                 # 私有作品、基础资料与作品级访问服务
├── manage_identity.py     # 受控账户开通与 Outbox 诊断命令
├── manage_workspaces.py   # 受控工作空间诊断命令
├── mail_dispatcher.py     # 邮件派发进程入口
├── health.py              # 根级存活与就绪探针
└── main.py                # 应用装配
```

业务 router 通过 `src/app/api.py` 聚合，并由 `API_PREFIX` 统一挂载。业务成功响应使用 `ApiResponse[T]`；`/health` 与 `/ready` 不使用业务响应包装。数据库 Schema 只通过 Alembic 变更，不在启动时调用 `create_all()` 或自动迁移。认证 API 位于 `/sessions`、`/sessions/current`、`/account-recovery-requests`、`/account-activations/exchanges` 和 `/account-recovery-exchanges`；工作空间及作品 API 位于 `/workspaces` 与其下的 `/works`，邀请兑换位于 `/workspace-invitation-exchanges`。作品只向同时具有当前工作空间成员资格与作品访问关系的账户返回，维护者才可更新资料或管理访问。工作空间邀请使用 `Idempotency-Key`，token 仅可放在兑换请求体。具体字段和错误 reason 以 OpenAPI 为准。

## 验证与构建

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
uv build
```

默认测试会验证配置、错误响应、探针、中间件、请求 ID、认证 HTTP 契约和数据库 Session 生命周期。只有显式把隔离测试库的 `DB_*` 注入进程环境时，PostgreSQL 集成测试才会执行；未执行时 pytest 会明确显示跳过。

`uv run --locked alembic upgrade head` 仅使用 `MIGRATOR_DB_*` 连接 schema owner；升级后的 API 与派发进程仅使用 `DB_*`。迁移必须在可丢弃的隔离测试库先完成验证。`uv build` 在 `dist/` 生成 wheel 和 sdist；该目录是本地构建产物，不提交。

开发规则见 [AGENTS.md](AGENTS.md)，API、数据库和基础设施约束见 [`.claude/rules/`](.claude/rules/)。
