"""
streamlit_app.py
================
Streamlit Cloud 용 **읽기 전용** 예측 대시보드.

토스 API 를 호출하지 않는다. `publish.py` 가 저장소에 올린 `published/` 스냅샷
(predictions.json 또는 predictions.csv)만 읽는다.

구성
----
    전체 요약 탭 : 모든 종목·기간의 예측을 한 화면에서 비교
    종목 상세 탭 : 선택한 종목의 분포·가격 경로·진단·백테스트

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

ROOT = Path(__file__).resolve().parent
PUBLISHED = ROOT / "published"
STALE_HOURS = 36

DISCLAIMER = (
    "본 대시보드의 모든 수치는 통계 모델의 예측 분포이며 투자 조언이 아닙니다. "
    "실제 투자 판단과 그 결과에 대한 책임은 이용자 본인에게 있습니다."
)

# 신뢰도 등급별 (아이콘, 색)
GRADE_STYLE = {
    "HIGH": ("🟢", "#2ca02c"),
    "MEDIUM": ("🟡", "#ff9f1c"),
    "LOW": ("🔴", "#9aa0a6"),
}

st.set_page_config(page_title="주가 예측 대시보드", page_icon="📈", layout="wide")


# ======================================================================================
# 데이터 로딩
# ======================================================================================
@st.cache_data(ttl=300, show_spinner=False)
def load_manifest() -> Optional[Dict]:
    """manifest.json 이 없으면 CSV 수정 시각으로 최소 manifest 를 합성한다."""
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
    """
    predictions.json 우선, 없으면 predictions.csv 로 대체한다.
    CSV 만 올려도 대시보드가 동작해야 하기 때문이다.
    """
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
        "schema_version": "csv-only",
        "generated_at": None,
        "predictions": df.to_dict(orient="records"),
        "backtests": {},
        "diagnostics": {},
        "source": "predictions.csv",
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
# 표시 유틸
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


def grade_badge(pred: Dict) -> str:
    icon, _ = GRADE_STYLE.get(grade_of(pred), GRADE_STYLE["LOW"])
    return f"{icon} {grade_of(pred)} {fmt_num(pred.get('confidence'), 0)}"


def ret_of(pred: Dict) -> Optional[float]:
    """예상수익률 — 값이 없으면 P50/현재가로 계산."""
    v = pred.get("expected_return")
    if not is_missing(v):
        return float(v)
    p50, now = pred.get("p50"), pred.get("current_price")
    if not is_missing(p50) and not is_missing(now) and float(now) > 0:
        return float(p50) / float(now) - 1.0
    return None


def staleness_banner(manifest: Dict) -> None:
    gen = manifest.get("generated_at")
    if not gen:
        return
    try:
        ts = datetime.fromisoformat(str(gen))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        st.caption(f"데이터 생성 시각: {gen}")
        return
    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    label = ts.astimezone().strftime("%Y-%m-%d %H:%M")
    if age_h > STALE_HOURS:
        st.error(
            f"⚠️ 이 스냅샷은 **{age_h:.0f}시간 전**({label}) 결과입니다. "
            "로컬에서 `python main.py && python publish.py` 를 다시 실행하세요."
        )
    else:
        st.caption(f"📅 스냅샷 {label} · {age_h:.1f}시간 전 · 읽기 전용")


# ======================================================================================
# 차트
# ======================================================================================
def overview_chart(rows: List[Dict]) -> go.Figure:
    """
    종목·기간별 예상수익률 가로 막대.
    막대 = P50 기준 예상수익률, 수염 = P10~P90, 색 = 신뢰도 등급.
    """
    labels, centers, lo, hi, colors, texts = [], [], [], [], [], []
    for p in rows:
        now = p.get("current_price")
        r = ret_of(p)
        if is_missing(now) or float(now) <= 0 or r is None:
            continue
        now = float(now)
        labels.append(f"{p.get('name', p.get('symbol'))} · {int(p.get('horizon', 0))}일")
        centers.append(r * 100)
        p10, p90 = p.get("p10"), p.get("p90")
        lo.append(max(0.0, (r - (float(p10) / now - 1.0)) * 100) if not is_missing(p10) else 0.0)
        hi.append(max(0.0, ((float(p90) / now - 1.0) - r) * 100) if not is_missing(p90) else 0.0)
        colors.append(GRADE_STYLE.get(grade_of(p), GRADE_STYLE["LOW"])[1])
        texts.append(f"{r * 100:+.1f}%")

    fig = go.Figure()
    if labels:
        fig.add_trace(go.Bar(
            x=centers, y=labels, orientation="h",
            marker=dict(color=colors), text=texts, textposition="outside",
            error_x=dict(type="data", symmetric=False, array=hi, arrayminus=lo,
                         color="rgba(120,120,120,0.55)", thickness=1.4, width=5),
            hovertemplate="%{y}<br>예상 %{x:+.2f}%<extra></extra>",
        ))
    fig.add_vline(x=0, line=dict(color="#888", width=1))
    fig.update_layout(
        height=max(260, 46 * max(1, len(labels))),
        margin=dict(l=10, r=50, t=10, b=40),
        xaxis_title="현재가 대비 예상수익률 (%) · 수염 = P10~P90",
        yaxis=dict(autorange="reversed"), showlegend=False,
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def distribution_bar(pred: Dict) -> go.Figure:
    """현재가와 예측 분포(P10~P90)를 가로 한 줄로."""
    currency = pred.get("currency", "KRW")
    now = pred.get("current_price")
    p10, p25, p50, p75, p90 = (pred.get(k) for k in ("p10", "p25", "p50", "p75", "p90"))

    fig = go.Figure()
    if not is_missing(p10) and not is_missing(p90):
        fig.add_trace(go.Scatter(
            x=[float(p10), float(p90)], y=[0, 0], mode="lines",
            line=dict(color="rgba(31,119,180,0.22)", width=28), hoverinfo="skip",
        ))
    if not is_missing(p25) and not is_missing(p75):
        fig.add_trace(go.Scatter(
            x=[float(p25), float(p75)], y=[0, 0], mode="lines",
            line=dict(color="rgba(31,119,180,0.55)", width=28), hoverinfo="skip",
        ))
    if not is_missing(p50):
        fig.add_trace(go.Scatter(
            x=[float(p50)], y=[0], mode="markers+text",
            marker=dict(color="#d62728", size=15, symbol="diamond"),
            text=[f"P50 {fmt_price(p50, currency)}"], textposition="top center",
            hoverinfo="skip",
        ))
    if not is_missing(now):
        fig.add_trace(go.Scatter(
            x=[float(now)], y=[0], mode="markers+text",
            marker=dict(color="#333", size=16, symbol="line-ns",
                        line=dict(width=3, color="#333")),
            text=[f"현재 {fmt_price(now, currency)}"], textposition="bottom center",
            hoverinfo="skip",
        ))
    fig.update_layout(
        height=180, margin=dict(l=20, r=20, t=35, b=25), showlegend=False,
        yaxis=dict(visible=False, range=[-1, 1]),
        xaxis=dict(title=f"가격 ({currency})", showgrid=True, gridcolor="rgba(0,0,0,0.06)"),
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def fan_chart(hist: Optional[pd.DataFrame], pred: Dict) -> go.Figure:
    """과거 종가 + 미래 h거래일 분위수 콘."""
    currency = pred.get("currency", "KRW")
    fig = go.Figure()

    last_date = None
    if hist is not None and not hist.empty:
        show = hist.tail(180)
        fig.add_trace(go.Scatter(
            x=show["date"], y=show["close"], mode="lines", name="종가",
            line=dict(color="#1f77b4", width=1.8),
        ))
        last_date = show["date"].iloc[-1]
    if last_date is None:
        last_date = pd.Timestamp.today().normalize()

    h = int(pred.get("horizon") or 0)
    now = pred.get("current_price")
    if not h or is_missing(now):
        fig.update_layout(height=430, margin=dict(l=10, r=10, t=30, b=10))
        return fig
    now = float(now)

    future = pd.bdate_range(last_date + pd.Timedelta(days=1), periods=h)
    steps = len(future)
    if steps == 0:
        return fig
    scale = [((i + 1) / steps) ** 0.5 for i in range(steps)]

    def cone(key: str) -> List[float]:
        v = pred.get(key)
        if is_missing(v):
            return []
        return [now + (float(v) - now) * s for s in scale]

    p10, p25, p50, p75, p90 = (cone(k) for k in ("p10", "p25", "p50", "p75", "p90"))
    fx = list(future)

    if p10 and p90:
        fig.add_trace(go.Scatter(
            x=fx + fx[::-1], y=p90 + p10[::-1], fill="toself",
            fillcolor="rgba(31,119,180,0.12)", line=dict(width=0),
            name="80% 구간", hoverinfo="skip",
        ))
    if p25 and p75:
        fig.add_trace(go.Scatter(
            x=fx + fx[::-1], y=p75 + p25[::-1], fill="toself",
            fillcolor="rgba(31,119,180,0.28)", line=dict(width=0),
            name="50% 구간", hoverinfo="skip",
        ))
    if p50:
        fig.add_trace(go.Scatter(
            x=fx, y=p50, mode="lines", name="P50 (중앙)",
            line=dict(color="#d62728", width=2, dash="dash"),
        ))
    fig.add_hline(y=now, line=dict(color="gray", width=1, dash="dot"))
    fig.update_layout(
        height=430, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified",
        legend=dict(orientation="h", y=1.1), yaxis_title=f"가격 ({currency})",
        plot_bgcolor="rgba(0,0,0,0)",
    )
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
                             line=dict(color="#2ca02c", width=1.8)))
    bh = next((c for c in ["buy_hold", "bh_equity", "benchmark"] if c in bt.columns), None)
    if bh:
        fig.add_trace(go.Scatter(x=bt[xcol], y=bt[bh], mode="lines", name="Buy & Hold",
                                 line=dict(color="#999", width=1.4, dash="dot")))
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=20, b=10),
                      legend=dict(orientation="h", y=1.15),
                      plot_bgcolor="rgba(0,0,0,0)")
    return fig


# ======================================================================================
# 탭 1 — 전체 요약
# ======================================================================================
def render_overview(preds: List[Dict]) -> None:
    st.markdown("#### 종목별 예측 한눈에 보기")
    st.caption(
        "막대 = 예상수익률(P50 기준) · 수염 = P10~P90 불확실성 폭 · "
        "색 = 신뢰도 (🟢 높음 · 🟡 보통 · 🔴 낮음)"
    )
    st.plotly_chart(overview_chart(preds), use_container_width=True)

    st.markdown("#### 요약 표")
    rows = []
    for p in preds:
        currency = p.get("currency", "KRW")
        rows.append({
            "종목": f"{p.get('name', p.get('symbol'))} ({p.get('symbol')})",
            "기간": f"{int(p.get('horizon', 0))}일",
            "현재가": fmt_price(p.get("current_price"), currency),
            "예측가 (P50)": fmt_price(p.get("p50"), currency),
            "예상수익률": fmt_pct(ret_of(p)),
            "예측범위 (P10~P90)":
                f"{fmt_price(p.get('p10'), currency)} ~ {fmt_price(p.get('p90'), currency)}",
            "상승확률": fmt_pct(p.get("prob_up"), signed=False),
            "신뢰도": grade_badge(p),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    n_low = sum(1 for p in preds if grade_of(p) == "LOW")
    if n_low:
        st.info(
            f"전체 {len(preds)}건 중 **{n_low}건이 신뢰도 LOW** 입니다. "
            "모델 고장이 아니라 해당 구간의 예측 가능성이 낮다는 표시이므로, "
            "점 예측보다 범위의 폭을 보십시오."
        )


# ======================================================================================
# 탭 2 — 종목 상세
# ======================================================================================
def render_detail(pred: Dict, payload: Dict, symbol: str, horizon: int) -> None:
    currency = pred.get("currency", "KRW")
    now = pred.get("current_price")

    head_l, head_r = st.columns([3, 1])
    with head_l:
        st.markdown(f"### {pred.get('name', symbol)} · {horizon}거래일 후 예측")
    with head_r:
        st.markdown(f"### {grade_badge(pred)}")
        st.caption("모델 신뢰도 (0~100)")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("현재가", fmt_price(now, currency))
    c2.metric("예측 중앙가 (P50)", fmt_price(pred.get("p50"), currency), fmt_pct(ret_of(pred)))
    c3.metric("상승 확률", fmt_pct(pred.get("prob_up"), signed=False))
    c4.metric("예상 변동성 (연율)",
              fmt_pct(pred.get("expected_volatility_annual"), signed=False))

    if grade_of(pred) == "LOW":
        st.warning("신뢰도가 낮은 예측입니다. 중앙값보다 **아래 분포의 폭**을 근거로 삼으십시오.")

    st.markdown("#### 예측 분포")
    st.plotly_chart(distribution_bar(pred), use_container_width=True)
    st.caption(
        f"{horizon}거래일 후 가격이 진한 구간(P25~P75)에 들어올 확률이 50%, "
        "연한 구간(P10~P90)에 들어올 확률이 80% 라는 뜻입니다."
    )

    st.markdown("#### 가격 경로")
    st.plotly_chart(fan_chart(load_history(symbol), pred), use_container_width=True)
    st.caption(
        "콘의 폭은 √t 로 보간한 시각적 근사입니다. 모델이 산출하는 것은 "
        f"{horizon}일 후 최종 시점의 분포 하나뿐이며, 미래 날짜는 공휴일 미반영 근사입니다."
    )

    left, right = st.columns(2)
    with left:
        st.markdown("**분위수별 가격**")
        q_rows = []
        for key, label in [("p10", "P10 (하위 10%)"), ("p25", "P25"), ("p50", "P50 (중앙)"),
                           ("p75", "P75"), ("p90", "P90 (상위 10%)")]:
            v = pred.get(key)
            chg = None
            if not is_missing(v) and not is_missing(now) and float(now) > 0:
                chg = float(v) / float(now) - 1.0
            q_rows.append({"분위수": label, "가격": fmt_price(v, currency),
                           "현재가 대비": fmt_pct(chg)})
        st.dataframe(pd.DataFrame(q_rows), hide_index=True, use_container_width=True)

    with right:
        st.markdown("**참고용 레벨** — 투자 조언 아님")
        lv_rows = [
            {"항목": "2차 목표", "가격": fmt_price(pred.get("target_2"), currency)},
            {"항목": "1차 목표", "가격": fmt_price(pred.get("target_1"), currency)},
            {"항목": "추가매수 고려", "가격": fmt_price(pred.get("add_buy_reference"), currency)},
            {"항목": "손절 고려", "가격": fmt_price(pred.get("stop_loss_reference"), currency)},
        ]
        st.dataframe(pd.DataFrame(lv_rows), hide_index=True, use_container_width=True)
        st.caption(
            f"Risk/Reward {fmt_num(pred.get('risk_reward'))} · "
            f"ATR {fmt_pct(pred.get('atr_pct'), signed=False)} · "
            f"지지 {fmt_price(pred.get('support_20d'), currency)} / "
            f"저항 {fmt_price(pred.get('resistance_20d'), currency)}"
        )

    with st.expander("🔬 모델 진단 · 데이터 상태"):
        d1, d2 = st.columns(2)
        with d1:
            st.markdown("**Out-of-Sample 성능**")
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
            st.caption("커버리지가 80%에서 크게 벗어나면 구간 추정을 믿기 어렵다는 뜻입니다.")
        with d2:
            st.markdown("**모델 · 데이터**")
            info = [
                ("선택된 모델", str(pred.get("model_weights") or pred.get("models") or "-")),
                ("Fallback level", str(pred.get("fallback_level"))),
                ("마지막 데이터", str(pred.get("last_data_time"))),
                ("학습 시각", str(pred.get("trained_at"))),
            ]
            shrink = pred.get("shrinkage")
            if not is_missing(shrink) and float(shrink) < 0.999:
                info.insert(1, ("과대외삽 보정", f"x{float(shrink):.2f}"))
            missing = pred.get("missing_data")
            if missing:
                info.append(("누락 데이터", str(missing)))
            st.dataframe(pd.DataFrame(info, columns=["항목", "값"]),
                         hide_index=True, use_container_width=True)

        regime = pred.get("regime")
        if regime:
            st.markdown("**시장 regime**")
            st.code(str(regime), language=None)
        notes = pred.get("notes")
        if notes:
            st.markdown("**처리 내역**")
            for n in str(notes).split(" | "):
                if n.strip():
                    st.markdown(f"- {n.strip()}")

    bt_meta = (payload.get("backtests") or {}).get(f"{symbol}_h{horizon}")
    bt_df = load_backtest(symbol, horizon)
    if bt_meta or bt_df is not None:
        with st.expander("📉 백테스트 (Out-of-Sample)"):
            if bt_meta:
                m = bt_meta.get("metrics") or {}
                b = bt_meta.get("buy_hold") or {}
                cols = st.columns(4)
                cols[0].metric("Sharpe", fmt_num(m.get("sharpe")))
                cols[1].metric("연환산 수익", fmt_pct(m.get("annual_return")))
                cols[2].metric("MDD", fmt_pct(m.get("max_drawdown")))
                cols[3].metric("Buy&Hold Sharpe", fmt_num(b.get("sharpe")))
            if bt_df is not None:
                fig = equity_chart(bt_df)
                if fig is not None:
                    st.plotly_chart(fig, use_container_width=True)
            st.caption(
                "상승장에서는 어떤 타이밍 전략도 단순 보유를 이기기 어렵습니다. "
                "Buy&Hold 대비 개선이 없다면 보유가 낫다는 정보로 읽으십시오."
            )


# ======================================================================================
# 본문
# ======================================================================================
def main() -> None:
    manifest = load_manifest()
    payload = load_predictions()

    st.title("📈 주가 예측 대시보드")
    if manifest is None or payload is None:
        st.error(
            "`published/` 안에서 읽을 파일을 찾지 못했습니다.\n\n"
            "`predictions.json` 또는 `predictions.csv` 중 하나가 있어야 합니다.\n\n"
            "```bash\npython main.py\npython publish.py\n```"
        )
        st.stop()

    staleness_banner(manifest)
    if payload.get("source") == "predictions.csv":
        st.info("CSV 만으로 구동 중입니다. 백테스트·진단 상세는 `publish.py` 게시 시 표시됩니다.")

    preds: List[Dict] = payload.get("predictions") or []
    if not preds:
        st.warning("예측 결과가 비어 있습니다.")
        st.stop()

    df = pd.DataFrame(preds)
    symbols = sorted(df["symbol"].astype(str).unique())
    name_of = {s: str(df[df["symbol"].astype(str) == s]["name"].iloc[0]) for s in symbols}

    with st.sidebar:
        st.markdown("### 조회 조건")
        symbol = st.selectbox("종목", symbols,
                              format_func=lambda s: f"{name_of.get(s, s)} ({s})")
        sub = df[df["symbol"].astype(str) == symbol]
        horizons = sorted(int(h) for h in sub["horizon"].unique())
        horizon = st.selectbox("예측 기간", horizons, format_func=lambda h: f"{h} 거래일")
        st.divider()
        st.caption(f"종목 {len(symbols)}개 · 예측 {len(preds)}건")
        st.caption(f"스키마 v{manifest.get('schema_version', '?')}")
        st.divider()
        st.caption("로컬 계산 결과의 스냅샷을 읽기만 합니다. 실시간 시세가 아닙니다.")

    tab_all, tab_one = st.tabs(["📊 전체 요약", "🔍 종목 상세"])
    with tab_all:
        render_overview(preds)
    with tab_one:
        row = sub[sub["horizon"] == horizon]
        if row.empty:
            st.warning("해당 조합의 예측이 없습니다.")
        else:
            render_detail(row.iloc[0].to_dict(), payload, symbol, horizon)

    st.divider()
    st.caption(DISCLAIMER)


if __name__ == "__main__":
    main()