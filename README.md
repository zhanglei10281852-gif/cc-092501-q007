# 地下水同位素与污染迁移计算服务

这是一个面向水文地质研究团队和环境监管人员的模块化后端，集中管理地下水井、同位素观测、补给端元、混合源反演、污染物迁移、计算任务、参数版本、结果置信区间、用户权限、会话和审计。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 井点与样本：登记井点坐标、含水层、采样批次和实验室测量结果。
- 样本批量导入：野外批次先解析校验再暂存，单位统一换算后确认入库，支持幂等重交、行级错误和修正续传。
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

野外队一次交回数百条记录时，走"暂存 → 修正 → 确认"两段流程，坏行不会进入已确认数据：

```bash
# 1. 提交批次（batch_key 由来源方生成，重复提交返回原结果，HTTP 200 且 replayed=true）
curl -sS -X POST http://127.0.0.1:8432/api/hydro/imports \
  -H 'Content-Type: application/json' \
  -d '{"batch_key":"LAB-A-20260925-01","source":"甲实验室","rows":[
        {"well_code":"W-001","sampled_at":"2026-09-20T08:00:00+08:00","observation_type":"solute","unit":"µg/L","value":12500,"detection_limit":5,"measurement_error":0.2},
        {"well_code":"W-001","sampled_at":"2026-09-20T08:00:00+08:00","observation_type":"isotope_d18o","unit":"‰","value":-7.5}
      ]}'

# 2. 查看批次与每行状态（含原始字段快照 raw 与修正记录 correction）
curl -sS http://127.0.0.1:8432/api/hydro/imports/1

# 3. 按行号修正坏行，可多次修正后再确认
curl -sS -X PATCH http://127.0.0.1:8432/api/hydro/imports/1/rows \
  -H 'Content-Type: application/json' \
  -d '{"corrections":[{"line_number":3,"fields":{"unit":"mg/L"}}]}'

# 4. 确认入库（幂等，重复确认返回原结果）；查看某口井已确认观测
curl -sS -X POST http://127.0.0.1:8432/api/hydro/imports/1/confirm
curl -sS http://127.0.0.1:8432/api/hydro/wells/1/observations
```

- 每行校验井点编码、采样时间（ISO 8601，统一归一到 UTC）、观测类型（`isotope_d18o` / `isotope_d2h` / `solute`）、单位与数值范围；坏行在 `errors` 中给出行号与全部原因。
- 单位统一入库：同位素一律 `‰`；溶质一律 `mg/L`（`µg/L`÷1000、`g/L`×1000，`ug/L`、`μg/L` 等写法自动识别），检测限随单位同步换算。
- 摘要 `summary` 含 `accepted`（接受）、`rejected`（拒绝）、`converted`（发生单位换算）、`suspected_duplicates`（疑似重复）数量；疑似重复指与本批次其他行或已确认记录在"井点+采样时间+观测类型"上相同，确认时自动跳过。
- 同一 `batch_key` 提交不同内容返回 409；已确认批次不允许再修正。

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
