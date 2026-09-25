# 地下水同位素与污染迁移计算服务

这是一个面向水文地质研究团队和环境监管人员的模块化后端，集中管理地下水井、同位素观测、补给端元、混合源反演、污染物迁移、计算任务、参数版本、结果置信区间、用户权限、会话和审计。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 井点与样本：登记井点坐标、含水层、采样批次和实验室测量结果。
- 同位素计算：处理稳定同位素、溶质浓度、检测限和质量守恒约束，反演多个补给端元比例。
- 污染迁移：计算一维平流、弥散和一阶衰减，提供到达时间和浓度曲线。
- 任务与审计：保存参数版本、计算输入摘要、置信区间、失败重试和结果差异。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 样本批量导入

野外队可一次提交数百条样本记录（JSON 行或 CSV 文本），导入分“暂存—修正—确认”两阶段，坏行不会污染已确认数据。

1. `POST /api/hydro/imports`：提交 `source_batch_id` 与 `rows`（或 `csv_content`，支持中英文表头）。服务逐行解析校验井点编码、采样时间、观测类型（`d18o`/`d2h`/`solute` 及常见别名）、单位（‰、mg/L、µg/L）与数值范围，并把溶质统一换算为 mg/L。结果仅写入暂存表，返回每行的物理行号、`staged`/`rejected`/`suspected_duplicate` 状态、错误原因、换算后数值与**原始字段快照**。
2. 校验规则包括：井点不存在、时间/数值无法解析、未知观测类型、不支持单位、换算后超合理范围，以及检测限/误差列疑似错位（误差显著大于检测限）。
3. `POST /api/hydro/imports/{id}/fix`：按行号修正坏行，原始快照保留，修正后重新校验并可继续确认。
4. `POST /api/hydro/imports/{id}/resolve-duplicates`：对疑似重复行（批内重复、同样本编号已入库、同井同时刻同类型同值）人工裁定接受或驳回。
5. `POST /api/hydro/imports/{id}/confirm`：按“井点+样本编号+采样时间”分组，将长表观测合并为一条样本（宽表列）以统一单位入库。含坏行或未裁定重复行的组整组暂缓；显式点名坏组会返回 422 与阻塞行号。已确认行不可修改。

同一 `source_batch_id` 重复提交且内容一致时直接返回原批次结果（响应带 `replayed: true`，包含后续确认进度）；同批次号但内容不同返回 422。导入摘要包含接受、拒绝、单位转换、疑似重复、已确认数量与本次新增样本数，所有暂存、修正、裁定和确认动作写入 `hydro_audit`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、井点样本、同位素约束、迁移计算、任务恢复和数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  hydro/            地下水、同位素反演和污染迁移服务
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
