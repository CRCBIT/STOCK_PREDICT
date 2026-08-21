"""
streamlit_app.py
================
Streamlit Cloud 용 **읽기 전용** 예측 대시보드 (다크).

토스 API 를 호출하지 않는다. `publish.py` 가 저장소에 올린 `published/` 스냅샷
(predictions.json 또는 predictions.csv)만 읽는다.

설계 원칙
--------
1. 화면당 질문 하나 — "이 종목이 h거래일 뒤 어디쯤에 있을까".
2. 캔들 차트가 중심. 과거는 캔들, 미래는 예측 분포를 같은 축에 이어 그린다.
3. 숫자보다 먼저 **판정 한 줄**을 보여준다. 이 시스템은 신뢰도 LOW 가 대부분이고,
   그 경우 중앙값을 방향성 근거로 쓰면 안 되기 때문이다.

로컬 확인:
    streamlit run streamlit_app.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parent
PUBLISHED = ROOT / "published"
STALE_HOURS = 36

DISCLAIMER = "통계 모델의 예측 분포이며 투자 조언이 아닙니다. 투자 판단의 책임은 이용자에게 있습니다."

# ---- 다크 팔레트 ---------------------------------------------------------------------
BG = "rgba(0,0,0,0)"
GRID = "rgba(255,255,255,0.06)"
TEXT = "#8b949e"
UP = "#f23645"        # 상승 (국내 관행: 빨강)
DOWN = "#2196f3"      # 하락
FCOL = "#f0b90b"      # 예측 (앰버)
DOT = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "⚪"}

st.set_page_config(page_title="주가 예측", page_icon="📈", layout="wide")

st.markdown("""
<style>
  #MainMenu, footer, header {visibility: hidden;}
  .block-container {padding-top: 1.6rem; padding-bottom: 2rem; max-width: 1400px;}
  [data-testid="stMetricValue"] {font-size: 1.35rem;}
  [data-testid="stMetricLabel"] {color: #8b949e; font-size: 0.8rem;}
  [data-testid="stMetricDelta"] {font-size: 0.9rem;}
  .stTabs [data-baseweb="tab-list"] {gap: 4px;}
  .verdict {border-left: 3px solid #30363d; padding: 6px 0 6px 12px;
            color: #c9d1d9; font-size: 0.95rem; margin: 4px 0 14px 0;}
</style>
""", unsafe_allow_html=True)


# ======================================================================================
# 데이터 로딩
# ======================================================================================
@st.cache_data(ttl=300, show_spinner=False)
def load_manifest() -> Optional[Dict]:
    path = PUBLISHED / "manifest.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    csv_path = PUBLISHED / "predictions.csv"
    if not csv_path.exists():
        return None
    mtime = datetime.fromtimestamp(csv_path.stat().st_mtime, tz=timezone.utc)
    return {"schema_version": "csv-only", "generated_at": mtime.isoformat(timespec="seconds")}


@st.cache_data(ttl=300, show_spinner=False)
def load_predictions() -> Optional[Dict]:
    """predictions.json 우선, 없으면 predictions.csv 로 대체."""
    jpath = PUBLISHED / "predictions.json"
    if jpath.exists():
        with open(jpath, "r", encoding="utf-8") as f:
            return json.load(f)
    cpath = PUBLISHED / "predictions.csv"
    if not cpath.exists():
        return None
    df = pd.read_csv(cpath)
    df = df.where(pd.notna(df), None)
    return {
        "schema_version": "csv-only", "generated_at": None,
        "predictions": df.to_dict(orient="records"),
        "backtests": {}, "diagnostics": {}, "source": "predictions.csv",
    }


@st.cache_data(ttl=30, show_spinner=False)
def load_quotes() -> Dict:
    """
    현재가 스냅샷(quotes.json). `quotes.py` 가 짧은 주기로 갱신해 올린다.
    없으면 빈 dict — 이 경우 예측 계산 시점의 가격을 그대로 쓴다.
    """
    path = PUBLISHED / "quotes.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def load_history(symbol: str) -> Optional[pd.DataFrame]:
    path = PUBLISHED / "history" / f"{symbol}.csv"
    if not path.exists():
        return None
    return pd.read_csv(path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)


@st.cache_data(ttl=300, show_spinner=False)
def load_backtest(symbol: str, horizon: int) -> Optional[pd.DataFrame]:
    path = PUBLISHED / "backtest" / f"backtest_{symbol}_h{horizon}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    for col in list(df.columns):
        if col.lower() in ("date", "index"):
            df = df.rename(columns={col: "date"})
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            break
    return df


# ======================================================================================
# 유틸
# ======================================================================================
def is_missing(v) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def num(v) -> Optional[float]:
    if is_missing(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def price(v, currency: str, unit: bool = True) -> str:
    f = num(v)
    if f is None:
        return "N/A"
    if currency == "KRW":
        return f"{f:,.0f}원" if unit else f"{f:,.0f}"
    return f"${f:,.2f}" if unit else f"{f:,.2f}"


def pct(v, signed: bool = True) -> str:
    f = num(v)
    if f is None:
        return "N/A"
    return f"{f * 100:+.2f}%" if signed else f"{f * 100:.1f}%"


def fnum(v, digits: int = 2) -> str:
    f = num(v)
    return "N/A" if f is None else f"{f:.{digits}f}"


def grade_of(p: Dict) -> str:
    return str(p.get("confidence_grade") or "LOW").upper()


def ret_of(p: Dict) -> Optional[float]:
    v = num(p.get("expected_return"))
    if v is not None:
        return v
    p50, now = num(p.get("p50")), num(p.get("current_price"))
    if p50 is not None and now:
        return p50 / now - 1.0
    return None


def reanchor(p: Dict, live_price: Optional[float]) -> Dict:
    """
    예측 분포를 최신 현재가 기준으로 다시 스케일한다.

    모델이 산출하는 것은 '현재가 대비 로그수익률의 분포' 이므로, 기준 가격이
    바뀌면 모든 분위수를 같은 비율로 옮기면 된다. 비율만 곱하는 것이지
    예측을 다시 계산하는 것이 아니다 (특징량은 여전히 마지막 확정 봉 기준).
    """
    anchor = num(p.get("current_price"))
    if live_price is None or anchor is None or anchor <= 0:
        return p
    ratio = live_price / anchor
    if not (0.5 < ratio < 2.0):          # 통화·종목 불일치 등 이상값 방어
        return p
    out = dict(p)
    for key in ("p10", "p25", "p50", "p75", "p90",
                "interval_80_low", "interval_80_high",
                "interval_90_low", "interval_90_high",
                "conservative_price", "optimistic_price", "target_1", "target_2",
                "stop_loss_reference", "add_buy_reference",
                "support_20d", "resistance_20d"):
        v = num(p.get(key))
        if v is not None:
            out[key] = v * ratio
    out["current_price"] = live_price
    out["_anchor_price"] = anchor
    out["_reanchored"] = True
    return out


def quote_age_label(fetched_at: Optional[str]) -> str:
    if not fetched_at:
        return ""
    try:
        ts = datetime.fromisoformat(str(fetched_at))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(fetched_at)
    mins = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    if mins < 1:
        return "방금"
    if mins < 60:
        return f"{mins:.0f}분 전"
    return f"{mins / 60:.1f}시간 전"


def snapshot_label(manifest: Dict) -> Tuple[str, bool]:
    gen = manifest.get("generated_at")
    if not gen:
        return "시각 정보 없음", False
    try:
        ts = datetime.fromisoformat(str(gen))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(gen), False
    age = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    return f"{ts.astimezone():%Y-%m-%d %H:%M} · {age:.0f}시간 전", age > STALE_HOURS


def verdict(p: Dict) -> str:
    """
    숫자를 어떻게 읽어야 하는지 한 줄로 말해준다.

    이 시스템은 대부분의 조합에서 신뢰도 LOW 로 떨어진다. 그 상태의 중앙값을
    방향성 근거로 쓰는 것이 가장 위험하므로, 화면 최상단에서 먼저 경고한다.
    """
    g = grade_of(p)
    shrink = num(p.get("shrinkage"))
    cov = num(p.get("coverage_80"))
    parts: List[str] = []

    if shrink is not None and shrink < 0.05:
        parts.append(
            "모델이 예측에 쓸 방향성 정보를 찾지 못해 **점 예측을 0으로 축소**했습니다. "
            "아래 분포는 사실상 과거 변동 범위이며, 방향 판단 근거가 아닙니다."
        )
    elif g == "LOW":
        parts.append(
            "신뢰도가 낮습니다. **중앙값(P50)을 방향 근거로 쓰지 마시고**, "
            "구간의 폭만 위험 크기 참고용으로 보십시오."
        )
    elif g == "MEDIUM":
        parts.append("참고 가능한 수준입니다. 다만 단독 근거로 삼기에는 부족합니다.")
    else:
        parts.append("상대적으로 신뢰도가 높은 구간입니다.")

    # 커버리지는 과소/과대를 구분해야 한다.
    # 80% 미만 = 구간이 좁아 실제 변동을 놓침(위험 과소평가), 초과 = 과도하게 보수적.
    if cov is not None:
        if cov < 0.68:
            parts.append(
                f"과거 검증에서 80% 구간이 실제로 {cov * 100:.0f}% 만 포함했습니다 — "
                "구간이 좁아 **위험을 과소평가**하고 있습니다."
            )
        elif cov > 0.92:
            parts.append(
                f"과거 검증 커버리지가 {cov * 100:.0f}% 로 목표보다 높습니다 — "
                "구간이 과도하게 넓어 보수적입니다."
            )
    return " ".join(parts)


# ======================================================================================
# 차트 — 캔들 + 예측 구간
# ======================================================================================
def candle_chart(hist: Optional[pd.DataFrame], p: Dict,
                 lookback: int, show_volume: bool) -> go.Figure:
    """과거 캔들과 미래 예측 분포를 같은 x축에 이어 그린다."""
    currency = p.get("currency", "KRW")
    rows = 2 if show_volume else 1
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True,
                        row_heights=[0.8, 0.2] if show_volume else [1.0],
                        vertical_spacing=0.02)

    last_date = None
    if hist is not None and not hist.empty and {"open", "high", "low", "close"} <= set(hist.columns):
        h = hist.tail(int(lookback))
        fig.add_trace(go.Candlestick(
            x=h["date"], open=h["open"], high=h["high"], low=h["low"], close=h["close"],
            increasing=dict(line=dict(color=UP, width=1), fillcolor=UP),
            decreasing=dict(line=dict(color=DOWN, width=1), fillcolor=DOWN),
            name="주가", showlegend=False,
        ), row=1, col=1)
        last_date = h["date"].iloc[-1]
        if show_volume and "volume" in h.columns:
            colors = [UP if c >= o else DOWN for o, c in zip(h["open"], h["close"])]
            fig.add_trace(go.Bar(
                x=h["date"], y=h["volume"], marker=dict(color=colors, opacity=0.3),
                showlegend=False, hoverinfo="skip",
            ), row=2, col=1)

    if last_date is None:
        last_date = pd.Timestamp.today().normalize()

    hz = int(p.get("horizon") or 0)
    now = num(p.get("current_price"))
    if hz and now:
        future = pd.bdate_range(last_date + pd.Timedelta(days=1), periods=hz)
        steps = len(future)
        if steps:
            scale = [((i + 1) / steps) ** 0.5 for i in range(steps)]
            fx = [last_date] + list(future)

            def cone(key: str) -> List[float]:
                v = num(p.get(key))
                return [] if v is None else [now] + [now + (v - now) * s for s in scale]

            c10, c25, c50, c75, c90 = (cone(k) for k in ("p10", "p25", "p50", "p75", "p90"))
            if c10 and c90:
                fig.add_trace(go.Scatter(
                    x=fx + fx[::-1], y=c90 + c10[::-1], fill="toself",
                    fillcolor="rgba(240,185,11,0.10)", line=dict(width=0),
                    name="80%", hoverinfo="skip"), row=1, col=1)
            if c25 and c75:
                fig.add_trace(go.Scatter(
                    x=fx + fx[::-1], y=c75 + c25[::-1], fill="toself",
                    fillcolor="rgba(240,185,11,0.22)", line=dict(width=0),
                    name="50%", hoverinfo="skip"), row=1, col=1)
            if c50:
                fig.add_trace(go.Scatter(
                    x=fx, y=c50, mode="lines", name="P50",
                    line=dict(color=FCOL, width=1.8, dash="dot"),
                    hovertemplate="%{x|%m/%d} · %{y:,.0f}<extra></extra>"), row=1, col=1)
                fig.add_annotation(x=fx[-1], y=c50[-1], text=f" {price(c50[-1], currency, False)}",
                                   showarrow=False, xanchor="left",
                                   font=dict(color=FCOL, size=12), row=1, col=1)
            fig.add_vline(x=last_date,
                          line=dict(color="rgba(255,255,255,0.22)", width=1, dash="dot"))

    fig.update_layout(
        template="plotly_dark", height=500 if show_volume else 430,
        margin=dict(l=8, r=70, t=8, b=8), paper_bgcolor=BG, plot_bgcolor=BG,
        font=dict(color=TEXT, size=12), hovermode="x unified",
        xaxis_rangeslider_visible=False, showlegend=False, bargap=0.1,
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID,
                     rangebreaks=[dict(bounds=["sat", "mon"])])
    fig.update_yaxes(showgrid=True, gridcolor=GRID, linecolor=GRID, side="right",
                     row=1, col=1)
    if show_volume:
        fig.update_yaxes(showgrid=False, showticklabels=False, row=2, col=1)
    return fig


def equity_chart(bt: pd.DataFrame) -> Optional[go.Figure]:
    if bt is None or bt.empty:
        return None
    ycol = next((c for c in ["equity", "strategy_equity", "cum_return", "nav"]
                 if c in bt.columns), None)
    if ycol is None:
        numeric = [c for c in bt.columns if pd.api.types.is_numeric_dtype(bt[c])]
        if not numeric:
            return None
        ycol = numeric[0]
    xcol = "date" if "date" in bt.columns else bt.columns[0]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=bt[xcol], y=bt[ycol], mode="lines", name="전략",
                             line=dict(color=FCOL, width=1.6)))
    bh = next((c for c in ["buy_hold", "bh_equity", "benchmark"] if c in bt.columns), None)
    if bh:
        fig.add_trace(go.Scatter(x=bt[xcol], y=bt[bh], mode="lines", name="Buy & Hold",
                                 line=dict(color="#6e7681", width=1.3, dash="dot")))
    fig.update_layout(template="plotly_dark", height=250,
                      margin=dict(l=8, r=8, t=8, b=8), paper_bgcolor=BG, plot_bgcolor=BG,
                      font=dict(color=TEXT, size=11),
                      legend=dict(orientation="h", y=1.18, bgcolor=BG))
    fig.update_xaxes(showgrid=False, linecolor=GRID)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, linecolor=GRID)
    return fig


# ======================================================================================
# 종목 화면
# ======================================================================================
def render_symbol(symbol: str, sub: pd.DataFrame, payload: Dict,
                  quotes: Optional[Dict] = None) -> None:
    horizons = sorted(int(h) for h in sub["horizon"].unique())

    c_h, c_lb, c_vol = st.columns([3, 2, 1])
    with c_h:
        horizon = st.radio("예측 기간", horizons, horizontal=True, key=f"h_{symbol}",
                           format_func=lambda h: f"{h}일", label_visibility="collapsed")
    with c_lb:
        lookback = st.select_slider("과거", options=[60, 120, 250, 400], value=120,
                                    key=f"lb_{symbol}", format_func=lambda v: f"{v}일",
                                    label_visibility="collapsed")
    with c_vol:
        show_volume = st.checkbox("거래량", value=True, key=f"v_{symbol}")

    row = sub[sub["horizon"] == horizon]
    if row.empty:
        st.warning("해당 기간의 예측이 없습니다.")
        return
    # Streamlit 은 요소 id 를 인자 조합으로 계산하므로, 탭마다 같은 모양의 차트/표를
    # 그리면 id 가 충돌한다(StreamlitDuplicateElementId). 종목·기간으로 키를 준다.
    uid = f"{symbol}_{horizon}"
    p = row.iloc[0].to_dict()
    quotes = quotes or {}
    live = num((quotes.get("quotes") or {}).get(symbol, {}).get("price"))
    p = reanchor(p, live)
    currency = p.get("currency", "KRW")
    now = num(p.get("current_price"))

    # ---- 결론 한 줄 ----
    st.markdown(
        f"<div class='verdict'>{DOT.get(grade_of(p), '⚪')} "
        f"<b>신뢰도 {fnum(p.get('confidence'), 0)}/100 · {grade_of(p)}</b> — {verdict(p)}</div>",
        unsafe_allow_html=True,
    )

    # ---- 차트 ----
    st.plotly_chart(candle_chart(load_history(symbol), p, lookback, show_volume),
                    use_container_width=True, key=f"candle_{uid}")

    # ---- 핵심 수치 ----
    m = st.columns(5)
    if p.get("_reanchored"):
        anchor = num(p.get("_anchor_price"))
        delta = pct(now / anchor - 1.0) if (anchor and now) else None
        m[0].metric(f"현재가 · {quote_age_label(quotes.get('fetched_at'))}",
                    price(now, currency), delta)
    else:
        m[0].metric("현재가", price(now, currency))
    m[1].metric(f"{horizon}일 후 P50", price(p.get("p50"), currency), pct(ret_of(p)))
    m[2].metric("P10 ~ P90",
                f"{price(p.get('p10'), currency, False)} ~ {price(p.get('p90'), currency, False)}")
    m[3].metric("상승 확률", pct(p.get("prob_up"), signed=False))
    m[4].metric("변동성(연율)", pct(p.get("expected_volatility_annual"), signed=False))

    # ---- 접힌 상세 ----
    with st.expander("분위수 · 참고 레벨"):
        left, right = st.columns(2)
        with left:
            rows = []
            for key, lab in [("p90", "P90"), ("p75", "P75"), ("p50", "P50"),
                             ("p25", "P25"), ("p10", "P10")]:
                v = num(p.get(key))
                chg = (v / now - 1.0) if (v is not None and now) else None
                rows.append({"구간": lab, "가격": price(v, currency), "현재가 대비": pct(chg)})
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                         key=f"quant_{uid}")
        with right:
            lv = [("2차 목표", "target_2"), ("1차 목표", "target_1"),
                  ("추가매수 고려", "add_buy_reference"), ("손절 고려", "stop_loss_reference")]
            st.dataframe(
                pd.DataFrame([{"항목": k, "가격": price(p.get(v), currency)} for k, v in lv]),
                hide_index=True, use_container_width=True, key=f"levels_{uid}")
            st.caption(
                f"R/R {fnum(p.get('risk_reward'))} · ATR {pct(p.get('atr_pct'), signed=False)} · "
                f"지지 {price(p.get('support_20d'), currency, False)} / "
                f"저항 {price(p.get('resistance_20d'), currency, False)}"
            )
        st.caption("참고용 레벨이며 투자 조언이 아닙니다.")

    with st.expander("모델 진단"):
        d1, d2 = st.columns(2)
        with d1:
            st.dataframe(pd.DataFrame({
                "지표": ["IC (Spearman)", "방향 정확도", "RMSE", "baseline RMSE",
                         "80% 구간 실측 커버리지"],
                "값": [fnum(p.get("oos_ic"), 3),
                       pct(p.get("oos_directional_accuracy"), signed=False),
                       fnum(p.get("oos_rmse"), 4), fnum(p.get("baseline_rmse"), 4),
                       pct(p.get("coverage_80"), signed=False)],
            }), hide_index=True, use_container_width=True, key=f"diag_{uid}")
        with d2:
            info = [("선택된 모델", str(p.get("model_weights") or p.get("models") or "-")),
                    ("Fallback level", str(p.get("fallback_level"))),
                    ("마지막 데이터", str(p.get("last_data_time"))),
                    ("학습 시각", str(p.get("trained_at")))]
            sh = num(p.get("shrinkage"))
            if sh is not None and sh < 0.999:
                info.insert(1, ("과대외삽 보정", f"x{sh:.2f}"))
            if p.get("missing_data"):
                info.append(("누락 데이터", str(p.get("missing_data"))))
            st.dataframe(pd.DataFrame(info, columns=["항목", "값"]),
                         hide_index=True, use_container_width=True, key=f"meta_{uid}")
        if p.get("regime"):
            st.caption(f"시장 regime · {p.get('regime')}")
        if p.get("notes"):
            for n in str(p.get("notes")).split(" | "):
                if n.strip():
                    st.caption(f"· {n.strip()}")

    bt_meta = (payload.get("backtests") or {}).get(f"{symbol}_h{horizon}")
    bt_df = load_backtest(symbol, horizon)
    if bt_meta or bt_df is not None:
        with st.expander("백테스트 (Out-of-Sample)"):
            if bt_meta:
                mm = bt_meta.get("metrics") or {}
                bb = bt_meta.get("buy_hold") or {}
                c = st.columns(4)
                c[0].metric("Sharpe", fnum(mm.get("sharpe")))
                c[1].metric("연환산 수익", pct(mm.get("annual_return")))
                c[2].metric("MDD", pct(mm.get("max_drawdown")))
                c[3].metric("B&H Sharpe", fnum(bb.get("sharpe")))
            if bt_df is not None:
                fig = equity_chart(bt_df)
                if fig is not None:
                    st.plotly_chart(fig, use_container_width=True, key=f"equity_{uid}")
            st.caption("상승장에서는 타이밍 전략이 단순 보유를 이기기 어렵습니다.")


# ======================================================================================
# 본문
# ======================================================================================
def main() -> None:
    manifest = load_manifest()
    payload = load_predictions()
    if manifest is None or payload is None:
        st.error(
            "`published/` 에서 읽을 파일이 없습니다. "
            "`predictions.json` 또는 `predictions.csv` 가 필요합니다.\n\n"
            "```bash\npython main.py\npython publish.py\n```"
        )
        st.stop()

    preds: List[Dict] = payload.get("predictions") or []
    if not preds:
        st.warning("예측 결과가 비어 있습니다.")
        st.stop()

    df = pd.DataFrame(preds)
    df["symbol"] = df["symbol"].astype(str)
    symbols = sorted(df["symbol"].unique())
    label, stale = snapshot_label(manifest)

    top_l, top_r = st.columns([3, 2])
    with top_l:
        st.markdown("## 📈 주가 예측")
    with top_r:
        st.markdown(
            f"<div style='text-align:right;padding-top:18px;color:#8b949e;font-size:0.85rem'>"
            f"스냅샷 {label} · 종목 {len(symbols)} · 예측 {len(preds)}건</div>",
            unsafe_allow_html=True,
        )

    if stale:
        st.error(f"이 스냅샷은 {label} 결과입니다. 로컬에서 다시 실행 후 게시하세요.")
    if payload.get("source") == "predictions.csv":
        st.info("CSV 만으로 구동 중 · 백테스트와 진단은 publish.py 게시 시 표시됩니다.")

    quotes = load_quotes()
    if quotes.get("fetched_at"):
        st.caption(
            f"💹 현재가 {quote_age_label(quotes['fetched_at'])} 갱신 · "
            "예측 분포는 이 가격 기준으로 재조정되어 표시됩니다 "
            "(모델 입력은 마지막 확정 봉 기준)."
        )

    names = [str(df[df["symbol"] == s]["name"].iloc[0]) for s in symbols]
    for tab, symbol, name in zip(st.tabs(names), symbols, names):
        with tab:
            render_symbol(symbol, df[df["symbol"] == symbol], payload, quotes)

    st.divider()
    st.caption(DISCLAIMER)


if __name__ == "__main__":
    main()