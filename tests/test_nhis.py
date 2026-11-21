"""The retired household's health-insurance premium as regional subscribers (report design §50): figures by hand
from the 2026 rules (rate 7.19%, 211.5 won a property point, one 1억 deduction, long-term care 13.14% on top)."""
import unittest
from decimal import Decimal

from smith import nhis


class PremiumTests(unittest.TestCase):
    def test_property_points_follow_the_grades_after_one_deduction(self):
        # (property-tax base in won, points): nothing at or under 1억, a grade's upper bound is inclusive.
        cases = [(0, 0), (100_000_000, 0), (100_000_001, 22), (104_500_000, 22), (104_500_001, 44),
                 (581_000_000, 785), (600_000_000, 812), (7_881_240_000, 2271), (7_881_240_001, 2341)]
        for base, points in cases:
            with self.subTest(base=base):
                self.assertEqual(nhis.property_points(Decimal(base)), points)
        self.assertEqual(len(nhis.PROPERTY_POINTS), 60)
        bounds = [upper for upper, _ in nhis.PROPERTY_POINTS[:-1]]
        self.assertEqual(bounds, sorted(bounds))

    def test_the_premium_adds_the_income_and_property_parts_and_long_term_care(self):
        # Two public pensions of 3,960만 a year: half counts, a twelfth a month at 7.19%; a 6억 base is 5억 after the deduction: 812 points.
        found = nhis.regional_premium(Decimal(39_600_000), Decimal(600_000_000))
        self.assertEqual(found["income"], Decimal(1_650_000) * nhis.RATE)                    # 118,635 won.
        self.assertEqual(found["property"], Decimal(812) * nhis.POINT_VALUE)                # 171,738 won.
        self.assertEqual(found["total"], (found["income"] + found["property"]) * (1 + nhis.CARE_RATIO))
        # No pension yet: the income part is the minimum, and the property part still comes on top.
        before = nhis.regional_premium(Decimal(0), Decimal(600_000_000))
        self.assertEqual((before["income"], before["health"]), (nhis.MINIMUM, nhis.MINIMUM + found["property"]))
        # An income month of exactly 28만 (672만 a year of pension, half counted) still pays the minimum.
        self.assertEqual(nhis.regional_premium(Decimal(6_720_000), Decimal(0))["income"], nhis.MINIMUM)
        # Unknown property is not zero: the part is None and the total a lower bound; the yearly ceiling holds.
        unknown = nhis.regional_premium(Decimal(39_600_000), None)
        self.assertIsNone(unknown["property"])
        self.assertEqual(unknown["health"], found["income"])
        self.assertEqual(nhis.regional_premium(Decimal(2_000_000_000), None)["health"], nhis.MAXIMUM)


if __name__ == "__main__":
    unittest.main()
