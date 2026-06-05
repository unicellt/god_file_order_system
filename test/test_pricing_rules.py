from helpers import IsolatedOrderSystemTestCase, osys


class PricingRuleCharacterizationTests(IsolatedOrderSystemTestCase):
    def test_quote_accepts_but_ignores_coupon_and_has_no_side_effects_status_quo(self):
        items = [{"sku": "SKU-FRESH-1", "price": 20, "qty": 3, "cat": "fresh", "weight": 1.0}]
        user = {"id": "q", "vip": False}
        system = osys.OrderSystem(region="cn")

        self.assertEqual(68.0, system.quote(items, user))
        self.assertEqual(68.0, system.quote(items, user, coupon="FIX10"))
        self.assertEqual(0, len(osys._ORDERS))
        self.assertEqual(0, len(osys._EVENTS))
        self.assertEqual(0, len(osys._AUDIT_LOG))
        self.assertEqual(100, osys._INVENTORY["SKU-FRESH-1"])

    def test_percent_coupon_threshold_differs_between_checkout_and_calc_v1_status_quo(self):
        items = [{"sku": "SKU-LUX-1", "price": 100, "qty": 1, "cat": "luxury", "weight": 0}]
        user = {"id": "u", "vip": False, "loyalty_points": 0}

        checkout_order = osys.OrderSystem(region="cn").checkout(
            items,
            user,
            coupon="PCT10",
            dry_run=True,
        )
        legacy_total = osys.calc_v1(items, user, coupon="PCT10", region="cn")

        self.assertEqual(100.0, checkout_order["total"])
        self.assertEqual(100.0, checkout_order["breakdown"]["after_coupon"])
        self.assertEqual(90.0, legacy_total)

    def test_eu_electronics_double_discount_and_rounding_status_quo(self):
        items = [{"sku": "SKU-ELEC-1", "price": 33.335, "qty": 2, "cat": "electronics"}]
        user = {"id": "eu", "vip": False, "loyalty_points": 0}

        order = osys.OrderSystem(region="eu").checkout(items, user, dry_run=True)

        self.assertEqual(60.17, order["items"][0]["line"])
        self.assertEqual(60.17, order["breakdown"]["subtotal_after_cat"])
        self.assertAlmostEqual(12.034, order["breakdown"]["tax"])
        self.assertEqual(19.0, order["breakdown"]["shipping"])
        self.assertEqual(60, order["breakdown"]["points_earned"])
        self.assertEqual(91.2, order["total"])
