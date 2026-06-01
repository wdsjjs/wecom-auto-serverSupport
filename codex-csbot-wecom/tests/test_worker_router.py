import tempfile
import time
import unittest
from pathlib import Path

from csbot.worker_router import (
    claim_worker,
    complete_worker,
    heartbeat_worker,
    list_workers,
    register_worker,
)


class WorkerRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "router.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_claims_only_idle_worker(self) -> None:
        register_worker(self.db, "w1")
        register_worker(self.db, "w2")
        heartbeat_worker(self.db, "w1", status="busy")

        claimed = claim_worker(self.db, job_id="job-1", now=100.0)

        self.assertEqual(claimed["worker_id"], "w2")

    def test_does_not_claim_compressing_worker(self) -> None:
        register_worker(self.db, "w1")
        heartbeat_worker(self.db, "w1", status="compressing")

        claimed = claim_worker(self.db, job_id="job-1", now=100.0)

        self.assertIsNone(claimed)

    def test_stale_busy_worker_can_be_reclaimed_after_timeout(self) -> None:
        register_worker(self.db, "w1", now=0.0)
        heartbeat_worker(self.db, "w1", status="busy", now=0.0, job_id="old")

        claimed = claim_worker(self.db, job_id="job-2", now=301.0, lease_timeout=300.0)

        self.assertEqual(claimed["worker_id"], "w1")
        self.assertEqual(claimed["job_id"], "job-2")

    def test_complete_worker_returns_to_idle_only_for_matching_job(self) -> None:
        register_worker(self.db, "w1", now=1.0)
        claim_worker(self.db, job_id="job-1", now=2.0)

        self.assertFalse(complete_worker(self.db, "w1", job_id="wrong"))
        self.assertEqual(list_workers(self.db)[0]["status"], "busy")

        self.assertTrue(complete_worker(self.db, "w1", job_id="job-1"))
        self.assertEqual(list_workers(self.db)[0]["status"], "idle")


if __name__ == "__main__":
    unittest.main()
