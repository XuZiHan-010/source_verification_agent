# Railway 故障快速修复指南

## 当前问题：Volume 满了导致应用崩溃

**症状**：
- Railway 显示 "Disk quota exceeded" 或应用不响应
- `OOM killed` 错误
- 无法上传新文件

**根本原因**：
- Railway volume 不会自动清空
- 旧的任务、缓存和上传文件堆积
- Volume 填满后应用崩溃

---

## 快速修复步骤

### 1️⃣ 立即重启应用（恢复访问）

在 Railway Dashboard：
1. 找到你的 FastAPI 服务
2. 点击 **Redeploy** 或 **Stop** → **Start**
3. 等待 1-2 分钟应用重启

### 2️⃣ 部署代码修复（长期解决）

```bash
git pull origin main
git push
```

这会自动部署包含自动清理机制的新代码。

### 3️⃣ 验证清理在运行

**方法 A：检查日志**
- Railway Dashboard → Logs
- 搜索 "cleanup: completed"
- 看到这条日志说明清理在工作

**方法 B：监控 Volume 使用**
```bash
curl https://your-railway-app.up.railway.app/api/volume
```

返回例子：
```json
{
  "cache_mb": 45.2,
  "uploads_mb": 12.3,
  "reports_mb": 234.5,
  "total_mb": 292.0,
  "warning": false
}
```

**方法 C：手动触发清理**
```bash
curl -X POST https://your-railway-app.up.railway.app/api/cleanup
```

---

## 新增功能说明

### 自动周期清理
- **触发**：每次调用 `GET /api/runs` 时检查
- **间隔**：每小时执行一次
- **清理策略**（Railway 优化版）：
  - 删除 3 天前的任务
  - 清理 3 天前的缓存
  - 删除 12 小时前的上传文件
  - 清理 3 天前的报告

### 新 API 端点

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/volume` | GET | 查看 Volume 使用情况 |
| `/api/cleanup` | POST | 手动触发清理 |

---

## 如果还是不行

### 情况 1：Volume 仍然在增长

**可能原因**：清理没有运行，或运行太慢

**解决**：
1. 手动执行清理：
```bash
curl -X POST https://your-app.up.railway.app/api/cleanup
```

2. 查看返回的删除数量：
```json
{
  "deleted_runs": 150,
  "deleted_cache": 500,
  "deleted_uploads": 200,
  "deleted_reports": 50
}
```

3. 如果显示 0，说明没有旧文件可删除，需要降低清理的天数阈值（联系我修改代码）

### 情况 2：应用仍然崩溃

**可能原因**：单个任务太大，或清理还是赶不上

**临时方案**：
1. 删除所有任务（清空 Volume）：
```bash
curl -X DELETE https://your-app.up.railway.app/api/runs
```

2. 重启应用

3. 长期：升级 Railway Volume 大小或迁移到 MongoDB

### 情况 3：看不到清理日志

**检查清理是否在运行**：
1. Railway Dashboard → Logs
2. 搜索这些关键词：
   - "cleanup: starting"
   - "cleanup: completed"
   - "deleted_"

如果没看到这些日志，可能是：
- 没有人在调用 `GET /api/runs`（前端不活跃）
- 清理间隔还没到（最多 1 小时）

**主动测试**：
访问前端，点击"查看历史记录"按钮，会触发 `GET /api/runs`

---

## 配置调整（可选）

如果 Volume 仍然增长太快，可以编辑 `config/settings.yaml`：

```yaml
# 更激进的清理（保留更少天数）
# 注：不需要改，默认已经是 Railway 优化版本

# 但如果还是不够，可以：
# - 减少并发数
# - 限制缓存时间

concurrency:
  llm_workers: 1      # 降低并发
  fetch_workers: 2

cache:
  fetch_ttl_days: 0   # 完全禁用缓存
```

---

## 监控建议

### 每天检查一次
```bash
# 脚本：check_volume.sh
curl https://your-app.up.railway.app/api/volume | jq .total_mb
```

### 设置告警
如果 `total_mb > 500`，就手动清理或升级 Volume

### 查看趋势
记录每天的 Volume 大小，看是否在增长

---

## 总结

| 问题 | 解决方案 | 时间 |
|------|--------|------|
| 应用无法访问 | 重启 Railway | 2 分钟 |
| Volume 满了 | 部署新代码 + 手动清理 | 10 分钟 |
| Volume 继续增长 | 监控 + 调整清理策略 | 持续 |
| 问题持续 | 升级 Volume 或用 MongoDB | 1 小时 |

**最快修复路径**：
1. 重启 Railway（恢复访问）
2. 推送新代码（部署修复）
3. 运行 `curl -X POST .../api/cleanup`（清空历史）
4. 每天监控 `curl .../api/volume`（防止再满）

---

## 相关文档

- **详细清理指南**：[RAILWAY_VOLUME_CLEANUP.md](RAILWAY_VOLUME_CLEANUP.md)
- **内存优化**：[RAILWAY_MEMORY_OPTIMIZATION.md](RAILWAY_MEMORY_OPTIMIZATION.md)
- **故障排查**：[RAILWAY_TROUBLESHOOT.md](RAILWAY_TROUBLESHOOT.md)
