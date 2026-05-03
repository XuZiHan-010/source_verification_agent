# 前端全面美化 — 企业级 Analytics Dashboard 重设计

## Context

此次重设计面向 Henkel 战略与市场部门，作为实习交付产品。当前界面过于"开发者/黑客终端"风格（`//` 注释式标签、极暗纯黑背景、decorative serif 字体），不适合非技术业务受众。目标：在保留所有 JS 逻辑不变的前提下，将视觉风格升级为**专业内部分析工具**（精致 · 可信 · 数据导向）。

---

## 设计方向：Precision Analytics Dark

**核心关键词**：深海蓝底、蓝宝石主色、精致数据卡片、清晰层级、去除 dev jargon

### 1. 字体
| 用途 | 字体 |
|---|---|
| 英文/数字标题 display | `Sora` (Google Fonts) |
| 中文正文 | `Noto Sans SC` |
| 数据/标签/代码 | `JetBrains Mono` (保留) |

### 2. 颜色 token 重设
```css
--bg:        #080C14   /* 深海蓝底，非纯黑 */
--bg-2:      #0C1120
--surface:   #111827   /* warm dark navy */
--surface-2: #1B2333
--border:    #1F2D42
--border-2:  #2A3D58
--text:      #E2E8F0
--text-2:    #94A3B8
--text-3:    #4B5E79
--text-4:    #2D3D52

--accent:    #3B82F6   /* 品牌主色：专业蓝 */
--accent-bg: rgba(59,130,246,.10)

--ok:        #10B981   /* 核验通过 */
--warn:      #F59E0B   /* 部分支持 */
--bad:       #F87171   /* 未找到 */
--crit:      #DC2626   /* 矛盾 */
--mute:      #4B5E79   /* 无法验证 */

--flag:      #EF4444   /* 标红/可疑 */

--tier-a:    #60A5FA
--tier-b:    #34D399
--tier-c:    #F87171
```

### 3. 主要视觉变更

#### Nav
- 品牌名改为中英双行：「源验」/「Source Verify」
- 增加 Henkel internship 小标签 `STRATEGIC INTELLIGENCE v0.1`
- API Key 字段更优雅（label内嵌 icon）
- 历史记录按钮：改为 outline 蓝色按钮

#### Hero
- 去除 `//` 前缀标签
- 上传标签：「上传文件」「粘贴文本」（非 `// 上传文件`）
- 主标题：保持中文有力 copy，但字体换 Sora italic + Noto Sans SC
- 副文案保持中文业务语境
- 上传卡：玻璃态 (glassmorphism-lite) — `backdrop-filter + border-glow`

#### Pipeline 进度条
- stage 卡片更宽松，加 icon 前缀（数字圆圈）
- 完成状态：绿色 checkmark，非单纯文字
- 优化底部进度线动画

#### Summary 统计卡
- 4 卡保持，但内部布局更干净
- 数字更大更有力（`font-size: 64px`）
- 百分比进度条改为更优雅的弧形/分段进度

#### Report 表格
- Filter pill → **segmented control** 样式（更接近业务工具）
- 下载按钮组：独立区域，主按钮 XLSX highlighted
- 表头：去掉 `added` 红色标注，改为蓝色左border
- 行 hover：更明显的蓝色左侧 highlight
- Verdict badge：pill 样式带背景色，更易读
- 来源列：去掉 scrollable box，直接截断 + tooltip

#### Drawer 详情面板
- 更干净的 key-value 布局
- 多源核验明细：用卡片列表代替纯文本
- 证据引用：更精致的引用块

#### Footer
- 简化为只有 Product info + 版本号

---

## 关键约束（JS 钩子 — 不得更改）

以下 HTML id/class 必须原封不动保留：
- IDs: `fileInput`, `drop`, `pipeline`, `report`, `mask`, `drawer`, `drawer-id`, `drawer-body`, `drawer-close`, `histMask`, `histPanel`, `histBody`, `histClose`, `apiKeyInput`
- Classes: `.filter-pill[data-filter]`, `.download-group button`, `.summary .stat`, `.upload-tabs button[data-tab]`, `[data-pane]`, `#pipeline .stage`, `.stage .status`, `.stage .bar`, `.stage .num`, `.stage .name`
- Selectors in JS: `.hero .corner-meta`, `#report tbody`, `.verdict`, `.tier`, `.metric`, `.num`, `.year`, `.src`, `.domain`, `.row-id`, `.reason`, `.col-verdict`, `.col-tier`
- Global functions: `runDemo`, `openHistory`
- `.btn-primary` on the run buttons
- JS script block: 完全不改变

---

## 修改文件

- [web/demo.html](web/demo.html) — 完全重写 `<style>` + 局部 HTML label 改进（不动 `<script>`）

---

## 验证

1. 在浏览器打开 `web/demo.html` 检查页面渲染
2. 上传测试文件确认所有按钮/拖拽功能正常
3. 检查 filter pills / download group 功能
4. 检查 drawer 打开/关闭
5. 检查 history panel 打开/关闭
6. 检查响应式（缩小到 960px 以下）
