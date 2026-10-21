# PDF parser bake-off

**Question:** which PDF parser should the ingest Lambda use?

**Answer:** pypdf. On ten two-column neuroscience articles it recovers 92.0% of ground-truth sentences at a 0.95 similarity threshold, against 7.9% for pdfplumber, and extracts roughly three times faster. pdfplumber's default extraction reads straight across the two-column layout and interleaves the columns line by line.

## Method

**Corpus.** Ten *Frontiers in Neuroscience* articles from the PMC Open Access Subset, selected from a 2020–2025 search window, CC BY licensed: eight research articles, one case report, one review. [`corpus/manifest.csv`](../corpus/manifest.csv) lists each article's versioned ID and the SHA-256 of both files; [`corpus/query.json`](../corpus/query.json) records the search and filters.

**Ground truth.** Each article ships with JATS XML, the publisher's structured text. [`src/bakeoff/jats.py`](../src/bakeoff/jats.py) extracts the main abstract and body, excluding references, figures, tables, formulas, acknowledgements and footnotes. Before any parser was scored, [`scripts/inspect_ground_truth.py`](../scripts/inspect_ground_truth.py) checked the extraction on the real articles; it found and led to fixes for three defects (lost text after pruned elements, spaces inserted into inline markup, and an unpaired quotation mark that merged 30% of one article into a single sentence).

**Metrics** ([`src/bakeoff/fidelity.py`](../src/bakeoff/fidelity.py)). Both sides are unwrapped, split into sentences with pysbd, and normalised identically. A ground-truth sentence counts as recovered if some extracted sentence reaches the similarity threshold (`difflib` ratio).

- **Recovery** at 0.90, 0.95 and 0.99: the fraction of ground-truth sentences found.
- **Order:** Spearman correlation between ground-truth order and matched position, over matched sentences only. It is meaningless without recovery beside it.

**Settings.** Both parsers ran with default settings; tuning one and not the other would bias the comparison. Extraction time is the median of three runs.

**Reproducibility.** Each run records the git commit, a clean-tree flag, the manifest hash, and the versions of every package that can change the numbers. The final run ([`docs/bakeoff/bakeoff-20261008T104026Z.json`](bakeoff/bakeoff-20261008T104026Z.json)) used pypdf 6.18.1 with its `fonts` extra (fontTools 4.66.1), pdfplumber 0.11.10, pysbd 0.3.4 and Python 3.12.11, from a clean tree. Repeated runs reproduced every recovery score exactly.

## Results

| | pypdf | pdfplumber |
|---|---|---|
| Mean recovery @ 0.90 | 0.928 | 0.103 |
| Mean recovery @ 0.95 | **0.920** | **0.079** |
| Mean recovery @ 0.99 | 0.885 | 0.053 |
| Worst article @ 0.95 | 0.812 | 0.015 |
| Errors | 0 | 0 |

Per article at 0.95 (full data in [`bakeoff-20261008T104026Z.csv`](bakeoff/bakeoff-20261008T104026Z.csv)):

| Article | Type | Truth sentences | pypdf | pdfplumber |
|---|---|---|---|---|
| PMC12794296 | research | 166 | 0.849 | 0.054 |
| PMC12757395 | research | 166 | 0.940 | 0.084 |
| PMC12757877 | review | 262 | 0.958 | 0.015 |
| PMC12756487 | research | 85 | 0.941 | 0.129 |
| PMC12756446 | research | 252 | 0.952 | 0.083 |
| PMC12756492 | research | 148 | 0.932 | 0.061 |
| PMC12756163 | research | 255 | 0.957 | 0.118 |
| PMC12756476 | case report | 67 | 0.925 | 0.164 |
| PMC12756502 | research | 206 | 0.932 | 0.049 |
| PMC12756423 | research | 277 | 0.812 | 0.036 |

## Why pdfplumber fails

Its default `extract_text()` reads each horizontal line across the full page width. On a two-column page, every output line joins a line of the left column to a line of the right. From page 2 of PMC12757395:

> The glymphatic system is a brain-wide perivascular transport **Here, we utilize a statistical model known as segmented regression,**
> route for fluids and solutes, including nutrients, drugs, and metabolic **which may be used to analyze chronologically ordered series of**

Interleaved sentences match nothing, so recovery collapses. pdfplumber's order scores still look good (0.71–1.00), because they are computed over the few sentences that did match, most likely from full-width regions. This is the reason order is never reported alone.

pypdf follows the order in which the PDF draws its text, and Frontiers PDFs draw it in reading order.

## What pypdf misses

[`scripts/analyse_misses.py`](../scripts/analyse_misses.py) classifies every unrecovered sentence by its closest candidate. The misses in the two lowest articles fall into three groups.

**Parser defects**

- **Lost word spaces** (PMC12756423): 688 missing-space differences among its near misses at 0.99, e.g. `intermediatelevelzithe`, `chow (2019)noted`. Each costs about 1% similarity, which is why this article drops from 0.812 at 0.95 to 0.578 at 0.99. When the lost space follows a full stop (`performance.unlike`), the sentence splitter cannot separate the sentences, and both become misses.
- **Glyph names instead of digits** (PMC12794296 only, 373 occurrences; none in the other nine): `2.3.3` is extracted as `/two.tnum./three.tnum…`. The PDF uses suffixed glyph names for tabular digits, and pypdf looks them up without stripping the suffix that the Adobe Glyph List specification says to drop (`two.tnum` → `two` → `2`).

**Layout effects** (any parser would face these)

- Running headers and footers inserted mid-sentence at page breaks.
- Floating elements interrupting sentences: the keyword box merges into the first abstract sentence, and figures cut sentences off.

**Comparison artefacts** (from the measurement, not the parser)

- Formula gaps: the PDF contains formula text that the ground truth excludes.
- Compound hyphens at line breaks are joined (`intermediate-level` → `intermediatelevel`), because they cannot be told apart from syllable hyphenation.
- Sentence splitting occasionally differs between the two sides.

## Limitations

- One journal, ten articles, one layout family. The result is decisive for this corpus; it is not a general ranking of parsers.
- pypdf's correct column order depends on the PDF drawing text in reading order. Other publishers' PDFs may not.
- This shows pdfplumber's **defaults** fail on this layout. Column-aware extraction (cropping each column) was deliberately not built: that would test custom code, not the parser.
- Recovery measures coverage only. pypdf extracts about twice as many sentences as the ground truth contains (references, captions, tables, page furniture), and none of that extra text is penalised.
- The first runs lacked pypdf's optional fontTools support, which pypdf warned about for one article. Installing `pypdf[fonts]` removed the warnings and changed no score ([before](bakeoff/bakeoff-20261008T072003Z.csv), [after](bakeoff/bakeoff-20261008T104026Z.csv)).
- The headline is reported at 0.95. At 0.99 the metric largely measures cosmetic spacing loss, reported separately above.

## Consequences for ingest

What the bake-off recommended, and what `src/ingest` does:

1. **Use pypdf with `pypdf[fonts]`** in the ingest Lambda. Done; pdfplumber remains only in the `bakeoff` dependency group, to reproduce this comparison.
2. **Repair glyph names** after extraction. Done more narrowly than planned: the only suffixed glyph names in the corpus are the ten tabular digits, so cleanup rule C7 maps `/zero.tnum` to `/nine.tnum` with a regex instead of going through the Adobe Glyph List. No `pypdf+repair` row was added to the bake-off; the corpus test in `tests/test_clean.py` checks that no `.tnum` remains.
3. **Test `extraction_mode="layout"`** against the lost-space defect on PMC12756423. Not done; the defect is accepted (P1 in `docs/failure-taxonomy.md`), since no eval answer was affected.
4. **Strip running headers and footers** before chunking. Done: rule C1, plus C2 for the page-1 sidebar.
5. **Exclude references and captions** from chunks. References are excluded: rule C3 cuts the back matter from the first back-matter heading, references included. Captions are not excluded and are chunked with the body text.
