"""
devnotes_view.py
================
Streamlit 대시보드의 "개발자 노트" 섹션.

`streamlit_app.py` 는 읽기 전용이므로 여기서도 `published/devnotes.json` 만 읽는다.
파일이 없으면 아무것도 그리지 않는다 (구버전 스냅샷 호환).

사용:
    from devnotes_view import render_devnotes
    render_devnotes(PUBLISHED, section_head=section_head)
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta
import re
import json
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd
import streamlit as st

IMPACT_META = {
    "high": ("#f23645", "높음", "예측값이나 해석이 달라지는 변경"),
    "medium": ("#f0b90b", "보통", "동작 개선 / 기능 추가"),
    "low": ("#5b6b7f", "낮음", "문서·정리·실험 기록"),
}

SECTION_ICON = {
    "Added": "＋",
    "Changed": "～",
    "Fixed": "✓",
    "Removed": "－",
    "Deprecated": "!",
    "Notes": "·",
    "Known Issues": "!",
}

_CSS = """
<style>
.dn-wrap { margin-top: 4px; }
.dn-card {
  border: 1px solid rgba(255,255,255,0.09);
  border-radius: 12px;
  padding: 14px 16px 10px 16px;
  margin-bottom: 12px;
  background: rgba(255,255,255,0.022);
}
.dn-card.dn-high { border-left: 3px solid #f23645; }
.dn-card.dn-medium { border-left: 3px solid #f0b90b; }
.dn-card.dn-low { border-left: 3px solid #5b6b7f; }
.dn-head {
  display: flex; align-items: baseline; gap: 10px;
  flex-wrap: wrap; margin-bottom: 8px;
}
.dn-ver { font-size: 1.02rem; font-weight: 700; color: #e8edf4; letter-spacing: .2px; }
.dn-date { font-size: .82rem; color: #8b98a8; }
.dn-badge {
  font-size: .68rem; font-weight: 700; letter-spacing: .4px;
  padding: 2px 7px; border-radius: 5px; text-transform: uppercase;
}
.dn-tag {
  font-size: .68rem; color: #9fb0c4;
  border: 1px solid rgba(255,255,255,0.12);
  padding: 1px 6px; border-radius: 4px;
}
.dn-sec { margin: 9px 0 3px 0; font-size: .78rem; font-weight: 700;
          color: #9fb0c4; letter-spacing: .5px; text-transform: uppercase; }
.dn-item { font-size: .87rem; color: #c8d2de; line-height: 1.62;
           margin: 0 0 4px 0; padding-left: 15px; position: relative; }
.dn-item:before { content: "▪"; position: absolute; left: 2px; color: #55637a; }
.dn-item code {
  background: rgba(255,255,255,0.07); padding: 1px 5px;
  border-radius: 4px; font-size: .82em; color: #ffd479;
}
.dn-item strong { color: #e8edf4; }
.dn-env { font-size: .76rem; color: #8b98a8; margin-top: 2px; }
.dn-pill {
  display: inline-block; font-size: .7rem; margin: 2px 5px 2px 0;
  padding: 2px 8px; border-radius: 999px;
  border: 1px solid rgba(255,255,255,0.12); color: #9fb0c4;
}
.dn-pill.on { color: #4ec9a5; border-color: rgba(78,201,165,0.35); }
.dn-pill.off { color: #6c7787; }
</style>
"""


@st.cache_data(ttl=300, show_spinner=False)
def load_devnotes(published_dir: str) -> Optional[Dict]:
    path = Path(published_dir) / "devnotes.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _inline_md(text: str) -> str:
    """`code` 와 **bold** 만 최소 지원. 나머지는 이스케이프한다."""
    out = html.escape(str(text))
    parts = out.split("`")
    out = "".join(p if i % 2 == 0 else f"<code>{p}</code>" for i, p in enumerate(parts))
    parts = out.split("**")
    out = "".join(p if i % 2 == 0 else f"<strong>{p}</strong>" for i, p in enumerate(parts))
    return out


def _days_ago(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except Exception:
        return ""
    n = (date.today() - d).days
    if n <= 0:
        return "오늘"
    if n == 1:
        return "어제"
    if n < 30:
        return f"{n}일 전"
    if n < 365:
        return f"{n // 30}개월 전"
    return f"{n // 365}년 전"


def _card_html(note: Dict) -> str:
    impact = str(note.get("impact", "medium")).lower()
    color, label, _ = IMPACT_META.get(impact, IMPACT_META["medium"])
    ver = html.escape(str(note.get("version", "?")))
    dt = str(note.get("date", ""))
    ago = _days_ago(dt)

    tags = "".join(
        f"<span class='dn-tag'>{html.escape(str(t))}</span>"
        for t in (note.get("tags") or [])
    )
    head = (
        f"<div class='dn-head'>"
        f"<span class='dn-ver'>{ver}</span>"
        f"<span class='dn-date'>{html.escape(dt)}"
        f"{' · ' + ago if ago else ''}</span>"
        f"<span class='dn-badge' style='background:{color}22;color:{color};"
        f"border:1px solid {color}55'>{label}</span>"
        f"{tags}</div>"
    )

    body = []
    sections = note.get("sections") or {}
    order = ["Added", "Changed", "Fixed", "Removed", "Deprecated", "Notes", "Known Issues"]
    keys = [k for k in order if k in sections] + [k for k in sections if k not in order]
    for key in keys:
        items = sections.get(key) or []
        if not items:
            continue
        icon = SECTION_ICON.get(key, "·")
        body.append(f"<div class='dn-sec'>{icon} {html.escape(str(key))}</div>")
        for it in items:
            body.append(f"<div class='dn-item'>{_inline_md(it)}</div>")

    return f"<div class='dn-card dn-{impact}'>{head}{''.join(body)}</div>"


def _env_html(env: Dict) -> str:
    opt = (env or {}).get("optional_models") or {}
    if not opt:
        return ""
    pills = "".join(
        f"<span class='dn-pill {'on' if v else 'off'}'>"
        f"{'●' if v else '○'} {html.escape(str(k))}</span>"
        for k, v in sorted(opt.items())
    )
    conf = (env or {}).get("config") or {}
    bits: List[str] = []
    if conf.get("n_symbols"):
        bits.append(f"종목 {conf['n_symbols']}")
    if conf.get("horizons"):
        bits.append(f"horizon {','.join(str(h) for h in conf['horizons'])}")
    if conf.get("max_features"):
        bits.append(f"max_features {conf['max_features']}")
    for key, label in (("use_panel", "패널"), ("use_nnls_stacking", "NNLS"),
                       ("use_garch_sigma", "GARCH")):
        if key in conf:
            bits.append(f"{label} {'ON' if conf[key] else 'OFF'}")
    meta = " · ".join(bits)
    return (
        f"<div class='dn-env'>이 스냅샷 실행 환경 &nbsp;{pills}</div>"
        + (f"<div class='dn-env'>{html.escape(meta)}</div>" if meta else "")
    )


# --------------------------------------------------------------------------------------
# 시계열 타임라인 (버전 = 점, 높이 = impact, 색 = 첫 태그, 크기 = 항목 수)
# --------------------------------------------------------------------------------------
_IMPACT_LANE = {"high": 3, "medium": 2, "low": 1}
_TAG_COLORS = {
    "panel": "#e07b39", "conformal": "#6a5acd", "ensemble": "#2a9d8f", "bugfix": "#d62828",
    "measurement": "#1d3557", "kcs": "#b5838d", "data": "#457b9d", "training": "#457b9d",
    "gru": "#f4a261", "gpu": "#f4a261", "features": "#8a9a5b", "calibration": "#6a5acd",
    "volatility": "#6a5acd", "backtest": "#7f8c8d", "dashboard": "#7f8c8d", "cron": "#7f8c8d",
    "backup": "#7f8c8d", "config": "#7f8c8d", "predict-only": "#2a9d8f",
}


def build_timeline_figure(notes: List[Dict], height: int = 280):
    """
    DEVNOTES 를 한 눈에 보는 타임라인.
    x = 버전(시간순, 등간격) — 같은 날 여러 버전이 나와도 겹치지 않게 날짜 대신 버전을 축으로 쓰고
    눈금에 날짜를 같이 적는다. y = impact(높음/중간/낮음), 점 크기 = 항목 수, 색 = 대표 태그.
    hover 에 버전·날짜·태그·첫 항목. plotly 가 없으면 None.
    """
    try:
        import plotly.graph_objects as go
    except Exception:
        return None
    rows = []
    for n in notes:
        d = str(n.get("date", ""))[:10]
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except Exception:
            continue
        rows.append((d, str(n.get("version", "")), n))
    if not rows:
        return None

    def _vkey(v: str):
        return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))
    rows.sort(key=lambda r: (r[0], _vkey(r[1])))

    xs, ys, sizes, colors, texts, ticks = [], [], [], [], [], []
    prev_date = None
    for i, (d, ver, n) in enumerate(rows):
        imp = str(n.get("impact", "medium")).lower()
        tags = [str(t) for t in (n.get("tags") or [])]
        secs = n.get("sections") or {}
        n_items = sum(len(v) for v in secs.values()) if isinstance(secs, dict) else 0
        first = ""
        for sec in ("Fixed", "Changed", "Added", "Notes", "Known Issues"):
            if isinstance(secs, dict) and secs.get(sec):
                first = str(secs[sec][0])
                break
        first = re.sub(r"[`*]", "", first)[:100]
        xs.append(i); ys.append(_IMPACT_LANE.get(imp, 2))
        sizes.append(9 + 2.5 * min(n_items, 10))
        colors.append(_TAG_COLORS.get(tags[0] if tags else "", "#999999"))
        # 눈금: 버전 + (날짜가 바뀔 때만) 날짜
        ticks.append(f"{ver}<br>{d[5:]}" if d != prev_date else ver)
        prev_date = d
        texts.append(f"<b>{ver}</b> · {d} · {imp}<br>태그: {', '.join(tags) or '-'} · 항목 {n_items}개"
                     f"<br>{html.escape(first)}")

    fig = go.Figure()
    # impact 가 '높음' 인 버전은 연한 띠로 강조
    for x, y in zip(xs, ys):
        if y == 3:
            fig.add_shape(type="rect", x0=x - 0.5, x1=x + 0.5, y0=0.55, y1=3.45,
                          fillcolor="rgba(214,40,40,0.06)", line=dict(width=0), layer="below")
        fig.add_shape(type="line", x0=x, x1=x, y0=0.55, y1=y, line=dict(color="#d0d0d0", width=1))
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="lines+markers", line=dict(color="#cfd8dc", width=1.5, shape="hv"),
        marker=dict(size=sizes, color=colors, line=dict(color="white", width=1)),
        hovertext=texts, hoverinfo="text", showlegend=False,
    ))
    fig.update_xaxes(tickvals=xs, ticktext=ticks, tickfont=dict(size=10), showgrid=False,
                     range=[-0.6, len(xs) - 0.4])
    fig.update_yaxes(tickvals=[1, 2, 3], ticktext=["낮음", "중간", "높음"], range=[0.55, 3.45],
                     showgrid=True, gridcolor="#eeeeee", zeroline=False, title="")
    fig.update_layout(height=height, margin=dict(l=45, r=15, t=10, b=45),
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                      hoverlabel=dict(align="left"))
    return fig


def render_devnotes_timeline(notes: List[Dict]) -> None:
    fig = build_timeline_figure(notes)
    if fig is None:
        return
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    st.caption("점 = 버전(시간순), 높이 = 해석 영향도, 크기 = 항목 수, 색 = 대표 태그. 마우스를 올리면 요약.")


# --------------------------------------------------------------------------------------
# 마일스톤별 개선 — 라이브 트래커 주간 지표 위에 버전 표식
# --------------------------------------------------------------------------------------
def build_milestone_figure(notes: List[Dict], track: Dict, horizon: int = 5, height: int = 360):
    """
    위: 80% 구간 적중률(주간, 앵커 기준). 75~85% 를 '목표 구간' 띠로, 점 크기 = 앵커 수.
    아래: 방향 edge(다수방향 대비) 막대 — 양수 초록, 음수 주황.
    세로선 = 해석 영향(high) 버전 배포일, 같은 날은 한 라벨. LIVE 만.
    """
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return None
    rows = [t for t in (track.get("timeline") or [])
            if str(t.get("source")) == "LIVE" and int(t.get("horizon", -1)) == horizon]
    if not rows:
        return None
    rows.sort(key=lambda r: r["week"])
    weeks = [datetime.strptime(r["week"], "%Y-%m-%d") for r in rows]
    cov = [None if r.get("coverage_80") is None else r["coverage_80"] * 100 for r in rows]
    edge = [None if r.get("direction_edge") is None else r["direction_edge"] * 100 for r in rows]
    n_anc = [int(r.get("n_anchors") or 0) for r in rows]
    def _model_label(r: Dict) -> str:
        ta = [t for t in (r.get("trained_at") or []) if t]
        if not ta:
            return "모델 학습일 기록 없음 (0.9.10 이전 모델)"
        # 학습일 → 그 날짜 이전 가장 최근 DEVNOTES 버전
        vers = []
        for t in ta:
            v = ""
            for n in sorted(notes, key=lambda n: str(n.get("date", ""))):
                if str(n.get("date", ""))[:10] <= t:
                    v = str(n.get("version", ""))
            vers.append(f"{t[5:].replace('-', '/')}{f' ({v})' if v else ''}")
        return "모델 학습일 " + ", ".join(vers) + (" — 한 주에 모델 2개 섞임" if len(ta) > 1 else "")

    hover = [
        f"기준일 {r.get('anchor_first', r['week'])[5:].replace('-', '/')}~{r.get('anchor_last', r['week'])[5:].replace('-', '/')}"
        f"<br>표본 {r['n']}건 · 기준일 {r['n_anchors']}일 (만기 채점 완료분)<br>{_model_label(r)}"
        for r in rows
    ]

    # 대시보드 톤(하늘색-파랑)에 맞춘 팔레트
    C_LINE, C_BAND, C_POS, C_NEG = "#7cc4ff", "rgba(59,130,246,0.16)", "#60a5fa", "#64748b"
    C_MARK, C_TEXT, C_GRID = "rgba(148,163,184,0.5)", "#94a3b8", "rgba(148,163,184,0.12)"
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.62, 0.38], vertical_spacing=0.06)

    # 목표 띠 75~85%
    x0, x1 = min(weeks) - timedelta(days=4), max(weeks) + timedelta(days=18)
    fig.add_shape(type="rect", x0=x0, x1=x1, y0=75, y1=85, xref="x", yref="y",
                  fillcolor=C_BAND, line=dict(width=0), layer="below")
    fig.add_annotation(x=x1, y=85, xref="x", yref="y", text="목표 75~85%", showarrow=False,
                       xanchor="right", yanchor="bottom", font=dict(size=10, color=C_POS))
    fig.add_annotation(x=x0, y=104, xref="x", yref="y", text="80% 구간 적중률 (LIVE h5, 주간)", showarrow=False,
                       xanchor="left", yanchor="top", font=dict(size=11, color=C_TEXT))
    fig.add_trace(go.Scatter(
        x=weeks, y=cov, mode="lines+markers+text",
        line=dict(color=C_LINE, width=2.5, shape="spline", smoothing=0.6),
        marker=dict(size=[9 + 2.2 * min(a, 6) for a in n_anc], color=C_LINE,
                    line=dict(color="rgba(255,255,255,0.9)", width=1.5)),
        text=[f"{c:.0f}%" if c is not None else "" for c in cov],
        textposition=["bottom center" if (c or 0) > 92 else "top center" for c in cov],
        textfont=dict(size=10, color=C_LINE),
        hovertext=hover, hoverinfo="text+y", name="80% 구간 적중률",
    ), row=1, col=1)
    fig.add_hline(y=80, line=dict(color="rgba(96,165,250,0.55)", width=1, dash="dash"), row=1, col=1)
    fig.add_trace(go.Bar(
        x=weeks, y=edge, marker=dict(color=[C_POS if (e or 0) >= 0 else C_NEG for e in edge], opacity=0.85),
        width=[2.6 * 86400000] * len(weeks), hovertext=hover, hoverinfo="text+y", name="방향 edge",
        text=[f"{e:+.0f}%p" if e is not None else "" for e in edge], textposition="inside",
        insidetextanchor="end", textfont=dict(size=10, color="white"),
    ), row=2, col=1)
    fig.add_hline(y=0, line=dict(color="rgba(148,163,184,0.6)", width=1), row=2, col=1)

    # 버전 표식: 같은 날 여러 버전은 한 라벨, 라벨은 위 패널 아래쪽에
    by_date: Dict[str, List[str]] = {}
    for n in notes:
        if str(n.get("impact", "")).lower() != "high":
            continue
        d = str(n.get("date", ""))[:10]
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except Exception:
            continue
        if dt < x0 or dt > x1:
            continue
        by_date.setdefault(d, []).append(str(n.get("version", "")))
    for d, vers in sorted(by_date.items()):
        dt = datetime.strptime(d, "%Y-%m-%d")
        vers = sorted(vers, key=lambda v: tuple(int(x) if x.isdigit() else 0 for x in v.split(".")))
        label = vers[0] if len(vers) == 1 else f"{vers[0]}~{vers[-1]}"
        fig.add_shape(type="line", x0=dt, x1=dt, y0=0, y1=1, xref="x", yref="paper",
                      line=dict(color=C_MARK, width=1, dash="dot"))
        fig.add_annotation(x=dt, y=51, xref="x", yref="y", text=label, showarrow=False, textangle=-90,
                           xanchor="right", yanchor="bottom", font=dict(size=9, color=C_TEXT))

    fig.update_yaxes(range=[50, 106], ticksuffix="%", showgrid=True, gridcolor=C_GRID, zeroline=False,
                     title=dict(text="", font=dict(size=11)), row=1, col=1)
    fig.update_yaxes(ticksuffix="%p", showgrid=False, zeroline=False,
                     title=dict(text="방향 edge", font=dict(size=11)), row=2, col=1)
    fig.update_xaxes(range=[x0, x1], tickformat="%m-%d", showgrid=False, ticks="outside", row=2, col=1)
    fig.update_xaxes(showgrid=False, row=1, col=1)
    fig.update_layout(height=height, margin=dict(l=45, r=15, t=12, b=30), showlegend=False,
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                      hoverlabel=dict(align="left"), bargap=0.4)
    return fig


_MILESTONE_ROWS = [
    # (버전, 지표, 값, 비고) — DEVNOTES 의 실측치. 같은 잣대끼리만 비교할 것.
    ("0.9.0", "패널 IC h30", "+0.220", "in-sample 채점 (0.9.9 이전 잣대)"),
    ("0.9.9", "패널 IC h30", "−0.033", "cross-fit 채점 — 잣대가 바뀜, 모델은 같음"),
    ("0.9.10", "종목 IC 평균(58조합)", "0.153 → 0.025", "정직 채점. 이후 이 잣대로만 비교"),
    ("0.9.10", "전체 학습 시간", "5.1h → 12.1h → 5.6h", "5년→20년, GPU·NGBoost off, 토요일 크론"),
    ("0.9.14", "LIVE h5 방향 edge", "−9.5%p", "다수방향 기준, 앵커 6일 → 판단 보류"),
    ("0.9.14", "삼성 σ 국면 계수", "0.83 (h5 폭 −17%)", "가격 0.60 · VIX 0.80 · HY (수집 시작)"),
    ("목표", "LIVE h5 80% 적중", "98% → 80%대", "앵커 20일 이상 쌓인 뒤 판정 (9월 말)"),
]


def render_milestones(notes: List[Dict], track: Dict) -> None:
    """업데이트 탭: 마일스톤별 성적. 그래프(라이브 주간) + 표(DEVNOTES 실측치)."""
    st.markdown("**마일스톤별 성적**")
    fig = build_milestone_figure(notes, track or {})
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        st.caption("x = 예측 기준일 주의 금요일(만기가 돌아와 채점된 것만). 점 크기 = 그 주 기준일 수. "
                   "세로선 = 해석에 영향 주는 버전 배포일. 라이브 기록은 8월 21일부터, 앵커 20일 전엔 흐름만 참고.")
    else:
        st.caption("라이브 주간 기록이 아직 없습니다 (track_summary.json 의 timeline).")
    rows = [{"버전": v, "지표": m, "값": val, "비고": note} for v, m, val, note in _MILESTONE_ROWS]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("0.9.9 에서 채점 방식이 바뀌어 그 전후 IC 는 같은 잣대가 아닙니다. 이후 개선의 기준은 라이브 트래커입니다.")


def render_devnotes(
    published_dir: Path,
    section_head: Optional[Callable[..., None]] = None,
    max_versions: int = 12,
    expanded: bool = False,
) -> None:
    """개발자 노트 섹션 전체. 데이터가 없으면 조용히 아무것도 그리지 않는다."""
    data = load_devnotes(str(published_dir))
    if not data:
        return
    notes = data.get("notes") or []
    if not notes:
        return

    latest = notes[0]
    n_high = sum(1 for n in notes if str(n.get("impact", "")).lower() == "high")

    if section_head is not None:
        section_head(
            "CHANGELOG",
            "개발자 노트",
            f"최근 {html.escape(str(latest.get('version','')))} · "
            f"버전 {len(notes)}개 · 해석에 영향 주는 변경 {n_high}건",
        )
    else:
        st.subheader("개발자 노트")

    st.markdown(_CSS, unsafe_allow_html=True)

    # 타임라인은 접힌 상세 목록 밖, 섹션 최상단에 항상 보인다
    render_devnotes_timeline(notes)

    # 마일스톤별 성적 (라이브 트래커 주간 지표 + 버전 표식)
    track: Dict = {}
    try:
        import json as _json
        tp = Path(published_dir) / "track_summary.json"
        if tp.exists():
            track = _json.loads(tp.read_text(encoding="utf-8")) or {}
    except Exception:
        track = {}
    with st.expander("마일스톤별 성적 — 버전이 바뀌면서 얼마나 좋아졌나", expanded=False):
        render_milestones(notes, track)

    with st.expander(
        f"변경 이력 보기 — 최근 {latest.get('version','')} "
        f"({latest.get('date','')}, {_days_ago(str(latest.get('date','')))})",
        expanded=expanded,
    ):
        st.caption(
            "이전 스냅샷을 볼 때는 그 시점의 로직이 지금과 같다고 가정하지 마십시오. "
            "빨간 테두리(높음) 항목은 예측값이나 그 해석 자체가 달라진 변경입니다."
        )

        opts = ["전체", "해석 영향(높음)만"]
        all_tags = sorted({t for n in notes for t in (n.get("tags") or [])})
        choice = st.radio("표시 범위", opts, horizontal=True,
                          key="devnotes_scope", label_visibility="collapsed")
        picked: List[str] = []
        if all_tags:
            picked = st.multiselect("태그 필터", all_tags, default=[],
                                    key="devnotes_tags",
                                    placeholder="태그로 좁히기 (선택)")

        shown = notes
        if choice == opts[1]:
            shown = [n for n in shown if str(n.get("impact", "")).lower() == "high"]
        if picked:
            shown = [n for n in shown if set(picked) & set(n.get("tags") or [])]

        if not shown:
            st.info("조건에 맞는 항목이 없습니다.")
        else:
            cards = "".join(_card_html(n) for n in shown[:max_versions])
            st.markdown(f"<div class='dn-wrap'>{cards}</div>", unsafe_allow_html=True)
            if len(shown) > max_versions:
                st.caption(f"이하 {len(shown) - max_versions}개 버전은 DEVNOTES.md 에서 확인하세요.")

        env_html = _env_html(data.get("environment") or {})
        if env_html:
            st.markdown(env_html, unsafe_allow_html=True)


def render_devnotes_badge(published_dir: Path, within_days: int = 14) -> None:
    """
    상단 상태 스트립 옆에 붙이는 짧은 배지.
    최근 `within_days` 안에 impact=high 변경이 있었으면 한 줄로 알린다.
    """
    data = load_devnotes(str(published_dir))
    if not data:
        return
    for n in (data.get("notes") or []):
        if str(n.get("impact", "")).lower() != "high":
            continue
        try:
            d = date.fromisoformat(str(n.get("date")))
        except Exception:
            continue
        if (date.today() - d).days <= within_days:
            items = []
            for key in ("Fixed", "Changed", "Added"):
                items.extend((n.get("sections") or {}).get(key) or [])
            head = items[0] if items else ""
            st.warning(
                f"최근 변경 [{n.get('version')}] {_days_ago(str(n.get('date')))} — "
                f"{head[:110]}{'…' if len(head) > 110 else ''}",
                icon="⚠️",
            )
            return
