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
import json
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

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
