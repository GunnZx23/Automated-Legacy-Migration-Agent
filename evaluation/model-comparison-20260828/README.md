# Local Engineer model comparison — 2026-08-28

## Decision

Keep `qwen3.8:latest` for the capstone's local Engineer role and pin the tested
digest. It was the only model in this bounded comparison to produce a valid
11-file migration plan and pass every deterministic gate on its first attempt.
Stock Gemma 4 and Devstral generated schema-valid plans, but their code failed
domain and behavioral checks. GPT-OSS did not complete this harness's strict
JSON-schema protocol.

This is a one-task, one-primary-run comparison, not a statistically powered
general model benchmark. It is decisive for the exact capstone VF-to-LWC seam
because the alternative candidates did not meet the minimum acceptance bar.

## Frozen protocol

- Role isolated: Engineer only. The Architect handoff from run
  `3cd384f951d0cb4199dd3f07` supplied the same scope and semantic decisions to
  every candidate.
- Task: migrate the bounded account/contact Visualforce page and controller to
  an additive LWC/Apex implementation with security, state, stale-response,
  metadata, permission-set, Apex-test, and Jest-test requirements.
- Engineer definition: `engineer/v23`.
- Corrected requirement: `@jest/globals` is the first static import and contains
  every used Jest API; the current Salesforce validation Wiki page is included.
- Prompt digest:
  `sha256:69e41d45eff9cf5047a6c387de20776e96e56853caa654d4ad2e8be366bfdc2d`.
- Context digest:
  `sha256:54a49425044384897c2cce263c9898a71d4d73b5ee75b85028b56bb5f9ba8107`.
- Projected schema digest:
  `sha256:e8e34ccee2dd06713215977567c8a51b02c75e1eb4c859734de01ee451877741`.
- Combined protocol digest:
  `sha256:d3736614018ec4daef8f49ebe6377f0ecc0a98b7da29086c61deb437bc5dd8d8`.
- Structured input size: 39,186 characters. Provider tokenization ranged from
  13,082 to 14,032 input tokens.
- Generation: local Ollama `/api/chat`, `stream=false`, `think=false`,
  `temperature=0`, 600-second wall limit, and the same projected JSON schema.
- Each valid plan was applied only in a disposable isolated workspace.
- Validation gates: Salesforce candidate contract, dependency closure, pinned
  toolchain contract, OS sandbox probe, candidate-authored LWC Jest, independent
  controller-owned LWC Jest, and workspace fingerprint.
- No Salesforce org was contacted. Apex and metadata were checked statically;
  Apex tests were generated but were not compiled or executed in an org.

The exact frozen prompt, context, request, manifest, Wiki trace, projected
schema, and digest record are in [`protocol/`](protocol/).

## Results

| Rank | Exact local model | Revision digest | Strict schema | Gates | Jest evidence | Latency | Disposition |
|---:|---|---|:---:|---:|---|---:|---|
| 1 | `qwen3.8:latest` | `sha256:22130167c4c20e20c7b71454612966ca8e8171e9b3cc8ab6ce8aa6cbfec79643` | Yes | **7/7** | Candidate 10/10; controller 9/9 | 475.428 s | `ready_for_human_review` |
| 2 | `gemma4:31b` | `sha256:6316f0629137b426c9d9b853ffc4c8209589f30ee39aebede6285096c0ff47e7` | Yes | 4/7 | Candidate 0/6; controller 8/9 | 428.060 s | `recoverable_failure` |
| 3 | `devstral-small-2:24b` | `sha256:24277f07f62db8f9cb68e9dfc679ea1818a7fbac47a50eff0a701d3f645b63c8` | Yes | 3/7 | Both suites failed before tests ran | 375.930 s | `recoverable_failure` |
| 4 | `gpt-oss:20b` | `sha256:17052f91a42e97930aa6e28a6c6c06a983e6a58dbb00434885a0cf5313e376f7` | No | — | Not reached | 50.704 s initial | `structured_generation` failure |

### Qwen 3.8

Qwen changed the exact 11 approved paths, passed the candidate contract and a
17-node/39-edge dependency closure with no warnings, then passed all 19 LWC
tests. Candidate revision:
`sha256:86e3a7337ebdd73c1d6e64442d792d4878c09594003bd7e8d82831c0b29728cb`.
This is the only tested candidate suitable for the next full UI E2E run.

### Stock Gemma 4

Gemma returned a valid exact-path plan, but the candidate contract rejected
`salesforce_manifest_contract` and `jest_unapproved_module_target`. All six
candidate-authored Jest tests failed, and the independent suite found one
empty-state behavior failure among nine tests. Passing dependency closure did
not compensate for the functional failures.

`gemma4:31b` is the official stock dense 31B workstation model, not a community
fine-tune. Local Ollama metadata reports 31.3B parameters, Q4_K_M, 262,144-token
context, and Apache 2.0. It was the strongest official Gemma 4 size that safely
fit the 64 GB test host. Official references: [Ollama Gemma 4](https://ollama.com/library/gemma4),
[Google model card](https://huggingface.co/google/gemma-4-31B-it).

### Devstral Small 2

Devstral returned a valid exact-path plan but failed the candidate contract with
six diagnostics: `salesforce_manifest_contract`, `salesforce_apex_test_contract`,
`lwc_forbidden_runtime_capability`, `lwc_template_binding_invalid`,
`salesforce_lwc_metadata_contract`, and `jest_unapproved_module_target`.
Dependency closure and both Jest suites also failed. The result is a domain and
code-quality failure, not an unavailable-toolchain failure.

### GPT-OSS 20B

The initial exact call and two diagnostic repeats consistently returned an
empty incomplete Ollama envelope (`model=""`, `done=false`, no provider error or
usage counts). The adapter correctly rejected the response at the model-identity
boundary before JSON validation or code generation. A trivial unstructured
`/api/chat` probe succeeded with the correct alias, so this is classified as
incompatibility with the capstone's full strict-schema/non-thinking protocol,
not as a measured Salesforce coding-quality result.

## Actually run versus research-only

Actually run under the frozen protocol:

- `qwen3.8:latest`
- `gpt-oss:20b`
- `devstral-small-2:24b`
- official stock `gemma4:31b`

Research-only; not downloaded or run in this bounded round:

| Candidate | Verified availability | Why not run |
|---|---|---|
| IBM `granite4.2:30b` | Official Ollama tag, 18 GB, 128K context, Apache 2.0, advertised structured JSON | Another 18 GB download and full run was not justified after Qwen alone cleared 7/7. [Official page](https://ollama.com/library/granite4.2) |
| `Kwaipilot/KAT-Coder-V2.5-Dev` | Official HF weights, 35B MoE/3B active, 262K context, Apache 2.0 | Official card documents Transformers/vLLM/SGLang rather than a first-party Ollama artifact; a community quant would add provenance and runtime variables. [Model card](https://huggingface.co/Kwaipilot/KAT-Coder-V2.5-Dev) |
| `aneeq-hashmi/SalesforceCoder-Qwen3.5-9B` | Community HF Salesforce fine-tune | The community claim is not enough to replace validated evidence; a fair evaluation also requires the stock 9B base control. It was not installed. [Model card](https://huggingface.co/aneeq-hashmi/SalesforceCoder-Qwen3.5-9B) |
| Official `qwen3.5:9b` control | Official Ollama tag, 6.6 GB, 256K context | Kept as the required base control for any future SalesforceCoder test; not needed to decide the current winner. [Official tags](https://ollama.com/library/qwen3.5/tags) |

No credible MuleSoft/DataWeave-specialized local model with stronger provenance
was identified. Future model work should first add a representative Mule 3 to
Mule 4 frozen task, then compare challengers against both platform slices.

## Host and reproducibility notes

- Host: Apple M4 Max (`Mac16,5`), 64 GB unified memory.
- Runtime: macOS 26.6.1, Ollama 0.32.15.
- Free disk after the requested Gemma installation: 538 GiB.
- Local model metadata:
  - Qwen alias: 27.3B, Q4_K_M, 262,144 context.
  - Gemma 4: 31.3B, Q4_K_M, 262,144 context.
  - Devstral: 24.0B, Q4_K_M, 393,216 context.
  - GPT-OSS: 20.9B, MXFP4, 131,072 context.
- No installed model was removed or modified.
- Raw structured outcomes and validation reports are in [`results/`](results/).
  Disposable run workspaces, caches, and local state are regenerated under the
  ignored `runtime/` directory when the benchmark is rerun; they are not part
  of the repository evidence package.

Re-run an already installed model against the frozen bytes with:

```bash
uv run --frozen python evaluation/model-comparison-20260828/benchmark.py run \
  --model qwen3.8:latest
```

Because aliases are mutable, compare the resulting `model_revision` with the
tested digest before treating a later result as the same model.
