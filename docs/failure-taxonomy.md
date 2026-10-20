# Failure taxonomy

Every failure observed while building and evaluating bedrock-rag-eval, grouped by pipeline stage.
Sources: the main run `20261019T194300Z_156a0cee6ec0` (Nova Lite, Amazon Rerank, labels v2), the
v0 run `20261019T150117Z_156a0cee6ec0`, the vector-only ablation `20261019T223318Z_a3da1668c2f7`,
the parser bake-off (`docs/parser-bakeoff.md`) and the judge sensitivity test
(`eval/judge_probes.yaml`). The eval set has 40 questions: 30 answerable, 4 not covered by the
corpus, 3 off-topic and 3 with a false premise.

## Summary

| ID | Stage | Failure | Example | Caught by | Status |
|---|---|---|---|---|---|
| P1 | Parsing | Lost word spacing | PMC12756423 | Parser bake-off | Accepted |
| P2 | Parsing | Spaces inserted inside words | PMC12794296 p1 | Manual inspection | Accepted |
| P3 | Parsing | Some digits mis-extracted from glyph names | PMC12794296 | Parser bake-off | Accepted |
| R1 | Retrieval | Right page retrieved, answer sentence not | q17 | Nothing | Open (metric limitation) |
| R2 | Retrieval | Reranker scores of answerable questions as low as off-topic ones | q10, q13, q22 | Score inspection | Design consequence |
| G1 | Generation | Background statistic given instead of the study's own result | q17 | Nothing | Open |
| G2 | Generation | Discussion claim preferred over the results it contradicts | q19 | Nothing | Open |
| G3 | Generation | Sample units conflated (per region read as per group) | q04 | Nothing | Open |
| G4 | Generation | Fact for all models attributed to one model | u07 | Nothing | Documented |
| G5 | Generation | Refusal token appended to a complete answer | u06 | Premise grading | Open |
| G6 | Generation | Unrequested "not covered" sentences | q13, q16 | Manual inspection | Minor |
| E1 | Eval harness | Refusal-parsing bug undercounted correct abstentions | v0 run | Metric sanity check | Fixed |
| E2 | Eval labels | Expected pages incomplete | labels v1 | Completeness pass | Fixed |
| E3 | Eval harness | Skipped premise grade counted as a failure | ablation run | Metric sanity check | Fixed |
| J1 | Judge | Judge accepts support from an uncited passage | probe c3 | Sensitivity test | Documented |

## Parsing

**P1, lost word spacing.** pypdf drops the spaces between words in parts of the zither paper
(PMC12756423), producing text such as "statisticallysignificantimpact". No eval answer was
affected: q07 retrieves a damaged passage and is still answered correctly, with a reranker score
of 0.99. An earlier probe suggested damaged text gets low reranker scores; the eval does not
confirm this, since this paper's top scores range from 0.16 to 0.99 and equally low scores occur on
undamaged papers (R2).

**P2, inserted spaces.** On page 1 of the sEMG paper (PMC12794296), pypdf splits words ("reveale d",
"sign als"). No eval answer was affected.

**P3, glyph-name digits.** Some digits in PMC12794296 are not mapped from their glyph names (Adobe
Glyph List suffixes). Details are in `docs/parser-bakeoff.md`. No eval answer was affected.

pypdf was still chosen: 92.0% mean sentence recovery at the 0.95 threshold, against 7.9% for
pdfplumber, whose defaults interleave the two-column layout.

## Retrieval

**R1, right page, wrong chunk.** For q17, retrieval returned two chunks from page 1, the expected
page, but neither contains the answer sentence ("Among 206 enrolled patients, 78 (37.86%)
developed END"). page_hit counts this as a success. Page-level labels are too coarse to show
whether the answering text was retrieved; this failure led directly to G1.

**R2, reranker scores do not separate answerable from unanswerable questions.** Three answerable
questions, all of which the model still answered, had top reranker scores between 0.002 and
0.006 (q10, q13, q22). The seven questions the system correctly abstained on had top scores between
0.000 and 0.035. A score
threshold for abstention would have refused answerable questions, so abstention is left to the
model, which reached 7/7 correct abstentions. Why these three score so low is not established; a
likely factor is that the question wording differs from the passage wording.

## Generation

The three answers below (G1 to G3) are wrong or misleading, yet score perfectly on every automated
metric: retrieval, citation validity, citation on evidence and groundedness. They were found
incidentally, not by a systematic check against the answer sketches, so the true number of
incorrect answers among the 30 is not known.

**G1, background statistic instead of the study's result.** q17 asks what share of patients
developed early neurological deterioration. The answer gives 43%–60%, a range the introduction
cites from earlier studies. The study's own result (78 of 206, 37.86%) was not retrieved (R1).
The answer is grounded in its passage and still answers a different question.

**G2, self-contradicting source.** For q19, the paper's results (p7) report that white matter
fraction had the highest AUC (0.73), while its discussion (p8) calls CSF fraction optimal (AUC
0.72). Both passages were shown to the model, and it repeated the discussion. The answer is
grounded in a cited passage and contradicted by another.

**G3, conflated sample units.** q04 answers 33 students per group. The cited passage says 66
learners, 33 from each of two regions, were randomly assigned to the groups; a test-results table
in an uncited passage shows 31 in the control group and 32 in the experimental group.

**G4, scope drift.** u07 says SynthStrip performed best with a median Dice score between 0.88 and
0.96. The passage gives that range for all seven models. The claim is logically implied, because
SynthStrip is one of the seven, but it presents a shared range as SynthStrip's own result.

**G5, refusal token after a complete answer.** u06 corrects the false premise and then appends
INSUFFICIENT_CONTEXT. Premise handling is still graded correct, but a client that treats the token
as a refusal would hide the answer. A one-line prompt fix was identified ("use
INSUFFICIENT_CONTEXT only as your entire reply") and not applied.

**G6, unrequested gap statements.** q13 and q16 end with a sentence saying part of the question is
not covered by the passages, naming details the question never asked for. Harmless, but noise.

## Evaluation

**E1, refusal parsing.** In the v0 run, correct abstention was 3/7 because the parser did not
recognise some of the model's refusals. After the fix, v1 reached 7/7.

**E2, incomplete labels.** The v1 labels listed one expected page per question, although many
answers appear on several pages. A completeness pass raised the expected pages from 30 to 62
(21 questions changed). Rescoring the same run raised page_hit from 0.933 to 1.000, MRR from 0.786
to 0.961 and citation_on_evidence from 0.867 to 1.000. Most apparent retrieval misses were
labelling gaps, not system failures.

**E3, skipped grade counted as a failure.** A premise reply left ungraded was counted in the
denominator, showing 0.667 instead of 2/2. Fixed: ungraded replies are now reported separately.

## Groundedness judge

**J1, citation boundaries not enforced.** The judge (Nova Pro) caught 14 of 15 seeded errors (95%
Wilson interval 0.70 to 0.99) and passed 6 of 6 meaning-preserving controls. It caught every
changed number, entity and direction. The miss was probe c3: a correct claim citing a passage that
does not contain it was rated supported, with a reason quoting the uncited passage. The natural
case q04 (G3) points the same way. The judge rated all 45 claims in the main run supported,
including G1 to G4. A probe for "both groups" (s3) was dropped because its cited passage never
names a group, so it did not test what it intended. The judge was not calibrated against human
grades.

## Limitations

The corpus has 10 clearly distinct papers, so retrieval reaches a ceiling: document hit rate is
1.000 with or without the reranker. The reranker improved MRR only from 0.940 to 0.961, at about
1.45 s against 0.95 s per question and a throughput limit of 2 questions per minute; its value on a
larger or more overlapping corpus is untested. The 40 questions were drafted by an LLM from the
corpus, with every evidence quote machine-verified and every question reviewed by hand. Answer
correctness against the answer sketches is not measured, which is why G1 to G3 pass every metric.
