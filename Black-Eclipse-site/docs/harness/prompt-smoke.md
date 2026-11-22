# Prompt smoke suite

Short, user-style prompts that run through the **real graph** with the
deterministic evaluation model. No network and no API key are required; the
scripted model emits the tool calls each prompt needs, while the graph, tool
surface, executor, environment and graders are the production ones.

## Prompts

| Prompt | What it exercises |
|---|---|
| "Reply with the single word PLUGIN_READY." | answer path and exact grader |
| "Store answer=plugin in memory, read it back, then answer with it." | memory write/read tools, environment state, tool grader |
| "Write 'hello plugin' to note.txt, then read it back." | filesystem write/read, sandbox state, tool grader |
| "Get the quote for ACME from the quote service and report its price." | mock service seam, tool arguments, exact output |
| "Use the prompt-health plugin to check its health." | a plugin-contributed `plug_*` tool is visible on the agent surface |

The first four are data in `python/tests/prompt_smoke_v1.json`; the fifth lives
in `test_prompt_smoke.py` because it needs a live plugin install.

## Run it

```bash
node scripts/test-python.js python/tests/test_prompt_smoke.py
```

Expected:

```
2 passed
```

The evaluation prompts assert `measured_trials == 4` and every trial passed;
`UNMEASURED` never counts as a pass.

## Live-model runs

The live tier is opt-in and never part of CI. It uses the same prompts and the
same runner, but a real provider:

```bash
HARNESS_LIVE_PROMPT_TEST=1 \
HARNESS_LIVE_PROVIDER=opencode-go \
HARNESS_LIVE_MODEL=deepseek-v4.1-flash \
HARNESS_LIVE_BASE_URL=https://opencode.ai/inference/go/openai/v1 \
HARNESS_LIVE_API_KEY=<operator key> \
node scripts/test-python.js python/tests/test_prompt_live.py
```

`test_prompt_live.py` skips without `HARNESS_LIVE_PROMPT_TEST=1`, so the offline
suite stays key-free. It asserts all four prompts are **measured** and passed,
and reports the provider failure as `UNMEASURED` rather than a fake pass.

Recorded live run on this machine (key read from the encrypted operator vault,
never printed): **4/4 prompts passed**, `task_success = 1.0`, one pytest test
passed in 16 s. The responses were:

- `PLUGIN_READY`
- `42`
- a one-sentence stop-loss explanation containing “stop” and “loss”
- three real portfolio tools (`portfolio_list`, `portfolio_aggregate`, …)

