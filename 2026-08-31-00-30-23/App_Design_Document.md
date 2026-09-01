# 🕵️ Gonka AI Fact Checker — App Design Document

> **应用设计文档 · Hackathon 参赛作品**
>
> 基于 **Gonka Router Gateway** 的多模型交叉验证事实核查系统
> **Multi-Model Cross-Verification Fact-Checking System**

| 项目 | 内容 |
|------|------|
| 项目名称 | Gonka AI Fact Checker（多模型交叉验证事实核查系统） |
| 架构 | Streamlit 前端 + OpenAI SDK / Gonka Router Gateway 后端 |
| 版本 | v1.0（Hackathon Demo） |
| 日期 | 2026-08-31 |
| 技术栈 | Streamlit · OpenAI SDK · Gonka Router · Requests · BeautifulSoup |

---

## 目录 Table of Contents

1. [Product Overview 产品概述](#1-product-overview-产品概述)
2. [User Flow 用户工作流](#2-user-flow-用户工作流)
3. [UI/UX Design Highlights 界面与设计亮点](#3-uiux-design-highlights-界面与设计亮点)
4. [Technical Stack 技术栈](#4-technical-stack-技术栈)
5. [数据流与错误处理](#5-数据流与错误处理)
6. [扩展路线图 Roadmap](#6-扩展路线图-roadmap)

---

## 1. Product Overview 产品概述

### 1.1 核心痛点 Core Pain Points

在信息爆炸时代，网络新闻、社媒帖文（Tweet）与网页文章鱼龙混杂，普通用户缺乏专业核查手段，面临三个核心痛点：

| # | 痛点 | 说明 |
|---|------|------|
| P1 | **单一模型不可信** | 依赖单一 LLM 判断真伪，容易出现"幻觉 + 盲区"，缺少交叉验证，结论不可靠。 |
| P2 | **核查过程黑盒** | 多数工具只给一个"真/假"结论，却不给出推理过程与证据链，用户无法判断结论依据、也无法复核。 |
| P3 | **输入门槛高** | 需要手动整理文本、复制正文，无法直接粘贴推文或输入网页链接，长文本核对费时费力。 |

> **一句话定位：** 让每一次"这是真的吗？"都能得到 —— **多模型交叉验证 + 可信评分 + 透明证据链**。

### 1.2 目标用户 Target Users

- **普通互联网用户**：想快速判断一篇新闻/推文是否可信。
- **媒体从业者 / 内容审核员**：需要证据留痕、可复核的核查流程。
- **开发者 / Hacker**：关注 LLM 多智能体协作与网关路由的工程实践。

### 1.3 解决方案价值 Proposition

利用 **Gonka Router Gateway 一张 API Key 接入多模型** 的能力，让两个在各自维度擅长的模型**并行**工作、互为印证，再由独立的 **Arbiter（仲裁器）** 融合双方证据，输出 **Truth Score（0-100%）** 与完整 **Reasoning Trace（推理轨迹）**——把"AI 说了算"升级为"多方证据共同裁决，结论全程可溯"。

### 1.4 多模型并行架构 Multi-Model Parallel Architecture

系统采用 **"双专家并行 + 独立仲裁"** 的架构。两个模型通过 **Gonka Router Gateway**（OpenAI 兼容端点 `https://api.gonkarouter.io/v1`）并发调用，各自只负责最擅长的核查维度，避免"一个模型干所有事"导致的偏科。

```
                 ┌─────────────────────────────────────────────────┐
                 │           Gonka Router Gateway                  │
                 │        https://api.gonkarouter.io/v1            │
                 │       （一张 API Key，多模型路由）              │
                 └──────┬───────────────────────────┬─────────────┘
                        │ 并发 ThreadPoolExecutor   │
              ┌─────────▼─────────┐        ┌────────▼──────────┐
              │   DeepSeek V4     │        │   MiniMax M2.7    │
              │    Flash-0731     │        │                   │
              │  快速逻辑漏洞提取  │        │  长文本 & 事实比对 │
              │  (fast reasoning) │        │  (long context)   │
              └─────────┬─────────┘        └────────┬──────────┘
                        │  JSON 结构化结论           │ JSON 结构化结论
                        │ (Label/Confidence/        │ (Label/Confidence/
                        │  Claims/Evidence)         │  Claims/Evidence)
                        └──────────────┬────────────┘
                                       ▼
                          ┌───────────────────────┐
                          │   ARBITER 仲裁器      │
                          │  (独立裁决模型)       │
                          │ 融合两侧证据 → 评分  │
                          └───────────────────────┘
                                       ▼
                          Truth Score (0-100%)
                          Verdict 分级标签
                          Reasoning Trace + Agreement
```

**模型分工 Division of Labour：**

| 模型 | 引擎 | 职责 | 维度优势 |
|------|------|------|----------|
| **DeepSeek V4 Flash** | `deepseek-ai/DeepSeek-V4-Flash-0731` | 快速逻辑漏洞提取：拆解子主张、识别偷换概念/因果谬误/数字失真/过度概括 | 推理速度快、逻辑严谨、成本低 |
| **MiniMax M2.7** | `MiniMaxAI/MiniMax-M2.7` | 长文本 & 事实比对：对照常识/公共知识，标出支持、反驳、无法定论之处 | 超长上下文（195K）、事实记忆丰富 |

### 1.5 Arbiter 仲裁机制 Arbitration Mechanism

**为什么需要仲裁？** 两个模型可能结论一致，也可能**分歧**（例如 DeepSeek 认为逻辑有漏洞，而 MiniMax 认为事实大体成立）。仲裁器（Arbiter）作为**中立的裁判角色**，负责把双方证据公平融合，避免"谁嗓门大听谁的"。

**仲裁输入（双方结构化证据）**
- DeepSeek：`label`（支持/存疑/证伪/混合）、`confidence`（0-100）、子主张清单 `claims[]`、`evidence`
- MiniMax：同类 JSON 结构，外加其主观置信度

**仲裁输出（单条 JSON）**

| 字段 | 类型 | 含义 |
|------|------|------|
| `veracity` | int 0-100 | **Truth Score**：0=完全虚假，100=完全真实 |
| `verdict` | string | 分级结论：`真实 / 基本真实 / 部分存疑 / 高度存疑 / 虚假 / 无法判断` |
| `reasoning` | string | 详细中文推理轨迹（引用两侧关键论据与分歧点） |
| `agreement` | string | 两模型一致点与分歧点的说明 |

**仲裁融合原则（Prompt 工程设计）**
1. **证据优先于立场**：只依据两侧给出的具体 claims/evidence，不凭印象。
2. **显式分歧处理**：当两模型结论冲突时，必须显式说明分歧并给出取舍理由。
3. **分级而非二元**：输出 0-100 连续评分，拒绝"非真即假"的粗暴二分。
4. **可追溯**：reasoning 中必须引用是哪一侧模型提供了哪条证据。

> **工程韧性：** 若任意一个核查模型调用失败，系统不崩溃 —— 仲裁器会跳过并以 `ARBITER_SKIPPED` 占位，前端仍展示可用一侧的分析与错误详情（每模型独立 try/except + 30s 超时 + `@st.cache_data` 缓存）。

---
## 2. User Flow 用户工作流

### 2.1 三种输入方式 Input Modes

系统支持三种来源，覆盖"一句话"到"整篇长文"：

| 输入方式 | 场景 | 处理方式 |
|----------|------|----------|
| **① 粘贴文本 / 新闻** | 手上有整段文字 | 直接作为声明文本送入核查链路 |
| **② 推文 Tweet** | 复制一条推文链接或文字 | 文字部分直接使用；链接自动抓取 |
| **③ 网页 URL** | 看到一篇在线文章 | 通过 **Requests + BeautifulSoup** 自动抓取正文 → 清洗 → 截断后核查 |

### 2.2 网页 URL 抓取流程 Article Scraping Pipeline

```
用户输入 URL
    │
    ▼
requests.get(url, headers=UA, timeout=10s)   ← 携带浏览器 UA，防反爬
    │
    ▼
BeautifulSoup(html, 'html.parser')           ← 解析 DOM
    │
filter: 移除 script/style/nav/footer/header/aside   ← 去噪
    │
    ▼
定位 <article>（回退到 <body>）→ get_text()
    │
    ▼
清洗空行 + 截取前 100 段（限制 Token，防撑爆上下文）
    │
    ▼
  声明文本 → 进入双模型核查
```

> **防呆设计：** 抓取失败（超时/404/反爬）时返回 `[Error fetching URL: ...]` 错误占位，前端给出提示而不是崩溃。

### 2.3 主流程端到端（End-to-End Flow）

```
┌──────────┐   ┌───────────┐   ┌──────────────────────────┐
│  输入声明  │ → │ 一键触发    │ → │  双模型并发核查（~并行） │
│ 文本/推文 │   │  Verify    │   │  DeepSeek ‖ MiniMax     │
└──────────┘   └───────────┘   └───────────┬──────────────┘
                                           ▼
        ┌──────────────────────────────────────────┐
        │            Arbiter 仲裁                   │
        │   融合证据 → Truth Score + Reasoning       │
        └───────────────────┬──────────────────────┘
                            ▼
   ┌────────────────────────────────────────────────────┐
   │     透明证据看板 Evidence Transparency Dashboard      │
   │  ┌─────────────┬─────────────┬───────────────────┐  │
   │  │ DeepSeek Tab│ MiniMax Tab │ Reasoning Trace   │  │
   │  │  分析+ReqID │  分析+ReqID │  仲裁理由+Agreement│  │
   │  └─────────────┴─────────────┴───────────────────┘  │
   │  顶部：Truth Score 进度条 + Verdict 徽章 + 仲裁 ReqID │
   └────────────────────────────────────────────────────┘
```

**逐步说明 Step-by-Step：**
1. **输入**：用户粘贴文本 / 推文，或填入 URL（自动抓取正文）。
2. **一键触发**：点击「🚀 验证（并发两模型）」。
3. **并发核查**：后台 `ThreadPoolExecutor(max_workers=2)` 同时发起两个 Gonka 请求，互不等待。
4. **仲裁融合**：两侧 JSON 结果喂给 Arbiter，产出 Truth Score + Verdict + Reasoning Trace。
5. **透明展示**：前端分 Tab 呈现双方分析与各自 **Gonka Request ID（response.id）**，顶部展示最终评分与推理轨迹。

---
## 3. UI/UX Design Highlights 界面与设计亮点

### 3.1 侧边栏 API 连接配置（Sidebar Connection Panel）

将**连接层与业务层分离**，用户无需改代码即可接入自己的 Gonka Key：

- 🔑 **API Key 输入框**（`type="password"` 脱敏显示）
  - 优先级：侧栏输入框 > 环境变量 `GONKA_API_KEY`，模糊记忆，方便长期使用
- 🌐 **Base URL 可配置**：默认 `https://api.gonkarouter.io/v1`，可在不重启的情况下切换网关
- ✅ **一键连通性测试**：`Test connection (/v1/models)` 按钮，直接拉取可用模型列表并展示前 15 个，几秒内确认连接有效
- 🧩 **当前模型阵容展示**：列出 DeepSeek V4 Flash（逻辑）、MiniMax M2.7（事实）、Arbiter 引擎，让用户时刻知道"谁在干活"

> **设计意图**：Hackathon 现场演示最怕"登录态丢失 / Key 配错"，侧边栏自治让任何评委可现场自备 Key 一键连接。

### 3.2 模型分工状态展示（Division-of-Labour Visibility）

- 通过 `MODELS` 配置驱动 UI，模型名、职责、引擎 ID **单点定义、全局复用**，杜绝硬编码漂移。
- 核查时以 `st.spinner` 提供"并发运行中"的实时反馈；结论打标 `Label / Confidence`，一眼看懂每个模型的态度（支持 ✅ / 存疑 ⚠️ / 证伪 ❌ / 混合）。
- 子主张级拆解（`claims[]`）逐条列出，每条附 evaluation 与 stance，把"模型思路"摊开给用户看。

### 3.3 多 Tab 证据对照（Evidence Comparison Tabs）

核查结果用 **3 个 Tab** 承载，证据互不干扰、对照清晰：

| Tab | 内容 | 价值 |
|-----|------|------|
| **DeepSeek analysis (logic)** | 逻辑漏洞盘点 + 子主张拆解 + **Gonka Request ID** | 看到"逻辑维度"的全部分析 |
| **MiniMax analysis (facts)** | 事实比对 + 支持/反驳证据 + 该模型 **Request ID** | 看到"事实维度"的全部分析 |
| **Reasoning trace** | 仲裁理由 + 模型一致/分歧点 + 仲裁 **Request ID** | 理解"最终结论怎么来的" |

### 3.4 铁证透明看板（Evidence Transparency Dashboard）

- **顶部聚合卡**：用 `st.metric` 展示 Truth Score（%）、Verdict 彩色徽章、Arbiter Request ID、模型数 —— 结论 3 秒可读。
- **视觉编码**：Truth Score 用进度条 + 语义色（绿=可信 / 黄=存疑 / 红=虚假）双通道编码，色盲友好（不只靠颜色）。
- **金色证据**：每个模型、仲裁器的 **Gonka Request ID（即 `response.id`）** 都以等宽代码样式展示——这是面向评委的"黄金证据"：每一步调用都可在 Gonka 网关侧追溯对账。
- **原始证据 JSON**：底部 `📋 原始证据日志` 折叠面板，导出完整 JSON（含三个 request_id 与全部 parsed 结果），便于演示取证与审计。

### 3.5 状态与反馈设计

```
[输入空]        → 校验提示 "请先输入一段声明"
[Key 缺失]      → 错误卡片提示配置侧栏
[单模型失败]    → 该 Tab 内联错误卡，另一侧照常展示，整体不崩溃
[抓取 URL 失败] → 错误占位文本进入流程，不静默吞错
[核查完成]      → 成功横幅 + 透明看板
```

---
## 4. Technical Stack 技术栈

### 4.1 技术选型总览

| 层次 | 技术 | 选型理由 |
|------|------|----------|
| **前端 / UI 框架** | **Streamlit** | Python 原生、组件丰富（tabs/metric/spinner）、秒级原型迭代，最适合 Hackathon 演示 |
| **API 客户端** | **OpenAI SDK** (`openai`) | 官方成熟 SDK，天然支持 `base_url` 覆盖 + `response.id` 直读，零成本对接 Gonka |
| **模型路由网关** | **Gonka Router Gateway** | OpenAI 兼容端点，一张 API Key 调用多模型，支持并发、计费与 Request ID 追踪 |
| **网页正文抓取** | **Requests + BeautifulSoup** | 轻量、无重依赖，数十行实现"URL → 正文"清洗管线 |
| **并发执行** | `concurrent.futures.ThreadPoolExecutor` | 标准库即可实现双模型**真并发**，无需引入 asyncio 重写 |

### 4.2 工程亮点 Implemented Engineering

- **并发不阻塞**：`ThreadPoolExecutor(max_workers=2) + as_completed`，两模型并行、各自独立失败隔离。
- **结构化输出**：双模型按统一 JSON Schema 输出（label/confidence/claims/evidence），经 `extract_json()` 容忍文本围栏（```json）稳健解析。
- **缓存加速**：`@st.cache_data` 对相同输入的核查结果做缓存，重复演示零成本。
- **超时保护**：模型调用 30s、网页抓取 10s 超时，防止外部服务卡死前端。
- **单一事实来源**：模型名/职责集中在开头的 `MODELS` 配置常量，全局引用。

### 4.3 关键依赖（requirements.txt）

```
streamlit>=1.30        # 前端框架
openai>=1.30           # Gonka 兼容 OpenAI SDK
requests               # URL 抓取
beautifulsoup4         # HTML 解析
```

---
## 5. 数据流与错误处理 Data Flow & Error Handling

### 5.1 数据流（数据级视角）

```
声明文本 / URL正文
   │  fetch_article_text()  [url 模式]
   ▼
USER_PROMPT_TMPL.format(claim)   ← 统一的 user prompt 模板
   │
   ▼  ┌───────────────┐  ┌───────────────┐
   ├─→ │ DeepSeek call │  │ MiniMax call  │    (并发)
   │   └──────┬────────┘  └──────┬────────┘
   │          ▼ JSON             ▼ JSON
   │   {label, confidence,   {label, confidence,
   │    claims[], evidence}   claims[], evidence}
   └──────────────────┬──────────────────┘
                      ▼
   call_arbiter() → {veracity, verdict, reasoning, agreement}
                      ▼
   render_transparency() → Streamlit 前端看板
```

### 5.2 错误处理矩阵 Error Handling Matrix

| 场景 | 处理策略 | 用户可见反馈 |
|------|----------|--------------|
| API Key 缺失 | 前端校验拦截 | 错误卡片提示配置 |
| 模型调用超时/失败 | 该模型独立 try/except，标记 `ERROR`，仲裁跳过 | 该 Tab 内联错误 + 错误详情 |
| 两个模型均失败 | 仲裁置 `ARBITER_SKIPPED` 占位 | 前端提示 unable to arbitrate |
| URL 抓取失败（超时/404/反爬） | 返回错误占位文本继续流程 | 声明文本中可见错误信息 |
| 模型返回非法 JSON | `extract_json()` 分级回退（围栏→平衡括号→原文） | 展示原文 / "无法解析" |

---
## 6. 扩展路线图 Roadmap

- **V2 — 来源搜索增强**：接入搜索引擎/新闻 API，让 MiniMax 不再只靠内部知识，而是检索实时来源后比对。
- **V3 — 证据引用落地**：把证据映射到具体链接 + 高亮标注，`Reasoning Trace` 可点击跳转原文。
- **V4 — 多轮追问**：用户可对存疑子主张继续追问，进入深挖会话。
- **V5 — 批量核查 / 事实档案**：历史核查形成可检索档案，支持 CSV 导出，面向审核团队。
- **V6 — 流式输出**：`stream=True` 流式渲染推理过程，提升现场演示的"过程感"。

---
## 附录 Appendix

**文件结构**

```
app.py                        # 单文件应用（~470 行）
requirements.txt              # 依赖清单
App_Design_Document.md        # 本文档
```

**环境变量**

| 变量 | 说明 |
|------|------|
| `GONKA_API_KEY` | Gonka 网关 API Key（也可在侧栏输入） |

**本地运行**

```bash
pip install -r requirements.txt
export GONKA_API_KEY="jg-xxxx"      # Windows: set GONKA_API_KEY=jg-xxxx
streamlit run app.py
```

---

> 📌 **一句话总结：** 用 Gonka 一张 Key 挂载两个各司其职的模型，并发核查、独立仲裁、全程透明 —— 让 AI 事实核查从"黑盒结论"升级为"证据可溯的裁决"。
