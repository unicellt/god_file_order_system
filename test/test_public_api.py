from helpers import IsolatedOrderSystemTestCase, osys


class PublicApiCharacterizationTests(IsolatedOrderSystemTestCase):
    def test_dispatch_checkout_switches_return_shape(self):
        items = self.base_cart()

        osys.FEATURE_FLAGS["use_legacy_v1"] = True
        legacy = osys.dispatch_checkout(
            items,
            {"id": "u", "vip": True, "vip_level": 2},
            coupon="PCT10",
            region="cn",
        )
        self.assertEqual({"total": 148.77, "engine": "v1"}, legacy)

        osys.reset_state()
        osys.FEATURE_FLAGS["use_legacy_v1"] = False
        current = osys.dispatch_checkout(
            items,
            {"id": "u", "vip": True, "vip_level": 2},
            coupon="PCT10",
            region="cn",
        )
        self.assertEqual("ORD-1001", current["id"])
        self.assertEqual("confirmed", current["status"])
        self.assertEqual("CNY", current["currency"])
        self.assertIn("breakdown", current)
        self.assertNotIn("engine", current)
