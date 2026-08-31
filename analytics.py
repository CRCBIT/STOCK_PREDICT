"""
analytics.py
============
Streamlit 대시보드 방문 추적 + Discord 알림 + 일일 통계 그래프.

동작
----
1. 새 Streamlit 세션 → Discord 방문 알림
2. 종목 전환 → Discord 종목 조회 알림 (webhook_verbose=true)
3. 같은 종목 단순 rerun → 조회로 세지 않음
4. A → B → A → A는 두 번째 A까지 재조회로 기록
5. 하루 동안 서버 메모리에 통계를 누적
6. 날짜가 바뀐 뒤 첫 앱 실행 시 전날 통계 그래프를 Discord에 1회 전송

중요
----
Firestore/DB를 사용하지 않는다.

따라서 Streamlit Cloud 프로세스가 재시작되면 메모리 통계는 초기화된다.
Discord Webhook은 과거 메시지를 읽을 수 없으므로 재시작 이전 데이터를
복구할 수는 없다.

또한 백그라운드 스케줄러가 없으므로 정확히 00:00에 보내는 것이 아니라,
날짜가 바뀐 뒤 첫 방문 또는 첫 rerun 시 전날 그래프가 전송된다.

개인정보
--------
IP, 이메일, User-Agent 등 방문자를 식별하는 정보는 수집하지 않는다.
세션 ID는 무작위 UUID 일부만 사용한다.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid

from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import streamlit as st


# ======================================================================================
# 시간
# ======================================================================================

KST = timezone(timedelta(hours=9))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _kst_datetime() -> datetime:
    return _utc_now().astimezone(KST)


def _now() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _kst_now() -> str:
    return _kst_datetime().strftime("%m/%d %H:%M KST")


def _kst_day() -> str:
    return _kst_datetime().strftime("%Y-%m-%d")


# ======================================================================================
# 로그
# ======================================================================================

def _log(event: str, **fields) -> None:
    """
    Streamlit Cloud:
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
# Secrets
# ======================================================================================

def _secret_bool(name: str, default: bool = False) -> bool:
    try:
        value = st.secrets.get(name, default)

        if isinstance(value, str):
            return value.strip().lower() in {
                "true",
                "1",
                "yes",
                "on",
            }

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

    return max(
        minimum,
        min(maximum, value),
    )


# ======================================================================================
# Discord 설정
# ======================================================================================

def _webhook_url() -> Optional[str]:
    """
    지원 형식 1:

        webhook_url = "https://discord.com/api/webhooks/..."

    지원 형식 2:

        [discord]
        webhook_url = "https://discord.com/api/webhooks/..."
    """
    try:
        url = st.secrets.get("webhook_url")

        if not url:
            discord = st.secrets.get("discord")

            if discord:
                url = (
                    discord.get("webhook_url")
                    or discord.get("url")
                )

        if not url:
            _log(
                "webhook_secret_missing",
                secret_keys=list(st.secrets.keys()),
            )
            return None

        url = str(url).strip()

        valid_prefixes = (
            "https://discord.com/api/webhooks/",
            "https://discordapp.com/api/webhooks/",
        )

        if not url.startswith(valid_prefixes):
            _log("webhook_url_invalid")
            return None

        return url

    except Exception as exc:
        _log(
            "webhook_secret_error",
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )
        return None


def _webhook_verbose() -> bool:
    """
    true:
        종목 전환까지 Discord 알림

    false:
        세션 방문 알림만
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
            return value.strip().lower() in {
                "true",
                "1",
                "yes",
                "on",
            }

        return bool(value)

    except Exception:
        return False


def _daily_chart_enabled() -> bool:
    return _secret_bool(
        "webhook_daily_chart",
        True,
    )


def _chart_top_n() -> int:
    """
    종목이 많으므로 그래프에는 상위 종목만 표시.
    """
    return _secret_int(
        "webhook_chart_top_n",
        default=20,
        minimum=5,
        maximum=40,
    )


def _chart_history_days() -> int:
    """
    서버 프로세스가 살아 있는 동안 최근 N일 일별 방문 추세를 보관.
    """
    return _secret_int(
        "webhook_chart_history_days",
        default=7,
        minimum=2,
        maximum=30,
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
            error=f"{type(exc).__name__}: {str(exc)[:220]}",
        )
        return False


# ======================================================================================
# Discord 이미지 전송
# ======================================================================================

def _webhook_send_image(
    image_path: str,
    message: str,
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

        # --------------------------------------------------------------
        # Discord payload_json
        # --------------------------------------------------------------

        body += f"--{boundary}\r\n".encode()

        body += (
            'Content-Disposition: form-data; '
            'name="payload_json"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode()

        body += payload_json
        body += b"\r\n"

        # --------------------------------------------------------------
        # PNG 파일
        # --------------------------------------------------------------

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
# 서버 메모리 통계
# ======================================================================================

class _AnalyticsMemory:
    """
    Streamlit Python 프로세스 안에서 모든 방문 세션이 공유하는 통계.

    st.cache_resource 로 생성되므로 다른 브라우저 세션끼리도 공유된다.

    단:
        Streamlit Cloud 프로세스가 재시작되면 초기화된다.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()

        # 현재 집계 날짜
        self.current_day: str = _kst_day()

        # 현재 날짜 통계
        self.session_count: int = 0
        self.symbol_views: Counter[str] = Counter()
        self.hourly_sessions: Counter[int] = Counter()

        # 완료된 날짜 통계
        self.daily_sessions: OrderedDict[str, int] = OrderedDict()
        self.daily_views: OrderedDict[str, int] = OrderedDict()

        # 아직 Discord 전송되지 않은 날짜 snapshot
        self.pending_snapshots: list[dict] = []


@st.cache_resource(show_spinner=False)
def _analytics_memory() -> _AnalyticsMemory:
    return _AnalyticsMemory()


# ======================================================================================
# 날짜 변경 처리
# ======================================================================================

def _trim_history(
    memory: _AnalyticsMemory,
) -> None:
    keep = _chart_history_days()

    while len(memory.daily_sessions) > keep:
        memory.daily_sessions.popitem(last=False)

    while len(memory.daily_views) > keep:
        memory.daily_views.popitem(last=False)


def _rollover_day_if_needed() -> None:
    """
    날짜가 변경되면 이전 날짜 통계를 snapshot으로 보관하고
    오늘 통계를 새로 시작한다.

    이 함수 자체에서는 Discord HTTP 요청을 하지 않는다.
    """
    memory = _analytics_memory()
    today = _kst_day()

    with memory.lock:

        if memory.current_day == today:
            return

        previous_day = memory.current_day

        # --------------------------------------------------------------
        # 이전 날짜 기록 완료
        # --------------------------------------------------------------

        memory.daily_sessions[
            previous_day
        ] = memory.session_count

        memory.daily_views[
            previous_day
        ] = sum(
            memory.symbol_views.values()
        )

        _trim_history(memory)

        # --------------------------------------------------------------
        # Discord 그래프용 snapshot
        # --------------------------------------------------------------

        snapshot = {
            "date": previous_day,

            "sessions":
                memory.session_count,

            "total_views":
                sum(memory.symbol_views.values()),

            "unique_symbols":
                len(memory.symbol_views),

            "symbol_views":
                dict(memory.symbol_views),

            "hourly_sessions":
                dict(memory.hourly_sessions),

            "daily_sessions":
                list(memory.daily_sessions.items()),

            "daily_views":
                list(memory.daily_views.items()),
        }

        # 아무 사용 기록도 없는 날은 전송할 필요 없음
        if (
            snapshot["sessions"] > 0
            or snapshot["total_views"] > 0
        ):
            memory.pending_snapshots.append(
                snapshot
            )

        _log(
            "analytics_day_rollover",
            previous_day=previous_day,
            new_day=today,
            sessions=snapshot["sessions"],
            views=snapshot["total_views"],
        )

        # --------------------------------------------------------------
        # 오늘 통계 초기화
        # --------------------------------------------------------------

        memory.current_day = today
        memory.session_count = 0
        memory.symbol_views = Counter()
        memory.hourly_sessions = Counter()


# ======================================================================================
# 일일 그래프 생성
# ======================================================================================

def _make_daily_chart(
    snapshot: dict,
) -> Optional[str]:
    """
    한 장에 표시:

    1. 종목 조회 TOP N
    2. 시간대별 방문 세션
    3. 최근 일별 방문 추세

    종목이 수십/수백 개여도 TOP N만 표시.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")

        import matplotlib.pyplot as plt

    except Exception as exc:
        _log(
            "analytics_chart_failed",
            reason="matplotlib_import",
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )
        return None

    try:
        top_n = _chart_top_n()

        symbol_counter = Counter(
            snapshot.get(
                "symbol_views",
                {},
            )
        )

        top_symbols = symbol_counter.most_common(
            top_n
        )

        bar_count = max(
            5,
            len(top_symbols),
        )

        figure_height = max(
            10,
            7 + (bar_count * 0.25),
        )

        fig = plt.figure(
            figsize=(12, figure_height)
        )

        grid = fig.add_gridspec(
            3,
            1,
            height_ratios=[
                max(
                    2.8,
                    bar_count * 0.20,
                ),
                1.5,
                1.5,
            ],
            hspace=0.46,
        )

        # ==============================================================
        # 1. 종목 조회 TOP N
        # ==============================================================

        ax1 = fig.add_subplot(
            grid[0]
        )

        if top_symbols:

            # barh는 마지막 항목이 위에 오므로 reverse
            rows = list(
                reversed(top_symbols)
            )

            labels = [
                symbol
                for symbol, _ in rows
            ]

            values = [
                count
                for _, count in rows
            ]

            bars = ax1.barh(
                labels,
                values,
            )

            ax1.set_title(
                f"Most viewed symbols · Top {len(top_symbols)}"
            )

            ax1.set_xlabel(
                "Views"
            )

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
                "No symbol views",
                ha="center",
                va="center",
                transform=ax1.transAxes,
            )

            ax1.set_title(
                "Most viewed symbols"
            )

        # ==============================================================
        # 2. 시간대별 방문
        # ==============================================================

        ax2 = fig.add_subplot(
            grid[1]
        )

        hourly = snapshot.get(
            "hourly_sessions",
            {},
        )

        hours = list(range(24))

        hour_values = [
            int(
                hourly.get(
                    hour,
                    hourly.get(
                        str(hour),
                        0,
                    ),
                )
            )
            for hour in hours
        ]

        ax2.bar(
            hours,
            hour_values,
        )

        ax2.set_title(
            "Sessions by hour · KST"
        )

        ax2.set_xlabel(
            "Hour"
        )

        ax2.set_ylabel(
            "Sessions"
        )

        ax2.set_xticks(
            list(range(0, 24, 2))
        )

        ax2.grid(
            axis="y",
            alpha=0.25,
        )

        # ==============================================================
        # 3. 최근 날짜별 방문 추세
        # ==============================================================

        ax3 = fig.add_subplot(
            grid[2]
        )

        daily = snapshot.get(
            "daily_sessions",
            [],
        )

        if daily:

            dates = []
            counts = []

            for day_string, count in daily:
                try:
                    parsed = date.fromisoformat(
                        day_string
                    )

                    dates.append(
                        parsed.strftime("%m/%d")
                    )

                except Exception:
                    dates.append(
                        str(day_string)
                    )

                counts.append(
                    int(count)
                )

            ax3.plot(
                dates,
                counts,
                marker="o",
            )

            for index, count in enumerate(
                counts
            ):
                ax3.annotate(
                    str(count),
                    (
                        index,
                        count,
                    ),
                    textcoords="offset points",
                    xytext=(0, 6),
                    ha="center",
                    fontsize=8,
                )

        else:
            ax3.text(
                0.5,
                0.5,
                "No history yet",
                ha="center",
                va="center",
                transform=ax3.transAxes,
            )

        ax3.set_title(
            "Daily sessions · server-memory history"
        )

        ax3.set_ylabel(
            "Sessions"
        )

        ax3.grid(
            axis="y",
            alpha=0.25,
        )

        # ==============================================================
        # 상단 요약
        # ==============================================================

        title = (
            f"Dashboard Analytics · {snapshot['date']} KST\n"
            f"Sessions {snapshot['sessions']}    "
            f"Symbol views {snapshot['total_views']}    "
            f"Unique symbols {snapshot['unique_symbols']}"
        )

        fig.suptitle(
            title,
            fontsize=15,
            fontweight="bold",
            y=0.995,
        )

        fig.subplots_adjust(
            top=0.94,
            bottom=0.06,
            left=0.14,
            right=0.96,
        )

        # ==============================================================
        # 임시 PNG
        # ==============================================================

        temp_file = tempfile.NamedTemporaryFile(
            suffix=".png",
            delete=False,
        )

        path = temp_file.name
        temp_file.close()

        fig.savefig(
            path,
            dpi=150,
            bbox_inches="tight",
        )

        plt.close(fig)

        _log(
            "analytics_chart_created",
            date=snapshot["date"],
            sessions=snapshot["sessions"],
            views=snapshot["total_views"],
            unique_symbols=snapshot["unique_symbols"],
            top_n=top_n,
        )

        return path

    except Exception as exc:

        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass

        _log(
            "analytics_chart_failed",
            error=f"{type(exc).__name__}: {str(exc)[:220]}",
        )

        return None


# ======================================================================================
# 전날 그래프 Discord 전송
# ======================================================================================

def _send_pending_daily_chart() -> None:
    """
    아직 전송되지 않은 일일 snapshot을 Discord로 전송.

    동시 방문이 여러 명 발생해도 같은 snapshot은 한 프로세스에서
    한 번만 꺼내도록 lock 사용.

    실패하면 다시 queue 맨 앞에 넣어 다음 실행 때 재시도.
    """
    if not _daily_chart_enabled():
        return

    if not _webhook_url():
        return

    memory = _analytics_memory()

    # --------------------------------------------------------------
    # queue에서 먼저 제거
    # --------------------------------------------------------------

    with memory.lock:

        if not memory.pending_snapshots:
            return

        snapshot = memory.pending_snapshots.pop(0)

    path = None

    try:
        path = _make_daily_chart(
            snapshot
        )

        if not path:
            # 생성 실패 → 다음 실행에서 재시도
            with memory.lock:
                memory.pending_snapshots.insert(
                    0,
                    snapshot,
                )
            return

        top_n = min(
            _chart_top_n(),
            snapshot["unique_symbols"],
        )

        message = (
            f"📊 **대시보드 일일 방문 통계** · "
            f"`{snapshot['date']}` KST\n"
            f"방문 세션 **{snapshot['sessions']}회** · "
            f"종목 조회 **{snapshot['total_views']}회** · "
            f"고유 종목 **{snapshot['unique_symbols']}개**\n"
            f"종목 그래프는 조회수 상위 **{top_n}개** 표시"
        )

        success = _webhook_send_image(
            path,
            message,
        )

        if success:

            _log(
                "daily_chart_sent",
                date=snapshot["date"],
            )

        else:
            # Discord 실패 → 다음 이벤트 때 재시도
            with memory.lock:
                memory.pending_snapshots.insert(
                    0,
                    snapshot,
                )

    finally:
        if path:
            try:
                os.remove(path)
            except Exception:
                pass


def _daily_maintenance() -> None:
    """
    매 Streamlit rerun 때 매우 가볍게 호출.

    날짜가 그대로면 거의 아무 일도 하지 않는다.
    """
    _rollover_day_if_needed()
    _send_pending_daily_chart()


# ======================================================================================
# 실시간 통계 기록
# ======================================================================================

def _memory_record_session() -> None:
    memory = _analytics_memory()
    now_kst = _kst_datetime()

    with memory.lock:
        memory.session_count += 1
        memory.hourly_sessions[
            now_kst.hour
        ] += 1


def _memory_record_symbol(
    symbol: str,
) -> None:
    memory = _analytics_memory()

    with memory.lock:
        memory.symbol_views[
            symbol
        ] += 1


# ======================================================================================
# 공개 API - Session
# ======================================================================================

def track_session(
    app_version: str = "",
) -> str:
    """
    세션 시작을 한 번만 기록.

    단 날짜 변경 및 일일 그래프 확인은 Streamlit rerun마다 실행된다.
    """

    # ------------------------------------------------------------------
    # 날짜 변경 확인 + 전날 그래프
    # ------------------------------------------------------------------

    _daily_maintenance()

    # ------------------------------------------------------------------
    # 이미 같은 Streamlit 세션이면 새 방문으로 기록하지 않음
    # ------------------------------------------------------------------

    if "dashview_sid" in st.session_state:
        return str(
            st.session_state["dashview_sid"]
        )

    # ------------------------------------------------------------------
    # 새로운 세션
    # ------------------------------------------------------------------

    sid = uuid.uuid4().hex[:12]

    st.session_state[
        "dashview_sid"
    ] = sid

    # 고유 종목
    st.session_state[
        "dashview_symbols"
    ] = []

    # 실제 전환 이력
    # 중복 허용
    st.session_state[
        "dashview_symbol_history"
    ] = []

    # 직전에 보던 종목
    st.session_state[
        "dashview_last_symbol"
    ] = None

    # ------------------------------------------------------------------
    # 서버 전체 일일 통계
    # ------------------------------------------------------------------

    _memory_record_session()

    # ------------------------------------------------------------------
    # 로그
    # ------------------------------------------------------------------

    _log(
        "session_start",
        sid=sid,
        version=app_version,
    )

    # ------------------------------------------------------------------
    # Discord 실시간 알림
    # ------------------------------------------------------------------

    _webhook_send(
        f"📈 대시보드 방문 · "
        f"세션 `{sid}` · "
        f"{_kst_now()}"
    )

    return sid


# ======================================================================================
# 공개 API - Symbol
# ======================================================================================

def track_symbol(
    symbol: str,
) -> None:
    """
    실제 종목 전환만 기록.

    예:

        000660 → MU → 000660

    결과:

        000660 1회
        MU     1회
        000660 2회

    반면 같은 종목 화면에서 Streamlit rerun이 발생하는 것은
    새로운 조회로 세지 않는다.
    """
    if not symbol:
        return

    symbol = str(symbol).strip()

    if not symbol:
        return

    # ------------------------------------------------------------------
    # 직전 종목
    # ------------------------------------------------------------------

    last_symbol = st.session_state.get(
        "dashview_last_symbol"
    )

    # 단순 rerun
    if last_symbol == symbol:
        return

    st.session_state[
        "dashview_last_symbol"
    ] = symbol

    # ------------------------------------------------------------------
    # 세션 전체 실제 조회 순서
    # ------------------------------------------------------------------

    history = st.session_state.setdefault(
        "dashview_symbol_history",
        [],
    )

    history.append(
        symbol
    )

    # ------------------------------------------------------------------
    # 고유 종목
    # ------------------------------------------------------------------

    unique_symbols = st.session_state.setdefault(
        "dashview_symbols",
        [],
    )

    if symbol not in unique_symbols:
        unique_symbols.append(
            symbol
        )

    # ------------------------------------------------------------------
    # 해당 세션 정보
    # ------------------------------------------------------------------

    sid = st.session_state.get(
        "dashview_sid",
        "?",
    )

    order = len(
        history
    )

    symbol_view_count = history.count(
        symbol
    )

    # ------------------------------------------------------------------
    # 서버 전체 일일 통계
    # ------------------------------------------------------------------

    _memory_record_symbol(
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
    # Discord
    # ------------------------------------------------------------------

    if _webhook_verbose():

        _webhook_send(
            f"　└ `{sid}` → **{symbol}** "
            f"(전체 {order}번째 · "
            f"이 종목 {symbol_view_count}번째)"
        )


# ======================================================================================
# 수동 테스트용 - 현재까지 오늘 통계 그래프 전송
# ======================================================================================

def send_current_chart_now() -> bool:
    """
    테스트용.

    오늘 현재까지의 통계를 즉시 Discord로 보낸다.
    자동 일일 그래프 기록에는 영향을 주지 않는다.

    예:

        from analytics import send_current_chart_now
        send_current_chart_now()

    Streamlit 앱에서 직접 호출하지 않으면 전혀 실행되지 않는다.
    """
    memory = _analytics_memory()

    with memory.lock:

        current_daily = OrderedDict(
            memory.daily_sessions
        )

        current_daily[
            memory.current_day
        ] = memory.session_count

        snapshot = {
            "date":
                memory.current_day,

            "sessions":
                memory.session_count,

            "total_views":
                sum(memory.symbol_views.values()),

            "unique_symbols":
                len(memory.symbol_views),

            "symbol_views":
                dict(memory.symbol_views),

            "hourly_sessions":
                dict(memory.hourly_sessions),

            "daily_sessions":
                list(current_daily.items()),

            "daily_views":
                list(memory.daily_views.items()),
        }

    path = _make_daily_chart(
        snapshot
    )

    if not path:
        return False

    try:
        message = (
            f"🧪 **대시보드 통계 테스트** · "
            f"`{snapshot['date']}` 현재까지\n"
            f"방문 세션 **{snapshot['sessions']}회** · "
            f"종목 조회 **{snapshot['total_views']}회** · "
            f"고유 종목 **{snapshot['unique_symbols']}개**"
        )

        return _webhook_send_image(
            path,
            message,
        )

    finally:
        try:
            os.remove(path)
        except Exception:
            pass


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

    memory = _analytics_memory()

    with memory.lock:
        today_sessions = memory.session_count
        today_views = sum(
            memory.symbol_views.values()
        )
        today_unique = len(
            memory.symbol_views
        )

    routes = [
        "로그",
        "서버메모리",
    ]

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
        f"이번 세션 조회 {len(history)}회 · "
        f"고유 종목 {len(unique_symbols)}개 "
        f"({', '.join(unique_symbols[:8])}"
        f"{'…' if len(unique_symbols) > 8 else ''}) · "
        f"오늘 전체 세션 {today_sessions} · "
        f"오늘 전체 조회 {today_views} · "
        f"오늘 고유 종목 {today_unique} · "
        f"저장 {persisted}"
    )