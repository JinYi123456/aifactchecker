"""
Gonka AI Fact Checker  -  Hackathon single-file app
====================================================
Multi-model cross-verification fact checker built on Gonka Router
(OpenAI-compatible API).

Pipeline:
  1. User submits a news claim / statement in the input box.
  2. Backend CONCURRENTLY calls two models:
       - deepseek-ai/DeepSeek-V4-Flash-0731  -> fast logic-hole extraction
       - MiniMaxAI/MiniMax-M2.7              -> long-text & fact comparison
  3. Both outputs flow into an "arbiter" that fuses them into:
       - Truth Score (0-100%)
       - verdict (real / mostly real / partly dubious / highly dubious / fake / undecidable)
       - detailed Reasoning Trace
  4. Frontend "Evidence Transparency Dashboard" shows, clearly:
       DeepSeek analysis, MiniMax analysis, final score,
       and each model's Gonka Request ID (response.id).

How to run:
  pip install streamlit openai
  export GONKA_API_KEY="jg-xxxx"          # Windows CMD: set GONKA_API_KEY=jg-xxxx
  streamlit run app.py
"""

import os
import re
import json
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

try:
    import streamlit as st
    from openai import OpenAI
except ImportError as e:
    print(
        "Missing dependencies. Please run:\n"
        "  pip install streamlit openai\n"
        f"Original error: {e}",
        file=sys.stderr,
    )
    raise

# ============================================================================
# Configuration
# ============================================================================
DEFAULT_BASE_URL = "https://api.gonkarouter.io/v1"   # Gonka Router OpenAI-compatible endpoint
DEFAULT_KEY_ENV   = "GONKA_API_KEY"

# Two fact-checker models and their division of labour
# 两个事实核查模型的系统提示词（已加固：严禁任何多余废话）
MODELS = {
    "deepseek": {
        "display": "DeepSeek V4 Flash (fast logic-hole extraction)",
        "model":   "deepseek-ai/DeepSeek-V4-Flash-0731",
        "system": (
            "You are a rigorous fact-checker responsible for FAST LOGIC-HOLE EXTRACTION. "
            "CRITICAL INSTRUCTION: You must output ONLY a valid JSON object. "
            "DO NOT write any introductory text, markdown explanations, conversational filler, or notes outside the JSON block. "
            "The response MUST start with '{' and end with '}'. "
            "Task:\n"
            "1. Break the claim into checkable sub-claims.\n"
            "2. Assess each for internal consistency, logical fallacies, or missing sources.\n"
            "3. Label each as SUPPORTED / DOUBTFUL / REFUTED.\n"
            "JSON structure required: "
            "{\"label\": \"SUPPORTED|DOUBTFUL|REFUTED|MIXED\", \"confidence\": 0-100, "
            "\"claims\": [{\"text\": \"...\", \"evaluation\": \"...\", \"stance\": \"...\"}], "
            "\"evidence\": \"...\"}"
        ),
    },
    "minimax": {
        "display": "MiniMax M2.7 (long-text & fact comparison)",
        "model":   "MiniMaxAI/MiniMax-M2.7",
        "system": (
            "You are a professional fact-checking expert. "
            "CRITICAL INSTRUCTION: Output ONLY a valid JSON object. "
            "No conversational text before or after the JSON. "
            "Required JSON format: "
            "{\"label\": \"SUPPORTED|DOUBTFUL|REFUTED\", \"confidence\": 0-100, \"evidence\": \"...\"}"
        ),
    },
}

# 仲裁器系统提示词（同样加固）
ARBITER_SYSTEM = (
    "You are an impartial arbiter judge. "
    "CRITICAL INSTRUCTION: Output ONLY a valid JSON object. No conversational text or Markdown outside the JSON. "
    "Required JSON structure:\n"
    "{\n"
    '  "veracity": 0 to 100 (integer),\n'
    '  "verdict": "REAL" | "MOSTLY_REAL" | "PARTLY_DUBIOUS" | "HIGHLY_DUBIOUS" | "FAKE" | "UNKNOWN",\n'
    '  "reasoning": "detailed reasoning trace in English",\n'
    '  "agreement": "where models agree and disagree"\n'
    "}"
)

USER_PROMPT_TMPL = (
    "Fact-check the following news/claim:\n\n"
    "---\n{claim}\n---\n\n"
    "Follow your fact-checking role and output JSON with these suggested keys:\n"
    "{{\"label\": \"SUPPORTED|DOUBTFUL|REFUTED|MIXED\", "
    "\"confidence\": 0~100, "
    "\"claims\": [{{\"text\": \"...\", \"evaluation\": \"...\", "
    "\"stance\": \"SUPPORTED|DOUBTFUL|REFUTED\"}}], "
    "\"evidence\": \"key evidence/reasoning\", "
    "\"notes\": \"extra notes or points to verify\"}}"
)

# Arbiter reuses the fast deepseek model as the judge
ARBITER_MODEL = "deepseek-ai/DeepSeek-V4-Flash-0731"


# ============================================================================
# Helpers
# ============================================================================
def fetch_article_text(url: str) -> str:
    """给定一个 URL，自动抓取其网页正文并进行清洗，过滤掉侧边栏和无关噪音"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        }
        # 10秒超时，防止目标网站卡死
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # 使用 BeautifulSoup 解析 HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 1. 深度清理：移除所有干扰标签（不仅是脚本，还包括广告、侧边栏、推荐阅读等常见类名）
        for element in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]):
            element.decompose()
            
        # 尝试通过常见的正文 Class 或 Tag 提取核心区域
        # 很多新闻网站正文在 article 标签或 specific class 里
        article_tag = soup.find('article') or soup.find('div', class_=re.compile('content|article|post|story', re.I))
        
        if article_tag:
            text = article_tag.get_text(separator='\n', strip=True)
        else:
            text = soup.get_text(separator='\n', strip=True)

        # 2. 文本清洗：过滤掉太短的行、导航栏碎片、以及像 "Most Read" 这种常见的垃圾短句
        lines = []
        noise_keywords = ["most read", "related articles", "subscribe", "just in", "you may also like"]
        for line in text.splitlines():
            line_clean = line.strip()
            # 忽略空行、过短的行（如单个字或日期碎片）、以及包含垃圾关键词的行
            if len(line_clean) > 15 and not any(kw in line_clean.lower() for kw in noise_keywords):
                lines.append(line_clean)
                
        # 3. 限制总长度：只取前 60 段核心正文，防止 Token 过载导致模型崩溃
        return "\n".join(lines[:60])
        
    except Exception as e:
        return f"[Error fetching URL: {str(e)}]"


def extract_json(text):
    """Robustly pull a JSON object out of model text (handles markdown fences)."""
    if not text:
        return {}
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    if start == -1:
        return {"raw": text.strip()}
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start: i + 1])
                except Exception:
                    break
    return {"raw": text.strip(), "label": "UNPARSEABLE", "confidence": 0}


def safe_float(value, default=0.0):
    try:
        f = float(value)
        return f if 0 <= f <= 100 else default
    except (TypeError, ValueError):
        return default


def call_model(client, model_key, user_content, temperature=0.1, max_tokens=1400):
    """Single Gonka call. Returns dict with id/model/content/parsed/usage."""
    cfg = MODELS[model_key]
    try:
        resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": cfg["system"]},
                {"role": "user", "content": user_content},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=60.0,
        )
        content = (resp.choices[0].message.content or "")
        return {
            "id": getattr(resp, "id", "N/A"),          # Gonka Request ID
            "model": getattr(resp, "model", cfg["model"]),
            "display": cfg["display"],
            "content": content,
            "parsed": extract_json(content),
            "usage": resp.usage,
        }
    except Exception as e:
        return {
            "id": "ERROR",
            "model": cfg["model"],
            "display": cfg["display"],
            "content": "",
            "parsed": {"label": "CALL_FAILED", "confidence": 0, "error": str(e)},
            "usage": None,
            "error": str(e),
        }


def call_arbiter(client, deep, minimax, claim):
    """Arbiter logic: fuse both analyses into Truth Score + Reasoning Trace."""
    deep_txt = json.dumps(deep.get("parsed", {}), ensure_ascii=False)
    minimax_txt = json.dumps(minimax.get("parsed", {}), ensure_ascii=False)
    user = (
        f"Claim to rule on:\n---\n{claim}\n---\n\n"
        f"[DeepSeek analysis JSON]\n{deep_txt}\n\n"
        f"[minimax analysis JSON]\n{minimax_txt}\n\n"
        'Output JSON: {"veracity": 0~100, "verdict": "...", '
        '"reasoning": "...", "agreement": "..."}'
    )
    try:
        resp = client.chat.completions.create(
            model=ARBITER_MODEL,
            messages=[
                {"role": "system", "content": ARBITER_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            max_tokens=1400,
            timeout=90.0,
        )
        content = (resp.choices[0].message.content or "")
        return {
            "id": getattr(resp, "id", "N/A"),
            "model": getattr(resp, "model", ARBITER_MODEL),
            "content": content,
            "parsed": extract_json(content),
            "usage": resp.usage,
        }
    except Exception as e:
        return {
            "id": "ERROR",
            "model": ARBITER_MODEL,
            "content": "",
            "parsed": {"veracity": 0, "verdict": "UNKNOWN", "error": str(e)},
            "usage": None,
            "error": str(e),
        }


@st.cache_data(show_spinner=False)
def run_verification(claim, api_key, base_url):
    """Concurrently call the two checkers, then run the arbiter."""
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=30.0) # 设定 30 秒超时，防止瞬时卡死报错

    results = {}
    def _worker(key):
        try:
            return key, call_model(client, key, USER_PROMPT_TMPL.format(claim=claim))
        except Exception as e:
            return key, {
                "id": "ERROR", "model": MODELS[key]["model"],
                "display": MODELS[key]["display"],
                "content": "", "parsed": {"label": "CALL_FAILED",
                                          "confidence": 0,
                                          "error": str(e)},
                "usage": None, "error": str(e),
            }

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_worker, key): key for key in MODELS}
        for fut in as_completed(futures):
            key, payload = fut.result()
            results[key] = payload

    arbiter = None
    if "ERROR" not in (results.get("deepseek", {}).get("id", ""),
                       results.get("minimax", {}).get("id", "")):
        arbiter = call_arbiter(client, results["deepseek"], results["minimax"], claim)
    else:
        arbiter = {
            "id": "ARBITER_SKIPPED",
            "model": ARBITER_MODEL,
            "content": "",
            "parsed": {"veracity": 50, "verdict": "UNKNOWN", 
                       "reasoning": "One or more checker models failed, cannot perform arbitration."},
            "usage": None,
            "error": "Checker model(s) failed"
        }

    return {
        "claim": claim,
        "deepseek": results.get("deepseek"),
        "minimax": results.get("minimax"),
        "arbiter": arbiter,
        "ts": time.time(),
    }


# ============================================================================
# Streamlit UI
# ============================================================================
st.set_page_config(page_title="Gonka AI Fact Checker", page_icon="🔍", layout="wide")

# ============================================================================
# Custom CSS for Hackathon Grade UI
# ============================================================================
st.markdown("""
<style>
    /* 采用 Streamlit 官方 CSS 变量，完美自动适配 Light / Dark Mode，绝不出现文字隐形 */
    .stApp {
        background-color: var(--background-color);
        color: var(--text-color);
    }
    
    /* 现代化输入框圆角与高亮边框 */
    textarea, input {
        border-radius: 10px !important;
        border: 1px solid rgba(128, 128, 128, 0.3) !important;
    }
    
    /* 渐变主按钮 */
    .stButton>button {
        background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
        color: white;
        border-radius: 10px;
        font-weight: 600;
        border: none;
        padding: 0.5rem 1rem;
        box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3);
    }
    .stButton>button:hover {
        opacity: 0.9;
    }
</style>
""", unsafe_allow_html=True)


VERDICT_LABEL = {
    "REAL": "Real", "MOSTLY_REAL": "Mostly Real", "PARTLY_DUBIOUS": "Partly Dubious",
    "HIGHLY_DUBIOUS": "Highly Dubious", "FAKE": "Fake", "UNKNOWN": "Unknown",
}
VERDICT_COLOR = {
    "REAL": "#1a7f37", "MOSTLY_REAL": "#2e8b57", "PARTLY_DUBIOUS": "#c9a400",
    "HIGHLY_DUBIOUS": "#e07b00", "FAKE": "#c0392b", "UNKNOWN": "#6c757d",
}


def verdict_chip(verdict):
    label = VERDICT_LABEL.get(str(verdict).upper(), str(verdict))
    color = VERDICT_COLOR.get(str(verdict).upper(), "#6c757d")
    return f'<span style="background:{color};color:#fff;padding:2px 12px;' \
           f'border-radius:12px;font-weight:600">{label}</span>'


def display_model_analysis(model_key, data):
    """Display individual model analysis using modern, color-coded grid cards."""
    if not data:
        st.warning(f"{model_key} analysis not available")
        return

    parsed = data.get("parsed", {})
    req_id = data.get("id", "N/A")          # <-- Gonka Request ID (response.id)

    st.markdown(f"**Request ID**: `{req_id}`")

    # 状态标签颜色映射
    label_colors = {
        "SUPPORTED": "#1a7f37", "DOUBTFUL": "#c9a400", 
        "REFUTED": "#c0392b", "MIXED": "#e07b00"
    }
    lbl = str(parsed.get('label', 'UNKNOWN')).upper()
    lbl_color = label_colors.get(lbl, "#6c757d")
    
    st.markdown(
        f"Status: <span style='background:{lbl_color};color:#fff;padding:2px 10px;"
        f"border-radius:6px;font-weight:600'>{lbl}</span> · "
        f"Confidence: **{parsed.get('confidence', '?')}%**",
        unsafe_allow_html=True
    )
    st.markdown("<br>", unsafe_allow_html=True)

    claims = parsed.get("claims")
    if isinstance(claims, list) and claims:
        st.markdown("**🔍 Sub-claims & Breakdown Cards**")
        for c in claims:
            stance = str(c.get("stance", "DOUBTFUL")).upper()
            stance_color = label_colors.get(stance, "#6c757d")
            
            # 用优雅的 Markdown 块与自定义 HTML 渲染卡片
            st.markdown(
                f"""
                <div style="border: 1px solid rgba(128,128,128,0.2); border-radius: 10px; padding: 12px; margin-bottom: 10px; background-color: rgba(128,128,128,0.03);">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                        <span style="font-weight: 600; font-size: 0.95rem;">📌 Claim Element</span>
                        <span style="background:{stance_color}; color:#fff; padding:1px 8px; border-radius:4px; font-size:0.75rem; font-weight:600;">{stance}</span>
                    </div>
                    <p style="margin: 4px 0; font-weight: 500;">{c.get('text', '')}</p>
                    <p style="margin: 4px 0; color: #64748b; font-size: 0.9rem;">💡 <em>Evaluation:</em> {c.get('evaluation', '')}</p>
                </div>
                """,
                unsafe_allow_html=True
            )
    else:
        st.markdown("**Analysis Details**:")
    st.write(parsed.get("evidence") or parsed.get("raw") or data.get("content"))

    if parsed.get("notes"):
        st.markdown("**Notes / To Verify**:")
        st.write(parsed["notes"])


def render_transparency(result):
    """Evidence Transparency Dashboard.

    Shows, side by side:
      - DeepSeek V4 Flash analysis + its Gonka Request ID
      - MiniMax M2.7 analysis + its Gonka Request ID
      - The final arbiter Truth Score + verdict + reasoning trace
    """
    deep = result.get("deepseek") or {}
    minimax = result.get("minimax") or {}
    arb = result.get("arbiter") or {}
    av = arb.get("parsed", {})
    score = safe_float(av.get("veracity"), None)
    verdict = str(av.get("verdict", "UNKNOWN")).upper()

    st.subheader("🔍 Final Ruling")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Truth Score", f"{score:.0f}%" if score is not None else "N/A")
    c2.markdown("**Verdict**  \n" + verdict_chip(verdict), unsafe_allow_html=True)
    c3.metric("Arbiter Request ID", arb.get("id"),
              help="Gonka Request ID of the arbiter call")
    c4.metric("Models", "2 concurrent")

    if score is not None:
        color = VERDICT_COLOR.get(verdict, "#6c757d")
        st.markdown(
            f'<div style="background:#eceff1;border-radius:8px;height:20px;'
            f'width:100%"><div style="background:{color};width:{score}%;'
            f'height:20px;border-radius:8px"></div></div>',
            unsafe_allow_html=True,
        )
    st.divider()

    # --- Two checker panels ---
    tab_logic, tab_facts, tab_trace = st.tabs(
        ["DeepSeek analysis (logic)", "MiniMax analysis (facts)", "Reasoning trace"]
    )
    with tab_logic:
        if deep.get("id") == "ERROR":
            st.error(f"DeepSeek call failed: {deep.get('error', '?')}")
        else:
            display_model_analysis("DeepSeek V4 Flash", deep)
    with tab_facts:
        if minimax.get("id") == "ERROR":
            st.error(f"MiniMax call failed: {minimax.get('error', '?')}")
        else:
            display_model_analysis("MiniMax M2.7", minimax)
    with tab_trace:
        st.markdown("**Reasoning Trace**")
        st.write(av.get("reasoning", arb.get("content", "(no reasoning)")))
        st.divider()
        st.markdown("**Model agreement / disagreement**")
        st.write(av.get("agreement", "(none)"))
        st.caption(f"Arbiter Request ID: {arb.get('id')}")


# ============================================================================
# Main app
# ============================================================================
def main():
    # 初始化历史记录 Session State
    if "history" not in st.session_state:
        st.session_state.history = []

    st.title("🕵️ Gonka AI Fact Checker")
    st.caption(
        "Multi-model Cross-Validation & Evidence Transparency Dashboard | "
        "Running DeepSeek V4 Flash (Logic-hole extraction) and "
        "MiniMax M2.7 (Long-text fact comparison) **concurrently**, "
        "fused by an Arbiter for Truth Score and Reasoning Trace."
    )

    # --- Sidebar: Gonka connection ---
    key_env = os.getenv(DEFAULT_KEY_ENV, "")
    with st.sidebar:
        st.header("Gonka connection")
        api_key = st.text_input(
            "API Key",
            value="",      # 默认空白，不显示你的 Key
            type="password",
            placeholder="Enter your API Key",
            #help=f"Falls back to env var {DEFAULT_KEY_ENV}.",
        )
        # 把这几行删掉，不要让它自动 fallback 到你的 key
        #if not api_key:
            #api_key = key_env
        base_url = st.text_input("Base URL", value=DEFAULT_BASE_URL)
        st.caption("OpenAI SDK · `response.id` 即 Gonka Request ID。")
        st.markdown("---")
        st.caption("Models")
        st.write(f"- {MODELS['deepseek']['display']}")
        st.write(f"- {MODELS['minimax']['display']}")
        st.write(f"- Arbiter: {ARBITER_MODEL}")
        st.markdown("---")
        health = st.button("Test connection (/v1/models)")
        if health and api_key:
            try:
                client = OpenAI(base_url=base_url, api_key=api_key)
                models_response = client.models.list()
                models_list = models_response.data
                names = [m.id for m in models_list][:15]
                st.success(f"OK — {len(models_list)} models available")
                st.write(", ".join(names))
            except Exception as e:
                st.error(f"Connection failed: {e}")

        st.markdown("---")
        st.subheader("📜 Verification History")
        if not st.session_state.history:
            st.caption("No history yet.")
        else:
            for idx, item in enumerate(st.session_state.history[:5]): # 仅展示最近5条
                preview_text = item["claim"][:35] + "..."
                if st.button(f"[{item['verdict']}] {preview_text}", key=f"hist_btn_{idx}"):
                    # 点击历史记录时，直接重新渲染该条结果
                    st.session_state.selected_history = item["result"]

    # 如果用户点击了历史记录，优先载入历史结果
    if "selected_history" in st.session_state and st.session_state.selected_history:
        st.info("📂 Viewing a previous fact-check record from history.")
        if st.button("🔄 Clear & Start New Check"):
            st.session_state.selected_history = None
            st.rerun()
        render_transparency(st.session_state.selected_history)
        st.divider()
        st.caption("⭐ Hackathon Demo — Model outputs are generated by LLMs for demonstration purposes only "
                       "and do not constitute professional fact-checking conclusions.")
        return # 提前结束，直接看历史

    # --- Claim input ---
    claim_default = "Official figures claim a 12% regional GDP growth this year, far exceeding the national average."
    user_input = st.text_area(
        "Enter statement or drop a news link to fact-check",
        claim_default, 
        height=120,
        placeholder="Drop a news URL or paste text to verify..."
    )

    # 智能识别输入内容：如果是 URL，则自动抓取正文
    claim = user_input.strip()
    if claim.startswith("http://") or claim.startswith("https://"):
        with st.spinner("🌐 Fetching article content from URL..."):
            scraped_content = fetch_article_text(claim)
            if scraped_content.startswith("[Error"):
                st.error(scraped_content)
                claim = ""
            else:
                st.info(f"Successfully extracted content (Preview): {scraped_content[:250]}...")
                claim = scraped_content  # 将抓取到的正文作为最终核查对象

    col_btn, col_note = st.columns([1, 3])
    with col_btn:
        go = st.button("🚀 Verify (Dual-Model Concurrent)", type="primary",
                       use_container_width=True)
    with col_note:
        pass

    if go:
        if not api_key:
            st.error(f"Please provide your Gonka API Key (via sidebar or environment variable "
                     f"{DEFAULT_KEY_ENV}）。")
        elif not claim.strip():
            st.error("Please enter a statement or a valid web link first.")
        else:
            with st.spinner("Running DeepSeek + MiniMax concurrently, followed by arbitration…"):
                result = run_verification(claim.strip(), api_key, base_url)
            st.success("✅ Verification Complete")
            render_transparency(result)

            # 验证完成后自动存入历史
            score = safe_float((result.get("arbiter") or {}).get("parsed", {}).get("veracity"), None)
            verdict = str((result.get("arbiter") or {}).get("parsed", {}).get("verdict", "UNKNOWN")).upper()
            # 保存到历史记录中
            history_item = {
                "claim": result["claim"],
                "verdict": verdict,
                "result": result
            }
            # 避免重复连续添加同一条
            if not st.session_state.history or st.session_state.history[0]["claim"] != result["claim"]:
                st.session_state.history.insert(0, history_item)

            # --- Evidence log (JSON-exportable) with both Request IDs ---
            with st.expander("📋 Raw Evidence Log (JSON)"):
                st.json({
                    "claim": result["claim"],
                    "deepseek_request_id": (result.get("deepseek") or {}).get("id"),
                    "minimax_request_id": (result.get("minimax") or {}).get("id"),
                    "arbiter_request_id": (result.get("arbiter") or {}).get("id"),
                    "deepseek_parsed": (result.get("deepseek") or {}).get("parsed"),
                    "minimax_parsed": (result.get("minimax") or {}).get("parsed"),
                    "arbiter_parsed": (result.get("arbiter") or {}).get("parsed"),
                })

    st.divider()
    st.caption("⭐ Hackathon Demo — Model outputs are generated by LLMs for demonstration purposes only "
               "and do not constitute professional fact-checking conclusions.")


if __name__ == "__main__":
    main()
