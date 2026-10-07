"""`smith report compare`: the same report written by the single narrative and by the team, side by side.

Both reports are built from one ledger snapshot (the same instant and the same pinned research brief),
so the only difference is how the narrative is written. Nothing is sent. The pages are saved under
`reports/`, which Git ignores because they hold real figures.
"""
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from smith import ledger, narrative
from smith.config import local_time
from smith.report_data import build_report
from smith.report_html import render

LABELS = {"single": "단일 에이전트", "team": "전문가 팀"}
MEMO = "자동 점검 메모"
SEVERITY = {"high": "높음", "medium": "중간", "low": "낮음"}  # team.SEVERITIES, normalized by code


def run(db: Path, *, now: datetime, kind: str, tz: str, baseline: datetime | None, household: dict[str, Any] | None,
        brief_id: str | None, secrets: list[str], out_dir: Path,
        narrate: Callable[..., None] = narrative.narrate) -> Path:
    """Write both reports and the comparison page into `out_dir`; return the page's path.

    Without `brief_id`, the brief the single narrative would use is pinned for both.
    """
    with closing(ledger.connect_read_only(db)) as conn:
        reports = {p: build_report(conn, as_of=now, known_at=now, baseline=baseline, kind=kind, tz=tz,
                                   household=household) for p in narrative.PIPELINES}
    if brief_id is None:
        brief, _ = narrative.pinned_brief(db, reports["single"]["view"], now=now, brief_id=None)
        brief_id = None if brief is None else brief.get("brief_id")

    def write(pipeline: str) -> float:
        started = time.monotonic()
        data = reports[pipeline]
        narrate(db, data["view"], data, now=now, secrets=secrets, brief_id=brief_id, pipeline=pipeline)
        return time.monotonic() - started
    with ThreadPoolExecutor(max_workers=len(narrative.PIPELINES)) as pool:
        seconds = dict(zip(narrative.PIPELINES, pool.map(write, narrative.PIPELINES)))
    out_dir.mkdir(parents=True, exist_ok=True)
    memos = {}
    for pipeline, data in reports.items():
        _, html = render(data)
        (out_dir / f"{pipeline}.html").write_text(html, encoding="utf-8")
        memos[pipeline] = html.count(MEMO)
    page = out_dir / "index.html"
    page.write_text(comparison_page(reports, seconds, memos, local_time(now, tz)), encoding="utf-8")
    return page


def comparison_page(reports: dict[str, dict[str, Any]], seconds: dict[str, float], memos: dict[str, int],
                    at: datetime) -> str:
    """Summary table, the two reports side by side, and what each team specialist found."""
    rows = "".join(_row(p, reports[p]["narrative"]["status"], seconds[p], memos[p]) for p in reports)
    frames = "".join(f'<section><h2>{LABELS[p]}</h2><iframe src="{p}.html" title="{LABELS[p]} 보고서"></iframe>'
                     f'<a href="{p}.html" target="_blank">새 창에서 열기</a></section>' for p in reports)
    specialists = reports.get("team", {}).get("narrative", {}).get("status", {}).get("specialists", [])
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>보고서 비교 {at:%m-%d %H:%M}</title>
<style>{_STYLE}</style></head><body><main>
<h1>보고서 비교: 단일 에이전트 vs 전문가 팀</h1>
<p class="lede">{at:%Y-%m-%d %H:%M} 원장 시점과 같은 조사 브리프로 만든 두 보고서입니다. 계산·제안·화면은 같고, 서술을 쓰는 방식만
다릅니다. 메일로 보내지 않았습니다.</p>
<div class="scroll"><table><thead><tr><th>방식</th><th>결과</th><th>모델 호출</th><th>걸린 시간</th><th>비용(참고)</th>
<th>자동 점검 메모</th></tr></thead><tbody>{rows}</tbody></table></div>
<details open><summary>비교할 때 볼 것</summary><ol>
<li>맨 위 판단(지금 상황과 방향)이 더 정확하고 날카로운가</li>
<li>분야 사이 충돌(예: 공격적 투자 대 보증금 반환 대비 현금)을 분명히 정리했는가</li>
<li>제안 순서와 덧붙인 맥락이 납득되는가</li>
<li>인사이트와 "지켜볼 것"이 우리 집 숫자와 구체적으로 이어지는가</li>
<li>자동 점검 메모(근거가 확인되지 않은 부분)가 적은가</li></ol></details>
<div class="pair">{frames}</div>
<h2>전문가 팀이 편집자에게 넘긴 분석</h2>
<p class="lede">보고서에는 편집자가 고른 내용만 들어갑니다. 아래는 전문가 다섯이 각자 분야만 보고 남긴 메모입니다.</p>
{''.join(_specialist(s) for s in specialists) or '<p>전문가 단계가 실행되지 않았습니다.</p>'}
</main></body></html>"""


def _row(pipeline: str, status: dict[str, Any], seconds: float, memos: int) -> str:
    outcome = "성공" if status.get("outcome") == "success" else f"실패({escape(str(status.get('error_code')))})"
    cost = status.get("cost_usd")
    return (f"<tr><td>{LABELS[pipeline]}</td><td>{outcome}</td><td>{len(status.get('run_ids', []))}회</td>"
            f"<td>{seconds / 60:.1f}분</td><td>{'미상' if cost is None else f'${escape(str(cost))}'}</td>"
            f"<td>{memos}건</td></tr>")


def _specialist(item: dict[str, Any]) -> str:
    if item["status"] != "success":
        return (f'<section class="area"><h3>{escape(item["title"])}</h3>'
                f'<p class="fail">실패: {escape(str(item["error_code"]))}</p></section>')
    findings = "".join(
        f'<li><span class="sev {f["severity"]}">{SEVERITY[f["severity"]]}</span>'
        f'<b>{escape(f["headline"])}</b><p>{escape(f["what"])}</p><p>{escape(f["why_it_matters"])}</p>'
        f'<p class="so">→ {escape(f["so_what"])}</p>'
        + (f'<p class="fail">코드 점검 실패: {escape(f["check_failed"])}</p>' if f.get("check_failed") else "")
        + "</li>" for f in item["findings"])
    extras = "".join(f"<p class='note'><b>{label}</b> {escape(text)}</p>"
                     for label, key in (("분야 간 충돌:", "cross_sector"), ("부족한 자료:", "data_gaps"))
                     for text in item[key])
    return f'<section class="area"><h3>{escape(item["title"])}</h3><ol class="findings">{findings}</ol>{extras}</section>'


_STYLE = """
:root{--ink:#0a0b0d;--muted:#5b616e;--line:#dee1e6;--panel:#f4f6f9;--accent:#0052ff;--high:#b4460b;--paper:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);padding-inline:20px;
font-family:"Pretendard","Malgun Gothic","Apple SD Gothic Neo",sans-serif;font-size:15px;line-height:1.65;word-break:keep-all}
main{max-width:1500px;margin:0 auto;padding-block:32px 64px;display:grid;gap:20px}
h1{font-size:26px;margin:0}h2{font-size:18px;margin:8px 0 0}h3{font-size:16px;margin:0 0 8px}
.lede{color:var(--muted);margin:0;max-width:72ch}.scroll{overflow-x:auto}
table{border-collapse:collapse;min-width:620px}th,td{text-align:left;padding:8px 14px;border-bottom:1px solid var(--line)}
th{font-size:12px;color:var(--muted);background:var(--panel)}
details{background:var(--panel);border-radius:8px;padding:10px 16px}summary{font-weight:600;cursor:pointer}
.pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,640px),1fr));gap:20px}
.pair section{display:grid;gap:6px}iframe{width:100%;height:80vh;border:1px solid var(--line);border-radius:8px}
.area{border:1px solid var(--line);border-radius:8px;padding:14px 18px}
.findings{margin:0;padding-left:1.2em;display:grid;gap:12px}.findings p{margin:2px 0;color:var(--muted)}
.so{color:var(--ink)!important}.note{margin:6px 0 0;color:var(--muted)}.fail{color:var(--high)!important}
.sev{font-size:11px;font-weight:600;border-radius:4px;padding:1px 6px;margin-right:6px;background:var(--panel)}
.sev.high{color:var(--high)}a{color:var(--accent)}
"""
