# 好玩实验室后端

本仓库是好玩实验室（`rulefolio`）的 API 服务，使用 FastAPI、同步 SQLAlchemy 2.x、Psycopg 3、PostgreSQL、Alembic、Pydantic v2、pytest 和 Ruff。

当前基础工程已提供应用配置、数据库连接、请求日志、错误响应、CORS、`/health`、`/ready` 和空业务 router。作品、版本、场次、反馈、问题、权限、文件和邮件等业务能力尚未实现。

## 环境要求

- Python 3.12 或更高版本
- uv
- 项目已绑定的 PostgreSQL；执行完整测试时使用隔离测试库

## 快速开始

本机开发 `.env` 已准备完成。新环境才需要从公开示例创建配置，并填写该环境已经分配的真实资源绑定：

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

- `DB_*` 与 `TEST_DB_NAME`：开发库和隔离测试库；当前代码已经消费 `DB_*`。
- `APP_*`、`API_PREFIX`、`ENABLE_API_DOCS`、`LOG_LEVEL`、`CORS_ORIGINS`：应用、文档、日志和前端联调设置。
- `S3_*`：私有版本材料和作品图片的对象存储绑定。
- `SMTP_*` 与 `MAIL_*`：开发邮件发送和测试收件人绑定。

S3 和 SMTP 资源已经准备并验证，但当前业务代码尚未消费这些配置；后续只能在对应文件或邮件业务实现中接入，不得以基础工程存在配置键为由宣称能力已经可用。

`CORS_ORIGINS` 使用 JSON 字符串数组。本地配置只允许 `http://127.0.0.1:3105`。真实密码、访问密钥和邮件凭据不得进入源码、公开示例、日志或提交。

## 代码入口

```text
src/app/
├── core/       # 配置、数据库、响应、错误、日志与 middleware
├── api.py      # 业务 API 聚合
├── health.py   # 根级存活与就绪探针
└── main.py     # 应用装配
```

业务 router 通过 `src/app/api.py` 聚合，并由 `API_PREFIX` 统一挂载。业务成功响应使用 `ApiResponse[T]`；`/health` 与 `/ready` 不使用业务响应包装。数据库 Schema 只通过 Alembic 变更，不在启动时调用 `create_all()` 或自动迁移。

## 验证与构建

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
uv build
```

默认测试会验证配置、错误响应、探针、中间件、请求 ID 和数据库 Session 生命周期。只有显式把隔离测试库的 `DB_*` 注入进程环境时，PostgreSQL 集成测试才会执行；未执行时 pytest 会明确显示跳过。

当前没有业务模型和 migration revision，`uv run --locked alembic upgrade head` 只验证 Alembic 配置与数据库连接。`uv build` 在 `dist/` 生成 wheel 和 sdist；该目录是本地构建产物，不提交。

开发规则见 [AGENTS.md](AGENTS.md)，API、数据库和基础设施约束见 [`.claude/rules/`](.claude/rules/)。
