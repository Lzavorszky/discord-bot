import json
import os
import sys
import tempfile
import unittest


TEST_ROOT = os.path.dirname(__file__)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)


class TestRouteClaimsParser(unittest.TestCase):
    def test_parse_protocol_file_loads_adjacent_route_claims_sidecar(self):
        import protocol_parser as pp

        with tempfile.TemporaryDirectory() as tmp:
            protocol_path = os.path.join(tmp, "sample.txt")
            with open(protocol_path, "w", encoding="utf-8") as f:
                f.write(
                    "## METADATA\n"
                    "protocol_id: sample\n"
                    "source_label: Sample\n\n"
                    "## ALIASES\n"
                    "- sample protocol\n"
                )

            with open(os.path.join(tmp, "sample.route_claims.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "intents": ["dose"],
                        "subjects": ["drug"],
                        "owns": {"drugs": ["sample_drug"]},
                        "excludes": ["coverage_question"],
                    },
                    f,
                )

            parsed = pp.parse_protocol_file(protocol_path)

        self.assertEqual(parsed["route_claims"]["intents"], ["dose"])
        self.assertEqual(parsed["route_claims"]["owns"]["drugs"], ["sample_drug"])

    def test_parse_route_claims_section_supports_minimal_yaml_subset(self):
        import protocol_parser as pp

        parsed = pp._parse_protocol_text(
            "## METADATA\n"
            "protocol_id: sample\n\n"
            "## ROUTE_CLAIMS\n"
            "intents:\n"
            "  - test_interpretation\n"
            "subjects:\n"
            "  - test_panel\n"
            "owns:\n"
            "  tests:\n"
            "    - biofire\n"
            "  microbes:\n"
            "    source: pcr_organism_aliases\n"
            "requires:\n"
            "  - test\n"
        )

        self.assertEqual(parsed["route_claims"]["intents"], ["test_interpretation"])
        self.assertEqual(parsed["route_claims"]["owns"]["tests"], ["biofire"])
        self.assertEqual(
            parsed["route_claims"]["owns"]["microbes"],
            {"source": "pcr_organism_aliases"},
        )

    def test_initial_protocol_claims_are_loaded(self):
        import protocol_parser as pp

        expected = {
            "protocols/pneumonia_pcr.txt": "test_interpretation",
            "protocols/joint_infection_pcr.txt": "test_interpretation",
            "protocols/cap.txt": "empiric_treatment",
            "protocols/uti.txt": "empiric_treatment",
            "protocols/intraabdominal_infections.txt": "empiric_treatment",
            "protocols/cdiff.txt": "diagnosis",
            "protocols/sbp.txt": "diagnosis",
            "protocols/endocarditis_antibiotics.txt": "targeted_treatment",
            "protocols/antibiotics/meropenem.txt": "dose",
            "protocols/periop_gyogyszerek.txt": "perioperative_medication_management",
            "protocols/periop_steroids.txt": "perioperative_steroid_management",
            "protocols/steroid_equivalence.txt": "dose_conversion",
            "protocols/body_size_calculators.txt": "calculator",
            "protocols/echo_cardiac_output.txt": "calculator",
            "protocols/echo_ava.txt": "calculator",
            "protocols/echo_ero_rvol.txt": "calculator",
        }

        for rel_path, intent in expected.items():
            with self.subTest(rel_path=rel_path):
                parsed = pp.parse_protocol_file(os.path.join(TEST_ROOT, rel_path))
                self.assertIn(intent, parsed["route_claims"].get("intents", []))

    def test_all_current_protocols_have_claims_or_explicit_opt_out(self):
        import protocol_linter

        result = protocol_linter.run_linter(proto_dir=os.path.join(TEST_ROOT, "protocols"))

        route_errors = [i for i in result.errors() if i.code.startswith("route_claims")]
        self.assertEqual(route_errors, [])


class TestRouteClaimsLinter(unittest.TestCase):
    def _write_protocol_with_claims(self, claims):
        tmp = tempfile.TemporaryDirectory()
        protocols_dir = os.path.join(tmp.name, "protocols")
        os.mkdir(protocols_dir)
        protocol_path = os.path.join(protocols_dir, "sample.txt")
        with open(protocol_path, "w", encoding="utf-8") as f:
            f.write(
                "## METADATA\n"
                "protocol_id: sample\n"
                "source_label: Sample\n"
                "protocol_type: general_rules_protocol\n"
                "answer_mode: info_only\n"
                "selection_mode: none\n"
                "version: 1\n"
                "last_reviewed: test\n"
                "owner: test\n"
                "status: draft\n\n"
                "## ALIASES\n"
                "- sample protocol\n"
            )
        with open(os.path.join(protocols_dir, "sample.route_claims.json"), "w", encoding="utf-8") as f:
            json.dump(claims, f)
        return tmp, protocols_dir

    def _write_protocol(self, metadata, aliases="- sample protocol\n", claims=None):
        tmp = tempfile.TemporaryDirectory()
        protocols_dir = os.path.join(tmp.name, "protocols")
        os.mkdir(protocols_dir)
        protocol_path = os.path.join(protocols_dir, "sample.txt")
        with open(protocol_path, "w", encoding="utf-8") as f:
            f.write("## METADATA\n")
            for key, value in metadata.items():
                f.write(f"{key}: {value}\n")
            f.write("\n## ALIASES\n")
            f.write(aliases)
        if claims is not None:
            with open(os.path.join(protocols_dir, "sample.route_claims.json"), "w", encoding="utf-8") as f:
                json.dump(claims, f)
        return tmp, protocols_dir

    def test_linter_accepts_valid_route_claims(self):
        import protocol_linter

        tmp, protocols_dir = self._write_protocol_with_claims(
            {
                "intents": ["dose"],
                "subjects": ["drug"],
                "owns": {"drugs": ["sample_drug"], "microbes": {"source": "aliases"}},
                "requires": ["drug"],
                "excludes": ["coverage_question"],
                "clarify_if_missing": ["drug"],
            }
        )
        with tmp:
            result = protocol_linter.run_linter(proto_dir=protocols_dir)

        route_errors = [i for i in result.errors() if i.code.startswith("route_claims")]
        self.assertEqual(route_errors, [])

    def test_linter_rejects_unknown_route_claims_key(self):
        import protocol_linter

        tmp, protocols_dir = self._write_protocol_with_claims(
            {
                "intents": ["dose"],
                "subjects": ["drug"],
                "owns": {"drugs": ["sample_drug"]},
                "confidence": "high",
            }
        )
        with tmp:
            result = protocol_linter.run_linter(proto_dir=protocols_dir)

        self.assertIn("route_claims_unknown_key", [i.code for i in result.errors()])

    def test_linter_rejects_non_string_claim_list_items(self):
        import protocol_linter

        tmp, protocols_dir = self._write_protocol_with_claims(
            {
                "intents": ["dose", 42],
                "subjects": ["drug"],
                "owns": {"drugs": ["sample_drug"]},
            }
        )
        with tmp:
            result = protocol_linter.run_linter(proto_dir=protocols_dir)

        self.assertIn("route_claims_invalid_list", [i.code for i in result.errors()])

    def test_linter_rejects_missing_route_claims(self):
        import protocol_linter

        tmp, protocols_dir = self._write_protocol(
            {
                "protocol_id": "sample",
                "source_label": "Sample",
                "protocol_type": "general_rules_protocol",
                "answer_mode": "info_only",
                "selection_mode": "none",
                "version": "1",
                "last_reviewed": "test",
                "owner": "test",
                "status": "draft",
            }
        )
        with tmp:
            result = protocol_linter.run_linter(proto_dir=protocols_dir)

        self.assertIn("route_claims_missing", [i.code for i in result.errors()])

    def test_linter_rejects_drug_dosing_claim_without_coverage_excludes(self):
        import protocol_linter

        tmp, protocols_dir = self._write_protocol(
            {
                "protocol_id": "sample_drug",
                "source_label": "Sample Drug",
                "protocol_type": "drug_dosing_protocol",
                "answer_mode": "default_then_selected_output",
                "selection_mode": "priority_rules",
                "version": "1",
                "last_reviewed": "test",
                "owner": "test",
                "status": "draft",
            },
            claims={
                "intents": ["dose"],
                "subjects": ["drug"],
                "owns": {"drugs": ["sample_drug"]},
                "excludes": ["coverage_question"],
            },
        )
        with tmp:
            result = protocol_linter.run_linter(proto_dir=protocols_dir)

        self.assertIn(
            "route_claims_drug_dosing_missing_excludes",
            [i.code for i in result.errors()],
        )

    def test_linter_rejects_microbiology_claim_without_microbe_requirement(self):
        import protocol_linter

        tmp, protocols_dir = self._write_protocol(
            {
                "protocol_id": "sample_pcr",
                "source_label": "Sample PCR",
                "protocol_type": "microbiology_interpretation_protocol",
                "answer_mode": "required_slots_then_selected_output",
                "selection_mode": "pcr_mapping",
                "version": "1",
                "last_reviewed": "test",
                "owner": "test",
                "status": "draft",
            },
            claims={
                "intents": ["test_interpretation"],
                "subjects": ["test_panel"],
                "owns": {"tests": ["pcr"]},
                "requires": ["test"],
            },
        )
        with tmp:
            result = protocol_linter.run_linter(proto_dir=protocols_dir)

        self.assertIn(
            "route_claims_microbiology_missing_microbe_or_marker",
            [i.code for i in result.errors()],
        )

    def test_linter_rejects_broad_syndrome_claim_without_fallback_excludes(self):
        import protocol_linter

        tmp, protocols_dir = self._write_protocol(
            {
                "protocol_id": "sample_syndrome",
                "source_label": "Sample Syndrome",
                "protocol_type": "pathway_selection_protocol",
                "answer_mode": "default_then_selected_output",
                "selection_mode": "priority_rules",
                "version": "1",
                "last_reviewed": "test",
                "owner": "test",
                "status": "draft",
            },
            aliases="- pneumonia\n",
            claims={
                "intents": ["empiric_treatment"],
                "subjects": ["syndrome"],
                "owns": {"syndromes": ["pneumonia"]},
            },
        )
        with tmp:
            result = protocol_linter.run_linter(proto_dir=protocols_dir)

        self.assertIn(
            "route_claims_broad_syndrome_missing_excludes",
            [i.code for i in result.errors()],
        )


class TestShadowRouteResolver(unittest.TestCase):
    def setUp(self):
        import aliases as alias_helpers

        self.alias_helpers = alias_helpers
        self._old_aliases = dict(alias_helpers.ALIASES)
        self._old_alias_index = dict(alias_helpers.ALIAS_INDEX)
        self._old_blocked_aliases = set(alias_helpers.BLOCKED_ALIASES)
        self._old_unsupported_syndromes = dict(alias_helpers.UNSUPPORTED_SYNDROMES)
        self._old_file_labels = dict(alias_helpers.PROTOCOL_FILE_TO_LABEL)
        alias_helpers.load_aliases(os.path.join(TEST_ROOT, "protocols", "aliases.json"))
        self.protocol_claims = self._load_protocol_claims()

    def tearDown(self):
        alias_helpers = self.alias_helpers
        alias_helpers.ALIASES = self._old_aliases
        alias_helpers.ALIAS_INDEX = self._old_alias_index
        alias_helpers.BLOCKED_ALIASES = self._old_blocked_aliases
        alias_helpers.UNSUPPORTED_SYNDROMES = self._old_unsupported_syndromes
        alias_helpers.PROTOCOL_FILE_TO_LABEL = self._old_file_labels

    def _load_protocol_claims(self):
        import protocol_parser as pp

        rel_paths = [
            "protocols/pneumonia_pcr.txt",
            "protocols/joint_infection_pcr.txt",
            "protocols/cap.txt",
            "protocols/cdiff.txt",
            "protocols/sbp.txt",
            "protocols/antibiotics/meropenem.txt",
            "protocols/periop_gyogyszerek.txt",
            "protocols/periop_steroids.txt",
            "protocols/steroid_equivalence.txt",
        ]
        return {
            rel_path: pp.parse_protocol_file(os.path.join(TEST_ROOT, rel_path))
            for rel_path in rel_paths
        }

    def _decision_for(self, question):
        import routing

        evidence = routing.extract_routing_evidence(question, state={})
        return routing.resolve_route(evidence, self.protocol_claims, state={})

    def _assert_routes_to(self, question, expected_suffix):
        decision = self._decision_for(question)
        self.assertEqual(decision.kind, "route")
        self.assertTrue(
            decision.protocol_file.replace("\\", "/").endswith(expected_suffix),
            f"{question!r} routed to {decision.protocol_file!r}, expected {expected_suffix!r}",
        )
        return decision

    def test_pneumonia_pcr_typo_proteus_routes_to_pcr_not_cap(self):
        decision = self._assert_routes_to("Penumonia PCR Proteus", "protocols/pneumonia_pcr.txt")
        self.assertFalse(decision.protocol_file.replace("\\", "/").endswith("protocols/cap.txt"))

    def test_biofire_pn_proteus_routes_to_pneumonia_pcr(self):
        self._assert_routes_to("BioFire PN Proteus", "protocols/pneumonia_pcr.txt")

    def test_pcr_proteus_asks_panel_source_without_explicit_panel(self):
        decision = self._decision_for("PCR Proteus")
        self.assertEqual(decision.kind, "clarify")
        self.assertIn("PCR/BioFire panel", decision.message)
        self.assertIsNone(decision.protocol_file)

    def test_proteus_pneumonia_asks_if_this_is_pcr_result(self):
        decision = self._decision_for("Proteus pneumonia")
        self.assertEqual(decision.kind, "clarify")
        self.assertIn("PCR/BioFire", decision.message)
        self.assertIsNone(decision.protocol_file)

    def test_meropenem_dose_routes_to_dosing_protocol(self):
        self._assert_routes_to("meropenem dose GFR 35", "protocols/antibiotics/meropenem.txt")

    def test_meropenem_coverage_question_is_not_answered_by_dosing_protocol(self):
        decision = self._decision_for("is meropenem good vs staphylococcal pneumonia")
        self.assertEqual(decision.kind, "unsupported")
        self.assertIsNone(decision.protocol_file)
        self.assertIn("coverage", decision.message)

    def test_pneumonia_antibiotics_routes_to_empiric_cap(self):
        self._assert_routes_to("pneumonia what antibiotics", "protocols/cap.txt")

    def test_aspirin_before_surgery_routes_to_periop_medication_protocol(self):
        self._assert_routes_to("aspirin before surgery", "protocols/periop_gyogyszerek.txt")

    def test_hydrocortisone_before_surgery_routes_to_periop_steroid_protocol(self):
        self._assert_routes_to("hydrocortisone before surgery", "protocols/periop_steroids.txt")

    def test_hydrocortisone_to_prednisolone_routes_to_steroid_conversion(self):
        self._assert_routes_to("hydrocortisone to prednisolone", "protocols/steroid_equivalence.txt")

    def test_sbp_diagnosis_routes_to_diagnostic_protocol(self):
        self._assert_routes_to("SBP diagnosis", "protocols/sbp.txt")

    def test_cdiff_diagnosis_routes_to_cdiff_protocol(self):
        self._assert_routes_to("C diff diagnosis", "protocols/cdiff.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
