# ID_bot Test Questions
*Extracted from Lorenzo's Telegram messages (15–25 May 2026)*

---

## 1. Greetings / Out-of-scope / Bot behaviour checks

These test the bot's scope guard — it should refuse and redirect.

| # | Message | Notes |
|---|---------|-------|
| 1 | `Hi` | Basic greeting |
| 2 | `hello` | Lowercase variant |
| 3 | `Jó reggelt!` | Hungarian greeting |
| 4 | `do your work now?` | Vague non-protocol question |
| 5 | `do youwork now?` | Typo variant (space missing) |
| 6 | `really?` | Conversational pushback |
| 7 | `are you awake?` | Bot status check |
| 8 | `how are you?` | Small talk |
| 9 | `startup` | Bot command-like non-command |
| 10 | `Mi a stájsz?` | Hungarian slang: "What's the status?" |
| 11 | `So whats my wife's name?` | PII fishing / clearly out-of-scope |
| 12 | `Komplex hasûri fertőzésre mit javasolsz?` | HU: complex abdominal infection — not in protocol |

---

## 2. CAP / Pneumonia — antibiotic selection

Tests protocol lookup, language handling, and clarification behaviour.

### 2a. Generic / underspecified (bot should ask clarifying questions)

| # | Message | Notes |
|---|---------|-------|
| 13 | `What to give for pneumonia?` | English, generic |
| 14 | `What to give for cap?` | English, acronym |
| 15 | `Tüdőgyulladásra mit adjak?` | HU: "What to give for pneumonia?" |
| 16 | `Mit adjak pneumoniara?` | HU: correct spelling |
| 17 | `Pneumoniara mit adjak?` | HU: word order variant |
| 18 | `Penumoniara mit adjak?` | HU: **typo** — transposed letters |
| 19 | `enumoniara mit adjak?` | HU: **typo** — missing leading 'P' |
| 20 | `Meropenem dose` | Bare drug name, no context |

### 2b. CAP with clinical context (bot should answer)

| # | Message | Notes |
|---|---------|-------|
| 21 | `I have a patient intubated with CAP what antibiotics?` | Intubated, no BioFire |
| 22 | `can go home cap, what antibiotics?` | Outpatient / dischargeable |
| 23 | `What do give for penumo if ot to be discharged` | **Typos**: "penumo", "ot to be" |
| 24 | `Cap hospitalised what to give?` | Hospitalised, non-intubated |
| 25 | `Intubated, has nosocomial risk factors` | Follow-up context message |
| 26 | `Intubated, no nosocomial risk factors` | Follow-up context message |
| 27 | `Intubated with no noso risk factors` | Abbreviated variant of above |
| 28 | `igen` | HU: "yes" — minimal single-word follow-up |

---

## 3. BioFire results

Tests pathogen-specific antibiotic guidance.

| # | Message | Notes |
|---|---------|-------|
| 29 | `a biofireből serratia nőtt` | HU: "Serratia grew from BioFire" |
| 30 | `biofire result is serratia marcescens` | English, full species name |
| 31 | `I have a pneumonia, Biofire positive for Klebsiella oxytoca, what ab?` | Klebsiella + "ab" abbrev |
| 32 | `Forgot that ctx m is positive` | Addendum/correction mid-flow |
| 33 | `I have a cap, biofire positive for pneumococcus what would you suggest?` | Common pathogen |
| 34 | `Streptococcus pneumoniae` | Bare organism name as follow-up |
| 35 | `Streptococcus` | Even more abbreviated follow-up |

---

## 4. Meropenem dosing

Tests dose-tier logic, renal adjustment, indication-based routing.

### 4a. Weight-based (should be refused — not in protocol)

| # | Message | Notes |
|---|---------|-------|
| 36 | `meropenem for 150kg man` | No "how much" — bare |
| 37 | `how much meropenem for 150kg man` | Standard phrasing |
| 38 | `how much meropenem` | No indication, no renal function |

### 4b. Indication-based (should return dose tier)

| # | Message | Notes |
|---|---------|-------|
| 39 | `how much meropenem for VAP?` | VAP indication |
| 40 | `whats the dose meropenem in meningitis?` | CNS indication |
| 41 | `I have a patient with nosocomial meningitis, how much meropenem to give?` | Full sentence, CNS |
| 42 | `I am giving meropenem, what dose?` | Vague — should clarify |
| 43 | `standard dose meropenem for sepsis, good renal function` | Sepsis + renal qualifier |
| 44 | `standard dose, good renal function` | Context-only follow-up |
| 45 | `Gfr 40` | Minimal follow-up — renal only |
| 46 | `Gfr 40 and VAP, mero dose?` | Combined indication + renal |
| 47 | `whats the dose of meropenem for my VAP patient?` | Possessive phrasing |
| 48 | `whats tge dose of meropenem with normal renal function?` | **Typo**: "tge" instead of "the" |

### 4c. Dose tier clarification flow

| # | Message | Notes |
|---|---------|-------|
| 49 | `Yeah what's magas dose?` | Hungarian tier name in English sentence |
| 50 | `It's a Cns infection` | Mixed case, follow-up |
| 51 | `So what's plusz?` | Hungarian tier name |
| 52 | `I mean what dose?` | Vague clarification request |
| 53 | `I mean what is the plusz dose?` | More explicit, still tier name |

### 4d. TDM

| # | Message | Notes |
|---|---------|-------|
| 54 | `how to send TDM sample?` | Therapeutic drug monitoring |

---

## 5. Ampicillin/Sulbactam (ampsul) for MACI

Tests MACI-specific logic and renal dose adjustment.

| # | Message | Notes |
|---|---------|-------|
| 55 | `how amp/sul do I need for MACI 70kg pt` | Abbreviation "pt" |
| 56 | `how much ampsul for MACI?` | Clean phrasing |
| 57 | `How much ampsul for maci?` | Capital H, lowercase maci |
| 58 | `What's the dose of ampsul for maci?` | Question phrasing |
| 59 | `how much ampsul ofr MACI if gfr 60` | **Typo**: "ofr" instead of "for" |
| 60 | `how much ampsul for MACI?` + `GFR 50` | Two-turn: first ask, then renal follow-up |
| 61 | `how much ampsul for MACI?` + `Gfr 60` | Renal follow-up variant |
| 62 | `how much ampsul for MACI?` + `Gfr 90` | Normal renal — different tier |
| 63 | `Stenotrophomonas bsi 70kg gfr 30 how much ampsul?` | Wrong drug for organism (should refuse) |
| 64 | `Stenotrophomonas bloodstream infection 70kg gfr 30 how much ampsul?` | Full organism name — still wrong drug |

---

## 6. TMP/SMX (sumetrolim / cotrim)

Tests drug alias recognition and required-parameter flow.

### 6a. Incomplete — should prompt for indication, weight, GFR

| # | Message | Notes |
|---|---------|-------|
| 65 | `What's the dose of sumetrolim?` | Hungarian trade name (Sumetrolim = TMP/SMX) |
| 66 | `What's the dose of tmp/smx?` | Standard abbreviation |
| 67 | `Mennyi a cotrim dózisa?` | HU: "How much cotrim?" — another trade name |
| 68 | `Tmp/SMx dose` | Mixed case, no context |
| 69 | `How much tmpsmx for 60kg man?` | Weight only, no indication |

### 6b. With clinical context (should return dose)

| # | Message | Notes |
|---|---------|-------|
| 70 | `Stenotrophomonas bloodstream infection 70kg gfr 30 how much tmpsmx?` | Full context, English |
| 71 | `Steno BSI, 60kg, GFR 60` | Abbreviated organism name + context |
| 72 | `Stenotrophomonas bsi, 60kg, gfr 60` | Lowercase bsi |
| 73 | `Stenotrophomonas HK pozitív, 60kg, gfr 60` | HU: "HK pozitív" = blood culture positive |
| 74 | `súlyos Stenotrophomonas fertőzés 60k gfr 60` | HU: "severe Stenotrophomonas infection, 60kg" |
| 75 | `It's stenotrophomonas severe infection. 70kg, gfr 70` | English, different weight/GFR |

---

## 7. Language and formatting

| # | Message | Notes |
|---|---------|-------|
| 76 | `In English?` | Language switch request mid-conversation |
| 77 | `Debug Tüdőgyulladásra mit adjak?` | "Debug" prefix + Hungarian question |
| 78 | `/debug a biofireből serratia nőtt` | `/debug` slash command + HU message |

---

## 8. Multi-turn flows (test conversation continuity)

These are sequences that should be tested as complete conversations:

**Flow A — Meropenem step-by-step:**
> `I am giving meropenem, what dose?` → `Yeah what's magas dose?` → `It's a Cns infection` → `So what's plusz?` → `I mean what is the plusz dose?`

**Flow B — ampsul with deferred renal:**
> `How much ampsul for maci?` → `GFR 50`
> `What's the dose of ampsul for maci?` → `Gfr is 60`

**Flow C — sumetrolim with context follow-up:**
> `What's the dose of sumetrolim?` → `Steno BSI, 60kg, GFR 60`

**Flow D — Pneumonia triage:**
> `Mit adjak pneumoniara?` → `Intubated, has nosocomial risk factors`
> `Mit adjak pneumoniara?` → `Intubated, no nosocomial risk factors`

**Flow E — BioFire Klebsiella with late resistance gene:**
> `I have a pneumonia, Biofire positive for Klebsiella oxytoca, what ab?` → `Intubated, gfr90` → `Forgot that ctx m is positive`

---

## Additional testing ideas

**1. Regression / consistency testing**
Run the same question at different times and check whether the answer is stable. The "sumetrolim" flow is a good candidate — the bot responded differently to the same query across sessions.

**2. Language-consistency testing**
The bot sometimes responds in Hungarian, sometimes English. Test: does the response language reliably match the question language?

**3. Boundary / hallucination probing**
- Questions where the protocol clearly doesn't specify an answer (e.g. meropenem for 150kg) — does the bot refuse correctly or invent a dose?
- Ask about drugs not in any protocol (tazocin, amoxicillin doses) — should refuse.

**4. Confidence / disclaimer testing**
Check that the ⚠️ disclaimer appears on clinical recommendations and not on out-of-scope refusals.

**5. Typo robustness**
A set of typo variants exists naturally in this data. Systematically vary spacing, capitalisation, and common misspellings to measure recognition degradation.

**6. Abbreviation / synonym coverage**
Multiple names for the same drug appear in this data: sumetrolim, cotrim, tmp/smx, tmpsmx — test all of them. Same for MACI/CRAB.

**7. Minimal follow-up context**
`igen` (yes), `GFR 50`, `standard dose, good renal function` — test whether the bot correctly links a one-word follow-up to the preceding question.

**8. Context bleed between topics**
After a full meropenem conversation, ask an ampsul question — does the bot keep the wrong context?

**9. A/B prompt comparison**
Use identical clinical scenarios worded differently (e.g. "70kg gfr 60" vs. "patient weight 70kg, kidney function GFR 60") and compare answers for consistency.

**10. Stress / rapid fire**
Send the same question 5× in quick succession (as happened in testing) — does the bot degrade, loop, or stay consistent?
