"""
analytics.py
============
대시보드 방문 추적.

Streamlit Community Cloud 의 내장 Analytics 와 무엇이 다른가
-----------------------------------------------------------
내장 기능(share.streamlit.io → 앱 → Analytics)은 **총 조회수와 최근 고유
방문자 20명** 을 보여준다. 그것이 조회수의 공식 수치이며 이 모듈이
대체하지 않는다.

여기서 얻는 것은 내장 기능이 주지 않는 정보다.
  - 어떤 **종목** 을 많이 보는가
  - 어떤 **시간대** 에 들어오는가
  - 한 세션에서 종목을 몇 개나 바꿔 보는가

왜 파일에 못 쓰는가
-------------------
Streamlit Cloud 의 파일시스템은 휘발성이라 앱이 재시작하면 사라진다.
게다가 publish.py 는 게시할 때마다 published/ 를 통째로 교체하므로
저장소에 카운터 파일을 두어도 다음 게시에서 덮어써진다.

그래서 두 가지 경로만 쓴다.
  1. **stdout 로그** (기본) — Streamlit Cloud "Manage app" 로그에서 볼 수 있다.
     설정이 필요 없고 의존성도 없다.
  2. **Firestore** (선택) — st.secrets 에 자격증명이 있을 때만 활성화된다.
     영구 보존이 필요하면 이쪽을 쓴다.

개인정보
--------
공개 앱이므로 방문자를 식별하지 않는다. 세션 ID 는 무작위이고 서버 재시작
시 사라진다. IP·이메일·User-Agent 를 수집하지 않는다.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import streamlit as st

_TZ_KST = timezone.utc  # 표시는 UTC 기준. 필요하면 호출부에서 변환한다.


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kst_now() -> str:
    """웹훅 메시지에는 UTC 대신 KST 로 보여준다."""
    from datetime import timedelta

    kst = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    return kst.strftime("%m/%d %H:%M KST")


def _log(event: str, **fields) -> None:
    """
    stdout 한 줄 JSON. Streamlit Cloud 로그에서 grep 하기 쉽게 접두어를 붙인다.

        Manage app → 로그 → "DASHVIEW" 검색
    """
    payload = {"ts": _now(), "event": event, **fields}
    try:
        print("DASHVIEW " + json.dumps(payload, ensure_ascii=False), flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# Firestore (선택)
# --------------------------------------------------------------------------------------
def _firestore_client():
    """
    st.secrets["firestore"] 에 서비스 계정 JSON 이 있을 때만 연결한다.
    없으면 None 을 돌려주고 로그 경로만 쓴다.
    """
    try:
        creds = st.secrets.get("firestore")  # type: ignore[attr-defined]
    except Exception:
        return None
    if not creds:
        return None
    try:
        from google.cloud import firestore
        from google.oauth2 import service_account

        info = dict(creds) if not isinstance(creds, str) else json.loads(creds)
        cred = service_account.Credentials.from_service_account_info(info)
        return firestore.Client(credentials=cred, project=info.get("project_id"))
    except Exception as exc:
        _log("firestore_init_failed", error=str(exc)[:120])
        return None


@st.cache_resource(show_spinner=False)
def _client_cached():
    return _firestore_client()


def _firestore_write(collection: str, doc: dict) -> None:
    client = _client_cached()
    if client is None:
        return
    try:
        client.collection(collection).add(doc)
    except Exception as exc:
        _log("firestore_write_failed", error=str(exc)[:120])


# --------------------------------------------------------------------------------------
# 웹훅 (Discord / Slack) — 가장 설정이 쉬운 경로
# --------------------------------------------------------------------------------------
# Firestore 는 GCP 프로젝트·서비스 계정·JSON 키가 필요하다. 웹훅은 URL 하나면
# 끝나고, 기록은 채널 히스토리에 남아 휴대폰으로도 바로 볼 수 있다.
#
# st.secrets["webhook_url"] 에 URL 을 넣으면 활성화된다.
#   Discord: 채널 설정 → 연동 → 웹후크 → 새 웹후크 → URL 복사
#   Slack:   api.slack.com/apps → Incoming Webhooks → Add New Webhook
#
# 주의 — 매 방문마다 쏘면 채널이 시끄럽다. 기본은 **세션 시작만** 보낸다.
# 종목 조회까지 받고 싶으면 secrets 에 webhook_verbose = true 를 넣는다.

def _webhook_url() -> Optional[str]:
    try:
        # 1순위: 최상위
        url = st.secrets.get("webhook_url")

        # 2순위: [discord] 섹션 안에 넣은 경우도 지원
        if not url:
            discord = st.secrets.get("discord")
            if discord:
                url = discord.get("webhook_url") or discord.get("url")

        # 3순위: 실수로 [firestore] 아래 들어간 경우도 진단용 지원
        if not url:
            firestore_cfg = st.secrets.get("firestore")
            if firestore_cfg:
                url = firestore_cfg.get("webhook_url")

        if not url:
            _log(
                "webhook_secret_missing",
                secret_keys=list(st.secrets.keys()),
            )
            return None

        url = str(url).strip()

        if not url.startswith(
            ("https://discord.com/api/webhooks/",
             "https://discordapp.com/api/webhooks/")
        ):
            _log("webhook_url_invalid")
            return None

        return url

    except Exception as exc:
        _log(
            "webhook_secret_error",
            error=f"{type(exc).__name__}: {str(exc)[:120]}",
        )
        return None


def _webhook_verbose() -> bool:
    try:
        value = st.secrets.get("webhook_verbose")

        if value is None:
            discord = st.secrets.get("discord")
            if discord:
                value = discord.get("webhook_verbose", False)

        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")

        return bool(value)

    except Exception as exc:
        _log(
            "webhook_verbose_error",
            error=f"{type(exc).__name__}: {str(exc)[:120]}",
        )
        return False




def _webhook_send(text: str) -> None:
    url = _webhook_url()

    if not url:
        _log("webhook_skipped", reason="no_webhook_url")
        return

    try:
        import urllib.request

        body = json.dumps({
            "content": text,
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "StreamlitDashboard/1.0",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=5) as response:
            status = response.status

        _log("webhook_sent", status=status)

    except Exception as exc:
        _log(
            "webhook_failed",
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )

# --------------------------------------------------------------------------------------
# 공개 API
# --------------------------------------------------------------------------------------
def track_session(app_version: str = "") -> str:
    """
    세션 시작을 한 번만 기록한다. 스크립트가 재실행돼도 중복 기록하지 않는다.
    반환값은 세션 ID (표시용).
    """
    if "dashview_sid" not in st.session_state:
        sid = uuid.uuid4().hex[:12]
        st.session_state["dashview_sid"] = sid
        st.session_state["dashview_symbols"] = []
        _log("session_start", sid=sid, version=app_version)
        _firestore_write("dashboard_sessions",
                         {"ts": _now(), "sid": sid, "version": app_version})
        _webhook_send(f"📈 대시보드 방문 · 세션 `{sid}` · {_kst_now()}")
    return str(st.session_state["dashview_sid"])


def track_symbol(symbol: str) -> None:
    """
    종목 조회를 기록한다. 같은 세션에서 같은 종목을 반복 선택해도 한 번만 남긴다.
    이것이 내장 Analytics 가 주지 않는 정보다 — 어떤 종목이 실제로 읽히는가.
    """
    if not symbol:
        return
    seen = st.session_state.setdefault("dashview_symbols", [])
    if symbol in seen:
        return
    seen.append(symbol)
    sid = st.session_state.get("dashview_sid", "?")
    _log("symbol_view", sid=sid, symbol=str(symbol), order=len(seen))
    _firestore_write("dashboard_symbol_views",
                     {"ts": _now(), "sid": sid, "symbol": str(symbol),
                      "order": len(seen)})
    if _webhook_verbose():
        _webhook_send(f"　└ `{sid}` → **{symbol}** ({len(seen)}번째)")


def render_session_footer(show: bool = False) -> None:
    """
    개발자 확인용 각주. 기본은 숨김이다 — 방문자에게 보일 이유가 없다.
    URL 에 ?debug=1 을 붙이면 나타난다.
    """
    try:
        want = show or st.query_params.get("debug") == "1"
    except Exception:
        want = show
    if not want:
        return
    sid = st.session_state.get("dashview_sid", "-")
    syms = st.session_state.get("dashview_symbols", [])
    routes = ["로그"]
    if _client_cached() is not None:
        routes.append("Firestore")
    if _webhook_url():
        routes.append("웹훅" + ("(상세)" if _webhook_verbose() else ""))
    persisted = "+".join(routes)
    st.caption(
        f"세션 {sid} · 이번 세션 조회 종목 {len(syms)}개 "
        f"({', '.join(syms[:8])}{'…' if len(syms) > 8 else ''}) · 저장 {persisted}"
    )
