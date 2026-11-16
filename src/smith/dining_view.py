"""How a restaurant pick reads (FR-55): one dict form for the CLI, the evening mail, the cache and the counsel letter's
facts, and the Korean lines made from it. Every word here is code's; no model writes a pick's figures.
"""
from typing import Any

from smith import dining, naver_place

BROTH_NOTE = ("일식 국물과 토핑(우동·소바·라멘 국물, 가쓰오부시)은 대개 생선 육수입니다. 이런 곳은 주문할 때 꼭 확인하세요.")
FISH_NOTE = "육수나 소스에 든 생선은 메뉴 이름으로 알 수 없으니 주문할 때 꼭 확인하세요."
NO_RULES_NOTE = "건강 원장에 음식 제약이 없어 못 드시는 음식을 거르지 않았습니다."
CROSS_NOTES = {"google-quota": "구글 교차 확인: 이번 달 무료 한도에 닿아 멈췄습니다",
               "google-cap": "구글 교차 확인: 이번 달 무료 한도에 닿아 멈췄습니다",
               "google-key-rejected": "구글 교차 확인: 키가 거절됐습니다(`smith dining google-login`으로 다시 넣으세요)",
               "google-unreachable": "구글 교차 확인: 연결하지 못했습니다",
               "google-log-unreadable": "구글 교차 확인: 사용량 기록을 읽거나 쓰지 못해 멈췄습니다",
               "google-keyring": "구글 교차 확인: 자격 증명 저장소를 읽지 못했습니다"}


def to_dict(outcome: dining.Outcome) -> dict[str, Any]:
    """The outcome as plain data (JSON-safe): what the evening cache keeps and the mail renders."""
    return {"query": outcome.query, "total": outcome.total, "listed": outcome.listed, "ruled_out": outcome.ruled_out,
            "unread": outcome.unread, "rules_known": outcome.rules_known, "cross_note": outcome.cross_note,
            "picks": [pick_dict(p) for p in outcome.picks]}


def pick_dict(p: dining.Pick) -> dict[str, Any]:
    place = p.place
    average = p.stars_average
    cross = None if p.cross is None else {
        "source": p.cross.source, "rating": None if p.cross.rating is None else str(p.cross.rating),
        "count": p.cross.count, "url": p.cross.url, "lower": p.cross.lower_than(average)}
    visit = None if p.visit is None else {"on": p.visit.on.isoformat(), "rating": p.visit.rating, "note": p.visit.note}
    return {"id": place.id, "name": place.name, "category": place.category, "area": place.area,
            "score": round(p.score, 4), "voters": p.voters, "tasty": p.tasty, "taste_share": p.taste_share,
            "rating": None if average is None else str(average), "rated": p.rated, "low": p.low, "split": p.split,
            "stars_hidden": p.detail.stars_hidden, "visitor_reviews": place.visitor_reviews, "typical_price": p.typical_price,
            "signature": [{"name": m.name, "price": m.price} for m in p.signature], "avoid": list(p.avoid),
            "highlights": [name for name, _ in p.highlights], "broth": p.broth, "cross": cross, "visit": visit,
            "michelin": bool(place.michelin), "broadcasts": place.broadcasts, "booking": place.booking,
            "status": place.status, "link": naver_place.link(place.id)}


def facts(d: dict[str, Any]) -> list[str]:
    """The evidence in short phrases: the taste vote, the stars and their bad end, Google's word."""
    out = []
    if d.get("taste_share") is not None:
        out.append(f"\"{dining.TASTE}\" {pct(d['taste_share'])} (키워드를 고른 리뷰 {d['voters']:,}건)")
    if d.get("rated"):
        out.append(f"별점 {d['rating']} · 1~2점 {pct(d['low'] / d['rated'])} ({d['rated']:,}건 중 {d['low']:,}건)"
                   if d.get("rating") else f"1~2점 {pct(d['low'] / d['rated'])} (별점 {d['rated']:,}건 중 {d['low']:,}건)")
    elif d.get("stars_hidden"):
        out.append("별점 비공개(사장님 설정)")
    elif d.get("rating"):
        out.append(f"별점 {d['rating']}")
    cross = d.get("cross")
    if cross and cross.get("rating"):
        out.append(f"{cross['source']} {cross['rating']} (리뷰 {cross.get('count') or 0:,}건)")
    return out


def pick_lines(n: int, d: dict[str, Any], *, show_status: bool = True) -> list[str]:
    """One pick as lines: the head, the evidence, the warnings, the dishes, the record, the link. `show_status` off
    for a mail about tomorrow (Naver's 영업 중 is about the moment of reading)."""
    head = f"{n}. {d['name']} · {d['category']}" + (f" · {d['area']}" if d.get("area") else "")
    lines = [head, "   " + " · ".join(facts(d))]
    warnings = []
    if d.get("split"):
        warnings.append(f"평이 갈리는 편: 별점 1~2점이 {pct(d['low'] / d['rated'])}입니다")
    cross = d.get("cross") or {}
    if cross.get("lower"):
        warnings.append(f"{cross['source']} 평점이 네이버보다 확연히 낮습니다")
    lines += [f"   주의: {w}" for w in warnings]
    if d.get("signature"):
        label = "드실 메뉴" if d.get("avoid") else "대표 메뉴"
        lines.append(f"   {label}: " + ", ".join(m["name"] + (f" {m['price']:,}원" if m.get("price") else "") for m in d["signature"]))
    if d.get("avoid"):
        lines.append("   본인은 피할 메뉴: " + ", ".join(d["avoid"]))
    if d.get("highlights"):
        lines.append("   많이 꼽힌 점: " + ", ".join(d["highlights"]))
    visit = d.get("visit")
    if visit:
        lines.append(f"   지난번 {visit['on'][5:].replace('-', '/')} 다녀옴 · 두 분 평가 {visit['rating']}점"
                     + (f" ({visit['note']})" if visit.get("note") else ""))
    extra = [x for x in (d.get("michelin") and "미쉐린 가이드", d.get("broadcasts") and f"방송 {d['broadcasts']}회",
                         d.get("booking") and "네이버 예약 가능", show_status and d.get("status")) if x]
    if extra:
        lines.append("   " + " · ".join(extra))
    lines.append("   " + d["link"])
    return lines


def notes(result: dict[str, Any]) -> list[str]:
    """The closing cautions: fish in broths (stronger for Japanese kitchens), or that nothing was filtered."""
    if not result.get("rules_known"):
        return [NO_RULES_NOTE]
    out = [BROTH_NOTE] if any(p.get("broth") for p in result.get("picks") or []) else []
    return out + [FISH_NOTE]


def pct(share: float) -> str:
    return "1% 미만" if 0 < share < 0.005 else f"{share:.0%}"
