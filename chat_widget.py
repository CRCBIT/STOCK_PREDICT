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

SUGGESTIONS = ["{name} 10일 전망은?", "{name} 판정이 왜 이래?", "메모리 종목 중 20일 기준 제일 좋은 건?", "HBM이 뭐야?"]


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


def render_chat(symbol: str | None = None, name: str | None = None, section_head=None) -> None:
    url, key = _cfg()
    st.session_state.setdefault("_cf_chat_sid", uuid.uuid4().hex[:16])
    hist = st.session_state.setdefault("_cf_chat", [])

    if section_head:
        section_head("AI CHAT", "AI 챗봇", "ChipForecast 예측 데이터에 대해 물어보세요.")

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
