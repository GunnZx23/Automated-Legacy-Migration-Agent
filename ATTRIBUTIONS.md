# Attributions and third-party notices

This repository and its original source, documentation, diagrams, tests, and
synthetic Salesforce and MuleSoft fixtures are released under the
[Apache License 2.0](LICENSE). The fixtures were created for this capstone; they do
not contain customer, employer, or proprietary source code or data.

## Referenced platforms and documentation

Salesforce, Lightning Web Components, Visualforce, Apex, MuleSoft, Mule, and
DataWeave are trademarks of their respective owners. Their names are used only
to identify the public technologies modeled by the synthetic examples. The
curated Wiki records the title, publisher, URL, version scope, and review state
for each external documentation source in
[`knowledge/wiki/catalog.json`](knowledge/wiki/catalog.json). External
documentation is linked and summarized; it is not redistributed here.

## Direct software dependencies

The executable package uses the following third-party projects under their
upstream licenses:

- [LangGraph](https://github.com/langchain-ai/langgraph) and
  [LangGraph SQLite checkpointing](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-sqlite)
  — MIT License.
- [Pydantic](https://github.com/pydantic/pydantic) — MIT License.
- [PyYAML](https://github.com/yaml/pyyaml) — MIT License.
- [OpenAI Python](https://github.com/openai/openai-python), an optional live
  model adapter dependency — Apache License 2.0.

The application can also call an operator-installed
[Ollama](https://github.com/ollama/ollama) runtime over loopback. Ollama and
local model weights are not bundled, vendored, downloaded, or redistributed by
this repository. The operator is responsible for the upstream runtime and
model license applicable to the exact installed alias.

Development and domain-validation tooling is declared in `pyproject.toml`,
`uv.lock`, and `tooling/lwc-jest/package-lock.json`. In particular,
[`@salesforce/sfdx-lwc-jest`](https://github.com/salesforce/sfdx-lwc-jest) is
MIT-licensed. The retained system-flow SVG was generated separately from its
adjacent Mermaid source with
[`@mermaid-js/mermaid-cli`](https://github.com/mermaid-js/mermaid-cli), which
is not installed as a runtime dependency. Transitive packages remain governed
by the notices and license terms distributed by their upstream projects and
recorded by the lockfiles.

No third-party package source is vendored into the Python package. Generated
artifacts and evaluation evidence do not change the license of the tools that
produced them.
