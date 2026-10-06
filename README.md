# 盲样分类复核 API（Blind Classification Review）

用 Python + FastAPI + SQLite 实现的盲样分类复核 HTTP API。完整流程：

1. **建批**：提交允许类别与样本（代号 + 真实类别），创建响应中一次性返回**两份评审凭据 + 一份仲裁凭据**（仅此一次，数据库只存哈希）。
2. **双评审独立分类**：两名评审各持凭据提交覆盖全部样本的分类；**交卷后不可修改**。
3. **盲态**：封存前普通查询只返回样本代号与阶段；评审只能查到自己的提交状态，看不到真值，也看不到对方分类。
4. **仲裁**：两份交卷齐全后，仲裁者凭专属凭据查看分歧样本及双方分类，逐项从两者中选择最终分类；一致样本直接取共同分类。**无分歧时第二份交卷即直接封存。**
5. **封存揭盲**：分歧全部裁定后一次性封存。查询返回逐样本最终分类、真值、两名评审各自正确数。封存结果落库后不再变化。
6. **封存后勘误（揭盲勘误版本）**：封存后若发现样本真值录错，仲裁者凭原仲裁凭据提交**期望修订号**与若干样本更正真值，生成新版本。版本 0 即封存原件；后续版本只替换指定样本的真值，原有最终分类与两份评审分类一律不动。交卷与裁定仍不可更改。

## 快速开始（单条命令）

```bash
docker compose up --build
```

服务监听本地 **8000** 端口，SQLite 数据保存在命名卷 `blindreview-data` 中，容器重启后数据仍在。

不使用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000   # 数据默认写入 ./data/blindreview.db
```

数据库路径可用环境变量 `DB_PATH` 覆盖。

## API 一览

| 方法 | 路径 | 凭据 | 说明 |
|---|---|---|---|
| POST | `/batches` | 无 | 创建批次，响应中返回三份凭据（仅此一次） |
| GET | `/batches/{id}` | 无 | 盲态：仅代号 + 阶段；封存后：完整揭盲结果 |
| GET | `/batches/{id}/submission` | `X-Reviewer-Token` | 评审查询自己的提交状态（只见自己的分类） |
| POST | `/batches/{id}/submissions` | `X-Reviewer-Token` | 评审交卷（覆盖全部样本，交卷后不可改） |
| GET | `/batches/{id}/disagreements` | `X-Arbiter-Token` | 仲裁者查看分歧样本及双方分类 |
| POST | `/batches/{id}/adjudications` | `X-Arbiter-Token` | 仲裁者逐项裁定分歧，齐全后自动封存 |
| GET | `/batches/{id}/results` | 无 | 揭盲结果（仅封存后可用）；永远是封存原件（版本 0） |
| POST | `/batches/{id}/errata` | `X-Arbiter-Token` | 封存后勘误：提交期望修订号与更正真值，生成下一版 |
| GET | `/batches/{id}/errata` | 无 | 列出全部勘误版本及每版更正样本 |
| GET | `/batches/{id}/errata/{version}` | 无 | 按版本查询勘误结果（版本 0 = 封存原件） |
| GET | `/healthz` | 无 | 健康检查 |

阶段流转：`collecting`（收卷中）→ `adjudicating`（仲裁中，有分歧时）→ `sealed`（已封存揭盲）。

## 可执行的调用示例

以下示例假设服务运行在 `http://localhost:8000`。也可以直接运行端到端脚本 `./scripts/demo.sh`（需要 curl 与 python3），它会自动走完全部流程。

### 1. 创建批次（凭据只在本响应中出现，请妥善保存）

```bash
curl -sS -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "allowed_categories": ["cat", "dog", "bird"],
    "samples": [
      {"code": "S01", "true_label": "cat"},
      {"code": "S02", "true_label": "dog"},
      {"code": "S03", "true_label": "bird"}
    ]
  }'
```

响应（节选）：

```json
{
  "batch_id": "9f2c...",
  "phase": "collecting",
  "reviewer1_token": "....",
  "reviewer2_token": "....",
  "arbiter_token": "....",
  "notice": "These credentials are shown only in this response and cannot be recovered."
}
```

下文用 `$BID`、`$R1`、`$R2`、`$ARB` 分别代指批次 ID 与三份凭据。

### 2. 盲态查询：只有代号与阶段

```bash
curl -sS http://localhost:8000/batches/$BID
# {"batch_id":"9f2c...","phase":"collecting","samples":["S01","S02","S03"]}
```

### 3. 两名评审分别交卷

```bash
curl -sS -X POST http://localhost:8000/batches/$BID/submissions \
  -H "X-Reviewer-Token: $R1" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "bird"}}'

curl -sS -X POST http://localhost:8000/batches/$BID/submissions \
  -H "X-Reviewer-Token: $R2" -H 'Content-Type: application/json' \
  -d '{"labels": {"S01": "cat", "S02": "dog", "S03": "cat"}}'
# 第二份交卷后：有分歧 -> {"phase":"adjudicating"}；无分歧 -> {"phase":"sealed"}
```

评审只能查自己的提交状态：

```bash
curl -sS http://localhost:8000/batches/$BID/submission -H "X-Reviewer-Token: $R1"
```

重复交卷返回 `409`，原判不变。

### 4. 仲裁者查看分歧并逐项裁定

```bash
curl -sS http://localhost:8000/batches/$BID/disagreements -H "X-Arbiter-Token: $ARB"
# {"disagreements":[{"code":"S03","reviewer1_label":"bird","reviewer2_label":"cat"}], ...}

curl -sS -X POST http://localhost:8000/batches/$BID/adjudications \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"decisions": {"S03": "bird"}}'
# 分歧全部裁定 -> 一次性封存：{"status":"sealed","phase":"sealed"}
```

裁定必须**恰好覆盖全部分歧样本**，且每个选择只能是该样本两位评审之一的标签；封存后再次裁定返回 `409`。

### 5. 揭盲查询

```bash
curl -sS http://localhost:8000/batches/$BID/results
```

```json
{
  "phase": "sealed",
  "results": [
    {"code": "S01", "final_label": "cat",  "true_label": "cat",  "reviewer1_label": "cat",  "reviewer2_label": "cat", "agreed": true,  "reviewer1_correct": true, "reviewer2_correct": true},
    {"code": "S03", "final_label": "bird", "true_label": "bird", "reviewer1_label": "bird", "reviewer2_label": "cat", "agreed": false, "reviewer1_correct": true, "reviewer2_correct": false}
  ],
  "summary": {"sample_count": 3, "disagreement_count": 1, "reviewer1_correct": 3, "reviewer2_correct": 2, "final_correct": 3}
}
```

### 6. 封存后勘误（仅仲裁者，封存后可用）

封存后发现样本真值录错时，仲裁者凭**原有仲裁凭据**提交期望修订号（刚封存时为 `0`）与更正真值。版本 0 是封存原件；每次成功勘误生成一个严格递增的新版本，只替换指定样本的真值，原有最终分类与两份评审分类保持不变。

```bash
# v1：S01 的真值 cat 录错，实际为 dog
curl -sS -X POST http://localhost:8000/batches/$BID/errata \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 0,
    "corrections": [{"code": "S01", "true_label": "dog"}]
  }'
```

响应节选（分类标签沿用原件，仅真值与正误重算）：

```json
{
  "version": 1,
  "current_revision": 1,
  "results": [
    {"code": "S01", "final_label": "cat", "true_label": "dog", "reviewer1_label": "cat", "reviewer2_label": "cat", "agreed": true, "reviewer1_correct": false, "reviewer2_correct": false, "final_correct": false}
  ],
  "summary": {"sample_count": 3, "disagreement_count": 1, "reviewer1_correct": 2, "reviewer2_correct": 1, "final_correct": 2},
  "corrected_samples": [{"code": "S01", "old_true_label": "cat", "new_true_label": "dog"}]
}
```

**整次拒绝规则**（任一不满足，本次不落任何版本）：

- 只能在封存后提交；未封存返回 `409`；评审凭据一律返回 `401`（只有仲裁凭据可操作）。
- 只能引用**本批次**样本代号，新真值必须在 `allowed_categories` 内，否则 `422`。
- 同次请求内代号重复、更正列表为空，返回 `422`。
- 没有实际变化（新真值与该版当前真值相同），返回 `422`。
- `expected_revision` 与当前修订号不一致（过期或超前）返回 `409`，并在响应体给出当前修订号：

```bash
curl -sS -X POST http://localhost:8000/batches/$BID/errata \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0, "corrections": [{"code": "S02", "true_label": "cat"}]}'
# 409 {"detail":{"detail":"expected_revision does not match the current revision","current_revision":1}}
```

用返回的修订号重试即可继续：

```bash
# v2：一次更正多个样本
curl -sS -X POST http://localhost:8000/batches/$BID/errata \
  -H "X-Arbiter-Token: $ARB" -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 1,
    "corrections": [
      {"code": "S02", "true_label": "cat"},
      {"code": "S03", "true_label": "dog"}
    ]
  }'
```

### 7. 按版本查询勘误结果

```bash
# 版本 0 == 封存原件；版本号不存在返回 404（响应体含 current_revision）
curl -sS http://localhost:8000/batches/$BID/errata/0
curl -sS http://localhost:8000/batches/$BID/errata/1
curl -sS http://localhost:8000/batches/$BID/errata/2

# 列出全部版本及每版相对上一版更正的样本（含旧值/新值）
curl -sS http://localhost:8000/batches/$BID/errata
```

每个版本的结果都：

- 保留原有 `final_label`（最终分类）与 `reviewer1_label` / `reviewer2_label`（两份评审分类）以及 `agreed`；
- 按**该版真值**逐样本重算 `reviewer1_correct`、`reviewer2_correct`、`final_correct`，并重算 `summary` 中的三方正确数；
- 以 `corrected_samples` 列出本版相对上一版更正的样本。

原有查询不受影响：`GET /batches/{id}` 与 `GET /batches/{id}/results` 永远返回封存原件；交卷与裁定在勘误后依然返回 `409`，不能更改。并发勘误按修订号顺次生效——相同 `expected_revision` 的并发请求仅一人成功，其余返回 `409` 与当前修订号；失败不会留下部分版本，各版持久化、重启后仍可查询。

## 设计要点

- **凭据**：`secrets.token_urlsafe(32)` 生成，数据库只存 SHA-256 哈希，仅在创建响应中明文返回一次；评审凭据与仲裁凭据角色互不通用（401）。
- **不可改写**：交卷与裁定均有唯一约束与状态机保护，重复提交返回 `409` 且原数据不变。
- **交错请求安全**：所有写操作在进程级锁内以 `BEGIN IMMEDIATE` 事务执行；校验失败整体回滚。封存时一次性写入不可变的 `finals` 结果表，揭盲查询只读该表——不会提前揭盲，也不会出现两份不同结果。
- **勘误版本**：勘误只追加写入 `errata_versions` / `errata_items`（含每版旧值/新值），`finals` 封存原件永不修改；版本 0 直接由 `finals` 复现。按版本查询时把 1..N 版的更正叠加到原件真值上重算正误，分类标签全部沿用原件。期望修订号构成乐观锁：整请求校验（代号、类别、重复、无变化）通过后才比对修订号，任何一步失败都在同一事务内回滚，不留半截版本。
- **持久化**：SQLite 文件（WAL 模式）落盘，Compose 挂载命名卷，重启后数据仍在（含全部勘误版本）。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

覆盖：完整流程（有/无分歧）、重复提交不改写、盲态信息隔离、凭据角色隔离、参数校验、并发重复交卷仅一人成功、交错请求不提前揭盲、重启后数据一致；勘误：封存后才可用、评审凭据拒绝、版本 0 等同原件、更正后逐样本与三方正确数重算、原件查询不变、重复代号/未知代号/越界类别/无变化整次拒绝、修订号过期返回当前修订号、并发勘误按修订号顺次生效、失败不留版本、交卷与裁定勘误后仍不可改、多版本链式更正、重启后各版可查并可继续勘误。
