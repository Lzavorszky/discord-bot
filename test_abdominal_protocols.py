import unittest

import protocol_parser
import selection_engine


def load_protocol(path):
    return protocol_parser.parse_protocol_file(path)


class TestAbdominalProtocols(unittest.TestCase):
    def test_cdiff_requires_diagnosis_or_treatment(self):
        parsed = load_protocol("protocols/cdiff.txt")
        slots = selection_engine.extract_slots_from_query("cdiff", parsed_protocol=parsed)
        result = selection_engine.run_selection(parsed, slots, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertTrue(result.default_used)
        self.assertIn("Choose one C. difficile section", rendered)
        self.assertIn("diagnosis", rendered.lower())
        self.assertIn("treatment", rendered.lower())

    def test_cdiff_diagnosis_returns_whole_diagnosis_chunk(self):
        parsed = load_protocol("protocols/cdiff.txt")
        slots = selection_engine.extract_slots_from_query("cdiff diagnosis toxin", parsed_protocol=parsed)
        result = selection_engine.run_selection(parsed, slots, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertEqual(result.output_key, "DIAGNOSIS_CHUNK")
        self.assertIn("More than 3 loose stools", rendered)
        self.assertIn("Contact isolation", rendered)

    def test_cdiff_treatment_returns_whole_treatment_chunk(self):
        parsed = load_protocol("protocols/cdiff.txt")
        slots = selection_engine.extract_slots_from_query("cdiff treatment", parsed_protocol=parsed)
        result = selection_engine.run_selection(parsed, slots, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertEqual(result.output_key, "TREATMENT_CHUNK")
        self.assertIn("NG vancomycin 4x125 mg", rendered)
        self.assertIn("Probiotics are not effective", rendered)

    def test_cdiff_both_chunks_prompts_for_one(self):
        parsed = load_protocol("protocols/cdiff.txt")
        slots = selection_engine.extract_slots_from_query("cdiff diagnosis and treatment", parsed_protocol=parsed)
        result = selection_engine.run_selection(parsed, slots, lang="en")

        self.assertNotIn("cdiff_request_type", slots)
        self.assertTrue(result.default_used)

    def test_iai_complex_nosocomial_selects_meropenem_pathway(self):
        parsed = load_protocol("protocols/intraabdominal_infections.txt")
        slots = selection_engine.extract_slots_from_query(
            "komplex nosocomialis hasuri fertozes reoperacio",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertEqual(result.output_key, "COMPLEX_NOSOCOMIAL")
        self.assertIn("meropenem", rendered.lower())
        self.assertIn("not de-escalation", rendered.lower())

    def test_sbp_prompt_returns_whole_protocol_chunk(self):
        parsed = load_protocol("protocols/sbp.txt")
        result = selection_engine.run_selection(parsed, {}, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertEqual(result.output_key, "WHOLE_SBP")
        self.assertIn("Diagnosis = paracentesis", rendered)
        self.assertIn("ten mL ascites", rendered)
        self.assertIn("ceftriaxone", rendered.lower())

    def test_uti_categorical_slots_select_without_python_special_case(self):
        parsed = load_protocol("protocols/uti.txt")
        slots = selection_engine.extract_slots_from_query(
            "complicated UTI hospitalized nosocomial risk",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")

        self.assertEqual(slots.get("syndrome_class"), "complicated_uti")
        self.assertEqual(result.output_key, "COMPLICATED_HOSPITALIZED_NOSOCOMIAL_RISK")

    def test_ambiguous_same_slot_aliases_do_not_select_uti_pathway(self):
        parsed = load_protocol("protocols/uti.txt")
        slots = selection_engine.extract_slots_from_query(
            "UTI uncomplicated complicated",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")

        self.assertNotIn("syndrome_class", slots)
        self.assertTrue(result.default_used)


class TestPcrProtocolMapping(unittest.TestCase):
    def test_biofire_pn_behavior_preserved_after_protocol_migration(self):
        parsed = load_protocol("protocols/pneumonia_pcr.txt")
        slots = selection_engine.extract_slots_from_query(
            "BioFire PN result: E. coli CTX-M",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")

        self.assertEqual(slots.get("pathogen_list"), ["escherichia coli"])
        self.assertIn("ctx_m", slots.get("resistance_gene_list", []))
        self.assertEqual(result.output_key, "TIER_3_ERTAPENEM")

    def test_ji_pcr_klebsiella_aerogenes_baseline_cefepime(self):
        parsed = load_protocol("protocols/joint_infection_pcr.txt")
        slots = selection_engine.extract_slots_from_query(
            "BioFire JI Klebsiella aerogenes detected",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertEqual(result.output_key, "TIER_2_CEFEPIME")
        self.assertIn("cefepime", rendered.lower())

    def test_ji_pcr_bare_klebsiella_asks_species_because_antibiotic_changes(self):
        parsed = load_protocol("protocols/joint_infection_pcr.txt")
        slots = selection_engine.extract_slots_from_query(
            "BioFire JI Klebsiella detected",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")

        self.assertEqual(result.output_key, "ambiguous_pathogen")
        self.assertIn("Which Klebsiella", result.ask_missing)

    def test_ji_pcr_ctx_m_replaces_gram_negative_backbone_with_meropenem(self):
        parsed = load_protocol("protocols/joint_infection_pcr.txt")
        slots = selection_engine.extract_slots_from_query(
            "BioFire JI E. coli CTX-M",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertEqual(result.output_key, "TIER_3_MEROPENEM")
        self.assertIn("meropenem", rendered.lower())

    def test_ji_pcr_carbapenemase_replaces_backbone_with_meropenem_colistin(self):
        parsed = load_protocol("protocols/joint_infection_pcr.txt")
        slots = selection_engine.extract_slots_from_query(
            "BioFire JI Klebsiella pneumoniae KPC",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertEqual(result.output_key, "TIER_4_MEROPENEM_COLISTIN")
        self.assertIn("meropenem + colistin", rendered.lower())
        self.assertIn("id consultation", rendered.lower())

    def test_ji_pcr_meca_mrej_adds_vancomycin_for_staph_aureus(self):
        parsed = load_protocol("protocols/joint_infection_pcr.txt")
        slots = selection_engine.extract_slots_from_query(
            "BioFire JI Staph aureus mecA/C MREJ",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertEqual(result.output_key, "STAPH_AUREUS_MRSA")
        self.assertIn("vancomycin", rendered.lower())

    def test_ji_pcr_vana_b_adds_linezolid(self):
        parsed = load_protocol("protocols/joint_infection_pcr.txt")
        slots = selection_engine.extract_slots_from_query(
            "BioFire JI Enterococcus faecium VanA/B",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertEqual(result.output_key, "LINEZOLID")
        self.assertIn("linezolid", rendered.lower())

    def test_ji_pcr_enterococcus_faecalis_selects_ampicillin_vancomycin(self):
        parsed = load_protocol("protocols/joint_infection_pcr.txt")
        slots = selection_engine.extract_slots_from_query(
            "BioFire JI Enterococcus faecalis",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertEqual(result.output_key, "AMPICILLIN_VANCOMYCIN")
        self.assertIn("ampicillin + vancomycin", rendered.lower())

    def test_ji_pcr_anaerobe_returns_anaerobe_guidance(self):
        parsed = load_protocol("protocols/joint_infection_pcr.txt")
        slots = selection_engine.extract_slots_from_query(
            "BioFire JI Bacteroides fragilis",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertEqual(result.output_key, "ANAEROBE_GUIDANCE")
        self.assertIn("metronidazole", rendered.lower())

    def test_ji_pcr_nosocomial_iai_context_appends_no_narrowing_note(self):
        parsed = load_protocol("protocols/joint_infection_pcr.txt")
        slots = selection_engine.extract_slots_from_query(
            "BioFire JI E. coli nosocomial intraabdominal",
            parsed_protocol=parsed,
        )
        result = selection_engine.run_selection(parsed, slots, lang="en")
        rendered = selection_engine.render_selected_output(parsed, result, lang="en")

        self.assertEqual(slots.get("pcr_context"), "nosocomial_intraabdominal")
        self.assertIn("do not narrow below meropenem", rendered.lower())


if __name__ == "__main__":
    unittest.main()
