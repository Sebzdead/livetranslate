---
name: glossary-builder
description: Build or extend the livetranslate glossary.tsv from a presenter's notes, slides, abstract, or talk transcript. Extracts ASR-critical terms (people, places, organizations, acronyms, technical/field-specific jargon) and translates each into the session's target languages. Use this whenever the user provides speaker notes, a paper, slides, or any talk material and wants glossary terms extracted, keyterms prepared, or the glossary updated/translated — even if they don't say "glossary" (e.g. "get the jargon out of this talk", "prep the keyterms for Saturday's speaker").
---

# Glossary Builder

Turn presenter material into rows of `glossary.tsv` — the single file that drives both
ASR keyterm boosting and translation-prompt glossary enforcement in the livetranslate
pipeline. A good glossary is the highest-leverage accuracy input this system has: the
ASR vendor boosts recognition of `term_src`, and the translator is *required* to use the
target renderings. A bad row is worse than no row (it forces a wrong rendering on every
occurrence), so precision beats recall here.

## Output format (exact)

UTF-8 TSV with real tab characters, this exact header:

```
term_src	es	fr	de	pt	ar	zh	priority	notes
```

- `term_src` — the term as the speaker will *say* it.
- Language columns — required rendering in that language. **An empty cell means "keep
  the source term untranslated"** (the pipeline treats empty as identity). A cell equal
  to the source term means the same thing and is also fine.
- `priority` — `1` = must-recognize (the talk's core jargon, the speaker's name, terms
  the ASR will likely fumble); `2` = nice-to-have (well-known places, common org names).
  Priority drives keyterm ordering when the vendor cap truncates the list.
- `notes` — optional; use for review hints like `canonical rendering only` or `verify
  with speaker`.

## Workflow

### 1. Determine target languages

Read `[translate].targets` from `config.toml` in the project root (default
`["es", "fr", "de", "pt"]`). Fill **only** those columns; leave the others empty.
If config.toml is missing, ask which languages to fill rather than guessing.

### 2. Extract candidate terms from the notes

Read the provided material and collect terms in these categories:

- **People** — speakers, cited authors, historical figures. Include the form actually
  spoken ("Rosa Luxemburg", not "Luxemburg, R.").
- **Places** — cities, regions, institutions-as-places, especially ones with diacritics
  or non-English spelling the ASR will mangle (Tübingen, São Paulo).
- **Organizations** — institutes, journals, parties, companies.
- **Acronyms & abbreviations** — expand in `notes` if the expansion appears in the
  source (`notes: = Organisation for Economic Co-operation...`). If the speaker likely
  *says* the letters ("O-E-C-D"), the acronym is the term; if they say the expansion,
  list the expansion too.
- **Technical / field-specific terminology** — multi-word phrases the field treats as
  fixed expressions ("rate of profit", "primitive accumulation"). These are the most
  valuable rows: they have canonical translations that a generic MT would get wrong.

Filtering — be selective, not exhaustive:
- Skip everyday words the ASR and translator already handle ("economy", "crisis",
  "Europe", "Marx" alone is fine — but "Grundrisse" is not).
- Skip terms appearing only in references/bibliography that won't be spoken.
- Target roughly 40–80 rows for a full talk; a short abstract may only justify 10–20.
- The speaker says these words aloud: prefer spoken forms, drop citation formatting,
  page numbers, formula symbols.

### 3. Assign priorities

`priority 1`: core jargon of this specific talk, names central to the argument, anything
with unusual phonetics or spelling. `priority 2`: context terms, famous entities the ASR
probably gets right anyway. The ElevenLabs realtime keyterm budget is **50 terms, max
20 characters each** (longer terms still help the MT glossary but won't be sent as
keyterms) — so the ~50 most ASR-critical terms should be priority 1, and very long
phrases should get a short priority-1 sibling row when there's a natural short form.

### 4. Translate

Translate each term into each target language yourself, applying these rules:

- **Proper nouns (people, most places, orgs)**: usually keep as-is → leave the cell
  empty. Exceptions: places with established exonyms (Munich → Múnich/Munich/München/
  Munique), org names with official translations (use the org's own published name in
  that language, nothing invented).
- **Technical terms**: use the *canonical* rendering established in that language's
  literature of the field, not a fresh literal translation. If the field is academic,
  the canonical rendering is the one used in the standard translated editions (e.g.
  Marx's "ursprüngliche Akkumulation" → es "acumulación originaria", NOT "acumulación
  primitiva"... unless the regional literature prefers it — when two renderings genuinely
  compete, pick one, and flag it in `notes` for review).
- **Acronyms**: keep the acronym unless the target language uses a different official
  one (NATO → OTAN in es/fr/pt).
- **When unsure, leave the cell empty** (= keep source) and add `notes: verify`. An
  empty cell is safe; a wrong rendering is enforced on every sentence.

### 5. Merge into glossary.tsv

Use the bundled script — it validates format, dedupes, and never clobbers existing rows:

```bash
python .claude/skills/glossary-builder/scripts/merge_glossary.py \
    --existing glossary.tsv --new <your-new-rows.tsv> --out glossary.tsv
```

Write your extracted rows to a temp TSV first, then merge. Rules the script enforces:
existing `term_src` rows always win (the owner's hand corrections are sacred); new rows
are appended; duplicates within the new file are collapsed; output is sorted by
priority then term. It prints added/skipped counts and warns if priority-1 terms exceed
the 50-term ElevenLabs keyterm budget.

If the user asked for a draft instead of a merge, write `glossary-draft.tsv` next to
their notes and skip the merge.

### 6. Report for human review

End with a summary the owner can review in one glance:

```
Added 47 terms (12 people, 6 places, 5 orgs, 9 acronyms, 15 jargon), skipped 3 already present.
Priority 1: 38 (fits the 50-term keyterm budget; longest sent keyterm: 19 chars)
Flagged for review (4): ...rows with `notes: verify`...
```

Always remind the owner: per the project spec, translation renderings must be human-
reviewed before each event — especially the `verify`-flagged rows. Suggest running
`python -m harness.run_file` on a recording afterward to confirm jargon recall.
