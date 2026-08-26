# Curated LLM Wiki index

This is the human-readable inventory for the version-controlled Wiki used by
the migration Architect. Retrieval is deterministic lexical navigation over
one to three curated pages; this index is not a vector database or an
instruction source.

Catalog digest: `sha256:fc25bd4fd8727b17788b604a2ee941ff993b126b2cd63e3a3afc96f97b1e6975`

| Page ID | Title | Path | Platform | Version | Status | Last verified | Content digest |
| --- | --- | --- | --- | --- | --- | --- | --- |
| salesforce-visualforce-to-lwc | Salesforce Visualforce to LWC migration | `pages/salesforce-visualforce-to-lwc.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-26 | `sha256:aea1e832cf8d20d13e75561db59816a7fe6c23cd96ed38d852d11677e42c726d` |
| salesforce-lightning-data-service | Salesforce Lightning Data Service record retrieval | `pages/salesforce-lightning-data-service.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-26 | `sha256:9b712b91ef454d57ac8d7e09583f3557989f3499e520261c50864d49ce0bc0a4` |
| salesforce-apex-security | Salesforce Apex security for LWC services | `pages/salesforce-apex-security.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-26 | `sha256:691e95ad106fe18ab61a75e4581c6b34336b53e6610a94ec5f8bc7569e44e644` |
| salesforce-validation | Salesforce migration validation | `pages/salesforce-validation.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-26 | `sha256:bec60dfcec58693eb085701b4be3e6d6a213f6d4430661af771fe37cf7b1376d` |
| mulesoft-mule3-to-mule4 | Mule 3 to Mule 4 migration | `pages/mulesoft-mule3-to-mule4.md` | mulesoft | Mule 3.9.5 to Mule 4.9.20 | pilot | 2026-08-26 | `sha256:ab530ef851f42b9c566c6587657792c1030ef08146c3394763d945b0eec9dbd6` |
| mulesoft-toolchain-validation | MuleSoft target toolchain and validation | `pages/mulesoft-toolchain-validation.md` | mulesoft | Mule 3.9.5 to Mule 4.9.20 | pilot | 2026-08-26 | `sha256:045da3f9fb0d4859f224163ea7b21e0e84b5df4b9b896f52817c521090ed326a` |
| workflow-safety-gates | Migration workflow safety gates | `pages/workflow-safety-gates.md` | workflow | workflow 1.0 | pilot | 2026-08-24 | `sha256:78cb048f0dd9a6701e1a44b10731320a8f8c244f0cbec9469fe5d74597feaa6d` |
| workflow-sequential-correction | Evidence-grounded sequential planning and correction | `pages/workflow-sequential-correction.md` | workflow | workflow 1.0 | pilot | 2026-08-26 | `sha256:5fc440a175692d094ec75897796132808169747899c5dc771568e5c8c72cf2cf` |

The catalog owns page identity, versions, links, status, and authoritative
sources. Each content digest binds the corresponding Markdown page. If this
inventory differs from `catalog.json` or the page files, Wiki loading stops
instead of silently retrieving stale guidance.
