# Railway Volume 自动清理指南

## 问题

Railway 的 **volume 不会自动清空**，需要手动配置清理策略。否则：
- Volume 快速填满 → 应用无法写入文件 → 崩溃
- 表现为 `OOM` 或 `Disk quota exceeded` 错误

## 解决方案已实现

### 1. 自动周期清理（每小时）

在 API 的 `GET /api/runs?limit=20` 端点中自动触发：

```python
# src/api.py 中的 list_runs() 函数
if now - last_cleanup_time["value"] > 3600:  # 每小时触发一次
    run_cleanup(settings, dry_run=False)
```

**清理策略（Railway 优化版本）**：
- 删除 **3 天前** 的任务数据（之前是 7 天）
- 清理 **3 天前** 的缓存文件（更激进）
- 删除 **12 小时前** 的上传文件（非常激进）
- 清理 **3 天前** 的报告目录

### 2. 监控 Volume 使用

**API 端点**：
```bash
curl https://your-railway-app.up.railway.app/api/volume
```

**返回**：
```json
{
  "cache_mb": 45.2,
  "uploads_mb": 12.3,
  "reports_mb": 234.5,
  "total_mb": 292.0,
  "warning": false
}
```

- 如果 `total_mb > 500`，则 `warning: true`

### 3. 手动触发清理

```bash
curl -X POST https://your-railway-app.up.railway.app/api/cleanup
```

**返回**：
```json
{
  "deleted_runs": 5,
  "deleted_cache": 42,
  "deleted_uploads": 18,
  "deleted_reports": 3,
  "disk_usage_mb": {
    "cache": 45.2,
    "uploads": 12.3,
    "reports": 234.5,
    "total": 292.0
  }
}
```

## Railway 配置

### 1. 应用启动时自动清理（推荐）

编辑 `src/market_source_verification_agent/main.py` 或入口文件：

```python
from fastapi import FastAPI
from .api import create_app
from .startup import run_startup_cleanup

# 创建应用
app = create_app()

# 在启动时运行清理
@app.on_event("startup")
async def startup():
    run_startup_cleanup()
```

在 Railway 的 `Procfile` 中添加：

```
web: python -m uvicorn market_source_verification_agent.api:app --host 0.0.0.0 --port $PORT
```

### 2. Railway 环境变量配置

在 Railway Dashboard 的环境变量中设置：

```
# 禁用 Python 字节码缓存，节省磁盘
PYTHONDONTWRITEBYTECODE=1

# 不缓冲输出，实时看日志
PYTHONUNBUFFERED=1
```

### 3. 限制并发，减少磁盘占用

编辑 `config/settings.yaml`：

```yaml
concurrency:
  fetch_workers: 2          # 减少并发抓取
  llm_workers: 1            # 减少 LLM 并发
  llm_global_max: 1         # 全局最多 1 个

queue:
  worker_concurrency: 1     # 单个任务一次
  job_timeout: 600          # 10 分钟超时，快速失败

cache:
  fetch_ttl_days: 1         # 缓存只保留 1 天
  verify_ttl_days: 1
  classify_ttl_days: 1
```

### 4. 文件大小限制

添加到应用启动代码（`main.py`）：

```python
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware import Middleware

async def check_upload_size(request: Request, call_next):
    """限制上传文件大小为 50MB"""
    if request.method == "POST" and "/api/runs" in request.url.path:
        if "content-length" in request.headers:
            size = int(request.headers["content-length"])
            if size > 50 * 1024 * 1024:  # 50MB
                raise HTTPException(status_code=413, detail="File too large")
    return await call_next(request)

app.middleware("http")(check_upload_size)
```

## 监控脚本（可选）

如果想要更主动的监控，可以在 Railway 中创建一个定期检查的脚本：

### 方案 A：使用 Linux Cron（需要自定义镜像）

在 `Dockerfile` 中：

```dockerfile
FROM python:3.11

# 安装 curl 用于健康检查
RUN apt-get update && apt-get install -y curl

# 你的应用代码...
COPY . /app
WORKDIR /app

# 启动应用和清理脚本
CMD ["sh", "-c", "python -m uvicorn ... & while true; do sleep 3600 && curl -X POST http://localhost:8000/api/cleanup; done"]
```

### 方案 B：使用 Python 后台任务（更简单）

在 `api.py` 中添加：

```python
import asyncio
from datetime import datetime

class CleanupScheduler:
    def __init__(self):
        self.last_cleanup = 0
        self.cleanup_interval = 3600  # 1 hour
    
    async def start(self):
        """后台任务：定期清理"""
        while True:
            now = time.time()
            if now - self.last_cleanup > self.cleanup_interval:
                try:
                    run_cleanup(settings, dry_run=False)
                    self.last_cleanup = now
                except Exception as exc:
                    logger.error(f"Background cleanup failed: {exc}")
            await asyncio.sleep(60)  # 每分钟检查一次

scheduler = CleanupScheduler()

@app.on_event("startup")
async def startup():
    asyncio.create_task(scheduler.start())
```

## 常见问题

### Q1: Volume 还是满了，怎么办？

**立即操作**：

1. 手动触发清理：
```bash
curl -X POST https://your-app.up.railway.app/api/cleanup
```

2. 检查 Volume 使用：
```bash
curl https://your-app.up.railway.app/api/volume
```

3. 如果还是超过 800 MB，删除所有旧任务：
```bash
curl -X DELETE https://your-app.up.railway.app/api/runs
```

4. 重启应用：
在 Railway Dashboard → Redeploy

### Q2: 每小时清理一次够吗？

取决于使用量：
- 低使用（< 10 个任务/天） → 每小时清理足够
- 中等使用（10-100 个任务/天） → 每 30 分钟清理
- 高使用（> 100 个任务/天） → 考虑升级 Volume 或使用 MongoDB

修改清理间隔（`api.py`）：
```python
cleanup_interval_seconds = 1800  # 改为 30 分钟
```

### Q3: 删除了任务后还能下载结果吗？

**不能**。清理过程会：
1. 删除任务数据库记录
2. 删除生成的报告文件

解决方案：
- 提供下载前清理过期报告的通知
- 或者配置 MongoDB，让旧任务自动在数据库中过期（保持报告文件）

### Q4: 如何在 Railway 上看到清理日志？

在 Railway Dashboard：
1. 进入项目
2. 点击 FastAPI 服务
3. 在 **Logs** 标签搜索 "cleanup"

看到这些日志表示清理在运行：
```
cleanup: starting scheduled cleanup
cleanup: deleted cache test-file.json
cleanup: completed - {'deleted_runs': 2, 'deleted_cache': 5, ...}
```

### Q5: Volume 大小建议多少？

根据使用量：

| 使用量 | Volume 大小 | 备注 |
|--------|-----------|------|
| 测试/演示 | 1 GB | 够清理 1000 个任务 |
| 小型应用 | 5 GB | 够清理 5000 个任务 |
| 中型应用 | 10 GB | 建议用 MongoDB + S3 |

在 Railway Dashboard 中升级 Volume：
Projects → Settings → Volumes

## 最佳实践

1. **定期检查 Volume 使用**
```bash
# 每天检查一次
curl https://your-app.up.railway.app/api/volume
```

2. **设置告警**
   - Volume 使用 > 70% 时发送通知
   - 可以使用 Railway 的 Monitoring 功能

3. **定期审查日志**
   - 搜索 "ERROR" 或 "cleanup failed"
   - 确保清理流程没有卡住

4. **配置备份**
   - 重要的报告在清理前下载
   - 或者配置 S3 存储永久保存

5. **容量规划**
   - 监控 Volume 增长速度
   - 提前升级而不是等到满了再处理

## 总结

| 问题 | 解决方案 |
|------|--------|
| Volume 无法自动清空 | ✅ 已实现自动周期清理 |
| 不知道 Volume 用了多少 | ✅ 添加 `/api/volume` 监控端点 |
| 需要手动清理 | ✅ 提供 `/api/cleanup` 手动触发 |
| 清理太慢 | ✅ 改用更激进的策略（3 天 → 1 天） |
| 无法看到清理日志 | ✅ 在 Railway Logs 中搜索 "cleanup" |

部署这些改进后，Railway 应该能长期稳定运行。
