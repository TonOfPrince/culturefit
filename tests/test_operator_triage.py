"""Native, root-only tests for local triage of operator-backed CultureFit records."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from operator_triage import TriageError, read_review_inputs, save_new_private, triage


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
            "place": [{"lane": "place", "name": "Jasmine Room", "city": "Minneapolis",
                       "matched_interests": ["jazz", "coffee"], "match_count": 2,
                       "source_url": "https://example.org/venue/1", "source_label": "Operator notes",
                       "source_status": "user_supplied_unverified"}],
            "brand": [{"lane": "brand", "name": "Coffee Works", "city": None,
                       "matched_interests": ["coffee"], "match_count": 1,
                       "source_url": "https://example.org/brand/2", "source_label": "Operator catalogue",
                       "source_status": "user_supplied_unverified"}],
            "artist": [],
        },
        "missing_lanes": ["artist"],
    }


def handoff_sample(shortlist, digest):
    cards = []
    for lane, rank in (("place", 1), ("brand", 1)):
        card = shortlist["lanes"][lane][rank - 1]
        cards.append({
            "operator_selection": {"lane": lane, "rank": rank},
            "name": card["name"],
            "city": card["city"],
            "matched_interests": card["matched_interests"],
            "source_url": card["source_url"],
            "source_label": card["source_label"],
            "source_status": "user_supplied_unverified",
        })
    return {
        "format": "culturefit_operator_handoff_v1",
        "review_status": "operator_selected_not_provider_verified",
        "originating_shortlist_sha256": digest,
        "query": {k: shortlist[k] for k in ("city", "interests", "required_tags", "excluded_tags")},
        "operator_note": "Check these sources internally",
        "selection_count": len(cards),
        "selections": cards,
    }


class OperatorTriageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.shortlist = self.folder / "shortlist.json"
        self.handoff = self.folder / "handoff.json"
        self.output = self.folder / "triage.json"
        self.sample = shortlist_sample()
        self.write_inputs()

    def write_inputs(self, sample=None, handoff=None):
        sample = copy.deepcopy(sample if sample is not None else self.sample)
        raw = (json.dumps(sample, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.shortlist.write_bytes(raw)
        value = handoff if handoff is not None else handoff_sample(sample, hashlib.sha256(raw).hexdigest())
        self.handoff.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return value

    def read(self):
        return read_review_inputs(self.shortlist, self.handoff)

    def test_decision_on_two_real_source_backed_selections_preserves_lineage(self):
        handoff, digest = self.read()
        result = triage(handoff, digest, ["place:1:check"], note="Recheck venue listing")
        self.assertEqual(result["items"][0]["disposition"], "needs_source_check")
        self.assertEqual(result["items"][1]["disposition"], "pending")
        self.assertEqual(result["items"][0]["source_url"], "https://example.org/venue/1")
        self.assertEqual(result["items"][1]["source_label"], "Operator catalogue")
        self.assertEqual(result["originating_handoff_sha256"], digest)
        self.assertEqual(result["originating_shortlist_sha256"], handoff["originating_shortlist_sha256"])
        self.assertEqual(result["counts"]["pending"], 1)
        self.assertFalse(result["provider_independently_verified"])
        self.assertFalse(result["external_contact_authorized"])
        self.assertEqual(result["source_status"], "user_supplied_unverified")

    def test_explicit_retain_and_exclude_preserve_source_order(self):
        handoff, digest = self.read()
        result = triage(handoff, digest, ["brand:1:exclude", "place:1:retain"])
        self.assertEqual([x["name"] for x in result["items"]], ["Jasmine Room", "Coffee Works"])
        self.assertEqual([x["disposition"] for x in result["items"]],
                         ["retain_for_internal_review", "excluded_by_operator"])
        self.assertEqual(result["decided_count"], 2)
        self.assertEqual(result["counts"]["pending"], 0)

    def test_exact_byte_handoff_digest_changes_with_whitespace(self):
        original, orig_digest = self.read()
        self.handoff.write_bytes(self.handoff.read_bytes() + b"\n")
        revised, revised_digest = self.read()
        self.assertEqual(original, revised)
        self.assertNotEqual(orig_digest, revised_digest)

    def test_changed_shortlist_bytes_are_rejected_before_decision(self):
        self.shortlist.write_bytes(self.shortlist.read_bytes() + b"\n")
        with self.assertRaisesRegex(TriageError, "exact source-only shortlist"):
            self.read()

    def test_tampered_selected_source_and_name_are_rejected(self):
        original = self.write_inputs()
        for key, value in (("name", "Forged Place"), ("source_url", "https://example.org/forged"),
                           ("source_label", "Claimed independent source")):
            changed = copy.deepcopy(original)
            changed["selections"][0][key] = value
            self.write_inputs(handoff=changed)
            with self.subTest(key=key), self.assertRaisesRegex(TriageError, "differs"):
                self.read()

    def test_handoff_mismatched_query_or_claimed_verification_rejected(self):
        original = self.write_inputs()
        for change in (lambda r: r["query"].__setitem__("city", "Chicago"),
                       lambda r: r.__setitem__("review_status", "provider_verified"),
                       lambda r: r["selections"][0].__setitem__("source_status", "verified")):
            changed = copy.deepcopy(original)
            change(changed)
            self.write_inputs(handoff=changed)
            with self.assertRaises(TriageError):
                self.read()

    def test_casefold_city_consistency_across_shortlist_handoff_and_triage(self):
        # The catalogue and operator_handoff accept city equality by casefold.
        # Triage must use the same rule so a valid private handoff remains usable.
        sample = copy.deepcopy(self.sample)
        sample["lanes"]["place"][0]["city"] = "MINNEAPOLIS"
        self.write_inputs(sample=sample)
        handoff, digest = self.read()
        self.assertEqual(handoff["query"]["city"], "Minneapolis")
        self.assertEqual(handoff["selections"][0]["city"], "MINNEAPOLIS")
        result = triage(handoff, digest, ["place:1:check"])
        self.assertEqual(result["items"][0]["city"], "MINNEAPOLIS")
        self.assertEqual(result["items"][0]["disposition"], "needs_source_check")
        self.assertFalse(result["external_contact_authorized"])
        self.assertFalse(result["provider_independently_verified"])

    def test_non_string_place_city_fails_closed_with_domain_error(self):
        # A hostile or malformed shortlist must not crash on .casefold().
        for city in (None, 42, [], False):
            sample = copy.deepcopy(self.sample)
            sample["lanes"]["place"][0]["city"] = city
            self.write_inputs(sample=sample)
            with self.subTest(city=repr(city)), self.assertRaises(TriageError):
                self.read()

    def test_unselected_rank_and_unavailable_lane_fail(self):
        handoff, digest = self.read()
        for choice in ["artist:1:retain", "place:2:check", "brand:0:exclude"]:
            with self.subTest(choice=choice), self.assertRaises(TriageError):
                triage(handoff, digest, [choice])

    def test_duplicate_decisions_do_not_create_conflicting_review(self):
        handoff, digest = self.read()
        with self.assertRaisesRegex(TriageError, "duplicate"):
            triage(handoff, digest, ["place:1:check", "place:1:exclude"])

    def test_no_decision_or_extra_decisions_fail(self):
        handoff, digest = self.read()
        with self.assertRaises(TriageError):
            triage(handoff, digest, [])
        with self.assertRaises(TriageError):
            triage(handoff, digest, ["place:1:retain"] * 21)

    def test_note_control_characters_and_oversized_note_fail(self):
        handoff, digest = self.read()
        for note in ("bad\nsource", "x" * 501, "\ud800"):
            with self.subTest(note=repr(note)), self.assertRaises(TriageError):
                triage(handoff, digest, ["place:1:check"], note=note)

    def test_duplicate_json_fields_fail(self):
        self.handoff.write_text('{"format":"v1","format":"v2"}', encoding="utf-8")
        with self.assertRaisesRegex(TriageError, "duplicate JSON"):
            self.read()

    def test_non_finite_json_and_invalid_utf8_fail(self):
        for raw in (b'{"format":NaN}', b"\xff\xfe"):
            self.handoff.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(TriageError):
                self.read()

    def test_unavailable_input_fails_closed(self):
        self.handoff.unlink()
        with self.assertRaises(TriageError):
            self.read()

    def test_write_once_does_not_overwrite(self):
        handoff, digest = self.read()
        record = triage(handoff, digest, ["place:1:check"])
        save_new_private(self.output, record)
        initial = self.output.read_bytes()
        with self.assertRaisesRegex(TriageError, "already exists"):
            save_new_private(self.output, record)
        self.assertEqual(self.output.read_bytes(), initial)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)

    def test_actual_cli_from_independent_root_creates_private_record(self):
        completed = subprocess.run(
            [sys.executable, "operator_triage.py", "--shortlist", str(self.shortlist),
             "--handoff", str(self.handoff), "--decide", "brand:1:exclude",
             "--note", "Internal only", "--output", str(self.output)],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": ""},
            capture_output=True, text=True, timeout=6,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(result["decided_count"], 1)
        self.assertEqual(result["items"][1]["disposition"], "excluded_by_operator")
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertFalse(result["external_contact_authorized"])

    def test_bad_cli_decision_leaves_no_output(self):
        completed = subprocess.run(
            [sys.executable, "operator_triage.py", "--shortlist", str(self.shortlist),
             "--handoff", str(self.handoff), "--decide", "artist:4:retain",
             "--output", str(self.output)],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": ""},
            capture_output=True, text=True, timeout=6,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_source_name_unicode_preserved_in_triage(self):
        changed = copy.deepcopy(self.sample)
        changed["lanes"]["brand"][0]["name"] = "Café Beyoncé"
        self.write_inputs(sample=changed)
        handoff, digest = self.read()
        output = triage(handoff, digest, ["brand:1:check"])
        self.assertEqual(output["items"][1]["name"], "Café Beyoncé")

    def test_bool_selection_rank_rejected(self):
        changed = self.write_inputs()
        changed["selections"][0]["operator_selection"]["rank"] = True
        self.write_inputs(handoff=changed)
        with self.assertRaises(TriageError):
            self.read()

    def test_provenance_does_not_claim_provider_acceptance(self):
        handoff, digest = self.read()
        result = triage(handoff, digest, ["place:1:retain", "brand:1:check"])
        self.assertEqual(result["review_status"], "internal_operator_triage_only")
        self.assertEqual(result["source_status"], "user_supplied_unverified")
        self.assertFalse(result["provider_independently_verified"])
        self.assertFalse(result["external_contact_authorized"])


if __name__ == "__main__":
    unittest.main()
