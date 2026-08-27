# Curated LLM Wiki index

This is the human-readable inventory for the version-controlled Wiki used by
the migration Architect. Retrieval is deterministic lexical navigation over
one to three curated pages; this index is not a vector database or an
instruction source.

Catalog digest: `sha256:3e3cf7602459118e1ef7e88d5751eb245f9591335d6e7caf4ec4dceb6d13057f`

| Page ID | Title | Path | Platform | Version | Status | Last verified | Content digest |
| --- | --- | --- | --- | --- | --- | --- | --- |
| salesforce-visualforce-to-lwc | Salesforce Visualforce to LWC migration | `pages/salesforce-visualforce-to-lwc.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-27 | `sha256:67ffaf0d4350a97ffe0f043c5a9af976ff145a95e2e07b5da5b777936e10de79` |
| salesforce-lightning-data-service | Salesforce Lightning Data Service record retrieval | `pages/salesforce-lightning-data-service.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-26 | `sha256:9b712b91ef454d57ac8d7e09583f3557989f3499e520261c50864d49ce0bc0a4` |
| salesforce-apex-security | Salesforce Apex security for LWC services | `pages/salesforce-apex-security.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-27 | `sha256:449f3f3646cc7402b19a5d9dbc254621bd24d2636044d15a94109afdebc7b0d1` |
| salesforce-validation | Salesforce migration validation | `pages/salesforce-validation.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-27 | `sha256:b6f15ce5e7aceab5ab37453af1176e5242587fc0ba1f2999be868ea014b7e563` |
| mulesoft-mule3-to-mule4 | Mule 3 to Mule 4 migration | `pages/mulesoft-mule3-to-mule4.md` | mulesoft | Mule 3.9.5 to Mule 4.9.20 | pilot | 2026-08-26 | `sha256:ab530ef851f42b9c566c6587657792c1030ef08146c3394763d945b0eec9dbd6` |
| mulesoft-toolchain-validation | MuleSoft target toolchain and validation | `pages/mulesoft-toolchain-validation.md` | mulesoft | Mule 3.9.5 to Mule 4.9.20 | pilot | 2026-08-26 | `sha256:7eb3f44f1a91850f44165eb2b81d4da5c44db4a99dfe650e278ab916a0ba3568` |
| workflow-safety-gates | Migration workflow safety gates | `pages/workflow-safety-gates.md` | workflow | workflow 1.0 | pilot | 2026-08-24 | `sha256:78cb048f0dd9a6701e1a44b10731320a8f8c244f0cbec9469fe5d74597feaa6d` |
| workflow-sequential-correction | Evidence-grounded sequential planning and correction | `pages/workflow-sequential-correction.md` | workflow | workflow 1.0 | pilot | 2026-08-26 | `sha256:533583fcf53bd1030809af23436813d4deb9285a57b86465bbbb21ae920cb827` |

The catalog owns page identity, versions, links, status, and authoritative
sources. Each content digest binds the corresponding Markdown page. If this
inventory differs from `catalog.json` or the page files, Wiki loading stops
instead of silently retrieving stale guidance.
