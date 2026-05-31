---
name: add-hospital-protocol
description: Create or refactor hospital protocol bot files from raw source protocols. Use when Codex needs to turn clinical source material into this bot's canonical protocol structure, decide protocol_type, answer_mode, selection_mode, aliases, input slots, selected outputs, links, INFO_BLOCKS, safety boundaries, footer behavior, and validate contradictions before writing or accepting a protocol file.
---

# Add Hospital Protocol

Use this skill to convert raw hospital source protocols into this bot's canonical protocol file format.

Primary specification:
- Read `protocol structure guide.txt` first.
- Inspect relevant examples in `protocols/`, especially drug dosing, syndrome pathway, microbiology interpretation, and info-only protocols.
- Prefer current parser/linter constants over stale examples if they conflict.

Canonical panel order for every final protocol:

```text
# TITLE

## METADATA

## ALIASES

## INTENTS

## INPUT_SLOTS

## DEFAULT_ANSWER

## SELECTION_RULES

## SELECTED_OUTPUTS

## LINKS

## INFO_BLOCKS

## RESTRICTED_OUTPUTS

## SAFETY_RULES

## OUTPUT_TEMPLATES

## DEFAULT_FOOTER
```

Use `(none)` for any intentionally empty panel.

## Workflow

1. Read the raw source protocol and identify explicit source facts only.
2. Read `protocol structure guide.txt`.
3. Inspect `protocols/aliases.json`, including `drugs`, `conditions`, `unsupported_syndromes`, and legacy `blocked_aliases`.
4. Inspect 2-4 closest existing files in `protocols/`:
   - drug dosing: `meropenem.txt`, `tmpsmx.txt`, `ampsul.txt`
   - pathway: `cap.txt`
   - microbiology: `pneumonia_pcr.txt`
   - info-only: `general_rules_antibiotic_dosing.txt`
5. Before drafting final protocol text, decide whether any required design facts are unclear.
6. Ask concise questions for only the missing design facts. Do not ask about facts already explicit in the source.
7. Draft a design summary first:
   - protocol type
   - answer mode
   - selection mode
   - aliases and alias categories
   - whether any alias/term currently appears in `unsupported_syndromes` or legacy `blocked_aliases`
   - required and optional input slots
   - whether a default answer is allowed
   - deterministic outputs
   - modifiers that affect selection
   - modifiers that belong only in footer or safety notes
   - INFO_BLOCKS topics
   - links and target_missing_behavior
   - default footer text
8. Draft the protocol file only after the design summary has no unresolved clinical facts.
9. If this new protocol makes a formerly unsupported syndrome/test supported, update `protocols/aliases.json` in the same change:
   - add supported aliases under `drugs` or `conditions` as appropriate
   - remove or narrow matching terms from `unsupported_syndromes`
   - keep unrelated unsupported policy entries
   - keep legacy `blocked_aliases` compatible unless deliberately migrating/removing duplicates
10. Run contradiction checks before accepting the draft.
11. If a file is created or edited, run the linter:

```powershell
python -m protocol_linter
```

12. If aliases should be synchronized after the final protocol is approved, run:

```powershell
python alias_sync.py
```

## Required Questions

Ask only questions needed to avoid unsafe guessing. Keep them short and grouped.

Ask about:
- protocol type: drug dosing, syndrome pathway, microbiology interpretation, diagnostic protocol, monitoring protocol, general rules
- answer mode: `default_then_selected_output`, `required_slots_then_selected_output`, `tree_then_selected_output`, `info_only`
- selection mode: `none`, `priority_rules`, `table_lookup`, `decision_tree`, `organism_mapping_with_spectrum_escalation`
- aliases: local names, abbreviations, brand names, Hungarian forms, common typos
- required input slots before selection or dosing
- whether default answer is allowed when required details are missing
- exact deterministic outputs Python may select
- which modifiers select an output vs only modify footer or safety notes
- which general information belongs in INFO_BLOCKS
- whether links to other protocols are needed
- target_missing_behavior for each missing linked protocol
- exact default footer, if any

Do not ask the user to confirm invented doses, tiers, antibiotic choices, or clinical decisions. If the source lacks them, mark them missing and block finalization.

## Panel Rules

`METADATA`
- Include `protocol_id`, `protocol_name`, `source_label`, `canonical_name`, `protocol_type`, `answer_mode`, `selection_mode`, `allows_dosing`, `dosing_requires_link`, `default_dose_allowed`.
- Include governance fields used by examples: `version`, `last_reviewed`, `owner`, `status`.

`ALIASES`
- Keep syndrome aliases separate from organism or platform aliases when needed.
- For microbiology protocols, use category labels such as `platform_aliases`, `organism_aliases`, and `resistance_gene_aliases`.
- Avoid broad aliases that would steal unrelated queries.

`protocols/aliases.json`
- Supported runtime routing comes from central `drugs` and `conditions` entries.
- Unsupported syndrome/test policy lives in `unsupported_syndromes`, with `terms`, `message`, and `allowed_if_explicit_drug`.
- Before adding VAP, HAP, JI PCR, immunosuppressed pneumonia, or any previously unsupported item as a supported protocol, remove or narrow the corresponding unsupported policy terms; otherwise runtime will still block unsupported-only queries.
- Do not silence a real collision with `allow_supported_alias_collision: true` unless the user explicitly wants a temporary transitional state. Normal supported-protocol additions should eliminate the collision.
- Legacy `blocked_aliases` remain backward-compatible fallback policy terms, so check them too.

`INTENTS`
- Use `default_request`, `selection_request`, `dosing_request`, `info_request`, `link_request`.
- Deterministic intents route to Python selection. Info intents route to INFO_BLOCKS. Link intents route to LINKS.

`INPUT_SLOTS`
- Put required facts under `required_for_selection` and `required_for_dosing`.
- Put nonselecting context under `optional_modifiers`.
- Put link-transfer data under `forwardable_slots`.

`DEFAULT_ANSWER`
- Use only when broad queries may safely return a quick map or "send required inputs" prompt.
- Do not put default patient-specific dosing here unless the source explicitly allows it and `default_dose_allowed: yes`.

`SELECTION_RULES`
- Put all deterministic clinical decision logic here.
- Use simple priority rules or table lookup unless a real multi-turn decision tree is necessary.
- Do not leave polymicrobial escalation, resistance-marker handling, dose tiering, or treatment-pathway selection to the LLM.

`SELECTED_OUTPUTS`
- List exact outputs Python may select.
- The LLM must not invent outputs outside this panel.
- Doses, tiers, antibiotic choices, and required inputs must come only from source material.

`LINKS`
- Use links when a protocol names a drug, test, or other protocol whose details belong elsewhere.
- Non-dosing protocols must link to dosing protocols instead of providing doses.
- Every missing target must include `target_missing_behavior`.
- Include transfer slots such as renal function, weight, indication, severity, organism, allergy, and selected antimicrobial when relevant.

`INFO_BLOCKS`
- Put bounded, source-supported explanatory material here.
- Use for toxicity, monitoring, administration, diagnostic limitations, interpretation notes, escalation triggers, and stewardship notes.
- Do not put deterministic selection or dosing decisions here.

`RESTRICTED_OUTPUTS`
- State hard never rules, especially no invented dosing, no alternatives, no unsupported monitoring or toxicity management, no dosing from non-dosing protocols.

`SAFETY_RULES`
- State priority clarifications, conflict behavior, missing-input behavior, and cross-protocol handoff rules.
- Put reminders such as "store GFR and transfer through LINKS, do not use it here" when relevant.

`OUTPUT_TEMPLATES`
- Include concise templates for selected outputs, missing inputs, link missing behavior, and info answers.

`DEFAULT_FOOTER`
- Append exact source-supported reminders that should appear with every answer from this protocol.
- Use `(none)` if no footer is required.

## Safety Boundaries

- Deterministic clinical decisions belong only in `SELECTION_RULES` and `SELECTED_OUTPUTS`.
- General LLM-answerable material belongs only in `INFO_BLOCKS`.
- Never invent doses, dose adjustments, antibiotic choices, tiers, required inputs, resistance interpretation, toxicity management, duration, pregnancy advice, or allergy alternatives.
- Non-dosing protocols must not provide doses; they must link to dosing protocols.
- Missing linked protocols require explicit `target_missing_behavior`.
- Resistance markers must not be interpreted without a detected pathogen unless the source explicitly says otherwise.
- Pathogen aliases must not route to syndrome protocols just because of substring overlap.
- If the source and existing examples conflict, surface the contradiction and ask before finalizing.

## Contradiction Check

Before accepting the draft, verify:
- every canonical panel exists and appears in order
- metadata flags match panel content
- `answer_mode` and `selection_mode` are valid for the current parser
- `default_dose_allowed: no` has no dose-like default answer
- `allows_dosing: no` has no dose-like selected outputs except link text
- every selected output is reachable from selection rules or intentionally link-only/info-only
- every deterministic rule selects an output listed in `SELECTED_OUTPUTS`
- every named drug in a non-dosing protocol has a link or an explicit reason it is not linked
- every missing linked protocol has `target_missing_behavior`
- INFO_BLOCKS contain no deterministic dose/pathway/tier decisions
- RESTRICTED_OUTPUTS block likely unsafe requests
- footer text is exact and short
- aliases do not collide or overmatch
- supported aliases do not collide with `unsupported_syndromes` unless explicitly allowed for a temporary transition
- any newly supported formerly-unsupported syndrome/test no longer has active unsupported policy terms that would block it
- polymicrobial, resistance, renal, RRT, weight, and severity logic is deterministic when it affects decisions

## Per-Protocol Prompt

Use `references/per-protocol-prompt.md` as the starting prompt for each future source protocol. Keep protocol-specific facts in the user prompt or attached source. Keep durable process and safety rules in this skill.
