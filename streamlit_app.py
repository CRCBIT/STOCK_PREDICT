"""
streamlit_app.py
================
Streamlit Cloud 용 **읽기 전용** 예측 대시보드 (다크).

토스 API 를 호출하지 않는다. `publish.py` 가 저장소에 올린 `published/` 스냅샷
(predictions.json 또는 predictions.csv)만 읽는다.

선택 기능은 다음 파일이 게시되면 자동으로 켜진다.
    prediction_history.csv  실행별 예측 변화
    memory_spot.csv         DRAM/NAND 현물가
    data_status.json        원천별 최신성
    assets.csv              내 보유수량·평균단가
    portfolio_forecast.json 상관관계 반영 손실확률·VaR·ES
    diagnostics.latest_contributions 최신 입력의 상승·하락 기여도

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
import hashlib
import html
import ipaddress
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parent
KST = ZoneInfo("Asia/Seoul")
PUBLISHED = ROOT / "published"
STALE_HOURS = 36
QUOTE_MAX_OPEN_MINUTES = 120
QUOTE_MAX_CLOSED_HOURS = 96
REANCHOR_MAX_ABS_MOVE = 0.15
REANCHOR_ATR_MULTIPLE = 3.0

DISCLAIMER = "통계 모델의 예측 분포이며 투자 조언이 아닙니다. 투자 판단의 책임은 이용자에게 있습니다."

# ---- 다크 팔레트 ---------------------------------------------------------------------
BG = "rgba(0,0,0,0)"
GRID = "rgba(255,255,255,0.075)"
TEXT = "#aeb9c7"      # Plotly 축/범례용: 본문보다 낮지만 충분히 읽히는 회색
UP = "#f23645"        # 상승 (국내 관행: 빨강)
DOWN = "#2196f3"      # 하락
FCOL = "#3182f6"      # 예측 (토스 블루)
DOT = {"HIGH": "●", "MEDIUM": "●", "LOW": "●"}

# 관세청 월별 수출단가. HBM은 전용 HS코드가 아니라 MCP를 대리지표로 표시한다.
KCS_MEMORY_SERIES = {
    "8542321010": "DRAM",
    "8542321030": "NAND Flash",
    "8542323000": "MCP / HBM proxy",
}
KCS_LOGIC_CODE = "8542311000"

st.set_page_config(
    page_title="주가 전망 대시보드",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# 통합 UI 스타일 — 과거 버전별 override에서 실제 사용 규칙만 정리한 단일 블록.
DASHBOARD_CSS = """
<style>
:root {
  --green: #3fb950;
  --red: #f85149;
}
#MainMenu, footer, header {
  visibility: hidden;
}
div[data-testid="stElementContainer"]:has(iframe[title*="streamlit_js_eval"]) {
  display: none;
}
.dash-hero {
  display: flex;
  justify-content: space-between;
  gap: 22px;
  border-bottom: 1px solid var(--line);
}
.dash-meta {
  font-size: 0.78rem;
  text-align: right;
  white-space: nowrap;
}
.section-head {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 12px;
}
.section-kicker {
  font-weight: 800;
  text-transform: uppercase;
}
div[data-testid="stMetric"] {
  background: linear-gradient(180deg, rgba(22,27,35,0.9), rgba(13,17,23,0.82));
  border: 1px solid var(--line);
  box-shadow: 0 7px 20px rgba(0,0,0,0.13);
}
div[data-testid="stMetric"]:hover {
  transition: 120ms ease;
}
[data-testid="stMetricLabel"] {
  color: var(--text-soft) !important;
  font-weight: 560;
}
[data-testid="stMetricValue"] {
  font-weight: 760;
  letter-spacing: -0.025em;
  color: var(--text);
}
div[data-testid="stSelectbox"], div[data-testid="stSelectSlider"], div[data-testid="stRadio"], div[data-testid="stCheckbox"] {
  font-size: 0.86rem;
}
div[data-testid="stSelectbox"] {
  background: transparent !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"], div[data-testid="stSelectbox"] [data-baseweb="select"] > div, div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div, div[data-testid="stSelectbox"] [data-baseweb="select"] input {
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
div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus, div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus-within {
  background-color: #111720 !important;
  border-color: rgba(240,185,11,0.28) !important;
  box-shadow: 0 0 0 1px rgba(240,185,11,0.06) !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] span, div[data-testid="stSelectbox"] [data-baseweb="select"] div {
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
div[data-testid="stSelectbox"] div[role="combobox"], div[data-testid="stSelectbox"] div[role="combobox"] > div, div[data-testid="stSelectbox"] div[role="combobox"] span, div[data-testid="stSelectbox"] [data-baseweb="select"] *, div[data-testid="stSelectbox"] input {
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
div[data-testid="stSelectbox"] div[role="combobox"]:focus, div[data-testid="stSelectbox"] div[role="combobox"]:focus-within {
  background-color: #111720 !important;
  border-color: rgba(240,185,11,0.28) !important;
  outline: none !important;
}
div[data-testid="stSelectbox"] div[role="combobox"] span {
  color: #d7dee8 !important;
}
div[data-testid="stSelectbox"] div[role="combobox"] svg {
  color: #a1adbb !important;
  fill: #a1adbb !important;
  background-color: transparent !important;
}
div[data-baseweb="popover"] {
  background: transparent !important;
}
div[data-baseweb="popover"] > div, div[data-baseweb="menu"], ul[role="listbox"] {
  background-color: #0d1117 !important;
  border-color: var(--line-strong) !important;
  color: var(--text-soft) !important;
}
li[role="option"] {
  background-color: #0d1117 !important;
  color: var(--text-soft) !important;
}
li[role="option"]:hover, li[role="option"][aria-selected="true"] {
  background-color: #161b22 !important;
  color: var(--text) !important;
}
div[data-testid="stExpander"] details {
  background: rgba(13,17,23,0.55) !important;
  border-radius: 12px !important;
}
div[data-testid="stExpander"] details summary {
  background: rgba(13,17,23,0.90) !important;
  color: var(--text) !important;
  border: 1px solid var(--line) !important;
}
div[data-testid="stExpander"] details[open] summary {
  border-bottom-left-radius: 0 !important;
  border-bottom-right-radius: 0 !important;
  border-bottom-color: rgba(255,255,255,0.06) !important;
}
div[data-testid="stExpander"] details summary p, div[data-testid="stExpander"] details summary span, div[data-testid="stExpander"] details summary svg {
  color: var(--text) !important;
  fill: var(--text) !important;
}
div[role="radiogroup"] label {
  border: 1px solid var(--line) !important;
  color: var(--muted) !important;
  transition: 120ms ease;
}
div[role="radiogroup"] label:has(input:checked) {
  color: var(--text) !important;
}
div[data-testid="stRadio"] input[type="radio"] {
  accent-color: #f0b90b !important;
}
.stTabs button[data-baseweb="tab"] {
  padding-left: 14px;
  padding-right: 14px;
}
.stTabs [data-baseweb="tab-highlight"] {
  background-color: #f0b90b !important;
}
.stTabs [data-baseweb="tab-border"] {
  background-color: transparent !important;
}
.verdict {
  color: #d0d8e2;
  font-size: 0.91rem;
  line-height: 1.55;
}
div[data-testid="stExpander"] {
  border: 1px solid var(--line);
  overflow: hidden;
}
div[data-testid="stDataFrame"] {
  border: 1px solid var(--line);
  border-radius: 10px;
  overflow: hidden;
}
div[data-testid="stPlotlyChart"] {
  border: 1px solid var(--line);
  padding: 2px;
  overflow: hidden;
}
div[data-testid="stPlotlyChart"] .xtick text, div[data-testid="stPlotlyChart"] .ytick text {
  fill: #aeb9c7 !important;
}
.status-strip {
  display: flex;
  flex-wrap: wrap;
}
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--line);
  border-radius: 999px;
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
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  overflow-wrap: anywhere;
}
.feature-meaning {
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
  font-size: 0.72rem;
  font-variant-numeric: tabular-nums;
  text-align: right;
}
.model-weight-list {
  display: grid;
  margin: 7px 0 11px 0;
}
.model-weight-row {
  display: grid;
  align-items: center;
  gap: 9px;
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
hr {
  border-color: var(--line) !important;
}
[data-testid="stAppViewContainer"] {
  color: var(--text);
}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p, .stCaption, .stCaption p {
  color: var(--muted) !important;
  opacity: 1 !important;
  line-height: 1.5;
}
div[data-testid="stMarkdownContainer"] > p {
  color: var(--text-soft);
}
div[data-testid="stSelectbox"] > label p, div[data-testid="stSelectSlider"] > label p, div[data-testid="stSlider"] > label p, div[data-testid="stRadio"] > label p, div[data-testid="stCheckbox"] label p, div[data-testid="stCheckbox"] label span {
  color: var(--text-soft) !important;
  opacity: 1 !important;
}
div[role="radiogroup"] label, div[role="radiogroup"] label p, div[role="radiogroup"] label span {
  color: var(--muted) !important;
  opacity: 1 !important;
}
div[role="radiogroup"] label:has(input:checked), div[role="radiogroup"] label:has(input:checked) p, div[role="radiogroup"] label:has(input:checked) span {
  color: var(--text) !important;
}
div[data-testid="stCheckbox"] svg, [data-testid="stTooltipHoverTarget"] svg {
  color: var(--muted) !important;
  fill: var(--muted) !important;
}
div[data-testid="stSlider"] [data-baseweb="slider"] div {
  color: var(--text-soft);
}
div[data-testid="stExpander"] [data-testid="stMarkdownContainer"] p {
  color: var(--muted) !important;
}
div[data-testid="stExpander"] details summary p, div[data-testid="stExpander"] details summary span {
  color: var(--text-soft) !important;
  font-weight: 600;
}
div[data-testid="stAlert"] p, div[data-testid="stAlert"] span {
  color: var(--text-soft) !important;
}
div[data-testid="stDataFrame"] + div, div[data-testid="stDataFrame"] ~ div[data-testid="stCaptionContainer"] {
  color: var(--muted) !important;
}
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
button[aria-label*="help" i] svg, button[aria-label*="tooltip" i] svg, button[aria-label*="도움" i] svg {
  color: #d9a90d !important;
  fill: #d9a90d !important;
  transition: color 0.16s ease, fill 0.16s ease, filter 0.16s ease;
}
button[aria-label*="help" i]:hover svg, button[aria-label*="tooltip" i]:hover svg, button[aria-label*="도움" i]:hover svg {
  color: #ffd54a !important;
  fill: #ffd54a !important;
  filter: drop-shadow(0 0 4px rgba(240,185,11,0.38));
}
[data-baseweb="popover"] > div, [role="tooltip"] {
  background: #11161d !important;
  color: #e8edf3 !important;
  border: 1px solid rgba(240,185,11,0.28) !important;
  border-radius: 9px !important;
  box-shadow: 0 10px 28px rgba(0,0,0,0.34) !important;
}
@media (max-width: 850px) {
  .dash-hero {
    align-items: flex-start;
    flex-direction: column;
  }
  .dash-meta {
    text-align: left;
    white-space: normal;
  }
  .block-container {
    padding-left: 0.9rem;
    padding-right: 0.9rem;
  }
  .feature-row {
    grid-template-columns: 24px minmax(125px, 0.9fr) minmax(150px, 1.35fr) 64px;
  }
  .feature-track {
    display: none;
  }
}
div[data-testid="stSelectbox"] [data-baseweb="select"] > div, div[data-testid="stSelectbox"] div[role="combobox"] {
  outline: none !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus, div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus-within, div[data-testid="stSelectbox"] div[role="combobox"]:focus, div[data-testid="stSelectbox"] div[role="combobox"]:focus-within {
  border-color: rgba(240,185,11,0.24) !important;
}
div[data-testid="stExpander"], div[data-testid="stExpander"] details, div[data-testid="stExpander"] details summary, div[data-testid="stMetric"], div[data-testid="stPlotlyChart"], div[data-testid="stDataFrame"] {
  border-color: rgba(120,132,148,0.16) !important;
}
div[data-testid="stExpander"] details summary:hover {
  border-color: rgba(120,132,148,0.24) !important;
}
*:focus-visible {
  outline-color: rgba(240,185,11,0.22) !important;
}
div[data-testid="stSelectbox"], div[data-testid="stSelectbox"] [data-baseweb="select"], div[data-testid="stSelectbox"] [data-baseweb="select"] > div, div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div, div[data-testid="stSelectbox"] div[role="combobox"] {
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
div[data-testid="stSelectbox"] [data-baseweb="select"] > div, div[data-testid="stSelectbox"] div[role="combobox"] {
  background: #0d1117 !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] * {
  outline: none !important;
  box-shadow: none !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] > div:hover, div[data-testid="stSelectbox"] div[role="combobox"]:hover {
  border: 0 !important;
  outline: 0 !important;
  box-shadow: none !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus, div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus-within, div[data-testid="stSelectbox"] div[role="combobox"]:focus, div[data-testid="stSelectbox"] div[role="combobox"]:focus-within {
  background: #111720 !important;
  border: 0 !important;
  outline: 0 !important;
  box-shadow: none !important;
}
div[data-testid="stSelectbox"] *:focus, div[data-testid="stSelectbox"] *:focus-visible {
  border: 0 !important;
  outline: 0 !important;
  box-shadow: none !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] {
  box-shadow: inset 0 0 0 0 transparent !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div:last-child, div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div:last-child > div, div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div:last-child span, div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div:last-child button, div[data-testid="stSelectbox"] [data-baseweb="select"] [data-baseweb="icon"], div[data-testid="stSelectbox"] [data-baseweb="select"] svg {
  background: #0d1117 !important;
  background-color: #0d1117 !important;
  border: 0 !important;
  outline: 0 !important;
  box-shadow: none !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div:last-of-type {
  background: #0d1117 !important;
  background-color: #0d1117 !important;
  border-left: 0 !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] svg, div[data-testid="stSelectbox"] [data-baseweb="select"] svg path {
  color: #aeb9c7 !important;
  fill: #aeb9c7 !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"], div[data-testid="stSelectbox"] [data-baseweb="select"] > div, div[data-testid="stSelectbox"] div[role="combobox"] {
  border: none !important;
  outline: none !important;
  box-shadow: none !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"], div[data-testid="stSelectbox"] [data-baseweb="select"] > div, div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div {
  background-color: #0d1117 !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"]:hover, div[data-testid="stSelectbox"] [data-baseweb="select"]:hover > div, div[data-testid="stSelectbox"] [data-baseweb="select"]:hover > div > div {
  background-color: #111720 !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] span, div[data-testid="stSelectbox"] [data-baseweb="select"] div[role="combobox"], div[data-testid="stSelectbox"] [data-baseweb="select"] div[role="combobox"] span, div[data-testid="stSelectbox"] [data-baseweb="select"] input {
  color: #e8edf3 !important;
  -webkit-text-fill-color: #e8edf3 !important;
  opacity: 1 !important;
  font-weight: 620 !important;
}
div[data-testid="stSelectbox"] > label p {
  color: #b8c2cf !important;
  opacity: 1 !important;
  font-weight: 560 !important;
}
ul[role="listbox"] li[role="option"], ul[role="listbox"] li[role="option"] span, div[data-baseweb="menu"] li[role="option"], div[data-baseweb="menu"] li[role="option"] span {
  color: #d7dee8 !important;
  -webkit-text-fill-color: #d7dee8 !important;
  opacity: 1 !important;
  font-weight: 540 !important;
}
ul[role="listbox"] li[role="option"]:hover, ul[role="listbox"] li[role="option"][aria-selected="true"] {
  color: #f0f3f7 !important;
}
div[data-testid="stSelectbox"] input, div[data-testid="stSelectbox"] input::placeholder, div[data-testid="stSelectbox"] [data-baseweb="select"] input, div[data-testid="stSelectbox"] [data-baseweb="select"] input::placeholder {
  color: #e8edf3 !important;
  -webkit-text-fill-color: #e8edf3 !important;
  opacity: 1 !important;
  font-weight: 600 !important;
}
.dash-table-wrap {
  border: 1px solid rgba(120,132,148,0.16);
  border-radius: 10px;
  background: #0d1117;
  margin: 2px 0 7px 0;
}
table.dash-table {
  border-collapse: collapse;
  border-spacing: 0;
  background: #0d1117;
  color: #d7dee8;
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
table.dash-table td + td, table.dash-table th + th {
  border-left: 1px solid rgba(120,132,148,0.08);
}
table.dash-table tbody td:not(:first-child) {
  color: #e4e9ef;
}
html {
  scrollbar-color: #2a323d #080b10;
  scrollbar-width: thin;
}
* {
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
::selection {
  color: #ffffff;
}
.dash-hero {
  padding-top: 10px;
  padding-bottom: 19px;
  margin-bottom: 12px;
}
.dash-meta {
  line-height: 1.55;
  font-variant-numeric: tabular-nums;
}
.section-head {
  position: relative;
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
.status-strip {
  margin-top: 1px;
  margin-bottom: 17px;
}
div[data-testid="stMetric"] {
  position: relative;
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
[data-testid="stMetricValue"], [data-testid="stMetricDelta"] {
  font-variant-numeric: tabular-nums;
}
div[role="radiogroup"] label {
  align-items: center;
}
div[data-testid="stCheckbox"] label {
  min-height: 32px;
}
div[data-testid="stSlider"], div[data-testid="stSelectSlider"] {
  padding-top: 1px;
}
div[data-testid="stExpander"] details[open] {
  background: rgba(13,17,23,0.64) !important;
  box-shadow: 0 8px 24px rgba(0,0,0,0.10);
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
.feature-head > div:first-child, .feature-head > div:last-child {
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
.feature-row:nth-child(1) .feature-rank, .feature-row:nth-child(2) .feature-rank, .feature-row:nth-child(3) .feature-rank {
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
  .feature-head, .feature-row {
    grid-template-columns: 26px minmax(135px, 0.95fr) minmax(190px, 1.45fr) 72px;
  }
  .feature-head > div:nth-child(4), .feature-track {
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
    grid-template-areas: "rank name score"
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
.feature-catalog-chip strong {
  color: #f0b90b;
}
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
table.feature-catalog tbody tr:hover td {
  background: #111720;
}
table.feature-catalog th:nth-child(1), table.feature-catalog td:nth-child(1) {
  width: 76px;
}
table.feature-catalog th:nth-child(2), table.feature-catalog td:nth-child(2) {
  width: 92px;
}
table.feature-catalog th:nth-child(3), table.feature-catalog td:nth-child(3) {
  width: 245px;
}
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
  table.feature-catalog th:nth-child(2), table.feature-catalog td:nth-child(2) {
    display: none;
  }
  table.feature-catalog th:nth-child(3), table.feature-catalog td:nth-child(3) {
    width: 150px;
  }
}
@media (max-width: 600px) {
  .feature-catalog-wrap {
    max-height: none;
    overflow: visible;
    padding: 6px;
  }
  table.feature-catalog, table.feature-catalog tbody, table.feature-catalog tr, table.feature-catalog td {
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
html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
  overflow-x: hidden !important;
}
.block-container {
  width: min(100%, 1440px) !important;
}
.status-strip {
  margin: 0 0 13px 0;
  gap: 6px;
}
.status-pill {
  box-sizing: border-box;
}
div[data-testid="stHorizontalBlock"] {
  gap: 0.78rem !important;
}
div[data-testid="stColumn"] {
  min-width: 0 !important;
}
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
div[data-testid="stPlotlyChart"], div[data-testid="stPlotlyChart"] > div {
  width: 100% !important;
  max-width: 100% !important;
  min-width: 0 !important;
  box-sizing: border-box !important;
}
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
table.dash-table th, table.dash-table td {
  overflow-wrap: anywhere;
  word-break: keep-all;
}
.dash-table-wrap.cols-3 table.dash-table th:nth-child(1), .dash-table-wrap.cols-3 table.dash-table td:nth-child(1) {
  width: 23%;
}
.dash-table-wrap.cols-3 table.dash-table th:nth-child(2), .dash-table-wrap.cols-3 table.dash-table td:nth-child(2) {
  width: 16%;
  white-space: nowrap;
}
.dash-table-wrap.cols-3 table.dash-table th:nth-child(3), .dash-table-wrap.cols-3 table.dash-table td:nth-child(3) {
  width: 61%;
}
.dash-table-wrap.cols-2 table.dash-table th:first-child, .dash-table-wrap.cols-2 table.dash-table td:first-child {
  width: 42%;
}
.model-weight-list {
  gap: 6px;
}
.model-weight-row {
  grid-template-columns: minmax(115px, 0.82fr) minmax(150px, 1.7fr) 58px;
  min-height: 32px;
  padding: 5px 7px;
}
.model-weight-track {
  min-width: 60px;
}
.feature-head, .feature-row {
  grid-template-columns: 26px minmax(150px, 0.95fr) minmax(270px, 1.75fr) minmax(90px, 0.65fr) 72px;
}
div[data-testid="stExpander"] {
  margin-top: 7px;
}
div[data-testid="stExpander"] details > div {
  padding-top: 2px;
}
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
  .dash-hero {
    align-items: flex-start;
  }
  .section-note {
    max-width: 50%;
  }
  .feature-head, .feature-row {
    grid-template-columns: 25px minmax(130px, 0.9fr) minmax(190px, 1.5fr) 68px;
  }
  .feature-head > div:nth-child(4), .feature-track {
    display: none;
  }
}
@media (max-width: 760px) {
  .dash-hero {
    flex-direction: column;
  }
  .dash-subtitle {
    margin-top: 5px;
    font-size: 0.75rem;
    line-height: 1.42;
  }
  .dash-meta {
    padding-top: 7px;
    border-top: 1px solid rgba(120,132,148,0.11);
    font-size: 0.69rem;
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
    text-align: center;
    line-height: 1.25;
  }
  .status-dot {
    flex: 0 0 7px;
  }
  .section-head {
    flex-direction: column;
    align-items: flex-start;
  }
  .section-note {
    max-width: 100%;
    line-height: 1.4;
  }
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
  [data-testid="stMetricLabel"] {
    font-size: 0.66rem !important;
  }
  [data-testid="stMetricValue"] {
    font-size: 0.98rem !important;
    overflow-wrap: anywhere;
  }
  [data-testid="stMetricDelta"] {
    font-size: 0.67rem !important;
  }
  .verdict {
    font-size: 0.78rem;
    line-height: 1.48;
  }
  .dash-table-wrap {
    overflow: visible;
    border: 0;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
  }
  table.dash-table, table.dash-table tbody, table.dash-table tr, table.dash-table td {
    display: block;
    width: 100% !important;
    box-sizing: border-box;
  }
  table.dash-table thead {
    display: none;
  }
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
  .model-weight-row {
    gap: 6px;
  }
  .feature-row {
    background: rgba(13,17,23,0.78);
  }
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
  div[data-testid="stExpander"] details summary {
    padding-left: 9px !important;
    padding-right: 9px !important;
  }
  div[data-testid="stExpander"] details summary p {
    font-size: 0.75rem;
  }
  [data-testid="stCaptionContainer"] p, .stCaption p {
    font-size: 0.67rem !important;
    line-height: 1.5 !important;
  }
  hr {
    margin-top: 1.2rem !important;
    margin-bottom: 0.9rem !important;
  }
}
@media (max-width: 430px) {
  .status-pill {
    font-size: 0.60rem;
  }
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
  .diag-meta-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 620px) {
  .diag-meta-grid {
    grid-template-columns: 1fr;
    gap: 6px;
  }
  .diag-meta-card {
    padding: 8px 9px;
  }
}
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
.diag-perf-copy {
  min-width: 0;
}
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
  .diag-perf-label {
    font-size: 0.70rem !important;
  }
  .diag-perf-desc {
    font-size: 0.61rem !important;
    line-height: 1.38 !important;
    max-width: 100% !important;
  }
  .diag-perf-value {
    font-size: 0.74rem !important;
  }
  .model-weight-list {
    gap: 7px !important;
    margin: 0 !important;
  }
  .model-weight-row {
    display: grid !important;
    grid-template-columns: minmax(0, 1fr) auto !important;
    grid-template-areas: "mw-name mw-score"
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
  .diag-meta-label {
    font-size: 0.62rem !important;
  }
  .diag-meta-value {
    font-size: 0.73rem !important;
    line-height: 1.3 !important;
    overflow-wrap: anywhere !important;
  }
  .diag-meta-desc {
    display: none !important;
  }
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
  .feature-head {
    display: none !important;
  }
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
    grid-template-areas: "rank name score"
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
  .feature-score::before {
    font-size: 0.57rem !important;
  }
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
  .diag-meta-grid {
    grid-template-columns: 1fr 1fr !important;
  }
  .diag-meta-card {
    padding: 7px 8px !important;
  }
  .feature-row {
    padding: 9px !important;
  }
  .feature-score {
    max-width: 92px !important;
  }
}
.dash-meta {
  display: grid;
  justify-items: end;
  gap: 7px;
}
.dash-updated {
  font-size: 0.72rem;
}
.dashboard-facts {
  display: flex;
  flex-wrap: wrap;
}
.dashboard-fact {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border-radius: 999px;
  border: 1px solid rgba(120,132,148,0.13);
  font-size: 0.71rem;
  line-height: 1.2;
}
.dashboard-fact b {
  font-weight: 650;
}
.asset-picker-note {
  font-size: 0.79rem;
  line-height: 1.5;
  margin: -3px 0 6px 1px;
}
.forecast-controls {
  font-size: 0.74rem;
  margin: 2px 0 3px 1px;
}
.verdict {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 10px;
  align-items: start;
}
.verdict-icon {
  display: grid;
  place-items: center;
  border-radius: 9px;
}
.verdict-body {
  min-width: 0;
}
.verdict-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
}
.verdict-title {
  min-width: 0;
  font-weight: 760;
  line-height: 1.35;
}
.verdict-confidence {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 999px;
  background: rgba(255,255,255,0.035);
  color: #aeb6bf;
  font-weight: 730;
  line-height: 1.2;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.verdict-confidence::before {
  content: "";
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: #7f8995;
  box-shadow: 0 0 0 3px rgba(127,137,149,0.10);
}
.verdict-confidence.high {
  color: #67dbb9;
  border-color: rgba(32,201,151,0.22);
  background: rgba(32,201,151,0.075);
}
.verdict-confidence.high::before {
  background: #41c79f;
  box-shadow: 0 0 0 3px rgba(32,201,151,0.11);
}
.verdict-confidence.medium {
  color: #f2c94c;
  border-color: rgba(242,201,76,0.24);
  background: rgba(242,201,76,0.075);
}
.verdict-confidence.medium::before {
  background: #e7b93d;
  box-shadow: 0 0 0 3px rgba(242,201,76,0.10);
}
.verdict-confidence.low {
  color: #aab4bf;
  border-color: rgba(170,180,191,0.16);
  background: rgba(170,180,191,0.045);
}
.verdict-copy {
  line-height: 1.52;
  margin-top: 3px;
}
.forecast-metric {
  min-width: 0;
  box-sizing: border-box;
  border: 1px solid rgba(120,132,148,0.16);
  border-radius: 13px;
  background: linear-gradient(180deg, rgba(22,27,35,0.88), rgba(13,17,23,0.76));
  box-shadow: 0 7px 20px rgba(0,0,0,0.10);
}
.forecast-metric-label {
  font-size: 0.70rem;
  font-weight: 650;
  line-height: 1.28;
}
.forecast-metric-value {
  font-weight: 760;
  line-height: 1.25;
  letter-spacing: -0.025em;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
  margin-top: 7px;
}
.forecast-metric-sub {
  font-size: 0.67rem;
  line-height: 1.34;
  margin-top: 5px;
}
.forecast-metric-sub.neutral {
  color: #aab5c2;
}
.reading-guide {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 10px;
  align-items: start;
  margin: 0 0 13px 0;
  border: 1px solid rgba(88,166,255,0.16);
}
.reading-guide-label {
  font-weight: 760;
  white-space: nowrap;
  padding-top: 1px;
}
.reading-guide-copy {
  line-height: 1.55;
}
.reading-guide-copy b {
  color: #e1e8f0;
}
.chart-caption {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 13px;
  margin: 7px 2px 3px 2px;
  font-size: 0.71rem;
  line-height: 1.45;
}
.chart-caption span::before {
  content: "";
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 6px;
  border-radius: 50%;
  vertical-align: 1px;
  opacity: 0.85;
}
.help-list {
  display: grid;
  gap: 9px;
  margin: 4px 0 3px 0;
}
.help-item {
  display: grid;
  grid-template-columns: minmax(105px, 0.28fr) minmax(0, 1fr);
  gap: 12px;
  padding: 9px 10px;
  border-bottom: 1px solid rgba(120,132,148,0.10);
}
.help-item:last-child {
  border-bottom: 0;
}
.help-term {
  color: #d5dde7;
  font-size: 0.75rem;
  font-weight: 700;
}
.help-desc {
  color: #aab6c3;
  font-size: 0.74rem;
  line-height: 1.52;
}
@media (max-width: 760px) {
  .dashboard-facts {
    margin-top: 8px;
  }
  .forecast-metric {
    min-height: 96px;
    padding: 11px;
  }
  .reading-guide {
    grid-template-columns: 1fr;
    gap: 4px;
  }
  .help-item {
    grid-template-columns: 1fr;
    gap: 3px;
    padding: 9px 5px;
  }
  .stTabs [data-baseweb="tab-list"] {
    justify-content: flex-start;
  }
  .stTabs button[data-baseweb="tab"] {
    padding-left: 10px;
    padding-right: 10px;
  }
}
@media (max-width: 440px) {
  .forecast-metric-value {
    font-size: 0.96rem;
  }
  .forecast-metric-sub {
    font-size: 0.64rem;
  }
}
:root {
  --bg: #080a0d;
  --panel: #11151b;
  --panel-2: #151a21;
  --line: rgba(255,255,255,0.065);
  --line-strong: rgba(255,255,255,0.12);
  --text: #f2f4f6;
  --text-soft: #d1d6db;
  --muted: #8b95a1;
  --muted-2: #6b7684;
  --accent: #3182f6;
  --blue: #3182f6;
}
[data-testid="stAppViewContainer"] {
  background: #080a0d !important;
}
.block-container {
  padding-top: 1rem !important;
  padding-left: clamp(1rem, 2.3vw, 2.35rem) !important;
  padding-right: clamp(1rem, 2.3vw, 2.35rem) !important;
  padding-bottom: 2.5rem !important;
}
::selection {
  background: rgba(49,130,246,0.28);
}
.dash-hero {
  align-items: center !important;
  padding: 19px 20px !important;
  margin: 0 0 10px 0 !important;
  border: 1px solid var(--line) !important;
  border-radius: 18px !important;
  background: #11151b !important;
  box-shadow: none !important;
}
.dash-title {
  color: #f2f4f6 !important;
  font-size: clamp(1.42rem, 2vw, 1.78rem) !important;
  font-weight: 760 !important;
  letter-spacing: -0.04em !important;
  line-height: 1.18 !important;
  text-transform: none !important;
  text-shadow: none !important;
}
.dash-subtitle {
  max-width: 720px !important;
  margin-top: 7px !important;
  color: #aeb6bf !important;
  font-size: 0.84rem !important;
  line-height: 1.55 !important;
}
.dash-meta {
  color: var(--muted) !important;
}
.dash-updated {
  color: #7f8995 !important;
}
.status-pill {
  min-height: 28px !important;
  padding: 6px 10px !important;
  border-color: rgba(255,255,255,0.07) !important;
  background: #151a21 !important;
  box-shadow: none !important;
  color: #b7c0ca !important;
}
.status-dot.warn {
  background: #ffb020 !important;
  box-shadow: 0 0 0 3px rgba(255,176,32,0.10) !important;
}
.dashboard-facts {
  gap: 6px !important;
  margin: 8px 2px 1px 2px !important;
}
.dashboard-fact {
  padding: 5px 9px !important;
  border-color: rgba(255,255,255,0.055) !important;
  background: rgba(17,21,27,0.62) !important;
  color: #808b98 !important;
}
.dashboard-fact b {
  color: #c9d0d8 !important;
}
.section-head {
  padding-left: 0 !important;
}
.section-kicker {
  display: none !important;
}
.section-title {
  color: #f2f4f6 !important;
}
.asset-picker-note {
  color: #8b95a1 !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"], div[data-testid="stSelectbox"] [data-baseweb="select"] > div, div[data-testid="stSelectbox"] [data-baseweb="select"] > div > div, div[data-testid="stSelectbox"] div[role="combobox"], div[data-testid="stSelectbox"] div[role="combobox"] > div, div[data-testid="stSelectbox"] [data-baseweb="select"] input {
  background: #11151b !important;
  background-image: none !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] > div, div[data-testid="stSelectbox"] div[role="combobox"] {
  min-height: 48px !important;
  border: 1px solid rgba(255,255,255,0.075) !important;
  border-radius: 13px !important;
  box-shadow: none !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] > div:hover, div[data-testid="stSelectbox"] div[role="combobox"]:hover {
  background: #151a21 !important;
  border-color: rgba(255,255,255,0.13) !important;
}
div[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus-within, div[data-testid="stSelectbox"] div[role="combobox"]:focus-within {
  border-color: rgba(49,130,246,0.55) !important;
  box-shadow: 0 0 0 3px rgba(49,130,246,0.10) !important;
}
div[data-baseweb="popover"] > div, div[data-baseweb="menu"], ul[role="listbox"] {
  background: #11151b !important;
}
li[role="option"] {
  background: #11151b !important;
}
li[role="option"]:hover, li[role="option"][aria-selected="true"] {
  background: #1b222c !important;
}
div[role="radiogroup"] label {
  min-height: 36px !important;
  box-sizing: border-box !important;
  padding: 7px 11px !important;
  border-color: rgba(255,255,255,0.07) !important;
  border-radius: 10px !important;
  background: #11151b !important;
}
div[role="radiogroup"] label:hover {
  background: #151a21 !important;
  border-color: rgba(255,255,255,0.12) !important;
}
div[role="radiogroup"] label:has(input:checked) {
  background: rgba(49,130,246,0.13) !important;
  border-color: rgba(49,130,246,0.42) !important;
  box-shadow: none !important;
}
div[data-testid="stRadio"] input[type="radio"], div[data-testid="stCheckbox"] input[type="checkbox"] {
  accent-color: #3182f6 !important;
}
div[data-testid="stSlider"] [data-baseweb="slider"] div[role="slider"] {
  background-color: #3182f6 !important;
  border-color: #3182f6 !important;
}
.stTabs [data-baseweb="tab-list"] {
  box-shadow: none !important;
  backdrop-filter: none !important;
}
.stTabs button[data-baseweb="tab"] {
  border-radius: 0 !important;
  background: transparent !important;
}
.stTabs button[data-baseweb="tab"]:hover {
  background: transparent !important;
  color: #c8cfd7 !important;
}
.verdict {
  padding: 13px 14px !important;
  margin: 7px 0 12px 0 !important;
  border: 1px solid rgba(255,255,255,0.065) !important;
  border-left: 3px solid #6b7684 !important;
  border-radius: 14px !important;
  background: #11151b !important;
  box-shadow: none !important;
}
.verdict.high {
  border-left-color: #20c997 !important;
}
.verdict.medium {
  border-left-color: #3182f6 !important;
}
.verdict.low {
  border-left-color: #6b7684 !important;
}
.verdict-icon {
  width: 24px !important;
  height: 24px !important;
  background: transparent !important;
  color: #6b7684 !important;
  font-size: 0.70rem !important;
}
.verdict.high .verdict-icon {
  color: #20c997 !important;
}
.verdict.medium .verdict-icon {
  color: #3182f6 !important;
}
.verdict.low .verdict-icon {
  color: #6b7684 !important;
}
.verdict-title {
  color: #f2f4f6 !important;
}
.verdict-copy {
  color: #aeb6bf !important;
}
.forecast-primary-card, .forecast-metric {
  box-sizing: border-box;
  border: 1px solid rgba(255,255,255,0.065) !important;
  border-radius: 16px !important;
  background: #11151b !important;
  box-shadow: none !important;
}
.forecast-metric {
  min-height: 103px !important;
  padding: 13px 14px 12px 14px !important;
}
.forecast-metric-label {
  color: #8b95a1 !important;
}
.forecast-metric-value {
  color: #edf0f3 !important;
  font-size: clamp(0.92rem, 1.25vw, 1.08rem) !important;
}
.forecast-metric-sub {
  color: #727d8a !important;
}
.forecast-metric-sub.up {
  color: #ff777e !important;
}
.forecast-metric-sub.down {
  color: #68adff !important;
}
.reading-guide {
  padding: 12px 14px !important;
  margin-bottom: 13px !important;
  border-color: rgba(49,130,246,0.18) !important;
  border-radius: 13px !important;
  background: rgba(49,130,246,0.055) !important;
}
.reading-guide-label {
  color: #78adff !important;
}
.reading-guide-copy {
  color: #adb7c2 !important;
}
div[data-testid="stPlotlyChart"] {
  border-color: rgba(255,255,255,0.06) !important;
  border-radius: 16px !important;
  background: #0d1116 !important;
  box-shadow: none !important;
}
.chart-caption {
  color: #7f8995 !important;
}
.chart-caption span::before {
  background: #3182f6 !important;
}
.chart-caption span:nth-child(2)::before {
  background: rgba(49,130,246,0.52) !important;
}
.chart-caption span:nth-child(3)::before {
  background: #626d79 !important;
}
div[data-testid="stExpander"] {
  border-color: rgba(255,255,255,0.06) !important;
  border-radius: 14px !important;
  background: #0d1116 !important;
}
div[data-testid="stExpander"] details, div[data-testid="stExpander"] details summary {
  background: #0d1116 !important;
}
div[data-testid="stExpander"] details summary {
  border-color: rgba(255,255,255,0.06) !important;
  border-radius: 14px !important;
}
div[data-testid="stExpander"] details summary:hover {
  background: #121820 !important;
}
.decision-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin: 6px 0 9px 0;
}
.decision-card {
  min-width: 0;
  padding: 12px 13px;
  border: 1px solid rgba(255,255,255,0.06);
  border-radius: 13px;
  background: #11151b;
}
.decision-label {
  color: #8b95a1;
  font-size: 0.69rem;
  font-weight: 650;
}
.decision-value {
  margin-top: 7px;
  color: #edf0f3;
  font-size: 0.96rem;
  font-weight: 730;
  letter-spacing: -0.025em;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.decision-sub {
  margin-top: 5px;
  color: #717c89;
  font-size: 0.64rem;
  line-height: 1.35;
}
.feature-fill, .model-weight-fill {
  background: linear-gradient(90deg, rgba(49,130,246,0.60), #3182f6) !important;
}
.feature-catalog-chip strong, .feature-row:nth-child(1) .feature-rank, .feature-row:nth-child(2) .feature-rank, .feature-row:nth-child(3) .feature-rank {
  color: #68a4ff !important;
}
.feature-status-top {
  color: #7db1ff !important;
  background: rgba(49,130,246,0.10) !important;
  border-color: rgba(49,130,246,0.25) !important;
}
[data-testid="stTooltipIcon"] svg, button[aria-label*="help" i] svg, button[aria-label*="tooltip" i] svg, button[aria-label*="도움" i] svg {
  color: #5e9dff !important;
  fill: #5e9dff !important;
  filter: none !important;
}
[data-testid="stTooltipIcon"]:hover svg, button[aria-label*="help" i]:hover svg, button[aria-label*="tooltip" i]:hover svg, button[aria-label*="도움" i]:hover svg {
  color: #8bbcff !important;
  fill: #8bbcff !important;
  filter: none !important;
}
[role="tooltip"] {
  border-color: rgba(49,130,246,0.24) !important;
  background: #151a21 !important;
}
@media (max-width: 980px) {
  .decision-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 470px) {
  .block-container {
    padding-left: 0.82rem !important;
    padding-right: 0.82rem !important;
  }
  .forecast-metric {
    min-height: 92px !important;
  }
  .decision-grid {
    grid-template-columns: 1fr 1fr;
    gap: 7px;
  }
  .decision-card {
    padding: 11px;
  }
  .stTabs [data-baseweb="tab-list"] {
    gap: 16px !important;
  }
  .stTabs button[data-baseweb="tab"] {
    font-size: 0.76rem !important;
  }
}
.reading-guide.compact {
  padding-top: 10px !important;
  padding-bottom: 10px !important;
  margin-top: 8px !important;
  margin-bottom: 10px !important;
}
.forecast-controls {
  margin-bottom: 1px !important;
  color: #8d97a3 !important;
  font-weight: 560 !important;
}
div[data-testid="stPlotlyChart"] {
  margin-top: 1px;
}
.chart-caption {
  margin-top: 5px !important;
  margin-bottom: 1px !important;
}
.section-head {
  margin-top: 20px !important;
  margin-bottom: 7px !important;
}
.verdict {
  margin-bottom: 9px !important;
  padding-top: 11px !important;
  padding-bottom: 11px !important;
}
@media (max-width: 760px) {
  .block-container {
    padding-top: 0.42rem !important;
    padding-bottom: 1.5rem !important;
  }
  .dash-hero {
    display: grid !important;
    grid-template-columns: minmax(0, 1fr) auto !important;
    align-items: center !important;
    gap: 8px !important;
    padding: 10px 11px !important;
    margin-bottom: 6px !important;
    border-radius: 12px !important;
  }
  .dash-title {
    font-size: 1.23rem !important;
  }
  .dash-subtitle {
    display: none !important;
  }
  .dash-meta {
    width: auto !important;
    padding: 0 !important;
    border: 0 !important;
    justify-items: end !important;
    text-align: right !important;
  }
  .dash-updated {
    display: none !important;
  }
  .status-pill {
    min-height: 25px !important;
    padding: 5px 7px !important;
    font-size: 0.62rem !important;
    white-space: nowrap !important;
  }
  .dashboard-facts {
    display: none !important;
  }
  .section-head {
    margin-top: 12px !important;
    margin-bottom: 5px !important;
    padding-left: 0 !important;
  }
  .asset-picker-note {
    display: none !important;
  }
  .stTabs [data-baseweb="tab-list"] {
    margin-top: 1px !important;
  }
  .forecast-controls {
    display: none !important;
  }
  div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] div[data-testid="stRadio"]) {
    display: grid !important;
    gap: 0.30rem 0.55rem !important;
    width: 100% !important;
    align-items: end !important;
  }
  div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] div[data-testid="stRadio"]) > div[data-testid="stColumn"] {
    width: 100% !important;
    min-width: 0 !important;
    flex: none !important;
  }
  div[data-testid="stRadio"] > label p, div[data-testid="stSelectSlider"] > label p, div[data-testid="stCheckbox"] label p {
    font-size: 0.64rem !important;
  }
  div[role="radiogroup"] label {
    min-height: 30px !important;
    padding: 4px 8px !important;
  }
  div[data-testid="stCheckbox"] label {
    min-height: 28px !important;
  }
  .verdict {
    grid-template-columns: minmax(0, 1fr) !important;
    gap: 0 !important;
    margin: 5px 0 7px 0 !important;
    padding: 8px 10px !important;
    border-radius: 10px !important;
  }
  .verdict-icon {
    display: none !important;
  }
  .verdict-head {
    gap: 7px !important;
    align-items: center !important;
  }
  .verdict-title {
    display: block !important;
    margin-right: 0 !important;
    line-height: 1.30 !important;
  }
  .verdict-confidence {
    gap: 4px !important;
    padding: 3px 6px !important;
  }
  .verdict-confidence::before {
    width: 6px !important;
    height: 6px !important;
    flex-basis: 6px !important;
    box-shadow: none !important;
  }
  .verdict-copy {
    display: block !important;
    margin-top: 3px !important;
    line-height: 1.38 !important;
  }
  div[data-testid="stPlotlyChart"] {
    border-radius: 12px !important;
    padding: 0 !important;
  }
  .chart-caption {
    gap: 4px 9px !important;
    margin: 4px 1px 1px 1px !important;
    line-height: 1.35 !important;
  }
  .chart-caption span::before {
    width: 5px !important;
    height: 5px !important;
    margin-right: 4px !important;
  }
  .reading-guide.compact {
    grid-template-columns: 1fr !important;
    gap: 2px !important;
    margin: 6px 0 8px 0 !important;
    border-radius: 10px !important;
  }
  div[data-testid="stExpander"] {
    margin-top: 5px !important;
    border-radius: 11px !important;
  }
  div[data-testid="stExpander"] details summary {
    min-height: 40px !important;
    border-radius: 11px !important;
  }
}
@media (max-width: 430px) {
  .block-container {
    padding-left: 0.60rem !important;
    padding-right: 0.60rem !important;
  }
  .dash-title {
    font-size: 1.16rem !important;
  }
  .section-note {
    display: none !important;
  }
}
div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] div[data-testid="stRadio"]) > div[data-testid="stColumn"]:nth-child(2) div[data-testid="stSelectbox"] [data-baseweb="select"] > div {
  min-height: 40px !important;
}
@media (max-width: 760px) {
  div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] div[data-testid="stRadio"]) {
    grid-template-columns: minmax(0, 1fr) minmax(0, 0.72fr) !important;
  }
  div[data-testid="stHorizontalBlock"]:has(> div[data-testid="stColumn"] div[data-testid="stRadio"]) > div[data-testid="stColumn"]:first-child {
    grid-column: 1 / -1 !important;
  }
  div[data-testid="stSelectbox"] > label p {
    font-size: 0.64rem !important;
  }
  div[data-testid="stSelectbox"] [data-baseweb="select"] > div, div[data-testid="stSelectbox"] div[role="combobox"] {
    min-height: 36px !important;
    border-radius: 9px !important;
  }
}
.mobile-help {
  display: none;
}
@media (max-width: 760px) {
  [data-testid="stTooltipIcon"], [data-testid="stTooltipHoverTarget"], button[aria-label*="help" i], button[aria-label*="tooltip" i], button[aria-label*="도움" i] {
    display: none !important;
  }
  .mobile-help {
    display: block;
    margin-top: 5px;
    color: #8f9baa;
    font-size: 0.68rem;
    line-height: 1.42;
  }
  .mobile-help summary {
    width: fit-content;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    list-style: none;
    cursor: pointer;
    user-select: none;
    -webkit-user-select: none;
    color: #9aa6b3;
    padding: 4px 7px;
    border: 1px solid rgba(120,132,148,0.16);
    border-radius: 999px;
    background: rgba(13,17,23,0.72);
    -webkit-tap-highlight-color: transparent;
  }
  .mobile-help summary::-webkit-details-marker {
    display: none;
  }
  .mobile-help summary::before {
    content: "i";
    display: inline-grid;
    place-items: center;
    width: 15px;
    height: 15px;
    border-radius: 50%;
    border: 1px solid rgba(88,166,255,0.38);
    color: #78adff;
    font-size: 0.60rem;
    font-weight: 800;
    line-height: 1;
  }
  .mobile-help[open] summary {
    color: #c5ced9;
    border-color: rgba(88,166,255,0.24);
    background: rgba(49,130,246,0.07);
  }
  .mobile-help-copy {
    margin-top: 6px;
    padding: 8px 9px;
    border-left: 2px solid rgba(88,166,255,0.32);
    border-radius: 0 8px 8px 0;
    background: rgba(13,17,23,0.64);
    color: #aeb8c5;
    font-size: 0.69rem;
    line-height: 1.52;
    word-break: keep-all;
  }
  div[data-testid="stMetric"] + div .mobile-help, div[data-testid="stMetric"] ~ div .mobile-help {
    margin-top: 4px;
    margin-bottom: 2px;
  }
}
.forecast-snapshot {
  box-sizing: border-box;
  width: 100%;
  margin: 0 0 10px 0;
  overflow: hidden;
}
.snapshot-price {
  min-width: 0;
}
.snapshot-label {
  color: #8e99a6;
  font-size: 0.66rem;
  font-weight: 680;
  line-height: 1.28;
}
.snapshot-age {
  margin-left: 5px;
  color: #65707c;
  font-size: 0.58rem;
  font-weight: 600;
  white-space: nowrap;
}
.snapshot-value {
  margin-top: 6px;
  color: #dfe5eb;
  font-size: clamp(1.02rem, 1.55vw, 1.28rem);
  font-weight: 750;
  line-height: 1.10;
  letter-spacing: -0.035em;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.snapshot-forecast .snapshot-value {
  color: #f5f7f9;
  font-size: clamp(1.16rem, 1.85vw, 1.48rem);
  font-weight: 800;
}
.snapshot-sub {
  margin-top: 5px;
  color: #707b87;
  font-size: 0.60rem;
  line-height: 1.28;
  font-variant-numeric: tabular-nums;
}
.snapshot-sub.up, .snapshot-return.up, .snapshot-mini strong.up {
  color: #ff747b;
}
.snapshot-sub.down, .snapshot-return.down, .snapshot-mini strong.down {
  color: #65adff;
}
.snapshot-sub.neutral, .snapshot-return.neutral, .snapshot-mini strong.neutral {
  color: #8b95a1;
}
@media (max-width: 760px) {
  .forecast-snapshot {
    padding: 10px 10px 9px 10px;
  }
  .snapshot-label {
    font-size: 0.58rem;
  }
  .snapshot-age {
    margin-left: 3px;
    font-size: 0.50rem;
  }
  .snapshot-value {
    margin-top: 4px;
    font-size: 0.92rem;
  }
  .snapshot-forecast .snapshot-value {
    font-size: 1.02rem;
  }
  .snapshot-sub {
    margin-top: 3px;
    font-size: 0.51rem;
    line-height: 1.22;
  }
}
@media (max-width: 390px) {
  .snapshot-age {
    display: none;
  }
  .snapshot-value {
    font-size: 0.86rem;
  }
  .snapshot-forecast .snapshot-value {
    font-size: 0.96rem;
  }
}
.forecast-snapshot {
  padding: 0 !important;
  border-radius: 16px !important;
  background: linear-gradient(180deg, #11161d 0%, #0d1218 100%) !important;
  border: 1px solid rgba(255,255,255,0.07) !important;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.018) !important;
}
.snapshot-route {
  display: grid;
  align-items: center;
  margin: 0 auto;
  box-sizing: border-box;
}
.snapshot-route .snapshot-price {
  min-width: 0;
}
.snapshot-route .snapshot-current {
  text-align: right;
}
.snapshot-route .snapshot-forecast {
  text-align: left;
}
.snapshot-route .snapshot-label {
  color: #8995a2;
  font-weight: 690;
  line-height: 1.25;
}
.snapshot-route .snapshot-age {
  color: #66717d;
  font-size: 0.56rem;
  font-weight: 600;
}
.snapshot-route .snapshot-value {
  margin-top: 6px;
  color: #e5eaf0;
  font-weight: 780;
  line-height: 1.08;
  letter-spacing: -0.035em;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.snapshot-route .snapshot-forecast .snapshot-value {
  color: #f7f9fb;
  font-weight: 830;
  text-shadow: 0 0 18px rgba(49,130,246,0.08);
}
.snapshot-route .snapshot-forecast .snapshot-label {
  color: #9aa7b5;
}
.snapshot-route .snapshot-sub {
  margin-top: 5px;
  color: #6f7b87;
  line-height: 1.25;
  font-variant-numeric: tabular-nums;
}
.snapshot-connector {
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.snapshot-return-pill {
  position: relative;
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,0.085);
  color: #919ca8;
  line-height: 1;
  text-align: center;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.snapshot-return-pill.up {
  color: #ff7b82;
  border-color: rgba(255,116,123,0.20);
  background: #151619;
}
.snapshot-return-pill.down {
  color: #6fb2ff;
  border-color: rgba(101,173,255,0.20);
  background: #11171e;
}
.snapshot-return-pill.neutral {
  color: #929ca8;
}
.snapshot-detail-strip {
  display: grid;
  border-top: 1px solid rgba(255,255,255,0.055);
  background: rgba(7,10,14,0.18);
}
.snapshot-detail {
  min-width: 0;
}
.snapshot-detail + .snapshot-detail {
  border-left: 1px solid rgba(255,255,255,0.05);
}
.snapshot-detail-label {
  color: #7f8b98;
  font-weight: 680;
  line-height: 1.25;
}
.snapshot-detail-value {
  margin-top: 4px;
  color: #e8edf2;
  font-size: 0.90rem;
  font-weight: 780;
  line-height: 1.18;
  letter-spacing: -0.022em;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.snapshot-detail-value.range {
  font-size: 0.94rem;
}
.snapshot-detail-value.up {
  color: #ff747b;
}
.snapshot-detail-value.down {
  color: #65adff;
}
.snapshot-detail-value.neutral {
  color: #a0a9b3;
}
.snapshot-detail-sub {
  margin-top: 3px;
  color: #626e7a;
  line-height: 1.28;
}
@media (max-width: 980px) {
  .snapshot-route {
    padding-left: 17px;
    padding-right: 17px;
  }
  .snapshot-detail-strip {
    grid-template-columns: minmax(270px, 1.35fr) minmax(135px, 0.6fr) minmax(135px, 0.6fr);
  }
}
@media (max-width: 760px) {
  .forecast-snapshot {
    margin-bottom: 7px !important;
    border-radius: 12px !important;
  }
  .snapshot-route {
    width: 100%;
  }
  .snapshot-route .snapshot-current {
    text-align: left;
  }
  .snapshot-route .snapshot-forecast {
    text-align: right;
  }
  .snapshot-route .snapshot-age {
    margin-left: 3px;
    font-size: 0.49rem;
  }
  .snapshot-route .snapshot-value {
    margin-top: 4px;
  }
  .snapshot-route .snapshot-sub {
    margin-top: 3px;
    white-space: normal;
  }
  .snapshot-detail-strip {
    grid-template-columns: 1fr 1fr;
  }
  .snapshot-range-detail {
    grid-column: 1 / -1;
    border-bottom: 1px solid rgba(255,255,255,0.05);
  }
  .snapshot-detail:nth-child(2) {
    border-left: 0;
  }
  .snapshot-detail:nth-child(3) {
    border-left: 1px solid rgba(255,255,255,0.05);
  }
  .snapshot-detail-value, .snapshot-detail-value.range {
    margin-top: 3px;
  }
  .snapshot-detail-sub {
    margin-top: 2px;
  }
}
@media (max-width: 390px) {
  .snapshot-route {
    padding-left: 9px;
    padding-right: 9px;
  }
  .snapshot-route .snapshot-age {
    display: none;
  }
  .snapshot-route .snapshot-value {
    font-size: 0.88rem;
  }
  .snapshot-route .snapshot-forecast .snapshot-value {
    font-size: 0.96rem;
  }
}
.block-container {
  max-width: 1320px !important;
}
.section-head {
  margin: 26px 0 14px 0 !important;
  padding: 0 !important;
}
.section-head::before {
  display: none !important;
}
.section-title {
  font-weight: 800 !important;
  letter-spacing: -0.028em !important;
}
.section-kicker {
  margin-bottom: 4px !important;
  color: #697582 !important;
  letter-spacing: .11em !important;
}
.section-note {
  max-width: 520px;
  color: #7f8a96 !important;
  text-align: right;
}
.subsection-head {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 18px;
  margin: 24px 0 11px 0;
  padding-top: 2px;
}
.subsection-title {
  color: #e7ebf0;
  font-weight: 780;
  letter-spacing: -0.022em;
}
.subsection-note {
  color: #737f8b;
  text-align: right;
  line-height: 1.4;
}
.stTabs [data-baseweb="tab-list"] {
  min-height: 48px !important;
  gap: 26px !important;
  padding: 0 !important;
  background: transparent !important;
  border: 0 !important;
  border-bottom: 1px solid rgba(255,255,255,0.07) !important;
  border-radius: 0 !important;
}
.stTabs button[data-baseweb="tab"] {
  min-height: 48px !important;
  padding: 0 1px !important;
  color: #8f99a5 !important;
  font-weight: 650 !important;
}
.stTabs button[data-baseweb="tab"][aria-selected="true"] {
  background: transparent !important;
  color: #f0f3f7 !important;
}
.stTabs [data-baseweb="tab-highlight"] {
  height: 2px !important;
  border-radius: 999px !important;
  background: #f0b90b !important;
}
.snapshot-route {
  width: min(100%, 900px) !important;
  gap: 24px !important;
  padding: 20px 24px 18px !important;
}
.snapshot-connector {
  height: 52px !important;
}
.snapshot-detail-strip {
  grid-template-columns: 1.42fr .79fr .79fr !important;
}
.snapshot-detail {
  padding: 13px 17px 14px !important;
}
.cycle-overview-grid, .validation-card-grid, .strategy-card-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin: 2px 0 16px 0;
}
.overview-card, .validation-card, .strategy-card {
  min-width: 0;
  border: 1px solid rgba(255,255,255,0.065);
  border-radius: 14px;
  background: linear-gradient(180deg, #11161d 0%, #0d1218 100%);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.016);
}
.overview-card {
  padding: 16px 17px 15px;
}
.overview-label, .validation-period, .strategy-name {
  color: #84909d;
  font-weight: 700;
}
.overview-value {
  margin-top: 7px;
  color: #edf1f5;
  font-weight: 820;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}
.overview-value.up, .validation-main.up, .strategy-return.up {
  color: #ff737a;
}
.overview-value.down, .validation-main.down, .strategy-return.down {
  color: #67adff;
}
.overview-sub {
  margin-top: 7px;
  color: #6f7a86;
  line-height: 1.4;
}
.validation-card {
  padding: 14px 15px 13px;
}
.validation-main {
  margin-top: 6px;
  color: #ecf0f4;
  font-weight: 820;
  font-variant-numeric: tabular-nums;
}
.validation-pairs {
  display: grid;
  grid-template-columns: repeat(3, minmax(0,1fr));
  gap: 7px;
  margin-top: 12px;
}
.validation-pairs > div, .strategy-grid > div {
  min-width: 0;
}
.validation-pairs span, .strategy-grid span {
  display: block;
  color: #687481;
  line-height: 1.25;
}
.validation-pairs b, .strategy-grid b {
  display: block;
  margin-top: 3px;
  color: #cfd6de;
  font-weight: 740;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.strategy-card {
  padding: 15px 16px 14px;
}
.strategy-return {
  margin-top: 6px;
  color: #edf1f5;
  font-weight: 830;
  letter-spacing: -0.025em;
  font-variant-numeric: tabular-nums;
}
.strategy-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0,1fr));
  gap: 8px;
  margin-top: 12px;
  padding-top: 11px;
  border-top: 1px solid rgba(255,255,255,0.05);
}
.validation-context {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin: -2px 0 13px 0;
}
.validation-context span {
  padding: 5px 8px;
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,0.06);
  background: rgba(13,18,24,0.8);
  color: #7f8b97;
  font-variant-numeric: tabular-nums;
}
.tab-callout {
  margin: 8px 0 12px;
  padding: 11px 13px;
  border: 1px solid rgba(255,255,255,0.06);
  border-radius: 11px;
  background: #0e1319;
  color: #9ba6b2;
  line-height: 1.5;
}
.tab-callout.warn {
  border-left: 3px solid #f0b90b;
}
.tab-callout.neutral {
  border-left: 3px solid #637080;
}
div[data-testid="stPlotlyChart"], div[data-testid="stExpander"] {
  border-radius: 15px !important;
  border-color: rgba(255,255,255,0.06) !important;
  background: #0d1116 !important;
  box-shadow: none !important;
}
div[data-testid="stExpander"] details summary {
  min-height: 46px !important;
}
@media (max-width: 980px) {
  .cycle-overview-grid, .validation-card-grid, .strategy-card-grid {
    grid-template-columns: repeat(2, minmax(0,1fr));
  }
  .snapshot-route {
    grid-template-columns: minmax(0,1fr) 110px minmax(0,1fr) !important;
    width: 100% !important;
    gap: 15px !important;
  }
}
@media (max-width: 760px) {
  .block-container {
    padding-left: 0.78rem !important;
    padding-right: 0.78rem !important;
  }
  .section-head {
    margin: 20px 0 11px !important;
    gap: 4px !important;
  }
  .section-note {
    text-align: left !important;
  }
  .subsection-head {
    align-items: flex-start;
    flex-direction: column;
    gap: 3px;
    margin: 20px 0 9px;
  }
  .subsection-note {
    text-align: left;
  }
  .stTabs [data-baseweb="tab-list"] {
    gap: 20px !important;
    min-height: 46px !important;
    overflow-x: auto !important;
  }
  .stTabs button[data-baseweb="tab"] {
    min-height: 46px !important;
    white-space: nowrap !important;
  }
  .snapshot-route {
    padding: 13px 12px 12px !important;
  }
  .snapshot-connector {
    height: 42px !important;
  }
  .snapshot-detail {
    padding: 9px 11px 10px !important;
  }
  .cycle-overview-grid, .validation-card-grid, .strategy-card-grid {
    grid-template-columns: 1fr;
    gap: 8px;
    margin-bottom: 12px;
  }
  .overview-card {
    padding: 13px 14px 12px;
  }
  .validation-card, .strategy-card {
    padding: 12px 13px;
  }
  .validation-pairs {
    gap: 6px;
  }
  .strategy-grid {
    grid-template-columns: repeat(2, minmax(0,1fr));
    row-gap: 8px;
  }
  .reading-guide.compact {
    margin-top: 8px !important;
    padding: 10px 11px !important;
  }
}
.section-title {
  font-size: 1.22rem !important;
}
.section-kicker {
  font-size: 0.65rem !important;
}
.section-note {
  font-size: 0.76rem !important;
  line-height: 1.45 !important;
}
.subsection-title {
  font-size: 0.98rem !important;
}
.subsection-note {
  font-size: 0.72rem !important;
}
.stTabs button[data-baseweb="tab"] {
  font-size: 0.89rem !important;
}
.verdict-title {
  font-size: 0.96rem !important;
}
.verdict-copy {
  font-size: 0.84rem !important;
}
.verdict-confidence {
  font-size: 0.69rem !important;
}
.snapshot-route .snapshot-label {
  font-size: 0.73rem !important;
}
.snapshot-route .snapshot-value {
  font-size: clamp(1.46rem, 1.95vw, 1.72rem) !important;
}
.snapshot-route .snapshot-forecast .snapshot-value {
  font-size: clamp(1.58rem, 2.12vw, 1.88rem) !important;
}
.snapshot-route .snapshot-sub {
  font-size: 0.64rem !important;
}
.snapshot-detail-label {
  font-size: 0.68rem !important;
}
.snapshot-detail-value, .snapshot-detail-value.range {
  font-size: 1.03rem !important;
}
.snapshot-detail-sub {
  font-size: 0.61rem !important;
}
.overview-label, .validation-period, .strategy-name {
  font-size: 0.70rem !important;
}
.overview-value {
  font-size: 1.28rem !important;
}
.overview-sub {
  font-size: 0.67rem !important;
}
.validation-main {
  font-size: 1.14rem !important;
}
.validation-pairs span, .strategy-grid span {
  font-size: 0.59rem !important;
}
.validation-pairs b, .strategy-grid b {
  font-size: 0.72rem !important;
}
.strategy-return {
  font-size: 1.23rem !important;
}
.validation-context span {
  font-size: 0.64rem !important;
}
.tab-callout {
  font-size: 0.74rem !important;
}
.reading-guide-label {
  font-size: 0.67rem !important;
}
.reading-guide-copy {
  font-size: 0.72rem !important;
}
@media (max-width: 760px) {
  .section-title {
    font-size: 1.12rem !important;
  }
  .section-kicker {
    font-size: 0.62rem !important;
  }
  .section-note {
    font-size: 0.68rem !important;
  }
  .subsection-title {
    font-size: 0.94rem !important;
  }
  .subsection-note {
    font-size: 0.66rem !important;
  }
  .stTabs button[data-baseweb="tab"] {
    font-size: 0.82rem !important;
  }
  div[data-testid="stRadio"] > label p, div[data-testid="stSelectbox"] > label p, div[data-testid="stSelectSlider"] > label p, div[data-testid="stCheckbox"] label p {
    font-size: 0.69rem !important;
  }
  div[role="radiogroup"] label {
    font-size: 0.75rem !important;
  }
  .verdict-title {
    font-size: 0.82rem !important;
  }
  .verdict-copy {
    font-size: 0.73rem !important;
  }
  .verdict-confidence {
    font-size: 0.61rem !important;
  }
  .snapshot-route .snapshot-label {
    font-size: 0.62rem !important;
  }
  .snapshot-route .snapshot-value {
    font-size: 1.10rem !important;
  }
  .snapshot-route .snapshot-forecast .snapshot-value {
    font-size: 1.18rem !important;
  }
  .snapshot-route .snapshot-sub {
    font-size: 0.53rem !important;
  }
  .snapshot-detail-label {
    font-size: 0.59rem !important;
  }
  .snapshot-detail-value, .snapshot-detail-value.range {
    font-size: 0.89rem !important;
  }
  .snapshot-detail-sub {
    font-size: 0.52rem !important;
  }
  .overview-label, .validation-period, .strategy-name {
    font-size: 0.67rem !important;
  }
  .overview-value {
    font-size: 1.13rem !important;
  }
  .overview-sub {
    font-size: 0.63rem !important;
  }
  .validation-main {
    font-size: 1.08rem !important;
  }
  .strategy-return {
    font-size: 1.12rem !important;
  }
  .validation-pairs span, .strategy-grid span {
    font-size: 0.58rem !important;
  }
  .validation-pairs b, .strategy-grid b {
    font-size: 0.70rem !important;
  }
  .tab-callout {
    font-size: 0.70rem !important;
  }
  .reading-guide-label {
    font-size: 0.65rem !important;
  }
  .reading-guide-copy {
    font-size: 0.69rem !important;
    line-height: 1.46 !important;
  }
  .chart-caption {
    font-size: 0.64rem !important;
  }
}
.forecast-secondary-strip {
  margin: 9px 0 8px 0 !important;
  border: 1px solid rgba(255,255,255,0.06) !important;
  border-radius: 13px !important;
  overflow: hidden !important;
  background: #0d1116 !important;
}
.forecast-secondary-strip.snapshot-detail-strip {
  border-top: 1px solid rgba(255,255,255,0.06) !important;
}
.forecast-secondary-strip .snapshot-detail {
  min-height: 66px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}
.forecast-secondary-strip .snapshot-detail-label {
  color: #88939f !important;
}
.forecast-secondary-strip .snapshot-detail-sub {
  color: #697480 !important;
}
@media (max-width: 760px) {
  .forecast-secondary-strip {
    margin-top: 7px !important;
    margin-bottom: 7px !important;
    border-radius: 12px !important;
  }
  .forecast-secondary-strip .snapshot-detail {
    min-height: 58px;
  }
  .forecast-secondary-strip .snapshot-range-detail {
    min-height: 62px;
  }
}
.forecast-snapshot-price-only .snapshot-route {
  padding-top: 18px !important;
  padding-bottom: 18px !important;
}
@media (max-width: 760px) {
  .forecast-snapshot-price-only .snapshot-route {
    padding-top: 13px !important;
    padding-bottom: 13px !important;
  }
}
.snapshot-return-pill {
  min-width: 76px !important;
  padding: 6px 12px !important;
  font-size: 0.84rem !important;
  font-weight: 850 !important;
  letter-spacing: -0.018em !important;
  border-width: 1px !important;
}
.snapshot-route {
  grid-template-columns: minmax(0,1fr) 170px minmax(0,1fr) !important;
}
@media (max-width: 760px) {
  .snapshot-return-pill {
    padding: 5px 7px !important;
    font-size: 0.70rem !important;
  }
}
@media (max-width: 390px) {
  .snapshot-return-pill {
    padding: 4px 6px !important;
    font-size: 0.66rem !important;
  }
}
.snapshot-connector {
  position: relative !important;
  overflow: visible !important;
}
.snapshot-arrow-svg {
  position: absolute;
  inset: 50% 0 auto 0;
  transform: translateY(-50%);
  overflow: visible;
  pointer-events: none;
  z-index: 0;
}
.snapshot-arrow-svg path {
  fill: none;
  vector-effect: non-scaling-stroke;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.snapshot-return-pill {
  z-index: 2 !important;
  background: #11161d !important;
}
@media (max-width: 760px) {
  .snapshot-route {
    grid-template-columns: minmax(0,1fr) 108px minmax(0,1fr) !important;
    gap: 5px !important;
  }
  .snapshot-return-pill {
    min-width: 60px !important;
  }
}
@media (max-width: 390px) {
  .snapshot-route {
    grid-template-columns: minmax(0,1fr) 98px minmax(0,1fr) !important;
    gap: 4px !important;
  }
  .snapshot-return-pill {
    min-width: 56px !important;
    box-shadow: 0 0 0 3px #11161d !important;
  }
}
.snapshot-arrow-svg {
  left: -18px !important;
  right: auto !important;
  width: calc(100% + 36px) !important;
  height: 30px !important;
}
.snapshot-arrow-svg path {
  stroke: rgba(171,183,196,0.88) !important;
  stroke-width: 2.15 !important;
}
.snapshot-return-pill {
  box-shadow: 0 0 0 3px #11161d !important;
}
@media (max-width: 760px) {
  .snapshot-arrow-svg {
    left: -24px !important;
    width: calc(100% + 48px) !important;
    height: 34px !important;
  }
  .snapshot-arrow-svg path {
    stroke: rgba(186,197,209,0.96) !important;
    stroke-width: 2.6 !important;
  }
  .snapshot-return-pill {
    box-shadow: 0 0 0 2px #11161d !important;
  }
}
@media (max-width: 390px) {
  .snapshot-arrow-svg {
    left: -22px !important;
    width: calc(100% + 44px) !important;
    height: 32px !important;
  }
  .snapshot-arrow-svg path {
    stroke-width: 2.45 !important;
  }
}
.market-overview-grid, .history-summary-grid, .spot-overview-grid, .portfolio-summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin: 10px 0 16px;
}
.market-card {
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 15px;
  background: linear-gradient(180deg, rgba(22,27,35,.90), rgba(13,17,23,.82));
}
.market-card-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 8px;
}
.market-card-head b {
  display: block;
  color: var(--text);
  font-size: .88rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.market-card-head div > span {
  display: block;
  color: var(--muted-2);
  font-size: .69rem;
  margin-top: 2px;
}
.confidence-chip {
  flex: 0 0 auto;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 3px 7px;
  color: var(--muted);
  font-size: .67rem;
  font-weight: 750;
}
.confidence-chip.high {
  color: #7ee787;
  border-color: rgba(63,185,80,.35);
}
.confidence-chip.medium {
  color: #f2cc60;
  border-color: rgba(240,185,11,.35);
}
.confidence-chip.low {
  color: #9ca8b7;
}
.market-card-price {
  color: var(--text-soft);
  font-size: .78rem;
  margin-top: 12px;
}
.market-card-signal {
  color: var(--text);
  font-size: 1.28rem;
  font-weight: 820;
  margin-top: 2px;
}
.market-card-signal.up {
  color: #ff6b64;
}
.market-card-signal.down {
  color: #58a6ff;
}
.market-card-signal.neutral {
  color: #b4bfcc;
}
.market-card-sub, .market-card-range {
  color: var(--muted);
  font-size: .70rem;
  margin-top: 3px;
}
.market-card-range {
  border-top: 1px solid var(--line);
  padding-top: 8px;
  margin-top: 10px;
}
.market-card-range b {
  color: var(--text-soft);
}
.forecast-snapshot.low-confidence .snapshot-forecast .snapshot-value {
  color: #b4bfcc !important;
}
.forecast-snapshot.low-confidence .snapshot-arrow-svg path {
  stroke: #778391 !important;
}
.forecast-snapshot.low-confidence .snapshot-return-pill {
  color: #c2cad4 !important;
  background: rgba(127,139,153,.12) !important;
  border-color: rgba(127,139,153,.28) !important;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.forecast-controls) {
  border: 0;
  background: transparent;
  padding: 0;
}
.driver-list {
  display: grid;
  gap: 8px;
}
.driver-row {
  display: grid;
  grid-template-columns: minmax(150px, 1.1fr) minmax(120px, 1fr) 78px minmax(240px, 2fr);
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: rgba(13,17,23,.48);
}
.driver-name {
  color: var(--text-soft);
  font-size: .78rem;
  font-weight: 700;
}
.driver-name span {
  display: block;
  color: var(--muted);
  font-size: .66rem;
  font-weight: 500;
}
.driver-track {
  height: 7px;
  border-radius: 999px;
  background: rgba(127,139,153,.12);
  overflow: hidden;
}
.driver-fill {
  height: 100%;
  border-radius: inherit;
  background: #58a6ff;
}
.driver-row.up .driver-fill {
  background: #f85149;
}
.driver-value {
  color: var(--text);
  text-align: right;
  font-size: .73rem;
  font-variant-numeric: tabular-nums;
}
.driver-desc {
  color: var(--muted);
  font-size: .69rem;
  line-height: 1.45;
}
.health-list {
  border: 1px solid var(--line);
  border-radius: 13px;
  overflow: hidden;
  background: rgba(13,17,23,.48);
}
.health-row {
  display: grid;
  grid-template-columns: minmax(150px, .8fr) minmax(145px, .8fr) 70px minmax(260px, 2fr);
  gap: 12px;
  align-items: center;
  padding: 11px 13px;
  border-bottom: 1px solid var(--line);
}
.health-row:last-child {
  border-bottom: 0;
}
.health-source {
  color: var(--text-soft);
  font-size: .78rem;
  font-weight: 700;
}
.health-latest, .health-detail {
  color: var(--muted);
  font-size: .72rem;
}
.health-state {
  display: inline-block;
  border-radius: 999px;
  padding: 3px 7px;
  font-size: .67rem;
  font-weight: 800;
}
.health-state.ok {
  color: #7ee787;
  background: rgba(63,185,80,.12);
}
.health-state.warn {
  color: #f2cc60;
  background: rgba(240,185,11,.12);
}
.health-state.off {
  color: #9ca8b7;
  background: rgba(127,139,153,.12);
}
.history-summary-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.spot-overview-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.portfolio-summary-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
@media (max-width: 1050px) {
  .market-overview-grid, .portfolio-summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .spot-overview-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .driver-row {
    grid-template-columns: minmax(140px, 1fr) minmax(100px, 1fr) 70px;
  }
  .driver-desc {
    grid-column: 1 / -1;
  }
}
@media (max-width: 760px) {
  div[data-testid="stVerticalBlockBorderWrapper"]:has(.forecast-controls) {
    position: sticky;
    top: 0;
    z-index: 20;
    margin: 0 -8px 8px;
    padding: 8px 10px 5px;
    border: 1px solid rgba(120,132,148,.18);
    border-radius: 12px;
    background: rgba(8,11,16,.94);
    box-shadow: 0 8px 24px rgba(0,0,0,.28);
    backdrop-filter: blur(12px);
  }
  .market-overview-grid, .history-summary-grid, .spot-overview-grid, .portfolio-summary-grid {
    grid-template-columns: 1fr;
    gap: 8px;
  }
  .market-card {
    padding: 13px;
  }
  .driver-row {
    grid-template-columns: minmax(0, 1fr) 66px;
  }
  .driver-track {
    grid-column: 1 / -1;
    grid-row: 2;
  }
  .driver-value {
    grid-column: 2;
    grid-row: 1;
  }
  .driver-desc {
    grid-column: 1 / -1;
    grid-row: 3;
  }
  .health-list {
    border: 0;
    background: transparent;
    display: grid;
    gap: 8px;
  }
  .health-row {
    grid-template-columns: 1fr auto;
    gap: 4px 10px;
    border: 1px solid var(--line);
    border-radius: 11px;
    background: rgba(13,17,23,.58);
    padding: 11px 12px;
  }
  .health-latest {
    grid-column: 1;
  }
  .health-detail {
    grid-column: 1 / -1;
    margin-top: 4px;
  }
  .stTabs [data-baseweb="tab-list"] {
    overflow-x: auto;
    scrollbar-width: none;
    flex-wrap: nowrap;
  }
  .stTabs [data-baseweb="tab-list"]::-webkit-scrollbar {
    display: none;
  }
  .stTabs button[data-baseweb="tab"] {
    flex: 0 0 auto;
    white-space: nowrap;
  }
}
</style>
"""
st.markdown(DASHBOARD_CSS, unsafe_allow_html=True)


# ======================================================================================
# 데이터 로딩
# ======================================================================================
@st.cache_data(ttl=300, show_spinner=False)
def load_manifest() -> Optional[Dict]:
    path = PUBLISHED / "manifest.json"
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
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
        try:
            with open(jpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("predictions"), list):
                return data
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
    cpath = PUBLISHED / "predictions.csv"
    if not cpath.exists():
        return None
    # 종목코드 005930 이 정수로 읽히면 앞의 0 이 사라진다 (히스토리 조회 실패)
    try:
        df = pd.read_csv(cpath, dtype={"symbol": str, "confidence_grade": str,
                                       "country": str, "currency": str})
    except (OSError, pd.errors.ParserError, UnicodeDecodeError):
        return None
    if "symbol" not in df.columns:
        return None
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
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def load_portfolio_backtest() -> Optional[Dict]:
    """publish.py 가 올린 횡단면 포트폴리오 백테스트 결과."""
    path = PUBLISHED / "portfolio_backtest.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def render_portfolio_backtest(data: Optional[Dict]) -> None:
    """횡단면 포트폴리오 성과를 기간 선택 → 핵심 성과 → 상세 순서로 보여준다."""
    if not data:
        return

    subsection_head(
        "종목 선택 전략",
        "매일 예측 순위가 높은 종목을 골랐을 때의 과거 성적입니다.",
    )

    horizons = sorted(data, key=lambda x: int(x) if str(x).isdigit() else 0)
    if not horizons:
        return

    selected_h = st.radio(
        "검증 기간",
        horizons,
        horizontal=True,
        key="portfolio_validation_horizon",
        format_func=lambda h: f"{h}일",
    )
    d = data.get(selected_h) or {}
    metrics = d.get("metrics") or {}

    strategy_cards = []
    for name, m in metrics.items():
        ann = num(m.get("annual_return"))
        tone = _change_tone(ann)
        strategy_cards.append(
            "<div class='strategy-card'>"
            f"<div class='strategy-name'>{html.escape(str(name))}</div>"
            f"<div class='strategy-return {tone}'>{html.escape(pct(ann))}</div>"
            "<div class='strategy-grid'>"
            f"<div><span>Sharpe</span><b>{html.escape(fnum(m.get('sharpe')))}</b></div>"
            f"<div><span>MDD</span><b>{html.escape(pct(m.get('max_drawdown')))}</b></div>"
            f"<div><span>적중률</span><b>{html.escape(pct(m.get('hit_rate'), signed=False))}</b></div>"
            f"<div><span>일회전</span><b>{html.escape(fnum(m.get('turnover_daily')))}</b></div>"
            "</div></div>"
        )

    if strategy_cards:
        st.markdown(
            "<div class='strategy-card-grid'>" + "".join(strategy_cards) + "</div>",
            unsafe_allow_html=True,
        )

    ic, ic_sd = num(d.get("mean_ic")), num(d.get("ic_std"))
    context_bits = []
    if ic is not None:
        context_bits.append(f"평균 횡단면 IC {ic:+.3f}")
    if ic_sd is not None:
        context_bits.append(f"표준편차 {ic_sd:.3f}")
    if d.get("n_names_avg") is not None:
        context_bits.append(f"평균 {float(d['n_names_avg']):.1f}종목")
    if d.get("n_effective") is not None:
        context_bits.append(f"실효표본 {float(d['n_effective']):.0f}")
    if d.get("cost_bps") is not None:
        context_bits.append(f"비용 {float(d['cost_bps']):.1f}bp/회전")
    if context_bits:
        st.markdown(
            "<div class='validation-context'>" + "<span>" + "</span><span>".join(
                html.escape(x) for x in context_bits
            ) + "</span></div>",
            unsafe_allow_html=True,
        )

    q = d.get("quantile_returns") or {}
    with st.expander("분위별 수익과 상세 검증 보기", expanded=False):
        rows = []
        for name, m in metrics.items():
            rows.append({
                "전략": name,
                "연수익": pct(m.get("annual_return")),
                "Sharpe": fnum(m.get("sharpe")),
                "MDD": pct(m.get("max_drawdown")),
                "적중률": pct(m.get("hit_rate"), signed=False),
                "일회전": fnum(m.get("turnover_daily")),
            })
        if rows:
            render_dark_table(pd.DataFrame(rows))

        if q:
            mono = num(d.get("monotonicity"))
            st.markdown(
                f"<div class='diag-subhead'>분위별 {html.escape(str(selected_h))}일 수익 "
                f"<span>단조성 {html.escape(fnum(mono))} · 1.0이면 완전단조</span></div>",
                unsafe_allow_html=True,
            )
            render_dark_table(pd.DataFrame([
                {
                    "분위": k,
                    "평균 수익": pct(v),
                    "표본": f"{(d.get('quantile_counts') or {}).get(k, 0):,}일",
                }
                for k, v in q.items()
            ]))
            if mono is not None and mono < 0.5:
                st.warning(
                    "분위별 수익이 단조롭지 않습니다. 예측 순위와 실제 수익 순위가 "
                    "충분히 맞지 않을 수 있습니다.",
                    icon="⚠️",
                )

        for n in (d.get("notes") or []):
            st.caption(f"· {n}")


@st.cache_data(ttl=300, show_spinner=False)
def load_panel_diagnostics() -> Optional[Dict]:
    """
    publish.py 가 올린 패널(종목 횡단) 학습 진단을 읽는다.

    패널은 여러 종목을 한 판에 쌓아 학습한 모델이라 종목별 화면 어디에도
    자연스럽게 들어갈 자리가 없는데, 실제로는 앙상블 가중치를 크게 가져간다
    (MU 0.91, SK하이닉스 0.53). 어떤 근거로 그 가중치가 나왔는지 볼 수 있어야
    한다. 파일이 없으면 None 을 돌려주고 섹션 자체를 그리지 않는다.
    """
    path = PUBLISHED / "panel_diagnostics.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def render_panel_diagnostics(data: Optional[Dict], symbol: str) -> None:
    """패널 학습 요약을 카드 우선, 상세 표 후순위로 보여준다."""
    if not data:
        return

    subsection_head(
        "공통 모델",
        "여러 반도체 종목의 공통 흐름이 개별 종목 예측을 얼마나 보완했는지 봅니다.",
    )

    rows = []
    cards = []
    included = False
    any_rejected = False
    for h in sorted(data, key=lambda x: int(x) if str(x).isdigit() else 0):
        d = data.get(h) or {}
        m = d.get("metrics") or {}
        per = (d.get("per_symbol") or {}).get(symbol) or {}
        if per:
            included = True
        if d.get("nnls_rejected"):
            any_rejected = True

        rank_ic = num(m.get("rank_ic"))
        da = num(m.get("directional_accuracy"))
        eff = num(d.get("effective_n"))
        sym_ic = num(per.get("rank_ic")) if per else None
        tone = "up" if (rank_ic is not None and rank_ic > 0.03) else (
            "down" if (rank_ic is not None and rank_ic < 0) else "neutral"
        )
        cards.append(
            "<div class='validation-card'>"
            f"<div class='validation-period'>{html.escape(str(h))}일</div>"
            f"<div class='validation-main {tone}'>IC {html.escape(fnum(rank_ic, 3))}</div>"
            "<div class='validation-pairs'>"
            f"<div><span>방향</span><b>{html.escape(pct(da, signed=False))}</b></div>"
            f"<div><span>실효표본</span><b>{html.escape(fnum(eff, 0))}</b></div>"
            f"<div><span>{html.escape(symbol)} IC</span><b>{html.escape(fnum(sym_ic, 3) if per else '미포함')}</b></div>"
            "</div></div>"
        )

        weights = d.get("weights") or {}
        w_txt = ", ".join(
            f"{k.replace('panel_', '')} {v:.2f}"
            for k, v in sorted(weights.items(), key=lambda kv: -kv[1])
        )
        rows.append({
            "기간": f"{h}일",
            "IC": fnum(rank_ic, 3),
            "방향": pct(da, signed=False),
            "OOF": f"{int(m.get('n_oof', 0)):,}행",
            "실효표본": fnum(eff, 0),
            "구성": w_txt or "-",
            f"{symbol} IC": fnum(sym_ic, 3) if per else "미포함",
        })

    if cards:
        st.markdown(
            "<div class='validation-card-grid'>" + "".join(cards) + "</div>",
            unsafe_allow_html=True,
        )

    if not included:
        st.markdown(
            "<div class='tab-callout neutral'>"
            f"<b>{html.escape(symbol)}</b>은 패널 학습 대상이 아닙니다. "
            "공통 반도체 사이클 신호가 적용되지 않는 종목입니다."
            "</div>",
            unsafe_allow_html=True,
        )
    elif any_rejected:
        st.markdown(
            "<div class='tab-callout warn'>"
            "일부 기간에서 NNLS 스태킹이 패널 모델을 기각했습니다. "
            "해당 기간은 공통 신호의 재현성이 약한 구간으로 보세요."
            "</div>",
            unsafe_allow_html=True,
        )

    with st.expander("공통 모델 상세 수치 보기", expanded=False):
        if rows:
            render_dark_table(pd.DataFrame(rows))
        st.caption(
            "실효표본은 종목 간 잔차 상관을 보정한 값입니다. 같은 날 함께 움직이는 "
            "반도체 종목을 단순히 종목 수 × 기간으로 세지 않습니다."
        )


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
    period_text = df["period"].astype(str).str.strip().str.replace(
        r"^(\d{4})(\d{2})$", r"\1-\2", regex=True
    )
    df["period"] = period_text
    df["date"] = pd.to_datetime(period_text + "-01", errors="coerce")
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
    try:
        df = pd.read_csv(path)
    except (OSError, pd.errors.ParserError, UnicodeDecodeError):
        return None
    if "date" not in df.columns:
        return None
    date_text = df["date"].astype(str).str.strip().str.replace(
        r"^(\d{4})(\d{2})$", r"\1-\2", regex=True
    )
    df["date"] = pd.to_datetime(date_text, errors="coerce")
    df = df.dropna(subset=["date"])
    return df.sort_values("date").reset_index(drop=True) if not df.empty else None


@st.cache_data(ttl=300, show_spinner=False)
def load_track() -> Dict:
    """track.py 가 만든 라이브 검증 성적. 없으면 빈 dict."""
    path = PUBLISHED / "track_summary.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@st.cache_data(ttl=300, show_spinner=False)
def load_backtest(symbol: str, horizon: int) -> Optional[pd.DataFrame]:
    path = PUBLISHED / "backtest" / f"backtest_{symbol}_h{horizon}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
    except (OSError, pd.errors.ParserError, UnicodeDecodeError):
        return None
    for col in list(df.columns):
        if col.lower() in ("date", "index"):
            df = df.rename(columns={col: "date"})
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            break
    return df


def _first_existing(*paths: Path) -> Optional[Path]:
    """후방 호환용 후보 경로 중 실제로 존재하는 첫 파일."""
    return next((path for path in paths if path.exists() and path.is_file()), None)


@st.cache_data(ttl=300, show_spinner=False)
def load_prediction_history() -> pd.DataFrame:
    """실행별 예측 이력. 파일이 없으면 기능을 조용히 비활성화한다."""
    path = _first_existing(
        PUBLISHED / "prediction_history.csv",
        PUBLISHED / "predictions_history.csv",
        PUBLISHED / "forecast_history.csv",
    )
    if path is None:
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, dtype={"symbol": str})
    except (OSError, pd.errors.ParserError, UnicodeDecodeError):
        return pd.DataFrame()

    aliases = {
        "timestamp": "generated_at",
        "created_at": "generated_at",
        "as_of": "generated_at",
        "confidence_score": "confidence",
        "up_probability": "prob_up",
    }
    df = df.rename(columns={k: v for k, v in aliases.items() if k in df.columns})
    required = {"symbol", "horizon", "generated_at"}
    if not required.issubset(df.columns):
        return pd.DataFrame()
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["horizon"] = pd.to_numeric(df["horizon"], errors="coerce")
    df["generated_at"] = pd.to_datetime(df["generated_at"], errors="coerce", utc=True)
    for col in ("current_price", "p10", "p25", "p50", "p75", "p90",
                "interval_80_low", "interval_80_high", "expected_return",
                "prob_up", "confidence"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return (
        df.dropna(subset=["horizon", "generated_at"])
        .sort_values("generated_at")
        .drop_duplicates(["symbol", "horizon", "generated_at"], keep="last")
        .reset_index(drop=True)
    )


@st.cache_data(ttl=300, show_spinner=False)
def load_data_status() -> List[Dict]:
    """publish 파이프라인이 선택적으로 내보내는 원천별 최신성 정보."""
    path = PUBLISHED / "data_status.json"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []

    if isinstance(raw, dict):
        raw = raw.get("sources", raw)
    if isinstance(raw, dict):
        rows = []
        for name, value in raw.items():
            row = dict(value) if isinstance(value, dict) else {"value": value}
            row.setdefault("source", str(name))
            rows.append(row)
        return rows
    return [dict(x) for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


@st.cache_data(ttl=300, show_spinner=False)
def load_memory_spot() -> pd.DataFrame:
    """DRAM/NAND 현물가를 long 형식(date, series, value, unit)으로 정규화한다."""
    path = _first_existing(
        PUBLISHED / "memory_spot.csv",
        PUBLISHED / "memory_spot_prices.csv",
        ROOT / "memory_spot.csv",
    )
    if path is None:
        return pd.DataFrame()
    try:
        raw = pd.read_csv(path)
    except (OSError, pd.errors.ParserError, UnicodeDecodeError):
        return pd.DataFrame()
    if raw.empty:
        return pd.DataFrame()

    date_col = next((c for c in ("date", "timestamp", "observed_at", "period")
                     if c in raw.columns), None)
    if date_col is None:
        return pd.DataFrame()
    series_col = next((c for c in ("series", "product", "name", "item")
                       if c in raw.columns), None)
    value_col = next((c for c in ("value", "price", "spot_price", "usd")
                      if c in raw.columns), None)

    if series_col and value_col:
        unit_col = next((c for c in ("unit", "price_unit", "currency")
                         if c in raw.columns), None)
        keep = [date_col, series_col, value_col] + ([unit_col] if unit_col else [])
        df = raw[keep].copy()
        df.columns = ["date", "series", "value"] + (["unit"] if unit_col else [])
        if "unit" not in df.columns:
            df["unit"] = ""
    else:
        id_cols = [date_col]
        unit_candidates = [c for c in ("unit", "currency") if c in raw.columns]
        id_cols += unit_candidates
        value_cols = [c for c in raw.columns if c not in id_cols]
        numeric_cols = []
        for col in value_cols:
            converted = pd.to_numeric(raw[col], errors="coerce")
            if converted.notna().any():
                raw[col] = converted
                numeric_cols.append(col)
        if not numeric_cols:
            return pd.DataFrame()
        df = raw.melt(id_vars=id_cols, value_vars=numeric_cols,
                      var_name="series", value_name="value")
        df = df.rename(columns={date_col: "date"})
        if unit_candidates:
            df = df.rename(columns={unit_candidates[0]: "unit"})
        else:
            df["unit"] = ""

    date_text = df["date"].astype(str).str.strip().str.replace(
        r"^(\d{4})(\d{2})$", r"\1-\2", regex=True
    )
    df["date"] = pd.to_datetime(date_text, errors="coerce")
    df["series"] = df["series"].astype(str).str.strip()
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["unit"] = df["unit"].fillna("").astype(str).str.strip()
    return (
        df.dropna(subset=["date", "value"])
        .sort_values(["series", "date"])
        .drop_duplicates(["series", "date"], keep="last")
        .reset_index(drop=True)
    )


@st.cache_data(ttl=300, show_spinner=False)
def load_portfolio() -> pd.DataFrame:
    """선택적 assets/portfolio CSV를 공통 포맷으로 읽는다."""
    path = _first_existing(
        PUBLISHED / "portfolio.csv",
        PUBLISHED / "assets.csv",
        ROOT / "assets.csv",
    )
    if path is None:
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, dtype={"symbol": str, "ticker": str,
                                      "code": str, "asset": str,
                                      "종목": str, "종목코드": str})
    except (OSError, pd.errors.ParserError, UnicodeDecodeError):
        return pd.DataFrame()

    aliases = {
        "ticker": "symbol", "code": "symbol", "asset": "symbol",
        "종목": "symbol", "종목코드": "symbol",
        "qty": "quantity", "shares": "quantity", "holding": "quantity",
        "수량": "quantity", "보유수량": "quantity",
        "avg_price": "average_price", "average_cost": "average_price",
        "cost_basis": "average_price", "평단": "average_price", "평균단가": "average_price",
        "종목명": "name", "통화": "currency", "자산유형": "asset_type",
    }
    df = df.rename(columns={k: v for k, v in aliases.items() if k in df.columns})
    if "symbol" not in df.columns:
        return pd.DataFrame()
    if "quantity" not in df.columns:
        df["quantity"] = 0.0
    if "average_price" not in df.columns:
        df["average_price"] = pd.NA
    if "name" not in df.columns:
        df["name"] = df["symbol"]
    if "currency" not in df.columns:
        df["currency"] = ""
    if "asset_type" not in df.columns:
        df["asset_type"] = "security"

    df["symbol"] = df["symbol"].fillna("").astype(str).str.strip()
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(0.0)
    df["average_price"] = pd.to_numeric(df["average_price"], errors="coerce")
    return df[df["symbol"].ne("")].reset_index(drop=True)


@st.cache_data(ttl=300, show_spinner=False)
def load_portfolio_forecast() -> Dict:
    """상관관계를 반영해 엔진이 게시한 포트폴리오 분포가 있으면 우선 사용한다."""
    path = PUBLISHED / "portfolio_forecast.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """ISO/판다스 시각을 UTC aware datetime으로 안전하게 정규화한다."""
    if value is None or str(value).strip() in {"", "None", "NaT"}:
        return None
    try:
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return None
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert("UTC").to_pydatetime()
    except (TypeError, ValueError, OverflowError):
        return None


def _market_timezone(country: str) -> ZoneInfo:
    is_kr = str(country).upper() in {"KR", "KOR", "KOREA", "SOUTH KOREA"}
    return ZoneInfo("Asia/Seoul" if is_kr else "America/New_York")


def _market_is_open(country: str, now_utc: Optional[datetime] = None) -> bool:
    """추가 의존성 없이 주말과 정규장 시간만 보수적으로 판정한다."""
    now_utc = now_utc or datetime.now(timezone.utc)
    local = now_utc.astimezone(_market_timezone(country))
    if local.weekday() >= 5:
        return False
    minute = local.hour * 60 + local.minute
    if str(country).upper() in {"KR", "KOR", "KOREA", "SOUTH KOREA"}:
        return 9 * 60 <= minute <= 15 * 60 + 30
    return 9 * 60 + 30 <= minute <= 16 * 60


def _business_days_after(timestamp: datetime, tz: ZoneInfo,
                         now_utc: Optional[datetime] = None) -> int:
    """timestamp 다음 날부터 오늘까지의 평일 수. 거래소 휴일은 원천 날짜로 보완한다."""
    now_utc = now_utc or datetime.now(timezone.utc)
    start = timestamp.astimezone(tz).date() + pd.Timedelta(days=1)
    end = now_utc.astimezone(tz).date()
    if start > end:
        return 0
    return len(pd.bdate_range(start=start, end=end))


def quote_info(quotes: Dict, symbol: str, country: str) -> Dict[str, Any]:
    """종목별 시세 값·시각·사용 가능 여부를 한곳에서 판정한다."""
    entry = ((quotes.get("quotes") or {}).get(symbol) or {}) if isinstance(quotes, dict) else {}
    value = num(entry.get("price"))
    raw_time = (
        entry.get("fetched_at") or entry.get("updated_at") or entry.get("timestamp")
        or (quotes.get("fetched_at") if isinstance(quotes, dict) else None)
    )
    ts = _parse_timestamp(raw_time)
    result: Dict[str, Any] = {
        "price": value, "timestamp": ts, "age_label": quote_age_label(raw_time),
        "usable": False, "reason": "",
    }
    if value is None or value <= 0:
        result["reason"] = "유효한 최신 시세가 없습니다."
        return result
    if ts is None:
        result["reason"] = "시세 수집 시각을 확인할 수 없어 모델 기준가를 유지합니다."
        return result

    now_utc = datetime.now(timezone.utc)
    age_minutes = (now_utc - ts).total_seconds() / 60.0
    if age_minutes < -5:
        result["reason"] = "시세 시각이 현재보다 미래여서 모델 기준가를 유지합니다."
        return result
    if _market_is_open(country, now_utc):
        fresh = age_minutes <= QUOTE_MAX_OPEN_MINUTES
        if not fresh:
            result["reason"] = f"장중 시세가 {age_minutes / 60:.1f}시간 전 자료라 모델 기준가를 유지합니다."
    else:
        bdays = _business_days_after(ts, _market_timezone(country), now_utc)
        fresh = age_minutes <= QUOTE_MAX_CLOSED_HOURS * 60 and bdays <= 1
        if not fresh:
            result["reason"] = "최근 장 마감 기준으로도 시세가 오래되어 모델 기준가를 유지합니다."
    result["usable"] = bool(fresh)
    return result


def quotes_age_label(quotes: Dict) -> str:
    """전역 시각이 없으면 종목별 시각 중 가장 최근 값을 사용한다."""
    if not isinstance(quotes, dict):
        return "스냅샷 가격"
    candidates = [quotes.get("fetched_at")]
    for entry in (quotes.get("quotes") or {}).values():
        if isinstance(entry, dict):
            candidates.append(entry.get("fetched_at") or entry.get("updated_at") or entry.get("timestamp"))
    timestamps = [ts for ts in (_parse_timestamp(value) for value in candidates) if ts is not None]
    return quote_age_label(max(timestamps)) if timestamps else "스냅샷 가격"


def interval_80_of(p: Dict) -> Tuple[Optional[float], Optional[float], bool]:
    """화면 전체가 동일한 보정 후 80% 구간을 사용하도록 단일화한다."""
    low, high = num(p.get("interval_80_low")), num(p.get("interval_80_high"))
    calibrated = low is not None and high is not None and low <= high
    if not calibrated:
        low, high = num(p.get("p10")), num(p.get("p90"))
    if low is not None and high is not None and low > high:
        low, high = high, low
    return low, high, calibrated


def canonical_symbol(symbol: Any) -> str:
    """assets.csv의 005930.KS와 예측의 005930을 같은 종목으로 맞춘다."""
    value = str(symbol or "").strip()
    return re.sub(r"\.(KS|KQ)$", "", value, flags=re.IGNORECASE)


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




def mobile_help_html(text: str, label: str = "설명 보기") -> str:
    """모바일에서 long-press 없이 탭으로 여는 보조 설명. PC에서는 CSS로 숨긴다."""
    return (
        "<details class='mobile-help'>"
        f"<summary>{html.escape(label)}</summary>"
        f"<div class='mobile-help-copy'>{html.escape(str(text))}</div>"
        "</details>"
    )

def section_head(kicker: str, title: str, note: str = "") -> None:
    """일관된 섹션 헤더. 표시 계층만 담당한다."""
    note_html = f"<div class='section-note'>{html.escape(str(note))}</div>" if note else ""
    st.markdown(
        f"""
        <div class="section-head">
          <div>
            <div class="section-kicker">{html.escape(str(kicker))}</div>
            <div class="section-title">{html.escape(str(title))}</div>
          </div>
          {note_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def subsection_head(title: str, note: str = "") -> None:
    """탭 내부의 2차 섹션 제목. 종목 전망 탭의 계층을 다른 탭에도 그대로 쓴다."""
    note_html = f"<div class='subsection-note'>{html.escape(note)}</div>" if note else ""
    st.markdown(
        f"""
        <div class="subsection-head">
          <div class="subsection-title">{html.escape(title)}</div>
          {note_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _overview_card(label: str, value: str, sub: str = "", tone: str = "neutral") -> str:
    """업황/검증 탭에서 공통으로 쓰는 큰 숫자 카드."""
    return (
        "<div class='overview-card'>"
        f"<div class='overview-label'>{html.escape(str(label))}</div>"
        f"<div class='overview-value {html.escape(tone)}'>{html.escape(str(value))}</div>"
        f"<div class='overview-sub'>{html.escape(str(sub))}</div>"
        "</div>"
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
    raw_rmse = num(p.get("raw_oos_rmse"))
    raw_da = num(p.get("raw_oos_directional_accuracy"))
    raw_ic = num(p.get("raw_oos_ic"))
    final_da = num(p.get("oos_directional_accuracy"))
    final_ic = num(p.get("oos_ic"))
    baseline_da = num(p.get("baseline_directional_accuracy"))
    majority_da = num(p.get("majority_directional_accuracy"))
    direction_ref = num(p.get("direction_reference_accuracy"))
    direction_edge = num(p.get("directional_edge"))
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

    da_value = pct(final_da, signed=False)
    if direction_ref is not None:
        da_value += f" / 기준 {pct(direction_ref, signed=False)}"
    if direction_edge is not None:
        da_value += f" · edge {direction_edge * 100:+.1f}%p"

    metrics = [
        (
            "OOS 표본", sample_value,
            "h일 forward return은 날짜가 겹치므로 raw 행 수보다 독립 정보량이 작습니다. "
            "실효 표본은 보수적으로 OOS행/h로 표시합니다.",
        ),
        (
            "Spearman IC", fnum(final_ic, 3),
            "MZ까지 반영한 최종 OOS 예측의 Spearman IC입니다.",
        ),
        (
            "방향 정확도 / 기준", da_value,
            "최종 MZ OOS 방향정확도입니다. 기준은 50%, 단순 다수방향, baseline 모델 중 "
            "가장 높은 값이며 신뢰도는 이 기준을 넘은 edge만 인정합니다.",
        ),
        (
            "RMSE / baseline", rmse_value,
            "Walk-Forward OOS RMSE와 기준모델 RMSE. 마지막 %는 baseline 대비 개선율이며 양수여야 개선입니다.",
        ),
    ]

    # MZ 가 꺼져 있으면 (raw == final) "0.1340 -> 0.1340" 같은 값이 3줄 반복된다.
    # 실제로 달라진 경우에만 비교를 보여준다.
    def _differs(a, b, tol=1e-9):
        return a is not None and b is not None and abs(a - b) > tol

    if _differs(raw_rmse, rmse):
        metrics.append((
            "MZ 효과 · RMSE", f"{raw_rmse:.4f} → {rmse:.4f}",
            "MZ 적용 전 ML/DL 앙상블과 cross-fitted MZ 적용 후 최종 모델의 OOS RMSE 비교입니다. "
            "오른쪽 값이 작아져야 MZ가 점오차를 개선한 것입니다.",
        ))
    if _differs(raw_da, final_da):
        metrics.append((
            "MZ 효과 · 방향", f"{raw_da * 100:.1f}% → {final_da * 100:.1f}%",
            "MZ 적용 전후 OOS 방향정확도 비교입니다. 최종 신뢰도에는 MZ 후 값의 기준 대비 edge를 사용합니다.",
        ))
    if _differs(raw_ic, final_ic):
        metrics.append((
            "MZ 효과 · IC", f"{raw_ic:+.3f} → {final_ic:+.3f}",
            "MZ 적용 전후 OOS Spearman IC 비교입니다.",
        ))

    metrics.append((
        "80% 구간 · 보정 전", raw_cov_value,
        "구간 폭을 다시 넓히기 전에 별도 holdout에서 측정한 honest coverage. 신뢰도 계산은 이 값을 사용합니다.",
    ))

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
        "<div class='diag-subhead'>선택된 모델 "
        "<span>NNLS 스태킹 가중치</span></div>"
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
    for name, raw in top_features.items():
        score = num(raw)
        if score is None:
            continue
        items.append((str(name), float(score)))
    items.sort(key=lambda item: abs(item[1]), reverse=True)
    items = items[:limit]

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
    ranked_top = []
    for key, raw in (top_features or {}).items():
        score = num(raw)
        if score is not None:
            ranked_top.append((str(key), abs(score)))
    ranked_top.sort(key=lambda item: item[1], reverse=True)
    top_names = [name for name, _ in ranked_top[:10]]
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
    grade = str(p.get("confidence_grade") or "LOW").upper()
    return grade if grade in {"HIGH", "MEDIUM", "LOW"} else "LOW"


def ret_of(p: Dict) -> Optional[float]:
    v = num(p.get("expected_return"))
    if v is not None:
        return v
    p50, now = num(p.get("p50")), num(p.get("current_price"))
    if p50 is not None and now:
        return p50 / now - 1.0
    return None


def reanchor(p: Dict, live_price: Optional[float], *, quote_usable: bool = True,
             quote_reason: str = "") -> Dict:
    """
    예측 분포를 최신 현재가 기준으로 다시 스케일한다.

    모델이 산출하는 것은 '현재가 대비 로그수익률의 분포' 이므로, 기준 가격이
    바뀌면 모든 분위수를 같은 비율로 옮기면 된다. 비율만 곱하는 것이지
    예측을 다시 계산하는 것이 아니다 (특징량은 여전히 마지막 확정 봉 기준).
    """
    anchor = num(p.get("current_price"))
    if live_price is None or live_price <= 0:
        return p
    out = dict(p)
    out["_live_price"] = live_price
    if not quote_usable:
        out["_reanchor_warning"] = quote_reason or "최신 시세를 신뢰할 수 없어 모델 기준가를 유지합니다."
        return out
    if anchor is None or anchor <= 0:
        out["_reanchor_warning"] = "모델 기준가가 없어 최신가 기준 재계산을 생략했습니다."
        return out

    ratio = live_price / anchor
    if not (0.5 < ratio < 2.0):          # 통화·종목 불일치 등 이상값 방어
        out["_reanchor_warning"] = "최신가와 모델 기준가 차이가 비정상적으로 커서 자동 보정을 중단했습니다."
        return out

    anchor_move = ratio - 1.0
    atr = abs(num(p.get("atr_pct")) or 0.0)
    allowed_move = min(REANCHOR_MAX_ABS_MOVE, max(0.08, atr * REANCHOR_ATR_MULTIPLE))
    if abs(anchor_move) > allowed_move:
        out["_anchor_change"] = anchor_move
        out["_reanchor_warning"] = (
            f"최신가가 모델 기준가에서 {anchor_move * 100:+.1f}% 움직여 "
            "단순 비율 보정을 중단했습니다. 새 학습 결과를 기다려 주세요."
        )
        return out

    for key in ("p10", "p25", "p50", "p75", "p90",
                "interval_80_low", "interval_80_high",
                "interval_90_low", "interval_90_high",
                "conservative_price", "optimistic_price", "target_1", "target_2",
                "stop_loss_reference", "add_buy_reference"):
        v = num(p.get(key))
        if v is not None:
            out[key] = v * ratio
    out["current_price"] = live_price
    out["_anchor_price"] = anchor
    out["_anchor_change"] = anchor_move
    out["_reanchored"] = True
    return out


def display_prediction(p: Dict, quotes: Optional[Dict]) -> Dict:
    """표시 직전 최신가 판정과 안전한 재앵커링을 항상 같은 규칙으로 적용한다."""
    base = dict(p)
    quotes = quotes or {}
    qinfo = quote_info(
        quotes,
        str(base.get("symbol") or ""),
        str(base.get("country") or "KR"),
    )
    out = reanchor(
        base,
        qinfo.get("price"),
        quote_usable=bool(qinfo.get("usable")),
        quote_reason=str(qinfo.get("reason") or ""),
    )
    out["_quote_age_label"] = str(qinfo.get("age_label") or "")
    out["_quote_usable"] = bool(qinfo.get("usable"))
    return out


def _query_value(name: str) -> Optional[str]:
    """신·구 Streamlit 모두에서 URL 쿼리 값을 읽는다."""
    try:
        value = st.query_params.get(name)
    except Exception:
        try:
            value = st.experimental_get_query_params().get(name)
        except Exception:
            return None
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if value is not None else None


def _set_query_value(name: str, value: Any) -> None:
    """선택 상태를 공유 가능한 URL에 남기되 구버전에서는 조용히 생략한다."""
    try:
        if str(st.query_params.get(name, "")) != str(value):
            st.query_params[name] = str(value)
    except Exception:
        pass


def _exchange_today(country: str) -> Optional[pd.Timestamp]:
    """해당 시장 현지 기준 '오늘' 날짜."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return None
    return pd.Timestamp(datetime.now(_market_timezone(country)).date())


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


def quote_age_label(fetched_at: Any) -> str:
    ts = _parse_timestamp(fetched_at)
    if ts is None:
        return ""
    mins = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    if mins < -5:
        return "시각 오류"
    if mins < 1:
        return "방금"
    if mins < 60:
        return f"{mins:.0f}분 전"
    return f"{mins / 60:.1f}시간 전"


def snapshot_label(manifest: Dict) -> Tuple[str, bool]:
    gen = manifest.get("generated_at")
    if not gen:
        return "시각 정보 없음", False
    ts = _parse_timestamp(gen)
    if ts is None:
        return str(gen), False
    age = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 3600.0
    if age < -0.1:
        return f"{ts.astimezone(KST):%Y-%m-%d %H:%M} KST · 시각 확인 필요", True
    ts_kst = ts.astimezone(KST)
    # 금요일 결과를 주말에 낡았다고 표시하지 않는다. 평일 새 게시가 끝날
    # 시간(18시 KST)이 지난 뒤에도 갱신되지 않았을 때만 경고한다.
    bdays = _business_days_after(ts, KST)
    now_kst = datetime.now(KST)
    stale = age > STALE_HOURS and (bdays > 1 or (bdays == 1 and now_kst.hour >= 18))
    return f"{ts_kst:%Y-%m-%d %H:%M} KST · {max(0.0, age):.0f}시간 전", stale


def verdict(p: Dict) -> str:
    """첫 화면에 표시할 짧고 행동 가능한 해석 문장."""
    g = grade_of(p)
    shrink = num(p.get("shrinkage"))
    cov = num(p.get("raw_coverage_80"))
    if cov is None:
        cov = num(p.get("coverage_80"))

    if shrink is not None and abs(shrink) < 0.05:
        main = (
            "재보정 과정에서 방향 신호가 거의 제거됐습니다. "
            "한 가격보다 예상 범위를 중심으로 보세요."
        )
    elif shrink is not None and shrink < -0.05:
        main = (
            "과거 검증에서 반대 방향 관계가 확인되어 신호가 뒤집혀 보정됐습니다. "
            "검증 성적을 함께 확인하세요."
        )
    elif g == "LOW":
        main = (
            "아직 방향 판단에 쓰기 어렵습니다. 기준값(P50)보다 예상 범위의 폭을 "
            "위험 참고용으로 보세요."
        )
    elif g == "MEDIUM":
        main = "참고 가능한 신호지만, 다른 지표와 함께 확인하는 편이 안전합니다."
    else:
        main = "과거 검증상 상대적으로 안정적인 구간이지만 확정적인 목표가는 아닙니다."

    if cov is not None:
        if cov < 0.68:
            main += f" 과거 80% 구간의 실제 적중은 {cov * 100:.0f}%로 낮았습니다."
        elif cov > 0.92:
            main += f" 과거 실제 적중은 {cov * 100:.0f}%로, 범위가 다소 보수적입니다."
    return main


def grade_ko(grade: str) -> str:
    """영문 신뢰도 등급을 첫 화면용 한국어로 바꾼다."""
    return {"HIGH": "높음", "MEDIUM": "보통", "LOW": "낮음"}.get(grade, "낮음")


def _change_tone(value: Optional[float]) -> str:
    if value is None or abs(value) < 0.0005:
        return "neutral"
    return "up" if value > 0 else "down"


def _metric_card(label: str, value: str, sub: str = "",
                 tone: str = "neutral") -> str:
    """Streamlit columns에 의존하지 않는 반응형 핵심 수치 카드."""
    return (
        "<div class='forecast-metric'>"
        f"<div class='forecast-metric-label'>{html.escape(label)}</div>"
        f"<div class='forecast-metric-value'>{html.escape(value)}</div>"
        f"<div class='forecast-metric-sub {tone}'>{html.escape(sub)}</div>"
        "</div>"
    )


def _decision_card(label: str, value: str, sub: str = "") -> str:
    """목표·손절·지지선처럼 판단에 쓰는 값을 한눈에 묶는다."""
    return (
        "<div class='decision-card'>"
        f"<div class='decision-label'>{html.escape(label)}</div>"
        f"<div class='decision-value'>{html.escape(value)}</div>"
        f"<div class='decision-sub'>{html.escape(sub)}</div>"
        "</div>"
    )


def render_forecast_summary(p: Dict, hist: Optional[pd.DataFrame],
                            quotes: Dict, horizon: int) -> None:
    """차트 직전에는 현재가 → P50 기준값의 핵심 가격 흐름만 보여준다.

    예상 범위·상승 가능성·최근 변동성은 차트 아래 보조 스트립으로 분리해
    첫 화면의 시선을 가격과 예측 중앙값에 집중시킨다.
    """
    currency = str(p.get("currency") or "KRW")
    now = num(p.get("current_price"))
    expected = ret_of(p)
    low_confidence = grade_of(p) == "LOW"

    prev_close, prev_label = prev_close_ref(hist, str(p.get("country") or "KR"))
    day_change = (now / prev_close - 1.0) if (prev_close and now) else None
    if p.get("_reanchored"):
        current_label = "현재가"
        current_age = str(p.get("_quote_age_label") or quote_age_label(quotes.get("fetched_at")) or "최신 시세")
        current_sub = (
            f"{prev_label} 대비 {pct(day_change)}" if day_change is not None
            else "최신 가격 기준"
        )
    else:
        current_label = "예측 기준가"
        current_age = "모델 계산 시점"
        current_sub = (
            f"{prev_label} 대비 {pct(day_change)}" if day_change is not None
            else "모델 계산 시점"
        )

    expected_tone = "neutral" if low_confidence else _change_tone(expected)
    expected_display = (
        f"참고 {pct(expected)}" if low_confidence and expected is not None else pct(expected)
    )
    day_tone = _change_tone(day_change)

    st.markdown(
        f"<div class='forecast-snapshot forecast-snapshot-price-only {'low-confidence' if low_confidence else ''}'>"

        "<div class='snapshot-route'>"
        "<div class='snapshot-price snapshot-current'>"
        f"<div class='snapshot-label'>{html.escape(current_label)}"
        f"<span class='snapshot-age'>{html.escape(current_age)}</span></div>"
        f"<div class='snapshot-value'>{html.escape(price(now, currency))}</div>"
        f"<div class='snapshot-sub {day_tone}'>{html.escape(current_sub)}</div>"
        "</div>"

        "<div class='snapshot-connector'>"
        "<svg class='snapshot-arrow-svg' viewBox='0 0 160 32' preserveAspectRatio='none' aria-hidden='true'>"
        "<path d='M4 16 H150 M136 5 L150 16 L136 27'></path>"
        "</svg>"
        f"<div class='snapshot-return-pill {expected_tone}'>{html.escape(expected_display)}</div>"
        "</div>"

        "<div class='snapshot-price snapshot-forecast'>"
        "<div class='snapshot-label'>P50 기준값</div>"
        f"<div class='snapshot-value'>{html.escape(price(p.get('p50'), currency))}</div>"
        "<div class='snapshot-sub'>예측 분포의 중앙값</div>"
        "</div>"
        "</div>"

        "</div>",
        unsafe_allow_html=True,
    )

def render_forecast_secondary_metrics(p: Dict) -> None:
    """차트 아래에서 예측 범위·상승 가능성·변동성을 얇은 3칸 스트립으로 보여준다."""
    currency = str(p.get("currency") or "KRW")
    now = num(p.get("current_price"))
    prob_up = num(p.get("prob_up"))
    volatility = num(p.get("expected_volatility_annual"))

    low, high, calibrated_interval = interval_80_of(p)

    if low is not None and high is not None:
        interval_value = f"{price(low, currency)} ~ {price(high, currency)}"
        interval_sub = (
            f"현재가 대비 {pct(low / now - 1)} ~ {pct(high / now - 1)}"
            if now else "폭이 넓을수록 불확실성이 큼"
        )
    else:
        interval_value = "N/A"
        interval_sub = "예상 범위 정보 없음"

    range_label = "80% 예상 범위" if calibrated_interval else "P10~P90 예상 범위"
    prob_tone = _change_tone((prob_up - 0.5) if prob_up is not None else None)

    st.markdown(
        "<div class='forecast-secondary-strip snapshot-detail-strip'>"
        "<div class='snapshot-detail snapshot-range-detail'>"
        f"<div class='snapshot-detail-label'>{html.escape(range_label)}</div>"
        f"<div class='snapshot-detail-value range'>{html.escape(interval_value)}</div>"
        f"<div class='snapshot-detail-sub'>{html.escape(interval_sub)}</div>"
        "</div>"

        "<div class='snapshot-detail'>"
        "<div class='snapshot-detail-label'>상승 가능성</div>"
        f"<div class='snapshot-detail-value {prob_tone}'>{html.escape(pct(prob_up, signed=False))}</div>"
        "<div class='snapshot-detail-sub'>50% 부근은 방향 우위 약함</div>"
        "</div>"

        "<div class='snapshot-detail'>"
        "<div class='snapshot-detail-label'>최근 변동성</div>"
        f"<div class='snapshot-detail-value'>{html.escape(pct(volatility, signed=False))}</div>"
        "<div class='snapshot-detail-sub'>연율 환산 · 현재 상태</div>"
        "</div>"
        "</div>",
        unsafe_allow_html=True,
    )


def render_forecast_reading_guide(p: Dict, horizon: int) -> None:
    """차트를 먼저 본 뒤 숫자의 해석 원칙을 짧게 안내한다."""
    currency = str(p.get("currency") or "KRW")
    low, high, _ = interval_80_of(p)

    grade = grade_of(p)
    if grade == "LOW":
        first = "현재는 <b>방향보다 범위</b>를 우선해서 보세요."
    elif grade == "MEDIUM":
        first = "방향 신호는 참고하되 <b>예상 범위와 함께</b> 보세요."
    else:
        first = "검증상 비교적 안정적이지만 <b>범위 밖 움직임도 가능</b>합니다."

    if low is not None and high is not None:
        range_copy = (
            f" {horizon}거래일 뒤 예상 범위는 "
            f"<b>{html.escape(price(low, currency))}~{html.escape(price(high, currency))}</b>입니다."
        )
    else:
        range_copy = ""

    st.markdown(
        "<div class='reading-guide compact'>"
        "<div class='reading-guide-label'>읽는 법</div>"
        f"<div class='reading-guide-copy'>{first}{range_copy} "
        "P50은 목표가가 아니라 분포의 가운데 기준점입니다.</div>"
        "</div>",
        unsafe_allow_html=True,
    )

def render_forecast_help(p: Dict) -> None:
    """초보자도 차트와 핵심 용어를 바로 이해할 수 있게 설명한다."""
    raw_cov = num(p.get("raw_coverage_80"))
    if raw_cov is None:
        raw_cov = num(p.get("coverage_80"))
    coverage_desc = (
        f"과거 별도 검증에서 실제 포함률은 {raw_cov * 100:.0f}%였습니다."
        if raw_cov is not None else
        "과거 검증 포함률이 게시 데이터에 없어서 목표 수준만 표시합니다."
    )
    items = [
        (
            "P50 기준값",
            "예상 분포의 한가운데입니다. 최근 상승·하락 추세를 그대로 늘여 만든 목표가가 아니므로 "
            "P50 하나만 보고 방향을 판단하지 않습니다.",
        ),
        (
            "80% 예상 범위",
            "모델이 대부분의 경우를 담도록 제시한 가격 구간입니다. 범위가 넓을수록 불확실성이 큽니다. "
            + coverage_desc,
        ),
        (
            "상승 가능성",
            "현재가보다 높아질 가능성입니다. 50% 부근은 뚜렷한 방향 우위가 없다는 뜻이며, "
            "신뢰도가 낮을 때는 이 숫자도 강하게 해석하지 않습니다.",
        ),
        (
            "차트 만기 범위",
            "선택한 거래일 뒤의 굵은 세로선은 가운데 50%(P25~P75), 얇은 세로선은 보정된 80% 범위입니다. "
            "현재가와 P50을 잇는 점선은 위치 비교용이며 중간 날짜의 예측 경로가 아닙니다. 휴일은 게시된 거래일 데이터 기준으로 확인하세요.",
        ),
        (
            "현재가 반영",
            "최신 시세 파일이 있으면 모든 예상 가격을 같은 비율로 옮겨 현재가에 맞춥니다. "
            "모델의 입력 특징은 마지막 확정 거래일 기준이라 장중 재학습을 뜻하지는 않습니다.",
        ),
    ]
    rows = "".join(
        "<div class='help-item'>"
        f"<div class='help-term'>{html.escape(term)}</div>"
        f"<div class='help-desc'>{html.escape(desc)}</div>"
        "</div>"
        for term, desc in items
    )
    st.markdown("<div class='help-list'>" + rows + "</div>", unsafe_allow_html=True)


def _prediction_at(df: pd.DataFrame, symbol: str, horizon: int) -> Optional[Dict]:
    if df is None or df.empty or not {"symbol", "horizon"} <= set(df.columns):
        return None
    hs = pd.to_numeric(df["horizon"], errors="coerce")
    symbols = df["symbol"].astype(str).map(canonical_symbol)
    row = df[(symbols == canonical_symbol(symbol)) & (hs == int(horizon))]
    return row.iloc[0].to_dict() if not row.empty else None


def render_market_overview(df: pd.DataFrame, quotes: Dict) -> Optional[int]:
    """같은 기간의 모든 종목을 비교하되 LOW 신호는 순위처럼 과장하지 않는다."""
    if df is None or df.empty or "horizon" not in df.columns:
        return None
    horizons = sorted({int(x) for x in pd.to_numeric(df["horizon"], errors="coerce").dropna()})
    if not horizons:
        return None

    query_h = num(_query_value("h"))
    preferred = int(query_h) if query_h is not None and int(query_h) in horizons else (5 if 5 in horizons else horizons[0])
    if ("overview_horizon" not in st.session_state
            or st.session_state.get("overview_horizon") not in horizons):
        st.session_state["overview_horizon"] = preferred

    section_head(
        "MARKET MAP",
        "전체 종목 한눈에 보기",
        "같은 기간끼리 비교하며 LOW 등급은 예상수익률을 강조하지 않습니다.",
    )
    horizon = st.radio(
        "전체 비교 기간",
        horizons,
        horizontal=True,
        key="overview_horizon",
        format_func=lambda h: f"{h}일",
    )

    history = load_prediction_history()
    cards = []
    for symbol in sorted(df["symbol"].astype(str).unique()):
        raw = _prediction_at(df, symbol, horizon)
        if not raw:
            continue
        p = display_prediction(raw, quotes)
        grade = grade_of(p)
        expected = ret_of(p)
        now = num(p.get("current_price"))
        visible_now = num(p.get("_live_price")) if p.get("_quote_usable") else now
        visible_now = visible_now if visible_now is not None else now
        low, high, _ = interval_80_of(p)
        width = ((high - low) / now) if (low is not None and high is not None and now) else None
        name = str(p.get("name") or symbol)
        revision = ""
        if not history.empty:
            history_symbols = history["symbol"].astype(str).map(canonical_symbol)
            hh = history[(history_symbols == canonical_symbol(symbol)) &
                         (pd.to_numeric(history["horizon"], errors="coerce") == int(horizon))].copy()
            if "expected_return" not in hh.columns:
                hh["expected_return"] = pd.NA
            if {"p50", "current_price"} <= set(hh.columns):
                derived = hh["p50"] / hh["current_price"] - 1.0
                hh["expected_return"] = pd.to_numeric(hh["expected_return"], errors="coerce").fillna(derived)
            hh = hh.dropna(subset=["expected_return"]).sort_values("generated_at")
            if len(hh) >= 2:
                change = float(hh["expected_return"].iloc[-1] - hh["expected_return"].iloc[-2])
                revision = f" · 예측변화 {change * 100:+.1f}%p"
        if p.get("_reanchor_warning") and p.get("_quote_usable"):
            signal = "재학습 대기"
            signal_sub = "최신가 이탈이 커 예측 자동 보정 중단" + revision
            tone = "neutral"
        elif grade == "LOW":
            signal = "판단 보류"
            signal_sub = (f"P50 참고 {pct(expected)}" if expected is not None else "방향 근거 부족") + revision
            tone = "neutral"
        else:
            signal = pct(expected)
            signal_sub = f"상승 가능성 {pct(p.get('prob_up'), signed=False)}{revision}"
            tone = _change_tone(expected)
        cards.append(
            "<div class='market-card'>"
            "<div class='market-card-head'>"
            f"<div><b>{html.escape(name)}</b><span>{html.escape(symbol)}</span></div>"
            f"<span class='confidence-chip {grade.lower()}'>{html.escape(grade_ko(grade))} "
            f"{html.escape(fnum(p.get('confidence'), 0))}</span>"
            "</div>"
            f"<div class='market-card-price'>{html.escape(price(visible_now, str(p.get('currency') or 'KRW')))}</div>"
            f"<div class='market-card-signal {tone}'>{html.escape(signal)}</div>"
            f"<div class='market-card-sub'>{html.escape(signal_sub)}</div>"
            f"<div class='market-card-range'>80% 범위 폭 <b>{html.escape(pct(width, signed=False))}</b></div>"
            "</div>"
        )
    if cards:
        st.markdown("<div class='market-overview-grid'>" + "".join(cards) + "</div>",
                    unsafe_allow_html=True)
    return int(horizon)


def render_prediction_history(history: pd.DataFrame, symbol: str, horizon: int) -> None:
    """게시 실행별 P50 방향·신뢰도 변화를 보여준다."""
    if history is None or history.empty:
        return
    symbols = history["symbol"].astype(str).map(canonical_symbol)
    h = history[(symbols == canonical_symbol(symbol)) &
                (pd.to_numeric(history["horizon"], errors="coerce") == int(horizon))].copy()
    if h.empty:
        return
    if "expected_return" not in h.columns:
        h["expected_return"] = pd.NA
    if {"p50", "current_price"} <= set(h.columns):
        derived = h["p50"] / h["current_price"] - 1.0
        h["expected_return"] = pd.to_numeric(h["expected_return"], errors="coerce").fillna(derived)
    h = h.sort_values("generated_at").tail(60)
    if h.empty:
        return

    latest = h.iloc[-1]
    previous = h.iloc[-2] if len(h) >= 2 else None
    ret_now = num(latest.get("expected_return"))
    conf_now = num(latest.get("confidence"))
    prob_now = num(latest.get("prob_up"))

    def delta(col: str, percent_point: bool = False) -> str:
        if previous is None:
            return "이전 기록 없음"
        cur, prev = num(latest.get(col)), num(previous.get(col))
        if cur is None or prev is None:
            return "비교값 없음"
        diff = cur - prev
        return f"직전 대비 {diff * 100:+.1f}%p" if percent_point else f"직전 대비 {diff:+.1f}"

    with st.expander("예측 변화 추적 · 이전 실행과 비교", expanded=False):
        cards = [
            _overview_card("P50 예상수익", pct(ret_now), delta("expected_return", True), _change_tone(ret_now)),
            _overview_card("상승 가능성", pct(prob_now, signed=False), delta("prob_up", True),
                           _change_tone((prob_now - 0.5) if prob_now is not None else None)),
            _overview_card("신뢰도", f"{fnum(conf_now, 0)}/100", delta("confidence")),
        ]
        st.markdown("<div class='history-summary-grid'>" + "".join(cards) + "</div>",
                    unsafe_allow_html=True)

        if len(h) >= 2 and h["expected_return"].notna().any():
            fig = make_subplots(specs=[[{"secondary_y": True}]])
            fig.add_trace(go.Scatter(
                x=h["generated_at"], y=h["expected_return"], mode="lines+markers",
                name="P50 예상수익", line=dict(color=FCOL, width=2), marker=dict(size=4),
                hovertemplate="%{x|%m/%d %H:%M}<br>P50 수익 %{y:.2%}<extra></extra>",
            ), secondary_y=False)
            if "confidence" in h.columns and h["confidence"].notna().any():
                fig.add_trace(go.Scatter(
                    x=h["generated_at"], y=h["confidence"], mode="lines",
                    name="신뢰도", line=dict(color="#a78bfa", width=1.4, dash="dot"),
                    hovertemplate="%{x|%m/%d %H:%M}<br>신뢰도 %{y:.0f}<extra></extra>",
                ), secondary_y=True)
            fig.update_layout(
                template="plotly_dark", height=280, margin=dict(l=8, r=8, t=35, b=8),
                paper_bgcolor=BG, plot_bgcolor=BG, hovermode="x unified",
                legend=dict(orientation="h", y=1.15, x=0), font=dict(color=TEXT, size=11),
            )
            fig.update_yaxes(tickformat=".1%", gridcolor=GRID, secondary_y=False)
            fig.update_yaxes(range=[0, 100], showgrid=False, secondary_y=True)
            fig.update_xaxes(showgrid=False)
            st.plotly_chart(fig, use_container_width=True,
                            key=f"forecast_history_{symbol}_{horizon}",
                            config={"displayModeBar": False, "responsive": True})
        st.caption(
            "prediction_history.csv가 누적될수록 모델 판단이 강화·약화되는 흐름을 확인할 수 있습니다. "
            f"마지막 기록 {pd.Timestamp(latest['generated_at']).tz_convert(KST):%Y-%m-%d %H:%M} KST"
        )


def _extract_contributions(p: Dict, diag: Dict) -> List[Tuple[str, float]]:
    raw = (
        diag.get("latest_contributions") or diag.get("feature_contributions")
        or diag.get("shap_values") or p.get("latest_contributions")
    )
    items: List[Tuple[str, float]] = []
    if isinstance(raw, dict):
        iterable = raw.items()
    elif isinstance(raw, list):
        iterable = []
        for row in raw:
            if isinstance(row, dict):
                name = row.get("feature") or row.get("name")
                value = row.get("contribution") if "contribution" in row else row.get("value")
                iterable.append((name, value))
    else:
        iterable = []
    for name, value in iterable:
        score = num(value)
        if name is not None and score is not None and score != 0:
            items.append((str(name), float(score)))
    items.sort(key=lambda item: abs(item[1]), reverse=True)
    return items[:10]


def render_prediction_drivers(p: Dict, diag: Dict) -> None:
    """중요도와 달리 최신 관측치가 예측을 올렸는지 내렸는지 표시한다."""
    items = _extract_contributions(p, diag)
    if not items:
        return
    peak = max(abs(value) for _, value in items) or 1.0
    rows = []
    for name, value in items:
        tone = "up" if value > 0 else "down"
        direction = "상승 쪽" if value > 0 else "하락 쪽"
        rows.append(
            f"<div class='driver-row {tone}'>"
            f"<div class='driver-name'>{html.escape(name)}<span>{html.escape(direction)}</span></div>"
            "<div class='driver-track'>"
            f"<div class='driver-fill' style='width:{abs(value) / peak * 100:.1f}%'></div></div>"
            f"<div class='driver-value'>{value:+.5f}</div>"
            f"<div class='driver-desc'>{html.escape(feature_meaning(name))}</div>"
            "</div>"
        )
    with st.expander("이번 예측을 움직인 요인 · 상승/하락 기여", expanded=False):
        st.markdown("<div class='driver-list'>" + "".join(rows) + "</div>",
                    unsafe_allow_html=True)
        st.caption(
            "최신 입력값의 모델 기여도이며 인과관계를 뜻하지 않습니다. 막대는 이 예측 안에서의 상대 크기입니다."
        )


# ======================================================================================
# 관세청 메모리 수출단가
# ======================================================================================
def _spot_change(g: pd.DataFrame, months: int) -> Optional[float]:
    clean = g.dropna(subset=["date", "value"]).sort_values("date")
    if clean.empty:
        return None
    latest_date = pd.Timestamp(clean["date"].iloc[-1])
    target = latest_date - pd.DateOffset(months=int(months))
    prior = clean[clean["date"] <= target]
    if prior.empty:
        return None
    prev, cur = float(prior["value"].iloc[-1]), float(clean["value"].iloc[-1])
    return cur / prev - 1.0 if prev > 0 else None


def memory_spot_chart(df: pd.DataFrame, years: int,
                      normalized: bool = True) -> go.Figure:
    fig = go.Figure()
    if df is None or df.empty:
        return fig
    cutoff = df["date"].max() - pd.DateOffset(years=int(years))
    shown = df[df["date"] >= cutoff].copy()
    palette = ["#58a6ff", "#3fb950", "#a78bfa", "#f0b90b", "#f97316", "#ec4899"]
    suffix = "가격"
    for idx, (series, group) in enumerate(shown.groupby("series", sort=True)):
        g = group.sort_values("date").copy()
        if g.empty:
            continue
        values = g["value"].astype(float)
        if normalized:
            base = next((float(v) for v in values if pd.notna(v) and float(v) > 0), None)
            if base is None:
                continue
            y = values / base * 100.0
            suffix = "지수"
        else:
            y = values
            unit = str(g["unit"].dropna().iloc[-1]) if g["unit"].notna().any() else ""
            suffix = unit or "가격"
        fig.add_trace(go.Scatter(
            x=g["date"], y=y, mode="lines", name=str(series),
            line=dict(color=palette[idx % len(palette)], width=2),
            customdata=g[["value"]],
            hovertemplate=(
                "%{x|%Y-%m-%d}<br>" + html.escape(str(series)) +
                "<br><b>%{customdata[0]:,.4g}</b><extra></extra>"
            ),
        ))
    fig.update_layout(
        template="plotly_dark", height=360, margin=dict(l=12, r=28, t=32, b=10),
        paper_bgcolor=BG, plot_bgcolor=BG, font=dict(color=TEXT, size=11),
        hovermode="x unified", yaxis_title="시작일=100" if normalized else suffix,
        legend=dict(orientation="h", y=1.16, x=0,
                    bgcolor="rgba(13,17,23,0.78)", borderwidth=0),
        hoverlabel=dict(bgcolor="#161b22", bordercolor="#30363d"),
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, linecolor=GRID, side="right")
    return fig


def render_memory_spot(df: pd.DataFrame) -> None:
    subsection_head(
        "DRAM·NAND 현물가",
        "실제 현물가격의 방향과 제품별 상대 강도를 확인합니다.",
    )
    if df is None or df.empty:
        st.info(
            "게시된 메모리 현물가가 아직 없습니다. published/memory_spot.csv가 생기면 "
            "제품별 최근 가격과 1·3개월 변화가 자동으로 표시됩니다."
        )
        return

    cards = []
    for series, group in list(df.groupby("series", sort=True))[:6]:
        g = group.sort_values("date")
        latest = float(g["value"].iloc[-1])
        latest_date = pd.Timestamp(g["date"].iloc[-1])
        unit = str(g["unit"].iloc[-1] or "")
        m1, m3 = _spot_change(g, 1), _spot_change(g, 3)
        sub = f"{latest_date:%Y-%m-%d} · 1개월 {pct(m1)} · 3개월 {pct(m3)}"
        cards.append(_overview_card(str(series), f"{latest:,.4g} {unit}".strip(), sub,
                                    _change_tone(m1)))
    st.markdown("<div class='spot-overview-grid'>" + "".join(cards) + "</div>",
                unsafe_allow_html=True)

    c1, c2 = st.columns([3.0, 1.3])
    with c1:
        years = st.radio("현물가 차트 기간", [1, 3, 5, 10], index=1,
                         horizontal=True, key="spot_years",
                         format_func=lambda value: f"{value}년")
    with c2:
        normalized = st.checkbox("제품별 시작값=100", value=True, key="spot_normalized")
    st.plotly_chart(memory_spot_chart(df, years, normalized), use_container_width=True,
                    key="memory_spot_chart",
                    config={"displayModeBar": False, "responsive": True})
    st.caption(
        "제품별 단위가 다를 수 있어 기본 차트는 선택 기간의 첫 값을 100으로 맞춥니다. "
        "체크를 끄면 원자료 가격을 표시합니다."
    )


def _kcs_change(g: pd.DataFrame, periods: int) -> Optional[float]:
    """마지막 기준월과 정확히 periods개월 전 기준월의 변화율."""
    if g is None or g.empty or not {"date", "export_unit_price_weight"} <= set(g.columns):
        return None
    clean = g.dropna(subset=["date", "export_unit_price_weight"]).copy()
    if clean.empty:
        return None
    clean["month"] = pd.to_datetime(clean["date"]).dt.to_period("M")
    clean = clean.sort_values("month").drop_duplicates("month", keep="last")
    latest = clean.iloc[-1]
    target = latest["month"] - int(periods)
    prior = clean[clean["month"] == target]
    if prior.empty:
        return None
    prev, cur = float(prior.iloc[-1]["export_unit_price_weight"]), float(latest["export_unit_price_weight"])
    if prev <= 0:
        return None
    return cur / prev - 1.0


def kcs_memory_chart(df: pd.DataFrame, years: int = 5,
                     include_logic: bool = False,
                     normalized: bool = True) -> go.Figure:
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
        "DRAM": "#3182f6",
        "NAND Flash": "#00c2a8",
        "MCP / HBM proxy": "#8b5cf6",
        "Logic comparator": "#8b949e",
    }
    for code, label in wanted.items():
        g = shown[shown["hs_code"] == code].sort_values("date")
        if g.empty:
            continue
        raw_values = g["export_unit_price_weight"].astype(float)
        base = float(raw_values.iloc[0]) if len(raw_values) and raw_values.iloc[0] > 0 else None
        y_values = raw_values / base * 100.0 if normalized and base else raw_values
        fig.add_trace(go.Scatter(
            x=g["date"], y=y_values,
            mode="lines+markers", name=label,
            line=dict(color=colors.get(label), width=2,
                      dash="dot" if code == KCS_LOGIC_CODE else "solid"),
            marker=dict(size=4),
            customdata=pd.DataFrame({"period": g["period"], "raw": raw_values}),
            hovertemplate=(
                "%{customdata[0]}<br>" + label +
                "<br><b>%{customdata[1]:,.0f} USD/kg</b>" +
                ("<br>지수 %{y:.1f}" if normalized else "") +
                "<extra></extra>"
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
        yaxis_title="선택 기간 시작값=100" if normalized else "수출단가 (USD/kg)",
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
    """메모리 업황 탭을 종목 전망 탭과 같은 카드 → 차트 → 해석 순서로 렌더링한다."""
    if df is None or df.empty:
        return

    focus = df[df["hs_code"].isin(KCS_MEMORY_SERIES)].copy()
    if focus.empty:
        return

    latest_period = str(focus["period"].max())
    subsection_head(
        "관세청 수출단가",
        f"관세청 수출단가 · 최종 게시월 {latest_period} · 계열별 기준월은 카드에 별도 표시",
    )

    cards = []
    for code, label in KCS_MEMORY_SERIES.items():
        g = focus[focus["hs_code"] == code].sort_values("date")
        if g.empty:
            cards.append(_overview_card(label, "N/A", "게시 데이터 없음"))
            continue
        latest = float(g["export_unit_price_weight"].iloc[-1])
        series_period = str(g["period"].iloc[-1])
        mom = _kcs_change(g, 1)
        yoy = _kcs_change(g, 12)
        tone = _change_tone(mom)
        sub_bits = [f"{series_period} 기준"]
        if mom is not None:
            sub_bits.append(f"전월 {pct(mom)}")
        if yoy is not None:
            sub_bits.append(f"전년 {pct(yoy)}")
        cards.append(
            _overview_card(
                label,
                f"{latest:,.0f} USD/kg",
                " · ".join(sub_bits) if sub_bits else "변화율 계산 대기",
                tone,
            )
        )

    st.markdown(
        "<div class='cycle-overview-grid'>" + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns([2.6, 1.15, 1.15])
    with c1:
        years = st.radio(
            "차트 기간",
            options=[3, 5, 7, 10],
            index=1,
            horizontal=True,
            key="kcs_years",
            format_func=lambda v: f"{v}년",
        )
    with c2:
        st.markdown("<div style='height:2px'></div>", unsafe_allow_html=True)
        include_logic = st.checkbox("Logic 대조군", value=False, key="kcs_logic")
    with c3:
        st.markdown("<div style='height:2px'></div>", unsafe_allow_html=True)
        normalized = st.checkbox("시작값=100", value=True, key="kcs_normalized")

    st.plotly_chart(
        kcs_memory_chart(df, years, include_logic, normalized),
        use_container_width=True,
        key="kcs_memory_chart",
        config={"displayModeBar": False, "responsive": True},
    )

    st.markdown(
        "<div class='reading-guide compact'>"
        "<div class='reading-guide-label'>읽는 법</div>"
        "<div class='reading-guide-copy'>"
        "관세청 수출단가는 <b>현물 칩 가격이 아니라 수출금액÷중량</b>으로 계산한 제품 믹스 포함 지표입니다. "
        "MCP는 HBM 전용 가격이 아니라 HBM을 포함할 수 있는 대리지표이며, 모델에는 공표 지연을 반영합니다. "
        "기본 차트는 서로 다른 단가 수준을 비교하기 쉽도록 선택 기간의 첫 값을 100으로 맞춥니다."
        "</div></div>",
        unsafe_allow_html=True,
    )

    with st.expander("메모리 지표 설명 보기", expanded=False):
        st.markdown(
            "- **DRAM / NAND Flash**: 관세청 월별 품목별 수출금액÷중량 기준 단가\n"
            "- **MCP / HBM proxy**: HBM 전용 HS 코드가 없어 사용하는 대리지표\n"
            "- **Logic 대조군**: 일반 로직 IC와 메모리 사이클의 상대 흐름 비교용\n"
            "- 모델 학습에서는 해당 월 통계를 **익월 15일 이후**에만 사용할 수 있도록 시점을 지연합니다."
        )


def _display_timestamp(value: Any, date_only: bool = False) -> str:
    ts = _parse_timestamp(value)
    if ts is None:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return "확인 불가"
        return f"{parsed:%Y-%m-%d}" if date_only else str(value)
    local = ts.astimezone(KST)
    return f"{local:%Y-%m-%d}" if date_only else f"{local:%Y-%m-%d %H:%M} KST"


def render_data_health(manifest: Dict, quotes: Dict, symbol: str,
                       predictions: pd.DataFrame) -> None:
    """핵심 원천별 최신 시각·사용 가능 여부를 한 화면에서 감사한다."""
    rows: List[Tuple[str, str, str, str]] = []
    _, snapshot_stale = snapshot_label(manifest)
    rows.append((
        "예측 결과", _display_timestamp(manifest.get("generated_at")),
        "확인" if snapshot_stale else "정상",
        "새 학습 필요" if snapshot_stale else "최근 거래일 일정 기준",
    ))

    raw_prediction = _prediction_at(predictions, symbol, 5)
    if raw_prediction is None:
        candidates = predictions[predictions["symbol"].astype(str).map(canonical_symbol) == canonical_symbol(symbol)]
        raw_prediction = candidates.iloc[0].to_dict() if not candidates.empty else {}
    country = str((raw_prediction or {}).get("country") or "KR")
    missing_inputs = (raw_prediction or {}).get("missing_data")
    if missing_inputs:
        rows.append(("현재 모델 입력", "일부 자동 제외", "확인", f"누락: {missing_inputs}"))
    else:
        rows.append(("현재 모델 입력", "게시 결과 기준", "정상", "보고된 누락 데이터 없음"))
    qinfo = quote_info(quotes, symbol, country)
    rows.append((
        f"현재가 · {symbol}", str(qinfo.get("age_label") or "시각 없음"),
        "정상" if qinfo.get("usable") else "확인",
        "자동 보정 사용 가능" if qinfo.get("usable") else str(qinfo.get("reason") or "시세 없음"),
    ))

    history = load_history(symbol)
    if history is not None and not history.empty:
        last_day = pd.Timestamp(history["date"].max())
        if last_day.tzinfo is not None:
            last_day = last_day.tz_convert(_market_timezone(country)).tz_localize(None)
        local_today = pd.Timestamp(datetime.now(_market_timezone(country)).date())
        bdays = max(0, len(pd.bdate_range(last_day.normalize(), local_today)) - 1)
        rows.append(("일봉", f"{last_day:%Y-%m-%d}", "정상" if bdays <= 1 else "확인",
                     f"평일 기준 {bdays}일 경과"))
    else:
        rows.append(("일봉", "파일 없음", "미설정", "차트와 전일 대비 계산 제한"))

    kcs = load_kcs_memory()
    if kcs is not None and not kcs.empty:
        latest_month = pd.Period(pd.Timestamp(kcs["date"].max()), freq="M")
        now_kst = datetime.now(KST)
        current_month = pd.Period(now_kst.strftime("%Y-%m"), freq="M")
        expected_month = current_month - (1 if now_kst.day >= 15 else 2)
        rows.append((
            "관세청 메모리", str(latest_month),
            "정상" if latest_month >= expected_month else "확인",
            f"현재 기대 공표월 {expected_month}",
        ))
    else:
        rows.append(("관세청 메모리", "파일 없음", "미설정", "KCS Feature와 업황 탭 제한"))

    spot = load_memory_spot()
    if not spot.empty:
        last_spot = pd.Timestamp(spot["date"].max())
        if last_spot.tzinfo is not None:
            last_spot = last_spot.tz_convert(KST).tz_localize(None)
        age_days = max(0, (pd.Timestamp.now().normalize() - last_spot.normalize()).days)
        rows.append(("메모리 현물가", f"{last_spot:%Y-%m-%d}",
                     "정상" if age_days <= 7 else "확인", f"{age_days}일 경과"))
    else:
        rows.append(("메모리 현물가", "파일 없음", "미설정", "현물가 비교 화면 비활성"))

    prediction_history = load_prediction_history()
    if not prediction_history.empty:
        rows.append(("예측 변화 이력", _display_timestamp(prediction_history["generated_at"].max()),
                     "정상", f"{len(prediction_history):,}건 누적"))
    else:
        rows.append(("예측 변화 이력", "파일 없음", "미설정", "prediction_history.csv 필요"))

    portfolio = load_portfolio()
    rows.append(("보유자산", f"{len(portfolio):,}개 항목" if not portfolio.empty else "파일 없음",
                 "정상" if not portfolio.empty else "미설정",
                 "내 자산 탭 활성" if not portfolio.empty else "assets.csv 또는 portfolio.csv 필요"))

    known = {row[0].lower() for row in rows}
    for item in load_data_status():
        source = str(item.get("source") or item.get("name") or "기타 원천")
        if source.lower() in known:
            continue
        available = item.get("available", True)
        raw_state = str(item.get("status") or "").lower()
        bad = available is False or raw_state in {"error", "failed", "stale", "missing"}
        state = "확인" if bad else "정상"
        when = item.get("last_data_time") or item.get("updated_at") or item.get("as_of") or "확인 불가"
        detail = item.get("message") or item.get("detail") or item.get("note") or "publish 상태 파일"
        rows.append((source, _display_timestamp(when), state, str(detail)))

    rendered = []
    for source, latest, state, detail in rows:
        cls = {"정상": "ok", "확인": "warn", "미설정": "off"}.get(state, "off")
        rendered.append(
            "<div class='health-row'>"
            f"<div class='health-source'>{html.escape(source)}</div>"
            f"<div class='health-latest'>{html.escape(latest)}</div>"
            f"<div><span class='health-state {cls}'>{html.escape(state)}</span></div>"
            f"<div class='health-detail'>{html.escape(detail)}</div>"
            "</div>"
        )
    subsection_head("데이터 상태", "원천별 마지막 시각과 현재 화면 사용 가능 여부입니다.")
    st.markdown("<div class='health-list'>" + "".join(rendered) + "</div>",
                unsafe_allow_html=True)


def _is_cash_asset(row: pd.Series) -> bool:
    symbol = str(row.get("symbol") or "").upper()
    asset_type = str(row.get("asset_type") or "").lower()
    return asset_type == "cash" or symbol.startswith("CASH") or symbol in {"KRW", "USD"}


def render_portfolio(portfolio: pd.DataFrame, predictions: pd.DataFrame,
                     quotes: Dict) -> None:
    """보유수량 기준 현재·80% 하단·P50·80% 상단 자산가치를 통화별로 표시한다."""
    section_head("MY ASSETS", "내 보유자산", "통화가 다른 자산은 임의 환산하지 않고 따로 합산합니다.")
    horizons = sorted({int(x) for x in pd.to_numeric(predictions["horizon"], errors="coerce").dropna()})
    if not horizons:
        st.info("보유자산에 적용할 예측 기간이 없습니다.")
        return
    query_h = num(_query_value("h"))
    default_h = int(query_h) if query_h is not None else 0
    if default_h not in horizons:
        default_h = 5 if 5 in horizons else horizons[0]
    if ("portfolio_horizon" not in st.session_state
            or st.session_state.get("portfolio_horizon") not in horizons):
        st.session_state["portfolio_horizon"] = default_h
    horizon = st.radio("보유자산 전망 기간", horizons, horizontal=True,
                       key="portfolio_horizon", format_func=lambda h: f"{h}일")

    forecast_payload = load_portfolio_forecast()
    horizon_payload = forecast_payload.get("horizons", forecast_payload)
    risk = (horizon_payload.get(str(horizon)) or horizon_payload.get(horizon) or {}) \
        if isinstance(horizon_payload, dict) else {}
    if isinstance(risk, dict) and risk:
        loss_prob = num(risk.get("loss_probability"))
        var95 = num(risk.get("var_95_return"))
        es95 = num(risk.get("expected_shortfall_95_return"))
        paths = num(risk.get("simulation_paths") or risk.get("n_paths"))
        risk_cards = []
        if loss_prob is not None:
            risk_cards.append(_overview_card("손실확률", pct(loss_prob, signed=False),
                                             "상관관계 반영 시뮬레이션"))
        if var95 is not None:
            risk_cards.append(_overview_card("95% VaR", pct(var95), "하위 5% 경계"))
        if es95 is not None:
            risk_cards.append(_overview_card("95% ES", pct(es95), "하위 5% 평균 손실"))
        if paths is not None:
            risk_cards.append(_overview_card("시뮬레이션", f"{paths:,.0f}경로", "게시 엔진 계산"))
        if risk_cards:
            st.markdown("<div class='portfolio-summary-grid'>" + "".join(risk_cards) + "</div>",
                        unsafe_allow_html=True)

    totals: Dict[str, Dict[str, float]] = {}
    table_rows = []
    for _, row in portfolio.iterrows():
        symbol = str(row.get("symbol") or "")
        quantity = num(row.get("quantity")) or 0.0
        average = num(row.get("average_price"))
        currency = str(row.get("currency") or "").upper()
        name = str(row.get("name") or symbol)

        if _is_cash_asset(row):
            if not currency:
                currency = "USD" if "USD" in symbol.upper() else "KRW"
            amount = (num(row.get("market_value")) or num(row.get("value"))
                      or num(row.get("amount")) or quantity or average or 0.0)
            values = {"cost": amount, "current": amount, "low": amount,
                      "median": amount, "high": amount}
            table_rows.append({"자산": name, "종목": symbol, "상태": "현금", "수량": "현금",
                               "현재가": price(amount, currency), "현재 손익": "—",
                               "80% 하단": price(amount, currency),
                               "P50": price(amount, currency), "80% 상단": price(amount, currency)})
        else:
            raw = _prediction_at(predictions, symbol, horizon)
            if raw is None:
                table_rows.append({"자산": name, "종목": symbol, "상태": "예측 없음", "수량": f"{quantity:,.4g}",
                                   "현재가": "예측 없음", "현재 손익": "—",
                                   "80% 하단": "—", "P50": "—", "80% 상단": "—"})
                continue
            p = display_prediction(raw, quotes)
            currency = currency or str(p.get("currency") or "KRW")
            forecast_current = num(p.get("current_price"))
            current = (num(p.get("_live_price")) if p.get("_quote_usable") else None) or forecast_current
            low, high, _ = interval_80_of(p)
            median = num(p.get("p50"))
            if current is None or forecast_current is None:
                continue
            cost = average * quantity if average is not None else current * quantity
            values = {
                "cost": cost, "current": current * quantity,
                "low": (low if low is not None else forecast_current) * quantity,
                "median": (median if median is not None else forecast_current) * quantity,
                "high": (high if high is not None else forecast_current) * quantity,
            }
            pnl = values["current"] / cost - 1.0 if cost > 0 else None
            state = "재학습 대기" if p.get("_reanchor_warning") and p.get("_quote_usable") else grade_ko(grade_of(p))
            table_rows.append({"자산": name, "종목": symbol, "상태": state, "수량": f"{quantity:,.4g}",
                               "현재가": price(current, currency), "현재 손익": pct(pnl),
                               "80% 하단": price(values["low"], currency),
                               "P50": price(values["median"], currency),
                               "80% 상단": price(values["high"], currency)})

        bucket = totals.setdefault(currency or "KRW", {key: 0.0 for key in values})
        for key, value in values.items():
            bucket[key] += float(value)

    cards = []
    for currency, values in totals.items():
        pnl = values["current"] / values["cost"] - 1.0 if values["cost"] > 0 else None
        forecast = values["median"] / values["current"] - 1.0 if values["current"] > 0 else None
        cards.extend([
            _overview_card(f"현재 자산 · {currency}", price(values["current"], currency),
                           f"원금 대비 {pct(pnl)}", _change_tone(pnl)),
            _overview_card(f"{horizon}일 P50 · {currency}", price(values["median"], currency),
                           f"현재 대비 {pct(forecast)}", _change_tone(forecast)),
            _overview_card(f"80% 하단 · {currency}", price(values["low"], currency), "보수 시나리오"),
            _overview_card(f"80% 상단 · {currency}", price(values["high"], currency), "낙관 시나리오"),
        ])
    if cards:
        st.markdown("<div class='portfolio-summary-grid'>" + "".join(cards) + "</div>",
                    unsafe_allow_html=True)
    if table_rows:
        render_dark_table(pd.DataFrame(table_rows))
    st.caption(
        "각 종목의 동일 분위수를 단순 합산한 시나리오입니다. 종목 간 상관관계를 반영한 "
        "포트폴리오 손실확률로 해석하면 안 되며, 통화도 서로 합산하지 않습니다. "
        "손실확률·VaR·ES가 별도 표시될 때만 portfolio_forecast.json의 상관관계 반영 결과입니다."
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
        if len(future):
            target_date = future[-1]
            hover_number = ",.0f" if str(currency).upper() == "KRW" else ",.2f"
            outer_low, outer_high, _ = interval_80_of(p)
            inner_low, inner_high = num(p.get("p25")), num(p.get("p75"))
            median = num(p.get("p50"))

            # 이 모델이 실제로 예측한 값은 선택한 h거래일 뒤의 종점 분포다.
            # 중간 날짜를 임의 보간하지 않고 만기일의 수직 범위로만 표시한다.
            if outer_low is not None and outer_high is not None:
                fig.add_trace(go.Scatter(
                    x=[target_date, target_date], y=[outer_low, outer_high],
                    mode="lines", name="80% 만기 범위",
                    line=dict(color="rgba(49,130,246,0.46)", width=4),
                    hovertemplate=(
                        f"{hz}거래일 뒤 80% 범위<br>"
                        f"%{{y:{hover_number}}}<extra></extra>"
                    ),
                ), row=1, col=1)
            if inner_low is not None and inner_high is not None:
                fig.add_trace(go.Scatter(
                    x=[target_date, target_date], y=[inner_low, inner_high],
                    mode="lines", name="50% 만기 범위",
                    line=dict(color="rgba(49,130,246,0.92)", width=12),
                    hovertemplate=(
                        f"{hz}거래일 뒤 50% 범위<br>"
                        f"%{{y:{hover_number}}}<extra></extra>"
                    ),
                ), row=1, col=1)
            if median is not None:
                guide_color = "#7f8b99" if grade_of(p) == "LOW" else FCOL
                fig.add_trace(go.Scatter(
                    x=[last_date, target_date], y=[now, median], mode="lines",
                    name="현재가→P50 안내선",
                    line=dict(color=guide_color, width=1.8, dash="dot"),
                    hoverinfo="skip",
                ), row=1, col=1)
                fig.add_trace(go.Scatter(
                    x=[target_date], y=[median], mode="markers", name="P50 (기준값)",
                    marker=dict(color=guide_color, size=10,
                                line=dict(color="#e8edf3", width=1)),
                    hovertemplate=(
                        f"{hz}거래일 뒤 P50<br><b>%{{y:{hover_number}}}</b>"
                        "<extra></extra>"
                    ),
                ), row=1, col=1)
                fig.add_annotation(
                    x=target_date, y=median,
                    text=f"{hz}일 뒤 · {price(median, currency, False)} ",
                    showarrow=False, xanchor="right", yshift=15,
                    bgcolor="rgba(8,11,16,0.82)", borderpad=3,
                    font=dict(color=guide_color, size=11), row=1, col=1,
                )
            fig.add_vline(x=last_date,
                          line=dict(color="rgba(255,255,255,0.20)", width=1, dash="dot"))
            fig.add_hline(
                y=now,
                line=dict(color="rgba(255,255,255,0.10)", width=1, dash="dot"),
                annotation_text="현재가",
                annotation_position="top left",
                annotation_font=dict(color="#7f8995", size=10),
                row=1, col=1,
            )

    fig.update_layout(
        template="plotly_dark", height=438 if show_volume else 398,
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
    horizon_values = pd.to_numeric(sub["horizon"], errors="coerce")
    horizons = sorted({int(h) for h in horizon_values.dropna()})
    stock_name = str(sub["name"].iloc[0]) if "name" in sub.columns and len(sub) else symbol

    if not horizons:
        st.warning("이 종목에는 표시할 수 있는 예측 기간이 없습니다.")
        return

    section_head(
        "FORECAST",
        f"{stock_name} · {symbol}",
        "한 가격을 맞히기보다 가능한 범위와 불확실성을 함께 보여줍니다.",
    )

    try:
        control_container = st.container(border=True)
    except TypeError:  # 구버전 Streamlit 호환
        control_container = st.container()
    with control_container:
        st.markdown(
            "<div class='forecast-controls'>보고 싶은 예측 기간과 차트 범위를 선택하세요.</div>",
            unsafe_allow_html=True,
        )
        horizon_key = f"h_{symbol}"
        query_h = num(_query_value("h"))
        if (horizon_key not in st.session_state
                or st.session_state.get(horizon_key) not in horizons):
            requested_h = int(query_h) if query_h is not None else None
            st.session_state[horizon_key] = requested_h if requested_h in horizons else (5 if 5 in horizons else horizons[0])
        c_h, c_lb, c_vol = st.columns([3, 1.65, 1.2])
        with c_h:
            horizon = st.radio(
                "얼마 뒤를 볼까요? (거래일 기준)", horizons, horizontal=True, key=horizon_key,
                format_func=lambda h: f"{h}일",
            )
            _set_query_value("h", horizon)
        with c_lb:
            chart_windows = {
                "1개월": 22,
                "3개월": 66,
                "6개월": 132,
                "1년": 250,
                "2년": 500,
            }
            chart_window = st.selectbox(
                "차트 기간",
                options=list(chart_windows),
                index=2,
                key=f"lb_{symbol}",
            )
            lookback = chart_windows[chart_window]
        with c_vol:
            st.markdown("<div style='height:2px'></div>", unsafe_allow_html=True)
            show_volume = st.checkbox("거래량 함께 보기", value=True, key=f"v_{symbol}")

    row = sub[horizon_values == horizon]
    if row.empty:
        st.warning("해당 기간의 예측이 없습니다.")
        return
    # Streamlit 은 요소 id 를 인자 조합으로 계산하므로, 탭마다 같은 모양의 차트/표를
    # 그리면 id 가 충돌한다(StreamlitDuplicateElementId). 종목·기간으로 키를 준다.
    uid = f"{symbol}_{horizon}"
    p = row.iloc[0].to_dict()
    quotes = quotes or {}
    p = display_prediction(p, quotes)
    currency = p.get("currency", "KRW")
    now = num(p.get("current_price"))
    hist = load_history(symbol)
    diag = (((payload.get("diagnostics") or {}).get(symbol) or {}).get(str(horizon)) or {})

    # ---- 결론 -> 핵심 숫자 -> 읽는 법 ----
    grade = grade_of(p)
    grade_label = grade_ko(grade)
    confidence = fnum(p.get("confidence"), 0)
    verdict_title = {
        "LOW": "방향 판단은 잠시 보류",
        "MEDIUM": "참고 가능한 신호",
        "HIGH": "상대적으로 안정적인 신호",
    }.get(grade, "방향 판단은 잠시 보류")
    st.markdown(
        f"<div class='verdict {grade.lower()}'>"
        f"<div class='verdict-icon'>{DOT.get(grade, '●')}</div>"
        "<div class='verdict-body'>"
        "<div class='verdict-head'>"
        f"<div class='verdict-title'>{html.escape(verdict_title)}</div>"
        f"<div class='verdict-confidence {grade.lower()}'>"
        f"신뢰도 {html.escape(confidence)}/100 · {html.escape(grade_label)}</div>"
        "</div>"
        f"<div class='verdict-copy'>{html.escape(verdict(p))}</div>"
        "</div></div>",
        unsafe_allow_html=True,
    )
    if p.get("_reanchor_warning"):
        live_note = ""
        if p.get("_quote_usable") and num(p.get("_live_price")) is not None:
            live_note = f" 최신 시세는 {price(p.get('_live_price'), str(currency))}입니다."
        st.markdown(
            "<div class='tab-callout warn'><b>최신가 자동 보정 보류</b> · "
            f"{html.escape(str(p.get('_reanchor_warning')) + live_note)}</div>",
            unsafe_allow_html=True,
        )
    render_forecast_summary(p, hist, quotes, horizon)

    # ---- 차트: 첫 화면에서 최대한 빨리 보이도록 핵심 요약 바로 아래에 둔다. ----
    st.plotly_chart(
        candle_chart(hist, p, lookback, show_volume),
        use_container_width=True, key=f"candle_{uid}",
        config={"displayModeBar": False, "responsive": True},
    )
    render_forecast_secondary_metrics(p)
    st.markdown(
        "<div class='chart-caption'>"
        "<span>끝점: 선택 기간의 P50 기준값</span>"
        "<span>굵은 세로선 50% · 얇은 세로선 80%</span>"
        "<span>점선은 현재와 만기를 잇는 안내선 · 중간 날짜 예측 아님</span>"
        "</div>",
        unsafe_allow_html=True,
    )
    render_forecast_reading_guide(p, horizon)
    render_prediction_history(load_prediction_history(), symbol, horizon)
    render_prediction_drivers(p, diag)

    # ---- 판단에 쓰는 참고값은 하나의 찾기 쉬운 묶음으로 제공한다. ----
    with st.expander("투자 판단 참고선 · 목표·손절·추가매수"):
        if grade == "LOW":
            st.warning(
                "현재 신뢰도는 LOW입니다. 아래 가격은 주문 지시가 아니라 위험 범위를 확인하는 참고값입니다.",
                icon="⚠️",
            )
        decision_cards = [
            _decision_card("1차 목표", price(p.get("target_1"), currency), "수익 실현 참고"),
            _decision_card("2차 목표", price(p.get("target_2"), currency), "강한 상승 시 참고"),
            _decision_card("추가매수 고려", price(p.get("add_buy_reference"), currency), "분할 접근 참고"),
            _decision_card("손절 고려", price(p.get("stop_loss_reference"), currency), "위험 관리 참고"),
            _decision_card("20일 지지", price(p.get("support_20d"), currency), "최근 가격 하단"),
            _decision_card("20일 저항", price(p.get("resistance_20d"), currency), "최근 가격 상단"),
            _decision_card("손익비 R/R", fnum(p.get("risk_reward")), "1보다 크면 보상 우위"),
            _decision_card("ATR", pct(p.get("atr_pct"), signed=False), "최근 가격 변동 폭"),
        ]
        st.markdown(
            "<div class='decision-grid'>" + "".join(decision_cards) + "</div>",
            unsafe_allow_html=True,
        )
        st.caption("기계적인 주문 가격이 아니라, 예상 분포와 최근 가격대를 바탕으로 만든 참고선입니다.")

    with st.expander("예상 분포 자세히 보기 · P10~P90"):
        rows = []
        for key, lab in [("p90", "P90"), ("p75", "P75"), ("p50", "P50 (기준값)"),
                         ("p25", "P25"), ("p10", "P10")]:
            v = num(p.get(key))
            chg = (v / now - 1.0) if (v is not None and now) else None
            rows.append({"구간": lab, "가격": price(v, currency), "현재가 대비": pct(chg)})
        render_dark_table(pd.DataFrame(rows))

    with st.expander("차트와 숫자, 어떻게 읽나요?"):
        render_forecast_help(p)

    with st.expander("왜 이런 결과가 나왔나요? · 모델 진단"):
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
        # MZ 값은 보정이 identity(alpha=0, beta=1)인 경우에도 항상 표시한다.
        # 즉 대시보드만 보고도 "보정됨 / 원예측 유지" 여부를 바로 알 수 있게 한다.
        sh = num(p.get("shrinkage"))
        mz_alpha = num(p.get("mz_intercept"))
        mz_raw_beta = num(p.get("mz_raw_slope"))
        mz_beta_se = num(p.get("mz_slope_se"))

        # 최신 Prediction에는 항상 들어오는 값이지만, 과거 published 스냅샷과의
        # 호환성을 위해 필드가 없으면 identity MZ 값으로 표시한다.
        sh_display = 1.0 if sh is None else sh
        mz_alpha_display = 0.0 if mz_alpha is None else mz_alpha

        # MZ 재보정은 2026-08-30 자로 기본 비활성화됐다(apply_mz_shrinkage=False).
        # 4종목 A/B 에서 IC·RMSE·DA 를 일관되게 악화시켰기 때문이다
        # (MU IC +0.099 -> +0.269). 자세한 근거는 DEVNOTES 0.9.2 참조.
        #
        # 꺼져 있을 때 α/β/원기울기 3줄을 계속 띄우면 "+0.00 / +1.00" 만 반복되어
        # 자리만 차지한다. identity 이면 한 줄로 접고, 실제로 보정이 걸린
        # 경우에만 상세를 펼친다. 옵션을 다시 켜면 자동으로 원래대로 보인다.
        mz_identity = (abs(sh_display - 1.0) <= 1e-3
                       and abs(mz_alpha_display) <= 1e-12)

        if mz_identity:
            info.insert(1, (
                "MZ 재보정", "미적용",
                "원예측을 그대로 사용합니다 (α=0, β=1). "
                "MZ 는 OOS 성능을 악화시켜 2026-08-30 자로 껐습니다."
            ))
        else:
            if sh_display < -0.05:
                mz_desc = "통계적으로 확인된 역방향 관계를 반영"
            elif abs(sh_display) < 0.05:
                mz_desc = "ML 변동신호는 거의 제거됨"
            else:
                mz_desc = "최종 점예측 = MZ 절편 + β × ML/DL 예측"

            info.insert(1, ("MZ 보정 β", f"{sh_display:+.2f}", mz_desc))
            info.insert(2, (
                "MZ 절편 α", f"{mz_alpha_display:+.2%}",
                "0이면 별도 절편 보정을 적용하지 않음"
            ))
            if mz_raw_beta is not None:
                se_txt = f" ± {mz_beta_se:.3f}" if mz_beta_se is not None else ""
                info.insert(3, (
                    "MZ 원기울기", f"{mz_raw_beta:+.3f}{se_txt}",
                    "전체 OOS에서 추정한 raw β와 HAC 표준오차"
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

        show_catalog = st.checkbox(
            "Feature 전체 사전 불러오기",
            value=False,
            key=f"show_feature_catalog_{symbol}_{horizon}",
            help="수백 개 Feature 표는 필요할 때만 불러와 첫 화면을 가볍게 유지합니다.",
        )
        if show_catalog:
            st.markdown("<div class='diag-section-title'>Feature 전체 사전<span>선택된 항목과 미선택 후보를 모두 표시</span></div>", unsafe_allow_html=True)
            render_all_feature_catalog(symbol, horizon, payload, diag, top_features)
        else:
            st.caption("전체 Feature 정의와 선택·제외 상태는 위 옵션을 켜면 표시됩니다.")

        comps = p.get("confidence_components")
        if isinstance(comps, dict) and comps:
            st.markdown("**신뢰도 구성** — 100점 만점 신뢰도를 어떤 항목이 깎거나 받쳐주는지")
            st.caption("각 달성도는 독립적인 성공확률이 아니라 모델 신뢰도 점수를 구성하는 내부 진단값입니다.")
            label = {
                "baseline_improvement": "baseline 대비 RMSE 개선",
                "information_coefficient": "IC (순위 상관)",
                "directional_accuracy": "방향 edge (기준 대비)",
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
                    "방향정확도가 기준선(50%/다수방향/baseline)을 넘지 못함 → "
                    "신뢰도는 LOW 범위(최대 44)로 제한됩니다."
                )
            elif comps.get("_weak_predictive_edge_cap"):
                st.caption(
                    "예측 edge가 아직 약합니다: RMSE 개선<0.5% + IC<0.03 + "
                    "방향 기준선 대비 edge<+2%p → HIGH는 보류하고 최대 69점까지 허용합니다."
                )

        if p.get("regime"):
            st.caption(f"시장 regime · {p.get('regime')}")
        if p.get("notes"):
            notes_list = [n.strip() for n in str(p.get("notes")).split(" | ")
                          if n.strip()]

            # 파이프라인 구성은 문장 대신 배지로 먼저 보여준다. 어떤 기법이
            # 실제로 걸려 있는지가 긴 설명보다 먼저 눈에 들어와야 한다.
            joined = " ".join(notes_list)
            flags = []
            if "패널 OOF 합류" in joined:
                flags.append(("패널", True))
            if "NNLS" in joined:
                flags.append(("NNLS 스태킹", True))
            if "조건부 스케일" in joined:
                flags.append(("조건부 sigma", True))
            if "꼬리 이탈 보정" in joined:
                flags.append(("꼬리 보정", True))
            if "drift 축소" in joined:
                flags.append(("drift 축소", True))
            if flags:
                st.markdown(
                    "<div class='status-strip'>" + "".join(
                        f"<span class='status-pill'>{html.escape(name)}</span>"
                        for name, _ in flags
                    ) + "</div>",
                    unsafe_allow_html=True,
                )

            for n in notes_list:
                st.caption(f"· {n}")

def render_symbol_validation(symbol: str, sub: pd.DataFrame, payload: Dict) -> None:
    """종목 전망에서 분리한 라이브 검증과 백테스트."""
    horizon_values = pd.to_numeric(sub["horizon"], errors="coerce")
    horizons = sorted({int(h) for h in horizon_values.dropna()})
    if not horizons:
        return
    forecast_h = st.session_state.get(f"h_{symbol}")
    default_h = int(forecast_h) if forecast_h in horizons else horizons[0]
    key = f"validation_h_{symbol}"
    if key not in st.session_state or st.session_state.get(key) not in horizons:
        st.session_state[key] = default_h

    subsection_head("선택 종목 검증", "라이브 기록과 과거 OOS 백테스트를 같은 기간으로 봅니다.")
    horizon = st.radio("종목 검증 기간", horizons, horizontal=True, key=key,
                       format_func=lambda h: f"{h}일")
    uid = f"validation_{symbol}_{horizon}"

    track = load_track()
    cands = [g for g in (track.get("groups") or [])
             if canonical_symbol(g.get("symbol")) == canonical_symbol(symbol)
             and int(g.get("horizon", -1)) == horizon]
    tg = next((g for g in cands if str(g.get("source")) == "LIVE"),
              cands[0] if cands else None)
    bt_meta = (payload.get("backtests") or {}).get(f"{symbol}_h{horizon}")
    bt_df = load_backtest(symbol, horizon)
    if not tg and not track and not bt_meta and bt_df is None:
        st.info("이 종목·기간의 게시된 라이브 검증 또는 백테스트가 아직 없습니다.")
        return

    if tg or track:
        with st.expander("예측 기록과 실제 결과 비교", expanded=bool(tg)):
            if tg and tg.get("n_resolved"):
                if str(tg.get("source")) == "BACKFILL":
                    st.caption(
                        "구분: **BACKFILL** — 과거 시점마다 그 시점 정보만으로 재학습한 기록이며 "
                        "라이브 기록이 쌓이기 전의 대용치입니다."
                    )
                n = int(tg["n_resolved"])
                cov, ci = tg.get("coverage_80"), tg.get("coverage_80_ci") or [None, None]
                dh, dci = tg.get("direction_hit"), tg.get("direction_hit_ci") or [None, None]
                c = st.columns(4)
                sample_help = "예측을 먼저 기록하고 만기 후 결과를 채운 건수입니다."
                coverage_help = (f"목표 80%. 95% 신뢰구간 "
                                 f"{pct(ci[0], signed=False)}~{pct(ci[1], signed=False)}")
                direction_help = (f"50%가 동전 던지기. 95% 신뢰구간 "
                                  f"{pct(dci[0], signed=False)}~{pct(dci[1], signed=False)}")
                c[0].metric("확정 표본", f"{n}건", help=sample_help)
                c[0].markdown(mobile_help_html(sample_help), unsafe_allow_html=True)
                c[1].metric("80% 구간 적중", pct(cov, signed=False), help=coverage_help)
                c[1].markdown(mobile_help_html(coverage_help), unsafe_allow_html=True)
                c[2].metric("방향 적중", pct(dh, signed=False), help=direction_help)
                c[2].markdown(mobile_help_html(direction_help), unsafe_allow_html=True)
                c[3].metric("P50 평균오차", pct(tg.get("mae_p50"), signed=False))
                if n < 30:
                    st.warning(f"확정 표본이 {n}건이라 아직 결론을 내리기 어렵습니다.", icon="⚠️")
                elif cov is not None and ci[1] is not None and ci[1] < 0.8:
                    st.warning("80% 구간 적중률이 목표를 유의하게 밑돕니다.", icon="⚠️")
            else:
                st.caption(
                    f"이 조합은 아직 만기 도래분이 없습니다. 기록 {track.get('n_total', 0)}건 · "
                    f"대기 {track.get('n_pending', 0)}건. 약 {horizon}거래일 뒤부터 채워집니다."
                )
            st.caption(
                "예측을 먼저 남기고 결과를 나중에 채우므로 사후 조정이 불가능한 검증입니다."
            )

    if bt_meta or bt_df is not None:
        with st.expander("과거 데이터로 확인한 성적 · 백테스트", expanded=False):
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
                    st.plotly_chart(fig, use_container_width=True, key=f"equity_{uid}",
                                    config={"displayModeBar": False, "responsive": True})
            st.caption(
                "⚠️ 모델 채택과 가중치가 이 OOS 구간 성능의 영향을 받으므로 selection bias가 있습니다. "
                "실제 운용 성과는 더 낮을 수 있으며 다중검정과 상승장 Buy & Hold도 함께 고려하세요."
            )


# ======================================================================================
# 브라우저 네트워크 정보 · Streamlit Cloud 프록시 우회용
# ======================================================================================
def _browser_network_info() -> Optional[Dict]:
    """
    방문자 브라우저에서 직접 공인 IP를 조회한다.

    Streamlit Community Cloud의 Python 프로세스는 실제 방문자 대신 내부 프록시
    (127.0.0.1)를 볼 수 있으므로 서버 IP는 방문자 IP로 사용하지 않는다.

    이번 버전은 ``streamlit-js-eval``을 사용한다. 이 컴포넌트는 Promise 결과를
    Python으로 돌려줄 수 있어 비동기 fetch 결과를 받을 수 있다.
    실제 공인 IP가 오기 전에는 analytics 세션을 시작하지 않는다.
    """
    cached = st.session_state.get("dashview_browser_network_info")
    if isinstance(cached, dict) and str(cached.get("ip") or "").strip():
        return cached

    try:
        from streamlit_js_eval import streamlit_js_eval
    except Exception as exc:
        print(
            "DASHVIEW_BROWSER_IP_COMPONENT_ERROR "
            f"{type(exc).__name__}: {str(exc)[:180]}",
            flush=True,
        )
        return None

    javascript = r"""
    (async function(){
        const base = {
            user_agent: (window.navigator && window.navigator.userAgent) || "",
            timezone: (Intl.DateTimeFormat().resolvedOptions().timeZone) || "",
            locale: (window.navigator && (window.navigator.language ||
                     (window.navigator.languages && window.navigator.languages[0]))) || ""
        };

        async function lookup(url, label) {
            const controller = new AbortController();
            const timer = setTimeout(function(){ controller.abort(); }, 3500);
            try {
                const response = await fetch(url, {
                    method: "GET",
                    mode: "cors",
                    cache: "no-store",
                    credentials: "omit",
                    signal: controller.signal
                });
                if (!response.ok) {
                    throw new Error(label + " HTTP " + response.status);
                }
                const data = await response.json();
                const ip = (data && data.ip) ? String(data.ip).trim() : "";
                if (!ip) {
                    throw new Error(label + " returned empty IP");
                }
                return Object.assign({}, base, {
                    ip: ip,
                    source: label,
                    ok: true,
                    error: ""
                });
            } finally {
                clearTimeout(timer);
            }
        }

        let errors = [];
        try {
            return await lookup("https://api.ipify.org?format=json", "browser-ipify-v4");
        } catch (e1) {
            errors.push(String(e1 || "ipify-v4 failed"));
        }

        try {
            return await lookup("https://api64.ipify.org?format=json", "browser-ipify-v64");
        } catch (e2) {
            errors.push(String(e2 || "ipify-v64 failed"));
        }

        return Object.assign({}, base, {
            ip: "",
            source: "browser-ipify-failed",
            ok: false,
            error: errors.join(" | ")
        });
    })()
    """

    try:
        value = streamlit_js_eval(
            js_expressions=javascript,
            key="DASHVIEW_BROWSER_PUBLIC_IP",
        )
    except Exception as exc:
        print(
            "DASHVIEW_BROWSER_IP_EXEC_ERROR "
            f"{type(exc).__name__}: {str(exc)[:180]}",
            flush=True,
        )
        return None

    # 컴포넌트 첫 렌더에서는 None일 수 있다.
    if value is None:
        print("DASHVIEW_BROWSER_IP_PENDING value=None", flush=True)
        return None

    if not isinstance(value, dict):
        print(
            "DASHVIEW_BROWSER_IP_PENDING "
            f"type={type(value).__name__} value={str(value)[:100]}",
            flush=True,
        )
        return None

    info = {
        "ip": str(value.get("ip") or "").strip(),
        "source": str(value.get("source") or "browser-ipify"),
        "user_agent": str(value.get("user_agent") or ""),
        "timezone": str(value.get("timezone") or ""),
        "locale": str(value.get("locale") or ""),
        "ok": bool(value.get("ok")),
        "error": str(value.get("error") or "")[:300],
    }

    if not info["ip"]:
        print(
            "DASHVIEW_BROWSER_IP_FAILED "
            f"source={info['source']} error={info['error']}",
            flush=True,
        )
        return None

    st.session_state["dashview_browser_network_info"] = info
    print(
        "DASHVIEW_BROWSER_IP_OK "
        f"source={info['source']}",
        flush=True,
    )
    return info


def _privacy_safe_browser_info(info: Dict) -> Dict:
    """방문 집계에는 원 IP 대신 축약 네트워크와 비가역 식별값을 전달한다."""
    out = dict(info or {})
    raw_ip = str(out.get("ip") or "").strip()
    if not raw_ip:
        return out
    try:
        parsed = ipaddress.ip_address(raw_ip)
        prefix = 24 if parsed.version == 4 else 64
        network = ipaddress.ip_network(f"{parsed}/{prefix}", strict=False)
        masked = str(network.network_address)
    except ValueError:
        prefix = 0
        masked = "invalid"
    try:
        salt = str(st.secrets.get("ANALYTICS_IP_SALT", "")).strip()
    except Exception:
        salt = ""
    # 비밀 salt가 없을 때 원 IP를 고정 문자열로 해싱하면 역추적 위험이 있으므로
    # 축약된 네트워크만 해싱한다. salt를 설정하면 방문 중복 제거 정밀도가 높아진다.
    hash_input = f"{salt}|{raw_ip}" if salt else f"network|{masked}"
    out["ip_hash"] = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
    out["ip"] = masked
    out["ip_mask_prefix"] = prefix
    out["privacy"] = "network-masked+sha256"
    return out


# ======================================================================================
# 본문
# ======================================================================================
def main() -> None:
    # 방문 추적. 첫 렌더에서는 브라우저 공인 IP component가 비동기로 동작하므로
    # 값이 아직 없으면 analytics만 잠시 보류한다. 대시보드 화면은 정상 렌더링된다.
    browser_info = _browser_network_info()
    try:
        from analytics import render_session_footer, track_session, track_symbol

        if browser_info is None:
            # component 결과가 돌아오면 Streamlit이 자동 rerun한다.
            track_symbol = None           # type: ignore[assignment]
            render_session_footer = None  # type: ignore[assignment]
        else:
            track_session(browser_info=_privacy_safe_browser_info(browser_info))
    except Exception as exc:
        print(
            f"DASHVIEW_BOOT_ERROR {type(exc).__name__}: {exc}",
            flush=True,
        )
        track_symbol = None           # type: ignore[assignment]
        render_session_footer = None  # type: ignore[assignment]

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
    required_prediction_columns = {"symbol", "horizon", "current_price", "p50"}
    missing_prediction_columns = required_prediction_columns - set(df.columns)
    if missing_prediction_columns:
        st.error(
            "예측 파일 형식이 올바르지 않습니다. 누락 필드: "
            + ", ".join(sorted(missing_prediction_columns))
        )
        st.stop()
    df["symbol"] = df["symbol"].astype(str)
    symbols = sorted(df["symbol"].unique())
    label, stale = snapshot_label(manifest)
    quotes = load_quotes()

    # 첫 화면에는 목적과 최신성만 먼저 보이고, 운영 메타정보는 작은 칩으로 낮춘다.
    quote_label = quotes_age_label(quotes)
    st.markdown(
        f"""
        <div class="dash-hero">
          <div>
            <div class="dash-title">주가 전망</div>
            <div class="dash-subtitle">
              가능한 가격 범위와 불확실성을 함께 확인해 보세요.
            </div>
          </div>
          <div class="dash-meta">
            <span class="status-pill"><span class="status-dot {'warn' if stale else ''}"></span>
            {'업데이트 필요' if stale else '데이터 최신'}</span>
            <div class="dash-updated">예측 생성 · {html.escape(label)}</div>
          </div>
        </div>
        <div class="dashboard-facts">
          <span class="dashboard-fact">분석 종목 <b>{len(symbols)}개</b></span>
          <span class="dashboard-fact">예측 결과 <b>{len(preds)}건</b></span>
          <span class="dashboard-fact">현재가 기준 <b>{html.escape(quote_label)}</b></span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if stale:
        st.error(
            f"예측 데이터가 오래되었습니다. 마지막 결과는 {label}입니다. "
            "로컬 학습과 게시 작업을 다시 실행해 주세요."
        )
    if payload.get("source") == "predictions.csv":
        st.info(
            "현재는 간단한 CSV 결과만 표시 중입니다. publish.py로 전체 결과를 게시하면 "
            "모델 진단과 백테스트도 함께 볼 수 있습니다."
        )

    render_market_overview(df, quotes)

    section_head("ASSET", "어떤 종목을 볼까요?", "선택한 종목을 기준으로 모든 탭이 바뀝니다.")
    st.markdown(
        "<div class='asset-picker-note'>종목을 선택하면 전망·업황·검증 결과가 함께 바뀝니다.</div>",
        unsafe_allow_html=True,
    )
    if "name" in df.columns:
        name_of = {sym: str(df[df["symbol"] == sym]["name"].iloc[0]) for sym in symbols}
    else:
        name_of = {sym: sym for sym in symbols}
    requested_symbol = _query_value("symbol")
    if "symbol_select" not in st.session_state:
        st.session_state["symbol_select"] = requested_symbol if requested_symbol in symbols else symbols[0]
    symbol = st.selectbox(
        "분석할 종목", symbols, key="symbol_select",
        format_func=lambda sym: f"{name_of.get(sym, sym)}  ·  {sym}",
    )
    _set_query_value("symbol", symbol)
    if track_symbol is not None:
        try:
            track_symbol(symbol)
        except Exception:
            pass

    portfolio = load_portfolio()
    tab_names = ["종목 전망"]
    if not portfolio.empty:
        tab_names.append("내 자산")
    tab_names += ["메모리 업황", "전략 검증", "업데이트"]
    tab_map = dict(zip(tab_names, st.tabs(tab_names)))

    with tab_map["종목 전망"]:
        render_symbol(symbol, df[df["symbol"] == symbol], payload, quotes)

    if "내 자산" in tab_map:
        with tab_map["내 자산"]:
            render_portfolio(portfolio, df, quotes)

    with tab_map["메모리 업황"]:
        section_head(
            "MEMORY CYCLE", "메모리 업황",
            "현물가격과 관세청 수출단가를 분리해 확인합니다.",
        )
        render_memory_spot(load_memory_spot())
        kcs_memory = load_kcs_memory()
        if kcs_memory is None or kcs_memory.empty:
            subsection_head("관세청 수출단가", "월별 수출금액÷중량 지표")
            st.info(
                "게시된 메모리 수출단가가 아직 없습니다. "
                "kcs_memory_prices.csv를 게시하면 이 탭에 월별 흐름이 표시됩니다."
            )
        else:
            render_kcs_memory(kcs_memory)

    with tab_map["전략 검증"]:
        panel_data = load_panel_diagnostics()
        portfolio_data = load_portfolio_backtest()
        section_head(
            "VALIDATION",
            "전략 검증",
            "좋아 보이는 예측이 실제 과거 검증에서도 반복됐는지 확인합니다.",
        )
        render_symbol_validation(symbol, df[df["symbol"] == symbol], payload)
        render_panel_diagnostics(panel_data, symbol)
        render_portfolio_backtest(portfolio_data)

    with tab_map["업데이트"]:
        # 업데이트 탭도 동일한 제목 계층을 사용하고, 상세 노트만 아래에 쌓는다.
        section_head(
            "UPDATES",
            "업데이트",
            "모델·데이터·대시보드에서 무엇이 바뀌었는지 확인합니다.",
        )
        render_data_health(manifest, quotes, symbol, df)
        st.divider()
        try:
            from devnotes_view import render_devnotes

            # devnotes_view 내부의 큰 섹션 헤더는 2차 제목으로 낮춰 탭 전체 계층을 통일한다.
            def _devnote_head(_kicker: str, title: str, note: str = "") -> None:
                subsection_head(title, note)

            render_devnotes(PUBLISHED, section_head=_devnote_head)
        except ImportError:
            st.info(
                "아직 표시할 업데이트 기록이 없습니다. devnotes_view.py와 "
                "published/devnotes.json을 함께 게시하면 이 탭에 나타납니다."
            )
        except Exception as exc:
            st.caption(f"업데이트 기록 표시 실패: {type(exc).__name__}: {exc}")

    st.divider()
    if render_session_footer is not None:
        try:
            render_session_footer()
        except Exception:
            pass
    st.caption(DISCLAIMER)
    st.caption("방문 통계는 중복 집계를 줄이기 위해 IP를 네트워크 단위로 축약하고 비가역 식별값으로 처리합니다.")


if __name__ == "__main__":
    main()
