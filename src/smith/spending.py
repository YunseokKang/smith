"""Card spending by category, month by month (FR-60, docs/spending-design.md).

`parse` turns one raw statement reading (`samsungcard.read`) into entries and refuses it unless every section adds up
to the total the page itself prints. `categorize` names a category from the merchant's name: public rules here, the
household's own shops in a local file (`data/spending/merchants.json`, never in Git). `summary` is the deterministic
picture the letter and the adviser read; no model computes an amount.

Amounts are whole won (int). `spent` is what the purchase cost before card discounts, points and vouchers; `billed` is
what the statement charges. A purchase settled early (선결제) is billed 0 but still spent.
"""
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

CATEGORIES = {
    "cafe": "커피·간식", "dining": "식비·외식", "drinks": "술·유흥", "grocery": "장보기·생활용품",
    "transport": "교통", "culture": "문화·책", "learning": "배움·자기계발", "subscription": "구독·디지털",
    "shopping": "쇼핑",
    "health": "의료·건강", "fitness": "운동·몸 관리", "travel": "여행·숙박", "housing": "주거·세금·통신", "fee": "연회비·수수료", "other": "미분류",
}
# Recurring obligations: a habit review reads them apart from the spending one chooses day by day.
FIXED = frozenset({"housing", "fee"})
# Public names only (chains, apps, generic words), first match wins. A household's own shops go in the local file.
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fee", ("연회비", "바로알림서비스", "수수료")),
    ("housing", ("관리비", "지자체세입금", "지방세", "국세", "도시가스", "전기요금", "에스케이텔레콤", "케이티", "엘지유플러스")),
    ("subscription", ("넷플릭스", "ANTHROPIC", "OPENAI", "GUMROAD", "유튜브", "YOUTUBE", "APPLE.COM", "GOOGLE", "스포티파이",
                      "디즈니", "티빙", "웨이브", "멜론", "리드플렉스", "이지원인터넷")),
    ("transport", ("택시", "카카오모빌리티", "UBER", "버스타고", "코레일", "주식회사 에스알", "주유소", "하이패스", "티머니", "철도", "SRT", "주차")),
    ("travel", ("호텔", "HOTEL", "아고다", "대한항공", "아시아나", "항공", "AIRLINE", "에어비앤비", "AIRBNB", "야놀자", "여기어때",
                "리조트", "놀유니버스")),
    ("cafe", ("스타벅스", "커피", "카페", "CAFE", "COFFEE", "에스프레소", "베이글", "베이크", "파리바게뜨", "뚜레쥬르", "매머드",
              "투썸", "이디야", "폴바셋", "마카롱", "카롱")),
    ("culture", ("문고", "서점", "교보", "예스24", "알라딘", "미술관", "박물관", "과학관", "재단", "MUSEUM", "CGV", "메가박스",
                 "롯데시네마", "인터파크", "티켓", "미디어앤아트", "매일경제신문")),
    ("learning", ("대학", "학원", "CONFERENCE", "UDEMY", "COURSERA", "인프런", "클래스101")),
    ("grocery", ("쿠팡", "이마트", "홈플러스", "롯데마트", "코스트코", "마켓컬리", "컬리", "편의점", "GS25", "CU ", "세븐일레븐",
                 "TARGET", "다이소", "올리브영")),
    # Before health: a gym or a studio is not medical care (the household survey counts it under leisure).
    ("fitness", ("헬스", "필라테스", "요가", "피트니스", "FITNESS", "GYM", "크로스핏", "수영장", "스포츠센터", "마사지", "스파")),
    ("health", ("약국", "병원", "의원", "클리닉", "치과", "한의원", "안과", "피부과")),
    ("shopping", ("삼성전자", "아울렛", "백화점", "네이버페이", "무신사", "유니클로", "UNIQLO", "자라", "나이키", "뉴발란스", "페더럴익스프레스")),
    ("dining", ("김밥", "스시", "식당", "찌개", "돈가스", "피자", "파파존스", "버거", "IN-N-OUT", "HABIT", "치킨", "국밥", "고기",
                "식육", "갈비", "곱창", "냉면", "웰스토리", "아워홈", "프레시웨이", "맥도날드", "롯데리아", "써브웨이", "쇼핑 ")),
)
# The card company's own industry label on an overseas purchase, when the name says nothing.
_OVERSEAS = (("택시", "transport"), ("호텔", "travel"), ("패스트푸드", "dining"), ("음식", "dining"), ("식료품", "grocery"),
             ("소프트웨어", "subscription"), ("정기구독", "subscription"))
_DAY = re.compile(r"(\d{2})\.\s*(\d{1,2})\.\s*(\d{1,2})")
_USED = re.compile(r"이용금액 (-?[\d,]+)원")
_BENEFIT = re.compile(r"(청구할인|포인트사용|온누리상품권)\s*-([\d,]+)원")
_FOREIGN = re.compile(r"업종/국가 (\S+?)/(\S+) 현지 이용금액 ([\d,.]+ [A-Z]{3})")
_OVERSEAS_KEY = "DE"   # the overseas detail repeats rows of the purchase sections; it is read for detail only
_FEE_KEY = "D5"
_PURCHASE_KEY = "D0"   # 일시불: every statement read so far has it
_REFUND_KEY = "DF"     # refunds of earlier months: the one section the page gives no total
_SMALL = 10_000
_LARGE = 50_000   # a purchase the reader would remember: counted apart inside its area


class SpendingError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Entry:
    day: date
    merchant: str
    spent: int
    billed: int
    benefits: tuple[tuple[str, int], ...] = ()   # (청구할인 | 포인트사용 | 온누리상품권, won)
    prepaid: bool = False
    fee: bool = False
    foreign: str = ""        # "220 USD"
    industry: str = ""       # the card company's label, overseas purchases only


@dataclass(frozen=True)
class Statement:
    settled: date
    start: date
    end: date
    entries: tuple[Entry, ...]

    @property
    def label(self) -> str:
        return f"{self.start.month}/{self.start.day}~{self.end.month}/{self.end.day}"


def parse(raw: dict[str, Any]) -> Statement:
    """One raw reading as entries. Raises SpendingError when the page's shape or its own totals do not hold."""
    days = [_day(part) for part in str(raw.get("period", "")).split("~")]
    settled = str(raw.get("settled", ""))
    if len(days) != 2 or None in days or not re.fullmatch(r"\d{8}", settled):
        raise SpendingError("statement-shape")
    sections = {str(s.get("key")): s for s in raw.get("sections", []) if isinstance(s, dict)}
    foreign = {(row.get("date"), row.get("merchant"), row.get("amount")): _FOREIGN.search(str(row.get("detail", "")))
               for row in sections.get(_OVERSEAS_KEY, {}).get("rows", [])}
    # A page that rendered no purchase section (slow load, changed markup) is not a month without spending, and a fee
    # section alone is such a page. A month truly without a purchase is refused too: it stays visible as a failed
    # sync instead of a month of zero.
    if _PURCHASE_KEY not in sections:
        raise SpendingError("statement-shape")
    entries: list[Entry] = []
    for key, section in sections.items():
        if key == _OVERSEAS_KEY:
            continue
        found = _section(section.get("rows", []), fee=key == _FEE_KEY, foreign=foreign)
        total = _won(section.get("total"))
        if total is None and key != _REFUND_KEY:
            raise SpendingError("total-missing")   # the rows cannot be checked, so they are not kept
        if total is not None and total != sum(entry.billed for entry in found):
            raise SpendingError("totals-mismatch")
        entries += found
    return Statement(date(int(settled[:4]), int(settled[4:6]), int(settled[6:])), days[0], days[1],
                     tuple(sorted(entries, key=lambda e: e.day)))


def _section(rows: list[dict[str, Any]], *, fee: bool, foreign: dict[tuple, re.Match | None]) -> list[Entry]:
    entries: list[Entry] = []
    for row in rows:
        amount, day = _won(row.get("amount")), _day(str(row.get("date", "")))
        if amount is None:
            raise SpendingError("statement-shape")
        if day is None:
            # "미리입금/할인/포인트자동납부 금액 등": takes the row above off the bill (paid early or covered).
            # It belongs to the last purchase, which a refund row may have followed.
            at = next((i for i in range(len(entries) - 1, -1, -1) if entries[i].spent > 0), None)
            if at is None or amount > 0:
                raise SpendingError("statement-shape")
            entries[at] = replace(entries[at], billed=entries[at].billed + amount, prepaid=True)
            continue
        detail = str(row.get("detail", ""))
        used = _USED.search(detail)
        spent = _won(used.group(1)) if used and amount >= 0 else amount
        abroad = foreign.get((row.get("date"), row.get("merchant"), row.get("amount")))
        entries.append(Entry(day, str(row.get("merchant", "")).strip(), spent if spent is not None else amount, amount,
                             tuple((kind, int(won.replace(",", ""))) for kind, won in _BENEFIT.findall(detail)),
                             fee=fee, foreign=abroad.group(3) if abroad else "",
                             industry=abroad.group(1) if abroad else ""))
    return entries


def _day(text: str) -> date | None:
    found = _DAY.search(text)
    try:
        return date(2000 + int(found.group(1)), int(found.group(2)), int(found.group(3))) if found else None
    except ValueError:
        return None


def _won(text: Any) -> int | None:
    cleaned = str(text or "").replace(",", "").replace("원", "").strip()
    return int(cleaned) if re.fullmatch(r"-?\d+", cleaned) else None


def categorize(entry: Entry, own: dict[str, str]) -> str:
    """The category key of one entry: the household's own table, then the public rules, the overseas label, and last
    what a place search found for the exact name (`own` keys starting with "=", from `load_own`)."""
    if entry.fee:
        return "fee"
    name = entry.merchant.upper()
    for needle, category in own.items():
        if not needle.startswith("=") and needle.upper() in name and category in CATEGORIES:
            return category
    for category, needles in RULES:
        if any(needle.upper() in name for needle in needles):
            return category
    abroad = next((category for word, category in _OVERSEAS if word in entry.industry), None)
    return abroad or own.get("=" + entry.merchant) or "other"


def _table_file(path: Path) -> dict[str, Any]:
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return table if isinstance(table, dict) else {}


def load_own(folder: Path) -> dict[str, str]:
    """The household's merchant table ({"name fragment": "category" | {"category", "note"}}) and, under "=" + the
    exact name, what the place search found (`lookups.json`). Empty when neither file is readable."""
    own = {}
    for needle, value in _table_file(folder / "merchants.json").items():
        category = value.get("category") if isinstance(value, dict) else value
        if category in CATEGORIES:
            own[str(needle)] = str(category)
    for name, found in _table_file(folder / "lookups.json").items():
        if isinstance(found, dict) and found.get("category") in CATEGORIES:
            own["=" + str(name)] = found["category"]
    return own


def household_members(folder: Path) -> int | None:
    """How many people live in the household (context.json "household_members"), for choosing the public
    statistics' size group; None when it was never recorded."""
    members = _table_file(folder / "context.json").get("household_members")
    return members if isinstance(members, int) and not isinstance(members, bool) and 0 < members < 20 else None


def notes(folder: Path) -> list[str]:
    """What the client said about the spending: about a merchant (merchants.json "note") or in general (context.json)."""
    told = [f"{needle}: {value['note']}" for needle, value in _table_file(folder / "merchants.json").items()
            if isinstance(value, dict) and isinstance(value.get("note"), str)]
    general = _table_file(folder / "context.json").get("notes")
    return told + ([str(n) for n in general if isinstance(n, str)] if isinstance(general, list) else [])


# Google place types (Places API New) to a category, first match wins. Words are matched whole, so
# "barbecue_restaurant" is a restaurant and not a bar.
_PLACE_WORDS = (("drinks", {"bar", "pub", "izakaya", "brewery", "karaoke", "night", "wine"}),
                ("cafe", {"cafe", "coffee", "bakery", "dessert", "tea", "ice", "donut", "confectionery"}),
                ("dining", {"restaurant", "meal", "food"}),
                ("fitness", {"gym", "fitness", "yoga", "pilates", "sports", "massage", "spa", "sauna"}),
                ("health", {"doctor", "hospital", "pharmacy", "physiotherapist", "dentist", "clinic", "medical"}),
                ("culture", {"museum", "book", "gallery", "movie", "library", "theater", "performing"}),
                ("travel", {"lodging", "hotel", "travel", "airport"}),
                ("transport", {"gas", "parking", "taxi", "transit"}),
                ("grocery", {"supermarket", "grocery", "convenience", "market"}),
                ("shopping", {"store", "shopping"}))


def category_of_place(types: list[str]) -> str | None:
    """The category a place's Google types point to, the primary type first; None when they say nothing useful."""
    for kind in types:
        words = set(kind.split("_"))
        for category, needles in _PLACE_WORDS:
            if words & needles:
                return category
    return None


def looked_up(folder: Path) -> dict[str, Any]:
    return _table_file(folder / "lookups.json")


def save_lookup(folder: Path, name: str, found: dict[str, Any]) -> None:
    table = looked_up(folder)
    table[name] = found
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / "lookups.tmp"
    temporary.write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, folder / "lookups.json")


def save_raw(folder: Path, raw: dict[str, Any]) -> Path:
    """Store one verified raw reading under its payment day (atomic; the same day is replaced)."""
    target = folder / "samsungcard" / f"{raw['settled']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temporary, target)
    return target


def stored_days(folder: Path) -> frozenset[str]:
    return frozenset(path.stem for path in (folder / "samsungcard").glob("*.json") if re.fullmatch(r"\d{8}", path.stem))


def load(folder: Path, last: int = 0) -> list[Statement]:
    """Stored statements, oldest first (the newest `last` when given). A file that no longer parses is skipped."""
    statements = []
    for day in sorted(stored_days(folder)):
        try:
            statements.append(parse(json.loads((folder / "samsungcard" / f"{day}.json").read_text(encoding="utf-8"))))
        except (OSError, ValueError, SpendingError):
            continue
    return statements[-last:] if last else statements


def month(statement: Statement, own: dict[str, str]) -> dict[str, Any]:
    """One statement as numbers: totals, each category, benefits, rhythm of the week, and the largest purchases."""
    groups: dict[str, list[Entry]] = defaultdict(list)
    for entry in statement.entries:
        groups[categorize(entry, own)].append(entry)
    spent = sum(e.spent for e in statement.entries)
    chosen = [e for key, found in groups.items() if key not in FIXED for e in found if e.spent > 0]
    benefits: dict[str, int] = defaultdict(int)
    for entry in statement.entries:
        for kind, won in entry.benefits:
            benefits[kind] += won
    return {
        "label": statement.label, "settled": statement.settled.isoformat(), "days": (statement.end - statement.start).days + 1,
        "spent": spent, "billed": sum(e.billed for e in statement.entries),
        "prepaid": sum(e.spent for e in statement.entries if e.prepaid),
        "fixed": sum(e.spent for key in FIXED for e in groups.get(key, [])),
        "categories": {key: _group(found, spent) for key, found in sorted(groups.items(), key=lambda g: -sum(e.spent for e in g[1]))},
        "benefits": dict(benefits), "benefit_total": sum(benefits.values()),
        "weekend": _slice([e for e in chosen if e.day.weekday() >= 5]),
        "small": _slice([e for e in chosen if e.spent < _SMALL]),
        "overseas": _slice([e for e in statement.entries if e.foreign]),
        "largest": [_line(e, own) for e in sorted(chosen, key=lambda e: -e.spent)[:5]],
        "unsorted": sorted({e.merchant for e in groups.get("other", [])}),
    }


def _group(entries: list[Entry], whole: int) -> dict[str, Any]:
    amount = sum(e.spent for e in entries)
    merchants: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for entry in entries:
        merchants[entry.merchant][0] += entry.spent
        merchants[entry.merchant][1] += 1
    top = sorted(merchants.items(), key=lambda m: -m[1][0])[:4]
    return {"amount": amount, "count": sum(1 for e in entries if e.spent > 0), "share": round(amount / whole, 3) if whole > 0 else None,
            "merchants": [{"name": name, "amount": won, "count": count} for name, (won, count) in top]}


def _slice(entries: list[Entry]) -> dict[str, int]:
    return {"amount": sum(e.spent for e in entries), "count": len(entries)}


def _line(entry: Entry, own: dict[str, str]) -> dict[str, Any]:
    return {"day": entry.day.isoformat(), "merchant": entry.merchant, "amount": entry.spent,
            "category": categorize(entry, own)}


def _units(statement: Statement, own: dict[str, str]) -> dict[str, dict[str, int]]:
    """Per category, the purchases alone (refunds left out): how many, their sum, and the large ones among them."""
    found: dict[str, dict[str, int]] = defaultdict(lambda: {"amount": 0, "count": 0, "large_amount": 0, "large_count": 0})
    for entry in statement.entries:
        if entry.spent <= 0:
            continue
        unit = found[categorize(entry, own)]
        unit["amount"] += entry.spent
        unit["count"] += 1
        if entry.spent >= _LARGE:
            unit["large_amount"] += entry.spent
            unit["large_count"] += 1
    return found


def yardsticks(statements: list[Statement], own: dict[str, str], income: int | None = None) -> dict[str, Any] | None:
    """What the newest statement is judged against (FR-60, spending design §5.1): the reader's own earlier months,
    the price and pace of one purchase in each area, the large purchases inside it, and the share of the monthly
    income when the ledger has one. A verdict may quote these figures and no others, so nothing is added up by a model.

    Fixed costs and unsorted merchants have no area here: a habit is judged on what is chosen and known.
    None when nothing is stored; "usual" is absent with a single statement, "income" without an income.
    """
    if not statements:
        return None
    months, units = [month(s, own) for s in statements], [_units(s, own) for s in statements]
    latest, now, earlier = months[-1], units[-1], len(statements) - 1
    areas = []
    for key, group in latest["categories"].items():
        unit = now.get(key)
        if key in FIXED or key == "other" or not unit or not unit["count"]:
            continue
        area: dict[str, Any] = {
            "category": key, "amount": group["amount"], "count": unit["count"], "each": round(unit["amount"] / unit["count"]),
            "per_week": round(unit["count"] * 7 / max(latest["days"], 1), 1),
            "large": {"from": _LARGE, "count": unit["large_count"], "amount": unit["large_amount"]}}
        if earlier:
            before = [u[key] for u in units[:-1] if key in u]
            usual = round(sum(m["categories"].get(key, {}).get("amount", 0) for m in months[:-1]) / earlier)
            counted = sum(u["count"] for u in before)
            area["usual"] = {"months": earlier, "amount": usual, "count": round(counted / earlier, 1),
                             "each": round(sum(u["amount"] for u in before) / counted) if counted else None}
            area["over_usual"] = group["amount"] - usual
            area["over_usual_pct"] = round((group["amount"] - usual) * 100 / usual) if usual > 0 else None
        areas.append(area)
    result: dict[str, Any] = {"areas": areas, "months": [{"label": m["label"], "spent": m["spent"],
                                                         "chosen": m["spent"] - m["fixed"]} for m in months]}
    if income and income > 0:
        recent = months[-3:]
        result["income"] = {
            "share": round(latest["spent"] / income, 3), "chosen_share": round((latest["spent"] - latest["fixed"]) / income, 3),
            "without_paid_early_share": round((latest["spent"] - latest["prepaid"]) / income, 3),
            "average_share": round(sum(m["spent"] for m in recent) / len(recent) / income, 3), "average_months": len(recent)}
    return result


def summary(statements: list[Statement], own: dict[str, str]) -> dict[str, Any] | None:
    """The months side by side, newest last, with each category's average over the months before the newest.

    None when nothing is stored. `recurring` lists merchants charged in every stored month (three months or more),
    which is how subscriptions and standing habits show up.
    """
    if not statements:
        return None
    months = [month(statement, own) for statement in statements]
    earlier = months[:-1]
    usual = {key: round(sum(m["categories"].get(key, {}).get("amount", 0) for m in earlier) / len(earlier))
             for key in CATEGORIES} if earlier else {}
    seen: dict[str, list[int]] = defaultdict(list)
    for statement in statements:
        per: dict[str, int] = defaultdict(int)
        for entry in statement.entries:
            per[entry.merchant] += entry.spent
        for name, won in per.items():
            seen[name].append(won)
    recurring = [{"merchant": name, "category": categorize(Entry(statements[-1].end, name, 0, 0), own),
                  "monthly": round(sum(won) / len(won))}
                 for name, won in seen.items() if len(statements) >= 3 and len(won) == len(statements)]
    return {"months": months, "usual": {k: v for k, v in usual.items() if v},
            "recurring": sorted(recurring, key=lambda r: -r["monthly"])}
