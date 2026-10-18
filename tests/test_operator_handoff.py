"""No-network product tests for operator-created shortlist handoffs."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from operator_handoff import HandoffError, read_shortlist, save_handoff, select_handoff  # noqa: E402


def shortlist_sample():
    return {
        "basis": "exact_matches_to_user_authored_tags",
        "source_status": "user_supplied_unverified",
        "provider_calls": 0,
        "city": "Minneapolis",
        "interests": ["jazz", "coffee"],
        "required_tags": [],
        "excluded_tags": [],
        "lanes": {
            "place": [{"lane": "place", "name": "Room One", "city": "Minneapolis",
                       "source_url": "https://example.org/venues/1", "source_label": "Operator notes",
                       "source_status": "user_supplied_unverified", "matched_interests": ["jazz", "coffee"],
                       "match_count": 2}],
            "brand": [{"lane": "brand", "name": "Coffee House", "city": None,
                       "source_url": "https://example.org/brands/a", "source_label": "Supplier list",
                       "source_status": "user_supplied_unverified", "matched_interests": ["coffee"],
                       "match_count": 1}],
            "artist": [],
        },
        "missing_lanes": ["artist"],
    }


class OperatorHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.input = Path(self.temp.name) / "shortlist.json"
        self.output = Path(self.temp.name) / "review.json"
        self.write_shortlist(shortlist_sample())

    def write_shortlist(self, payload):
        self.input.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")

    def run_cli(self, *extra):
        return subprocess.run([sys.executable, str(ROOT / "operator_handoff.py"),
                               "--shortlist", str(self.input), "--output", str(self.output),
                               *extra], cwd=ROOT, env={"PYTHONPATH": "", "PATH": os.environ.get("PATH", "")},
                              capture_output=True, text=True, timeout=5)

    def test_single_operator_selection_keeps_exact_attribution(self):
        payload, digest = read_shortlist(self.input)
        record = select_handoff(payload, digest, ["place:1"], note="Visit first")
        self.assertEqual(record["originating_shortlist_sha256"], hashlib.sha256(self.input.read_bytes()).hexdigest())
        self.assertEqual(record["selection_count"], 1)
        self.assertEqual(record["operator_note"], "Visit first")
        self.assertEqual(record["selections"][0]["source_label"], "Operator notes")
        self.assertEqual(record["selections"][0]["source_status"], "user_supplied_unverified")
        self.assertEqual(record["review_status"], "operator_selected_not_provider_verified")

    def test_multiple_selections_preserve_operator_order_and_positions(self):
        payload, digest = read_shortlist(self.input)
        result = select_handoff(payload, digest, ["brand:1", "place:1"])
        self.assertEqual([(r["operator_selection"]["lane"], r["operator_selection"]["rank"])
                          for r in result["selections"]], [("brand", 1), ("place", 1)])
        self.assertEqual(result["query"]["interests"], ["jazz", "coffee"])

    def test_actual_subprocess_cli_writes_private_json(self):
        proc = self.run_cli("--select", "place:1", "--select", "brand:1", "--note", "Review Friday")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(record["selection_count"], 2)
        self.assertEqual(record["operator_note"], "Review Friday")
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_no_selection_of_unavailable_rank_or_missing_lane(self):
        payload, digest = read_shortlist(self.input)
        for choose in (["place:2"], ["artist:1"], ["place:0"], ["venue:1"], ["place:" + "9" * 10000]):
            with self.subTest(choose=choose), self.assertRaises(HandoffError):
                select_handoff(payload, digest, choose)

    def test_no_duplicate_or_empty_selection(self):
        payload, digest = read_shortlist(self.input)
        for picks in ([], ["place:1", "place:1"], ["place:1"] * 21):
            with self.subTest(picks=picks), self.assertRaises(HandoffError):
                select_handoff(payload, digest, picks)

    def test_non_authoritative_or_forged_provenance_is_rejected(self):
        for mutation in (
            lambda x: x.update(provider_calls=1),
            lambda x: x.update(provider_calls=True),
            lambda x: x.update(source_status="verified_by_qloo"),
            lambda x: x["lanes"]["place"][0].update(source_status="verified_by_qloo"),
            lambda x: x["lanes"]["place"][0].update(source_url="http://insecure.example/"),
            lambda x: x["lanes"]["place"][0].update(match_count=99),
            lambda x: x["lanes"]["place"][0].update(city="Chicago"),
        ):
            source = shortlist_sample()
            mutation(source)
            self.write_shortlist(source)
            with self.subTest(mutation=str(mutation)), self.assertRaises(HandoffError):
                read_shortlist(self.input)

    def test_missing_lane_statement_must_match_records(self):
        x = shortlist_sample()
        x["missing_lanes"] = []
        self.write_shortlist(x)
        with self.assertRaisesRegex(HandoffError, "missing-lane"):
            read_shortlist(self.input)

    def test_missing_city_field_and_bad_unicode_tags_are_rejected(self):
        for mutation in (
            lambda x: x["lanes"]["brand"][0].pop("city"),
            lambda x: x["interests"].append("\ud800"),
            lambda x: x["required_tags"].append("\ud800"),
        ):
            source = shortlist_sample()
            mutation(source)
            self.write_shortlist(source)
            with self.assertRaises(HandoffError):
                read_shortlist(self.input)

    def test_invalid_direct_note_value_fails_closed(self):
        with self.assertRaises(HandoffError):
            select_handoff(*read_shortlist(self.input), ["place:1"], note=None)

    def test_duplicate_json_keys_are_rejected(self):
        s = self.input.read_text()
        self.input.write_text(s.replace('"provider_calls": 0,', '"provider_calls": 0, "provider_calls": 0,'))
        with self.assertRaisesRegex(HandoffError, "duplicate"):
            read_shortlist(self.input)

    def test_missing_file_and_excess_size_fail_closed(self):
        self.input.unlink()
        with self.assertRaises(HandoffError):
            read_shortlist(self.input)
        self.input.write_bytes(b" " * 250001)
        with self.assertRaises(HandoffError):
            read_shortlist(self.input)

    def test_malformed_note_rejected_without_writing(self):
        p = self.run_cli("--select", "place:1", "--note", "invalid\nnewline")
        self.assertEqual(p.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_overwritten(self):
        self.output.write_text('existing private artifact', encoding="utf-8")
        proc = self.run_cli("--select", "place:1")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.output.read_text(), 'existing private artifact')

    def test_invalid_rank_does_not_create_any_output(self):
        proc = self.run_cli("--select", "place:2")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_different_source_files_result_in_distinct_hashes(self):
        original, digest = read_shortlist(self.input)
        changed = copy.deepcopy(original)
        changed["lanes"]["place"][0]["source_label"] = "Different user notes"
        self.write_shortlist(changed)
        next_payload, other_digest = read_shortlist(self.input)
        self.assertNotEqual(digest, other_digest)
        self.assertEqual(select_handoff(next_payload, other_digest, ["place:1"])["selections"][0]["source_label"],
                         "Different user notes")

    def test_save_handoff_creates_and_refuses_second_write(self):
        payload, digest = read_shortlist(self.input)
        result = select_handoff(payload, digest, ["brand:1"])
        save_handoff(self.output, result)
        with self.assertRaisesRegex(HandoffError, "already exists"):
            save_handoff(self.output, result)

    def test_duplicate_or_noncanonical_interests_are_rejected(self):
        payload = shortlist_sample()
        payload["interests"] = ["jazz", "jazz"]
        for lane in payload["lanes"].values():
            for card in lane:
                card["matched_interests"] = ["jazz"]
                card["match_count"] = 1
        self.write_shortlist(payload)
        with self.assertRaisesRegex(HandoffError, "canonical unique"):
            read_shortlist(self.input)

    def test_duplicate_matched_interests_cannot_inflate_match_count(self):
        payload = shortlist_sample()
        payload["lanes"]["place"][0]["matched_interests"] = ["jazz", "jazz"]
        payload["lanes"]["place"][0]["match_count"] = 2
        self.write_shortlist(payload)
        with self.assertRaisesRegex(HandoffError, "interest matches"):
            read_shortlist(self.input)

    def test_casefold_duplicates_and_conflicting_filters_are_rejected(self):
        for mutation in (
            lambda x: x.update(required_tags=["jazz"], excluded_tags=["jazz"]),
            lambda x: x.update(required_tags=["jazz", "JAZZ"]),
            lambda x: x.update(excluded_tags=["coffee", "COFFEE"]),
        ):
            payload = shortlist_sample()
            mutation(payload)
            self.write_shortlist(payload)
            with self.subTest(mutation=str(mutation)), self.assertRaises(HandoffError):
                read_shortlist(self.input)

    def test_source_status_always_remains_unverified(self):
        result = select_handoff(*read_shortlist(self.input), ["place:1"])
        serialized = json.dumps(result)
        self.assertNotIn('\"source_status\": \"provider_verified\"', serialized)
        self.assertIn('operator_selected_not_provider_verified', serialized)
        self.assertIn('user_supplied_unverified', serialized)


if __name__ == "__main__":
    unittest.main()
