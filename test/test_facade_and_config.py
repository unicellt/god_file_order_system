from helpers import IsolatedOrderSystemTestCase, osys


class FacadeAndConfigCharacterizationTests(IsolatedOrderSystemTestCase):
    def test_checkout_facade_payment_decline_mutates_returned_order_status_quo(self):
        items = [{"sku": "SKU-ELEC-1", "price": 4000, "qty": 1, "cat": "electronics", "weight": 1}]
        user = {"id": "newbie", "new": True, "vip": False, "loyalty_points": 0}

        result = osys.CheckoutFacade(region="cn").place_order(items, user, method="bnpl")

        self.assertFalse(result["ok"])
        self.assertEqual("payment", result["stage"])
        self.assertEqual("payment_failed", result["order"]["status"])
        self.assertNotIn("payment_failed", osys.ORDER_STATES)
        self.assertEqual("declined", result["payment"]["status"])
        self.assertEqual("超过 bnpl 上限 3000", result["payment"]["reason"])
        self.assertEqual(result["payment"], osys._PAYMENTS["ORD-1001"])

        events = list(osys._EVENTS)
        self.assertEqual(
            [{"kind": "order_confirmed", "payload": {"order_id": "ORD-1001", "total": 4000.0}}],
            events,
        )
        self.assertFalse(any(e["kind"] == "payment_captured" for e in events))
        self.assertFalse(any(e["kind"] == "notification_sent" for e in events))
        self.assertIn("notify newbie: 订单 ORD-1001 已确认，应付 4000.00", list(osys._AUDIT_LOG))

    def test_tax_rate_change_only_affects_new_order_system_instances_status_quo(self):
        old_system = osys.OrderSystem(region="us")
        osys.ConfigManager().set_tax_rate("us", 0.5)
        new_system = osys.OrderSystem(region="us")
        items = [{"sku": "SKU-LUX-1", "price": 100, "qty": 1, "cat": "luxury", "weight": 0}]
        user = {"id": "tax", "vip": False, "loyalty_points": 0}

        old_order = old_system.checkout(items, user, dry_run=True)
        new_order = new_system.checkout(items, user, dry_run=True)

        self.assertEqual(0.08, old_system.tax)
        self.assertEqual(0.5, new_system.tax)
        self.assertEqual(8.0, old_order["breakdown"]["tax"])
        self.assertEqual(50.0, new_order["breakdown"]["tax"])
