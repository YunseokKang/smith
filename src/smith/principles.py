"""Investment principles and the household's investment policy (docs/report-design.md §13).

The library paraphrases principles of well-known investors from primary sources their authors published
for free (shareholder letters, memos, the publisher's free online edition, the firm's own pages). Each
entry cites its documents, their locators and URLs; no book text is stored. Paraphrases are Smith's
wording, reviewed by people; they change only through a code change, never through model output.

From the library code derives the household's investment policy: a few checks with thresholds that are
explicit assumptions (core index share, single-company limit, goal money out of stocks, reserve first, no
borrowing to hold stocks). The narrative stage grounds its strategy and holding opinions in these
principles and cites their ids; code verifies the ids.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from smith.fmt import percent, short_won
from smith.holdings import CONCENTRATION_LIMIT, KIND_LABELS, kind  # noqa: F401 - part of this module's API

CORE_MIN = Decimal("0.5")  # Assumption: at least half of the stocks in broad, low-cost index funds (core).
# Debts that are neither housing nor dated obligations: holding stocks while carrying them is close to
# owning stocks with borrowed money.
UNSECURED_DEBT = ("credit_loan", "credit_line", "card_balance", "policy_loan", "margin_loan")


@dataclass(frozen=True)
class Principle:
    id: str
    investor: str
    title: str        # Short Korean name shown in the report.
    principle: str    # Smith's Korean paraphrase of the sources.
    apply: str        # How Smith applies it to one household's holdings.
    sources: tuple[tuple[str, str], ...]  # (document and locator, URL), the main source first.

    @property
    def url(self) -> str:
        return self.sources[0][1]


_B = "https://www.berkshirehathaway.com/letters/"
_B1987 = ("Berkshire Hathaway 1987 주주서한 'Marketable Securities'", _B + "1987.html")
_B1993 = ("Berkshire Hathaway 1993 주주서한 'Common Stock Investments'", _B + "1993.html")
_B1996 = ("Berkshire Hathaway 1996 주주서한, 개인 투자에 관한 생각", _B + "1996.html")
_B2013 = "Berkshire Hathaway 2013 주주서한 'Some Thoughts About Investing' "
_B2017 = "Berkshire Hathaway 2017 주주서한 "
_MUNGER = ("Poor Charlie's Almanack(Stripe Press 무료 온라인판) 3장", "https://www.stripe.press/poor-charlies-almanack/book")
_MARKS_2003 = ("Howard Marks, Oaktree 메모 'The Most Important Thing'(2003-07-01)",
               "https://www.oaktreecapital.com/docs/default-source/memos/2003-07-01-the-most-important-thing.pdf")
_VANGUARD = ("Vanguard, Principles for Investing Success(목표·균형·비용·규율)",
             "https://corporate.vanguard.com/content/corporatesite/us/en/corp/about-our-funds/how-we-invest/"
             "principles-for-investing-success.html")

LIBRARY: tuple[Principle, ...] = (
    Principle("BUF-INDEX", "워런 버핏", "비전문가는 저비용 지수펀드",
              "대부분의 투자자에게 주식을 가장 잘 보유하는 방법은 수수료가 아주 낮은 지수펀드입니다. 비전문가의 목표는 "
              "이길 종목을 고르는 것이 아니라 전체로 보아 잘될 수밖에 없는 기업들의 단면을 갖는 것입니다.",
              "주식의 핵심(코어)은 넓은 시장 지수펀드로 두고, 개별 종목은 이해하는 범위에서만 더합니다.",
              (_B1996, (_B2013 + "19~20쪽", _B + "2013ltr.pdf"))),
    Principle("BUF-9010", "워런 버핏", "단기 국채 10%, S&P 500 지수 90%",
              "버핏은 아내를 위한 신탁에 현금의 10%는 단기 국채, 90%는 아주 저비용 S&P 500 지수펀드에 두라고 유언에 "
              "적었습니다. 고비용 운용자를 쓰는 대부분의 투자자보다 장기 결과가 나을 것이라 보았습니다.",
              "장기 자금의 기본 구조를 '아주 단순한 지수 중심'으로 잡는 기준점으로 씁니다.",
              ((_B2013 + "20쪽", _B + "2013ltr.pdf"),)),
    Principle("BUF-CIRCLE", "워런 버핏", "능력 범위",
              "자기 능력 범위 안의 회사만 평가할 수 있으면 됩니다. 범위의 크기보다 그 경계를 아는 것이 결정적입니다. "
              "5년 뒤 이익의 범위를 대략 추정할 수 없으면 그냥 넘어갑니다.",
              "개별 종목마다 '이 회사의 5년 뒤 이익을 대략이라도 말할 수 있는가'를 묻고, 아니면 비중을 키우지 않습니다.",
              (_B1996, (_B2013 + "19쪽", _B + "2013ltr.pdf"))),
    Principle("BUF-KNOW-SOMETHING", "워런 버핏", "아는 투자자의 집중",
              "사업의 경제성을 이해하고 장기 경쟁우위를 가진 합리적 가격의 회사를 5~10개 찾을 수 있는 투자자에게 통상의 "
              "분산은 의미가 없습니다. 위험은 변동성이 아니라 세후 구매력을 잃을 가능성입니다.",
              "집중은 '깊이 이해하는 회사'일 때만 정당하다는 조건으로 씁니다. 이해한다는 근거가 없으면 지수 쪽을 기본으로 "
              "두는 것은 버핏의 단일 원칙이 아니라 Smith의 정책 해석입니다(BUF-INDEX와 함께 읽음).",
              (_B1993,)),
    Principle("BUF-RATIONAL-PRICE", "워런 버핏", "합리적 가격의 이해하기 쉬운 사업",
              "투자자의 목표는 5년, 10년, 20년 뒤 이익이 지금보다 크게 높을 것이 거의 확실한 이해하기 쉬운 사업의 일부를 "
              "합리적 가격에 사는 것입니다. 10년 보유할 생각이 없다면 10분도 보유하지 마십시오.",
              "추가 매수 의견은 장기 이익 성장의 근거와 가격(밸류에이션)이 함께 있을 때만 냅니다. 단기 시세 전망은 근거가 아닙니다.",
              (_B1996,)),
    Principle("BUF-MR-MARKET", "워런 버핏", "미스터 마켓과 공포",
              "시장 가격은 이용할 대상이지 길잡이가 아닙니다. 투자할 때 공포의 분위기는 친구이고 들뜬 분위기는 적입니다. "
              "최근에 올랐다는 사실은 결코 살 이유가 아닙니다.",
              "많이 오른 종목을 그 이유만으로 더 사거나, 떨어졌다는 이유만으로 팔라는 의견을 내지 않습니다.",
              (_B1987, (_B2013 + "18~19쪽", _B + "2013ltr.pdf"))),
    Principle("BUF-NO-MACRO", "워런 버핏", "거시 예측에 기대지 않기",
              "거시 전망을 세우거나 남의 시장 전망을 듣는 것은 시간 낭비이며, 정말 중요한 사실을 흐리게 해 위험하기까지 합니다.",
              "금리·환율 전망만으로 종목을 사고팔라는 의견을 내지 않습니다. 거시 변화는 우리 집 숫자에 닿는 경로로만 씁니다.",
              ((_B2013 + "18쪽", _B + "2013ltr.pdf"),)),
    Principle("BUF-DCA", "워런 버핏", "오래 나눠 사고, 나쁜 뉴스에 팔지 않기",
              "들뜬 시기에 한꺼번에 들어갔다가 손실에 실망하는 것이 가장 큰 위험입니다. 오랜 기간 나눠 사고, 나쁜 뉴스로 "
              "주가가 고점보다 크게 떨어졌을 때 팔지 않으십시오. 분산하고 비용을 최소로 하면 만족스러운 결과가 거의 확실합니다.",
              "매수는 매월 여유 자금으로 나눠서, 매도는 계획(목표 자금 전환)에 따라 나눠서 권합니다.",
              ((_B2013 + "20쪽", _B + "2013ltr.pdf"),)),
    Principle("BUF-NO-LEVERAGE", "워런 버핏", "빚내서 주식 보유 금지",
              "버크셔 주가도 짧은 기간에 37~59% 떨어진 적이 네 번 있습니다. 주식이 단기간에 얼마나 떨어질지는 알 수 없으므로 "
              "빌린 돈으로 주식을 보유하지 말아야 합니다. 큰 하락은 빚에 묶이지 않은 사람에게 기회가 됩니다.",
              "신용대출 등 갚아야 할 빚이 있는 동안 주식을 늘리라는 의견을 내지 않고, 급락 때 팔지 않아도 되는 구조를 먼저 봅니다.",
              ((_B2017 + "10쪽", _B + "2017ltr.pdf"),)),
    Principle("BUF-FEES", "워런 버핏", "수수료는 반드시 남는다",
              "10년 내기에서 헤지펀드 묶음은 9년 연속 지수펀드에 뒤졌습니다. 성과는 오가지만 수수료는 사라지지 않습니다.",
              "같은 역할의 펀드라면 보수가 낮은 쪽을, 잦은 매매보다 보유를 권합니다.",
              ((_B2017 + "'The Bet' 11쪽", _B + "2017ltr.pdf"),)),
    Principle("BUF-HORIZON", "워런 버핏", "위험은 기간에 따라 다르다",
              "하루, 일주일, 1년 단위로 보면 주식은 단기 국채보다 훨씬 위험합니다. 투자 기간이 길어질수록 적정 가격에 산 분산된 "
              "주식은 채권보다 덜 위험해집니다. 위험은 나중에 쓰려던 목적을 이루지 못할 가능성입니다.",
              "몇 년 안에 날짜가 정해진 돈은 주식 밖에, 오래 둘 돈은 주식에 둡니다(목표 자금 전환 계획의 근거).",
              ((_B2017 + "12쪽", _B + "2017ltr.pdf"),)),
    Principle("MUN-INVERT", "찰리 멍거", "거꾸로 생각하기",
              "앞으로도 뒤로도 생각하십시오. 항상 거꾸로 뒤집어 보십시오. 무엇이 일을 망칠지 먼저 따져 피하는 것이 출발점입니다.",
              "제안마다 '무엇이 이 계획을 실패하게 하나(급락, 급한 지출, 세금)'를 먼저 점검합니다.",
              (_MUNGER,)),
    Principle("MUN-CHECKLIST", "찰리 멍거", "체크리스트로 실수 줄이기",
              "과학적 방법과 효과적인 체크리스트가 실수와 누락을 줄입니다. 탁월함을 찾기보다 어리석음을 피하는 것이 중요합니다.",
              "투자 기준 점검표를 매번 같은 순서로 확인하고, 어긋난 항목부터 고칩니다.",
              (_MUNGER,)),
    Principle("MUN-SIT", "찰리 멍거", "오래 앉아 기다리는 투자",
              "좋은 회사를 오래 보유하면 중개 비용과 잡음이 줄고 세금에서도 해마다 몇 %포인트의 이점이 생깁니다. 훌륭한 사업을 "
              "적정 가격에 사는 것이 그저 그런 사업을 싼 가격에 사는 것보다 낫습니다.",
              "좋은 회사의 단기 매매를 권하지 않고, 매도 의견에는 세금과 거래 비용을 함께 적습니다.",
              (_MUNGER,)),
    Principle("MARKS-PRICE", "하워드 막스", "가격과 가치의 관계",
              "가장 중요한 것은 가격과 가치의 관계입니다. 어떤 자산도 높은 수익이 타고나지 않으며, 가격이 맞을 때만 매력적입니다. "
              "확신 없는 보유는 가격이 떨어질 때 팔게 만듭니다.",
              "좋은 회사라도 이미 높은 기대가 가격에 반영됐는지(밸류에이션)를 함께 봅니다.",
              (_MARKS_2003,)),
    Principle("MARKS-DEFENSIVE", "하워드 막스", "방어적으로 투자하기",
              "패자를 피하면 승자는 알아서 따라옵니다. 방어적 투자의 요소는 확인 가능한 가치를 싸게 사기, 집중보다 분산, "
              "거시 예측과 시장 타이밍에 기대지 않기입니다. 나쁜 해는 반드시 오므로 늘 방어적으로 투자합니다.",
              "한 종목 쏠림과 목표 자금의 주식 의존을 먼저 줄이고, 그다음에 수익을 늘릴 방법을 봅니다.",
              (_MARKS_2003,)),
    Principle("MARKS-CYCLES", "하워드 막스", "예측할 수 없지만 대비할 수 있다",
              "경기와 시장은 반드시 순환하고, 사람들은 과거를 잊습니다. 어디로 갈지는 알 수 없지만 지금 순환의 어디쯤인지는 "
              "가늠할 수 있고, 그에 맞춰 대비할 수 있습니다.",
              "시장 과열·공포 신호는 매매 타이밍이 아니라 대비(현금 여유, 분할 실행)의 근거로만 씁니다.",
              (("Howard Marks, Oaktree 메모 'You Can't Predict. You Can Prepare.'(2001-11-20)",
                "https://www.oaktreecapital.com/docs/default-source/memos/2001-11-20-you-cant-predict-you-can-prepare.pdf"),
               _MARKS_2003)),
    Principle("VAN-GOALS", "뱅가드(존 보글)", "분명한 목표",
              "투자자 자신의 상황과 선호에 맞는 분명하고 적절한 투자 목표를 세우는 것이 성공의 첫 원칙입니다.",
              "모든 의견을 우리 집 목표(비상금, 보증금 반환, 주택 이전, 노후)에 연결해 씁니다.",
              (_VANGUARD,)),
    Principle("VAN-BALANCE", "뱅가드(존 보글)", "균형과 분산, 낮은 비용, 규율",
              "주식과 채권, 업종과 국가에 나눠 담아 변동성과 불필요하게 큰 손실을 줄이고, 비용을 최소화하며, 장기적 관점과 "
              "규율을 지킵니다.",
              "업종·국가 쏠림을 점검하고, 같은 효과라면 비용이 낮은 수단을 권합니다.",
              (_VANGUARD,)),
    Principle("DALIO-BALANCE", "레이 달리오", "모든 날씨에 대비한 균형",
              "미래를 모른다는 사실을 받아들이고, 성장과 물가가 기대보다 오르거나 내리는 네 가지 환경 각각에서 잘 되는 자산에 "
              "위험을 고르게 나눕니다. 그 밖의 것은 미래를 예측하려는 시도입니다.",
              "포트폴리오가 한 가지 경제 환경(예: 기술주 호황)에만 기대고 있지 않은지 봅니다.",
              (("Bridgewater Associates, 'The All Weather Story'(2012)",
                "https://www.bridgewater.com/research-and-insights/the-all-weather-story"),)),
    Principle("LYNCH-STORY", "피터 린치", "보유 이유(스토리)를 알기",
              "주식을 갖고 있다면 그 회사의 이야기를 알아야 합니다. 경제 예측에 1년에 13분 넘게 쓰면 10분은 낭비입니다. "
              "예측이 아니라 사실을 다룹니다.",
              "종목마다 보유 이유를 한 문장으로 말할 수 있는지 보고, 말할 수 없으면 비중 축소를 검토합니다.",
              (("PBS FRONTLINE 'Betting on the Market' Peter Lynch 인터뷰(1996)",
                "https://www.pbs.org/wgbh/pages/frontline/shows/betting/pros/lynch.html"),)),
)
BY_ID = {p.id: p for p in LIBRARY}

@dataclass(frozen=True)
class Check:
    """One line of the household's investment policy: the standard, where the household is, the verdict."""
    key: str
    title: str
    standard: str
    current: str
    status: str            # ok, breach, plan (a scheduled fix), watch, unknown
    principles: tuple[str, ...]


def policy(data: dict[str, Any]) -> list[Check]:
    """The investment policy checks for this report, in the order the client should fix them."""
    review = data["holdings"]
    positions = [p for p in review["positions"] if not p["hermes"]]
    if not positions and not review["unlisted"]:
        return []
    return [_reserve(data), _leverage(data), _dated(data), _single(review, positions), _core(review, positions)]


def unsecured_debt(data: dict[str, Any]) -> Decimal | None:
    """Outstanding unsecured debt (credit loans, credit lines, card balances); unknown if any is unconverted."""
    amounts = [l["amount"] for l in data["sectors"]["debt"]["loans"] if l["category"] in UNSECURED_DEBT]
    return None if None in amounts else sum(amounts, Decimal(0))


def core_short(data: dict[str, Any]) -> bool:
    """True when the broad-index core is below its standard or cannot be shown to meet it."""
    return any(c.key == "core" and c.status != "ok" for c in data.get("policy", []))


def _reserve(data: dict[str, Any]) -> Check:
    cash = data["sectors"]["cash"]
    # Without recorded spending the target is zero, which says nothing about the reserve.
    gap = None if not cash["reserve_target"] else cash["reserve_gap"]
    status = "unknown" if gap is None else "ok" if gap == 0 else "breach"
    current = "미상(월 지출 기록 필요)" if gap is None else "충족" if gap == 0 else f"{short_won(gap)} 부족"
    return Check("reserve", "비상금이 먼저", f"월 지출 {cash['emergency_months_assumption']}개월분(가정)",
                 current, status, ("MUN-INVERT", "MARKS-DEFENSIVE", "BUF-NO-LEVERAGE"))


def _leverage(data: dict[str, Any]) -> Check:
    total = unsecured_debt(data)
    status = "unknown" if total is None else "ok" if total == 0 else "watch"
    current = "미상" if total is None else "없음" if total == 0 else f"신용성 대출 {short_won(total)}"
    return Check("leverage", "빚으로 주식을 들고 있지 않기", "신용대출·마이너스통장이 있으면 주식을 늘리지 않음", current, status,
                 ("BUF-NO-LEVERAGE",))


def _dated(data: dict[str, Any]) -> Check:
    glide = data["advice"].get("glide")
    standard = "3년 안에 날짜가 정해진 돈은 주식 밖에"
    principles = ("BUF-HORIZON", "MARKS-DEFENSIVE", "VAN-GOALS")
    if glide is None:
        return Check("dated", "목표 자금은 주식 밖에", standard, "주식에 기대지 않음", "ok", principles)
    if not glide["steps"]:
        return Check("dated", "목표 자금은 주식 밖에", standard, "미상(환산하지 못한 금액)", "unknown", principles)
    first = glide["steps"][0]
    current = f"{first['due']:%Y.%m} {first['label']} 자금 중 {short_won(glide['earmarked'])}이 주식에 있음"
    return Check("dated", "목표 자금은 주식 밖에", standard,
                 current + ("" if first["started"] else f"({first['start']:%Y.%m}부터 옮길 계획)"),
                 "breach" if first["started"] else "plan", principles)


def _single(review: dict[str, Any], positions: list[dict[str, Any]]) -> Check:
    """Shares are of all the securities, listed or not. A security without a symbol could itself be one
    company above the limit, so with such an amount above the limit the verdict cannot be "ok"."""
    total, unlisted = review["total"], review["unlisted"]
    over = [p for p in positions if kind(p) == "stock" and p["share"] is not None and p["share"] > CONCENTRATION_LIMIT]
    if total is None:
        status, current = "unknown", "미상"
    elif over:
        status, current = "breach", ", ".join(f"{p['name']} {percent(p['share'], 1)}" for p in over)
    elif total and unlisted / total > CONCENTRATION_LIMIT:
        status, current = "unknown", f"구성을 모르는 증권이 {percent(unlisted / total, 1)}"
    else:
        status, current = "ok", "없음"
    return Check("single", "개별 종목 한도", f"한 회사가 주식의 {percent(CONCENTRATION_LIMIT, 0)} 이하(가정)", current,
                 status, ("BUF-CIRCLE", "BUF-KNOW-SOMETHING", "MARKS-DEFENSIVE", "VAN-BALANCE"))


def _core(review: dict[str, Any], positions: list[dict[str, Any]]) -> Check:
    """The broad-index share of all the securities. Securities of unknown make-up (no symbol) might be index
    funds: when only they could lift the share to the standard, the verdict is unknown, not met."""
    total, unlisted = review["total"], review["unlisted"]
    broad = [p["value"] for p in positions if kind(p) == "broad_index"]
    share = None if not total or None in broad else sum(broad, Decimal(0)) / total
    if share is None:
        status, current = "unknown", "미상"
    elif share >= CORE_MIN:
        status, current = "ok", percent(share, 1)
    elif share + unlisted / total >= CORE_MIN:
        status, current = "unknown", f"{percent(share, 1)}(구성을 모르는 증권 {percent(unlisted / total, 1)} 제외)"
    else:
        status, current = "breach", percent(share, 1)
    return Check("core", "핵심은 넓은 저비용 지수펀드", f"주식의 {percent(CORE_MIN, 0)} 이상(가정)", current, status,
                 ("BUF-INDEX", "BUF-9010", "VAN-BALANCE", "BUF-FEES"))


def cited(ids: list[str]) -> list[Principle]:
    """Known principles among cited ids, in citation order without repeats; unknown ids are dropped."""
    return [BY_ID[i] for i in dict.fromkeys(ids) if i in BY_ID]
