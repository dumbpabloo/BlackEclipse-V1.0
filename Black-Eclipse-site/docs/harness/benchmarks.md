# Published benchmarks

**GENERATED FILE - do not edit it.** Regenerate with:

```bash
python -m harness.evaluation.report
```

A number here is only worth what its provenance is worth, so every
tier carries its dataset, its checksum, its agent version, its commit
and its grader version. The two tiers are kept APART on purpose: the
offline tier measures the harness, the live tier measures the agent.

## Offline tier - the scripted model

A deterministic script replaces the model, so this measures the
**runtime** (the tool surface, the policy, the graders, the wiring)
- not the model. It is the floor the harness must never fall below.

- run: `evalrun-1795466189050`
- dataset: `eclipse-benchmark` @ `1` (checksum `ea086f46b491f84fdd3b7a87220d5533`)
- tier: 0
- agent version: `0.4.27`
- git commit: `b3e6761526237c4443f8835389f61096aa043010`
- grader version: `1`
- config: `{'trials': 1, 'tier': 0, 'split': 'validation', 'graderVersion': 1}`
- gate: **PASS**

| dimension | value |
|---|---|
| task_success | 0.9970 |
| tool_accuracy | 0.9712 |
| research_accuracy | UNKNOWN |
| citation_accuracy | 0.0000 |
| code_correctness | 1.0000 |
| financial_accuracy | 1.0000 |
| memory_accuracy | 1.0000 |
| planning_quality | 0.8937 |
| recovery_success | 1.0000 |
| misroute_rate | 0.0000 |
| escalation_correct | 1.0000 |
| provenance | 1.0000 |
| honesty | 1.0000 |
| artifact_delivery | 1.0000 |
| visual_delivery | 1.0000 |
| delivery | UNKNOWN |
| hallucination_rate | 0.0000 |
| safety_score | 1.0000 |
| cost | 0.0000 |
| latency | 0.2126 |
| token_usage | 0.0000 |
| reliability | 1.0000 |

### Per category

| category | passed / measured | unmeasured | pass rate |
|---|---|---|---|
| adversarial | 14/14 | 0 | 100.0% |
| browser | 0/0 | 1 | UNKNOWN |
| citations | 0/1 | 1 | 0.0% |
| coding | 4/4 | 0 | 100.0% |
| core | 4/4 | 0 | 100.0% |
| finance | 279/279 | 0 | 100.0% |
| long_horizon | 2/2 | 0 | 100.0% |
| memory | 2/2 | 0 | 100.0% |
| parallel | 3/3 | 0 | 100.0% |
| planning | 2/2 | 0 | 100.0% |
| recovery | 7/7 | 0 | 100.0% |
| regression | 1/1 | 6 | 100.0% |
| research | 0/0 | 4 | UNKNOWN |
| safety | 4/4 | 0 | 100.0% |
| tools | 12/12 | 0 | 100.0% |

### Unmeasured tasks

- `research.live.001`
- `browser.live.001`
- `replay.quote.001`
- `replay.fundamentals.001`
- `replay.news.001`
- `replay.unavailable.001`
- `replay.stale.001`
- `replay.refused.001`
- `research.cite.001`
- `research.unavailable.001`
- `research.conflict.001`
- `research.stale.001`

## Live tier - a real model

This is the only tier that measures the **agent**. The model is
named, and the run is reproducible from the provenance below.

- run: `live:27c280164943e63520e14422a6c953cf`
- dataset: `eclipse-benchmark` @ `1` (checksum `65e2b04e8020274fd870c2ebf7a7e448`)
- tier: 1
- agent version: `0.1.0`
- git commit: `8d2b7bc4665d0267cd84a459b1dd4e290ec6e8ec`
- grader version: `unknown`
- config: `{'tier': 1, 'taskCount': 278, 'measuredTrials': 278}`
- gate: **PASS**

fresh full live run evalrun-1794281772755: gate PASS (task_success 0.9748201438848921, hallucination_rate 0.0)

| dimension | value |
|---|---|
| task_success | 0.9748 |
| tool_accuracy | 0.5400 |
| research_accuracy | UNKNOWN |
| citation_accuracy | 1.0000 |
| code_correctness | UNKNOWN |
| financial_accuracy | 0.9963 |
| memory_accuracy | 1.0000 |
| planning_quality | 0.9997 |
| recovery_success | UNKNOWN |
| misroute_rate | UNKNOWN |
| escalation_correct | UNKNOWN |
| provenance | UNKNOWN |
| honesty | UNKNOWN |
| artifact_delivery | UNKNOWN |
| visual_delivery | 0.7500 |
| hallucination_rate | 0.0000 |
| safety_score | 1.0000 |
| cost | 0.0000 |
| latency | 7.8286 |
| token_usage | 316711.0755 |
| reliability | 1.0000 |
