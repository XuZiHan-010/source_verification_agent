# Railway 内存优化指南

## 问题诊断

Railway OOM（内存溢出）通常由以下原因导致：

1. **旧任务和缓存堆积** — 没有定期清理
2. **日志文件无限增长** — `server.log` 等未轮转
3. **临时文件未清理** — 上传和中间文件堆积
4. **Python 进程内存泄漏** — 某些库未正确释放资源

## 已实现的自动清理机制

### 1. 定期自动清理（每小时）

在 `api.py` 的 `list_runs` 端点中自动触发：
- 删除 **7 天前**的任务数据
- 清理 **30 天前**的缓存文件
- 删除 **1 天前**的上传文件
- 清理 **30 天前**的报告目录

每次调用 `GET /api/runs?limit=20` 时检查，如果距离上次清理已超过 1 小时则触发。

### 2. 手动清理 API

```bash
curl -X POST https://your-railway-app.up.railway.app/api/cleanup
```

返回：
```json
{
  "deleted_runs": 5,
  "deleted_cache": 42,
  "deleted_uploads": 18,
  "deleted_reports": 3
}
```

## Railway 配置

### 1. 限制内存使用

在 Railway 项目设置中：
- **Memory**: 设置为 `512 MB` 或 `1 GB`（根据用户量）
- **启用自动重启**: 内存超过阈值时自动重启

### 2. 配置文件轮转

为防止日志文件无限增长，编辑 `config/settings.yaml`：

```yaml
logging:
  level: INFO  # 不要用 DEBUG
  max_file_size: 10MB
  backup_count: 3
```

### 3. 环境变量配置

在 Railway 环境变量中添加：

```
# 缩小内存占用
PYTHONUNBUFFERED=1
PYTHONHASHSEED=random

# 禁用 Python 字节码缓存
PYTHONDONTWRITEBYTECODE=1

# 限制并发数（防止内存爆炸）
CONCURRENCY_LLM_WORKERS=2
CONCURRENCY_FETCH_WORKERS=4
```

### 4. Worker 进程限制

编辑 `config/settings.yaml`：

```yaml
concurrency:
  fetch_workers:        4      # 降低从 8 到 4
  llm_workers:          2      # 降低从 4 到 2
  llm_global_max:       2      # 降低从 8 到 2

queue:
  worker_concurrency:   1      # 每次只处理 1 个任务
  job_timeout:          600    # 降低到 10 分钟，快速失败
```

## 监控和告警

### 1. 检查当前内存状态

```bash
# Railway Dashboard 中查看
# Metrics → Memory
```

### 2. 启用内存日志

在 FastAPI 启动时添加日志：

```python
import psutil
import logging

logger = logging.getLogger(__name__)

def log_memory_stats():
    process = psutil.Process()
    memory_info = process.memory_info()
    logger.info(f"Memory usage: {memory_info.rss / 1024 / 1024:.1f} MB")

# 在每个请求前后调用
```

### 3. 自动清理的验证

查看 Railway 日志，搜索 "cleanup: completed"：

```
2026-05-07 15:42:01 cleanup: completed - {'deleted_runs': 5, 'deleted_cache': 42, 'deleted_uploads': 18, 'deleted_reports': 3}
```

## 长期解决方案

### 1. 使用 MongoDB Atlas 替代本地存储

将任务和缓存移到 MongoDB：
- 任务自动过期（TTL 索引）
- 不占用应用内存
- 支持水平扩展

配置 `config/settings.yaml`：
```yaml
task_store_backend: mongodb  # 从 local 改为 mongodb
mongodb:
  uri_env: MONGODB_URI
```

### 2. 使用 S3 存储报告

```yaml
storage:
  backend: s3
  s3:
    bucket: your-bucket
    region: ap-east-1
```

### 3. 实现周期性的进程重启

在 Railway 中添加 Cron job，每 12 小时重启一次应用：
```
0 */12 * * * railway up --restart
```

## 紧急操作

如果 Railway 再次 OOM：

### 1. 立即重启
```bash
# Railway Dashboard
Redeploy → Stop → Start
```

### 2. 清理所有旧任务
```bash
# 手动删除本地存储
rm -rf data/reports/*
rm -rf data/cache/*
rm -rf data/uploads/*
```

### 3. 临时限制并发
修改 `config/settings.yaml`：
```yaml
concurrency:
  fetch_workers: 2
  llm_workers: 1
```

### 4. 临时禁用缓存
```yaml
cache:
  fetch_ttl_days: 0  # 禁用缓存
```

## 性能基准

| 场景 | 内存占用 | 备注 |
|------|--------|------|
| 空闲 | ~80 MB | 初始状态 |
| 单个任务运行 | ~200-300 MB | 正常 |
| 10 个任务队列 | ~400-500 MB | 可接受 |
| 100 个任务堆积 | ~800+ MB | OOM 风险 |

如果内存超过 60% 应立即触发清理。

## 监控脚本

在 Railway 中运行这个脚本定期检查：

```python
# scripts/monitor_memory.py
import psutil
import requests
import os

memory_percent = psutil.virtual_memory().percent
print(f"Memory usage: {memory_percent:.1f}%")

if memory_percent > 80:
    api_url = os.getenv("API_URL")
    requests.post(f"{api_url}/api/cleanup")
    print("Cleanup triggered")
```

在 Procfile 中添加：
```
monitor: python scripts/monitor_memory.py
```

## 常见错误

### "MemoryError: Unable to allocate..."
- 原因：单个任务处理数据过大（PDF > 500MB）
- 解决：限制上传文件大小，分块处理

### "Process killed"
- 原因：Railway 因 OOM 强制杀死进程
- 解决：立即清理，重启，降低并发

### "Redis timeout"
- 原因：内存不足，Redis 反应迟钝
- 解决：清理本地存储，减少队列长度

## 最佳实践

1. **定期监控** — 每天检查一次内存使用
2. **提前清理** — 设置在 50% 内存时触发清理
3. **日志轮转** — 限制日志文件大小
4. **任务超时** — 设置合理的超时时间
5. **渐进式扩展** — 从低配到高配，测试内存影响
