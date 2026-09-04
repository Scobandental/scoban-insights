import unittest

from automation.sync import SNAPSHOT_AGE_SECONDS, capture_14_day_snapshot


class CaptureSnapshotTests(unittest.TestCase):
    def test_waits_until_video_is_fourteen_days_old(self):
        account = {}
        video = {"id": "123", "create_time": 1_000, "view_count": 4_505}

        captured = capture_14_day_snapshot(
            account, video, 1_000 + SNAPSHOT_AGE_SECONDS - 1
        )

        self.assertFalse(captured)
        self.assertEqual(account, {})

    def test_captures_once_and_never_overwrites(self):
        account = {}
        video = {"id": "123", "create_time": 1_000, "view_count": 4_505}
        due = 1_000 + SNAPSHOT_AGE_SECONDS

        self.assertTrue(capture_14_day_snapshot(account, video, due))
        video["view_count"] = 5_053
        self.assertFalse(capture_14_day_snapshot(account, video, due + 86_400))
        self.assertEqual(account["view_snapshots_14d"]["123"], 4_505)


if __name__ == "__main__":
    unittest.main()
