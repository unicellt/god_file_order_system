from helpers import IsolatedOrderSystemTestCase, osys


class StatusPathCharacterizationTests(IsolatedOrderSystemTestCase):
    def test_rejected_order_keeps_reservation_and_has_no_currency_status_quo(self):
        items = [{"sku": "SKU-ELEC-2", "price": 100, "qty": 2, "cat": "electronics", "weight": 2.0}]
        user = {"id": "mallory", "blacklist": True, "loyalty_points": 0}

        order = osys.OrderSystem(region="cn").checkout(items, user)

        self.assertEqual("rejected", order["status"])
        self.assertEqual(190.0, order["total"])
        self.assertNotIn("currency", order)
        self.assertEqual(100, order["breakdown"]["risk_score"])
        self.assertEqual(0, order["points_earned"])
        self.assertEqual(190, order["breakdown"]["points_earned"])

        self.assertEqual(order, osys._ORDERS["ORD-1001"])
        self.assertEqual({"ORD-1001": {"SKU-ELEC-2": 2}}, dict(osys._RESERVATIONS.items()))
        self.assertEqual(3, osys._INVENTORY["SKU-ELEC-2"])
        self.assertEqual(
            [{"kind": "order_rejected", "payload": {"order_id": "ORD-1001", "risk": 100}}],
            list(osys._EVENTS),
        )
        self.assertEqual(
            [
                "reserved {'SKU-ELEC-2': 2} for ORD-1001",
                "saved order ORD-1001 total=190.0",
            ],
            list(osys._AUDIT_LOG),
        )

    def test_out_of_stock_order_is_not_saved_status_quo(self):
        items = [{"sku": "SKU-LUX-1", "price": 8000, "qty": 5, "cat": "luxury", "weight": 0.5}]
        user = {"id": "dave", "vip": True, "vip_level": 5, "loyalty_points": 8000}

        order = osys.OrderSystem(region="eu").checkout(items, user)

        self.assertEqual("out_of_stock", order["status"])
        self.assertEqual("EUR", order["currency"])
        self.assertEqual(40800.0, order["total"])
        self.assertEqual(60, order["breakdown"]["risk_score"])
        self.assertEqual(0, len(osys._ORDERS))
        self.assertEqual({}, dict(osys._RESERVATIONS.items()))
        self.assertEqual(3, osys._INVENTORY["SKU-LUX-1"])
        self.assertEqual([], list(osys._EVENTS))
        self.assertEqual(
            ["reserve failed: SKU-LUX-1 need 5 have 3"],
            list(osys._AUDIT_LOG),
        )
