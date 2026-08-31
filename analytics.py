"""
analytics.py
============
대시보드 방문 추적.

Streamlit Community Cloud 의 내장 Analytics 와 무엇이 다른가
-----------------------------------------------------------
내장 기능(share.streamlit.io → 앱 → Analytics)은 총 조회수와 최근 고유
방문자 20명을 보여준다. 그것이 조회수의 공식 수치이며 이 모듈이
대체하지 않는다.

여기서 얻는 것은 내장 기능이 주지 않는 정보다.
  - 어떤 종목을 많이 보는가
  - 어떤 시간대에 들어오는가
  - 한 세션에서 종목을 몇 개나 바꿔 보는가
  - 같은 종목을 다시 보는가

왜 파일에 못 쓰는가
-------------------
Streamlit Cloud 의 파일시스템은 휘발성이라 앱이 재시작하면 사라진다.
게다가 publish.py 는 게시할 때마다 published/ 를 통째로 교체하므로
저장소에 카운터 파일을 두어도 다음 게시에서 덮어써진다.

그래서 다음 경로를 쓴다.
  1. stdout 로그
  2. Firestore (선택)
  3. Discord Webhook (선택)
  4. Discord 누적 통계 그래프 (Firestore 사용 시)

개인정보
--------
공개 앱이므로 방문자를 식별하지 않는다.
세션 ID 는 무작위이다.
IP·이메일·User-Agent 를 수집하지 않는다.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import streamlit as st


# ======================================================================================
# 시간
# ======================================================================================

KST = timezone(timedelta(hours=9))


def _now() -> str:
    """UTC ISO timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kst_now() -> str:
    """Discord 메시지용 현재 KST."""
    return datetime.now(timezone.utc).astimezone(KST).strftime("%m/%d %H:%M KST")


def _kst_today() -> str:
    """YYYY-MM-DD KST."""
    return datetime.now(timezone.utc).astimezone(KST).strftime("%Y-%m-%d")


# ======================================================================================
# 로그
# ======================================================================================

def _log(event: str, **fields) -> None:
    """
    Streamlit Cloud 로그.

    Manage app → Logs → DASHVIEW 검색
    """
    payload = {
        "ts": _now(),
        "event": event,
        **fields,
    }

    try:
        print(
            "DASHVIEW " + json.dumps(payload, ensure_ascii=False),
            flush=True,
        )
    except Exception:
        pass


# ======================================================================================
# Firestore
# ======================================================================================

def _firestore_client():
    """
    st.secrets["firestore"] 설정이 있으면 Firestore 연결.
    없으면 None.
    """
    try:
        creds = st.secrets.get("firestore")
    except Exception:
        return None

    if not creds:
        return None

    try:
        from google.cloud import firestore
        from google.oauth2 import service_account

        info = (
            dict(creds)
            if not isinstance(creds, str)
            else json.loads(creds)
        )

        cred = service_account.Credentials.from_service_account_info(info)

        return firestore.Client(
            credentials=cred,
            project=info.get("project_id"),
        )

    except Exception as exc:
        _log(
            "firestore_init_failed",
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )
        return None


@st.cache_resource(show_spinner=False)
def _client_cached():
    return _firestore_client()


def _firestore_write(collection: str, doc: dict) -> None:
    """Firestore 기록. 실패해도 대시보드에는 영향 없음."""
    client = _client_cached()

    if client is None:
        return

    try:
        client.collection(collection).add(doc)

    except Exception as exc:
        _log(
            "firestore_write_failed",
            collection=collection,
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )


# ======================================================================================
# Discord Webhook 설정
# ======================================================================================

def _webhook_url() -> Optional[str]:
    """
    Discord webhook URL.

    지원:
      webhook_url = "..."

    또는

      [discord]
      webhook_url = "..."
    """
    try:
        # 1순위: 최상위
        url = st.secrets.get("webhook_url")

        # 2순위: [discord]
        if not url:
            discord = st.secrets.get("discord")

            if discord:
                url = (
                    discord.get("webhook_url")
                    or discord.get("url")
                )

        # 3순위:
        # 실수로 [firestore] 아래에 넣은 경우도 읽어줌.
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
            (
                "https://discord.com/api/webhooks/",
                "https://discordapp.com/api/webhooks/",
            )
        ):
            _log("webhook_url_invalid")
            return None

        return url

    except Exception as exc:
        _log(
            "webhook_secret_error",
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )
        return None


def _secret_bool(name: str, default: bool = False) -> bool:
    try:
        value = st.secrets.get(name, default)

        if isinstance(value, str):
            return value.strip().lower() in (
                "true",
                "1",
                "yes",
                "on",
            )

        return bool(value)

    except Exception:
        return default


def _secret_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:

    try:
        value = int(st.secrets.get(name, default))
    except Exception:
        value = default

    return max(minimum, min(maximum, value))


def _webhook_verbose() -> bool:
    """
    종목 조회 알림 여부.

    webhook_verbose = true
    """
    try:
        value = st.secrets.get("webhook_verbose")

        if value is None:
            discord = st.secrets.get("discord")

            if discord:
                value = discord.get(
                    "webhook_verbose",
                    False,
                )

        if isinstance(value, str):
            return value.strip().lower() in (
                "true",
                "1",
                "yes",
                "on",
            )

        return bool(value)

    except Exception as exc:
        _log(
            "webhook_verbose_error",
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )
        return False


def _daily_chart_enabled() -> bool:
    """
    하루 1회 Discord 누적 통계 그래프.

    기본 True.

    끄려면:
        webhook_daily_chart = false
    """
    return _secret_bool(
        "webhook_daily_chart",
        True,
    )


def _chart_days() -> int:
    """
    그래프에 표시할 최근 일수.
    기본 7일.
    """
    return _secret_int(
        "webhook_chart_days",
        default=7,
        minimum=1,
        maximum=30,
    )


def _chart_top_n() -> int:
    """
    종목이 많아도 Discord 이미지가 난잡해지지 않도록
    상위 N개만 표시한다.

    기본 20.
    """
    return _secret_int(
        "webhook_chart_top_n",
        default=20,
        minimum=5,
        maximum=40,
    )


# ======================================================================================
# Discord 텍스트 전송
# ======================================================================================

def _webhook_send(text: str) -> bool:
    url = _webhook_url()

    if not url:
        _log(
            "webhook_skipped",
            reason="no_webhook_url",
        )
        return False

    try:
        import urllib.request

        body = json.dumps(
            {
                "content": text,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "StreamlitDashboard/1.0",
            },
            method="POST",
        )

        with urllib.request.urlopen(
            req,
            timeout=5,
        ) as response:
            status = response.status

        _log(
            "webhook_sent",
            status=status,
        )

        return 200 <= status < 300

    except Exception as exc:
        _log(
            "webhook_failed",
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return False


# ======================================================================================
# Discord 이미지 전송
# ======================================================================================

def _webhook_send_image(
    image_path: str,
    message: str = "📊 대시보드 방문 통계",
) -> bool:

    url = _webhook_url()

    if not url:
        _log(
            "webhook_image_skipped",
            reason="no_webhook_url",
        )
        return False

    try:
        import urllib.request

        boundary = uuid.uuid4().hex

        with open(image_path, "rb") as f:
            image_data = f.read()

        payload_json = json.dumps(
            {
                "content": message,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        body = b""

        # payload_json
        body += f"--{boundary}\r\n".encode()

        body += (
            'Content-Disposition: form-data; '
            'name="payload_json"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode()

        body += payload_json
        body += b"\r\n"

        # image
        body += f"--{boundary}\r\n".encode()

        body += (
            'Content-Disposition: form-data; '
            'name="files[0]"; '
            'filename="dashboard_analytics.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()

        body += image_data
        body += b"\r\n"

        body += f"--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type":
                    f"multipart/form-data; boundary={boundary}",
                "User-Agent": "StreamlitDashboard/1.0",
            },
            method="POST",
        )

        with urllib.request.urlopen(
            req,
            timeout=15,
        ) as response:
            status = response.status

        _log(
            "webhook_image_sent",
            status=status,
        )

        return 200 <= status < 300

    except Exception as exc:
        _log(
            "webhook_image_failed",
            error=f"{type(exc).__name__}: {str(exc)[:220]}",
        )
        return False


# ======================================================================================
# Firestore 통계 읽기
# ======================================================================================

def _parse_ts(value) -> Optional[datetime]:
    if value is None:
        return None

    # Firestore Timestamp 객체인 경우
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            text = str(value).strip()

            if text.endswith("Z"):
                text = text[:-1] + "+00:00"

            dt = datetime.fromisoformat(text)

        except Exception:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def _recent_firestore_docs(
    collection_name: str,
    start_iso: str,
):
    """
    최근 데이터만 조회한다.

    가능한 경우 Firestore where 사용.
    문제가 생기면 전체 읽기 후 Python 필터 fallback.
    """
    client = _client_cached()

    if client is None:
        return []

    collection = client.collection(collection_name)

    # 최신 Firestore API
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = collection.where(
            filter=FieldFilter(
                "ts",
                ">=",
                start_iso,
            )
        )

        return list(query.stream())

    except Exception:
        pass

    # 이전 API
    try:
        query = collection.where(
            "ts",
            ">=",
            start_iso,
        )

        return list(query.stream())

    except Exception:
        pass

    # 최후 fallback
    docs = []

    try:
        for doc in collection.stream():
            data = doc.to_dict() or {}

            ts = str(data.get("ts", ""))

            if ts >= start_iso:
                docs.append(doc)

    except Exception as exc:
        _log(
            "analytics_read_failed",
            collection=collection_name,
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )

    return docs


def _load_analytics_stats(
    days: int = 7,
) -> Optional[dict]:
    """
    Firestore 누적 방문 데이터 집계.
    """
    client = _client_cached()

    if client is None:
        return None

    now_kst = datetime.now(timezone.utc).astimezone(KST)

    first_date = (
        now_kst.date()
        - timedelta(days=days - 1)
    )

    start_kst = datetime(
        first_date.year,
        first_date.month,
        first_date.day,
        tzinfo=KST,
    )

    start_utc = start_kst.astimezone(timezone.utc)

    start_iso = start_utc.isoformat(
        timespec="seconds"
    )

    # ------------------------------------------------------------------
    # 세션
    # ------------------------------------------------------------------

    session_docs = _recent_firestore_docs(
        "dashboard_sessions",
        start_iso,
    )

    # ------------------------------------------------------------------
    # 종목 조회
    # ------------------------------------------------------------------

    view_docs = _recent_firestore_docs(
        "dashboard_symbol_views",
        start_iso,
    )

    daily_sessions = Counter()
    hourly_sessions = Counter()
    symbol_views = Counter()

    session_ids = set()

    # ------------------------------------------------------------------
    # 세션 집계
    # ------------------------------------------------------------------

    for doc in session_docs:
        try:
            data = doc.to_dict() or {}
        except Exception:
            continue

        ts = _parse_ts(data.get("ts"))

        if ts is None:
            continue

        kst = ts.astimezone(KST)

        if kst.date() < first_date:
            continue

        day_key = kst.strftime("%m/%d")
        hour_key = kst.hour

        daily_sessions[day_key] += 1
        hourly_sessions[hour_key] += 1

        sid = data.get("sid")

        if sid:
            session_ids.add(str(sid))

    # ------------------------------------------------------------------
    # 종목 조회 집계
    # ------------------------------------------------------------------

    for doc in view_docs:
        try:
            data = doc.to_dict() or {}
        except Exception:
            continue

        ts = _parse_ts(data.get("ts"))

        if ts is None:
            continue

        kst = ts.astimezone(KST)

        if kst.date() < first_date:
            continue

        symbol = str(
            data.get("symbol", "")
        ).strip()

        if symbol:
            symbol_views[symbol] += 1

    # ------------------------------------------------------------------
    # 날짜 전체 생성
    # 데이터 없는 날도 0으로 표시
    # ------------------------------------------------------------------

    date_labels = []

    for offset in range(days):
        d = first_date + timedelta(days=offset)

        date_labels.append(
            d.strftime("%m/%d")
        )

    return {
        "days": days,
        "date_labels": date_labels,
        "daily_sessions": daily_sessions,
        "hourly_sessions": hourly_sessions,
        "symbol_views": symbol_views,
        "total_sessions": len(session_docs),
        "unique_sessions": len(session_ids),
        "total_symbol_views": sum(symbol_views.values()),
        "unique_symbols": len(symbol_views),
    }


# ======================================================================================
# 통계 그래프
# ======================================================================================

def make_analytics_chart(
    days: int = 7,
    top_n: int = 20,
) -> tuple[Optional[str], Optional[dict]]:
    """
    Discord 전송용 PNG.

    1. 종목 조회 TOP N
    2. 일별 방문 세션
    3. 시간대별 방문

    종목이 많아도 TOP N만 표시한다.
    """
    stats = _load_analytics_stats(days)

    if stats is None:
        _log(
            "analytics_chart_skipped",
            reason="firestore_unavailable",
        )
        return None, None

    try:
        import matplotlib

        # 서버 환경용
        matplotlib.use("Agg")

        import matplotlib.pyplot as plt

    except Exception as exc:
        _log(
            "analytics_chart_failed",
            reason="matplotlib_import",
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )
        return None, stats

    symbol_views: Counter = stats["symbol_views"]

    top_symbols = symbol_views.most_common(top_n)

    # ------------------------------------------------------------------
    # 그래프 크기
    # TOP 20까지 가독성 확보
    # ------------------------------------------------------------------

    bar_count = max(
        5,
        len(top_symbols),
    )

    figure_height = max(
        10,
        7 + bar_count * 0.26,
    )

    try:
        fig = plt.figure(
            figsize=(12, figure_height)
        )

        grid = fig.add_gridspec(
            3,
            1,
            height_ratios=[
                max(2.8, bar_count * 0.20),
                1.5,
                1.5,
            ],
            hspace=0.42,
        )

        # ==================================================================
        # 1. TOP 종목
        # ==================================================================

        ax1 = fig.add_subplot(grid[0])

        if top_symbols:
            # 높은 순위가 위로 오도록 뒤집음
            top_symbols_rev = list(
                reversed(top_symbols)
            )

            labels = [
                x[0]
                for x in top_symbols_rev
            ]

            values = [
                x[1]
                for x in top_symbols_rev
            ]

            bars = ax1.barh(
                labels,
                values,
            )

            ax1.set_title(
                f"Top {min(top_n, len(top_symbols))} symbols by views"
            )

            ax1.set_xlabel(
                "Views"
            )

            # 숫자 표시
            for bar, count in zip(
                bars,
                values,
            ):
                ax1.text(
                    bar.get_width(),
                    bar.get_y()
                    + bar.get_height() / 2,
                    f"  {count}",
                    va="center",
                    fontsize=9,
                )

        else:
            ax1.text(
                0.5,
                0.5,
                "No symbol view data",
                ha="center",
                va="center",
                transform=ax1.transAxes,
            )

            ax1.set_title(
                "Symbol views"
            )

        # ==================================================================
        # 2. 일별 세션
        # ==================================================================

        ax2 = fig.add_subplot(grid[1])

        date_labels = stats["date_labels"]

        daily_values = [
            stats["daily_sessions"].get(
                day,
                0,
            )
            for day in date_labels
        ]

        ax2.plot(
            date_labels,
            daily_values,
            marker="o",
        )

        ax2.set_title(
            f"Daily sessions · last {days} days"
        )

        ax2.set_ylabel(
            "Sessions"
        )

        ax2.grid(
            axis="y",
            alpha=0.25,
        )

        for x, value in zip(
            range(len(date_labels)),
            daily_values,
        ):
            ax2.annotate(
                str(value),
                (x, value),
                textcoords="offset points",
                xytext=(0, 6),
                ha="center",
                fontsize=8,
            )

        # ==================================================================
        # 3. 시간대별 방문
        # ==================================================================

        ax3 = fig.add_subplot(grid[2])

        hours = list(range(24))

        hour_values = [
            stats["hourly_sessions"].get(
                hour,
                0,
            )
            for hour in hours
        ]

        ax3.bar(
            hours,
            hour_values,
        )

        ax3.set_title(
            "Sessions by hour · KST"
        )

        ax3.set_xlabel(
            "Hour"
        )

        ax3.set_ylabel(
            "Sessions"
        )

        ax3.set_xticks(
            list(range(0, 24, 2))
        )

        ax3.grid(
            axis="y",
            alpha=0.25,
        )

        # ==================================================================
        # 전체 요약
        # ==================================================================

        summary = (
            f"Dashboard Analytics · {_kst_today()} KST\n"
            f"Sessions {stats['total_sessions']}    "
            f"Symbol views {stats['total_symbol_views']}    "
            f"Unique symbols {stats['unique_symbols']}"
        )

        fig.suptitle(
            summary,
            fontsize=15,
            fontweight="bold",
            y=0.995,
        )

        fig.subplots_adjust(
            top=0.94,
            bottom=0.06,
            left=0.13,
            right=0.96,
        )

        # ------------------------------------------------------------------
        # 임시 PNG
        # ------------------------------------------------------------------

        tmp = tempfile.NamedTemporaryFile(
            suffix=".png",
            delete=False,
        )

        path = tmp.name
        tmp.close()

        fig.savefig(
            path,
            dpi=150,
            bbox_inches="tight",
        )

        plt.close(fig)

        _log(
            "analytics_chart_created",
            path=os.path.basename(path),
            days=days,
            top_n=top_n,
            sessions=stats["total_sessions"],
            views=stats["total_symbol_views"],
        )

        return path, stats

    except Exception as exc:
        try:
            plt.close("all")
        except Exception:
            pass

        _log(
            "analytics_chart_failed",
            error=f"{type(exc).__name__}: {str(exc)[:220]}",
        )

        return None, stats


# ======================================================================================
# Discord 통계 그래프 즉시 전송
# ======================================================================================

def send_analytics_chart_now(
    days: Optional[int] = None,
    top_n: Optional[int] = None,
) -> bool:
    """
    현재 누적 Firestore 데이터로
    Discord 통계 그래프 즉시 전송.

    필요하면 다른 코드에서 직접 호출 가능:

        from analytics import send_analytics_chart_now
        send_analytics_chart_now()
    """
    if days is None:
        days = _chart_days()

    if top_n is None:
        top_n = _chart_top_n()

    path = None

    try:
        path, stats = make_analytics_chart(
            days=days,
            top_n=top_n,
        )

        if not path or not stats:
            return False

        message = (
            f"📊 **대시보드 방문 통계** · {_kst_now()}\n"
            f"최근 **{days}일** · "
            f"세션 **{stats['total_sessions']}회** · "
            f"종목 조회 **{stats['total_symbol_views']}회** · "
            f"고유 종목 **{stats['unique_symbols']}개**\n"
            f"그래프에는 조회수 상위 **{min(top_n, stats['unique_symbols'])}개 종목**을 표시합니다."
        )

        return _webhook_send_image(
            path,
            message,
        )

    finally:
        if path:
            try:
                os.remove(path)
            except Exception:
                pass


# ======================================================================================
# 하루 한 번만 Discord 그래프
# ======================================================================================

def _maybe_send_daily_chart() -> None:
    """
    그날 Discord 통계 이미지가 아직 전송되지 않았다면
    첫 Streamlit 세션 시작 시 한 번 전송한다.

    Firestore dashboard_meta/discord_daily_chart 에
    마지막 전송 날짜를 기록한다.
    """
    if not _daily_chart_enabled():
        return

    if not _webhook_url():
        return

    client = _client_cached()

    if client is None:
        _log(
            "daily_chart_skipped",
            reason="firestore_unavailable",
        )
        return

    today = _kst_today()

    try:
        meta_ref = (
            client
            .collection("dashboard_meta")
            .document("discord_daily_chart")
        )

        snapshot = meta_ref.get()

        if snapshot.exists:
            meta = snapshot.to_dict() or {}

            if meta.get("last_sent_kst") == today:
                return

    except Exception as exc:
        _log(
            "daily_chart_meta_read_failed",
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )

        return

    # ------------------------------------------------------------------
    # 이미지 생성 + 전송
    # ------------------------------------------------------------------

    success = send_analytics_chart_now(
        days=_chart_days(),
        top_n=_chart_top_n(),
    )

    if not success:
        return

    # ------------------------------------------------------------------
    # 오늘 이미 보냈다고 기록
    # ------------------------------------------------------------------

    try:
        meta_ref.set(
            {
                "last_sent_kst": today,
                "last_sent_at": _now(),
            },
            merge=True,
        )

        _log(
            "daily_chart_marked",
            date=today,
        )

    except Exception as exc:
        _log(
            "daily_chart_meta_write_failed",
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )


# ======================================================================================
# 공개 API
# ======================================================================================

def track_session(
    app_version: str = "",
) -> str:
    """
    Streamlit 세션 시작을 한 번만 기록.

    script rerun은 새 세션으로 세지 않는다.
    """
    if "dashview_sid" not in st.session_state:

        sid = uuid.uuid4().hex[:12]

        st.session_state[
            "dashview_sid"
        ] = sid

        # 고유 종목
        st.session_state[
            "dashview_symbols"
        ] = []

        # 실제 조회 순서
        # 중복 허용
        st.session_state[
            "dashview_symbol_history"
        ] = []

        # 직전 종목
        st.session_state[
            "dashview_last_symbol"
        ] = None

        # ------------------------------------------------------------------
        # 로그
        # ------------------------------------------------------------------

        _log(
            "session_start",
            sid=sid,
            version=app_version,
        )

        # ------------------------------------------------------------------
        # Firestore
        # ------------------------------------------------------------------

        _firestore_write(
            "dashboard_sessions",
            {
                "ts": _now(),
                "sid": sid,
                "version": app_version,
            },
        )

        # ------------------------------------------------------------------
        # Discord 실시간 방문 알림
        # ------------------------------------------------------------------

        _webhook_send(
            f"📈 대시보드 방문 · "
            f"세션 `{sid}` · "
            f"{_kst_now()}"
        )

        # ------------------------------------------------------------------
        # Discord 하루 1회 통계 그래프
        # ------------------------------------------------------------------

        _maybe_send_daily_chart()

    return str(
        st.session_state["dashview_sid"]
    )


def track_symbol(
    symbol: str,
) -> None:
    """
    실제 종목 전환만 기록.

    예:
        000660 → MU → 000660

    기록:
        000660 1회
        MU 1회
        000660 2회

    단순 Streamlit rerun은 조회로 세지 않는다.
    """
    if not symbol:
        return

    symbol = str(symbol)

    # ------------------------------------------------------------------
    # 직전 종목
    # ------------------------------------------------------------------

    last_symbol = st.session_state.get(
        "dashview_last_symbol"
    )

    # 동일 종목 화면 rerun
    if last_symbol == symbol:
        return

    # ------------------------------------------------------------------
    # 현재 종목
    # ------------------------------------------------------------------

    st.session_state[
        "dashview_last_symbol"
    ] = symbol

    # ------------------------------------------------------------------
    # 실제 조회 이력
    # ------------------------------------------------------------------

    history = st.session_state.setdefault(
        "dashview_symbol_history",
        [],
    )

    history.append(symbol)

    # ------------------------------------------------------------------
    # 고유 종목 목록
    # ------------------------------------------------------------------

    unique_symbols = st.session_state.setdefault(
        "dashview_symbols",
        [],
    )

    if symbol not in unique_symbols:
        unique_symbols.append(symbol)

    # ------------------------------------------------------------------
    # 정보
    # ------------------------------------------------------------------

    sid = st.session_state.get(
        "dashview_sid",
        "?",
    )

    order = len(history)

    symbol_view_count = history.count(
        symbol
    )

    # ------------------------------------------------------------------
    # stdout
    # ------------------------------------------------------------------

    _log(
        "symbol_view",
        sid=sid,
        symbol=symbol,
        order=order,
        symbol_view_count=symbol_view_count,
    )

    # ------------------------------------------------------------------
    # Firestore
    # ------------------------------------------------------------------

    _firestore_write(
        "dashboard_symbol_views",
        {
            "ts": _now(),
            "sid": sid,
            "symbol": symbol,
            "order": order,
            "symbol_view_count":
                symbol_view_count,
        },
    )

    # ------------------------------------------------------------------
    # Discord 상세 알림
    # ------------------------------------------------------------------

    if _webhook_verbose():

        _webhook_send(
            f"　└ `{sid}` → **{symbol}** "
            f"(전체 {order}번째 · "
            f"이 종목 {symbol_view_count}번째)"
        )


# ======================================================================================
# 개발자 Footer
# ======================================================================================

def render_session_footer(
    show: bool = False,
) -> None:
    """
    URL:
        ?debug=1

    일 때만 표시.
    """
    try:
        want = (
            show
            or st.query_params.get("debug") == "1"
        )

    except Exception:
        want = show

    if not want:
        return

    sid = st.session_state.get(
        "dashview_sid",
        "-",
    )

    unique_symbols = st.session_state.get(
        "dashview_symbols",
        [],
    )

    history = st.session_state.get(
        "dashview_symbol_history",
        [],
    )

    routes = ["로그"]

    if _client_cached() is not None:
        routes.append(
            "Firestore"
        )

    if _webhook_url():
        routes.append(
            "웹훅"
            + (
                "(상세)"
                if _webhook_verbose()
                else ""
            )
        )

    persisted = "+".join(
        routes
    )

    st.caption(
        f"세션 {sid} · "
        f"조회 {len(history)}회 · "
        f"고유 종목 {len(unique_symbols)}개 "
        f"({', '.join(unique_symbols[:8])}"
        f"{'…' if len(unique_symbols) > 8 else ''}) · "
        f"저장 {persisted}"
    )