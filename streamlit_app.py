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

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parent
KST = ZoneInfo("Asia/Seoul")
PUBLISHED = ROOT / "published"
STALE_HOURS = 36

DISCLAIMER = "통계 모델의 예측 분포이며 투자 조언이 아닙니다. 투자 판단의 책임은 이용자에게 있습니다."

# ---- 다크 팔레트 ---------------------------------------------------------------------
BG = "rgba(0,0,0,0)"
GRID = "rgba(255,255,255,0.075)"
TEXT = "#aeb9c7"      # Plotly 축/범례용: 본문보다 낮지만 충분히 읽히는 회색
UP = "#f23645"        # 상승 (국내 관행: 빨강)
DOWN = "#2196f3"      # 하락
FCOL = "#f0b90b"      # 예측 (앰버)
DOT = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "⚪"}

# 관세청 월별 수출단가. HBM은 전용 HS코드가 아니라 MCP를 대리지표로 표시한다.
KCS_MEMORY_SERIES = {
    "8542321010": "DRAM",
    "8542321030": "NAND Flash",
    "8542323000": "MCP / HBM proxy",
}
KCS_LOGIC_CODE = "8542311000"

st.set_page_config(
    page_title="주가 예측",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# FINAL UI BUILD — 데이터/계산/판정 로직은 변경하지 않는다.
st.markdown("""
<style>
  :root {
    --bg: #080b10;
    --panel: rgba(18, 23, 31, 0.78);
    --panel-2: rgba(13, 17, 23, 0.72);
    --line: rgba(120,132,148,0.16);
    --line-strong: rgba(120,132,148,0.24);
    --text: #e8edf3;
    --text-soft: #c5ced9;
    --muted: #a1adbb;
    --muted-2: #7f8b99;
    --accent: #f0b90b;
    --blue: #58a6ff;
    --green: #3fb950;
    --red: #f85149;
  }

  #MainMenu, footer, header {visibility: hidden;}

  [data-testid="stAppViewContainer"] {
    background:
      radial-gradient(1100px 420px at 15% -10%, rgba(240,185,11,0.075), transparent 55%),
      radial-gradient(900px 380px at 88% 0%, rgba(88,166,255,0.06), transparent 52%),
      var(--bg);
  }

  .block-container {
    padding-top: 1.15rem;
    padding-bottom: 2.5rem;
    max-width: 1460px;
  }

  /* 상단 헤더 */
  .dash-hero {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 22px;
    padding: 8px 2px 18px 2px;
    border-bottom: 1px solid var(--line);
    margin-bottom: 14px;
  }
  .dash-eyebrow {
    color: var(--accent);
    font-size: 0.72rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    margin-bottom: 5px;
  }
  .dash-title {
    color: var(--text);
    font-size: clamp(1.55rem, 2.1vw, 2.15rem);
    font-weight: 800;
    letter-spacing: -0.04em;
    line-height: 1.1;
  }
  .dash-subtitle {
    color: var(--text-soft);
    font-size: 0.82rem;
    margin-top: 7px;
  }
  .dash-meta {
    color: var(--muted);
    font-size: 0.78rem;
    text-align: right;
    white-space: nowrap;
  }

  /* 섹션 */
  .section-head {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 12px;
    margin: 22px 0 10px 0;
  }
  .section-kicker {
    color: #9ca8b7;
    font-size: 0.68rem;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    margin-bottom: 2px;
  }
  .section-title {
    color: var(--text);
    font-size: 1.05rem;
    font-weight: 750;
    letter-spacing: -0.02em;
  }
  .section-note {
    color: var(--muted);
    font-size: 0.76rem;
  }

  /* Metric 카드 */
  div[data-testid="stMetric"] {
    background: linear-gradient(180deg, rgba(22,27,35,0.9), rgba(13,17,23,0.82));
    border: 1px solid var(--line);
    border-radius: 13px;
    padding: 13px 14px 11px 14px;
    box-shadow: 0 7px 20px rgba(0,0,0,0.13);
    min-height: 94px;
  }
  div[data-testid="stMetric"]:hover {
    border-color: var(--line-strong);
    transform: translateY(-1px);
    transition: 120ms ease;
  }
  [data-testid="stMetricLabel"] {
    color: var(--text-soft) !important;
    font-size: 0.76rem;
    font-weight: 560;
  }
  [data-testid="stMetricValue"] {
    font-size: 1.24rem;
    font-weight: 760;
    letter-spacing: -0.025em;
    color: var(--text);
  }
  [data-testid="stMetricDelta"] {font-size: 0.82rem;}

  /* 컨트롤 */
  div[data-testid="stSelectbox"],
  div[data-testid="stSelectSlider"],
  div[data-testid="stRadio"],
  div[data-testid="stCheckbox"] {
    font-size: 0.86rem;
  }

  /* 종목 선택: 메모리 단가 패널과 같은 어두운 톤으로 통일 */
  div[data-testid="stSelectbox"] {
    background: transparent !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"],
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div,
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div,
  div[data-testid="stSelectbox"] [data-baseweb="select"] input {
    background-color: #0d1117 !important;
    color: var(--text) !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"] > div {
    min-height: 46px !important;
    border: 1px solid var(--line) !important;
    border-radius: 11px !important;
    box-shadow: none !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"] > div:hover {
    background-color: #111720 !important;
    border-color: var(--line-strong) !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus,
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus-within {
    background-color: #111720 !important;
    border-color: rgba(240,185,11,0.28) !important;
    box-shadow: 0 0 0 1px rgba(240,185,11,0.06) !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"] span,
  div[data-testid="stSelectbox"] [data-baseweb="select"] div {
    color: var(--text-soft) !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"] svg {
    color: var(--muted) !important;
    fill: var(--muted) !important;
  }

  div[data-testid="stSelectbox"] label p {
    color: var(--text-soft) !important;
    font-weight: 560;
  }

  /* Streamlit 버전에 따라 실제 입력 박스가 role=combobox 레이어에 그려진다.
     종목명이 표시되는 칸 자체를 완전히 다크 톤으로 고정한다. */
  div[data-testid="stSelectbox"] div[role="combobox"],
  div[data-testid="stSelectbox"] div[role="combobox"] > div,
  div[data-testid="stSelectbox"] div[role="combobox"] span,
  div[data-testid="stSelectbox"] [data-baseweb="select"] *,
  div[data-testid="stSelectbox"] input {
    background-color: #0d1117 !important;
    background-image: none !important;
  }

  div[data-testid="stSelectbox"] div[role="combobox"] {
    color: #d7dee8 !important;
    border-color: rgba(120,132,148,0.18) !important;
    box-shadow: none !important;
    border-radius: 11px !important;
  }

  div[data-testid="stSelectbox"] div[role="combobox"]:hover {
    background-color: #111720 !important;
  }

  div[data-testid="stSelectbox"] div[role="combobox"]:focus,
  div[data-testid="stSelectbox"] div[role="combobox"]:focus-within {
    background-color: #111720 !important;
    border-color: rgba(240,185,11,0.28) !important;
    outline: none !important;
  }

  /* 종목명 텍스트와 화살표는 충분히 보이게 */
  div[data-testid="stSelectbox"] div[role="combobox"] span {
    color: #d7dee8 !important;
  }

  div[data-testid="stSelectbox"] div[role="combobox"] svg {
    color: #a1adbb !important;
    fill: #a1adbb !important;
    background-color: transparent !important;
  }

  /* 드롭다운을 열었을 때 목록도 흰색으로 뜨지 않게 */
  div[data-baseweb="popover"] {
    background: transparent !important;
  }

  div[data-baseweb="popover"] > div,
  div[data-baseweb="menu"],
  ul[role="listbox"] {
    background-color: #0d1117 !important;
    border-color: var(--line-strong) !important;
    color: var(--text-soft) !important;
  }

  li[role="option"] {
    background-color: #0d1117 !important;
    color: var(--text-soft) !important;
  }

  li[role="option"]:hover,
  li[role="option"][aria-selected="true"] {
    background-color: #161b22 !important;
    color: var(--text) !important;
  }

  /* expander summary/header: 흰 막대 제거 */
  div[data-testid="stExpander"] details {
    background: rgba(13,17,23,0.55) !important;
    border-radius: 12px !important;
  }
  div[data-testid="stExpander"] details summary {
    background: rgba(13,17,23,0.90) !important;
    color: var(--text) !important;
    border: 1px solid var(--line) !important;
    border-radius: 12px !important;
  }
  div[data-testid="stExpander"] details[open] summary {
    border-bottom-left-radius: 0 !important;
    border-bottom-right-radius: 0 !important;
    border-bottom-color: rgba(255,255,255,0.06) !important;
  }
  div[data-testid="stExpander"] details summary:hover {
    background: rgba(18,23,31,0.95) !important;
  }
  div[data-testid="stExpander"] details summary p,
  div[data-testid="stExpander"] details summary span,
  div[data-testid="stExpander"] details summary svg {
    color: var(--text) !important;
    fill: var(--text) !important;
  }
  div[role="radiogroup"] {
    gap: 6px;
  }
  div[role="radiogroup"] label {
    background: rgba(13,17,23,0.72) !important;
    border: 1px solid var(--line) !important;
    border-radius: 9px;
    padding: 5px 10px;
    color: var(--muted) !important;
    transition: 120ms ease;
  }
  div[role="radiogroup"] label:hover {
    background: rgba(255,255,255,0.035) !important;
    border-color: var(--line-strong) !important;
  }
  /* 선택된 예측기간: 흰색 대신 어두운 앰버 톤 */
  div[role="radiogroup"] label:has(input:checked) {
    background: rgba(240,185,11,0.10) !important;
    border-color: rgba(240,185,11,0.32) !important;
    color: var(--text) !important;
    box-shadow: inset 0 0 0 1px rgba(240,185,11,0.05);
  }
  div[data-testid="stRadio"] input[type="radio"] {
    accent-color: #f0b90b !important;
  }

  /* Streamlit 탭도 흰색 면이 뜨지 않도록 같은 톤으로 통일 */
  .stTabs [data-baseweb="tab-list"] {
    gap: 5px;
    background: rgba(13,17,23,0.58);
    border: 1px solid var(--line);
    border-radius: 11px;
    padding: 4px;
  }
  .stTabs button[data-baseweb="tab"] {
    background: transparent !important;
    color: var(--muted) !important;
    border-radius: 8px;
    padding-left: 14px;
    padding-right: 14px;
  }
  .stTabs button[data-baseweb="tab"]:hover {
    background: rgba(255,255,255,0.035) !important;
    color: var(--text) !important;
  }
  .stTabs button[data-baseweb="tab"][aria-selected="true"] {
    background: rgba(240,185,11,0.10) !important;
    color: var(--text) !important;
  }
  .stTabs [data-baseweb="tab-highlight"] {
    background-color: #f0b90b !important;
    height: 2px !important;
  }
  .stTabs [data-baseweb="tab-border"] {
    background-color: transparent !important;
  }

  /* 판정 카드 */
  .verdict {
    border: 1px solid var(--line);
    border-left: 4px solid #6e7681;
    border-radius: 11px;
    background: rgba(22,27,35,0.72);
    padding: 11px 14px;
    color: #d0d8e2;
    font-size: 0.91rem;
    line-height: 1.55;
    margin: 8px 0 14px 0;
  }
  .verdict.high { border-left-color: var(--green); }
  .verdict.medium { border-left-color: var(--accent); }
  .verdict.low { border-left-color: #6e7681; }

  /* Expander / table */
  div[data-testid="stExpander"] {
    border: 1px solid var(--line);
    border-radius: 12px;
    background: rgba(13,17,23,0.55) !important;
    overflow: hidden;
    margin-top: 8px;
  }
  div[data-testid="stDataFrame"] {
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
  }

  /* Plotly 영역 */
  div[data-testid="stPlotlyChart"] {
    border: 1px solid var(--line);
    border-radius: 14px;
    background: rgba(13,17,23,0.42);
    padding: 2px;
    overflow: hidden;
  }

  /* Plotly SVG 보조 텍스트 대비. 실제 legend 색은 figure 설정에서 별도 지정한다. */
  div[data-testid="stPlotlyChart"] .xtick text,
  div[data-testid="stPlotlyChart"] .ytick text {
    fill: #aeb9c7 !important;
  }

  .micro-status {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--text-soft);
    font-size: 0.76rem;
    padding: 5px 8px;
    border: 1px solid var(--line);
    border-radius: 999px;
    background: rgba(13,17,23,0.58);
  }

  /* 스냅샷/현재가/진단 가용성 — 상단에서 한눈에 확인 */
  .status-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
    margin: 2px 0 14px 0;
  }
  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    border: 1px solid var(--line);
    border-radius: 999px;
    background: rgba(13,17,23,0.64);
    color: var(--text-soft);
    padding: 6px 10px;
    font-size: 0.74rem;
    line-height: 1;
  }
  .status-pill b {
    color: var(--text);
    font-weight: 700;
  }
  .status-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 0 3px rgba(63,185,80,0.08);
  }
  .status-dot.warn {
    background: var(--accent);
    box-shadow: 0 0 0 3px rgba(240,185,11,0.08);
  }

  /* 실제 학습 feature importance Top 10 */
  .feature-list {
    display: grid;
    gap: 6px;
    margin: 7px 0 6px 0;
  }
  .feature-row {
    display: grid;
    grid-template-columns: 28px minmax(155px, 1.05fr) minmax(210px, 1.55fr) minmax(105px, 0.7fr) 78px;
    align-items: center;
    gap: 9px;
    min-height: 34px;
    padding: 5px 8px;
    border: 1px solid rgba(120,132,148,0.12);
    border-radius: 8px;
    background: rgba(13,17,23,0.76);
  }
  .feature-rank {
    color: var(--muted-2);
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
    text-align: right;
  }
  .feature-name {
    color: #dde4ec;
    font-size: 0.77rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    overflow-wrap: anywhere;
  }
  .feature-meaning {
    color: var(--text-soft);
    font-size: 0.74rem;
    line-height: 1.38;
    overflow-wrap: anywhere;
  }
  .feature-track {
    height: 6px;
    border-radius: 999px;
    background: rgba(120,132,148,0.13);
    overflow: hidden;
  }
  .feature-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, rgba(240,185,11,0.52), rgba(240,185,11,0.95));
  }
  .feature-score {
    color: var(--text-soft);
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
    text-align: right;
  }

  hr { border-color: var(--line) !important; }

  /* -----------------------------------------------------------------
     읽기성: 흰색으로 번쩍이지 않으면서 보조 텍스트를 충분히 띄운다.
     ----------------------------------------------------------------- */
  [data-testid="stAppViewContainer"] {
    color: var(--text);
  }

  /* Streamlit caption이 기본 테마에서 너무 어두워지는 문제 보정 */
  [data-testid="stCaptionContainer"],
  [data-testid="stCaptionContainer"] p,
  .stCaption,
  .stCaption p {
    color: var(--muted) !important;
    opacity: 1 !important;
    line-height: 1.5;
  }

  /* 일반 안내 문구는 제목보다 낮고 caption보다 살짝 밝게 */
  div[data-testid="stMarkdownContainer"] > p {
    color: var(--text-soft);
  }

  /* 입력 컨트롤 라벨 */
  div[data-testid="stSelectbox"] > label p,
  div[data-testid="stSelectSlider"] > label p,
  div[data-testid="stSlider"] > label p,
  div[data-testid="stRadio"] > label p,
  div[data-testid="stCheckbox"] label p,
  div[data-testid="stCheckbox"] label span {
    color: var(--text-soft) !important;
    opacity: 1 !important;
  }

  /* 라디오 비선택 텍스트도 너무 죽지 않게 */
  div[role="radiogroup"] label,
  div[role="radiogroup"] label p,
  div[role="radiogroup"] label span {
    color: var(--muted) !important;
    opacity: 1 !important;
  }
  div[role="radiogroup"] label:has(input:checked),
  div[role="radiogroup"] label:has(input:checked) p,
  div[role="radiogroup"] label:has(input:checked) span {
    color: var(--text) !important;
  }

  /* 체크박스 문구와 help 아이콘 */
  div[data-testid="stCheckbox"] svg,
  [data-testid="stTooltipHoverTarget"] svg {
    color: var(--muted) !important;
    fill: var(--muted) !important;
  }

  /* 슬라이더: 빨간 기본 테마보다 대시보드의 앰버 포인트와 맞춘다 */
  div[data-testid="stSlider"] [data-baseweb="slider"] div[role="slider"] {
    background-color: var(--accent) !important;
    border-color: var(--accent) !important;
  }
  div[data-testid="stSlider"] [data-baseweb="slider"] div {
    color: var(--text-soft);
  }

  /* Expander 안 설명이 배경에 묻히지 않도록 */
  div[data-testid="stExpander"] [data-testid="stMarkdownContainer"] p {
    color: var(--muted) !important;
  }
  div[data-testid="stExpander"] details summary p,
  div[data-testid="stExpander"] details summary span {
    color: var(--text-soft) !important;
    font-weight: 600;
  }

  /* 상태/경고 박스 텍스트는 명도만 확보하고 배경색은 기존 유지 */
  div[data-testid="stAlert"] p,
  div[data-testid="stAlert"] span {
    color: var(--text-soft) !important;
  }

  /* 데이터프레임 위/아래의 작은 레이블 */
  div[data-testid="stDataFrame"] + div,
  div[data-testid="stDataFrame"] ~ div[data-testid="stCaptionContainer"] {
    color: var(--muted) !important;
  }


  /* Streamlit help / tooltip 아이콘 — 다크 테마에 맞는 은은한 골드 */
  [data-testid="stTooltipIcon"] {
    opacity: 1 !important;
  }

  [data-testid="stTooltipIcon"] svg {
    color: #d9a90d !important;
    fill: #d9a90d !important;
    width: 0.92rem !important;
    height: 0.92rem !important;
    transition: color 0.16s ease, fill 0.16s ease, filter 0.16s ease,
                transform 0.16s ease;
  }

  [data-testid="stTooltipIcon"]:hover svg {
    color: #ffd54a !important;
    fill: #ffd54a !important;
    filter: drop-shadow(0 0 4px rgba(240,185,11,0.38));
    transform: translateY(-1px);
  }

  /* Streamlit 버전에 따라 help 아이콘이 button 안에 렌더링되는 경우까지 대응 */
  button[aria-label*="help" i] svg,
  button[aria-label*="tooltip" i] svg,
  button[aria-label*="도움" i] svg {
    color: #d9a90d !important;
    fill: #d9a90d !important;
    transition: color 0.16s ease, fill 0.16s ease, filter 0.16s ease;
  }

  button[aria-label*="help" i]:hover svg,
  button[aria-label*="tooltip" i]:hover svg,
  button[aria-label*="도움" i]:hover svg {
    color: #ffd54a !important;
    fill: #ffd54a !important;
    filter: drop-shadow(0 0 4px rgba(240,185,11,0.38));
  }

  /* 도움말 팝오버도 배경/테두리를 대시보드 톤에 맞춤 */
  [data-baseweb="popover"] > div,
  [role="tooltip"] {
    background: #11161d !important;
    color: #e8edf3 !important;
    border: 1px solid rgba(240,185,11,0.28) !important;
    border-radius: 9px !important;
    box-shadow: 0 10px 28px rgba(0,0,0,0.34) !important;
  }

  @media (max-width: 850px) {
    .dash-hero {align-items: flex-start; flex-direction: column;}
    .dash-meta {text-align: left; white-space: normal;}
    .block-container {padding-left: 0.9rem; padding-right: 0.9rem;}
    .feature-row {
      grid-template-columns: 24px minmax(125px, 0.9fr) minmax(150px, 1.35fr) 64px;
    }
    .feature-track {display: none;}
  }

  /* ===============================================================
     최종 border override
     Streamlit/BaseWeb가 상태별로 넣는 흰 테두리/outline을 제거한다.
     =============================================================== */
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div,
  div[data-testid="stSelectbox"] div[role="combobox"] {
    border: 1px solid rgba(120,132,148,0.18) !important;
    outline: none !important;
    box-shadow: none !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"] > div:hover,
  div[data-testid="stSelectbox"] div[role="combobox"]:hover {
    border-color: rgba(120,132,148,0.28) !important;
    outline: none !important;
    box-shadow: none !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus,
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus-within,
  div[data-testid="stSelectbox"] div[role="combobox"]:focus,
  div[data-testid="stSelectbox"] div[role="combobox"]:focus-within {
    border-color: rgba(240,185,11,0.24) !important;
    outline: none !important;
    box-shadow: 0 0 0 1px rgba(240,185,11,0.035) !important;
  }

  div[data-testid="stExpander"],
  div[data-testid="stExpander"] details,
  div[data-testid="stExpander"] details summary,
  div[data-testid="stMetric"],
  div[data-testid="stPlotlyChart"],
  div[data-testid="stDataFrame"] {
    border-color: rgba(120,132,148,0.16) !important;
  }

  div[data-testid="stExpander"] details summary:hover {
    border-color: rgba(120,132,148,0.24) !important;
  }

  /* 브라우저/테마 기본 focus ring 제거 */
  *:focus-visible {
    outline-color: rgba(240,185,11,0.22) !important;
  }


  /* ===============================================================
     v9: 종목 선택 박스는 테두리 자체를 없앤다.
     색을 어둡게 바꾸는 게 아니라 border/outline/focus ring을 전부 제거.
     =============================================================== */
  div[data-testid="stSelectbox"],
  div[data-testid="stSelectbox"] [data-baseweb="select"],
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div,
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div,
  div[data-testid="stSelectbox"] div[role="combobox"] {
    border: 0 !important;
    border-width: 0 !important;
    border-color: transparent !important;
    outline: 0 !important;
    outline-offset: 0 !important;
    box-shadow: none !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"] {
    background: #0d1117 !important;
    border-radius: 11px !important;
    overflow: hidden !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"] > div,
  div[data-testid="stSelectbox"] div[role="combobox"] {
    background: #0d1117 !important;
    min-height: 46px !important;
    border-radius: 11px !important;
  }

  /* 내부 BaseWeb 레이어가 자체 border를 다시 만드는 경우까지 제거 */
  div[data-testid="stSelectbox"] [data-baseweb="select"] * {
    outline: none !important;
    box-shadow: none !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"] > div:hover,
  div[data-testid="stSelectbox"] div[role="combobox"]:hover {
    background: #111720 !important;
    border: 0 !important;
    outline: 0 !important;
    box-shadow: none !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus,
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus-within,
  div[data-testid="stSelectbox"] div[role="combobox"]:focus,
  div[data-testid="stSelectbox"] div[role="combobox"]:focus-within {
    background: #111720 !important;
    border: 0 !important;
    outline: 0 !important;
    box-shadow: none !important;
  }

  /* 브라우저 접근성 focus ring도 selectbox에는 흰색으로 나오지 않게 */
  div[data-testid="stSelectbox"] *:focus,
  div[data-testid="stSelectbox"] *:focus-visible {
    border: 0 !important;
    outline: 0 !important;
    box-shadow: none !important;
  }

  /* 테두리 대신 배경 차이만으로 박스를 구분 */
  div[data-testid="stSelectbox"] [data-baseweb="select"] {
    box-shadow: inset 0 0 0 0 transparent !important;
  }


  /* ===============================================================
     v10: 종목 선택 우측 화살표 칸까지 완전 다크 처리
     스크린샷에서 남아 있던 흰색 사각형은 BaseWeb select의
     우측 indicator container 레이어에서 발생한다.
     =============================================================== */
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div:last-child,
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div:last-child > div,
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div:last-child span,
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div:last-child button,
  div[data-testid="stSelectbox"] [data-baseweb="select"] [data-baseweb="icon"],
  div[data-testid="stSelectbox"] [data-baseweb="select"] svg {
    background: #0d1117 !important;
    background-color: #0d1117 !important;
    border: 0 !important;
    outline: 0 !important;
    box-shadow: none !important;
  }

  /* 우측 indicator 영역이 별도 flex item일 때 */
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div:last-of-type {
    background: #0d1117 !important;
    background-color: #0d1117 !important;
    border-left: 0 !important;
  }

  /* 화살표 자체는 밝은 회색 */
  div[data-testid="stSelectbox"] [data-baseweb="select"] svg,
  div[data-testid="stSelectbox"] [data-baseweb="select"] svg path {
    color: #aeb9c7 !important;
    fill: #aeb9c7 !important;
  }

  /* 전체 선택칸에 남아 있는 외곽선도 완전히 제거 */
  div[data-testid="stSelectbox"] [data-baseweb="select"],
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div,
  div[data-testid="stSelectbox"] div[role="combobox"] {
    border: none !important;
    outline: none !important;
    box-shadow: none !important;
  }

  /* 선택칸 전체를 하나의 동일한 배경으로 보이게 */
  div[data-testid="stSelectbox"] [data-baseweb="select"],
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div,
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div {
    background-color: #0d1117 !important;
  }

  div[data-testid="stSelectbox"] [data-baseweb="select"]:hover,
  div[data-testid="stSelectbox"] [data-baseweb="select"]:hover > div,
  div[data-testid="stSelectbox"] [data-baseweb="select"]:hover > div > div {
    background-color: #111720 !important;
  }


  /* ===============================================================
     v11: 종목 선택 텍스트 가독성 강화
     선택된 종목명은 주요 정보이므로 보조 회색이 아니라 밝은 본문색 사용.
     =============================================================== */
  div[data-testid="stSelectbox"] [data-baseweb="select"] span,
  div[data-testid="stSelectbox"] [data-baseweb="select"] div[role="combobox"],
  div[data-testid="stSelectbox"] [data-baseweb="select"] div[role="combobox"] span,
  div[data-testid="stSelectbox"] [data-baseweb="select"] input {
    color: #e8edf3 !important;
    -webkit-text-fill-color: #e8edf3 !important;
    opacity: 1 !important;
    font-weight: 620 !important;
  }

  /* 종목 선택 label은 본문보다 한 단계 낮게 */
  div[data-testid="stSelectbox"] > label p {
    color: #b8c2cf !important;
    opacity: 1 !important;
    font-weight: 560 !important;
  }

  /* 드롭다운 목록 안 종목명도 동일하게 읽히도록 */
  ul[role="listbox"] li[role="option"],
  ul[role="listbox"] li[role="option"] span,
  div[data-baseweb="menu"] li[role="option"],
  div[data-baseweb="menu"] li[role="option"] span {
    color: #d7dee8 !important;
    -webkit-text-fill-color: #d7dee8 !important;
    opacity: 1 !important;
    font-weight: 540 !important;
  }

  ul[role="listbox"] li[role="option"]:hover,
  ul[role="listbox"] li[role="option"][aria-selected="true"] {
    color: #f0f3f7 !important;
  }


  /* v12 fallback: selected selectbox value / placeholder text */
  div[data-testid="stSelectbox"] input,
  div[data-testid="stSelectbox"] input::placeholder,
  div[data-testid="stSelectbox"] [data-baseweb="select"] input,
  div[data-testid="stSelectbox"] [data-baseweb="select"] input::placeholder {
    color: #e8edf3 !important;
    -webkit-text-fill-color: #e8edf3 !important;
    opacity: 1 !important;
    font-weight: 600 !important;
  }


  /* ===============================================================
     v13: 상세 표를 대시보드 카드와 동일한 다크 톤으로 통일
     st.dataframe의 밝은 Glide 테마 대신 직접 렌더링하는 읽기 전용 표.
     =============================================================== */
  .dash-table-wrap {
    width: 100%;
    overflow-x: auto;
    border: 1px solid rgba(120,132,148,0.16);
    border-radius: 10px;
    background: #0d1117;
    margin: 2px 0 7px 0;
  }

  table.dash-table {
    width: 100%;
    border-collapse: collapse;
    border-spacing: 0;
    background: #0d1117;
    color: #d7dee8;
    font-size: 0.79rem;
    line-height: 1.35;
  }

  table.dash-table thead th {
    background: #111720;
    color: #bfc9d6;
    font-weight: 650;
    text-align: left;
    padding: 9px 10px;
    border-bottom: 1px solid rgba(120,132,148,0.20);
    white-space: nowrap;
  }

  table.dash-table tbody td {
    background: #0d1117;
    color: #d7dee8;
    padding: 8px 10px;
    border-bottom: 1px solid rgba(120,132,148,0.11);
    vertical-align: middle;
  }

  table.dash-table tbody tr:last-child td {
    border-bottom: 0;
  }

  table.dash-table tbody tr:hover td {
    background: #111720;
  }

  table.dash-table td + td,
  table.dash-table th + th {
    border-left: 1px solid rgba(120,132,148,0.08);
  }

  /* 숫자/값 열은 살짝 더 밝게 */
  table.dash-table tbody td:not(:first-child) {
    color: #e4e9ef;
  }


  /* ===============================================================
     FINAL POLISH
     기능/데이터 요소는 유지하고, 계층·간격·가독성·반응형만 최종 정리.
     =============================================================== */

  html {
    scrollbar-color: #2a323d #080b10;
    scrollbar-width: thin;
  }
  * {
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }
  ::selection {
    background: rgba(240,185,11,0.24);
    color: #ffffff;
  }

  .block-container {
    padding-left: clamp(0.9rem, 2vw, 2.1rem);
    padding-right: clamp(0.9rem, 2vw, 2.1rem);
  }

  .dash-hero {
    padding-top: 10px;
    padding-bottom: 19px;
    margin-bottom: 12px;
  }
  .dash-title {
    text-shadow: 0 1px 18px rgba(255,255,255,0.025);
  }
  .dash-meta {
    line-height: 1.55;
    font-variant-numeric: tabular-nums;
  }

  .section-head {
    position: relative;
    padding-left: 11px;
    margin-top: 26px;
    margin-bottom: 11px;
  }
  .section-head::before {
    content: "";
    position: absolute;
    left: 0;
    top: 3px;
    bottom: 3px;
    width: 2px;
    border-radius: 2px;
    background: linear-gradient(180deg, rgba(240,185,11,0.95), rgba(240,185,11,0.18));
  }
  .section-title {
    font-size: 1.08rem;
  }
  .section-note {
    line-height: 1.4;
  }

  .status-strip {
    margin-top: 1px;
    margin-bottom: 17px;
  }
  .status-pill {
    padding: 7px 11px;
    background: linear-gradient(180deg, rgba(18,23,31,0.76), rgba(13,17,23,0.72));
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.018);
  }

  div[data-testid="stMetric"] {
    position: relative;
    min-height: 96px;
    padding: 13px 14px 12px 14px;
    transition: border-color 130ms ease, background 130ms ease, box-shadow 130ms ease;
  }
  div[data-testid="stMetric"]::before {
    content: "";
    position: absolute;
    left: 13px;
    right: 13px;
    top: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, rgba(240,185,11,0.18), transparent);
  }
  div[data-testid="stMetric"]:hover {
    transform: none;
    background: linear-gradient(180deg, rgba(24,30,39,0.93), rgba(13,17,23,0.85));
    border-color: rgba(120,132,148,0.25) !important;
    box-shadow: 0 9px 25px rgba(0,0,0,0.16);
  }
  [data-testid="stMetricValue"],
  [data-testid="stMetricDelta"] {
    font-variant-numeric: tabular-nums;
  }
  [data-testid="stMetricLabel"] {
    line-height: 1.25;
  }

  div[role="radiogroup"] label {
    min-height: 34px;
    align-items: center;
  }
  div[data-testid="stCheckbox"] label {
    min-height: 32px;
  }
  div[data-testid="stSlider"],
  div[data-testid="stSelectSlider"] {
    padding-top: 1px;
  }

  div[data-testid="stExpander"] details[open] {
    background: rgba(13,17,23,0.64) !important;
    box-shadow: 0 8px 24px rgba(0,0,0,0.10);
  }
  div[data-testid="stExpander"] details > div {
    padding-top: 5px;
  }
  div[data-testid="stExpander"] details summary {
    min-height: 44px;
  }

  div[data-testid="stPlotlyChart"] {
    background: linear-gradient(180deg, rgba(13,17,23,0.55), rgba(8,11,16,0.34));
    box-shadow: 0 8px 28px rgba(0,0,0,0.10);
  }

  .dash-table-wrap {
    box-shadow: 0 7px 20px rgba(0,0,0,0.08);
  }
  table.dash-table {
    font-variant-numeric: tabular-nums;
  }
  table.dash-table thead th {
    position: sticky;
    top: 0;
    z-index: 1;
    letter-spacing: -0.01em;
  }
  table.dash-table tbody td {
    line-height: 1.48;
  }
  table.dash-table tbody td:first-child {
    color: #c7d0dc;
    font-weight: 580;
  }

  .feature-head {
    display: grid;
    grid-template-columns: 28px minmax(155px, 1.05fr) minmax(210px, 1.55fr) minmax(105px, 0.7fr) 78px;
    gap: 9px;
    align-items: center;
    padding: 0 8px 4px 8px;
    color: var(--muted-2);
    font-size: 0.66rem;
    font-weight: 700;
    letter-spacing: 0.035em;
    text-transform: uppercase;
  }
  .feature-head > div:first-child,
  .feature-head > div:last-child {
    text-align: right;
  }
  .feature-row {
    min-height: 42px;
    padding-top: 7px;
    padding-bottom: 7px;
    transition: background 110ms ease, border-color 110ms ease;
  }
  .feature-row:hover {
    background: rgba(18,23,31,0.90);
    border-color: rgba(120,132,148,0.21);
  }
  .feature-row:nth-child(1) .feature-rank,
  .feature-row:nth-child(2) .feature-rank,
  .feature-row:nth-child(3) .feature-rank {
    color: #d9a90d;
    font-weight: 750;
  }
  .feature-name {
    font-size: 0.755rem;
  }
  .feature-meaning {
    color: #b9c4d1;
  }
  .feature-score {
    color: #cdd6e1;
  }

  .verdict {
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.016);
  }
  .verdict b {
    color: #edf2f7;
  }

  [data-testid="stCaptionContainer"] p {
    max-width: 1180px;
  }

  hr {
    margin-top: 1.65rem !important;
    margin-bottom: 1.15rem !important;
    opacity: 0.78;
  }

  @media (max-width: 980px) {
    .feature-head,
    .feature-row {
      grid-template-columns: 26px minmax(135px, 0.95fr) minmax(190px, 1.45fr) 72px;
    }
    .feature-head > div:nth-child(4),
    .feature-track {
      display: none;
    }
  }

  @media (max-width: 700px) {
    .dash-title {
      font-size: 1.58rem;
    }
    .dash-subtitle {
      font-size: 0.78rem;
    }
    .section-head {
      align-items: flex-start;
      flex-direction: column;
      gap: 3px;
    }
    .status-pill {
      font-size: 0.70rem;
    }
    .feature-head {
      display: none;
    }
    .feature-row {
      grid-template-columns: 22px minmax(112px, 0.8fr) minmax(155px, 1.25fr) 62px;
      gap: 7px;
      padding-left: 6px;
      padding-right: 6px;
    }
    .feature-name {
      font-size: 0.70rem;
    }
    .feature-meaning {
      font-size: 0.69rem;
    }
    .feature-score {
      font-size: 0.68rem;
    }
    div[data-testid="stMetric"] {
      min-height: 88px;
    }
  }

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
    # 종목코드 005930 이 정수로 읽히면 앞의 0 이 사라진다 (히스토리 조회 실패)
    df = pd.read_csv(cpath, dtype={"symbol": str, "confidence_grade": str,
                                   "country": str, "currency": str})
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
def load_kcs_memory() -> Optional[pd.DataFrame]:
    """publish.py가 올린 관세청 메모리 월별 수출단가 스냅샷을 읽는다."""
    path = PUBLISHED / "kcs_memory_prices.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, dtype={"hs_code": str}, encoding="utf-8-sig")
    except (OSError, pd.errors.ParserError, UnicodeDecodeError):
        return None

    required = {"period", "hs_code", "series", "export_unit_price_weight"}
    if not required.issubset(df.columns):
        return None

    df["hs_code"] = df["hs_code"].astype(str).str.strip()
    df["date"] = pd.to_datetime(df["period"].astype(str) + "-01", errors="coerce")
    df["export_unit_price_weight"] = pd.to_numeric(
        df["export_unit_price_weight"], errors="coerce"
    )
    if "export_value" in df.columns:
        df["export_value"] = pd.to_numeric(df["export_value"], errors="coerce")
    if "export_weight" in df.columns:
        df["export_weight"] = pd.to_numeric(df["export_weight"], errors="coerce")
    df = df.dropna(subset=["date", "export_unit_price_weight"])
    return df.sort_values(["date", "series"]).reset_index(drop=True)


@st.cache_data(ttl=300, show_spinner=False)
def load_history(symbol: str) -> Optional[pd.DataFrame]:
    path = PUBLISHED / "history" / f"{symbol}.csv"
    if not path.exists():
        return None
    return pd.read_csv(path, parse_dates=["date"]).sort_values("date").reset_index(drop=True)


@st.cache_data(ttl=300, show_spinner=False)
def load_track() -> Dict:
    """track.py 가 만든 라이브 검증 성적. 없으면 빈 dict."""
    path = PUBLISHED / "track_summary.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


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


def section_head(kicker: str, title: str, note: str = "") -> None:
    """일관된 섹션 헤더. 표시 계층만 담당한다."""
    note_html = f"<div class='section-note'>{note}</div>" if note else ""
    st.markdown(
        f"""
        <div class="section-head">
          <div>
            <div class="section-kicker">{kicker}</div>
            <div class="section-title">{title}</div>
          </div>
          {note_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_dark_table(df: pd.DataFrame) -> None:
    """작은 진단/레벨 표를 대시보드 다크 톤으로 렌더링한다."""
    if df is None or df.empty:
        return
    html = df.to_html(
        index=False,
        escape=True,
        border=0,
        classes="dash-table",
    )
    st.markdown(
        f'<div class="dash-table-wrap">{html}</div>',
        unsafe_allow_html=True,
    )




def feature_meaning(feature_name: str) -> str:
    """
    현재 프로젝트의 feature naming convention을 사람이 읽기 쉽게 설명한다.
    모르는 이름은 억지로 추정하지 않고 일반 설명으로 남긴다.
    """
    n = str(feature_name).strip()
    low = n.lower()

    # ---- 관세청 메모리 사이클 ----
    if low.startswith("kcs_"):
        product = "메모리 품목"
        for key, label in {
            "dram": "DRAM", "nand": "NAND", "mcp": "MCP(HBM 포함 가능)",
            "logic": "Logic IC",
        }.items():
            if f"kcs_{key}_" in low:
                product = label
                break

        if low == "kcs_mcp_share":
            return "MCP 수출금액이 MCP+DRAM 수출금액에서 차지하는 비중. HBM 전환 흐름의 대리지표"
        if low == "kcs_mcp_share_chg6":
            return "MCP 수출금액 비중의 6개월 변화. HBM/MCP 쪽 믹스 변화 속도를 보는 대리지표"
        if "dram_nand_ratio_yoy" in low:
            return "DRAM/NAND 수출단가 비율의 전년 대비 변화. 두 메모리 품목의 상대적 가격 강도"
        if "dram_nand_ratio_z12" in low:
            return "DRAM/NAND 단가 비율이 최근 12개월 평균에서 얼마나 벗어났는지 나타내는 Z-score"
        if "dram_logic_ratio_yoy" in low:
            return "DRAM/Logic 수출단가 비율의 전년 대비 변화. 메모리와 일반 로직 IC의 상대 가격 강도"
        if "dram_logic_ratio_z12" in low:
            return "DRAM/Logic 단가 비율의 최근 12개월 표준화 위치"
        if "_price_mom" in low:
            return f"{product} 수출단가의 전월 대비 변화율(MoM)"
        if "_price_qoq" in low:
            return f"{product} 수출단가의 3개월 전 대비 변화율(QoQ)"
        if "_price_yoy" in low:
            return f"{product} 수출단가의 12개월 전 대비 변화율(YoY)"
        if "_price_z12" in low:
            return f"{product} 수출단가가 최근 12개월 평균에서 얼마나 벗어났는지 나타내는 Z-score"
        if "_value_yoy" in low:
            return f"{product} 수출금액의 전년 대비 변화율. 가격뿐 아니라 수요·출하 규모 변화도 일부 반영"
        if "_up_streak" in low:
            return f"{product} 수출단가가 연속으로 상승한 개월 수. 업황 상승세의 지속성 지표"
        return "관세청 월별 수출실적에서 만든 메모리/반도체 사이클 파생지표"

    # ---- 수익률 / 가격 위치 ----
    m = re.search(r"(?:^|_)ret_(\d+)(?:d)?(?:$|_)", low)
    if m:
        return f"최근 {m.group(1)}거래일 가격 수익률. 단기·중기 가격 모멘텀을 나타냄"
    if "log_ret" in low:
        return "로그수익률. 기간별 가격 변화율을 시계열 모델이 다루기 쉬운 형태로 변환한 값"
    if "overnight" in low:
        return "전일 종가에서 당일 시가까지의 야간(갭) 수익률"
    if "intraday" in low:
        return "당일 시가에서 종가까지의 장중 수익률"
    if low in {"gap", "gap1"} or "gap_ret" in low:
        return "전일 종가 대비 당일 시가의 갭 변화율"
    if "52w" in low or "range_pos252" in low:
        return "최근 약 52주 가격 범위에서 현재 가격이 어느 위치에 있는지 나타냄"
    if "range_pos" in low:
        nums = re.findall(r"\d+", low)
        return f"최근 {nums[-1]}일 가격 범위에서 현재 가격의 상대적 위치" if nums else "최근 가격 범위 내 현재 위치"

    # ---- 추세 / 기술적 지표 ----
    if "dist_sma" in low or "price_sma" in low or "over_sma" in low:
        nums = re.findall(r"\d+", low)
        d = nums[-1] if nums else "해당"
        return f"현재 가격이 {d}일 단순이동평균(SMA)에서 얼마나 위/아래 떨어져 있는지"
    if "dist_ema" in low or "over_ema" in low:
        nums = re.findall(r"\d+", low)
        d = nums[-1] if nums else "해당"
        return f"현재 가격이 {d}일 지수이동평균(EMA)에서 얼마나 이격돼 있는지"
    if "sma_slope" in low or "ma_slope" in low:
        return "이동평균선의 기울기. 추세의 방향과 속도를 수치화한 값"
    if "macd_hist" in low or "macd_diff" in low:
        return "MACD와 시그널선의 차이. 추세 모멘텀이 강화·약화되는 정도"
    if "macd_signal" in low:
        return "MACD의 시그널선. MACD 추세 신호를 부드럽게 만든 기준선"
    if "macd" in low:
        return "단기·장기 지수이동평균 차이로 만든 MACD 추세 모멘텀"
    if "stoch" in low and "rsi" in low:
        return "RSI가 최근 범위에서 어느 위치인지 보는 Stochastic RSI. 단기 과열·침체 정도"
    if "rsi" in low:
        return "RSI. 최근 상승폭과 하락폭을 비교해 과열·침체 및 모멘텀을 나타내는 지표"
    if "cci" in low:
        return "CCI. 가격이 최근 평균 수준에서 얼마나 이탈했는지 보는 모멘텀/평균회귀 지표"
    if "williams" in low:
        return "Williams %R. 최근 고가·저가 범위 안에서 종가 위치를 이용한 과열·침체 지표"
    if "bb_" in low or "bollinger" in low:
        if "width" in low:
            return "볼린저밴드 폭. 최근 가격 변동성이 커졌는지 작아졌는지 나타냄"
        return "볼린저밴드 안에서 현재 가격의 상대적 위치. 과열·평균회귀 상태를 나타냄"

    # ---- 변동성 / 위험 ----
    if "parkinson" in low:
        return "고가와 저가 범위를 이용한 Parkinson 변동성. 종가만 쓴 변동성보다 일중 정보를 더 반영"
    if "garman" in low or "gk_vol" in low:
        return "시가·고가·저가·종가를 함께 이용한 Garman–Klass 변동성 추정치"
    if "atr" in low:
        return "ATR 기반 변동폭을 가격 대비 비율로 본 값. 최근 실제 가격 움직임의 크기"
    if "down_vol" in low or "downvol" in low:
        return "하락 수익률에 초점을 둔 하방 변동성. 손실 쪽 위험의 크기"
    if "drawdown" in low:
        return "최근 고점 대비 현재/최근 가격의 낙폭. 하락 위험과 회복 정도를 나타냄"
    if "skew" in low:
        return "수익률 분포의 왜도. 상승·하락 꼬리가 어느 쪽으로 더 치우쳤는지"
    if "kurt" in low:
        return "수익률 분포의 첨도. 극단적 가격변동이 얼마나 자주 나타나는지"
    if "tail_loss" in low:
        return "최근 구간에서 나타난 큰 하락 꼬리의 크기"
    if "tail_gain" in low:
        return "최근 구간에서 나타난 큰 상승 꼬리의 크기"
    if "realized_vol" in low or re.search(r"(?:^|_)vol_\d+", low) or re.search(r"(?:^|_)vol\d+", low):
        nums = re.findall(r"\d+", low)
        d = nums[-1] if nums else "최근"
        return f"{d}일 수익률로 계산한 실현 변동성. 가격 움직임의 불확실성 크기"
    if "vol_ratio" in low:
        return "단기 변동성과 중기 변동성의 비율. 변동성이 최근 급격히 확대·축소됐는지"

    # ---- 거래량 / 유동성 ----
    if "amihud" in low:
        return "Amihud 비유동성. 적은 거래대금으로 가격이 크게 움직일수록 값이 커지는 유동성 위험 지표"
    if "obv" in low:
        return "OBV(On-Balance Volume) 기반 지표. 가격 방향에 따라 누적한 거래량으로 매수·매도 압력을 추정"
    if "mfi" in low:
        return "MFI(Money Flow Index). 가격과 거래량을 함께 이용해 자금 유입·유출 강도를 측정"
    if "cmf" in low:
        return "CMF(Chaikin Money Flow). 종가 위치와 거래량을 이용한 매집·분산 압력"
    if "volume" in low or "volu_" in low or "dollar_vol" in low:
        if "z" in low:
            return "현재 거래량이 최근 평균보다 얼마나 이례적으로 많은지/적은지 나타내는 Z-score"
        if "dollar" in low:
            return "가격×거래량 기준 거래대금의 변화. 시장 참여와 유동성 규모를 나타냄"
        return "최근 거래량의 수준·변화·추세를 나타내는 시장 참여도 지표"

    # ---- 시장 / 상대강도 / 금리 / 환율 ----
    if low.startswith("fx_") or "usdkrw" in low or "krw" in low and ("chg" in low or "ret" in low):
        return "달러/원 환율의 수준·변화·모멘텀/변동성. 원화와 글로벌 위험선호 변화의 영향을 반영"
    if "kospi" in low or "kosdaq" in low:
        return "한국 주가지수의 수익률·추세·변동성. 개별 종목이 놓인 국내 시장 환경을 반영"
    if "sox" in low:
        return "필라델피아 반도체지수(SOX)의 수익률·추세·변동성. 글로벌 반도체 업황/주가 환경을 반영"
    if "nasdaq" in low:
        return "NASDAQ 시장의 수익률·추세·변동성. 성장주·기술주 위험선호 환경을 반영"
    if "benchmark" in low or "relative" in low or "rel_" in low:
        return "종목을 기준 시장/지수와 비교한 상대강도 또는 초과수익 성격의 지표"
    if "beta" in low:
        return "종목 수익률이 시장 움직임에 얼마나 민감하게 반응하는지 나타내는 베타"
    if "corr" in low:
        return "종목과 시장/기준자산 수익률이 함께 움직이는 정도를 나타내는 상관계수"
    if "residual" in low or "market_adjust" in low:
        return "시장 움직임으로 설명되는 부분을 제거한 종목 고유의 초과수익 성분"
    if "bond" in low or "yield" in low or "dgs" in low or "fred" in low:
        if "slope" in low or "curve" in low or "spread" in low:
            return "장·단기 금리 차이 또는 수익률곡선 기울기. 경기·통화정책 기대를 반영"
        return "국채금리 수준 또는 변화. 할인율·경기·통화정책 환경을 반영"

    # ---- 한국 수급 ----
    if "foreign" in low:
        return "외국인 투자자의 순매수·보유·거래 흐름에서 만든 수급 지표"
    if "institution" in low or "pension" in low or "fund" in low or "insurance" in low:
        return "기관/연기금/투신/보험 등의 매매 흐름에서 만든 수급 지표"
    if "program" in low:
        return "프로그램 매매의 차익·비차익 또는 순매수 흐름을 나타내는 지표"
    if "short" in low:
        return "공매도 거래량·비율·변화에서 만든 매도 압력/포지셔닝 지표"
    if "credit" in low:
        return "신용융자·신용대주 신규/상환/잔고 등 레버리지 수급을 나타내는 지표"
    if "lending" in low or "loan" in low or "borrow" in low:
        return "주식 대차 체결·상환·잔고에서 만든 공매도 잠재수요/포지셔닝 지표"
    if "cfd" in low:
        return "CFD 잔고·거래 흐름을 이용한 레버리지 포지셔닝 지표"

    # ---- Regime / 달력 ----
    if "regime" in low or "gmm" in low or "cluster" in low:
        return "추세·변동성 등으로 분류한 시장 국면(regime). 같은 신호도 국면에 따라 다르게 해석하도록 사용"
    if "dow_" in low:
        return "요일을 주기형(sin/cos) 변수로 바꾼 계절성 Feature"
    if "month_" in low:
        return "월(month)을 주기형(sin/cos) 변수로 바꾼 계절성 Feature"

    return "모델에 실제 입력된 파생 Feature. 이름만으로 정의를 확정하기 어려워 원본 Feature명을 그대로 표시"


def render_feature_importance(top_features: Dict, limit: int = 10) -> None:
    """
    최종 앙상블이 기록한 feature importance를 시각화한다.
    막대는 절대 중요도 자체가 아니라 'Top 1 = 100' 상대 강도다.
    """
    if not isinstance(top_features, dict) or not top_features:
        st.caption("실제 학습 Feature 중요도 정보가 이 스냅샷에는 없습니다.")
        return

    items = []
    for name, raw in list(top_features.items())[:limit]:
        score = num(raw)
        if score is None:
            continue
        items.append((str(name), float(score)))

    if not items:
        st.caption("실제 학습 Feature 중요도 정보가 이 스냅샷에는 없습니다.")
        return

    peak = max(abs(v) for _, v in items) or 1.0
    rows = []
    for rank, (name, score) in enumerate(items, start=1):
        rel = max(0.0, min(100.0, abs(score) / peak * 100.0))
        meaning = feature_meaning(name)
        rows.append(
            "<div class='feature-row'>"
            f"<div class='feature-rank'>{rank}</div>"
            f"<div class='feature-name'>{html.escape(name)}</div>"
            f"<div class='feature-meaning'>{html.escape(meaning)}</div>"
            "<div class='feature-track'>"
            f"<div class='feature-fill' style='width:{rel:.1f}%'></div>"
            "</div>"
            f"<div class='feature-score'>{score:.6f}</div>"
            "</div>"
        )

    st.markdown(
        "<div class='feature-head'>"
        "<div>#</div><div>Feature</div><div>의미</div>"
        "<div>상대 강도</div><div>중요도</div>"
        "</div>"
        "<div class='feature-list'>" + "".join(rows) + "</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "막대는 이 조합의 1위 Feature를 100으로 둔 상대 강도입니다. "
        "중요도는 최종 모델의 예측 기여도를 나타내며 인과관계를 뜻하지 않습니다."
    )


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


def _exchange_today(country: str) -> Optional[pd.Timestamp]:
    """해당 시장 현지 기준 '오늘' 날짜."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return None
    tz = "Asia/Seoul" if country == "KR" else "America/New_York"
    return pd.Timestamp(datetime.now(ZoneInfo(tz)).date())


def prev_close_ref(hist: Optional[pd.DataFrame],
                   country: str) -> Tuple[Optional[float], str]:
    """
    현재가 변동률의 기준이 되는 **전일 종가**와 날짜 라벨을 돌려준다.

    마지막 봉이 오늘(거래소 현지 기준)이면 그것은 오늘의 종가이므로,
    전일 대비를 구하려면 그 앞의 봉을 써야 한다.
      - 장중: 마지막 봉 = 전일 -> 마지막 봉 사용
      - 장 마감 후 수집: 마지막 봉 = 당일 -> 그 앞 봉 사용
    두 경우 모두 결과는 '전일 종가 대비' 가 된다.
    """
    if hist is None or hist.empty or "close" not in hist.columns:
        return None, ""
    if "date" not in hist.columns or len(hist) == 0:
        return None, ""

    idx = len(hist) - 1
    today = _exchange_today(country)
    try:
        last_day = pd.Timestamp(hist["date"].iloc[idx]).normalize()
    except (IndexError, TypeError, ValueError):
        return None, ""
    if today is not None and last_day >= today and len(hist) >= 2:
        idx -= 1

    try:
        close = float(hist["close"].iloc[idx])
        day = pd.Timestamp(hist["date"].iloc[idx]).normalize()
    except (IndexError, TypeError, ValueError):
        return None, ""
    if not (close > 0):
        return None, ""
    return close, f"{day:%m/%d} 종가"


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
    age = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 3600.0
    ts_kst = ts.astimezone(KST)
    return f"{ts_kst:%Y-%m-%d %H:%M} KST · {age:.0f}시간 전", age > STALE_HOURS


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

    # P50 의 성격을 항상 알려준다 — 이름 때문에 '목표가' 로 읽히기 쉽다.
    parts.append(
        "P50 은 목표가가 아니라 추세를 연장하지 않은 기준점입니다. "
        "상승 국면에서는 실제 가격이 그 위에 놓이는 편이 정상입니다."
    )

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
# 관세청 메모리 수출단가
# ======================================================================================
def _kcs_change(g: pd.DataFrame, periods: int) -> Optional[float]:
    """마지막 관측값 대비 periods개월 전 변화율. 월 누락 시 해당 행 간격 기준."""
    s = g["export_unit_price_weight"].dropna().astype("float64")
    if len(s) <= periods:
        return None
    prev, cur = float(s.iloc[-1 - periods]), float(s.iloc[-1])
    if prev <= 0:
        return None
    return cur / prev - 1.0


def kcs_memory_chart(df: pd.DataFrame, years: int = 5,
                     include_logic: bool = False) -> go.Figure:
    """DRAM/NAND/MCP 월별 관세청 수출단가(USD/kg) 선그래프."""
    fig = go.Figure()
    if df is None or df.empty:
        return fig

    cutoff = df["date"].max() - pd.DateOffset(years=int(years))
    shown = df[df["date"] >= cutoff].copy()
    wanted = dict(KCS_MEMORY_SERIES)
    if include_logic:
        wanted[KCS_LOGIC_CODE] = "Logic comparator"

    # 기존 대시보드 팔레트와 충돌하지 않게 제품별 고정 색을 쓴다.
    colors = {
        "DRAM": "#f0b90b",
        "NAND Flash": "#58a6ff",
        "MCP / HBM proxy": "#3fb950",
        "Logic comparator": "#8b949e",
    }
    for code, label in wanted.items():
        g = shown[shown["hs_code"] == code].sort_values("date")
        if g.empty:
            continue
        fig.add_trace(go.Scatter(
            x=g["date"], y=g["export_unit_price_weight"],
            mode="lines+markers", name=label,
            line=dict(color=colors.get(label), width=2,
                      dash="dot" if code == KCS_LOGIC_CODE else "solid"),
            marker=dict(size=4),
            customdata=g[["period"]],
            hovertemplate=(
                "%{customdata[0]}<br>" + label +
                "<br><b>%{y:,.0f} USD/kg</b><extra></extra>"
            ),
        ))

    fig.update_layout(
        template="plotly_dark", height=380,
        margin=dict(l=12, r=28, t=22, b=12),
        paper_bgcolor=BG, plot_bgcolor=BG,
        font=dict(color=TEXT, size=11), hovermode="x unified",
        legend=dict(
            orientation="h",
            y=1.13,
            x=0,
            bgcolor="rgba(13,17,23,0.78)",
            bordercolor="rgba(120,132,148,0.20)",
            borderwidth=1,
            font=dict(size=12, color="#d7dee8"),
            itemsizing="constant",
        ),
        yaxis_title="수출단가 (USD/kg)",
        hoverlabel=dict(bgcolor="#161b22", bordercolor="#30363d"),
    )
    fig.update_xaxes(
        showgrid=False, linecolor=GRID,
        showspikes=True, spikecolor="rgba(255,255,255,0.18)",
        spikethickness=1, spikedash="dot",
    )
    fig.update_yaxes(
        showgrid=True, gridcolor=GRID, linecolor=GRID, side="right",
        separatethousands=True,
    )
    return fig


def render_kcs_memory(df: Optional[pd.DataFrame]) -> None:
    """대시보드 상단에 메모리 수출단가 최신값과 월별 추이를 표시한다."""
    if df is None or df.empty:
        return

    focus = df[df["hs_code"].isin(KCS_MEMORY_SERIES)].copy()
    if focus.empty:
        return

    latest_period = str(focus["period"].max())
    section_head(
        "MEMORY CYCLE",
        "메모리 반도체 수출단가",
        f"최근 통계 {latest_period} · USD/kg",
    )
    with st.expander("관세청 월별 단가 · 상세 보기", expanded=False):
        cols = st.columns(3)
        for col, (code, label) in zip(cols, KCS_MEMORY_SERIES.items()):
            g = focus[focus["hs_code"] == code].sort_values("date")
            if g.empty:
                col.metric(label, "N/A")
                continue
            latest = float(g["export_unit_price_weight"].iloc[-1])
            mom = _kcs_change(g, 1)
            yoy = _kcs_change(g, 12)
            delta = pct(mom) if mom is not None else None
            help_text = (
                f"관세청 품목별 수출입실적의 월별 수출금액/중량 기준 단가. "
                f"최근 YoY {pct(yoy) if yoy is not None else 'N/A'}."
            )
            col.metric(
                label, f"{latest:,.0f} USD/kg", delta,
                help=help_text,
            )

        c1, c2 = st.columns([2, 1])
        with c1:
            years = st.select_slider(
                "표시 기간", options=[3, 5, 7, 10], value=5,
                key="kcs_years", format_func=lambda v: f"{v}년",
                label_visibility="collapsed",
            )
        with c2:
            include_logic = st.checkbox(
                "Logic 대조군", value=False, key="kcs_logic",
                help="메모리 사이클과 일반 로직 IC 단가를 상대 비교할 때 사용합니다.",
            )

        st.plotly_chart(
            kcs_memory_chart(df, years, include_logic),
            use_container_width=True, key="kcs_memory_chart",
        )
        st.caption(
            "관세청 월별 **수출단가(USD/kg)** 입니다. DRAM/NAND 현물 칩 가격이 아니라 "
            "수출금액÷중량으로 계산된 제품 믹스 포함 지표입니다. "
            "MCP는 HBM 전용 가격이 아니라 **HBM을 포함할 수 있는 대리지표**이며, "
            "모델 학습에서는 공표 지연을 반영해 해당 월의 익월 15일 이후에만 사용합니다."
        )


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
                    x=fx, y=c50, mode="lines", name="P50 (기준값)",
                    line=dict(color=FCOL, width=1.8, dash="dot"),
                    hovertemplate="%{x|%m/%d} · %{y:,.0f}<extra></extra>"), row=1, col=1)
                fig.add_annotation(x=fx[-1], y=c50[-1], text=f" {price(c50[-1], currency, False)}",
                                   showarrow=False, xanchor="left",
                                   font=dict(color=FCOL, size=12), row=1, col=1)
            fig.add_vline(x=last_date,
                          line=dict(color="rgba(255,255,255,0.22)", width=1, dash="dot"))

    fig.update_layout(
        template="plotly_dark", height=510 if show_volume else 445,
        margin=dict(l=12, r=72, t=18, b=12), paper_bgcolor=BG, plot_bgcolor=BG,
        font=dict(color=TEXT, size=12), hovermode="x unified",
        xaxis_rangeslider_visible=False, showlegend=False, bargap=0.1,
        hoverlabel=dict(bgcolor="#161b22", bordercolor="#30363d"),
    )
    fig.update_xaxes(
        showgrid=False, linecolor=GRID,
        rangebreaks=[dict(bounds=["sat", "mon"])],
        showspikes=True, spikecolor="rgba(255,255,255,0.18)",
        spikethickness=1, spikedash="dot",
    )
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
    fig.update_layout(
        template="plotly_dark", height=270,
        margin=dict(l=12, r=14, t=22, b=10),
        paper_bgcolor=BG, plot_bgcolor=BG,
        font=dict(color=TEXT, size=11),
        legend=dict(
            orientation="h",
            y=1.16,
            x=0,
            bgcolor="rgba(13,17,23,0.78)",
            bordercolor="rgba(120,132,148,0.20)",
            borderwidth=1,
            font=dict(size=12, color="#d7dee8"),
            itemsizing="constant",
        ),
        hoverlabel=dict(bgcolor="#161b22", bordercolor="#30363d"),
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, linecolor=GRID)
    return fig


# ======================================================================================
# 종목 화면
# ======================================================================================
def render_symbol(symbol: str, sub: pd.DataFrame, payload: Dict,
                  quotes: Optional[Dict] = None) -> None:
    horizons = sorted(int(h) for h in sub["horizon"].unique())
    stock_name = str(sub["name"].iloc[0]) if "name" in sub.columns and len(sub) else symbol

    section_head(
        "FORECAST",
        f"{stock_name} · {symbol}",
        "확정 데이터 기반 확률 예측",
    )

    c_h, c_lb, c_vol = st.columns([3, 2, 1.2])
    with c_h:
        horizon = st.radio(
            "예측 기간", horizons, horizontal=True, key=f"h_{symbol}",
            format_func=lambda h: f"{h}일",
        )
    with c_lb:
        lookback = st.select_slider(
            "차트 기간", options=[60, 120, 250, 400], value=120,
            key=f"lb_{symbol}", format_func=lambda v: f"{v}일",
        )
    with c_vol:
        st.markdown("<div style='height:2px'></div>", unsafe_allow_html=True)
        show_volume = st.checkbox("거래량 표시", value=True, key=f"v_{symbol}")

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
    grade = grade_of(p)
    st.markdown(
        f"<div class='verdict {grade.lower()}'>{DOT.get(grade, '⚪')} "
        f"<b>신뢰도 {fnum(p.get('confidence'), 0)}/100 · {grade}</b> — {verdict(p)}</div>",
        unsafe_allow_html=True,
    )

    # ---- 차트 ----
    hist = load_history(symbol)
    st.plotly_chart(candle_chart(hist, p, lookback, show_volume),
                    use_container_width=True, key=f"candle_{uid}")
    st.caption(
        "음영은 P10~P90 / P25~P75 **분포 범위**입니다. 위 수치 카드의 80% 예측구간은 "
        "OOF 잔차로 별도 보정된 값이라 바깥 음영과 약간 다를 수 있습니다. "
        "점선(P50)은 **목표가가 아니라 기준점**이며, "
        "이 시스템은 최근 추세를 미래로 연장하지 않습니다. "
        "상승 국면에서는 실제 가격이 점선 위에 놓이는 편이 정상이므로, "
        "읽어야 할 것은 점선의 위치가 아니라 **음영의 폭**입니다. "
        "콘의 폭은 √t 로 보간한 시각적 근사이고 미래 날짜는 공휴일 미반영입니다."
    )

    # ---- 핵심 수치 ----
    m = st.columns(5)
    prev_close, prev_label = prev_close_ref(hist, str(p.get("country") or "KR"))
    delta = pct(now / prev_close - 1.0) if (prev_close and now) else None
    if p.get("_reanchored"):
        label = f"현재가 · {quote_age_label(quotes.get('fetched_at'))}"
        tip = ("quotes.py 가 올린 최신 체결가입니다. "
               f"변동률은 전일({prev_label or '?'}) 대비입니다.")
    else:
        label = "현재가"
        tip = ("예측을 계산한 시점의 가격입니다. quotes.py 를 돌리면 최신가로 갱신됩니다. "
               f"변동률은 전일({prev_label or '?'}) 대비입니다.")
    m[0].metric(label, price(now, currency), delta, help=tip)
    m[1].metric(f"{horizon}일 후 기준값 (P50)", price(p.get("p50"), currency), pct(ret_of(p)),
                help="목표가가 아닙니다. 이 시스템은 최근 추세를 미래로 연장하지 않도록 "
                     "설계되어 있어, P50 은 '추세를 연장하지 않았을 때의 기준점' 에 가깝습니다. "
                     "상승 국면에서는 실제 가격이 P50 위에 놓이는 경우가 더 많은 것이 정상입니다. "
                     "방향 근거가 아니라 구간 폭을 보는 용도로 쓰십시오.")
    i80_low = p.get("interval_80_low")
    i80_high = p.get("interval_80_high")
    if num(i80_low) is None or num(i80_high) is None:
        i80_low, i80_high = p.get("p10"), p.get("p90")
        i80_help = ("보정 80% 구간이 없어 P10~P90 분포 범위를 대신 표시합니다. "
                    "확률 보장을 뜻하지 않으며 폭이 넓을수록 불확실성이 큽니다.")
    else:
        i80_help = ("OOF 잔차를 이용해 보정한 80% 예측구간입니다. "
                    "과거 커버리지는 모델 진단의 '80% 구간 실측 커버리지'에서 확인하십시오.")
    m[2].metric("80% 예측구간",
                f"{price(i80_low, currency, False)} ~ {price(i80_high, currency, False)}",
                help=i80_help)
    m[3].metric("상승 확률", pct(p.get("prob_up"), signed=False),
                help="현재가보다 높을 확률. OOF 잔차 분포에서 계산한 뒤 isotonic 보정을 거칩니다. "
                     "50% 근처면 방향성 정보가 없다는 뜻입니다.")
    m[4].metric("변동성(연율)", pct(p.get("expected_volatility_annual"), signed=False),
                help="최근 일간 수익률 표준편차를 연율화(×√252)한 값. 예측이 아니라 현재 상태 지표입니다.")

    # ---- 접힌 상세 ----
    with st.expander("분위수 · 참고 레벨"):
        left, right = st.columns(2)
        with left:
            rows = []
            for key, lab in [("p90", "P90"), ("p75", "P75"), ("p50", "P50 (기준값)"),
                             ("p25", "P25"), ("p10", "P10")]:
                v = num(p.get(key))
                chg = (v / now - 1.0) if (v is not None and now) else None
                rows.append({"구간": lab, "가격": price(v, currency), "현재가 대비": pct(chg)})
            render_dark_table(pd.DataFrame(rows))
        with right:
            lv = [("2차 목표", "target_2"), ("1차 목표", "target_1"),
                  ("추가매수 고려", "add_buy_reference"), ("손절 고려", "stop_loss_reference")]
            render_dark_table(
                pd.DataFrame([{"항목": k, "가격": price(p.get(v), currency)} for k, v in lv])
            )
            st.caption(
                f"R/R {fnum(p.get('risk_reward'))} · ATR {pct(p.get('atr_pct'), signed=False)} · "
                f"지지 {price(p.get('support_20d'), currency, False)} / "
                f"저항 {price(p.get('resistance_20d'), currency, False)}"
            )
        st.caption("참고용 레벨이며 투자 조언이 아닙니다.")

    with st.expander("모델 진단"):
        d1, d2 = st.columns(2)
        with d1:
            render_dark_table(pd.DataFrame({
                "지표": ["IC (Spearman)", "방향 정확도", "RMSE", "baseline RMSE",
                         "80% 구간 실측 커버리지"],
                "값": [fnum(p.get("oos_ic"), 3),
                       pct(p.get("oos_directional_accuracy"), signed=False),
                       fnum(p.get("oos_rmse"), 4), fnum(p.get("baseline_rmse"), 4),
                       pct(p.get("coverage_80"), signed=False)],
                "간단 해석": [
                    "예측 순위와 실제 수익률 순위의 상관. +1에 가까울수록 좋고 0이면 순위 예측력이 거의 없음",
                    "상승·하락 방향을 맞힌 비율. 50% 부근이면 방향 정보가 약함",
                    "실제값과 예측값의 평균 오차 크기. 낮을수록 좋음",
                    "단순 기준모델의 RMSE. 위 RMSE가 이것보다 낮아야 모델 개선으로 볼 수 있음",
                    "실제 결과가 80% 예측구간 안에 들어온 비율. 대략 80% 부근이 이상적",
                ],
            }))
        with d2:
            info = [
                ("선택된 모델", str(p.get("model_weights") or p.get("models") or "-"),
                 "최종 예측에 사용된 모델과 앙상블 가중치"),
                ("Fallback level", str(p.get("fallback_level")),
                 "데이터 부족 시 단순화 단계. 1이 가장 완전한 구성이고 숫자가 커질수록 보수적 fallback"),
                ("마지막 데이터", str(p.get("last_data_time")),
                 "모델 입력에 사용된 마지막 확정 거래일"),
                ("학습 시각", str(p.get("trained_at")),
                 "현재 게시된 모델을 마지막으로 다시 학습한 시각"),
            ]
            sh = num(p.get("shrinkage"))
            if sh is not None and sh < 0.999:
                info.insert(1, (
                    "과대외삽 보정", f"x{sh:.2f}",
                    "예측값을 과도하게 멀리 보내지 않도록 0수익률 방향으로 축소한 정도"
                ))
            if p.get("missing_data"):
                info.append((
                    "누락 데이터", str(p.get("missing_data")),
                    "이번 학습에서 확보되지 않아 자동 제외된 데이터 그룹"
                ))
            render_dark_table(pd.DataFrame(info, columns=["항목", "값", "간단 해석"]))

        # 실제 학습 과정에서 계산된 feature importance 중 상위 10개만 표시한다.
        # main.py가 latest_predictions.json -> diagnostics에 저장한 top_features를 그대로 사용하므로
        # Streamlit에서 중요도를 다시 계산하거나 추정하지 않는다.
        diag = (((payload.get("diagnostics") or {}).get(symbol) or {}).get(str(horizon)) or {})
        top_features = diag.get("top_features") or {}
        st.markdown("**실제 학습 Feature Top 10** — 이름 · 의미 · 최종 모델 중요도")
        render_feature_importance(top_features, limit=10)

        comps = p.get("confidence_components")
        if isinstance(comps, dict) and comps:
            st.markdown("**신뢰도 구성** — 100점 만점 신뢰도를 어떤 항목이 깎거나 받쳐주는지")
            st.caption("각 달성도는 독립적인 성공확률이 아니라 모델 신뢰도 점수를 구성하는 내부 진단값입니다.")
            label = {
                "baseline_improvement": "baseline 대비 RMSE 개선",
                "information_coefficient": "IC (순위 상관)",
                "directional_accuracy": "방향 정확도",
                "probability_calibration": "확률 보정 정확도",
                "interval_coverage": "구간 커버리지",
                "fold_stability": "fold 간 안정성",
                "recent_regime": "최근 구간 성능",
                "data_quantity": "데이터 양",
                "data_freshness": "데이터 최신성",
                "feature_completeness": "feature 완결성",
            }
            rows = [{"항목": label.get(k, k), "달성도": f"{float(v) * 100:.0f}%"}
                    for k, v in comps.items() if not k.startswith("_")]
            render_dark_table(pd.DataFrame(rows))
            if comps.get("_baseline_only_cap"):
                st.caption("ML 모델이 baseline 을 이기지 못해 신뢰도 상한 25 가 적용되었습니다.")

        if p.get("regime"):
            st.caption(f"시장 regime · {p.get('regime')}")
        if p.get("notes"):
            for n in str(p.get("notes")).split(" | "):
                if n.strip():
                    st.caption(f"· {n.strip()}")

    # ---- 라이브 검증 성적 ----
    track = load_track()
    cands = [g for g in (track.get("groups") or [])
             if str(g.get("symbol")) == symbol and int(g.get("horizon", -1)) == horizon]
    # 라이브 기록이 있으면 그것을 우선한다 (백필은 대용치)
    tg = next((g for g in cands if str(g.get("source")) == "LIVE"),
              cands[0] if cands else None)
    if tg or track:
        with st.expander("실적 추적 (예측 기록 vs 실제 결과)"):
            if tg and tg.get("n_resolved"):
                if str(tg.get("source")) == "BACKFILL":
                    st.caption(
                        "구분: **BACKFILL** — 과거 시점마다 그 시점 정보만으로 재학습해 "
                        "만든 기록입니다. 라이브 기록이 쌓이기 전의 대용치입니다."
                    )
                n = int(tg["n_resolved"])
                cov, ci = tg.get("coverage_80"), tg.get("coverage_80_ci") or [None, None]
                dh, dci = tg.get("direction_hit"), tg.get("direction_hit_ci") or [None, None]
                c = st.columns(4)
                c[0].metric("확정 표본", f"{n}건",
                            help="예측을 먼저 기록하고 만기 후 결과를 채운 건수입니다.")
                c[1].metric("80% 구간 적중", pct(cov, signed=False),
                            help=f"목표 80%. 95% 신뢰구간 "
                                 f"{pct(ci[0], signed=False)}~{pct(ci[1], signed=False)}")
                c[2].metric("방향 적중", pct(dh, signed=False),
                            help=f"50% 가 동전 던지기. 95% 신뢰구간 "
                                 f"{pct(dci[0], signed=False)}~{pct(dci[1], signed=False)}")
                c[3].metric("P50 평균오차", pct(tg.get("mae_p50"), signed=False))
                if n < 30:
                    st.caption(
                        f"표본 {n}건은 판단 근거가 되기에 부족합니다. "
                        "신뢰구간이 넓어 어떤 결론도 내리기 어렵습니다."
                    )
                elif cov is not None and (ci[1] is not None and ci[1] < 0.8):
                    st.caption("⚠️ 80% 구간 적중률이 목표를 유의하게 밑돕니다 — 구간이 좁습니다.")
            else:
                st.caption(
                    f"이 조합은 아직 만기 도래분이 없습니다. "
                    f"기록 {track.get('n_total', 0)}건 · 대기 {track.get('n_pending', 0)}건. "
                    f"h={horizon} 이므로 기록 후 약 {horizon}거래일 뒤부터 채워집니다."
                )
            st.caption(
                "백테스트와 달리 예측을 먼저 남기고 나중에 결과를 채우므로 "
                "사후 조정이 불가능한 검증입니다. 대신 표본이 쌓이는 데 시간이 걸립니다."
            )

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
            st.caption(
                "⚠️ 모델 채택·가중치가 이 OOS 구간 전체 성능으로 정해졌으므로 "
                "**selection bias** 가 있습니다. 실제 운용 성과는 이보다 낮을 가능성이 큽니다. "
                "또 여러 종목·기간을 동시에 보면 일부는 우연히 좋아 보입니다(다중검정). "
                "상승장에서는 타이밍 전략이 단순 보유를 이기기 어렵다는 점도 함께 보십시오."
            )


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
    quotes = load_quotes()

    st.markdown(
        f"""
        <div class="dash-hero">
          <div>
            <div class="dash-eyebrow">QUANT FORECAST DASHBOARD</div>
            <div class="dash-title">📈 주가 예측</div>
            <div class="dash-subtitle">확률분포 · 위험구간 · 라이브 검증을 한 화면에서 확인합니다.</div>
          </div>
          <div class="dash-meta">
            스냅샷 {label}<br>
            종목 {len(symbols)} · 예측 {len(preds)}건
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    source_label = "JSON · 진단 포함" if (payload.get("diagnostics") or {}) else "CSV fallback"
    quote_label = quote_age_label(quotes.get("fetched_at")) if quotes.get("fetched_at") else "스냅샷 가격"
    status_class = "warn" if stale else ""
    st.markdown(
        "<div class='status-strip'>"
        f"<span class='status-pill'><span class='status-dot {status_class}'></span>"
        f"모델 스냅샷 <b>{'지연' if stale else '최신'}</b></span>"
        f"<span class='status-pill'>현재가 <b>{html.escape(quote_label)}</b></span>"
        f"<span class='status-pill'>출력 <b>{html.escape(source_label)}</b></span>"
        "</div>",
        unsafe_allow_html=True,
    )

    if stale:
        st.error(f"이 스냅샷은 {label} 결과입니다. 로컬에서 다시 실행 후 게시하세요.")
    if payload.get("source") == "predictions.csv":
        st.info("CSV 만으로 구동 중 · 백테스트와 진단은 publish.py 게시 시 표시됩니다.")

    if quotes.get("fetched_at"):
        st.caption(
            f"💹 현재가 {quote_age_label(quotes['fetched_at'])} 갱신 · "
            "예측 가격대만 현재가에 맞춰 재조정하며 모델 입력은 마지막 확정 봉 기준입니다."
        )

    # 핵심 작업을 먼저 배치한다: 종목 선택 → Forecast → 산업 컨텍스트.
    section_head("ASSET", "분석 종목 선택", "종목을 바꾸면 아래 예측 화면만 갱신됩니다.")
    name_of = {sym: str(df[df["symbol"] == sym]["name"].iloc[0]) for sym in symbols}
    symbol = st.selectbox(
        "종목 선택", symbols, key="symbol_select",
        format_func=lambda sym: f"{name_of.get(sym, sym)}  ·  {sym}",
    )
    render_symbol(symbol, df[df["symbol"] == symbol], payload, quotes)

    # 관세청 메모리 단가는 보조 산업 컨텍스트이므로 Forecast 뒤에서 기본 접힘으로 제공한다.
    # Streamlit Cloud가 관세청 API나 로컬 절대경로를 직접 호출하지 않는다.
    render_kcs_memory(load_kcs_memory())

    st.divider()
    st.caption(DISCLAIMER)


if __name__ == "__main__":
    main()