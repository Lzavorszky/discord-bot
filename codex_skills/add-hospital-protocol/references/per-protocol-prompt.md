# Per-Protocol Prompt Template

Use this prompt when adding one new protocol.

```text
Use the add-hospital-protocol workflow for this bot.

Source protocol:
<paste or attach the raw source protocol here>

Known local context, if any:
- desired protocol_id:
- desired source_label:
- known aliases/local names:
- linked protocols that already exist:
- linked protocols that are expected but missing:
- exact footer, if required:

First, do not draft the final protocol. Inspect the source and produce a short design summary:
1. protocol_type
2. answer_mode
3. selection_mode
4. aliases and alias categories
5. required input slots
6. whether default answer is allowed
7. deterministic outputs Python may select
8. modifiers that affect selection
9. modifiers that belong only in footer/safety notes
10. INFO_BLOCKS topics
11. needed LINKS and target_missing_behavior
12. exact DEFAULT_FOOTER

Ask concise questions for anything needed to avoid guessing. Do not invent doses, antibiotic choices, tiers, required inputs, target protocols, or footer text.

After the design summary is resolved, draft the final protocol in this exact panel order:

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

Use `(none)` for intentionally empty panels.

Before accepting the draft, run the contradiction check:
- deterministic decisions only in SELECTION_RULES and SELECTED_OUTPUTS
- general explanatory material only in INFO_BLOCKS
- no invented doses, antibiotic choices, tiers, or required inputs
- non-dosing protocols link to dosing protocols instead of providing doses
- every missing linked protocol has target_missing_behavior
- metadata flags match content
- selected outputs are listed and reachable
- aliases are not overbroad or collision-prone
- footer is exact
```

Division of responsibility:
- The skill stores durable process, panel semantics, safety boundaries, example-selection guidance, and validation checks.
- Each per-protocol prompt supplies raw source material and local facts: desired ID, source label, known aliases, known links, missing-link behavior, and exact footer.
