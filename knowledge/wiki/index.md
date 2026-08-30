# Curated LLM Wiki index

This is the human-readable inventory for the version-controlled Wiki used by
the migration Architect. Retrieval is deterministic lexical navigation over
one to three curated pages; this index is not a vector database or an
instruction source.

Catalog digest: `sha256:ddfff75b6cc4be685b550d69961ca56562412e984d89a13e4c77d7ed472bf2c3`

| Page ID | Title | Path | Platform | Version | Status | Last verified | Content digest |
| --- | --- | --- | --- | --- | --- | --- | --- |
| salesforce-visualforce-to-lwc | Salesforce Visualforce to LWC migration | `pages/salesforce-visualforce-to-lwc.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-27 | `sha256:b023ed918acd675fb9edfaee1963513090b15015a03addfebeded7799464047c` |
| salesforce-lightning-data-service | Salesforce Lightning Data Service record retrieval | `pages/salesforce-lightning-data-service.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-26 | `sha256:9b712b91ef454d57ac8d7e09583f3557989f3499e520261c50864d49ce0bc0a4` |
| salesforce-apex-security | Salesforce Apex security for LWC services | `pages/salesforce-apex-security.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-27 | `sha256:54f818226af274a1cc3148cd533cf0ef9b7b0717377dc694e80af1d2f5c719de` |
| salesforce-validation | Salesforce migration validation | `pages/salesforce-validation.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-29 | `sha256:2fd56696df7e9b13b717b4c46bf3d98d8c41fc53b77a6f7061420fdc119051bf` |
| salesforce-case-management-console | Salesforce Case Management Console migration | `pages/salesforce-case-management-console.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-29 | `sha256:0ce7c343574f1f5f9d0f8a9be159a00771aea741b25c71b227e2a9697b437ba0` |
| mulesoft-mule3-to-mule4 | Mule 3 to Mule 4 migration | `pages/mulesoft-mule3-to-mule4.md` | mulesoft | Mule 3.9.5 to Mule 4.9.20 | pilot | 2026-08-26 | `sha256:af1c3abec691e422684811a191bba69783b353325b14a52f68c1b4082b305f74` |
| mulesoft-toolchain-validation | MuleSoft target toolchain and validation | `pages/mulesoft-toolchain-validation.md` | mulesoft | Mule 3.9.5 to Mule 4.9.20 | pilot | 2026-08-29 | `sha256:9f8ae62a1f97ff911f775e704e63f35406233d83bc88cd8c770fa4c821b9b263` |
| workflow-safety-gates | Migration workflow safety gates | `pages/workflow-safety-gates.md` | workflow | workflow 1.0 | pilot | 2026-08-24 | `sha256:78cb048f0dd9a6701e1a44b10731320a8f8c244f0cbec9469fe5d74597feaa6d` |
| workflow-sequential-correction | Evidence-grounded sequential planning and correction | `pages/workflow-sequential-correction.md` | workflow | workflow 1.0 | pilot | 2026-08-26 | `sha256:533583fcf53bd1030809af23436813d4deb9285a57b86465bbbb21ae920cb827` |

The catalog owns page identity, versions, links, status, and authoritative
sources. Each content digest binds the corresponding Markdown page. If this
inventory differs from `catalog.json` or the page files, Wiki loading stops
instead of silently retrieving stale guidance.
