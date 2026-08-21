"""
streamlit_app.py
================
Streamlit Cloud 용 **읽기 전용** 대시보드.

이 파일은 토스 API 를 호출하지 않는다. `publish.py` 가 저장소에 올린
`published/` 스냅샷만 읽는다. 따라서 클라우드에 API 자격증명이 필요 없고,
허용 IP 문제도 발생하지 않는다.

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

st.set_page_config(page_title="주가 예측 대시보드", page_icon="📈", layout="wide")


# ======================================================================================
# 데이터 로딩
# ======================================================================================
@st.cache_data(ttl=300, show_spinner=False)
def load_manifest() -> Optional[Dict]:
    """manifest.json 이 없으면 CSV 파일의 수정 시각으로 최소 manifest 를 합성한다."""
    path = PUBLISHED / "manifest.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    csv_path = PUBLISHED / "predictions.csv"
    if not csv_path.exists():
        return None
    mtime = datetime.fromtimestamp(csv_path.stat().st_mtime, tz=timezone.utc)
    return {
        "schema_version": "csv-only",
        "generated_at": mtime.isoformat(timespec="seconds"),
        "note": "manifest.json 없이 predictions.csv 만으로 구동 중",
    }


@st.cache_data(ttl=300, show_spinner=False)
def load_predictions() -> Optional[Dict]:
    """
    predictions.json 이 있으면 그것을 쓰고, 없으면 predictions.csv 로 대체한다.

    CSV 만으로도 대시보드가 돌아가야 한다 — main.py 가 만든 CSV 를 저장소에 직접
    올리는 방식(publish.py 없이 수동 커밋)도 지원하기 위해서다.
    이 경우 백테스트 지표/진단 상세는 비어 있고 예측 표시만 동작한다.
    """
    jpath = PUBLISHED / "predictions.json"
    if jpath.exists():
        with open(jpath, "r", encoding="utf-8") as f:
            return json.load(f)

    cpath = PUBLISHED / "predictions.csv"
    if not cpath.exists():
        return None
    df = pd.read_csv(cpath)
    df = df.where(pd.notna(df), None)          # NaN -> None (JSON 경로와 동일하게)
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
    for col in df.columns:
        if col.lower() in ("date", "index"):
            df[col] = pd.to_datetime(df[col], errors="coerce")
            df = df.rename(columns={col: "date"})
            break
    return df


# ======================================================================================
# 표시 유틸
# ======================================================================================
def fmt_price(v: Optional[float], currency: str) -> str:
    if v is None or pd.isna(v):
        return "N/A"
    return f"{v:,.0f}원" if currency == "KRW" else f"${v:,.2f}"


def fmt_pct(v: Optional[float], signed: bool = True) -> str:
    if v is None or pd.isna(v):
        return "N/A"
    return f"{v * 100:+.2f}%" if signed else f"{v * 100:.1f}%"


def fmt_num(v, digits: int = 2) -> str:
    """None / NaN 안전 숫자 포맷 (게시 시 NaN 은 None 으로 치환된다)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "N/A"
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "N/A"


def grade_color(grade: str) -> str:
    return {"HIGH": "🟢", "MEDIUM": "🟡"}.get(str(grade).upper(), "🔴")


def staleness_banner(manifest: Dict) -> None:
    gen = manifest.get("generated_at")
    if not gen:
        return
    try:
        ts = datetime.fromisoformat(str(gen))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
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
        st.caption(f"데이터 생성 시각: {label} ({age_h:.1f}시간 전) · 읽기 전용 스냅샷")


# ======================================================================================
# 차트
# ======================================================================================
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

    h = int(pred.get("horizon", 0) or 0)
    price_now = pred.get("current_price")
    if not h or price_now is None:
        fig.update_layout(height=420, margin=dict(l=10, r=10, t=30, b=10))
        return fig

    # 미래 거래일 (공휴일 미반영 근사)
    future = pd.bdate_range(last_date + pd.Timedelta(days=1), periods=h)
    steps = len(future)
    if steps == 0:
        return fig

    # 시간에 따른 폭 확대는 sqrt(t) 로 근사 (모델의 sigma_h 스케일링과 동일 가정)
    scale = [((i + 1) / steps) ** 0.5 for i in range(steps)]

    def cone(q_key: str) -> List[float]:
        target = pred.get(q_key)
        if target is None or pd.isna(target):
            return []
        return [price_now + (target - price_now) * s for s in scale]

    p10, p25, p50, p75, p90 = (cone(k) for k in ("p10", "p25", "p50", "p75", "p90"))

    if p10 and p90:
        fig.add_trace(go.Scatter(
            x=list(future) + list(future)[::-1], y=p90 + p10[::-1],
            fill="toself", fillcolor="rgba(31,119,180,0.12)",
            line=dict(width=0), name="80% 구간", hoverinfo="skip",
        ))
    if p25 and p75:
        fig.add_trace(go.Scatter(
            x=list(future) + list(future)[::-1], y=p75 + p25[::-1],
            fill="toself", fillcolor="rgba(31,119,180,0.25)",
            line=dict(width=0), name="50% 구간", hoverinfo="skip",
        ))
    if p50:
        fig.add_trace(go.Scatter(
            x=future, y=p50, mode="lines", name="P50 (중앙)",
            line=dict(color="#d62728", width=2, dash="dash"),
        ))

    fig.add_hline(y=price_now, line=dict(color="gray", width=1, dash="dot"),
                  annotation_text=f"현재 {fmt_price(price_now, currency)}",
                  annotation_position="top left")
    fig.update_layout(
        height=440, margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified", legend=dict(orientation="h", y=1.08),
        yaxis_title=f"가격 ({currency})",
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
                                 line=dict(color="#999999", width=1.4, dash="dot")))
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=20, b=10),
                      legend=dict(orientation="h", y=1.15))
    return fig


# ======================================================================================
# 본문
# ======================================================================================
def main() -> None:
    st.title("📈 주가 예측 대시보드")

    manifest = load_manifest()
    payload = load_predictions()
    if manifest is None or payload is None:
        st.error(
            "`published/` 안에서 읽을 파일을 찾지 못했습니다.\n\n"
            "`predictions.json` 또는 `predictions.csv` 중 하나가 있어야 합니다.\n\n"
            "```bash\npython main.py\npython publish.py\n```"
        )
        st.stop()

    if payload.get("source") == "predictions.csv":
        st.info(
            "CSV 만으로 구동 중입니다. 예측 표시는 정상이며, "
            "백테스트 지표와 진단 상세는 `publish.py` 로 게시해야 표시됩니다."
        )

    staleness_banner(manifest)
    preds: List[Dict] = payload.get("predictions", [])
    if not preds:
        st.warning("예측 결과가 비어 있습니다.")
        st.stop()

    df = pd.DataFrame(preds)

    # ---------------- 사이드바 ----------------
    with st.sidebar:
        st.header("조회 조건")
        symbols = sorted(df["symbol"].unique())
        name_map = {s: f"{s} ({df[df['symbol'] == s]['name'].iloc[0]})" for s in symbols}
        symbol = st.selectbox("종목", symbols, format_func=lambda s: name_map.get(s, s))

        sub = df[df["symbol"] == symbol]
        horizons = sorted(int(h) for h in sub["horizon"].unique())
        horizon = st.selectbox("예측 기간 (거래일)", horizons, index=0)

        st.divider()
        st.caption(f"스키마 v{manifest.get('schema_version', '?')}")
        st.caption(f"종목 {len(symbols)}개 · 예측 {len(preds)}건")

    row = sub[sub["horizon"] == horizon]
    if row.empty:
        st.warning("해당 조합의 예측이 없습니다.")
        st.stop()
    pred = row.iloc[0].to_dict()
    currency = pred.get("currency", "KRW")

    # ---------------- 헤더 ----------------
    st.subheader(f"{pred.get('name', symbol)} · {horizon}거래일 예측")

    grade = str(pred.get("confidence_grade", "LOW"))
    conf_txt = fmt_num(pred.get("confidence"), 0)
    if grade.upper() == "LOW":
        st.warning(
            f"{grade_color(grade)} **신뢰도 {conf_txt}/100 ({grade})** — "
            "점 예측보다 분포의 폭과 불확실성을 참고하세요."
        )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("현재가", fmt_price(pred.get("current_price"), currency))
    c2.metric("예측 중앙가 (P50)", fmt_price(pred.get("p50"), currency),
              fmt_pct(pred.get("expected_return")))
    c3.metric("상승 확률", fmt_pct(pred.get("prob_up"), signed=False))
    c4.metric("신뢰도", f"{conf_txt} / 100", grade)
    c5.metric("예상 변동성 (연율)",
              fmt_pct(pred.get("expected_volatility_annual"), signed=False))

    # ---------------- 차트 ----------------
    st.plotly_chart(fan_chart(load_history(symbol), pred), use_container_width=True)
    st.caption(
        "음영은 예측 분포의 50% / 80% 구간입니다. 콘의 폭은 √t 로 근사해 그린 것이며, "
        "실제 모델은 최종 시점(h일 후) 분포만 산출합니다. 미래 날짜는 공휴일 미반영 근사입니다."
    )

    # ---------------- 분위수 / 참고 레벨 ----------------
    left, right = st.columns(2)
    with left:
        st.markdown("**예측 분위수**")
        q = pd.DataFrame({
            "분위수": ["P10", "P25", "P50", "P75", "P90"],
            "가격": [fmt_price(pred.get(k), currency)
                     for k in ["p10", "p25", "p50", "p75", "p90"]],
            "현재가 대비": [
                fmt_pct((pred.get(k) / pred["current_price"] - 1)
                        if pred.get(k) and pred.get("current_price") else None)
                for k in ["p10", "p25", "p50", "p75", "p90"]
            ],
        })
        st.dataframe(q, hide_index=True, use_container_width=True)

    with right:
        st.markdown("**참고용 레벨** — 투자 조언 아님")
        lv = pd.DataFrame({
            "항목": ["보수적", "1차 목표", "2차 목표", "추가매수 고려", "손절 고려"],
            "가격": [fmt_price(pred.get(k), currency) for k in
                     ["conservative_price", "target_1", "target_2",
                      "add_buy_reference", "stop_loss_reference"]],
        })
        st.dataframe(lv, hide_index=True, use_container_width=True)
        st.caption(
            f"Risk/Reward {fmt_num(pred.get('risk_reward'))} · "
            f"ATR {fmt_pct(pred.get('atr_pct'), signed=False)}"
        )

    # ---------------- 진단 ----------------
    with st.expander("모델 진단 · 데이터 상태", expanded=False):
        d1, d2 = st.columns(2)
        with d1:
            st.markdown("**OOS 성능**")
            st.dataframe(pd.DataFrame({
                "지표": ["IC (Spearman)", "방향 정확도", "RMSE", "baseline RMSE",
                         "80% 구간 커버리지"],
                "값": [
                    fmt_num(pred.get("oos_ic"), 3),
                    fmt_pct(pred.get("oos_directional_accuracy"), signed=False),
                    fmt_num(pred.get("oos_rmse"), 4),
                    fmt_num(pred.get("baseline_rmse"), 4),
                    fmt_pct(pred.get("coverage_80"), signed=False),
                ],
            }), hide_index=True, use_container_width=True)
            st.caption(
                "커버리지가 80%에서 크게 벗어나면 구간 추정이 신뢰도 낮다는 뜻입니다."
            )
        with d2:
            st.markdown("**모델 / 데이터**")
            st.write(f"선택된 모델: `{pred.get('model_weights') or pred.get('models')}`")
            shrink = pred.get("shrinkage")
            if shrink is not None and not pd.isna(shrink) and shrink < 0.999:
                st.write(f"과대외삽 보정: **x{shrink:.2f}**")
            st.write(f"Fallback level: {pred.get('fallback_level')}")
            st.write(f"마지막 데이터: {pred.get('last_data_time')}")
            st.write(f"학습 시각: {pred.get('trained_at')}")
            missing = pred.get("missing_data")
            if missing:
                st.write(f"누락 데이터: {missing}")

        regime = pred.get("regime")
        if regime:
            st.markdown("**시장 regime**")
            st.code(str(regime), language=None)
        notes = pred.get("notes")
        if notes:
            st.markdown("**비고**")
            for n in str(notes).split(" | "):
                st.write(f"- {n}")

    # ---------------- 백테스트 ----------------
    bt_meta = (payload.get("backtests") or {}).get(f"{symbol}_h{horizon}")
    bt_df = load_backtest(symbol, horizon)
    if bt_meta or bt_df is not None:
        with st.expander("백테스트 (Out-of-Sample)", expanded=False):
            if bt_meta:
                m = bt_meta.get("metrics", {}) or {}
                b = bt_meta.get("buy_hold", {}) or {}
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
                "상승장 구간에서는 어떤 타이밍 전략도 단순 보유를 이기기 어렵습니다. "
                "Buy&Hold 대비 개선이 없다면 보유가 낫다는 정보로 읽으십시오."
            )

    # ---------------- 전체 표 ----------------
    with st.expander("전체 예측 표", expanded=False):
        cols = ["symbol", "name", "horizon", "current_price", "p50", "expected_return",
                "prob_up", "confidence", "confidence_grade", "fallback_level",
                "last_data_time"]
        show = df[[c for c in cols if c in df.columns]].copy()
        st.dataframe(show, hide_index=True, use_container_width=True)
        csv_path = PUBLISHED / "predictions.csv"
        if csv_path.exists():
            st.download_button("predictions.csv 내려받기", csv_path.read_bytes(),
                               file_name="predictions.csv", mime="text/csv")

    st.divider()
    st.caption(DISCLAIMER)


if __name__ == "__main__":
    main()
