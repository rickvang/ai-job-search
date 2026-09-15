"""Focused synthetic proof for the private job-ledger contract."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from job_ledger import (  # noqa: E402
    company_title_location_fingerprint,
    identity_key,
    load_ledger,
    normalize_canonical_url,
    observe,
    query_jobs,
    save_ledger,
)


class JobLedgerIdentityTests(unittest.TestCase):
    def test_stable_source_id_is_strongest_identity_and_repeat_updates_one_record(self):
        ledger = {"seen": {}}
        first = {
            "portal": "Example Portal",
            "source_id": "JOB-42",
            "title": "Platform Engineer",
            "company": "Acme, Inc.",
            "location": "Copenhagen",
            "url": "https://jobs.example.test/jobs/42?utm_source=weekly",
            "fit": "high",
            "rank_score": 88,
            "rank_verdict": "strong fit",
            "rank_date": "2026-09-14",
        }
        second = {**first, "url": "https://jobs.example.test/jobs/42?fbclid=click", "title": "Platform Engineer II"}

        first_result = observe(ledger, first, observed_on="2026-09-10")
        second_result = observe(ledger, second, observed_on="2026-09-14")

        self.assertTrue(first_result.created)
        self.assertFalse(second_result.created)
        self.assertTrue(second_result.material_changed)
        self.assertEqual(second_result.key, first_result.key)
        self.assertEqual(len(ledger["seen"]), 1)
        entry = ledger["seen"][first_result.key]
        self.assertEqual(entry["first_seen"], "2026-09-10")
        self.assertEqual(entry["last_seen"], "2026-09-14")
        self.assertEqual(entry["observation_count"], 2)
        self.assertEqual(entry["identity"]["source"], "example portal")
        self.assertEqual(entry["identity"]["source_id"], "job-42")
        self.assertEqual(entry["rank_score"], 88)
        self.assertEqual(entry["rank_verdict"], "strong fit")
        self.assertEqual(entry["portal"], "Example Portal")

    def test_tracking_only_url_differences_share_canonical_identity(self):
        first = "https://jobs.example.test/posting/42?utm_campaign=fall&lang=en"
        second = "https://JOBS.example.test/posting/42?lang=en&utm_medium=email#details"

        self.assertEqual(
            normalize_canonical_url(first),
            "https://jobs.example.test/posting/42?lang=en",
        )
        self.assertEqual(normalize_canonical_url(first), normalize_canonical_url(second))
        self.assertEqual(
            identity_key({"url": first}),
            identity_key({"url": second}),
        )
        ledger = {"seen": {}}
        first_result = observe(ledger, {"url": first}, observed_on="2026-09-10")
        second_result = observe(ledger, {"url": second}, observed_on="2026-09-14")
        self.assertFalse(second_result.created)
        self.assertEqual(first_result.key, second_result.key)
        self.assertEqual(len(ledger["seen"]), 1)

    def test_distinct_strong_source_ids_do_not_collide_on_one_landing_url(self):
        ledger = {"seen": {}}
        shared = {
            "source": "example portal",
            "url": "https://jobs.example.test/search/platform-engineer",
            "title": "Platform Engineer",
            "company": "Acme",
            "location": "Copenhagen",
        }

        first = observe(ledger, {**shared, "source_id": "job-1"}, observed_on="2026-09-10")
        second = observe(ledger, {**shared, "source_id": "job-2"}, observed_on="2026-09-10")
        repeat_first = observe(
            ledger,
            {**shared, "source_id": "job-1"},
            observed_on="2026-09-11",
        )

        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(repeat_first.key, first.key)
        self.assertEqual(ledger["seen"][first.key]["observation_count"], 2)
        self.assertEqual(ledger["seen"][second.key]["observation_count"], 1)

    def test_fingerprint_is_conservative_fallback(self):
        first = {
            "company": "Acme, Inc.",
            "title": "Platform Engineer (Remote)",
            "location": "Copenhagen, DK",
        }
        equivalent = {
            "company": "  ACME INC ",
            "title": "Platform Engineer (Remote)",
            "location": "Copenhagen, DK",
        }
        changed_location = {**equivalent, "location": "Aarhus, DK"}
        meaningful_symbol_change = {**first, "title": "C Developer (Remote)"}
        meaningful_symbol_title = {**first, "title": "C++ Developer (Remote)"}

        self.assertEqual(
            company_title_location_fingerprint(first),
            company_title_location_fingerprint(equivalent),
        )
        self.assertNotEqual(
            company_title_location_fingerprint(first),
            company_title_location_fingerprint(changed_location),
        )
        self.assertNotEqual(
            company_title_location_fingerprint(meaningful_symbol_change),
            company_title_location_fingerprint(meaningful_symbol_title),
        )
        self.assertEqual(identity_key(first), f"fingerprint:{company_title_location_fingerprint(first)}")


class JobLedgerLifecycleTests(unittest.TestCase):
    def test_closed_jobs_remain_queryable_and_are_not_new_by_default(self):
        ledger = {"seen": {}}
        for status in ("rejected", "applied", "expired"):
            observe(
                ledger,
                {
                    "source": "example",
                    "source_id": f"{status}-1",
                    "title": "Platform Engineer",
                    "company": "Acme",
                    "location": "Copenhagen",
                    "status": status,
                },
                observed_on="2026-09-01",
            )

        self.assertEqual(len(query_jobs(ledger)), 3)
        self.assertEqual(len(query_jobs(ledger, statuses={"rejected", "applied", "expired"})), 3)
        self.assertEqual(query_jobs(ledger, new_only=True), [])

        repeat = observe(
            ledger,
            {
                "source": "example",
                "source_id": "rejected-1",
                "title": "Platform Engineer",
                "company": "Acme",
                "location": "Copenhagen",
                "status": "new",
            },
            observed_on="2026-09-14",
        )
        self.assertFalse(repeat.created)
        self.assertEqual(repeat.status, "rejected")
        self.assertEqual(ledger["seen"][repeat.key]["last_seen"], "2026-09-14")

        updated = observe(
            ledger,
            {
                "source": "example",
                "source_id": "rejected-1",
                "title": "Platform Engineer",
                "company": "Acme",
                "location": "Copenhagen",
            },
            observed_on="2026-09-15",
            disposition="interview",
        )
        self.assertFalse(updated.created)
        self.assertEqual(updated.status, "interview")

    def test_repost_can_be_surfaced_once_while_prior_record_stays_queryable(self):
        ledger = {"seen": {}}
        original = {
            "portal": "example",
            "source_id": "42",
            "title": "Platform Engineer",
            "company": "Acme",
            "location": "Copenhagen",
            "description": "Build internal platforms.",
            "status": "rejected",
        }
        repost = {**original, "description": "Build internal platforms and developer tooling."}

        original_result = observe(ledger, original, observed_on="2026-08-01")
        repost_result = observe(
            ledger,
            repost,
            observed_on="2026-09-14",
            surface_repost=True,
        )
        repeat_result = observe(
            ledger,
            repost,
            observed_on="2026-09-15",
            surface_repost=True,
        )

        self.assertTrue(repost_result.created)
        self.assertTrue(repost_result.resurfaced)
        self.assertEqual(repeat_result.key, repost_result.key)
        self.assertFalse(repeat_result.created)
        self.assertEqual(len(ledger["seen"]), 2)
        self.assertEqual(ledger["seen"][original_result.key]["status"], "rejected")
        self.assertEqual(ledger["seen"][repost_result.key]["repost_of"], original_result.key)
        self.assertEqual(len(query_jobs(ledger, new_only=True)), 1)

        ordinary_result = observe(
            ledger,
            {**repost, "status": "new"},
            observed_on="2026-09-16",
        )
        self.assertEqual(ordinary_result.key, repost_result.key)
        self.assertFalse(ordinary_result.created)
        self.assertFalse(ordinary_result.is_new_candidate)
        self.assertEqual(ledger["seen"][repost_result.key]["last_seen"], "2026-09-16")
        self.assertEqual(ledger["seen"][original_result.key]["last_seen"], "2026-08-01")

    def test_is_new_candidate_requires_creation_even_when_status_is_new(self):
        ledger = {"seen": {}}
        observation = {
            "source": "example",
            "source_id": "42",
            "title": "Platform Engineer",
            "company": "Acme",
            "location": "Copenhagen",
            "status": "new",
        }

        first = observe(ledger, observation, observed_on="2026-09-14")
        repeat = observe(ledger, observation, observed_on="2026-09-15")

        self.assertTrue(first.created)
        self.assertTrue(first.is_new_candidate)
        self.assertFalse(repeat.created)
        self.assertFalse(repeat.is_new_candidate)


class JobLedgerCompatibilityTests(unittest.TestCase):
    def test_legacy_entry_is_matched_and_augmented_without_dropping_fields(self):
        legacy_key = "acme_platform_engineer"
        ledger = {
            "seen": {
                legacy_key: {
                    "title": "Platform Engineer",
                    "company": "Acme",
                    "location": "Copenhagen",
                    "url": "https://jobs.example.test/42?utm_source=old-run",
                    "first_seen": "2026-08-01",
                    "fit": "high",
                    "status": "ranked",
                    "portal": "example",
                    "rank_score": 82,
                    "rank_verdict": "strong fit",
                    "rank_date": "2026-08-02",
                }
            }
        }
        result = observe(
            ledger,
            {
                "portal": "example",
                "source_id": "42",
                "title": "Platform Engineer",
                "company": "Acme",
                "location": "Copenhagen",
                "url": "https://jobs.example.test/42?fbclid=new-click",
                "status": "new",
            },
            observed_on="2026-09-14",
        )

        self.assertFalse(result.created)
        self.assertEqual(result.key, legacy_key)
        entry = ledger["seen"][legacy_key]
        self.assertEqual(entry["first_seen"], "2026-08-01")
        self.assertEqual(entry["last_seen"], "2026-09-14")
        self.assertEqual(entry["status"], "ranked")
        self.assertEqual(entry["portal"], "example")
        self.assertEqual(entry["rank_score"], 82)
        self.assertEqual(entry["rank_verdict"], "strong fit")
        self.assertEqual(entry["rank_date"], "2026-08-02")
        self.assertEqual(entry["identity"]["canonical_url"], "https://jobs.example.test/42")

    def test_load_and_save_preserve_legacy_shape_and_additive_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "seen_jobs.json"
            path.write_text(json.dumps({"seen": {"legacy": {"status": "expired"}}}), encoding="utf-8")
            ledger = load_ledger(path)
            observe(
                ledger,
                {"url": "https://jobs.example.test/1", "title": "A", "company": "B", "location": "C"},
                observed_on="2026-09-14",
            )
            save_ledger(path, ledger)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["seen"]["legacy"]["status"], "expired")
            self.assertEqual(len(saved["seen"]), 2)


class JobLedgerPrivacyTests(unittest.TestCase):
    def test_private_ledger_and_tracker_are_ignored_and_untracked(self):
        root = Path(__file__).resolve().parent.parent
        for relative_path in ("job_scraper/seen_jobs.json", "job_search_tracker.csv"):
            ignored = subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", "--", relative_path],
                cwd=root,
                check=False,
            )
            self.assertEqual(ignored.returncode, 0, f"{relative_path} is not ignored")
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", relative_path],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(tracked.returncode, 0, f"{relative_path} is tracked")


if __name__ == "__main__":
    unittest.main()
