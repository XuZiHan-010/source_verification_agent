# 09 · Schema — 模块间数据合同

所有跨模块数据用 `pydantic` 定义，下面给出字段级规格。修改 schema 必须同步更新本文件。

## IR (Ingestor → Extractor) {#ir}

```python
class Block(BaseModel):
    type: Literal["heading", "paragraph", "table", "list"]
    level: int | None = None        # heading 用
    text: str | None = None         # heading/paragraph/list
    rows: list[list[str | None]] | None = None   # table
    page: int | None = None
    bbox: tuple[float,float,float,float] | None = None
    hyperlinks: dict[str, str] = {}              # "row,col" -> url
    footnotes: dict[int, str] = {}               # footnote number -> url, per PDF page/table

class IR(BaseModel):
    doc_id: str
    source_format: Literal["pdf","docx","txt","md"]
    blocks: list[Block]
```

## Claim (Extractor → Resolver/Verifier) {#claim}

```python
class Claim(BaseModel):
    claim_id: str                    # f"{doc_id}#t{table_idx}#r{row_idx}" 或 f"{doc_id}#p{para_idx}"
    section_path: list[str]
    metric: str | None
    value: str | None
    year: str | None                 # 保留原文，"2024年底" 不强转
    region: str | None
    statement: str                   # 自然语言重述，喂给 Verifier
    source_name_raw: str             # 原文「来源名称」字段
    source_url_hint: str | None      # 抽到的域名/URL 片段；若是完整 URL，Verifier 优先直接打开它读原文
    extra_source_urls: list[str]      # 同一来源单元格脚注解析出的其余 URL
    source_name_with_marks: str | None # 去掉脚注标记前的来源原文
    publish_time: str | None
    notes: str | None
    is_forecast: bool = False
```

## ResolvedSource (Resolver → Verifier/Classifier)

```python
class ResolvedSource(BaseModel):
    claim_id: str
    resolution_method: Literal["hyperlink","whitelist","user_provided","failed"]
    url: str | None                  # Verifier 实际打开并读取正文的目标 URL
    domain: str | None
    title: str | None
    fetch_status: Literal["ok","404","forbidden","timeout","paywalled","skipped"]
    local_cache_path: str | None
    content_type: Literal["html","pdf","text","unknown"] | None
    content_hash: str | None
```

## VerifyResult (Verifier → Reporter)

```python
class VerifyResult(BaseModel):
    claim_id: str
    verdict: Literal["supported","partially_supported","not_found","contradicted","not_verifiable"]
    confidence: float                # 0..1
    evidence_quote: str | None
    evidence_locator: str | None     # "page=3" / "para=12" / "url-anchor=#sec2"
    discrepancy: str | None
    reasoning: str
```

## ClassifyResult (Classifier → Reporter)

```python
class ClassifyResult(BaseModel):
    claim_id: str
    tier: Literal["A","B","C"]
    tier_reason: str
    matched_rule: str                # "whitelist:A:gov-cn" / "llm_fallback" / "fetch_failed"
```

## Report (Orchestrator → CLI/User)

```python
class Report(BaseModel):
    run_id: str
    input_path: str
    output_path: str
    summary: dict[str, int]          # {"total":..., "A":..., "B":..., "C":..., "supported":..., ...}
    started_at: datetime
    finished_at: datetime
    cost_usd: float | None
    cache_hit_rate: float
```

## RunTask (FastAPI / Worker / MongoDB Atlas)

```python
class RunTask(BaseModel):
    run_id: str                      # UUIDv4，禁止可枚举 ID
    owner_id: str                    # API key / session token 的 sha256，按此过滤所有读路由
    status: Literal["queued","running","completed","failed","cancelled"]
    input_kind: Literal["file","text"]
    input_filename: str | None
    input_path: str | None           # 单机部署用本地路径；多机部署改为 storage_uri
    requested_format: Literal["xlsx","md","html","json"]
    detailed: bool = False
    total_claims: int = 0
    completed_claims: int = 0
    current_stage: Literal[
        "queued","ingest","extract","resolve","verify","classify","report","completed","failed"
    ]
    summary: dict[str, int]
    artifact_ids: list[str]
    error: str | None
    created_at: datetime
    updated_at: datetime             # 僵尸扫描依赖此字段
    started_at: datetime | None
    finished_at: datetime | None
```

MongoDB 集合：`runs`。

## RunEvent (Worker → Frontend Timeline)

```python
class RunEvent(BaseModel):
    event_id: str
    run_id: str
    stage: str
    level: Literal["info","warning","error"]
    message: str
    progress_current: int | None
    progress_total: int | None
    created_at: datetime
```

MongoDB 集合：`events`，按 `{run_id, created_at}` 建索引。

## Artifact (Reporter → Download API)

```python
class Artifact(BaseModel):
    artifact_id: str
    run_id: str
    owner_id: str
    fmt: Literal["xlsx","md","html","json"]
    storage_uri: str                 # local: file:///abs/path; s3: s3://bucket/key
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
```

MongoDB 集合：`artifacts`。文件本体不进 MongoDB，默认位于 `data/reports/<run_id>/`；切换为对象存储时只改 `storage_uri` 与下载路由的解析逻辑。

## MongoDB 索引

| 集合 | 索引 | 用途 |
|---|---|---|
| `runs` | `{owner_id: 1, created_at: -1}` | 用户列表查询 |
| `runs` | `{status: 1, updated_at: 1}` | 僵尸任务扫描 |
| `runs` | `{run_id: 1}` 唯一 | 主键查询 |
| `claims` | `{run_id: 1, claim_id: 1}` 唯一 | 防重复写入 |
| `claims` | `{run_id: 1}` | 按 run 拉全量 |
| `events` | `{run_id: 1, created_at: 1}` | 前端 timeline |
| `artifacts` | `{run_id: 1}` | 下载查询 |
| `artifacts` | `{sha256: 1}` | 同文件去重（可选） |

## 不变量

- 每条 Claim 一定对应**恰好一个** VerifyResult 和**恰好一个** ClassifyResult（用 `claim_id` join）。
- `verdict=not_verifiable` ⇔ `ResolvedSource.fetch_status` 不是 `ok`，或 `resolution_method=failed`。
- `tier=A` 不依赖也不影响 `verdict`。
- Web 模式下每个 `run_id` 一定对应一个 `RunTask`；`status=completed` 时至少有一个 `Artifact`。
- MongoDB 只存结构化元数据和文本级结果；上传原件、PDF 缓存和导出报告存文件系统或对象存储。
