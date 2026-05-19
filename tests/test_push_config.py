import base64
import unittest

import main


class PushConfigTest(unittest.TestCase):
    def test_vapid_public_key_is_browser_subscription_format(self):
        info = main._ensure_vapid()
        key = info["vapid_public_key"]
        raw = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))

        self.assertEqual(65, len(raw))
        self.assertEqual(0x04, raw[0])

    def test_entry_api_includes_restaurant_id_for_status_polling(self):
        conn = main.get_db()
        restaurant = conn.execute("SELECT id FROM restaurants LIMIT 1").fetchone()
        if not restaurant:
            conn.close()
            self.skipTest("no restaurant fixture in local database")
        entry = conn.execute(
            "SELECT id FROM queue_entries WHERE restaurant_id = ? LIMIT 1",
            (restaurant["id"],),
        ).fetchone()
        conn.close()
        if not entry:
            self.skipTest("no queue entry fixture in local database")

        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        response = client.get(f"/api/queue/entry/{entry['id']}")
        self.assertEqual(200, response.status_code)
        self.assertEqual(restaurant["id"], response.json()["restaurant_id"])


if __name__ == "__main__":
    unittest.main()
