"""
streamlit_app.py
================
Streamlit Cloud 용 **읽기 전용** 예측 대시보드 (다크 테마).

토스 API 를 호출하지 않는다. `publish.py` 가 저장소에 올린 `published/` 스냅샷
(predictions.json 또는 predictions.csv)만 읽는다.

화면 구성은 캔들 차트 하나를 중심으로 한다.
과거 구간은 캔들, 미래 구간은 예측 분포(P10~P90) 음영으로 같은 축에 이어 그린다.

로컬 확인:
    streamlit run streamlit_app.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parent
PUBLISHED = ROOT / "published"
STALE_HOURS = 36

DISCLAIMER = "통계 모델의 예측 분포이며 투자 조언이 아닙니다. 투자 판단의 책임은 이용자에게 있습니다."

# ---- 다크 팔레트 -----------------------------------------------------------------
BG = "rgba(0,0,0,0)"
GRID = "rgba(255,255,255,0.06)"
TEXT = "#8b949e"
UP = "#f23645"        # 상승 (국내 관행: 빨강)
DOWN = "#2196f3"      # 하락 (파랑)
FORECAST = "#f0b90b"  # 예측 (앰버)
GRADE_DOT = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "⚪"}

st.set_page_config(page_title="주가 예측", page_icon="📈", layout="wide")

st.markdown("""
<style>
  #MainMenu, footer {visibility: hidden;}
  .block-container {padding-top: 2.2rem; padding-bottom: 2rem;}
  [data-testid="stMetricValue"] {font-size: 1.5rem;}
  [data-testid="stMetricLabel"] {color: #8b949e;}
  hr {margin: 0.8rem 0;}
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
    df = pd.read_csv(cpath).where(lambda d: d.notna(), None)
    return {
        "schema_version": "csv-only", "generated_at": None,
        "predictions": df.to_dict(orient="records"),
        "backtests": {}, "diagnostics": {}, "source": "predictions.csv",
    }


@st.cache_data(ttl=300, show_spinner=False)
def load_history(symbol: str) -> Optional[pd.DataFrame]:
    path = PUBLISHED / "history" / f"{symbol}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["date"])
    return df.sort_values("date").reset_index(drop=True)


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


def fmt_price(v, currency: str) -> str:
    if is_missing(v):
        return "N/A"
    return f"{float(v):,.0f}" if currency == "KRW" else f"{float(v):,.2f}"


def fmt_price_unit(v, currency: str) -> str:
    if is_missing(v):
        return "N/A"
    return f"{float(v):,.0f}원" if currency == "KRW" else f"${float(v):,.2f}"


def fmt_pct(v, signed: bool = True) -> str:
    if is_missing(v):
        return "N/A"
    return f"{float(v) * 100:+.2f}%" if signed else f"{float(v) * 100:.1f}%"


def fmt_num(v, digits: int = 2) -> str:
    if is_missing(v):
        return "N/A"
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "N/A"


def grade_of(pred: Dict) -> str:
    return str(pred.get("confidence_grade") or "LOW").upper()


def ret_of(pred: Dict) -> Optional[float]:
    v = pred.get("expected_return")
    if not is_missing(v):
        return float(v)
    p50, now = pred.get("p50"), pred.get("current_price")
    if not is_missing(p50) and not is_missing(now) and float(now) > 0:
        return float(p50) / float(now) - 1.0
    return None


def snapshot_label(manifest: Dict) -> tuple[str, bool]:
    """(표시 문자열, 오래되었는지)"""
    gen = manifest.get("generated_at")
    if not gen:
        return "시각 정보 없음", False
    try:
        ts = datetime.fromisoformat(str(gen))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return str(gen), False
    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    return f"{ts.astimezone():%Y-%m-%d %H:%M} · {age_h:.0f}시간 전", age_h > STALE_HOURS


# ======================================================================================
# 메인 차트 — 캔들 + 예측 구간
# ======================================================================================
def candle_forecast_chart(hist: Optional[pd.DataFrame], pred: Dict,
                          lookback: int = 120, show_volume: bool = True) -> go.Figure:
    """
    좌: 과거 캔들 / 우: 미래 h거래일 예측 분포를 같은 x축에 이어 그린다.
    분포의 폭은 √t 로 보간한 시각적 근사다 (모델은 h일 후 시점 분포만 산출).
    """
    currency = pred.get("currency", "KRW")
    rows = 2 if show_volume else 1
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True,
                        row_heights=[0.78, 0.22] if show_volume else [1.0],
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
            vcolor = [UP if c >= o else DOWN for o, c in zip(h["open"], h["close"])]
            fig.add_trace(go.Bar(
                x=h["date"], y=h["volume"], marker=dict(color=vcolor, opacity=0.35),
                name="거래량", showlegend=False, hoverinfo="skip",
            ), row=2, col=1)

    if last_date is None:
        last_date = pd.Timestamp.today().normalize()

    hz = int(pred.get("horizon") or 0)
    now = pred.get("current_price")
    if hz and not is_missing(now):
        now = float(now)
        future = pd.bdate_range(last_date + pd.Timedelta(days=1), periods=hz)
        steps = len(future)
        if steps:
            scale = [((i + 1) / steps) ** 0.5 for i in range(steps)]
            fx = [last_date] + list(future)

            def cone(key: str) -> List[float]:
                v = pred.get(key)
                if is_missing(v):
                    return []
                return [now] + [now + (float(v) - now) * s for s in scale]

            p10, p25, p50, p75, p90 = (cone(k) for k in ("p10", "p25", "p50", "p75", "p90"))

            if p10 and p90:
                fig.add_trace(go.Scatter(
                    x=fx + fx[::-1], y=p90 + p10[::-1], fill="toself",
                    fillcolor="rgba(240,185,11,0.10)", line=dict(width=0),
                    name="80% 구간", hoverinfo="skip",
                ), row=1, col=1)
            if p25 and p75:
                fig.add_trace(go.Scatter(
                    x=fx + fx[::-1], y=p75 + p25[::-1], fill="toself",
                    fillcolor="rgba(240,185,11,0.22)", line=dict(width=0),
                    name="50% 구간", hoverinfo="skip",
                ), row=1, col=1)
            if p50:
                fig.add_trace(go.Scatter(
                    x=fx, y=p50, mode="lines", name="예측 중앙(P50)",
                    line=dict(color=FORECAST, width=1.8, dash="dot"),
                    hovertemplate="%{x|%m/%d}<br>P50 %{y:,.0f}<extra></extra>",
                ), row=1, col=1)
                fig.add_annotation(
                    x=fx[-1], y=p50[-1], text=f" {fmt_price(p50[-1], currency)}",
                    showarrow=False, xanchor="left", font=dict(color=FORECAST, size=12),
                    row=1, col=1,
                )
            fig.add_vline(x=last_date, line=dict(color="rgba(255,255,255,0.25)",
                                                 width=1, dash="dot"))

    fig.update_layout(
        template="plotly_dark", height=520 if show_volume else 440,
        margin=dict(l=8, r=64, t=10, b=8),
        paper_bgcolor=BG, plot_bgcolor=BG,
        font=dict(color=TEXT, size=12),
        hovermode="x unified", xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", y=1.06, x=0, bgcolor=BG,
                    font=dict(color=TEXT, size=11)),
        bargap=0.1,
    )
    fig.update_xaxes(showgrid=False, rangebreaks=[dict(bounds=["sat", "mon"])],
                     linecolor=GRID)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, linecolor=GRID,
                     side="right", row=1, col=1)
    if show_volume:
        fig.update_yaxes(showgrid=False, showticklabels=False, row=2, col=1)
    return fig


def equity_chart(bt: pd.DataFrame) -> Optional[go.Figure]:
    if bt is None or bt.empty:
        return None
    ycol = next((c for c in ["equity", "strategy_equity", "cum_return", "nav"]
                 if c in bt.columns), None)
    if ycol is None:
        num = [c for c in bt.columns if pd.api.types.is_numeric_dtype(bt[c])]
        if not num:
            return None
        ycol = num[0]
    xcol = "date" if "date" in bt.columns else bt.columns[0]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=bt[xcol], y=bt[ycol], mode="lines", name="전략",
                             line=dict(color=FORECAST, width=1.6)))
    bh = next((c for c in ["buy_hold", "bh_equity", "benchmark"] if c in bt.columns), None)
    if bh:
        fig.add_trace(go.Scatter(x=bt[xcol], y=bt[bh], mode="lines", name="Buy & Hold",
                                 line=dict(color="#6e7681", width=1.3, dash="dot")))
    fig.update_layout(template="plotly_dark", height=260,
                      margin=dict(l=8, r=8, t=8, b=8),
                      paper_bgcolor=BG, plot_bgcolor=BG, font=dict(color=TEXT, size=11),
                      legend=dict(orientation="h", y=1.15, bgcolor=BG))
    fig.update_xaxes(showgrid=False, linecolor=GRID)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, linecolor=GRID)
    return fig


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
    symbols = sorted(df["symbol"].astype(str).unique())
    name_of = {s: str(df[df["symbol"].astype(str) == s]["name"].iloc[0]) for s in symbols}

    # ---------------- 사이드바 ----------------
    with st.sidebar:
        st.markdown("### 종목")
        symbol = st.selectbox("종목", symbols, label_visibility="collapsed",
                              format_func=lambda s: f"{name_of.get(s, s)}  ·  {s}")
        sub = df[df["symbol"].astype(str) == symbol]
        horizons = sorted(int(h) for h in sub["horizon"].unique())

        st.markdown("### 예측 기간")
        horizon = st.radio("예측 기간", horizons, label_visibility="collapsed",
                           format_func=lambda h: f"{h} 거래일", horizontal=True)

        st.markdown("### 차트")
        lookback = st.select_slider("과거 구간", options=[60, 120, 250, 400], value=120,
                                    format_func=lambda v: f"{v}일")
        show_volume = st.checkbox("거래량 표시", value=True)

        st.divider()
        label, stale = snapshot_label(manifest)
        st.caption(f"스냅샷 {label}")
        st.caption(f"종목 {len(symbols)} · 예측 {len(preds)}건")

    row = sub[sub["horizon"] == horizon]
    if row.empty:
        st.warning("해당 조합의 예측이 없습니다.")
        st.stop()
    pred = row.iloc[0].to_dict()
    currency = pred.get("currency", "KRW")
    now = pred.get("current_price")

    # ---------------- 헤더 ----------------
    label, stale = snapshot_label(manifest)
    if stale:
        st.error(f"이 스냅샷은 {label} 결과입니다. 로컬에서 다시 실행 후 게시하세요.")
    if payload.get("source") == "predictions.csv":
        st.info("CSV 만으로 구동 중 · 백테스트와 진단은 publish.py 게시 시 표시됩니다.")

    head_l, head_r = st.columns([4, 2])
    with head_l:
        st.markdown(f"## {pred.get('name', symbol)}")
        st.caption(f"{symbol} · {pred.get('country', '')} · {horizon}거래일 후 예측")
    with head_r:
        st.markdown(
            f"<div style='text-align:right;padding-top:14px'>"
            f"<span style='color:#8b949e;font-size:0.85rem'>신뢰도</span><br>"
            f"<span style='font-size:1.35rem'>{GRADE_DOT.get(grade_of(pred), '⚪')} "
            f"{fmt_num(pred.get('confidence'), 0)}"
            f"<span style='color:#8b949e;font-size:0.9rem'> / 100 · {grade_of(pred)}</span>"
            f"</span></div>",
            unsafe_allow_html=True,
        )

    # ---------------- 메인 차트 ----------------
    st.plotly_chart(
        candle_forecast_chart(load_history(symbol), pred, lookback, show_volume),
        use_container_width=True,
    )

    # ---------------- 핵심 수치 ----------------
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("현재가", fmt_price_unit(now, currency))
    m2.metric("예측 중앙 P50", fmt_price_unit(pred.get("p50"), currency), fmt_pct(ret_of(pred)))
    m3.metric("예측 범위 P10~P90",
              f"{fmt_price(pred.get('p10'), currency)} ~ {fmt_price(pred.get('p90'), currency)}")
    m4.metric("상승 확률", fmt_pct(pred.get("prob_up"), signed=False))
    m5.metric("변동성 (연율)", fmt_pct(pred.get("expected_volatility_annual"), signed=False))

    if grade_of(pred) == "LOW":
        st.caption("⚪ 신뢰도가 낮은 예측입니다. 중앙값보다 구간의 폭을 근거로 삼으십시오.")

    st.divider()

    # ---------------- 분위수 · 레벨 ----------------
    left, right = st.columns(2)
    with left:
        st.markdown("###### 분위수")
        q_rows = []
        for key, label_q in [("p90", "P90"), ("p75", "P75"), ("p50", "P50"),
                             ("p25", "P25"), ("p10", "P10")]:
            v = pred.get(key)
            chg = (float(v) / float(now) - 1.0) if (
                not is_missing(v) and not is_missing(now) and float(now) > 0) else None
            q_rows.append({"구간": label_q, "가격": fmt_price_unit(v, currency),
                           "현재가 대비": fmt_pct(chg)})
        st.dataframe(pd.DataFrame(q_rows), hide_index=True, use_container_width=True)

    with right:
        st.markdown("###### 참고 레벨 · 투자 조언 아님")
        lv = [
            {"항목": "2차 목표", "가격": fmt_price_unit(pred.get("target_2"), currency)},
            {"항목": "1차 목표", "가격": fmt_price_unit(pred.get("target_1"), currency)},
            {"항목": "추가매수 고려", "가격": fmt_price_unit(pred.get("add_buy_reference"), currency)},
            {"항목": "손절 고려", "가격": fmt_price_unit(pred.get("stop_loss_reference"), currency)},
        ]
        st.dataframe(pd.DataFrame(lv), hide_index=True, use_container_width=True)
        st.caption(
            f"R/R {fmt_num(pred.get('risk_reward'))} · "
            f"ATR {fmt_pct(pred.get('atr_pct'), signed=False)} · "
            f"지지 {fmt_price(pred.get('support_20d'), currency)} / "
            f"저항 {fmt_price(pred.get('resistance_20d'), currency)}"
        )

    # ---------------- 접힌 영역 ----------------
    with st.expander("모델 진단"):
        d1, d2 = st.columns(2)
        with d1:
            st.dataframe(pd.DataFrame({
                "지표": ["IC (Spearman)", "방향 정확도", "RMSE", "baseline RMSE",
                         "80% 구간 실측 커버리지"],
                "값": [
                    fmt_num(pred.get("oos_ic"), 3),
                    fmt_pct(pred.get("oos_directional_accuracy"), signed=False),
                    fmt_num(pred.get("oos_rmse"), 4),
                    fmt_num(pred.get("baseline_rmse"), 4),
                    fmt_pct(pred.get("coverage_80"), signed=False),
                ],
            }), hide_index=True, use_container_width=True)
            st.caption("커버리지가 80%에서 크게 벗어나면 구간 추정을 믿기 어렵습니다.")
        with d2:
            info = [
                ("선택된 모델", str(pred.get("model_weights") or pred.get("models") or "-")),
                ("Fallback level", str(pred.get("fallback_level"))),
                ("마지막 데이터", str(pred.get("last_data_time"))),
                ("학습 시각", str(pred.get("trained_at"))),
            ]
            shrink = pred.get("shrinkage")
            if not is_missing(shrink) and float(shrink) < 0.999:
                info.insert(1, ("과대외삽 보정", f"x{float(shrink):.2f}"))
            if pred.get("missing_data"):
                info.append(("누락 데이터", str(pred.get("missing_data"))))
            st.dataframe(pd.DataFrame(info, columns=["항목", "값"]),
                         hide_index=True, use_container_width=True)
        if pred.get("regime"):
            st.caption(f"시장 regime · {pred.get('regime')}")
        if pred.get("notes"):
            for n in str(pred.get("notes")).split(" | "):
                if n.strip():
                    st.caption(f"· {n.strip()}")

    bt_meta = (payload.get("backtests") or {}).get(f"{symbol}_h{horizon}")
    bt_df = load_backtest(symbol, horizon)
    if bt_meta or bt_df is not None:
        with st.expander("백테스트 (Out-of-Sample)"):
            if bt_meta:
                m = bt_meta.get("metrics") or {}
                b = bt_meta.get("buy_hold") or {}
                c = st.columns(4)
                c[0].metric("Sharpe", fmt_num(m.get("sharpe")))
                c[1].metric("연환산 수익", fmt_pct(m.get("annual_return")))
                c[2].metric("MDD", fmt_pct(m.get("max_drawdown")))
                c[3].metric("B&H Sharpe", fmt_num(b.get("sharpe")))
            if bt_df is not None:
                fig = equity_chart(bt_df)
                if fig is not None:
                    st.plotly_chart(fig, use_container_width=True)
            st.caption("상승장에서는 타이밍 전략이 단순 보유를 이기기 어렵습니다.")

    with st.expander("전체 종목 요약"):
        rows = []
        for p in preds:
            cur = p.get("currency", "KRW")
            rows.append({
                "종목": f"{p.get('name', p.get('symbol'))}",
                "기간": f"{int(p.get('horizon', 0))}일",
                "현재가": fmt_price_unit(p.get("current_price"), cur),
                "P50": fmt_price_unit(p.get("p50"), cur),
                "예상": fmt_pct(ret_of(p)),
                "P10~P90": f"{fmt_price(p.get('p10'), cur)} ~ {fmt_price(p.get('p90'), cur)}",
                "상승확률": fmt_pct(p.get("prob_up"), signed=False),
                "신뢰도": f"{GRADE_DOT.get(grade_of(p), '⚪')} {fmt_num(p.get('confidence'), 0)}",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.caption(DISCLAIMER)


if __name__ == "__main__":
    main()