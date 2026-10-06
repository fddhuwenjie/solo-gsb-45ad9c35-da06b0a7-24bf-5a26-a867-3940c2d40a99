# 盲样分类复核 API（Blind Classification Review）

用 Python + FastAPI + SQLite 实现的盲样分类复核 HTTP API。完整流程：

1. **建批**：提交允许类别与样本（代号 + 真实类别），创建响应中一次性返回**两份评审凭据 + 一份仲裁凭据**（仅此一次，数据库只存哈希）。
2. **双评审独立分类**：两名评审各持凭据提交覆盖全部样本的分类；**交卷后不可修改**。
3. **盲态**：封存前普通查询只返回样本代号与阶段；评审只能查到自己的提交状态，看不到真值，也看不到对方分类。
4. **仲裁**：两份交卷齐全后，仲裁者凭专属凭据查看分歧样本及双方分类，逐项从两者中选择最终分类；一致样本直接取共同分类。**无分歧时第二份交卷即直接封存。**
5. **封存揭盲**：分歧全部裁定后一次性封存。查询返回逐样本最终分类、真值、两名评审各自正确数。封存结果落库后不再变化。

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
| GET | `/batches/{id}/results` | 无 | 揭盲结果（仅封存后可用） |
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

## 设计要点

- **凭据**：`secrets.token_urlsafe(32)` 生成，数据库只存 SHA-256 哈希，仅在创建响应中明文返回一次；评审凭据与仲裁凭据角色互不通用（401）。
- **不可改写**：交卷与裁定均有唯一约束与状态机保护，重复提交返回 `409` 且原数据不变。
- **交错请求安全**：所有写操作在进程级锁内以 `BEGIN IMMEDIATE` 事务执行；校验失败整体回滚。封存时一次性写入不可变的 `finals` 结果表，揭盲查询只读该表——不会提前揭盲，也不会出现两份不同结果。
- **持久化**：SQLite 文件（WAL 模式）落盘，Compose 挂载命名卷，重启后数据仍在。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

覆盖：完整流程（有/无分歧）、重复提交不改写、盲态信息隔离、凭据角色隔离、参数校验、并发重复交卷仅一人成功、交错请求不提前揭盲、重启后数据一致。
