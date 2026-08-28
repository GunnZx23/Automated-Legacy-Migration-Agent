# Curated LLM Wiki index

This is the human-readable inventory for the version-controlled Wiki used by
the migration Architect. Retrieval is deterministic lexical navigation over
one to three curated pages; this index is not a vector database or an
instruction source.

Catalog digest: `sha256:a94455c273d55e2b1a75257d0f2739e0b808eb8d81d13d14cd1aadc590288442`

| Page ID | Title | Path | Platform | Version | Status | Last verified | Content digest |
| --- | --- | --- | --- | --- | --- | --- | --- |
| salesforce-visualforce-to-lwc | Salesforce Visualforce to LWC migration | `pages/salesforce-visualforce-to-lwc.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-27 | `sha256:dcc819578b8bf41038ef27928dbfd673db078915940a6aff123f556607892992` |
| salesforce-lightning-data-service | Salesforce Lightning Data Service record retrieval | `pages/salesforce-lightning-data-service.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-26 | `sha256:9b712b91ef454d57ac8d7e09583f3557989f3499e520261c50864d49ce0bc0a4` |
| salesforce-apex-security | Salesforce Apex security for LWC services | `pages/salesforce-apex-security.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-27 | `sha256:4b463143bdfa843b65e177cda7beff3b3745de1d535fc59ad7f3af96b4731bde` |
| salesforce-validation | Salesforce migration validation | `pages/salesforce-validation.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-27 | `sha256:c9f54962490f0c19034b04a9947f2f861133cd6465ac0ccaa47c0811469590d3` |
| mulesoft-mule3-to-mule4 | Mule 3 to Mule 4 migration | `pages/mulesoft-mule3-to-mule4.md` | mulesoft | Mule 3.9.5 to Mule 4.9.20 | pilot | 2026-08-26 | `sha256:ab530ef851f42b9c566c6587657792c1030ef08146c3394763d945b0eec9dbd6` |
| mulesoft-toolchain-validation | MuleSoft target toolchain and validation | `pages/mulesoft-toolchain-validation.md` | mulesoft | Mule 3.9.5 to Mule 4.9.20 | pilot | 2026-08-26 | `sha256:7eb3f44f1a91850f44165eb2b81d4da5c44db4a99dfe650e278ab916a0ba3568` |
| workflow-safety-gates | Migration workflow safety gates | `pages/workflow-safety-gates.md` | workflow | workflow 1.0 | pilot | 2026-08-24 | `sha256:78cb048f0dd9a6701e1a44b10731320a8f8c244f0cbec9469fe5d74597feaa6d` |
| workflow-sequential-correction | Evidence-grounded sequential planning and correction | `pages/workflow-sequential-correction.md` | workflow | workflow 1.0 | pilot | 2026-08-26 | `sha256:533583fcf53bd1030809af23436813d4deb9285a57b86465bbbb21ae920cb827` |

The catalog owns page identity, versions, links, status, and authoritative
sources. Each content digest binds the corresponding Markdown page. If this
inventory differs from `catalog.json` or the page files, Wiki loading stops
instead of silently retrieving stale guidance.
