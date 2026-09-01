"""
analytics.py
============
Streamlit 대시보드 방문 추적 + Discord 알림 + 일일 통계 그래프.

동작
----
1. 새 Streamlit 세션 → 일반 / 재연결 의심 / 자동접속 의심으로 분류하고 첫 종목과 합쳐 Discord 1회 알림
2. 같은 클라이언트가 짧은 간격으로 새 세션을 만들고 이전 세션에 상호작용이 없으면 재연결 의심
3. 첫 화면의 자동 선택 종목은 알림 생략, 사용자가 실제로 종목을 전환할 때만 Discord 알림 (webhook_verbose=true)
4. 같은 종목 단순 rerun → 조회로 세지 않음
5. A → B → A → A는 두 번째 A까지 재조회로 기록
6. 하루 동안 전체 연결/일반/재연결 의심/자동접속 의심/활동 세션을 따로 누적
7. 날짜가 바뀐 뒤 첫 앱 실행 시 전날 통계 그래프를 Discord에 1회 전송

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
원본 IP 주소는 Discord 세션 알림에 표시하고, 현재 Streamlit 서버 메모리의 세션 기록에도 보관한다.
가능하면 브라우저에서 직접 조회한 공인 IP를 사용하고, 실패할 때만 프록시 전달 헤더/st.context로 폴백한다.
IP 기반 추정 위치/통신사 조회는 백그라운드에서 수행해 화면 로딩을 막지 않는다.
전체 User-Agent 문자열은 저장하지 않고 클라이언트 종류만 표시한다.
세션 재연결 판별에는 브라우저 공인 IP 또는 폴백 IP/User-Agent/시간대/locale을 즉시 조합한 뒤
프로세스마다 새로 생성되는 salt로 해시한 짧은 임시 클라이언트 지문만 사용한다.
서버 프로세스가 재시작되면 salt와 지문 이력도 함께 사라진다.
세션 ID는 무작위 UUID 일부만 사용한다.

선택 설정 (Streamlit secrets)
------------------------------
webhook_session_diagnostics = true
analytics_reconnect_min_minutes = 10
analytics_reconnect_max_minutes = 25
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import tempfile
import threading
import uuid
import secrets

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

def _is_operator() -> bool:
    """
    운영자 접속 여부.

    Streamlit Secrets:
        analytics_owner_token = "충분히_긴_랜덤_문자열"

    운영자 접속:
        https://앱주소.streamlit.app/?owner=비밀문자열

    운영자는 방문/종목조회/Discord 통계에서 제외한다.
    """

    # 같은 Streamlit 세션에서는 한 번 판정한 값을 계속 사용
    if "dashview_is_operator" in st.session_state:
        return bool(st.session_state["dashview_is_operator"])

    try:
        expected = str(
            st.secrets.get("analytics_owner_token", "")
        ).strip()

        supplied = str(
            st.query_params.get("owner", "")
        ).strip()

        is_operator = bool(
            expected
            and supplied
            and secrets.compare_digest(expected, supplied)
        )

    except Exception:
        is_operator = False

    st.session_state["dashview_is_operator"] = is_operator

    if is_operator:
        _log("operator_session_excluded")

    return is_operator

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


def _session_diagnostics_enabled() -> bool:
    """Discord 세션 알림에 재연결/클라이언트 진단을 함께 표시한다."""
    return _secret_bool(
        "webhook_session_diagnostics",
        True,
    )


def _reconnect_min_minutes() -> int:
    """재연결 의심 구간의 시작. 기본 10분."""
    return _secret_int(
        "analytics_reconnect_min_minutes",
        default=10,
        minimum=1,
        maximum=120,
    )


def _reconnect_max_minutes() -> int:
    """재연결 의심 구간의 끝. 기본 25분."""
    lower = _reconnect_min_minutes()
    return _secret_int(
        "analytics_reconnect_max_minutes",
        default=max(25, lower),
        minimum=lower,
        maximum=240,
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
    Streamlit Python 프로세스 안에서 모든 브라우저 세션이 공유하는 통계.

    st.cache_resource 로 생성되므로 다른 브라우저 세션끼리도 공유된다.
    Streamlit Cloud 프로세스가 재시작되면 통계/임시 클라이언트 지문 이력이 초기화된다.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()

        # 현재 집계 날짜
        self.current_day: str = _kst_day()

        # 현재 날짜 통계: session_count는 모든 새 Streamlit 연결(legacy 의미)을 센다.
        self.session_count: int = 0
        self.normal_session_count: int = 0
        self.reconnect_session_count: int = 0
        self.bot_session_count: int = 0
        self.engaged_session_count: int = 0

        self.symbol_views: Counter[str] = Counter()
        self.hourly_sessions: Counter[int] = Counter()
        self.hourly_normal_sessions: Counter[int] = Counter()

        # 완료된 날짜 통계
        self.daily_sessions: OrderedDict[str, int] = OrderedDict()
        self.daily_normal_sessions: OrderedDict[str, int] = OrderedDict()
        self.daily_views: OrderedDict[str, int] = OrderedDict()

        # 아직 Discord 전송되지 않은 날짜 snapshot
        self.pending_snapshots: list[dict] = []

        # 재연결 판별용. 원본 IP/UA는 저장하지 않는다.
        self.fingerprint_salt: bytes = secrets.token_bytes(24)
        self.recent_clients: dict[str, dict] = {}
        self.session_records: dict[str, dict] = {}
        self.engaged_sids: set[str] = set()


@st.cache_resource(show_spinner=False)
def _analytics_memory_v2() -> _AnalyticsMemory:
    # v2 이름을 사용해 이전 배포에서 남아 있던 cache_resource 객체와 분리한다.
    # Streamlit hot-reload에서는 오래된 _AnalyticsMemory 인스턴스가 살아남을 수 있다.
    return _AnalyticsMemory()


def _ensure_memory_schema(memory: _AnalyticsMemory) -> _AnalyticsMemory:
    """
    hot-reload 뒤에도 구버전 cache_resource가 남아 있을 수 있으므로
    새 필드를 지연 초기화한다. 앱 재부팅 없이도 안전하게 마이그레이션한다.
    """
    # 구버전에도 lock은 있었지만, 혹시 없는 경우까지 방어한다.
    if not hasattr(memory, "lock"):
        memory.lock = threading.RLock()

    with memory.lock:
        defaults = {
            "normal_session_count": 0,
            "reconnect_session_count": 0,
            "bot_session_count": 0,
            "engaged_session_count": 0,
            "hourly_normal_sessions": Counter(),
            "daily_normal_sessions": OrderedDict(),
            "recent_clients": {},
            "session_records": {},
            "engaged_sids": set(),
        }

        for name, value in defaults.items():
            if not hasattr(memory, name):
                setattr(memory, name, value)

        if not hasattr(memory, "fingerprint_salt"):
            memory.fingerprint_salt = secrets.token_bytes(24)

    return memory


def _analytics_memory() -> _AnalyticsMemory:
    return _ensure_memory_schema(_analytics_memory_v2())


def _safe_context_value(name: str, default=""):
    """Streamlit 버전 차이를 견디도록 st.context 값을 안전하게 읽는다."""
    try:
        context = getattr(st, "context", None)
        if context is None:
            return default
        value = getattr(context, name, default)
        return default if value is None else value
    except Exception:
        return default


def _context_headers() -> dict[str, str]:
    try:
        headers = _safe_context_value("headers", {})
        return {str(k).lower(): str(v) for k, v in dict(headers).items()}
    except Exception:
        return {}


def _client_family(user_agent: str) -> str:
    """전체 UA를 보관하지 않고 Discord에 보여줄 거친 클라이언트 이름만 만든다."""
    ua = (user_agent or "").lower()

    if not ua:
        return "알 수 없음"
    if any(token in ua for token in (
        "bot", "crawler", "spider", "headless", "python-requests",
        "curl/", "wget/", "uptime", "monitor", "healthcheck", "probe",
    )):
        return "자동화/봇 계열"

    device = ""
    if "iphone" in ua:
        device = "iPhone"
    elif "ipad" in ua:
        device = "iPad"
    elif "android" in ua:
        device = "Android"
    elif "windows" in ua:
        device = "Windows"
    elif "macintosh" in ua or "mac os x" in ua:
        device = "macOS"
    elif "linux" in ua:
        device = "Linux"

    browser = "Browser"
    if "edg/" in ua:
        browser = "Edge"
    elif "crios/" in ua or "chrome/" in ua:
        browser = "Chrome"
    elif "fxios/" in ua or "firefox/" in ua:
        browser = "Firefox"
    elif "safari/" in ua and "chrome/" not in ua and "crios/" not in ua:
        browser = "Safari"

    return f"{device} {browser}".strip()


def _looks_like_bot(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    if not ua:
        return False
    return any(token in ua for token in (
        "bot", "crawler", "spider", "headless", "python-requests",
        "curl/", "wget/", "uptime", "monitor", "healthcheck", "probe",
    ))


def _mask_ip(ip_address: str) -> str:
    """Discord에는 원본 IP 대신 일부만 남긴 마스킹 주소를 표시한다."""
    value = str(ip_address or "").strip()
    if not value:
        return "-"

    # IPv4: 211.234.56.78 -> 211.234.xxx.xxx
    parts = value.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        return f"{parts[0]}.{parts[1]}.xxx.xxx"

    # IPv6: 앞 두 블록만 남기고 나머지는 숨긴다.
    if ":" in value:
        blocks = [block for block in value.split(":") if block]
        if len(blocks) >= 2:
            return f"{blocks[0]}:{blocks[1]}:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx"
        if blocks:
            return f"{blocks[0]}:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx:xxxx"
        return "IPv6(masked)"

    return "masked"



# ======================================================================================
# GeoIP - 비동기 조회
# ======================================================================================

_GEO_CACHE_LOCK = threading.RLock()
_GEO_CACHE: dict[str, tuple[datetime, dict]] = {}


def _normalize_public_ip(ip_address: str) -> str:
    """GeoIP 조회에 사용할 공개 IP만 정규화한다."""
    value = str(ip_address or "").strip()
    if not value or value == "-":
        return ""

    try:
        parsed = ipaddress.ip_address(value)
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            parsed = parsed.ipv4_mapped
        if not parsed.is_global:
            return ""
        return str(parsed)
    except ValueError:
        return ""


def _empty_geo() -> dict:
    return {
        "geo_ok": False,
        "country": "-",
        "country_code": "-",
        "region": "-",
        "city": "-",
        "location": "조회 불가",
        "isp": "-",
        "org": "-",
        "asn": "-",
        "flag": "",
    }


def _lookup_geoip(ip_address: str) -> dict:
    """
    IP 기반 국가/지역/도시/ISP 추정.

    이 함수는 Streamlit 렌더링 스레드가 아니라 daemon thread에서 호출한다.
    외부 GeoIP 서비스가 느리거나 장애가 나도 대시보드 화면 로딩을 막지 않는다.
    """
    public_ip = _normalize_public_ip(ip_address)
    if not public_ip:
        return _empty_geo()

    now = _utc_now()
    with _GEO_CACHE_LOCK:
        cached = _GEO_CACHE.get(public_ip)
        if cached and (now - cached[0]) < timedelta(hours=6):
            return dict(cached[1])

    result = _empty_geo()
    try:
        import urllib.parse
        import urllib.request

        encoded_ip = urllib.parse.quote(public_ip, safe=":")
        req = urllib.request.Request(
            f"https://ipwho.is/{encoded_ip}",
            headers={"User-Agent": "StockForecastDashboard/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=2.5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))

        if isinstance(payload, dict) and payload.get("success", False):
            country = str(payload.get("country") or "-").strip()
            country_code = str(payload.get("country_code") or "-").strip().upper()
            region = str(payload.get("region") or "-").strip()
            city = str(payload.get("city") or "-").strip()
            display_country = "대한민국" if country_code == "KR" else country

            parts = []
            for part in (display_country, region, city):
                if part and part != "-" and part not in parts:
                    parts.append(part)

            connection = payload.get("connection") or {}
            if not isinstance(connection, dict):
                connection = {}
            isp = str(connection.get("isp") or "-").strip()
            org = str(connection.get("org") or "-").strip()
            asn_value = connection.get("asn")
            asn = f"AS{asn_value}" if asn_value not in (None, "", "-") else "-"

            flag_data = payload.get("flag") or {}
            flag = (
                str(flag_data.get("emoji") or "").strip()
                if isinstance(flag_data, dict)
                else ""
            )

            result = {
                "geo_ok": True,
                "country": display_country or "-",
                "country_code": country_code or "-",
                "region": region or "-",
                "city": city or "-",
                "location": " · ".join(parts) if parts else "조회 불가",
                "isp": isp or "-",
                "org": org or "-",
                "asn": asn,
                "flag": flag,
            }
        else:
            _log(
                "geoip_lookup_failed",
                ip=public_ip,
                reason=(
                    str(payload.get("message") or "lookup_unsuccessful")[:160]
                    if isinstance(payload, dict)
                    else "invalid_payload"
                ),
            )
    except Exception as exc:
        _log(
            "geoip_lookup_failed",
            ip=public_ip,
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )

    with _GEO_CACHE_LOCK:
        _GEO_CACHE[public_ip] = (now, dict(result))
    return result


def _webhook_send_to_url(url: str, text: str) -> bool:
    """백그라운드 스레드용 Discord 전송. st.secrets/st.context에 접근하지 않는다."""
    if not url:
        return False
    try:
        import urllib.request

        body = json.dumps({"content": text}, ensure_ascii=False).encode("utf-8")
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
        return 200 <= status < 300
    except Exception as exc:
        _log(
            "webhook_failed",
            error=f"{type(exc).__name__}: {str(exc)[:220]}",
        )
        return False


def _send_first_session_notification_async(
    webhook_url: str,
    payload: dict,
    first_symbol: str,
) -> None:
    """GeoIP 조회 후 첫 종목까지 합친 세션 메시지를 백그라운드에서 1회 전송한다."""
    try:
        raw_ip = str(payload.get("raw_ip") or "-")
        geo = _lookup_geoip(raw_ip)

        location_prefix = f"{geo.get('flag', '')} " if geo.get("flag") else ""
        ip_source = str(payload.get("ip_source") or "unavailable")
        context_ip = str(payload.get("context_ip") or "-")
        ip_display = raw_ip if raw_ip not in {"", "-"} else "실제 클라이언트 IP 조회 불가"
        lines = [
            f"{payload.get('icon', '📈')} **{payload.get('label', '새 세션')}** · "
            f"세션 `{payload.get('sid', '?')}` · {payload.get('kst_time', '-')}",
            f"　IP `{ip_display}` · source `{ip_source}`",
        ]
        if raw_ip in {"", "-"} and context_ip not in {"", "-"}:
            lines.append(f"　프록시 연결 IP `{context_ip}`")
        lines.extend([
            f"　추정 위치 **{location_prefix}{geo.get('location', '조회 불가')}**",
            f"　통신사 **{geo.get('isp', '-')}** · ASN `{geo.get('asn', '-')}`",
            f"　클라이언트 **{payload.get('client', '-')}** · "
            f"TZ `{payload.get('timezone', '-')}` · locale `{payload.get('locale', '-')}`",
            f"　임시지문 `{payload.get('fingerprint') or '-'}`",
        ])

        gap = payload.get("gap_minutes")
        if gap is not None:
            previous_state = "활동 있음" if payload.get("previous_engaged") else "활동 없음"
            lines.append(
                f"　이전 동일 클라이언트 **{float(gap):.1f}분 전** · 이전 세션 {previous_state}"
            )

        if payload.get("classification") != "normal":
            lines.append(f"　판정 근거: {payload.get('reason', '-')}")

        lines.append(f"　첫 종목 **{first_symbol}**")
        success = _webhook_send_to_url(webhook_url, "\n".join(lines))
        _log(
            "session_notification_sent" if success else "session_notification_failed",
            sid=payload.get("sid", "?"),
            first_symbol=first_symbol,
            geo_ok=bool(geo.get("geo_ok")),
            city=geo.get("city", "-"),
        )
    except Exception as exc:
        _log(
            "session_notification_thread_failed",
            sid=payload.get("sid", "?"),
            error=f"{type(exc).__name__}: {str(exc)[:220]}",
        )

def _parse_ip_token(value: str) -> str:
    """헤더의 IP 토큰(IPv4/IPv6, [IPv6]:port, ::ffff:IPv4)을 정규화한다."""
    token = str(value or "").strip().strip('"').strip("'")
    if not token or token.lower() in {"unknown", "null", "none", "-"}:
        return ""

    if token.lower().startswith("for="):
        token = token[4:].strip().strip('"').strip("'")

    if token.startswith("[") and "]" in token:
        token = token[1:token.index("]")]
    elif token.count(":") == 1 and "." in token:
        host, maybe_port = token.rsplit(":", 1)
        if maybe_port.isdigit():
            token = host

    try:
        parsed = ipaddress.ip_address(token)
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            parsed = parsed.ipv4_mapped
        return str(parsed)
    except ValueError:
        return ""


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _extract_real_client_ip(headers: dict[str, str], context_ip: str) -> tuple[str, str]:
    """
    Streamlit Cloud/reverse proxy 환경에서 실제 클라이언트 IP 후보를 찾는다.

    forwarding header는 위조될 수 있으므로 보안/인증에는 사용하지 않고
    방문 분석·GeoIP 표시용 best-effort 값으로만 사용한다.
    """
    for name in (
        "cf-connecting-ip",
        "true-client-ip",
        "x-real-ip",
        "fly-client-ip",
        "x-client-ip",
    ):
        value = _parse_ip_token(headers.get(name, ""))
        if value and _is_public_ip(value):
            return value, name

    xff = headers.get("x-forwarded-for", "")
    if xff:
        for part in xff.split(","):
            value = _parse_ip_token(part)
            if value and _is_public_ip(value):
                return value, "x-forwarded-for"

    forwarded = headers.get("forwarded", "")
    if forwarded:
        for group in forwarded.split(","):
            for part in group.split(";"):
                if part.strip().lower().startswith("for="):
                    value = _parse_ip_token(part.strip())
                    if value and _is_public_ip(value):
                        return value, "forwarded"

    direct = _parse_ip_token(context_ip)
    if direct and _is_public_ip(direct):
        return direct, "st.context.ip_address"

    return "", "unavailable"


def _client_snapshot(
    memory: _AnalyticsMemory,
    browser_info: Optional[dict] = None,
) -> dict:
    """
    현재 연결의 IP, 클라이언트 정보와 임시 지문을 만든다.

    Streamlit Community Cloud에서는 서버가 보는 연결 IP가 127.0.0.1일 수 있으므로,
    streamlit_app.py가 브라우저에서 직접 확인한 공인 IP를 전달하면 그것을 최우선으로 쓴다.
    브라우저 조회가 실패하거나 없는 경우에만 forwarding header/st.context로 폴백한다.
    """
    headers = _context_headers()
    browser = browser_info if isinstance(browser_info, dict) else {}

    context_ip = str(_safe_context_value("ip_address", "") or "").strip()
    fallback_ip, fallback_source = _extract_real_client_ip(headers, context_ip)

    browser_ip = _normalize_public_ip(str(browser.get("ip") or ""))
    if browser_ip:
        ip_address = browser_ip
        ip_source = str(browser.get("source") or "browser-ipify")
    else:
        ip_address = fallback_ip
        ip_source = fallback_source

    user_agent = str(
        browser.get("user_agent")
        or browser.get("userAgent")
        or headers.get("user-agent", "")
        or ""
    ).strip()
    timezone_name = str(
        browser.get("timezone")
        or _safe_context_value("timezone", "")
        or ""
    ).strip()
    locale = str(
        browser.get("locale")
        or browser.get("language")
        or _safe_context_value("locale", "")
        or ""
    ).strip()

    raw = "|".join((ip_address, user_agent, timezone_name, locale)).strip("|")
    fingerprint = ""
    if raw:
        fingerprint = hashlib.sha256(
            memory.fingerprint_salt + raw.encode("utf-8", errors="ignore")
        ).hexdigest()[:10]

    quality = sum(bool(x) for x in (ip_address, user_agent, timezone_name, locale))

    return {
        "fingerprint": fingerprint,
        "raw_ip": ip_address or "-",
        "ip_source": ip_source,
        "context_ip": _parse_ip_token(context_ip) or context_ip or "-",
        "masked_ip": _mask_ip(ip_address),
        "client": _client_family(user_agent),
        "timezone": timezone_name or "-",
        "locale": locale or "-",
        "fingerprint_quality": quality,
        "fingerprint_strong": bool(ip_address and user_agent),
        "bot_hint": _looks_like_bot(user_agent),
    }


def _prune_session_records(memory: _AnalyticsMemory, now_utc: datetime) -> None:
    """세션 판별용 메모리가 무한히 커지지 않게 오래된 기록을 제거한다."""
    keep_minutes = max(180, _reconnect_max_minutes() * 4)
    cutoff = now_utc - timedelta(minutes=keep_minutes)

    stale_sids = [
        sid for sid, record in memory.session_records.items()
        if record.get("started_at", now_utc) < cutoff
    ]
    for sid in stale_sids:
        memory.session_records.pop(sid, None)
        memory.engaged_sids.discard(sid)

    stale_clients = [
        fp for fp, record in memory.recent_clients.items()
        if record.get("started_at", now_utc) < cutoff
    ]
    for fp in stale_clients:
        memory.recent_clients.pop(fp, None)


def _memory_record_session(
    sid: str,
    browser_info: Optional[dict] = None,
) -> dict:
    """새 연결을 분류하고 서버 메모리 통계에 기록한다."""
    memory = _analytics_memory()
    now_utc = _utc_now()
    now_kst = now_utc.astimezone(KST)

    with memory.lock:
        _prune_session_records(memory, now_utc)
        client = _client_snapshot(memory, browser_info)
        fingerprint = client["fingerprint"]
        previous = memory.recent_clients.get(fingerprint) if fingerprint else None

        gap_minutes = None
        previous_engaged = None
        classification = "normal"
        reason = "새 연결 패턴"

        if client["bot_hint"]:
            classification = "bot_suspect"
            reason = "User-Agent가 자동화/봇 패턴과 유사"
        elif previous is not None:
            previous_engaged = bool(previous.get("engaged", False))
            gap_minutes = max(
                0.0,
                (now_utc - previous.get("started_at", now_utc)).total_seconds() / 60.0,
            )

            reconnect_window = (
                gap_minutes <= 2.0
                or _reconnect_min_minutes() <= gap_minutes <= _reconnect_max_minutes()
            )

            # 초기 화면만 보고 아무 상호작용도 없던 동일 클라이언트가
            # 10~25분(기본값) 뒤 새 세션을 만들면 WebSocket/탭 재연결 의심으로 본다.
            if (
                reconnect_window
                and not previous_engaged
                and client.get("fingerprint_strong", False)
            ):
                classification = "reconnect_suspect"
                reason = "동일 클라이언트 + 이전 세션 상호작용 없음"

        memory.session_count += 1
        memory.hourly_sessions[now_kst.hour] += 1

        if classification == "normal":
            memory.normal_session_count += 1
            memory.hourly_normal_sessions[now_kst.hour] += 1
        elif classification == "reconnect_suspect":
            memory.reconnect_session_count += 1
        else:
            memory.bot_session_count += 1

        record = {
            "sid": sid,
            "started_at": now_utc,
            "last_seen_at": now_utc,
            "classification": classification,
            "reason": reason,
            "fingerprint": fingerprint,
            "raw_ip": client.get("raw_ip", "-"),
            "ip_source": client.get("ip_source", "-"),
            "context_ip": client.get("context_ip", "-"),
            "masked_ip": client.get("masked_ip", "-"),
            "client": client["client"],
            "timezone": client["timezone"],
            "locale": client["locale"],
            "fingerprint_quality": client["fingerprint_quality"],
            "fingerprint_strong": client.get("fingerprint_strong", False),
            "previous_sid": previous.get("sid") if previous else None,
            "previous_engaged": previous_engaged,
            "gap_minutes": gap_minutes,
            "engaged": False,
            "engagement_actions": [],
            "symbol_events": 0,
            "first_symbol": None,
        }

        memory.session_records[sid] = record
        if fingerprint:
            memory.recent_clients[fingerprint] = record

        # 바깥 코드가 내부 dict를 수정하지 못하도록 필요한 값만 복사
        return {k: v for k, v in record.items() if k != "started_at" and k != "last_seen_at"}


def _memory_note_symbol(sid: str, symbol: str, order: int) -> None:
    memory = _analytics_memory()
    with memory.lock:
        record = memory.session_records.get(str(sid))
        if not record:
            return

        record["last_seen_at"] = _utc_now()
        record["symbol_events"] = int(record.get("symbol_events", 0)) + 1
        if not record.get("first_symbol"):
            record["first_symbol"] = symbol

        # 첫 종목은 앱 초기 렌더링만으로도 기록될 수 있다.
        # 두 번째 실제 종목 전환부터는 명확한 상호작용으로 본다.
        if order >= 2:
            _memory_mark_engaged_locked(memory, str(sid), f"symbol:{symbol}")


def _memory_mark_engaged_locked(memory: _AnalyticsMemory, sid: str, action: str) -> bool:
    record = memory.session_records.get(sid)
    if not record:
        return False

    record["last_seen_at"] = _utc_now()
    actions = record.setdefault("engagement_actions", [])
    if action and action not in actions:
        actions.append(action[:80])

    if record.get("engaged"):
        return False

    record["engaged"] = True
    memory.engaged_sids.add(sid)
    memory.engaged_session_count += 1
    return True


def mark_engagement(action: str = "interaction") -> None:
    """
    선택적으로 앱의 버튼/탭/슬라이더 callback에서 호출하면 실제 활동 세션 판별이 더 정확해진다.

    예:
        st.button("새로고침", on_click=lambda: mark_engagement("refresh_button"))

    analytics.py만 교체해도 종목을 두 번째로 전환하는 순간은 자동으로 활동 세션으로 표시된다.
    """
    if _is_operator():
        return
    sid = str(st.session_state.get("dashview_sid", ""))
    if not sid:
        return
    memory = _analytics_memory()
    with memory.lock:
        newly_engaged = _memory_mark_engaged_locked(memory, sid, str(action or "interaction"))

    if newly_engaged:
        _log("session_engaged", sid=sid, action=str(action or "interaction")[:80])


# ======================================================================================
# 날짜 변경 처리
# ======================================================================================

def _trim_history(
    memory: _AnalyticsMemory,
) -> None:
    keep = _chart_history_days()

    while len(memory.daily_sessions) > keep:
        memory.daily_sessions.popitem(last=False)

    while len(memory.daily_normal_sessions) > keep:
        memory.daily_normal_sessions.popitem(last=False)

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

        memory.daily_normal_sessions[
            previous_day
        ] = memory.normal_session_count

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

            "normal_sessions":
                memory.normal_session_count,

            "reconnect_suspects":
                memory.reconnect_session_count,

            "bot_suspects":
                memory.bot_session_count,

            "engaged_sessions":
                memory.engaged_session_count,

            "total_views":
                sum(memory.symbol_views.values()),

            "unique_symbols":
                len(memory.symbol_views),

            "symbol_views":
                dict(memory.symbol_views),

            "hourly_sessions":
                dict(memory.hourly_sessions),

            "hourly_normal_sessions":
                dict(memory.hourly_normal_sessions),

            "daily_sessions":
                list(memory.daily_sessions.items()),

            "daily_normal_sessions":
                list(memory.daily_normal_sessions.items()),

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
            normal_sessions=snapshot.get("normal_sessions", 0),
            reconnect_suspects=snapshot.get("reconnect_suspects", 0),
            bot_suspects=snapshot.get("bot_suspects", 0),
            engaged_sessions=snapshot.get("engaged_sessions", 0),
            views=snapshot["total_views"],
        )

        # --------------------------------------------------------------
        # 오늘 통계 초기화
        # --------------------------------------------------------------

        memory.current_day = today
        memory.session_count = 0
        memory.normal_session_count = 0
        memory.reconnect_session_count = 0
        memory.bot_session_count = 0
        memory.engaged_session_count = 0
        memory.engaged_sids = set()
        memory.symbol_views = Counter()
        memory.hourly_sessions = Counter()
        memory.hourly_normal_sessions = Counter()


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
        hourly_normal = snapshot.get(
            "hourly_normal_sessions",
            {},
        )

        hours = list(range(24))

        hour_values = [
            int(hourly.get(hour, hourly.get(str(hour), 0)))
            for hour in hours
        ]
        normal_hour_values = [
            int(hourly_normal.get(hour, hourly_normal.get(str(hour), 0)))
            for hour in hours
        ]

        ax2.bar(
            hours,
            hour_values,
            alpha=0.35,
            label="All connections",
        )
        ax2.plot(
            hours,
            normal_hour_values,
            marker="o",
            linewidth=1.5,
            label="Normal sessions",
        )

        ax2.set_title(
            "Connections by hour · KST"
        )
        ax2.legend(fontsize=8)

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
        daily_normal_map = dict(snapshot.get(
            "daily_normal_sessions",
            [],
        ))

        if daily:

            dates = []
            counts = []
            normal_counts = []

            for day_string, count in daily:
                try:
                    parsed = date.fromisoformat(day_string)
                    dates.append(parsed.strftime("%m/%d"))
                except Exception:
                    dates.append(str(day_string))

                counts.append(int(count))
                normal_counts.append(int(daily_normal_map.get(day_string, 0)))

            ax3.plot(
                dates,
                counts,
                marker="o",
                label="All connections",
            )
            ax3.plot(
                dates,
                normal_counts,
                marker="o",
                linestyle="--",
                label="Normal sessions",
            )
            ax3.legend(fontsize=8)

            for index, count in enumerate(counts):
                ax3.annotate(
                    str(count),
                    (index, count),
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
            "Daily connections · server-memory history"
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
            f"Connections {snapshot['sessions']}    "
            f"Normal {snapshot.get('normal_sessions', snapshot['sessions'])}    "
            f"Reconnect? {snapshot.get('reconnect_suspects', 0)}    "
            f"Bot? {snapshot.get('bot_suspects', 0)}    "
            f"Engaged {snapshot.get('engaged_sessions', 0)}\n"
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
            f"📊 **대시보드 일일 방문 통계** · `{snapshot['date']}` KST\n"
            f"전체 연결 **{snapshot['sessions']}회** · "
            f"일반 **{snapshot.get('normal_sessions', snapshot['sessions'])}회** · "
            f"재연결 의심 **{snapshot.get('reconnect_suspects', 0)}회** · "
            f"자동접속 의심 **{snapshot.get('bot_suspects', 0)}회** · "
            f"활동 확인 **{snapshot.get('engaged_sessions', 0)}회**\n"
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
    browser_info: Optional[dict] = None,
) -> str:

    # 운영자는 모든 방문 통계에서 제외
    if _is_operator():
        if "dashview_sid" not in st.session_state:
            st.session_state["dashview_sid"] = "OPERATOR"
            st.session_state["dashview_symbols"] = []
            st.session_state["dashview_symbol_history"] = []
            st.session_state["dashview_last_symbol"] = None

        return "OPERATOR"

    _daily_maintenance()

    if "dashview_sid" in st.session_state:
        return str(st.session_state["dashview_sid"])

    # ------------------------------------------------------------------
    # 새로운 Streamlit 연결
    # ------------------------------------------------------------------
    sid = uuid.uuid4().hex[:12]

    st.session_state["dashview_sid"] = sid
    st.session_state["dashview_symbols"] = []
    st.session_state["dashview_symbol_history"] = []
    st.session_state["dashview_last_symbol"] = None

    diagnostics = _memory_record_session(sid, browser_info)
    classification = diagnostics.get("classification", "normal")
    st.session_state["dashview_session_class"] = classification
    st.session_state["dashview_client_fp"] = diagnostics.get("fingerprint", "")

    _log(
        "session_start",
        sid=sid,
        version=app_version,
        classification=classification,
        client=diagnostics.get("client", "-"),
        raw_ip=diagnostics.get("raw_ip", "-"),
        ip_source=diagnostics.get("ip_source", "-"),
        browser_probe=bool(isinstance(browser_info, dict)),
        context_ip=diagnostics.get("context_ip", "-"),
        masked_ip=diagnostics.get("masked_ip", "-"),
        client_fp=diagnostics.get("fingerprint", ""),
        gap_minutes=diagnostics.get("gap_minutes"),
        previous_engaged=diagnostics.get("previous_engaged"),
        reason=diagnostics.get("reason", ""),
    )

    labels = {
        "normal": ("👤", "일반 세션"),
        "reconnect_suspect": ("🔁", "재연결 의심"),
        "bot_suspect": ("🤖", "자동접속 의심"),
    }
    icon, label = labels.get(classification, ("📈", "새 세션"))

    # 첫 화면은 절대 외부 GeoIP HTTP 요청을 기다리지 않는다.
    # 필요한 값만 보관하고 첫 종목이 정해진 뒤 daemon thread에서 GeoIP + Discord 전송을 처리한다.
    st.session_state["dashview_pending_session_payload"] = {
        "icon": icon,
        "label": label,
        "sid": sid,
        "kst_time": _kst_now(),
        "raw_ip": diagnostics.get("raw_ip") or "-",
        "ip_source": diagnostics.get("ip_source", "-"),
        "context_ip": diagnostics.get("context_ip", "-"),
        "client": diagnostics.get("client", "-"),
        "fingerprint": diagnostics.get("fingerprint", ""),
        "timezone": diagnostics.get("timezone", "-"),
        "locale": diagnostics.get("locale", "-"),
        "gap_minutes": diagnostics.get("gap_minutes"),
        "previous_engaged": diagnostics.get("previous_engaged"),
        "classification": classification,
        "reason": diagnostics.get("reason", ""),
        "diagnostics_enabled": _session_diagnostics_enabled(),
    }
    _log("session_notification_pending", sid=sid)

    return sid


# ======================================================================================
# 공개 API - Symbol
# ======================================================================================

def track_symbol(symbol: str) -> None:

    # 운영자의 종목 조회는 집계하지 않음
    if _is_operator():
        return

    if not symbol:
        return

    # 이하 기존 코드 그대로...

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

    _memory_note_symbol(
        str(sid),
        symbol,
        order,
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
    # 첫 종목은 세션 진단 메시지와 합쳐서 딱 1개의 Discord 메시지로 보낸다.
    # 이후 실제 종목 전환은 webhook_verbose=true일 때 별도 알림한다.
    if order == 1:
        pending_payload = st.session_state.pop(
            "dashview_pending_session_payload",
            None,
        )

        if pending_payload:
            # webhook URL은 Streamlit 실행 스레드에서 미리 읽고,
            # 백그라운드 스레드는 st.secrets/st.context에 접근하지 않는다.
            webhook_url = _webhook_url() or ""
            if webhook_url:
                worker = threading.Thread(
                    target=_send_first_session_notification_async,
                    args=(webhook_url, dict(pending_payload), str(symbol)),
                    daemon=True,
                    name=f"dashview-geo-{sid}",
                )
                worker.start()
                _log(
                    "session_notification_thread_started",
                    sid=sid,
                    first_symbol=symbol,
                )
            else:
                _log(
                    "session_notification_skipped",
                    sid=sid,
                    reason="no_webhook_url",
                )
        elif _webhook_verbose():
            # Hot reload 등으로 pending 상태가 사라진 예외 상황에서도
            # 첫 종목 자체는 놓치지 않는다.
            _webhook_send(
                f"　└ `{sid}` → **{symbol}** (첫 종목)"
            )

    elif _webhook_verbose():
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

        current_daily_normal = OrderedDict(
            memory.daily_normal_sessions
        )
        current_daily_normal[
            memory.current_day
        ] = memory.normal_session_count

        snapshot = {
            "date":
                memory.current_day,

            "sessions":
                memory.session_count,

            "normal_sessions":
                memory.normal_session_count,

            "reconnect_suspects":
                memory.reconnect_session_count,

            "bot_suspects":
                memory.bot_session_count,

            "engaged_sessions":
                memory.engaged_session_count,

            "total_views":
                sum(memory.symbol_views.values()),

            "unique_symbols":
                len(memory.symbol_views),

            "symbol_views":
                dict(memory.symbol_views),

            "hourly_sessions":
                dict(memory.hourly_sessions),

            "hourly_normal_sessions":
                dict(memory.hourly_normal_sessions),

            "daily_sessions":
                list(current_daily.items()),

            "daily_normal_sessions":
                list(current_daily_normal.items()),

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
            f"전체 연결 **{snapshot['sessions']}회** · "
            f"일반 **{snapshot.get('normal_sessions', 0)}회** · "
            f"재연결 의심 **{snapshot.get('reconnect_suspects', 0)}회** · "
            f"자동접속 의심 **{snapshot.get('bot_suspects', 0)}회** · "
            f"활동 확인 **{snapshot.get('engaged_sessions', 0)}회**\n"
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

    # 운영자는 통계에서 제외됐다는 것만 표시
    if _is_operator():
        st.caption(
            "🛠️ 운영자 모드 · 방문/종목 조회 통계에서 제외됨"
        )
        return

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
        today_normal = memory.normal_session_count
        today_reconnect = memory.reconnect_session_count
        today_bot = memory.bot_session_count
        today_engaged = memory.engaged_session_count
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
        f"오늘 연결 {today_sessions} · "
        f"일반 {today_normal} · 재연결의심 {today_reconnect} · "
        f"자동접속의심 {today_bot} · 활동확인 {today_engaged} · "
        f"오늘 전체 조회 {today_views} · "
        f"오늘 고유 종목 {today_unique} · "
        f"저장 {persisted}"
    )