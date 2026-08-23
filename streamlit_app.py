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

from zoneinfo import ZoneInfo
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

  /* 최종 사용 모델: 단순 텍스트 대신 실제 ensemble weight를 bar로 표시 */
  .model-weight-list {
    display: grid;
    gap: 7px;
    margin: 7px 0 11px 0;
  }

  .model-weight-row {
    display: grid;
    grid-template-columns: minmax(118px, 0.85fr) minmax(150px, 1.5fr) 58px;
    align-items: center;
    gap: 9px;
    min-height: 34px;
    padding: 6px 8px;
    border: 1px solid rgba(120,132,148,0.12);
    border-radius: 8px;
    background: rgba(13,17,23,0.76);
  }

  .model-weight-name {
    min-width: 0;
    color: #dde4ec;
    font-size: 0.75rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    overflow-wrap: anywhere;
  }

  .model-weight-track {
    width: 100%;
    height: 8px;
    border-radius: 999px;
    background: rgba(120,132,148,0.14);
    overflow: hidden;
  }

  .model-weight-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, rgba(240,185,11,0.48), rgba(240,185,11,0.96));
  }

  .model-weight-score {
    color: #d6dee8;
    font-size: 0.71rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    text-align: right;
    white-space: nowrap;
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

    /* Top 10: 모바일에서는 가로 4열을 강제하지 않고 2단 카드로 배치.
       iPhone에서 중요도 열이 화면 밖으로 잘리던 문제를 없앤다. */
    .feature-head {
      display: none;
    }
    .feature-list {
      width: 100%;
      min-width: 0;
      overflow-x: hidden;
    }
    .feature-row {
      width: 100%;
      min-width: 0;
      box-sizing: border-box;
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr) auto;
      grid-template-areas:
        "rank name score"
        "rank track track"
        "rank meaning meaning";
      column-gap: 8px;
      row-gap: 5px;
      align-items: start;
      padding: 9px 8px;
    }
    .feature-rank {
      grid-area: rank;
      align-self: center;
      text-align: right;
      padding-top: 1px;
      font-weight: 750;
      color: #d9a90d;
    }
    .feature-rank::before {
      content: "#";
      margin-right: 1px;
      color: #8d98a6;
      font-weight: 600;
    }
    .feature-name {
      grid-area: name;
      min-width: 0;
      font-size: 0.70rem;
      line-height: 1.38;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .feature-meaning {
      grid-area: meaning;
      min-width: 0;
      font-size: 0.69rem;
      line-height: 1.48;
      overflow-wrap: anywhere;
      word-break: keep-all;
    }
    .feature-score {
      grid-area: score;
      align-self: start;
      text-align: right;
      white-space: nowrap;
      font-size: 0.63rem;
      line-height: 1.38;
      padding: 2px 5px;
      border: 1px solid rgba(120,132,148,0.18);
      border-radius: 999px;
      background: rgba(18,23,31,0.72);
      color: #cbd5e1;
    }
    .feature-score::before {
      content: "중요도 ";
      color: #8d98a6;
      font-size: 0.60rem;
      font-weight: 650;
    }
    .feature-track {
      grid-area: track;
      display: block !important;
      width: 100%;
      min-width: 0;
      height: 6px;
      margin: 1px 0 2px 0;
      border-radius: 999px;
      background: rgba(120,132,148,0.16);
      overflow: hidden;
    }
    .feature-fill {
      height: 100%;
      border-radius: 999px;
    }

    div[data-testid="stMetric"] {
      min-height: 88px;
    }

    .model-weight-row {
      grid-template-columns: minmax(92px, 0.9fr) minmax(90px, 1.35fr) 52px;
      gap: 7px;
      padding: 7px 7px;
    }

    .model-weight-name {
      font-size: 0.68rem;
    }

    .model-weight-score {
      font-size: 0.66rem;
    }

    .model-weight-track {
      height: 7px;
    }
  }


  /* 전체 Feature 사전: 선택/미선택 후보를 한눈에 비교 */
  .feature-catalog-summary {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
    margin: 8px 0 10px 0;
  }
  .feature-catalog-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 5px 9px;
    border: 1px solid rgba(120,132,148,0.18);
    border-radius: 999px;
    background: rgba(18,23,31,0.72);
    color: #cbd5e1;
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
  }
  .feature-catalog-chip strong { color: #f0b90b; }

  .feature-catalog-wrap {
    max-height: 610px;
    overflow: auto;
    border: 1px solid rgba(120,132,148,0.16);
    border-radius: 10px;
    background: #0b0f15;
  }
  table.feature-catalog {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    table-layout: fixed;
    font-size: 0.75rem;
  }
  table.feature-catalog thead th {
    position: sticky;
    top: 0;
    z-index: 3;
    background: #111720;
    color: #aeb9c7;
    text-align: left;
    font-weight: 700;
    padding: 8px 9px;
    border-bottom: 1px solid rgba(120,132,148,0.20);
  }
  table.feature-catalog tbody td {
    padding: 7px 9px;
    border-bottom: 1px solid rgba(120,132,148,0.09);
    color: #d6dee8;
    vertical-align: top;
    line-height: 1.42;
  }
  table.feature-catalog tbody tr:hover td { background: #111720; }
  table.feature-catalog th:nth-child(1),
  table.feature-catalog td:nth-child(1) { width: 76px; }
  table.feature-catalog th:nth-child(2),
  table.feature-catalog td:nth-child(2) { width: 92px; }
  table.feature-catalog th:nth-child(3),
  table.feature-catalog td:nth-child(3) { width: 245px; }
  table.feature-catalog td:nth-child(3) {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    color: #e3e9f0;
    overflow-wrap: anywhere;
  }
  .feature-status {
    display: inline-block;
    min-width: 54px;
    text-align: center;
    padding: 2px 6px;
    border-radius: 999px;
    font-size: 0.66rem;
    font-weight: 750;
    letter-spacing: 0.01em;
    white-space: nowrap;
  }
  .feature-status-top {
    color: #ffd54a;
    background: rgba(240,185,11,0.12);
    border: 1px solid rgba(240,185,11,0.27);
  }
  .feature-status-selected {
    color: #7ee787;
    background: rgba(63,185,80,0.10);
    border: 1px solid rgba(63,185,80,0.24);
  }
  .feature-status-unused {
    color: #9aa7b5;
    background: rgba(120,132,148,0.08);
    border: 1px solid rgba(120,132,148,0.15);
  }
  .feature-status-outside {
    color: #8fb9ff;
    background: rgba(88,166,255,0.08);
    border: 1px solid rgba(88,166,255,0.20);
  }

  @media (max-width: 850px) {
    table.feature-catalog th:nth-child(2),
    table.feature-catalog td:nth-child(2) { display: none; }
    table.feature-catalog th:nth-child(3),
    table.feature-catalog td:nth-child(3) { width: 150px; }
  }

  /* 휴대폰: 가로 표를 세로 카드로 전환해 좌우 잘림을 없앤다. */
  @media (max-width: 600px) {
    .feature-catalog-wrap {
      /* iOS Safari에서 중첩 스크롤 영역이 먹지 않는 경우가 있어
         모바일에서는 내부 스크롤을 없애고 페이지 자체가 자연스럽게 스크롤되게 한다. */
      max-height: none;
      overflow: visible;
      padding: 6px;
    }
    table.feature-catalog,
    table.feature-catalog tbody,
    table.feature-catalog tr,
    table.feature-catalog td {
      display: block;
      width: 100% !important;
      box-sizing: border-box;
    }
    table.feature-catalog {
      table-layout: auto;
      font-size: 0.74rem;
    }
    table.feature-catalog thead {
      display: none;
    }
    table.feature-catalog tbody tr {
      margin: 0 0 8px 0;
      padding: 8px 9px;
      border: 1px solid rgba(120,132,148,0.16);
      border-radius: 9px;
      background: #0d1219;
    }
    table.feature-catalog tbody td {
      display: grid;
      grid-template-columns: 64px minmax(0, 1fr);
      gap: 8px;
      align-items: start;
      padding: 4px 0;
      border-bottom: 0;
      min-width: 0;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    table.feature-catalog tbody td::before {
      content: attr(data-label);
      color: #8795a7;
      font-size: 0.66rem;
      font-weight: 700;
      line-height: 1.45;
    }
    table.feature-catalog tbody td:nth-child(2) {
      display: grid;
    }
    table.feature-catalog tbody td:nth-child(3) {
      font-size: 0.70rem;
      line-height: 1.35;
    }
    table.feature-catalog tbody td:nth-child(4) {
      font-size: 0.72rem;
      line-height: 1.48;
    }
    .feature-status {
      min-width: 48px;
      width: fit-content;
      font-size: 0.63rem;
    }
    .feature-catalog-chip {
      font-size: 0.68rem;
      padding: 4px 7px;
    }
  }


  /* ===============================================================
     RESPONSIVE DASHBOARD v14
     데이터/모델 로직은 건드리지 않고 PC·태블릿·스마트폰의
     밀도, 가독성, 표/카드 배치, 터치 영역만 정리한다.
     =============================================================== */
  html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
    overflow-x: hidden !important;
  }

  .block-container {
    width: min(100%, 1440px) !important;
    max-width: 1440px !important;
    padding-top: 0.95rem !important;
    padding-left: clamp(0.85rem, 2.1vw, 2.15rem) !important;
    padding-right: clamp(0.85rem, 2.1vw, 2.15rem) !important;
    padding-bottom: 2.4rem !important;
  }

  .dash-hero {
    align-items: center;
    padding: 15px 17px 16px 17px;
    margin: 0 0 11px 0;
    border: 1px solid rgba(120,132,148,0.15);
    border-radius: 15px;
    background:
      linear-gradient(110deg, rgba(240,185,11,0.045), transparent 34%),
      linear-gradient(180deg, rgba(18,23,31,0.72), rgba(13,17,23,0.54));
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.018), 0 9px 28px rgba(0,0,0,0.08);
  }
  .dash-title { font-size: clamp(1.48rem, 2vw, 2rem); }
  .dash-subtitle { max-width: 760px; line-height: 1.5; }
  .dash-meta { color: #9eabba; }

  .status-strip {
    margin: 0 0 13px 0;
    gap: 6px;
  }
  .status-pill {
    min-height: 29px;
    box-sizing: border-box;
  }

  .section-head {
    margin-top: 21px;
    margin-bottom: 9px;
  }
  .section-title { font-size: 1.03rem; }
  .section-note { max-width: 680px; text-align: right; }

  /* Streamlit 기본 column 간격을 조금 줄여 정보 밀도를 안정화 */
  div[data-testid="stHorizontalBlock"] {
    gap: 0.78rem !important;
  }
  div[data-testid="stColumn"] {
    min-width: 0 !important;
  }

  /* 핵심 metric: 한 줄 높이/폰트 균형 */
  div[data-testid="stMetric"] {
    min-height: 88px !important;
    padding: 11px 12px 10px 12px !important;
    border-radius: 11px !important;
  }
  [data-testid="stMetricLabel"] {
    font-size: 0.72rem !important;
    line-height: 1.28 !important;
  }
  [data-testid="stMetricValue"] {
    font-size: clamp(1.02rem, 1.45vw, 1.22rem) !important;
    line-height: 1.18 !important;
  }
  [data-testid="stMetricDelta"] {
    font-size: 0.74rem !important;
  }

  /* 차트는 브라우저 폭을 넘지 않게 */
  div[data-testid="stPlotlyChart"],
  div[data-testid="stPlotlyChart"] > div {
    width: 100% !important;
    max-width: 100% !important;
    min-width: 0 !important;
    box-sizing: border-box !important;
  }

  /* 공통 진단표: 데스크톱에서는 설명 열이 충분한 폭을 확보 */
  .dash-table-wrap {
    width: 100%;
    max-width: 100%;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
  table.dash-table {
    width: 100%;
    table-layout: auto;
    font-size: 0.76rem;
  }
  table.dash-table th,
  table.dash-table td {
    overflow-wrap: anywhere;
    word-break: keep-all;
  }
  .dash-table-wrap.cols-3 table.dash-table th:nth-child(1),
  .dash-table-wrap.cols-3 table.dash-table td:nth-child(1) { width: 23%; }
  .dash-table-wrap.cols-3 table.dash-table th:nth-child(2),
  .dash-table-wrap.cols-3 table.dash-table td:nth-child(2) { width: 16%; white-space: nowrap; }
  .dash-table-wrap.cols-3 table.dash-table th:nth-child(3),
  .dash-table-wrap.cols-3 table.dash-table td:nth-child(3) { width: 61%; }
  .dash-table-wrap.cols-2 table.dash-table th:first-child,
  .dash-table-wrap.cols-2 table.dash-table td:first-child { width: 42%; }

  /* 모델 가중치: 좌측 이름과 우측 수치를 고정하고 bar에 남은 폭을 할당 */
  .model-weight-list { gap: 6px; }
  .model-weight-row {
    grid-template-columns: minmax(115px, 0.82fr) minmax(150px, 1.7fr) 58px;
    min-height: 32px;
    padding: 5px 7px;
  }
  .model-weight-track { min-width: 60px; }

  /* Feature Top 10은 폭이 넓을 때 의미 열을 우선 확보 */
  .feature-head,
  .feature-row {
    grid-template-columns: 26px minmax(150px, 0.95fr) minmax(270px, 1.75fr) minmax(90px, 0.65fr) 72px;
  }

  /* Expander 내부를 살짝 압축 */
  div[data-testid="stExpander"] { margin-top: 7px; }
  div[data-testid="stExpander"] details summary { min-height: 41px; }
  div[data-testid="stExpander"] details > div { padding-top: 2px; }

  /* 라디오 선택지는 작은 화면에서 자연스럽게 다음 줄로 흐른다. */
  div[role="radiogroup"] {
    display: flex !important;
    flex-wrap: wrap !important;
    gap: 5px !important;
  }

  @media (max-width: 1000px) {
    .block-container {
      padding-left: 1rem !important;
      padding-right: 1rem !important;
    }
    .dash-hero { align-items: flex-start; }
    .section-note { max-width: 50%; }
    .feature-head,
    .feature-row {
      grid-template-columns: 25px minmax(130px, 0.9fr) minmax(190px, 1.5fr) 68px;
    }
    .feature-head > div:nth-child(4),
    .feature-track { display: none; }
  }

  @media (max-width: 760px) {
    .block-container {
      padding-top: 0.55rem !important;
      padding-left: 0.68rem !important;
      padding-right: 0.68rem !important;
      padding-bottom: 1.8rem !important;
    }

    .dash-hero {
      flex-direction: column;
      align-items: flex-start;
      gap: 8px;
      padding: 12px 12px 13px 12px;
      border-radius: 12px;
    }
    .dash-eyebrow { font-size: 0.62rem; }
    .dash-title { font-size: 1.42rem; }
    .dash-subtitle {
      margin-top: 5px;
      font-size: 0.75rem;
      line-height: 1.42;
    }
    .dash-meta {
      width: 100%;
      padding-top: 7px;
      border-top: 1px solid rgba(120,132,148,0.11);
      font-size: 0.69rem;
      text-align: left;
      line-height: 1.45;
    }

    .status-strip {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 5px;
    }
    .status-pill {
      justify-content: center;
      min-width: 0;
      padding: 6px 5px;
      font-size: 0.64rem;
      white-space: normal;
      text-align: center;
      line-height: 1.25;
    }
    .status-dot { flex: 0 0 7px; }

    .section-head {
      flex-direction: column;
      align-items: flex-start;
      gap: 3px;
      margin-top: 18px;
      margin-bottom: 7px;
      padding-left: 9px;
    }
    .section-kicker { font-size: 0.61rem; }
    .section-title { font-size: 0.98rem; }
    .section-note {
      max-width: 100%;
      text-align: left;
      font-size: 0.68rem;
      line-height: 1.4;
    }

    /* 예측기간/차트기간/거래량 컨트롤은 휴대폰에서 세로로 */
    div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] div[data-testid="stRadio"]) {
      display: grid !important;
      grid-template-columns: 1fr !important;
      gap: 0.35rem !important;
    }
    div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] div[data-testid="stRadio"]) > div[data-testid="stColumn"] {
      width: 100% !important;
      flex: 1 1 auto !important;
    }

    /* metric 묶음은 1열로 길게 늘어뜨리지 않고 2열 카드 그리드 */
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) {
      display: grid !important;
      grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
      gap: 0.48rem !important;
      width: 100% !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) > div[data-testid="stColumn"] {
      width: 100% !important;
      min-width: 0 !important;
      flex: none !important;
    }
    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) > div[data-testid="stColumn"]:last-child:nth-child(odd) {
      grid-column: 1 / -1;
    }
    div[data-testid="stMetric"] {
      min-height: 76px !important;
      padding: 9px 9px 8px 9px !important;
      border-radius: 9px !important;
    }
    [data-testid="stMetricLabel"] { font-size: 0.66rem !important; }
    [data-testid="stMetricValue"] {
      font-size: 0.98rem !important;
      overflow-wrap: anywhere;
    }
    [data-testid="stMetricDelta"] { font-size: 0.67rem !important; }

    .verdict {
      margin: 6px 0 9px 0;
      padding: 9px 10px;
      border-radius: 9px;
      font-size: 0.78rem;
      line-height: 1.48;
    }

    /* 일반 진단표는 스마트폰에서 행별 카드로 변환 */
    .dash-table-wrap {
      overflow: visible;
      border: 0;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
    }
    table.dash-table,
    table.dash-table tbody,
    table.dash-table tr,
    table.dash-table td {
      display: block;
      width: 100% !important;
      box-sizing: border-box;
    }
    table.dash-table thead { display: none; }
    table.dash-table tbody tr {
      margin: 0 0 7px 0;
      padding: 7px 8px;
      border: 1px solid rgba(120,132,148,0.14);
      border-radius: 9px;
      background: rgba(13,17,23,0.76);
    }
    table.dash-table tbody td {
      display: grid;
      grid-template-columns: minmax(72px, 0.34fr) minmax(0, 1fr);
      gap: 8px;
      align-items: start;
      padding: 3px 0;
      border: 0;
      white-space: normal !important;
      font-size: 0.70rem;
      line-height: 1.45;
    }
    table.dash-table tbody td::before {
      content: attr(data-label);
      color: #8794a4;
      font-size: 0.64rem;
      font-weight: 700;
      line-height: 1.45;
    }

    /* 모델 가중치: 휴대폰에서도 이름/bar/%를 한 줄 유지 */
    .model-weight-row {
      grid-template-columns: minmax(86px, 0.9fr) minmax(76px, 1.55fr) 49px;
      gap: 6px;
      min-height: 31px;
      padding: 5px 6px;
    }
    .model-weight-name { font-size: 0.65rem; }
    .model-weight-score { font-size: 0.63rem; }
    .model-weight-track { height: 7px; }

    /* Feature Top10: 모바일 카드 구조를 더 읽기 쉽게 */
    .feature-list { gap: 7px; }
    .feature-row {
      border-radius: 9px;
      padding: 9px 8px;
      background: rgba(13,17,23,0.78);
    }
    .feature-name { font-size: 0.69rem; }
    .feature-meaning { font-size: 0.68rem; line-height: 1.45; }
    .feature-score { font-size: 0.61rem; }

    .feature-catalog-summary {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 5px;
    }
    .feature-catalog-chip {
      justify-content: space-between;
      width: 100%;
      box-sizing: border-box;
      font-size: 0.65rem;
      padding: 5px 7px;
    }

    /* Expander 터치 영역과 내부 여백 */
    div[data-testid="stExpander"] { border-radius: 10px; }
    div[data-testid="stExpander"] details summary {
      min-height: 43px;
      padding-left: 9px !important;
      padding-right: 9px !important;
    }
    div[data-testid="stExpander"] details summary p { font-size: 0.75rem; }

    [data-testid="stCaptionContainer"] p,
    .stCaption p {
      font-size: 0.67rem !important;
      line-height: 1.5 !important;
    }

    hr {
      margin-top: 1.2rem !important;
      margin-bottom: 0.9rem !important;
    }
  }

  @media (max-width: 430px) {
    .block-container {
      padding-left: 0.55rem !important;
      padding-right: 0.55rem !important;
    }
    .status-pill { font-size: 0.60rem; }
    .model-weight-row {
      grid-template-columns: minmax(78px, 0.9fr) minmax(58px, 1.35fr) 45px;
      gap: 5px;
    }
    table.dash-table tbody td {
      grid-template-columns: 67px minmax(0, 1fr);
      gap: 6px;
    }
    div[role="radiogroup"] label {
      padding: 5px 8px !important;
      font-size: 0.72rem !important;
    }
  }



  /* 모델 진단 메타정보: PC에서는 한 줄 카드, 모바일에서는 자연스럽게 줄바꿈 */
  .diag-meta-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 8px;
    margin: 10px 0 14px 0;
  }

  .diag-meta-card {
    min-width: 0;
    padding: 9px 10px;
    border: 1px solid rgba(120,132,148,0.14);
    border-radius: 9px;
    background: rgba(13,17,23,0.72);
  }

  .diag-meta-label {
    color: #8f9baa;
    font-size: 0.66rem;
    font-weight: 700;
    letter-spacing: 0.01em;
    margin-bottom: 3px;
  }

  .diag-meta-value {
    color: #e3e9f0;
    font-size: 0.79rem;
    font-weight: 700;
    line-height: 1.35;
    overflow-wrap: anywhere;
  }

  .diag-meta-desc {
    color: #9da9b7;
    font-size: 0.66rem;
    line-height: 1.35;
    margin-top: 4px;
  }

  .diag-subhead {
    color: #c7d0dc;
    font-size: 0.74rem;
    font-weight: 700;
    margin: 1px 0 6px 0;
  }

  @media (max-width: 980px) {
    .diag-meta-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }

  @media (max-width: 620px) {
    .diag-meta-grid { grid-template-columns: 1fr; gap: 6px; }
    .diag-meta-card { padding: 8px 9px; }
  }


  /* ===============================================================
     MOBILE POLISH V3 — 모델 진단/Feature 영역 최종 반응형 override
     앞쪽의 여러 버전 CSS보다 마지막에 위치해 cascade 충돌을 없앤다.
     =============================================================== */
  .diag-overview-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.08fr) minmax(0, 0.92fr);
    gap: 12px;
    align-items: start;
    margin: 2px 0 10px 0;
  }
  .diag-panel {
    min-width: 0;
    padding: 10px;
    border: 1px solid rgba(120,132,148,0.14);
    border-radius: 11px;
    background: rgba(11,15,21,0.58);
  }
  .diag-panel .diag-subhead {
    margin: 0 0 8px 0;
    color: #d9e0e8;
    font-size: 0.78rem;
  }
  .diag-panel .diag-subhead span {
    color: #8f9baa;
    font-weight: 560;
    margin-left: 6px;
  }
  .diag-perf-list {
    display: grid;
    gap: 6px;
  }
  .diag-perf-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px;
    align-items: center;
    min-width: 0;
    padding: 8px 9px;
    border: 1px solid rgba(120,132,148,0.11);
    border-radius: 8px;
    background: rgba(13,17,23,0.68);
  }
  .diag-perf-copy { min-width: 0; }
  .diag-perf-label {
    color: #dce3eb;
    font-size: 0.72rem;
    font-weight: 700;
    line-height: 1.3;
  }
  .diag-perf-desc {
    margin-top: 3px;
    color: #8f9baa;
    font-size: 0.63rem;
    line-height: 1.38;
    word-break: keep-all;
  }
  .diag-perf-value {
    color: #f1f4f8;
    font-size: 0.78rem;
    font-weight: 760;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  .diag-empty {
    padding: 9px 10px;
    border: 1px dashed rgba(120,132,148,0.18);
    border-radius: 8px;
    color: #8f9baa;
    font-size: 0.69rem;
  }
  .diag-section-title {
    display: flex;
    align-items: baseline;
    gap: 7px;
    margin: 17px 0 8px 0;
    color: #dce3eb;
    font-size: 0.84rem;
    font-weight: 760;
    letter-spacing: -0.015em;
  }
  .diag-section-title span {
    color: #8f9baa;
    font-size: 0.68rem;
    font-weight: 520;
  }

  @media (max-width: 760px) {
    /* 모델 진단의 핵심: PC 2열 -> 모바일 1열. st.columns에 의존하지 않는다. */
    .diag-overview-grid {
      grid-template-columns: 1fr !important;
      gap: 8px !important;
      margin-top: 0 !important;
    }
    .diag-panel {
      padding: 9px !important;
      border-radius: 10px !important;
    }
    .diag-panel .diag-subhead {
      font-size: 0.76rem !important;
      margin-bottom: 7px !important;
    }
    .diag-panel .diag-subhead span {
      display: inline;
      margin-left: 5px;
      font-size: 0.65rem;
    }

    .diag-perf-row {
      grid-template-columns: minmax(0, 1fr) auto !important;
      gap: 8px !important;
      padding: 8px 9px !important;
    }
    .diag-perf-label { font-size: 0.70rem !important; }
    .diag-perf-desc {
      font-size: 0.61rem !important;
      line-height: 1.38 !important;
      max-width: 100% !important;
    }
    .diag-perf-value { font-size: 0.74rem !important; }

    /* 모델명 + 비율을 첫 줄, bar를 그 아래 전체 폭으로. */
    .model-weight-list {
      gap: 7px !important;
      margin: 0 !important;
    }
    .model-weight-row {
      display: grid !important;
      grid-template-columns: minmax(0, 1fr) auto !important;
      grid-template-areas:
        "mw-name mw-score"
        "mw-track mw-track" !important;
      column-gap: 8px !important;
      row-gap: 6px !important;
      min-height: 0 !important;
      padding: 8px 9px !important;
      border-radius: 8px !important;
    }
    .model-weight-name {
      grid-area: mw-name !important;
      min-width: 0 !important;
      font-size: 0.68rem !important;
      line-height: 1.35 !important;
      overflow-wrap: anywhere !important;
    }
    .model-weight-score {
      grid-area: mw-score !important;
      align-self: center !important;
      font-size: 0.67rem !important;
    }
    .model-weight-track {
      grid-area: mw-track !important;
      width: 100% !important;
      min-width: 0 !important;
      height: 6px !important;
    }

    /* 메타정보는 휴대폰에서도 2x2. 설명은 숨겨 세로 길이를 줄인다. */
    .diag-meta-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
      gap: 6px !important;
      margin: 9px 0 12px 0 !important;
    }
    .diag-meta-card {
      padding: 8px 9px !important;
      min-height: 58px !important;
      border-radius: 9px !important;
    }
    .diag-meta-label { font-size: 0.62rem !important; }
    .diag-meta-value {
      font-size: 0.73rem !important;
      line-height: 1.3 !important;
      overflow-wrap: anywhere !important;
    }
    .diag-meta-desc { display: none !important; }

    .diag-section-title {
      display: block !important;
      margin: 15px 0 8px 0 !important;
      font-size: 0.82rem !important;
      line-height: 1.25 !important;
    }
    .diag-section-title span {
      display: block !important;
      margin-top: 3px !important;
      font-size: 0.64rem !important;
      line-height: 1.35 !important;
    }

    /* Feature Top10: 1행 헤더 + 전체폭 bar + 전체폭 설명. */
    .feature-head { display: none !important; }
    .feature-list {
      display: grid !important;
      gap: 7px !important;
      width: 100% !important;
      min-width: 0 !important;
      overflow: visible !important;
    }
    .feature-row {
      display: grid !important;
      width: 100% !important;
      min-width: 0 !important;
      box-sizing: border-box !important;
      grid-template-columns: auto minmax(0, 1fr) auto !important;
      grid-template-areas:
        "rank name score"
        "track track track"
        "meaning meaning meaning" !important;
      column-gap: 7px !important;
      row-gap: 7px !important;
      align-items: center !important;
      padding: 10px 11px !important;
      border-radius: 10px !important;
      overflow: hidden !important;
    }
    .feature-rank {
      grid-area: rank !important;
      align-self: center !important;
      text-align: left !important;
      padding: 0 !important;
      font-size: 0.66rem !important;
      white-space: nowrap !important;
    }
    .feature-name {
      grid-area: name !important;
      min-width: 0 !important;
      font-size: 0.68rem !important;
      line-height: 1.35 !important;
      overflow-wrap: anywhere !important;
      word-break: break-word !important;
    }
    .feature-score {
      grid-area: score !important;
      justify-self: end !important;
      align-self: center !important;
      max-width: 104px !important;
      padding: 2px 6px !important;
      font-size: 0.60rem !important;
      line-height: 1.3 !important;
      white-space: nowrap !important;
    }
    .feature-score::before { font-size: 0.57rem !important; }
    .feature-track {
      grid-area: track !important;
      display: block !important;
      width: 100% !important;
      min-width: 0 !important;
      height: 6px !important;
      margin: 0 !important;
    }
    .feature-meaning {
      grid-area: meaning !important;
      min-width: 0 !important;
      font-size: 0.65rem !important;
      line-height: 1.48 !important;
      overflow-wrap: anywhere !important;
      word-break: keep-all !important;
    }

    /* Streamlit expander 안의 다른 2열 구성도 휴대폰에선 세로로 쌓는다. */
    div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] {
      display: grid !important;
      grid-template-columns: 1fr !important;
      gap: 0.45rem !important;
    }
    div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
      width: 100% !important;
      min-width: 0 !important;
      flex: none !important;
    }
  }

  @media (max-width: 390px) {
    .diag-meta-grid { grid-template-columns: 1fr 1fr !important; }
    .diag-meta-card { padding: 7px 8px !important; }
    .feature-row { padding: 9px !important; }
    .feature-score { max-width: 92px !important; }
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
        "backtests": {}, "diagnostics": {}, "feature_catalog": {}, "source": "predictions.csv",
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
    """작은 진단/레벨 표를 PC 표 + 모바일 카드 형태로 반응형 렌더링한다."""
    if df is None or df.empty:
        return

    cols = [str(c) for c in df.columns]
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    rows = []
    for _, row in df.iterrows():
        cells = []
        for col in cols:
            raw = row[col]
            text = "—" if is_missing(raw) else str(raw)
            cells.append(
                f"<td data-label='{html.escape(col, quote=True)}'>{html.escape(text)}</td>"
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")

    table_html = (
        f"<div class='dash-table-wrap cols-{len(cols)}'>"
        "<table class='dash-table'>"
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


def _model_weights_html(model_weights, models=None) -> str:
    """모델 가중치 영역의 HTML. Streamlit column에 의존하지 않아 모바일에서도 안정적이다."""
    if not isinstance(model_weights, dict) or not model_weights:
        fallback = html.escape(str(models or "-"))
        return f"<div class='diag-empty'>모델 가중치 정보 없음 · {fallback}</div>"

    items = []
    for name, raw in model_weights.items():
        weight = num(raw)
        if weight is None or weight < 0:
            continue
        items.append((str(name), float(weight)))

    if not items:
        return "<div class='diag-empty'>모델 가중치 정보가 없습니다.</div>"

    items.sort(key=lambda x: x[1], reverse=True)
    total = sum(weight for _, weight in items)
    if total <= 0:
        return "<div class='diag-empty'>유효한 모델 가중치가 없습니다.</div>"

    rows = []
    for name, weight in items:
        share = max(0.0, min(1.0, weight / total))
        pct_value = share * 100.0
        rows.append(
            "<div class='model-weight-row'>"
            f"<div class='model-weight-name'>{html.escape(name)}</div>"
            "<div class='model-weight-track'>"
            f"<div class='model-weight-fill' style='width:{pct_value:.2f}%'></div>"
            "</div>"
            f"<div class='model-weight-score'>{pct_value:.1f}%</div>"
            "</div>"
        )
    return "<div class='model-weight-list'>" + "".join(rows) + "</div>"


def render_model_weights(model_weights, models=None) -> None:
    """최종 예측에 사용된 ensemble model weight를 실제 비율 bar로 표시한다."""
    st.markdown(_model_weights_html(model_weights, models), unsafe_allow_html=True)


def _diag_performance_html(p: Dict) -> str:
    """Walk-Forward OOS 검증 성능을 과장 없이 읽을 수 있게 요약한다."""
    oos_n = num(p.get("oos_samples"))
    eff_n = num(p.get("effective_oos_samples"))
    eval_n = num(p.get("interval_eval_n"))
    eval_eff = num(p.get("interval_eval_effective"))

    rmse = num(p.get("oos_rmse"))
    base_rmse = num(p.get("baseline_rmse"))
    skill = num(p.get("baseline_improvement"))
    if skill is None and rmse is not None and base_rmse is not None and base_rmse > 0:
        skill = 1.0 - rmse / base_rmse

    raw_cov = num(p.get("raw_coverage_80"))
    adj_cov = num(p.get("coverage_80"))
    if raw_cov is None:
        raw_cov = adj_cov
    inflation = num(p.get("interval_inflation"))
    ece = num(p.get("probability_calibration_error"))

    sample_value = "—"
    if oos_n is not None:
        sample_value = f"{oos_n:.0f}행"
        if eff_n is not None:
            sample_value += f" · 실효≈{eff_n:.1f}"

    rmse_value = fnum(rmse, 4)
    if base_rmse is not None:
        rmse_value += f" / base {base_rmse:.4f}"
    if skill is not None:
        rmse_value += f" · {skill * 100:+.1f}%"

    raw_cov_value = pct(raw_cov, signed=False)
    if eval_n is not None:
        raw_cov_value += f" · n={eval_n:.0f}"
        if eval_eff is not None:
            raw_cov_value += f"(실효≈{eval_eff:.1f})"

    metrics = [
        (
            "OOS 표본", sample_value,
            "h일 forward return은 날짜가 겹치므로 raw 행 수보다 독립 정보량이 작습니다. "
            "실효 표본은 보수적으로 OOS행/h로 표시합니다.",
        ),
        (
            "IC (Spearman)", fnum(p.get("oos_ic"), 3),
            "예측 순위와 실제 수익률 순위의 상관. 0이면 순위 정보가 없고, 양수일수록 좋습니다.",
        ),
        (
            "방향 정확도", pct(p.get("oos_directional_accuracy"), signed=False),
            "상승·하락 방향을 맞힌 비율. 50% 부근이면 방향 정보가 약합니다.",
        ),
        (
            "RMSE / baseline", rmse_value,
            "Walk-Forward OOS RMSE와 기준모델 RMSE. 마지막 %는 baseline 대비 개선율이며 양수여야 개선입니다.",
        ),
        (
            "80% 구간 · 보정 전", raw_cov_value,
            "구간 폭을 다시 넓히기 전에 별도 holdout에서 측정한 honest coverage. 신뢰도 계산은 이 값을 사용합니다.",
        ),
    ]

    if (adj_cov is not None and raw_cov is not None and abs(adj_cov - raw_cov) > 1e-6):
        adj_value = pct(adj_cov, signed=False)
        if inflation is not None and inflation > 1.001:
            adj_value += f" · 폭×{inflation:.2f}"
        metrics.append((
            "80% 구간 · 보정 후", adj_value,
            "같은 holdout에서 관측된 꼬리 이탈을 보고 폭을 확대한 뒤의 값입니다. "
            "실제 운용 구간 진단용이며 독립 검증 성적으로 보지 않습니다.",
        ))

    if ece is not None:
        metrics.append((
            "원확률 ECE", fnum(ece, 3),
            "최종 isotonic 보정 전 원확률의 calibration error. 0에 가까울수록 확률과 실제 빈도가 잘 맞습니다.",
        ))

    rows = []
    for label, value, desc in metrics:
        rows.append(
            "<div class='diag-perf-row'>"
            "<div class='diag-perf-copy'>"
            f"<div class='diag-perf-label'>{html.escape(str(label))}</div>"
            f"<div class='diag-perf-desc'>{html.escape(str(desc))}</div>"
            "</div>"
            f"<div class='diag-perf-value'>{html.escape(str(value))}</div>"
            "</div>"
        )
    return "<div class='diag-perf-list'>" + "".join(rows) + "</div>"


def render_diag_overview(p: Dict, diag: Dict) -> None:
    """검증 성능 + 모델 가중치를 CSS grid 하나로 렌더링한다.

    st.columns를 쓰지 않으므로 PC에서는 2열, 휴대폰에서는 1열로 확실하게 전환된다.
    """
    perf = _diag_performance_html(p)
    weights = _model_weights_html(diag.get("weights"), p.get("models"))
    st.markdown(
        "<div class='diag-overview-grid'>"
        "<section class='diag-panel'>"
        "<div class='diag-subhead'>검증 성능 <span>Walk-Forward OOS</span></div>"
        f"{perf}"
        "</section>"
        "<section class='diag-panel'>"
        "<div class='diag-subhead'>선택된 모델 <span>최종 앙상블 가중치</span></div>"
        f"{weights}"
        "</section>"
        "</div>",
        unsafe_allow_html=True,
    )

def _build_source_feature_catalog() -> Dict[str, List[str]]:
    """업로드된 Feature 생성 소스에서 코드상 정의된 전체 Feature 목록."""
    out: Dict[str, List[str]] = {
        "technical": [],
        "momentum": [],
        "volatility": [],
        "liquidity": [],
        "market": [],
        "kr_flow": [],
        "regime": [],
    }

    for w in (5, 10, 20, 60, 120, 200):
        out["technical"] += [f"px_over_sma{w}", f"sma{w}_slope"]
    out["technical"] += [f"px_over_ema{w}" for w in (12, 26, 50)]
    out["technical"] += [
        "ma_alignment", "sma5_minus_sma20", "macd", "macd_signal", "macd_hist",
        "rsi14", "rsi7", "stoch_rsi14", "cci20", "bb_pos20", "bb_width20",
        "px_zscore10", "px_zscore20", "px_zscore60", "williams_r14",
    ]

    for w in (1, 2, 5, 10, 20, 60):
        out["momentum"] += [f"ret_{w}d", f"logret_{w}d"]
    out["momentum"] += [
        "overnight_ret", "intraday_ret", "gap_ret", "hl_range",
        "close_position_in_range",
    ]
    out["momentum"] += [f"roc_{w}" for w in (5, 10, 20, 60, 120)]
    out["momentum"] += [
        "mom_12_1", "mom_accel_20_60", "up_day_ratio_20",
        "ret_sign_consistency_10", "pos_52w", "drawdown_from_52w_high",
        "runup_from_52w_low", "streak",
    ]

    out["volatility"] += [f"vol_{w}d" for w in (5, 10, 20, 60, 120)]
    out["volatility"] += [
        "vol_ratio_5_60", "vol_ratio_20_120", "vol_change_20",
        "atr14_pct", "atr_ratio_14_60", "parkinson_20", "garman_klass_20",
        "parkinson_over_close_vol", "downside_vol_20", "upside_vol_20",
        "vol_skewness_20", "ret_skew_60", "ret_kurt_60", "vol_percentile",
        "drawdown_60",
    ]

    out["liquidity"] += ["volume_change_1d"]
    out["liquidity"] += [f"volume_over_ma{w}" for w in (5, 20, 60)]
    out["liquidity"] += [
        "volume_z20", "volume_z60", "log_volume_change_5", "volume_trend_20",
        "turnover_z20", "log_turnover_change_5", "px_vol_corr_20",
        "signed_volume_20", "obv_change_20", "obv_slope_5", "mfi14", "cmf20",
        "amihud_20", "amihud_z", "volume_percentile",
    ]

    for p in ("kospi", "kosdaq", "usbm"):
        out["market"] += [f"{p}_ret_1d"]
        out["market"] += [f"{p}_ret_{w}d" for w in (5, 20, 60)]
        out["market"] += [
            f"{p}_vol_20d", f"{p}_vol_60d", f"{p}_vol_ratio",
            f"{p}_px_over_ma20", f"{p}_px_over_ma60", f"{p}_px_over_ma200",
            f"{p}_drawdown",
        ]
        out["market"] += [f"rs_{p}_{w}d" for w in (5, 20, 60)]
        for w in (60, 120):
            out["market"] += [f"beta_{p}_{w}", f"corr_{p}_{w}"]
        out["market"] += [
            f"resid_ret_{p}_1d", f"resid_ret_{p}_5d", f"resid_ret_{p}_20d",
            f"resid_vol_{p}_20d", f"beta_{p}_change_20",
        ]

    for tag in ("2y", "3y", "5y", "10y", "20y", "30y"):
        out["market"] += [
            f"bond_{tag}_level", f"bond_{tag}_chg5",
            f"bond_{tag}_chg20", f"bond_{tag}_z60",
        ]
    for name in ("10y_2y", "10y_3y", "30y_10y", "5y_2y"):
        out["market"] += [f"curve_{name}", f"curve_{name}_chg20"]

    for tag in ("3mo", "2", "5", "10", "30"):
        out["market"] += [
            f"usbond_{tag}_level", f"usbond_{tag}_chg5",
            f"usbond_{tag}_chg20", f"usbond_{tag}_z60",
        ]
    for name in ("10y_2y", "10y_3m", "30y_10y", "5y_2y"):
        out["market"] += [f"uscurve_{name}", f"uscurve_{name}_chg20"]

    for who in ("individual", "foreigner", "institution", "pension", "fin_inv", "other_corp"):
        out["market"] += [
            f"mkt_{who}_net_norm", f"mkt_{who}_net_norm_5", f"mkt_{who}_net_z20",
        ]

    out["market"] += [
        "fx_usdkrw", "fx_ret_1d", "fx_ret_5d", "fx_ret_20d",
        "fx_vol_20d", "fx_over_ma20", "fx_over_ma60", "fx_z60",
    ]

    for p in ("dram", "nand", "mcp", "logic"):
        out["market"] += [
            f"kcs_{p}_price_mom", f"kcs_{p}_price_qoq", f"kcs_{p}_price_yoy",
            f"kcs_{p}_price_z12", f"kcs_{p}_value_yoy", f"kcs_{p}_up_streak",
        ]
    out["market"] += [
        "kcs_dram_nand_ratio_yoy", "kcs_dram_nand_ratio_z12",
        "kcs_dram_logic_ratio_yoy", "kcs_dram_logic_ratio_z12",
        "kcs_mcp_share", "kcs_mcp_share_chg6", "kcs_data_age_days",
    ]

    for who in ("individual", "foreigner", "institution", "other_corp"):
        base = f"flow_{who}"
        out["kr_flow"] += [
            f"{base}_net_norm", f"{base}_net_norm_5", f"{base}_net_norm_20",
            f"{base}_net_z20", f"{base}_net_z60", f"{base}_net_streak",
        ]
    for sub in ("fin_inv", "insurance", "trust", "pef", "bank", "other_fin", "pension"):
        out["kr_flow"] += [
            f"flow_inst_{sub}_net_norm", f"flow_inst_{sub}_net_norm_20",
        ]
    out["kr_flow"] += [
        "flow_foreign_inst_divergence", "flow_smart_money_20",
        "foreigner_holding_rate", "foreigner_holding_rate_chg5",
        "foreigner_holding_rate_chg20", "foreigner_holding_rate_z", "foreigner_room",
        "cfd_buy_balance_rate", "cfd_buy_balance_rate_chg5",
        "cfd_sell_balance_rate", "cfd_sell_balance_rate_chg5",
        "cfd_long_short_ratio",
    ]
    for who in ("prog_arb", "prog_nonarb"):
        base = f"flow_{who}"
        out["kr_flow"] += [
            f"{base}_net_norm", f"{base}_net_norm_5", f"{base}_net_norm_20",
            f"{base}_net_z20", f"{base}_net_z60", f"{base}_net_streak",
        ]
    out["kr_flow"] += [
        "flow_prog_total_net_norm", "short_ratio", "short_amount_ratio",
        "short_vol_norm", "short_ratio_ma5", "short_ratio_ma20", "short_ratio_chg",
        "short_ratio_z20", "short_ratio_z60", "short_vol_chg5",
        "short_ma5_over_ma20",
    ]
    for p in ("margin_loan", "stock_loan"):
        out["kr_flow"] += [
            f"{p}_balance_rate", f"{p}_trading_rate", f"{p}_balance_chg5",
            f"{p}_balance_chg20", f"{p}_balance_z60", f"{p}_net_new_norm",
            f"{p}_net_new_norm_20",
        ]
    out["kr_flow"] += [
        "credit_long_short_ratio", "lending_balance_chg5", "lending_balance_chg20",
        "lending_balance_z60", "lending_balance_norm", "lending_net_norm",
        "lending_net_norm_20", "lending_balance_amt_norm",
    ]

    out["regime"] += [
        "regime_trend_up", "regime_trend_mid_up", "regime_trend_score",
        "regime_vol_pct", "regime_high_vol", "regime_low_vol",
        "regime_volume_expansion", "regime_volume_ratio", "regime_risk_on",
        "regime_market_drawdown", "regime_bear_market", "regime_rate_up",
        "regime_rate_level_pct", "regime_gmm", "regime_gmm_low_vol",
        "regime_gmm_normal", "regime_gmm_high_vol",
    ]
    return out


SOURCE_FEATURES_BY_GROUP = _build_source_feature_catalog()
SOURCE_FEATURE_GROUP = {
    name: group
    for group, names in SOURCE_FEATURES_BY_GROUP.items()
    for name in names
}
SOURCE_FEATURE_COUNT = len(SOURCE_FEATURE_GROUP)


def feature_meaning(feature_name: str) -> str:
    n = str(feature_name).strip()
    if not n:
        return "빈 Feature 이름"
    low = n.lower()

    m = re.fullmatch(r"px_over_sma(5|10|20|60|120|200)", low)
    if m:
        w = int(m.group(1))
        return f"종가 ÷ {w}일 단순이동평균(SMA) - 1. 0보다 크면 종가가 {w}일 평균 위에 있음"
    m = re.fullmatch(r"sma(5|10|20|60|120|200)_slope", low)
    if m:
        w = int(m.group(1)); d = max(1, w // 5)
        return f"{w}일 SMA의 {d}거래일 변화율. 이동평균선의 방향과 변화 속도"
    m = re.fullmatch(r"px_over_ema(12|26|50)", low)
    if m:
        w = int(m.group(1))
        return f"종가 ÷ {w}일 지수이동평균(EMA) - 1. EMA 대비 가격 이격도"

    exact = {
        "ma_alignment": "(SMA5>SMA20) + (SMA20>SMA60) - 1. 값 -1/0/1로 역배열·혼조·정배열을 표현",
        "sma5_minus_sma20": "(SMA5 - SMA20) ÷ 종가. 단기와 중기 이동평균 간 상대 스프레드",
        "macd": "(EMA12 - EMA26) ÷ 종가. 가격수준을 제거한 MACD",
        "macd_signal": "MACD 원선(EMA12-EMA26)의 9일 EMA ÷ 종가. MACD 신호선",
        "macd_hist": "[MACD 원선 - 9일 Signal] ÷ 종가. 추세 모멘텀의 강화·약화",
        "rsi14": "RSI(14) ÷ 100. 최근 상승·하락폭을 0~1 범위로 정규화한 모멘텀",
        "rsi7": "RSI(7) ÷ 100. 더 짧은 구간의 상승·하락 강도를 0~1로 표시",
        "stoch_rsi14": "RSI(14)가 최근 14일 RSI 최저~최고 범위에서 차지하는 위치. 0~1",
        "cci20": "20일 CCI ÷ 100. 대표가격이 최근 평균에서 얼마나 이탈했는지",
        "bb_pos20": "20일·2표준편차 볼린저밴드에서 (종가-하단) ÷ (상단-하단). 밴드 내 상대 위치",
        "bb_width20": "20일 볼린저밴드 폭 ÷ 20일 평균 = 4×표준편차 ÷ 평균. 상대 변동성",
        "williams_r14": "(14일 최고가 - 종가) ÷ (14일 최고가 - 14일 최저가). 코드상 0~1형 Williams 위치",
    }
    if low in exact:
        return exact[low]
    m = re.fullmatch(r"px_zscore(10|20|60)", low)
    if m:
        w = m.group(1)
        return f"(종가 - {w}일 평균) ÷ {w}일 표준편차. 최근 {w}일 가격 분포에서 현재 위치"

    m = re.fullmatch(r"ret_(1|2|5|10|20|60)d", low)
    if m:
        w = m.group(1)
        return f"종가의 {w}거래일 단순 수익률: 현재 종가 ÷ {w}일 전 종가 - 1"
    m = re.fullmatch(r"logret_(1|2|5|10|20|60)d", low)
    if m:
        w = m.group(1)
        return f"종가의 {w}거래일 로그수익률: ln(현재 종가 ÷ {w}일 전 종가)"
    exact = {
        "overnight_ret": "당일 시가 ÷ 전일 종가 - 1. 장 마감 후~다음 시가 사이 갭 수익률",
        "intraday_ret": "당일 종가 ÷ 당일 시가 - 1. 장중 수익률",
        "gap_ret": "(당일 시가 - 전일 종가) ÷ 전일 종가. overnight_ret과 같은 산식의 갭 수익률",
        "hl_range": "(당일 고가 - 당일 저가) ÷ 당일 종가. 가격수준을 제거한 일중 범위",
        "close_position_in_range": "(종가 - 당일 저가) ÷ (당일 고가 - 당일 저가). 당일 범위 내 종가 위치",
        "mom_12_1": "21거래일 전 종가 ÷ 252거래일 전 종가 - 1. 최근 약 1개월을 제외한 12-1 모멘텀",
        "mom_accel_20_60": "20일 수익률 - (60일 수익률 ÷ 3). 최근 20일 모멘텀이 60일 평균 속도보다 강한지",
        "up_day_ratio_20": "최근 20거래일 중 1일 수익률이 양수인 날의 비율",
        "ret_sign_consistency_10": "최근 10일 누적 방향의 부호 × 그 방향과 같은 일간수익률 부호의 비율. 상승 지속은 +, 하락 지속은 -",
        "pos_52w": "현재 종가가 rolling 저점~고점 범위에서 차지하는 위치. 창은 최대 252일이며 짧은 이력에서는 축소",
        "drawdown_from_52w_high": "현재 종가 ÷ rolling 최고가 - 1. 최대 252거래일 고점 대비 낙폭",
        "runup_from_52w_low": "현재 종가 ÷ rolling 최저가 - 1. 최대 252거래일 저점 대비 상승폭",
        "streak": "같은 방향의 일간수익률 부호가 연속된 일수. 상승 연속은 +, 하락 연속은 -",
    }
    if low in exact:
        return exact[low]
    m = re.fullmatch(r"roc_(5|10|20|60|120)", low)
    if m:
        w = m.group(1)
        return f"현재 종가 ÷ {w}거래일 전 종가 - 1. {w}일 ROC; 같은 기간 ret_{w}d와 동일 산식"

    m = re.fullmatch(r"vol_(5|10|20|60|120)d", low)
    if m:
        w = m.group(1)
        return f"1일 로그수익률의 최근 {w}일 표준편차 × √252. 연율화 실현변동성"
    exact = {
        "vol_ratio_5_60": "5일 연율화 변동성 ÷ 60일 연율화 변동성. 초단기 변동성 확대 정도",
        "vol_ratio_20_120": "20일 연율화 변동성 ÷ 120일 연율화 변동성. 최근 변동성의 장기 대비 수준",
        "vol_change_20": "20일 연율화 변동성의 20거래일 전 대비 변화율",
        "atr14_pct": "ATR(14) ÷ 종가. 갭을 포함한 True Range의 14일 Wilder형 EMA를 가격으로 정규화",
        "atr_ratio_14_60": "ATR(14) ÷ ATR(60). 단기 실제 변동폭의 장기 대비 비율",
        "parkinson_20": "최근 20일 고가/저가 로그범위를 이용한 Parkinson 변동성의 연율화 값",
        "garman_klass_20": "최근 20일 시가·고가·저가·종가를 이용한 Garman–Klass 변동성의 연율화 값",
        "parkinson_over_close_vol": "20일 Parkinson 변동성 ÷ 20일 종가 로그수익률 변동성",
        "downside_vol_20": "최근 20일 음(-)의 로그수익률만 사용한 표준편차 × √252. 하방 변동성",
        "upside_vol_20": "최근 20일 양(+)의 로그수익률만 사용한 표준편차 × √252. 상방 변동성",
        "vol_skewness_20": "20일 상방 변동성 ÷ 20일 하방 변동성. 상승·하락 변동성 비대칭",
        "ret_skew_60": "최근 60일 1일 로그수익률의 왜도",
        "ret_kurt_60": "최근 60일 1일 로그수익률의 첨도",
        "vol_percentile": "20일 변동성의 expanding percentile rank. 과거 데이터만으로 현재 변동성 위치를 0~1로 표시",
        "drawdown_60": "현재 종가 ÷ 최근 60일 최고가 - 1. 60일 고점 대비 낙폭",
    }
    if low in exact:
        return exact[low]

    m = re.fullmatch(r"volume_over_ma(5|20|60)", low)
    if m:
        w = m.group(1)
        return f"당일 거래량 ÷ 최근 {w}일 평균 거래량. 평소 대비 거래량 배수"
    m = re.fullmatch(r"volume_z(20|60)", low)
    if m:
        return f"거래량의 {m.group(1)}일 rolling Z-score"
    exact = {
        "volume_change_1d": "거래량의 전일 대비 변화율",
        "log_volume_change_5": "log(1+거래량)의 5거래일 차이. 거래량 규모 변화",
        "volume_trend_20": "20일 평균 거래량 ÷ 60일 평균 거래량. 중기 거래량 추세",
        "turnover_z20": "근사 거래대금(종가×거래량)의 20일 Z-score",
        "log_turnover_change_5": "log(1+종가×거래량)의 5거래일 차이. 거래대금 변화",
        "px_vol_corr_20": "최근 20일 단순 일간수익률과 거래량 일간변화율의 rolling 상관계수",
        "signed_volume_20": "최근 20일 [수익률 부호×거래량] 합 ÷ 최근 20일 거래량 합. 거래량의 상승/하락 방향 편향",
        "obv_change_20": "OBV의 20일 변화량 ÷ 최근 20일 거래량 합",
        "obv_slope_5": "OBV의 5일 변화량 ÷ 최근 5일 거래량 합",
        "mfi14": "MFI(14) ÷ 100. 대표가격×거래량의 양·음 자금흐름 비율을 0~1로 정규화",
        "cmf20": "20일 Chaikin Money Flow. 일중 종가 위치×거래량을 20일 거래량 합으로 정규화",
        "amihud_20": "20일 평균 [|일간수익률| ÷ (종가×거래량)] × 1e9. Amihud 비유동성",
        "amihud_z": "amihud_20의 60일 rolling Z-score",
        "volume_percentile": "현재 거래량의 최근 120일 rolling percentile rank. 거래량 레짐 위치",
    }
    if low in exact:
        return exact[low]

    m = re.fullmatch(r"(kospi|kosdaq|usbm)_ret_(1|5|20|60)d", low)
    if m:
        lab = {"kospi": "KOSPI", "kosdaq": "KOSDAQ", "usbm": "미국 벤치마크 ETF"}[m.group(1)]
        return f"{lab} 종가의 {m.group(2)}거래일 로그수익률"
    m = re.fullmatch(r"(kospi|kosdaq|usbm)_vol_(20|60)d", low)
    if m:
        lab = {"kospi": "KOSPI", "kosdaq": "KOSDAQ", "usbm": "미국 벤치마크 ETF"}[m.group(1)]
        return f"{lab} 1일 로그수익률의 {m.group(2)}일 표준편차 × √252. 연율화 시장 변동성"
    m = re.fullmatch(r"(kospi|kosdaq|usbm)_vol_ratio", low)
    if m:
        lab = {"kospi": "KOSPI", "kosdaq": "KOSDAQ", "usbm": "미국 벤치마크 ETF"}[m.group(1)]
        return f"{lab} 20일 변동성 ÷ 60일 변동성"
    m = re.fullmatch(r"(kospi|kosdaq|usbm)_px_over_ma(20|60|200)", low)
    if m:
        lab = {"kospi": "KOSPI", "kosdaq": "KOSDAQ", "usbm": "미국 벤치마크 ETF"}[m.group(1)]
        return f"{lab} 종가 ÷ {m.group(2)}일 이동평균 - 1. 시장 추세선 대비 이격도"
    m = re.fullmatch(r"(kospi|kosdaq|usbm)_drawdown", low)
    if m:
        lab = {"kospi": "KOSPI", "kosdaq": "KOSDAQ", "usbm": "미국 벤치마크 ETF"}[m.group(1)]
        return f"{lab} 종가 ÷ 최근 120일 최고값 - 1. 시장 고점 대비 낙폭"
    m = re.fullmatch(r"rs_(kospi|kosdaq|usbm)_(5|20|60)d", low)
    if m:
        lab = {"kospi": "KOSPI", "kosdaq": "KOSDAQ", "usbm": "미국 벤치마크 ETF"}[m.group(1)]
        return f"종목 {m.group(2)}일 로그수익률 - {lab} {m.group(2)}일 로그수익률. 시장 대비 상대강도"
    m = re.fullmatch(r"(beta|corr)_(kospi|kosdaq|usbm)_(60|120)", low)
    if m:
        lab = {"kospi": "KOSPI", "kosdaq": "KOSDAQ", "usbm": "미국 벤치마크 ETF"}[m.group(2)]
        if m.group(1) == "beta":
            return f"최근 {m.group(3)}일 종목-시장 공분산 ÷ {lab} 수익률 분산. rolling beta"
        return f"최근 {m.group(3)}일 종목과 {lab}의 1일 로그수익률 rolling 상관계수"
    m = re.fullmatch(r"resid_ret_(kospi|kosdaq|usbm)_(1|5|20)d", low)
    if m:
        lab = {"kospi": "KOSPI", "kosdaq": "KOSDAQ", "usbm": "미국 벤치마크 ETF"}[m.group(1)]
        w = m.group(2)
        return f"일간 잔차=종목 로그수익률-beta60×{lab} 로그수익률; 이를 {w}일 {'값' if w == '1' else '합'}으로 만든 고유수익"
    m = re.fullmatch(r"resid_vol_(kospi|kosdaq|usbm)_20d", low)
    if m:
        lab = {"kospi": "KOSPI", "kosdaq": "KOSDAQ", "usbm": "미국 벤치마크 ETF"}[m.group(1)]
        return f"종목 로그수익률-beta60×{lab} 로그수익률 잔차의 20일 표준편차 × √252. 종목 고유변동성"
    m = re.fullmatch(r"beta_(kospi|kosdaq|usbm)_change_20", low)
    if m:
        lab = {"kospi": "KOSPI", "kosdaq": "KOSDAQ", "usbm": "미국 벤치마크 ETF"}[m.group(1)]
        return f"{lab} 대비 60일 rolling beta의 20거래일 차이"

    m = re.fullmatch(r"bond_(2y|3y|5y|10y|20y|30y)_(level|chg5|chg20|z60)", low)
    if m:
        ten = m.group(1).replace("y", "년")
        return {
            "level": f"한국 국고채 {ten} 수익률 수준. 계산 후 1거래일 lag 적용",
            "chg5": f"한국 국고채 {ten} 수익률의 5거래일 차이. 계산 후 1거래일 lag 적용",
            "chg20": f"한국 국고채 {ten} 수익률의 20거래일 차이. 계산 후 1거래일 lag 적용",
            "z60": f"한국 국고채 {ten} 수익률의 60일 Z-score. 계산 후 1거래일 lag 적용",
        }[m.group(2)]
    m = re.fullmatch(r"curve_(10y_2y|10y_3y|30y_10y|5y_2y)(?:_(chg20))?", low)
    if m:
        pair = {"10y_2y": "10년-2년", "10y_3y": "10년-3년", "30y_10y": "30년-10년", "5y_2y": "5년-2년"}[m.group(1)]
        return f"한국 국고채 {pair} 금리차" + ("의 20거래일 차이" if m.group(2) else "") + ". 계산 후 1거래일 lag 적용"
    m = re.fullmatch(r"usbond_(3mo|2|5|10|30)_(level|chg5|chg20|z60)", low)
    if m:
        ten = {"3mo": "3개월", "2": "2년", "5": "5년", "10": "10년", "30": "30년"}[m.group(1)]
        return {
            "level": f"FRED 미국 국채 {ten} 수익률 수준. 1거래일 lag 적용",
            "chg5": f"FRED 미국 국채 {ten} 수익률의 5거래일 차이. 1거래일 lag 적용",
            "chg20": f"FRED 미국 국채 {ten} 수익률의 20거래일 차이. 1거래일 lag 적용",
            "z60": f"FRED 미국 국채 {ten} 수익률의 60일 Z-score. 1거래일 lag 적용",
        }[m.group(2)]
    m = re.fullmatch(r"uscurve_(10y_2y|10y_3m|30y_10y|5y_2y)(?:_(chg20))?", low)
    if m:
        pair = {"10y_2y": "10년-2년", "10y_3m": "10년-3개월", "30y_10y": "30년-10년", "5y_2y": "5년-2년"}[m.group(1)]
        return f"FRED 미국 국채 {pair} 금리차" + ("의 20거래일 차이" if m.group(2) else "") + ". 1거래일 lag 적용"

    m = re.fullmatch(r"mkt_(individual|foreigner|institution|pension|fin_inv|other_corp)_(net_norm|net_norm_5|net_z20)", low)
    if m:
        actor = {"individual": "개인", "foreigner": "외국인", "institution": "기관 전체", "pension": "연기금", "fin_inv": "금융투자", "other_corp": "기타법인"}[m.group(1)]
        if m.group(2) == "net_norm":
            return f"{actor} KOSPI/KOSDAQ 순매매대금 ÷ 수집된 모든 투자자 buyAmount 합. 1거래일 lag 적용"
        if m.group(2) == "net_norm_5":
            return f"위 {actor} 시장 순매매 정규화값의 최근 5일 합. 1거래일 lag 적용"
        return f"{actor} KOSPI/KOSDAQ 원시 순매매대금의 20일 Z-score. 1거래일 lag 적용"

    fx = {
        "fx_usdkrw": "USD/KRW 환율 수준값. 최소 40개 관측 확보 시 사용하고 1거래일 lag 적용",
        "fx_ret_1d": "USD/KRW 환율의 1일 단순 변화율. 1거래일 lag 적용",
        "fx_ret_5d": "USD/KRW 환율의 5일 단순 변화율. 1거래일 lag 적용",
        "fx_ret_20d": "USD/KRW 환율의 20일 단순 변화율. 1거래일 lag 적용",
        "fx_vol_20d": "USD/KRW 1일 변화율의 20일 표준편차 × √252. 1거래일 lag 적용",
        "fx_over_ma20": "USD/KRW 환율 ÷ 20일 평균 - 1. 1거래일 lag 적용",
        "fx_over_ma60": "USD/KRW 환율 ÷ 60일 평균 - 1. 1거래일 lag 적용",
        "fx_z60": "USD/KRW 환율의 60일 Z-score. 1거래일 lag 적용",
    }
    if low in fx:
        return fx[low]

    product = {
        "dram": "DRAM(HS 8542321010)",
        "nand": "NAND Flash(HS 8542321030)",
        "mcp": "MCP/복합구조칩·HBM 포함 가능(HS 8542323000)",
        "logic": "Logic IC 대조군(HS 8542311000)",
    }
    m = re.fullmatch(r"kcs_(dram|nand|mcp|logic)_(price_mom|price_qoq|price_yoy|price_z12|value_yoy|up_streak)", low)
    if m:
        lab = product[m.group(1)]
        desc = {
            "price_mom": f"{lab} 월별 수출 중량단가(USD/kg)의 전월 대비 변화율",
            "price_qoq": f"{lab} 월별 수출 중량단가(USD/kg)의 3개월 전 대비 변화율",
            "price_yoy": f"{lab} 월별 수출 중량단가(USD/kg)의 12개월 전 대비 변화율",
            "price_z12": f"{lab} 수출 중량단가(USD/kg)의 12개월 Z-score(최소 6개월)",
            "value_yoy": f"{lab} 월별 수출금액(USD)의 12개월 전 대비 변화율",
            "up_streak": f"{lab} 수출 중량단가가 전월보다 오른 상태가 이어진 연속 개월 수",
        }[m.group(2)]
        return desc + ". 해당 월 값은 익월 15일 이후부터 일봉에 반영"
    kcs = {
        "kcs_dram_nand_ratio_yoy": "DRAM/NAND 수출 중량단가 비율의 12개월 전 대비 변화율. 익월 15일 공표 지연 반영",
        "kcs_dram_nand_ratio_z12": "DRAM/NAND 수출 중량단가 비율의 12개월 Z-score. 익월 15일 공표 지연 반영",
        "kcs_dram_logic_ratio_yoy": "DRAM/Logic IC 수출 중량단가 비율의 12개월 전 대비 변화율. 익월 15일 공표 지연 반영",
        "kcs_dram_logic_ratio_z12": "DRAM/Logic IC 수출 중량단가 비율의 12개월 Z-score. 익월 15일 공표 지연 반영",
        "kcs_mcp_share": "MCP 수출금액 ÷ (MCP 수출금액 + DRAM 수출금액). HBM/MCP 제품믹스 대리지표; 익월 15일 이후 반영",
        "kcs_mcp_share_chg6": "kcs_mcp_share의 6개월 차이. HBM/MCP 제품믹스 변화 속도; 익월 15일 이후 반영",
        "kcs_data_age_days": "각 거래일 기준 현재 사용 중인 KCS 월간 통계의 공표일(익월 15일)로부터 경과한 일수",
    }
    if low in kcs:
        return kcs[low]

    m = re.fullmatch(r"flow_(individual|foreigner|institution|other_corp)_(net_norm|net_norm_5|net_norm_20|net_z20|net_z60|net_streak)", low)
    if m:
        actor = {"individual": "개인", "foreigner": "외국인", "institution": "기관 전체", "other_corp": "기타법인"}[m.group(1)]
        desc = {
            "net_norm": f"{actor} 순매수수량 ÷ 종목의 최근 20일 평균 거래량",
            "net_norm_5": f"{actor} 순매수수량/20일 평균거래량 정규화값의 최근 5일 합",
            "net_norm_20": f"{actor} 순매수수량/20일 평균거래량 정규화값의 최근 20일 합",
            "net_z20": f"{actor} 원시 순매수수량의 20일 Z-score",
            "net_z60": f"{actor} 원시 순매수수량의 60일 Z-score",
            "net_streak": f"{actor} 순매수 부호(sign)의 최근 5일 합(-5~+5). 이름은 streak지만 실제로는 5일 방향 균형",
        }[m.group(2)]
        return desc + ". 투자자별 매매동향은 1거래일 lag 적용"

    m = re.fullmatch(r"flow_inst_(fin_inv|insurance|trust|pef|bank|other_fin|pension)_net_norm(?:_(20))?", low)
    if m:
        actor = {"fin_inv": "금융투자", "insurance": "보험", "trust": "투신", "pef": "사모펀드", "bank": "은행", "other_fin": "기타금융", "pension": "연기금"}[m.group(1)]
        return f"기관 세부 {actor} 순매수수량 ÷ 종목의 20일 평균 거래량" + ("의 최근 20일 합" if m.group(2) else "") + ". 1거래일 lag 적용"

    kr = {
        "flow_foreign_inst_divergence": "(외국인 순매수수량 - 기관 전체 순매수수량) ÷ 종목 20일 평균 거래량. 1거래일 lag 적용",
        "flow_smart_money_20": "최근 20일 (외국인+기관) 순매수수량 합 ÷ 현재 종목 20일 평균 거래량. 1거래일 lag 적용",
        "foreigner_holding_rate": "토스 외국인 보유비율 원값. 1거래일 lag 적용",
        "foreigner_holding_rate_chg5": "외국인 보유비율의 5거래일 차이. 1거래일 lag 적용",
        "foreigner_holding_rate_chg20": "외국인 보유비율의 20거래일 차이. 1거래일 lag 적용",
        "foreigner_holding_rate_z": "외국인 보유비율의 120일 Z-score. 1거래일 lag 적용",
        "foreigner_room": "1 - (외국인 보유수량 ÷ 외국인 보유한도수량). 외국인 추가 보유 여력 비율; 1거래일 lag",
        "cfd_buy_balance_rate": "CFD 매수잔고비율 원값. 투자자별 데이터 기준 1거래일 lag 적용",
        "cfd_buy_balance_rate_chg5": "CFD 매수잔고비율의 5거래일 차이. 1거래일 lag 적용",
        "cfd_sell_balance_rate": "CFD 매도잔고비율 원값. 투자자별 데이터 기준 1거래일 lag 적용",
        "cfd_sell_balance_rate_chg5": "CFD 매도잔고비율의 5거래일 차이. 1거래일 lag 적용",
        "cfd_long_short_ratio": "(CFD 매수잔고수량 - 매도잔고수량) ÷ (매수+매도 잔고수량). -1~+1 포지션 편향; 1거래일 lag",
    }
    if low in kr:
        return kr[low]

    m = re.fullmatch(r"flow_prog_(arb|nonarb)_(net_norm|net_norm_5|net_norm_20|net_z20|net_z60|net_streak)", low)
    if m:
        actor = "프로그램 차익" if m.group(1) == "arb" else "프로그램 비차익"
        desc = {
            "net_norm": f"{actor} 순매수수량 ÷ 종목 20일 평균 거래량",
            "net_norm_5": f"{actor} 순매수 정규화값의 최근 5일 합",
            "net_norm_20": f"{actor} 순매수 정규화값의 최근 20일 합",
            "net_z20": f"{actor} 원시 순매수수량의 20일 Z-score",
            "net_z60": f"{actor} 원시 순매수수량의 60일 Z-score",
            "net_streak": f"{actor} 순매수 부호의 최근 5일 합(-5~+5). 실제 연속일수보다 방향 균형에 가까움",
        }[m.group(2)]
        return desc + ". 1거래일 lag 적용"
    if low == "flow_prog_total_net_norm":
        return "(프로그램 차익 순매수 + 비차익 순매수) ÷ 종목 20일 평균 거래량. 1거래일 lag 적용"

    short = {
        "short_ratio": "토스 shortSellingVolumeRate 원값; 없으면 공매도수량 ÷ 종목 20일 평균 거래량. 1거래일 lag 적용",
        "short_amount_ratio": "토스 shortSellingAmountRate 원값. 1거래일 lag 적용",
        "short_vol_norm": "공매도수량 ÷ 종목 20일 평균 거래량. 1거래일 lag 적용",
        "short_ratio_ma5": "short_ratio의 5일 평균. 1거래일 lag 적용",
        "short_ratio_ma20": "short_ratio의 20일 평균. 1거래일 lag 적용",
        "short_ratio_chg": "short_ratio 5일 평균의 전일 대비 변화율. 1거래일 lag 적용",
        "short_ratio_z20": "short_ratio의 20일 Z-score. 1거래일 lag 적용",
        "short_ratio_z60": "short_ratio의 60일 Z-score. 1거래일 lag 적용",
        "short_vol_chg5": "공매도수량 5일 평균의 5거래일 전 대비 변화율. 1거래일 lag 적용",
        "short_ma5_over_ma20": "short_ratio 5일 평균 ÷ 20일 평균. 최근 공매도 비중의 확대/축소; 1거래일 lag",
    }
    if low in short:
        return short[low]

    m = re.fullmatch(r"(margin_loan|stock_loan)_(balance_rate|trading_rate|balance_chg5|balance_chg20|balance_z60|net_new_norm|net_new_norm_20)", low)
    if m:
        actor = "신용융자" if m.group(1) == "margin_loan" else "신용대주"
        desc = {
            "balance_rate": f"{actor} 잔고비율 원값",
            "trading_rate": f"{actor} 거래비율 원값",
            "balance_chg5": f"{actor} 잔고수량의 5거래일 변화율",
            "balance_chg20": f"{actor} 잔고수량의 20거래일 변화율",
            "balance_z60": f"{actor} 잔고수량의 60일 Z-score",
            "net_new_norm": f"({actor} 신규수량 - 상환수량) ÷ 종목 20일 평균 거래량",
            "net_new_norm_20": f"({actor} 신규-상환)/20일 평균거래량 정규화값의 최근 20일 합",
        }[m.group(2)]
        return desc + ". 신용거래 데이터는 보수적으로 2거래일 lag 적용"
    if low == "credit_long_short_ratio":
        return "(신용융자 잔고수량 - 신용대주 잔고수량) ÷ (두 잔고수량 합). 레버리지 롱/숏 편향; 2거래일 lag"

    lending = {
        "lending_balance_chg5": "대차잔고수량의 5거래일 변화율. 1거래일 lag 적용",
        "lending_balance_chg20": "대차잔고수량의 20거래일 변화율. 1거래일 lag 적용",
        "lending_balance_z60": "대차잔고수량의 60일 Z-score. 1거래일 lag 적용",
        "lending_balance_norm": "대차잔고수량 ÷ 종목 60일 평균 거래량. 1거래일 lag 적용",
        "lending_net_norm": "(대차 체결수량 - 상환수량) ÷ 종목 20일 평균 거래량. 1거래일 lag 적용",
        "lending_net_norm_20": "위 대차 순증 정규화값의 최근 20일 합. 1거래일 lag 적용",
        "lending_balance_amt_norm": "대차잔고금액 ÷ [종가×거래량의 20일 평균]. 시가총액이 아니라 거래대금 proxy로 정규화; 1거래일 lag",
    }
    if low in lending:
        return lending[low]

    regime = {
        "regime_trend_up": "종가가 200일 이동평균 위면 1, 아니면 0. 장기 추세 regime",
        "regime_trend_mid_up": "종가가 60일 이동평균 위면 1, 아니면 0. 중기 추세 regime",
        "regime_trend_score": "(종가>SMA60) + (SMA60>SMA200) - 1. -1/0/1의 추세 정렬 점수",
        "regime_vol_pct": "20일 로그수익률 변동성의 expanding percentile rank. 미래값 없이 현재 변동성 위치를 0~1로 표시",
        "regime_high_vol": "regime_vol_pct > 0.7이면 1. 과거 대비 상위 30% 고변동 국면",
        "regime_low_vol": "regime_vol_pct < 0.3이면 1. 과거 대비 하위 30% 저변동 국면",
        "regime_volume_expansion": "20일 평균 거래량 ÷ 60일 평균 거래량 > 1.1이면 1. 거래량 확대 국면",
        "regime_volume_ratio": "20일 평균 거래량 ÷ 60일 평균 거래량",
        "regime_risk_on": "벤치마크가 60일 평균 위이면서 벤치마크 20일 변동성 percentile<0.6일 때 1",
        "regime_market_drawdown": "벤치마크 현재값 ÷ 최근 120일 최고값 - 1. 시장 낙폭",
        "regime_bear_market": "벤치마크 120일 고점 대비 낙폭이 -15% 미만이면 1. 약세장 플래그",
        "regime_rate_up": "한국/미국 10년 국채금리가 20거래일 전보다 높으면 1",
        "regime_rate_level_pct": "10년 국채금리 수준의 expanding percentile rank",
        "regime_gmm": "20일 누적 로그수익률과 20일 변동성을 입력한 causal 3상태 GMM 레짐 번호(0=저변동,1=중간,2=고변동)",
        "regime_gmm_low_vol": "causal GMM 레짐이 0(LOW_VOL)이면 1",
        "regime_gmm_normal": "causal GMM 레짐이 1(NORMAL)이면 1",
        "regime_gmm_high_vol": "causal GMM 레짐이 2(HIGH_VOL)이면 1",
    }
    if low in regime:
        return regime[low]

    return f"정의 미확인: {n}"


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




def _feature_group_lookup(feature_groups: Dict) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    labels = {
        "technical": "기술",
        "momentum": "모멘텀",
        "volatility": "변동성",
        "liquidity": "유동성",
        "market": "시장",
        "kr_flow": "수급",
        "regime": "국면",
        "kcs": "관세청",
    }
    if isinstance(feature_groups, dict):
        for group, cols in feature_groups.items():
            if not isinstance(cols, list):
                continue
            label = labels.get(str(group), str(group))
            for col in cols:
                c = str(col)
                lookup[c] = "관세청" if c.startswith("kcs_") else label

    for c, group in SOURCE_FEATURE_GROUP.items():
        lookup.setdefault(c, "관세청" if c.startswith("kcs_") else labels.get(group, group))
    return lookup


def render_all_feature_catalog(
    symbol: str,
    horizon: int,
    payload: Dict,
    diag: Dict,
    top_features: Dict,
) -> None:
    """
    코드상 전체 정의 → 이번 post-prune 후보 → 최종 선택 → 중요도 Top 10을 구분한다.
    """
    catalog = ((payload.get("feature_catalog") or {}).get(symbol) or {})
    candidates = catalog.get("candidate_features") or diag.get("candidate_features") or []
    selected = diag.get("selected_features") or []
    groups = catalog.get("feature_groups") or diag.get("feature_groups") or {}

    candidates = [str(x) for x in candidates if str(x).strip()]
    selected = [str(x) for x in selected if str(x).strip()]
    candidate_set = set(candidates)
    selected_set = set(selected)

    # JSON top_features는 최대 15개가 저장되므로 화면의 TOP 상태는 정확히 앞 10개만 사용.
    top_names = [str(k) for k in list((top_features or {}).keys())[:10]]
    top_set = set(top_names)
    group_lookup = _feature_group_lookup(groups)

    source_order: List[str] = []
    for group in ("technical", "momentum", "volatility", "liquidity", "market", "kr_flow", "regime"):
        for name in SOURCE_FEATURES_BY_GROUP.get(group, []):
            if name not in source_order:
                source_order.append(name)

    all_names = list(source_order)
    for name in candidates:
        if name not in all_names:
            all_names.append(name)

    candidate_unused = sum(1 for x in candidates if x not in selected_set)
    source_outside = sum(1 for x in source_order if x not in candidate_set)
    top_count = sum(1 for x in candidates if x in top_set)

    st.markdown(
        "<div class='feature-catalog-summary'>"
        f"<span class='feature-catalog-chip'>코드 정의 <strong>{len(source_order)}</strong></span>"
        f"<span class='feature-catalog-chip'>이번 후보 <strong>{len(candidates)}</strong></span>"
        f"<span class='feature-catalog-chip'>최종 선택 <strong>{len(selected)}</strong></span>"
        f"<span class='feature-catalog-chip'>후보 미선택 <strong>{candidate_unused}</strong></span>"
        f"<span class='feature-catalog-chip'>후보외 <strong>{source_outside}</strong></span>"
        f"<span class='feature-catalog-chip'>Top 10 <strong>{top_count}</strong></span>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "TOP=최종 중요도 상위 10개 · 선택=최종 모델 입력 Feature · 미선택=이번 candidate에는 있었지만 "
        "최종 상위 K 선택에서 제외 · 후보외=코드에는 정의되어 있으나 이번 종목의 post-prune 후보에는 없음. "
        "후보외의 정확한 제외 원인은 현재 snapshot만으로 비대상/원천데이터 미가용/결측률 pruning을 완전히 분리할 수 없습니다."
    )

    view = st.radio(
        "Feature 표시 범위",
        ["전체", "TOP+선택", "미선택 후보", "후보외"],
        horizontal=True,
        key=f"feature_catalog_view_{symbol}_{horizon}",
        label_visibility="collapsed",
    )

    status_rank = {"TOP": 0, "선택": 1, "미선택": 2, "후보외": 3}
    indexed = []
    for order, name in enumerate(all_names):
        if name in top_set:
            status = "TOP"
        elif name in selected_set:
            status = "선택"
        elif name in candidate_set:
            status = "미선택"
        else:
            status = "후보외"

        if view == "TOP+선택" and status not in {"TOP", "선택"}:
            continue
        if view == "미선택 후보" and status != "미선택":
            continue
        if view == "후보외" and status != "후보외":
            continue
        indexed.append((status_rank[status], order, name, status))

    indexed.sort(key=lambda x: (x[0], x[1]))

    rows = []
    for _, _, name, status in indexed:
        if status == "TOP":
            cls = "feature-status-top"
        elif status == "선택":
            cls = "feature-status-selected"
        elif status == "미선택":
            cls = "feature-status-unused"
        else:
            cls = "feature-status-outside"

        group = group_lookup.get(name, "기타")
        rows.append(
            "<tr>"
            f"<td data-label='상태'><span class='feature-status {cls}'>{status}</span></td>"
            f"<td data-label='분류'>{html.escape(group)}</td>"
            f"<td data-label='Feature'>{html.escape(name)}</td>"
            f"<td data-label='정의'>{html.escape(feature_meaning(name))}</td>"
            "</tr>"
        )

    if not rows:
        st.caption("현재 필터에 해당하는 Feature가 없습니다.")
        return

    st.markdown(
        "<div class='feature-catalog-wrap'>"
        "<table class='feature-catalog'>"
        "<thead><tr><th>상태</th><th>분류</th><th>Feature</th><th>정확한 정의</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody>"
        "</table></div>",
        unsafe_allow_html=True,
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
    # 위험 과소/과대평가 경고는 같은 데이터로 폭을 재조정한 coverage가 아니라
    # 보정 전 honest coverage를 우선 사용한다.
    cov = num(p.get("raw_coverage_80"))
    if cov is None:
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
            config={"displayModeBar": False, "responsive": True},
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
                fig.add_annotation(
                    x=fx[-1], y=c50[-1], text=f"{price(c50[-1], currency, False)} ",
                    showarrow=False, xanchor="right",
                    bgcolor="rgba(8,11,16,0.72)", borderpad=2,
                    font=dict(color=FCOL, size=11), row=1, col=1,
                )
            fig.add_vline(x=last_date,
                          line=dict(color="rgba(255,255,255,0.22)", width=1, dash="dot"))

    fig.update_layout(
        template="plotly_dark", height=510 if show_volume else 445,
        margin=dict(l=8, r=18, t=16, b=10), paper_bgcolor=BG, plot_bgcolor=BG,
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
    st.plotly_chart(
        candle_chart(hist, p, lookback, show_volume),
        use_container_width=True, key=f"candle_{uid}",
        config={"displayModeBar": False, "responsive": True},
    )
    st.caption(
        "음영은 P10-P90 / P25-P75 **분포 범위**입니다. 위 수치 카드의 80% 예측구간은 "
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

    # 모델 가중치/Feature 중요도는 predictions 행이 아니라 diagnostics에 저장된다.
    # main.py의 latest_predictions.json 구조를 그대로 사용한다.
    diag = (((payload.get("diagnostics") or {}).get(symbol) or {}).get(str(horizon)) or {})

    with st.expander("모델 진단"):
        # 검증 성능과 모델 가중치는 Streamlit columns 대신 자체 반응형 grid로 렌더링한다.
        # 모바일에서 반쪽 폭으로 찌그러지지 않고 확실히 1열로 쌓인다.
        render_diag_overview(p, diag)

        # 실행/학습 메타정보는 두 열 아래의 공통 행으로 내려서 좌우 높이 불균형을 없앤다.
        trained_raw = str(p.get("trained_at") or "—")
        trained_display = trained_raw.replace("T", " ")
        if len(trained_display) >= 16:
            trained_display = trained_display[:16]

        info = [
            ("Fallback level", str(p.get("fallback_level")),
             "1이 가장 완전한 구성"),
            ("마지막 데이터", str(p.get("last_data_time")),
             "모델 입력 마지막 확정 거래일"),
            ("학습 시각", trained_display,
             "현재 게시 모델의 재학습 시각"),
        ]
        sh = num(p.get("shrinkage"))
        if sh is not None and sh < 0.999:
            info.insert(1, (
                "과대외삽 보정", f"x{sh:.2f}",
                "예측을 0수익률 방향으로 축소"
            ))
        if p.get("missing_data"):
            info.append((
                "누락 데이터", str(p.get("missing_data")),
                "이번 학습에서 자동 제외된 데이터"
            ))

        meta_cards = []
        for label, value, desc in info:
            meta_cards.append(
                "<div class='diag-meta-card'>"
                f"<div class='diag-meta-label'>{html.escape(str(label))}</div>"
                f"<div class='diag-meta-value'>{html.escape(str(value))}</div>"
                f"<div class='diag-meta-desc'>{html.escape(str(desc))}</div>"
                "</div>"
            )
        st.markdown(
            "<div class='diag-meta-grid'>" + "".join(meta_cards) + "</div>",
            unsafe_allow_html=True,
        )

        # 실제 학습 과정에서 계산된 feature importance 중 상위 10개만 표시한다.
        # main.py가 latest_predictions.json -> diagnostics에 저장한 top_features를 그대로 사용하므로
        # Streamlit에서 중요도를 다시 계산하거나 추정하지 않는다.
        top_features = diag.get("top_features") or {}
        st.markdown("<div class='diag-section-title'>실제 학습 Feature Top 10<span>이름 · 의미 · 최종 모델 중요도</span></div>", unsafe_allow_html=True)
        render_feature_importance(top_features, limit=10)

        st.markdown("<div class='diag-section-title'>Feature 전체 사전<span>선택된 항목과 미선택 후보를 모두 표시</span></div>", unsafe_allow_html=True)
        render_all_feature_catalog(symbol, horizon, payload, diag, top_features)

        comps = p.get("confidence_components")
        if isinstance(comps, dict) and comps:
            st.markdown("**신뢰도 구성** — 100점 만점 신뢰도를 어떤 항목이 깎거나 받쳐주는지")
            st.caption("각 달성도는 독립적인 성공확률이 아니라 모델 신뢰도 점수를 구성하는 내부 진단값입니다.")
            label = {
                "baseline_improvement": "baseline 대비 RMSE 개선",
                "information_coefficient": "IC (순위 상관)",
                "directional_accuracy": "방향 정확도",
                "probability_calibration": "원확률 calibration",
                "interval_coverage": "보정 전 구간 커버리지",
                "fold_stability": "fold 간 안정성",
                "recent_regime": "최근 구간 성능",
                "oos_evidence": "OOS 표본 근거",
                "data_quantity": "학습 데이터 양",
                "data_freshness": "데이터 최신성",
                "feature_completeness": "feature 완결성",
            }
            rows = []
            for k, v in comps.items():
                if k.startswith("_") or k == "effective_oos_samples":
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                # 구성요소는 0~1 점수만 %로 표시한다. 과거 버전의 실효표본수 같은
                # 메타값이 1250%처럼 보이는 것을 막는다.
                if 0.0 <= fv <= 1.0:
                    rows.append({"항목": label.get(k, k), "달성도": f"{fv * 100:.0f}%"})
            if rows:
                render_dark_table(pd.DataFrame(rows))

            eff = num(p.get("effective_oos_samples"))
            if eff is None:
                eff = num(comps.get("_effective_oos_samples")) or num(comps.get("effective_oos_samples"))
            cap = num(comps.get("_sample_confidence_cap"))
            eval_eff = num(p.get("interval_eval_effective"))
            meta_bits = []
            if eff is not None:
                meta_bits.append(f"실효 OOS 표본≈{eff:.1f}")
            if eval_eff is not None:
                meta_bits.append(f"구간검증 실효표본≈{eval_eff:.1f}")
            if cap is not None and cap < 99.95:
                meta_bits.append(f"표본수 기반 신뢰도 상한 {cap:.0f}/100")
            if meta_bits:
                st.caption(" · ".join(meta_bits))
            if comps.get("_baseline_only_cap"):
                st.caption("ML 모델이 baseline 을 이기지 못해 신뢰도 상한 25가 적용되었습니다.")
            elif comps.get("_no_predictive_edge_cap"):
                st.caption(
                    "예측 edge가 확인되지 않았습니다: baseline RMSE 비개선 + IC<0.02 + "
                    "방향정확도<52% → 신뢰도는 LOW 범위(최대 44)로 제한됩니다."
                )
            elif comps.get("_weak_predictive_edge_cap"):
                st.caption(
                    "예측 edge가 아직 약합니다: RMSE 개선<0.5% + IC<0.03 + "
                    "방향정확도<53% → HIGH는 보류하고 최대 69점까지 허용합니다."
                )

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
                    st.plotly_chart(
                        fig, use_container_width=True, key=f"equity_{uid}",
                        config={"displayModeBar": False, "responsive": True},
                    )
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