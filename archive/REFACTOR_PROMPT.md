# Prompt: standardize protocol schema + add decision-tree support

Paste the section below into a fresh Cowork/Claude session that has access to
`G:\My Drive\0100\ID_bot\ID_bot_1\`. The agent will propose a design first, wait
for your approval, then implement.

---

## Briefing

You are working on a hospital protocol Telegram bot located at
`G:\My Drive\0100\ID_bot\ID_bot_1\`. It is a Python app deployed on Railway.
The bot answers clinical questions in Hungarian/English using RAG over flat
`.txt` protocol files. Each user query goes through:

1. **Alias recognition** (`normalize_question` in `telegram_bot.py`) — maps
   user terms ("sumetrolim", "penumoniara") to canonical drug/condition via
   `protocols/aliases.json`. Already uses a 3-step cascade: exact substring →
   per-word fuzzy → whole-text partial_ratio. Do not regress this.
2. **Semantic retrieval** — embeds the (alias-tagged) query, picks top-k
   chunks across all protocol files using cosine similarity.
3. **Policy-header injection** (`extract_policy_header` /
   `PROTOCOL_POLICY_BY_FILE`) — when an alias hit identifies a specific
   protocol file, the file's `## ANSWER_POLICY`, `## DEFAULT_QUESTION`,
   `## REQUIRED_INFORMATION`, `## PATHWAY_PRIORITY` sections are *always*
   prepended to the LLM context so they don't get crowded out by treatment
   chunks. Do not regress this either.
4. **LLM call** (`gpt-4o-mini`) with a system prompt assembled from
   `system_rules.txt`, `answer_format_rules.txt`, `answer_style_rules.txt`,
   `safety_rules.txt`, plus the retrieved + policy context.
5. **Post-processing** (`clean_response`) strips markdown, removes
   LLM-generated source lines, appends the canonical `Source: <label>` line.

Conversation state today is minimal: `CONVERSATION_STATE[chat_id]` keeps a
message history and a single `active_recognized` protocol pointer. There is
no concept of "where am I in this protocol's decision tree".

## The three problems to solve

### 1. Inconsistent protocol structure

`cap.txt` is well-structured with `## ANSWER_POLICY`, `## DEFAULT_QUESTION`,
`## REQUIRED_INFORMATION`, `## PATHWAY_PRIORITY`, etc.
`tmpsmx.txt`, `meropenem.txt`, `ampsul.txt`, `pneumonia_pcr.txt`,
`general_rules_antibiotic_dosing.txt` each have different sections in
different orders. The policy-header extractor only finds sections that
happen to exist. This means rules I write for one protocol silently don't
apply to others.

I want a **fixed panel schema** — every protocol file has the same named
sections in the same order, even if some are intentionally empty (write
`(none)` rather than omit). The LLM and the Python code should both be able
to assume the panels are present.

### 2. No decision-tree support

Some protocols (MACI sulbactam MIC pathway, EuroSCORE II, BioFire result
interpretation) are not "collect 3 params then answer". They're multi-round
trees where the next question depends on the previous answer. The bot today
is single-round and dumps everything once it has the required params.

I want **stateful multi-turn decision trees** for the protocols that need
them, while keeping simple flat-collection behavior for protocols like
TMP/SMX dosing.

### 3. Per-protocol default footer

Some protocols have a fixed reminder that should ALWAYS append to any answer
about that protocol — e.g., meropenem: "Minden meropenem 24-órás folyamatos
infúzióban." Today this is sometimes in the LLM answer, sometimes missing,
sometimes mangled.

I want each protocol to declare a **default footer** that the Python code
always appends after `clean_response`, before the `Source:` line.

## What I want you to deliver

In this order, in one response, before writing any code:

### A. Proposed fixed panel schema

A canonical list of section headers (e.g., `## METADATA`,
`## ANSWER_POLICY`, `## REQUIRED_INFORMATION`, `## PREFERRED_INFORMATION`,
`## MODIFIER_INFORMATION`, `## DEFAULT_QUESTION`, `## PATHWAY_PRIORITY`,
`## DECISION_TREE`, `## TREATMENT_PATHWAYS`, `## SAFETY_NOTES`,
`## DEFAULT_FOOTER`) with:
- The exact name and order
- For each: one-sentence semantics, when to use vs leave empty, and whether
  the Python code parses it or only the LLM sees it
- Justify any departures from what `cap.txt` and `system_rules.txt` already
  use — be conservative, prefer continuity

### B. Decision-tree representation

How the `## DECISION_TREE` section is written in a `.txt` protocol so it's
human-readable AND machine-parseable. A simple YAML-ish or numbered-step
format is fine — avoid full YAML if it makes the txt file uglier. Show one
worked example for a MACI-style "is sulbactam MIC available? → yes/no →
next question" tree (you can invent the medical content; structure is what
matters).

### C. Conversation state model

How `CONVERSATION_STATE[chat_id]` extends to track:
- which protocol is active (already there)
- current node ID in the decision tree (if any)
- accumulated answers / collected parameters
- how the state is reset (`/reset` clears it; what else triggers a reset?)
- persistence: in-memory is acceptable for now (Railway hobby tier resets on
  redeploy); call out what would need to change for Redis/SQLite later

### D. Code-change outline

List, by function, the changes to `telegram_bot.py`:
- Schema parser: extends `extract_policy_header` to a full
  `parse_protocol_file` that returns a dict of all panels
- Decision-tree dispatcher: where it sits in `ask_ai`, how it short-circuits
  the LLM call when the tree dictates the next question deterministically,
  vs when it delegates to the LLM
- Footer injection: where in `clean_response` or after it
- Backward compatibility: old-style protocols (anything missing the new
  panels) must still work — define the fallback

### E. Per-file migration plan

For each existing file in `G:\My Drive\0100\ID_bot\ID_bot_1\protocols\`
(`cap.txt`, `tmpsmx.txt`, `ampsul.txt`, `meropenem.txt`, `pneumonia_pcr.txt`,
`general_rules_antibiotic_dosing.txt`), list the specific section additions
or reorderings needed. Identify which protocols stay flat-collection and
which become trees.

### F. Test plan

New cases to add to `test_bot.py`:
- Schema parser returns all panels for a fully-populated file
- Schema parser tolerates missing panels (backward compat)
- Decision-tree dispatcher walks a multi-step example correctly
- Footer is always appended
- Flat protocols (TMP/SMX) behave exactly as today

## Constraints

- Hungarian + English in user messages and answers — keep working.
- Do not break existing fuzzy matching or policy-header injection.
- Keep `.txt` files human-readable (clinicians edit them directly, no IDE).
- No new external services. In-memory state is fine; persistence is a later
  concern.
- Keep the Bálint-style answer voice (short, sharp, no chatty disclaimers)
  defined in `answer_style_rules.txt`.
- LLM model and embeddings stay on `gpt-4o-mini` / `text-embedding-3-small`.

## Process

1. Read `telegram_bot.py`, `system_rules.txt`, `answer_format_rules.txt`,
   `answer_style_rules.txt`, `safety_rules.txt`, and every file in
   `protocols/` before designing anything.
2. Produce sections A–F above as a single proposal. Don't write code yet.
3. Stop and wait for me to approve, push back, or amend.
4. Once approved, implement in this order: schema parser → state model →
   tree dispatcher → footer → migrate one protocol end-to-end as a
   reference → migrate the rest → add tests → run fast tests.
5. After each step, show the diff and pause for confirmation. I'd rather
   you go slow and check than charge ahead.
