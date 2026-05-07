# Railway 部署故障排查指南

## 当前问题

Railway 上的应用无法访问，原因是：

1. **前端轮询过于频繁** — 每个上传的任务都会被轮询，每 1.2 秒一次
2. **任务状态未更新** — 后端可能卡住或 Redis/MongoDB 连接失败
3. **请求堆积** — 导致内存溢出、连接耗尽、进程卡死

从日志看：多个客户端同时轮询同一个 `run_id`，导致大量 GET 请求堆积。

## 立即恢复步骤（紧急修复）

### 1. 重启 Railway 应用

进入 Railway Dashboard：
1. 打开你的项目
2. 找到 FastAPI 服务
3. 点击 **Redeploy** 或 **Stop** → **Start**
4. 应用应在 1-2 分钟内恢复

### 2. 部署代码修复

已修复以下问题：

#### 前端轮询改进（web/demo.html）
- ✅ 添加 10 分钟超时保护 — 避免无限轮询
- ✅ 添加动态间隔（exponential backoff） — 长任务自动降低轮询频率（1.2s → 5s）
- ✅ 添加无进度检测 — 20 次轮询无状态变化时警告

#### 后端日志增强（src/tasks.py）
- ✅ 添加详细日志记录任务执行各阶段
- ✅ 改进异常捕获和错误记录

## 部署修复代码

```bash
git add -A
git commit -m "Fix: Add polling timeout and exponential backoff to prevent Railway overload

- Add 10-minute max timeout for polling
- Implement exponential backoff for long-running tasks (1.2s → 5s)
- Add detailed logging for task execution stages
- Add no-progress detection warning"
git push
```

Railway 会自动检测代码更新并重新部署。

## 后续监控

### 监控指标（在 Railway Dashboard 查看）

1. **内存使用** — 如果仍然飙升，说明有任务泄漏
2. **CPU 使用** — 正常任务应该在 30%-60%
3. **请求数** — 修复后应显著下降
4. **错误日志** — 检查是否有 MongoDB/Redis 连接错误

### 检查任务是否卡住

访问 API：
```bash
curl -H "X-API-Key: your-api-key" \
  https://your-railway-app.up.railway.app/api/runs?limit=20
```

查看返回的 runs：
- `status: "completed"` — 正常完成 ✅
- `status: "failed"` — 失败（查看 error 字段）
- `status: "running"` 超过 30 分钟 — 任务卡住 ⚠️
- `status: "queued"` — 等待队列消费（正常）

### 如果任务仍卡住

检查 Redis 连接（如果配置了）：

```python
# 在 Railway 日志中寻找这些错误
# - "Redis connection failed"
# - "MongoDB connection timeout"
# - "Job timeout exceeded"
```

## 配置优化建议

编辑 `config/settings.yaml`：

```yaml
concurrency:
  llm_global_max:       4        # 降低并发（如果 API 限流）
  fetch_workers:        4        # 降低网页抓取并发

queue:
  worker_concurrency:   1        # 单个任务一次只有 1 个并发
  job_timeout:          600      # 降低到 10 分钟，快速失败而非无限卡住

web:
  # 增加 Railway 特定的 CORS 配置
  cors_origins:
    - "*"  # 或指定具体域名
```

## 长期解决方案

1. **实现 WebSocket** 替代轮询 — 减少 90% 的 HTTP 请求
2. **添加任务队列监控** — 定期扫描和清理僵尸任务
3. **使用 MongoDB TTL 索引** — 自动删除过期任务
4. **配置告警** — 当任务卡住 > 5 分钟时发送通知

## 需要帮助？

检查这些日志：
1. Railway Dashboard → Logs
2. 搜索 "ERROR" 或 "timeout"
3. 查看最后 100 行
