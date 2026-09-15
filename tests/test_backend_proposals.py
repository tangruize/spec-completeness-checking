from __future__ import annotations

import unittest
import shutil
from pathlib import Path
from uuid import uuid4

from specdet.adapters.verus.backend import VerusBackend
from specdet.adapters.verus.proposal_validation import checked_edits, proof_structure
from specdet.config import Config
from specdet.domain.models import Stage, digest, text_digest
from specdet.domain.proposals import GenerationRequest, Proposal
from specdet.storage.workspace import PreparedProject


class SourceProposalTests(unittest.TestCase):
    def test_whitespace_edit_preserves_tokens(self):
        source = "verus! { fn f()->(r:u8) ensures r==0, { 0 } }"
        changed, transformations, faithful = checked_edits({
            "edits": [{"file": "f.rs", "before": "r==0", "after": "r == 0"}],
        }, {"f.rs": source})
        self.assertTrue(faithful)
        self.assertIn("r == 0", changed["f.rs"])
        self.assertNotEqual(transformations[0]["before_digest"], transformations[0]["after_digest"])

    def test_goal_edit_is_not_mechanically_confirmed(self):
        _, _, faithful = checked_edits({
            "edits": [{"file": "f.rs", "before": "r==0", "after": "true"}],
        }, {"f.rs": "verus! { fn f()->(r:u8) ensures r==0, { 0 } }"})
        self.assertFalse(faithful)

    def test_parser_fix_is_not_itself_a_faithfulness_proof(self):
        _, _, faithful = checked_edits({
            "edits": [{"file": "f.rs", "before": "fn broken(", "after": "fn fixed() {}"}],
        }, {"f.rs": "fn broken("})
        self.assertFalse(faithful)

    def test_ambiguous_and_outside_edits_fail(self):
        for edit in (
            {"file": "../f.rs", "before": "x", "after": "y"},
            {"file": "f.rs", "before": "x", "after": "y"},
        ):
            with self.assertRaises(ValueError):
                checked_edits({"edits": [edit]}, {"f.rs": "x x"})


class ProofStructureTests(unittest.TestCase):
    def test_helpers_have_separate_goal_names(self):
        self.assertEqual(
            proof_structure("lemma();", "proof fn lemma() ensures true { }", {"original"}),
            ("lemma",),
        )

    def test_helpers_cannot_replace_context(self):
        for helper in (
            "proof fn original() ensures true {}",
            "spec fn sneaky() -> bool { true }",
            "use unverified::lemma;",
            "proof fn outer() { proof fn inner() {} }",
        ):
            with self.assertRaises(ValueError):
                proof_structure("", helper, {"original"})

    def test_escaped_or_nested_candidate_is_rejected(self):
        for body in ("} } verus! { proof fn second() {", "proof fn hidden() {}"):
            with self.assertRaises(ValueError):
                proof_structure(body, "", set())


class BackendAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / f"backend-adoption-{uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.source = "verus! {\npub fn f()->(r:u8) ensures r==0, { 0 }\npub fn g()->(r:u8) ensures r==1, { 1 }\n}"
        (self.root / "f.rs").write_text(self.source)
        hashes = {"f.rs": text_digest(self.source)}
        self.project = PreparedProject(self.root, self.root, digest(hashes), hashes)
        self.config = Config(self.root, self.root / "out")
        self.backend = VerusBackend(self.config)
        self.targets = self.backend.discover(self.project)[0]

    def test_target_normalization_is_isolated_and_refreshes_line(self):
        target, unrelated = self.targets
        context = self.backend.generation_context(Stage.EXTRACT, self.project, target, None, None, ())
        request = GenerationRequest(Stage.EXTRACT, "verus", ("normalization",), target.source_digest, "", context, target.id)
        proposal = Proposal(
            request.id, request.stage, "verus", "normalization", request.source_digest, "",
            {"edits": [{"file": "f.rs", "before": "pub fn f", "after": "\n\npub fn f"}]},
        )
        record = self.backend.validate_proposal(request, proposal, self.project, target, None, None)
        self.assertEqual(record.status, "accepted")
        restart = self.backend.adopt_proposal(proposal, self.project, target, None, None)
        self.assertEqual(restart, Stage.DISCOVER)
        changed = self.backend._source(self.project, "f.rs", target)
        self.assertNotEqual(changed, self.source)
        self.assertEqual(self.backend._source(self.project, "f.rs", unrelated), self.source)
        self.assertEqual((self.root / "f.rs").read_text(), self.source)
        refreshed = self.backend._location(self.project, target, changed)
        self.assertEqual(refreshed.target.line, target.line + 2)
        self.assertEqual(
            self.backend._target_key(self.project, target),
            self.backend._target_key(self.project, refreshed.target),
        )

    def test_normalized_target_survives_repeated_rediscovery_without_cross_target_leaks(self):
        original, unrelated = self.targets
        request = GenerationRequest(
            Stage.EXTRACT, "verus", ("normalization",), original.source_digest, "", {}, original.id,
        )
        proposal = Proposal(
            request.id, request.stage, "verus", "normalization", request.source_digest, "",
            {"edits": [{"file": "f.rs", "before": "pub fn f", "after": "\n\npub fn f"}]},
        )
        self.assertEqual(self.backend.validate_proposal(
            request, proposal, self.project, original, None, None,
        ).status, "accepted")
        self.assertEqual(self.backend.adopt_proposal(
            proposal, self.project, original, None, None,
        ), Stage.DISCOVER)
        rediscovered, _ = self.backend.discover(self.project)
        refreshed = next(target for target in rediscovered if target.name == "f")
        self.assertNotEqual(refreshed.id, original.id)
        self.assertEqual(refreshed.line, original.line + 2)
        self.assertEqual(next(target for target in rediscovered if target.name == "g"), unrelated)
        self.assertEqual(
            self.backend._target_key(self.project, refreshed),
            self.backend._target_key(self.project, original),
        )
        contract = self.backend.extract(self.project, refreshed)
        self.assertEqual(contract.target, refreshed)
        self.assertEqual(contract.source_digest, refreshed.source_digest)
        self.assertEqual(self.backend.discover(self.project)[0], rediscovered)
        self.assertEqual(self.backend._source(self.project, "f.rs", unrelated), self.source)
        self.assertEqual((self.root / "f.rs").read_text(), self.source)

        next_request = GenerationRequest(
            Stage.EXTRACT, "verus", ("normalization",), contract.source_digest, "", {}, refreshed.id,
        )
        next_proposal = Proposal(
            next_request.id, next_request.stage, "verus", "normalization", contract.source_digest, "",
            {"edits": [{"file": "f.rs", "before": "r==0", "after": "r == 0"}]},
        )
        self.assertEqual(self.backend.validate_proposal(
            next_request, next_proposal, self.project, refreshed, contract, None,
        ).status, "accepted")
        self.backend.adopt_proposal(next_proposal, self.project, refreshed, contract, None)
        final_target = next(target for target in self.backend.discover(self.project)[0] if target.name == "f")
        self.assertNotEqual(final_target.id, refreshed.id)
        final_contract = self.backend.extract(self.project, final_target)
        self.assertEqual(len(final_contract.native["transformations"]), 2)
        self.assertEqual(
            self.backend._target_key(self.project, final_target),
            self.backend._target_key(self.project, original),
        )

    def test_early_profile_is_bound_to_configuration_digest(self):
        context = self.backend.generation_context(Stage.CONFIG, None, None, None, None, ())
        request = GenerationRequest(
            Stage.CONFIG, "verus", ("project_profile",), digest(self.config.to_dict()), "", context,
        )
        proposal = Proposal(
            request.id, request.stage, "verus", "project_profile", request.source_digest, "",
            {"build": {"entrypoint": "f.rs"}},
        )
        record = self.backend.validate_proposal(request, proposal, None, None, None, None)
        self.assertEqual(record.status, "accepted")
        self.assertEqual(self.backend.adopt_proposal(proposal, None, None, None, None), Stage.CONFIG)
        self.assertEqual(self.backend.config.build.entrypoint, "f.rs")
        self.assertTrue(self.backend.generation_context(Stage.CONFIG, None, None, None, None, ())["profile_trace"])

    def test_empty_source_identity_is_allowed_before_any_source_exists(self):
        request = GenerationRequest(Stage.CONFIG, "verus", ("project_profile",), "", "", {})
        proposal = Proposal(
            request.id, request.stage, "verus", "project_profile", "", "",
            {"build": {"entrypoint": "f.rs"}},
        )
        record = self.backend.validate_proposal(request, proposal, None, None, None, None)
        self.assertEqual(record.status, "accepted")
        self.assertEqual(self.backend.adopt_proposal(proposal, None, None, None, None), Stage.CONFIG)

    def test_stale_or_cross_target_proposal_is_rejected(self):
        target, other = self.targets
        request = GenerationRequest(
            Stage.EXTRACT, "verus", ("normalization",), target.source_digest, "", {}, target.id,
        )
        proposal = Proposal(
            request.id, request.stage, "verus", "normalization", request.source_digest, "",
            {"edits": [{"file": "f.rs", "before": "r==0", "after": "r == 0"}]},
        )
        record = self.backend.validate_proposal(request, proposal, self.project, other, None, None)
        self.assertEqual(record.status, "rejected")
        with self.assertRaises(Exception):
            self.backend.adopt_proposal(proposal, self.project, other, None, None)

    def test_validated_proposal_cannot_be_adopted_for_another_target(self):
        target, other = self.targets
        request = GenerationRequest(
            Stage.EXTRACT, "verus", ("normalization",), target.source_digest, "", {}, target.id,
        )
        proposal = Proposal(
            request.id, request.stage, "verus", "normalization", request.source_digest, "",
            {"edits": [{"file": "f.rs", "before": "r==0", "after": "r == 0"}]},
        )
        record = self.backend.validate_proposal(request, proposal, self.project, target, None, None)
        self.assertEqual(record.status, "accepted")
        with self.assertRaises(Exception):
            self.backend.adopt_proposal(proposal, self.project, other, None, None)


if __name__ == "__main__":
    unittest.main()
