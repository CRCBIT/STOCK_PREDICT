"""ChipForecast AI chat tab for streamlit_app.py.

Secrets (Streamlit Cloud > Settings > Secrets):
    chat_url = "https://chat.chipforecast.io"
    chat_key = "<contents of ~/.chipforecast_chat_key on the Mac>"

The key stays on the Streamlit server; visitors' browsers never see it.
Status comes from <chat_url>/health, which is green only when the Mac chat server
AND the oMLX model server both answer.
"""
from __future__ import annotations

import time
import uuid

import requests
import streamlit as st

CF_CHAT_UI_VERSION = "v6-cf-intelligence-20261018"

SUGGESTIONS = ["{name} 10일 전망은?", "삼성전자랑 하이닉스 비교해줘", "메모리 종목 중 20일 기준 제일 좋은 건?", "HBM이 뭐야?"]

# 입력창 예시: 사이트의 여러 화면(전망·참고선·적중률·과거 기록·모델·메모리 업황·업데이트)을 골고루 보여준다.
PLACEHOLDERS = [
    ("하이닉스 10일 전망은?", "삼성전자 손절 참고선 얼마야?"),
    ("마이크론 실제 적중률 어때?", "HBM 수출단가 최근 추이 알려줘"),
    ("하이닉스 10월 10일엔 어떻게 예측했어?", "어떤 모델 섞어서 예측해?"),
    ("엔비디아 1년 최고가는?", "오늘 전망이 가장 크게 바뀐 종목은?"),
    ("삼전 신뢰도는 왜 낮아?", "메모리 종목 20일 순위"),
    ("삼성전자랑 하이닉스 비교해줘", "최근 업데이트 뭐 바뀌었어?"),
    ("하이닉스 AI 리서치 요약해줘", "P50이 뭐야?"),
    ("샌디스크 80% 예상 범위는?", "디램 가격 작년이랑 비교하면?"),
    ("하이닉스 10일 예측 어떤 모델로 했어?", "전체 종목 적중률은?"),
    ("마이크론 백테스트 결과는?", "몇 종목이나 분석해?"),
]



# Home-screen chat only. All selectors are scoped to the keyed Streamlit container;
# they do not change the rest of the dashboard (selectbox, metrics, charts or radios).
_CHAT_POLISH_CSS = r"""
<style>
  @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/variable/pretendardvariable-dynamic-subset.css');

  /* ChipForecast chat v6: style only app-owned containers and text.
     Streamlit owns input layout, buttons, message sizing and normal page scrolling. */
  .st-key-cf_ai_chat_home {
    --cf-font: "Pretendard Variable", Pretendard, -apple-system, BlinkMacSystemFont,
               "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
    --cf-line: rgba(149,166,190,.20);
    font-family: var(--cf-font);
    min-width: 0;
    max-width: 100%;
    font-variant-numeric: tabular-nums;
  }
  .st-key-cf_ai_chat_home [data-testid="stMarkdownContainer"] {
    font-family: var(--cf-font);
  }
  .st-key-cf_ai_chat_home .st-key-cf_chat_composer {
    box-sizing: border-box;
    width: 100%;
    max-width: 100%;
    min-width: 0;
    padding: 12px 13px;
    border: 1px solid rgba(145,164,188,.17);
    border-radius: 15px;
    background: rgba(17,24,34,.97);
    margin: 0 0 16px;
  }
  .st-key-cf_ai_chat_home .cf-chat-status {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 6px 11px;
    border-radius: 999px;
    background: #101720;
    border: 1px solid var(--cf-line);
    color: #c6d2df;
    font: 550 12px/1.3 var(--cf-font);
    white-space: nowrap;
  }
  .st-key-cf_ai_chat_home .cf-chat-status-dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #40c985;
    box-shadow: 0 0 0 3px rgba(64,201,133,.10);
  }
  .st-key-cf_ai_chat_home .cf-chat-status.busy .cf-chat-status-dot {background:#e5b65a;box-shadow:none;}
  .st-key-cf_ai_chat_home .cf-chat-status.offline .cf-chat-status-dot {background:#8593a5;box-shadow:none;}

  /* Native st.chat_input provides its own aligned send icon and responsive sizing. */
  .st-key-cf_ai_chat_home [data-testid="stChatInput"] {
    max-width: 100%;
    min-width: 0;
    font-family: var(--cf-font);
  }
  .st-key-cf_ai_chat_home [data-testid="stChatInput"] textarea {
    font-family: var(--cf-font) !important;
    font-size: 16px !important; /* no iOS Safari focus zoom */
    line-height: 1.48 !important;
    color: #ebf1fa !important;
  }

  /* Each message is a normal auto-sized Streamlit container, not st.chat_message.
     No height, overflow, fixed positioning or forced flex geometry on Streamlit internals. */
  .st-key-cf_ai_chat_home [class*="st-key-cf_chat_user_"] {
    padding: 11px 14px;
    margin: 5px 0 11px;
    background: #1a2533;
    border: 1px solid rgba(149,166,190,.17);
    border-radius: 13px;
    box-sizing: border-box;
    width: 100%;
    min-width: 0;
    max-width: 100%;
  }
  .st-key-cf_ai_chat_home [class*="st-key-cf_chat_assistant_"] {
    padding: 13px 15px 15px;
    margin: 5px 0 14px;
    background: #131c27;
    border: 1px solid var(--cf-line);
    border-radius: 14px;
    box-sizing: border-box;
    width: 100%;
    min-width: 0;
    max-width: 100%;
  }
  .st-key-cf_ai_chat_home [class*="st-key-cf_chat_user_"] [data-testid="stMarkdownContainer"] p,
  .st-key-cf_ai_chat_home [class*="st-key-cf_chat_assistant_"] [data-testid="stMarkdownContainer"] p,
  .st-key-cf_ai_chat_home [class*="st-key-cf_chat_assistant_"] [data-testid="stMarkdownContainer"] li {
    font-size: 15px !important;
    line-height: 1.65 !important;
    letter-spacing: -.012em;
    color: #dce5ef;
    overflow-wrap: anywhere;
    word-break: normal;
    margin-bottom: .45em;
  }
  .st-key-cf_ai_chat_home [class*="st-key-cf_chat_assistant_"] [data-testid="stMarkdownContainer"] strong {
    font-weight: 700;
    color: #f3f6fd;
  }
  .st-key-cf_ai_chat_home .cf-chat-role {
    font: 700 12px/1.35 var(--cf-font);
    letter-spacing: .01em;
    color: #b1c7e4;
  }
  .st-key-cf_ai_chat_home .cf-chat-role.user {color:#bfcada;}
  .st-key-cf_ai_chat_home [class*="st-key-cf_chat_assistant_"] [data-testid="stMarkdownContainer"] table {
    display: block;
    max-width: 100%;
    overflow-x: auto;
    white-space: nowrap;
    font-size: 13px;
    -webkit-overflow-scrolling: touch;
  }
  .st-key-cf_ai_chat_home [class*="st-key-cf_chat_assistant_"] [data-testid="stMarkdownContainer"] pre {
    max-width: 100%;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
  .st-key-cf_ai_chat_home [class*="st-key-cf_chat_assistant_"] [data-testid="stMarkdownContainer"] :not(pre) > code {
    overflow-wrap: anywhere;
    white-space: normal;
    word-break: break-word;
  }
  .st-key-cf_ai_chat_home .cf-chat-disclaimer {
    font: 450 12px/1.55 var(--cf-font);
    color: #8a9caf;
    overflow-wrap: anywhere;
  }
  .st-key-cf_ai_chat_home .st-key-cf_chat_footer [data-testid="stButton"] button {
    min-height: 34px;
    font-family: var(--cf-font);
    font-size: 12px;
  }
  @media (max-width: 730px) {
    .st-key-cf_ai_chat_home [class*="st-key-cf_chat_user_"] {padding: 10px 11px;margin-bottom:10px;}
    .st-key-cf_ai_chat_home [class*="st-key-cf_chat_assistant_"] {padding: 12px 12px 14px;margin-bottom:12px;}
    .st-key-cf_ai_chat_home [class*="st-key-cf_chat_assistant_"] [data-testid="stMarkdownContainer"] p,
    .st-key-cf_ai_chat_home [class*="st-key-cf_chat_assistant_"] [data-testid="stMarkdownContainer"] li {
      font-size:14px !important;
      line-height:1.65 !important;
    }
    .st-key-cf_ai_chat_home .cf-chat-disclaimer {font-size:11px;}
  }
</style>
"""


def _inject_chat_polish_styles() -> None:
    """Small, scoped typography-only stylesheet. Streamlit controls its own layout."""
    st.markdown(_CHAT_POLISH_CSS, unsafe_allow_html=True)


def _placeholder() -> str:
    """방문(세션)마다 다른 예시. 한 세션 안에서는 바꾸지 않는다 —
    placeholder가 바뀌면 Streamlit이 입력칸을 새 위젯으로 보고 입력값을 버려 '두 번 눌러야 전송'되는 문제가 생긴다."""
    seed = st.session_state.setdefault("_cf_ph_seed", uuid.uuid4().int % len(PLACEHOLDERS))
    a, b = PLACEHOLDERS[seed % len(PLACEHOLDERS)]
    return f"예: {a}"


def _cfg():
    try:
        return str(st.secrets.get("chat_url", "")).rstrip("/"), str(st.secrets.get("chat_key", ""))
    except Exception:
        return "", ""


@st.cache_data(ttl=15, show_spinner=False)
def _status(url: str) -> dict:
    if not url:
        return {"ok": False, "reason": "not_configured"}
    try:
        r = requests.get(url + "/health", timeout=3)
        d = r.json() if r.ok else {}
        return {"ok": bool(d.get("ok")), "busy": bool(d.get("busy")), "queue": d.get("queue", 0),
                "data_date": d.get("data_date"), "reason": d.get("error")}
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__}


def _stream(url, key, messages, symbol):
    body = {"messages": messages[-12:], "session": st.session_state["_cf_chat_sid"], "context_symbol": symbol or ""}
    try:
        with requests.post(url + "/chat", json=body, headers={"X-Chat-Key": key}, stream=True, timeout=(5, 300)) as r:
            if r.status_code == 429:
                yield "질문이 너무 많습니다. 잠시 후 다시 시도해 주세요."
                return
            if r.status_code != 200:
                yield "지금은 AI 챗봇을 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."
                _status.clear()
                return
            r.encoding = "utf-8"
            for chunk in r.iter_content(chunk_size=None, decode_unicode=True):
                if chunk:
                    yield chunk
    except requests.RequestException:
        _status.clear()
        yield "AI 챗봇 서버에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요."


def render_chat(symbol: str | None = None, name: str | None = None, section_head=None,
                compact: bool = False, height: int = 340) -> None:
    url, key = _cfg()
    st.session_state.setdefault("_cf_chat_sid", uuid.uuid4().hex[:16])
    hist = st.session_state.setdefault("_cf_chat", [])

    if section_head:
        section_head("AI CHAT", "AI 챗봇", "ChipForecast 예측 데이터에 대해 물어보세요.")
    elif compact:
        st.markdown("**AI 챗봇** <span style='opacity:.6;font-size:.85em'>종목 전망·판정·비교를 물어보세요</span>",
                    unsafe_allow_html=True)

    stt = _status(url)
    c1, c2 = st.columns([5, 1])
    with c1:
        if stt["ok"]:
            extra = " · 답변 대기 중인 질문이 있어요" if stt.get("busy") else ""
            st.markdown(f"🟢 **사용 가능** <span style='opacity:.6;font-size:.85em'>"
                        f"(데이터 기준일 {stt.get('data_date') or '-'}{extra})</span>", unsafe_allow_html=True)
        else:
            st.markdown("🔴 **현재 오프라인** <span style='opacity:.6;font-size:.85em'>"
                        "운영 서버가 꺼져 있어 지금은 답변할 수 없습니다.</span>", unsafe_allow_html=True)
    with c2:
        if st.button("새로고침", key="_cf_chat_refresh", use_container_width=True):
            _status.clear()
            st.rerun()

    box = st.container(height=height) if (compact and hist) else st.container()
    with box:
        for m in hist:
            with st.chat_message(m["role"]):
                st.markdown(m["content"])

    prompt = None
    if stt["ok"] and not hist:
        cols = st.columns(len(SUGGESTIONS))
        for i, (c, tpl) in enumerate(zip(cols, SUGGESTIONS)):
            label = tpl.format(name=name or "SK하이닉스")
            if c.button(label, key=f"_cf_chat_sug{i}", use_container_width=True):
                prompt = label

    typed = st.chat_input("질문을 입력하세요" if stt["ok"] else "AI 챗봇 오프라인",
                          disabled=not stt["ok"], max_chars=600, key="_cf_chat_input")
    prompt = typed or prompt
    if prompt and stt["ok"]:
        hist.append({"role": "user", "content": prompt})
        if compact and len(hist) == 1:
            box = st.container(height=height)
        with box:
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                t0 = time.time()
                answer = st.write_stream(_stream(url, key, hist, symbol))
                st.caption(f"{time.time() - t0:.1f}초")
        hist.append({"role": "assistant", "content": answer if isinstance(answer, str) else str(answer)})

    if hist and st.button("대화 지우기", key="_cf_chat_clear"):
        st.session_state["_cf_chat"] = []
        st.rerun()

    st.caption("AI 답변은 틀릴 수 있으며 투자 권유가 아닙니다. 모델 예측 수치를 설명할 뿐입니다. "
               "서비스 품질 확인을 위해 질문 내용이 운영 서버 로그에 기록될 수 있습니다.")


def _status_pill(ok: bool, busy: bool = False) -> str:
    """Small, understated status pill for the dashboard chat composer."""
    if ok and busy:
        cls, label = "busy", "AI 응답 중"
    elif ok:
        cls, label = "online", "CF Intelligence"
    else:
        cls, label = "offline", "AI 서버 오프라인"
    return (f'<span class="cf-chat-status {cls}">'
            f'<span class="cf-chat-status-dot" aria-hidden="true"></span>{label}</span>')


def _role_label(role: str) -> str:
    """A small visible label, not a pseudo-element inside a Streamlit button."""
    if role == "assistant":
        return '<span class="cf-chat-role">✦ ChipForecast AI</span>'
    return '<span class="cf-chat-role user">질문</span>'


def _display_chat_turn(index: int, role: str, content: str) -> None:
    """Use ordinary auto-sized containers to avoid iOS clipped chat-message boxes."""
    safe_role = "assistant" if role == "assistant" else "user"
    with st.container(key=f"cf_chat_{safe_role}_{index}"):
        st.markdown(_role_label(safe_role), unsafe_allow_html=True)
        st.markdown(content)


def _render_chat_bar_contents(height: int = 340) -> None:
    """Inline native composer and full-height replies; no fixed-height transcript."""
    url, key = _cfg()
    st.session_state.setdefault("_cf_chat_sid", uuid.uuid4().hex[:16])
    hist = st.session_state.setdefault("_cf_chat", [])
    stt = _status(url)
    ok = stt["ok"]

    # Native input includes its own send button. Nesting it in this container
    # places it inline above the symbol picker, not at the bottom of the page.
    with st.container(key="cf_chat_composer"):
        st.markdown(_status_pill(ok, stt.get("busy")), unsafe_allow_html=True)
        typed = st.chat_input(
            _placeholder() if ok else "AI 챗봇 오프라인",
            disabled=not ok,
            max_chars=600,
            key="_cf_chat_home_input",
        )

    prompt = typed.strip() if (ok and isinstance(typed, str)) else ""
    if not hist and not prompt:
        return

    # Use regular containers, *not* st.chat_message or st.container(height=...):
    # no nested scrollport and no CSS height overrides. Every answer expands
    # naturally with its Markdown and can be read through normal page scrolling.
    with st.container(key="cf_chat_transcript"):
        for i, m in enumerate(hist):
            _display_chat_turn(i, m["role"], m["content"])
        if prompt:
            hist.append({"role": "user", "content": prompt})
            _display_chat_turn(len(hist) - 1, "user", prompt)
            with st.container(key=f"cf_chat_assistant_{len(hist)}"):
                st.markdown(_role_label("assistant"), unsafe_allow_html=True)
                answer = st.write_stream(_stream(url, key, hist, None))
            hist.append({"role": "assistant", "content": answer if isinstance(answer, str) else str(answer)})

    # No tiny footer column, fake trash icon, forced widths or off-center glyph.
    with st.container(key="cf_chat_footer"):
        st.markdown(
            '<span class="cf-chat-disclaimer">AI 답변은 참고용이며 투자 권유가 아닙니다. 질문은 품질 확인을 위해 기록될 수 있습니다.</span>',
            unsafe_allow_html=True,
        )
        if st.button("🗑 대화 지우기", key="_cf_chat_clear_bar", help="대화 기록 삭제"):
            st.session_state["_cf_chat"] = []
            st.rerun()


def render_chat_bar(height: int = 340) -> None:
    """Home chat UI; uses Streamlit-native alignment and normal page scrolling."""
    with st.container(key="cf_ai_chat_home"):
        _inject_chat_polish_styles()
        _render_chat_bar_contents(height=height)
