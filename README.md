# 好玩实验室后端

本仓库是好玩实验室（`rulefolio`）的 API 服务，使用 FastAPI、同步 SQLAlchemy 2.x、Psycopg 3、PostgreSQL、Alembic、Pydantic v2、pytest 和 Ruff。

当前已提供应用配置、数据库连接、请求日志、错误响应、CORS、`/health`、`/ready`，以及账户开通、Argon2id 密码、opaque session、恢复凭据、Outbox 和 SMTP 派发能力；还包括私有工作空间、成员、邮件邀请及邀请兑换，以及默认私有的作品、基础资料更新、作品级访问控制、作品图片和唯一当前规则材料的上传、列表、预览与下载。维护者和组织者可创建试玩计划、安排场次、固定规则与材料快照、邀请已有激活账户、修改安排、开始或取消场次；已开始场次可保存实际规则和材料快照、实际参与、人数、时长、完成状态、实际玩法模式以及现场记录，并可设计题目和整理已提交反馈。维护者、组织者和协作者可读取同一作品已开始场次的脱敏概览，按安排日期、实际人数和实际玩法模式筛选；协作者不能读取场次结果、问题详情或证据。维护者可从现场观察和已提交反馈归纳问题，记录当前处理决定、理由和状态，并维护关联来源；调整说明改变后会清空当前结论，维护者可创建并原子关联一场针对性复测，待该场记录实际材料及关联证据后明确保存当前结论。受邀账户仅能读取自己的场次和固定材料并确认参加；场次开始后可保存、提交或更正自己的直接反馈，草稿不向管理者或其他参与者公开。所有已登录账户还可读取和处理个人待办，站内接受工作空间邀请，并在当前业务资格仍有效时对失败的业务邮件重投一次。维护者保存规则名称、可选说明、正文和完整当前材料集合；组织者与协作者只能读取当前关联的材料。当前作品访问者可取得按角色裁剪的 ZIP 资料归档；工作空间负责人经当前密码复核后可设置资料取得截止时间并发起不可撤销退出，窗口内保留读取与导出，到期由 RLS、服务与派发进程收敛该空间访问而不删除业务资料或账户。项目维护者可从空的项目数据库与私有桶创建交付基线，或经明确确认重置该项目范围。版本历史尚未实现。

## 环境要求

- Python 3.12 或更高版本
- uv
- 工作区统一 PostgreSQL `127.0.0.1:5432`；执行完整测试时使用隔离的 `rulefolio_test`

## 快速开始

本机开发 `.env` 需要同时配置 API/邮件运行身份 `DB_*` 和仅供 Alembic 使用的 schema owner `MIGRATOR_DB_*`；两者不得复用。数据库本身由不可登录的 `rulefolio_owner` 持有，不进入应用配置。新环境才需要从公开示例创建配置，并填写该环境已经分配的真实资源绑定：

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

- `DB_*`：API 与邮件派发使用的非 owner 运行身份；当前开发库为 `rulefolio_dev`。`TEST_DB_NAME` 只记录隔离测试库名称，不会自动替换 `DB_NAME`。
- `MIGRATOR_DB_*`：仅 Alembic 使用的 schema owner 身份；应用运行配置不得使用它。
- `APP_*`、`API_PREFIX`、`ENABLE_API_DOCS`、`LOG_LEVEL`、`CORS_ORIGINS`：应用、文档、日志和前端联调设置。
- `PASSWORD_*`、`ARGON2_*`、`SESSION_TTL_HOURS`、`ONE_TIME_TOKEN_TTL_MINUTES`、`*_MAX_ATTEMPTS`、`AUTH_ATTEMPT_*`、`RECOVERY_RESPONSE_MIN_DURATION_MS`、`RECOVERY_JOB_STALE_MINUTES`：服务端认证安全参数。
- `TOKEN_ENCRYPTION_KEY`、`AUTH_ATTEMPT_PEPPER`：恢复 token 信封和尝试主体摘要的受保护密钥，不能进入客户端或版本库。
- `BASELINE_PASSWORD`：交付基线账号的受保护密码，不能通过命令参数、日志或客户端传入；账号交接信息见[项目准备清单](../docs/requirements/项目准备清单.md)。
- `S3_*`：私有当前规则材料和作品图片的对象存储绑定。
- `SMTP_*` 与 `MAIL_*`：开发邮件发送和测试收件人绑定。

S3 资源已由作品图片、当前规则材料、已授权试玩场次的固定材料及作品资料导出以私有条件写入、服务端复核和受授权的流式读取方式消费；材料不会提供公开 URL、对象存储直连或导出对象持久化。SMTP 资源用于凭据邮件及已冻结正文的试玩邀请、改期、材料更新和取消提醒；SMTP 已接受只表示邮件服务接受请求，不表示已阅读。不得以基础工程存在配置键为由宣称未实现的能力已经可用。

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

初始化只接受操作者和理由，在当前项目数据库和私有桶均为空时创建交付账号、工作空间、作品、角色、图片、两场近期试玩、一次确认参加和经 dispatcher 实际处理的一条试玩邀请；`playtest-guest@example.com` 是不加入工作空间也没有作品访问关系的受邀试玩账号。已有数据时不修改。重置会删除当前配置绑定的全部项目记录与对象，执行前先停止本项目 API 与邮件派发进程，并使用当前 `DB_NAME:S3_BUCKET_NAME` 明确确认：

```bash
uv run --locked python -m app.manage_project initialize \
  --operator <operator> \
  --reason <reason>
uv run --locked python -m app.manage_project reset \
  --operator <operator> \
  --reason <reason> \
  --confirm-reset "<DB_NAME>:<S3_BUCKET_NAME>"
```

初始化或重置失败时不会报告成功；若出现部分写入，仅用带确认的 `reset` 收敛到完整基线。账号、数据范围和交付验收方式见[项目准备清单](../docs/requirements/项目准备清单.md)。

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
├── exports/               # 角色裁剪的临时 ZIP 归档与下载 router
├── files/                 # 私有图片与材料的检测、对象生命周期与 HTTP router
├── identity/              # 账户、密码、session 与一次性凭据
├── issues/                # 问题当前判断、来源关联与维护者专属 API
├── notifications/         # 个人待办、凭据与冻结业务邮件的 Outbox、SMTP adapter 与派发状态
├── playtests/             # 测试计划、场次快照、参与确认与受限材料读取
├── workspaces/            # 工作空间、成员、邀请、兑换与作品访问关系
├── works/                 # 私有作品、基础资料、当前规则材料与作品级访问服务
├── manage_identity.py     # 受控账户开通与 Outbox 诊断命令
├── manage_workspaces.py   # 受控工作空间诊断命令
├── manage_project.py      # 项目初始化与重置命令
├── project_baseline.py    # 交付基线编排与受控清理
├── mail_dispatcher.py     # 邮件派发进程入口
├── health.py              # 根级存活与就绪探针
└── main.py                # 应用装配
```

业务 router 通过 `src/app/api.py` 聚合，并由 `API_PREFIX` 统一挂载。业务成功响应使用 `ApiResponse[T]`；`/health` 与 `/ready` 不使用业务响应包装。数据库 Schema 只通过 Alembic 变更，不在启动时调用 `create_all()` 或自动迁移。认证 API 位于 `/sessions`、`/sessions/current`、`/account-recovery-requests`、`/account-activations/exchanges` 和 `/account-recovery-exchanges`；工作空间及作品 API 位于 `/workspaces` 与其下的 `/works`，其中作品资料下载为 `/workspaces/{workspaceId}/works/{workId}/export`，负责人退出为 `/workspaces/{workspaceId}/exit`；图片 API 位于作品路径下的 `/images`，当前规则材料 API 位于 `/rule-materials` 与 `/material-files`，试玩计划管理 API 位于作品路径下的 `/playtest-plans` 和 `/playtest-sessions`，试玩概览 API 位于 `/playtest-overview`，问题 API 位于作品路径下的 `/issues`，仅维护者可读取或维护，受邀者入口位于 `/playtest-sessions/{sessionId}`，邀请兑换位于 `/workspace-invitation-exchanges`，个人待办位于 `/notifications/todos`。作品、当前规则和当前材料只向同时具有当前工作空间成员资格与作品访问关系的账户返回；维护者才可更新资料、保存规则材料或读取材料候选；维护者和组织者可管理试玩，所有当前作品角色可读取脱敏试玩概览，受邀试玩者不因此获得作品或工作空间权限。工作空间邀请及退出使用 `Idempotency-Key`，token 仅可放在兑换请求体。具体字段和错误 reason 以 OpenAPI 为准。

## 验证与构建

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
uv build
```

默认测试会验证配置、错误响应、探针、中间件、请求 ID、认证 HTTP 契约和数据库 Session 生命周期。执行 PostgreSQL 集成测试时，保持 Settings 从 `.env` 读取配置，只把所需 `DB_*` 原值显式注入进程环境，并将 `DB_NAME` 指向 `TEST_DB_NAME`；不要在 shell 中 `source .env`，否则会破坏 `CORS_ORIGINS` 等 JSON 值。pytest 摘要不得显示 PostgreSQL 用例因配置缺失而跳过。

`uv run --locked alembic upgrade head` 仅使用 `MIGRATOR_DB_*` 连接 `rulefolio_migrator` schema owner；升级后的 API 与派发进程仅使用 `DB_*` 的 `rulefolio_app`。两个身份通过 SCRAM 只能连接工作区统一 5432 上的 Rulefolio 开发库和测试库，database owner `rulefolio_owner` 不可登录。迁移必须在可丢弃的隔离测试库先完成验证。`uv build` 在 `dist/` 生成 wheel 和 sdist；该目录是本地构建产物，不提交。

开发规则见 [AGENTS.md](AGENTS.md)，API、数据库和基础设施约束见 [`.claude/rules/`](.claude/rules/)。
