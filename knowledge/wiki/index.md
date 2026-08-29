# Curated LLM Wiki index

This is the human-readable inventory for the version-controlled Wiki used by
the migration Architect. Retrieval is deterministic lexical navigation over
one to three curated pages; this index is not a vector database or an
instruction source.

Catalog digest: `sha256:15cc958cb9dbac294c8835588d528cb9a9d5ae8b4aaeca666555218bfae5ddb9`

| Page ID | Title | Path | Platform | Version | Status | Last verified | Content digest |
| --- | --- | --- | --- | --- | --- | --- | --- |
| salesforce-visualforce-to-lwc | Salesforce Visualforce to LWC migration | `pages/salesforce-visualforce-to-lwc.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-27 | `sha256:ccb7ff290ff55f05c2f9ada1e5ef6c14db2380e3c32029195125dfef0e2ab4db` |
| salesforce-lightning-data-service | Salesforce Lightning Data Service record retrieval | `pages/salesforce-lightning-data-service.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-26 | `sha256:9b712b91ef454d57ac8d7e09583f3557989f3499e520261c50864d49ce0bc0a4` |
| salesforce-apex-security | Salesforce Apex security for LWC services | `pages/salesforce-apex-security.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-27 | `sha256:c9b6d58caf0056470f6417b7e05e45d4cbabd7cca58318ce80442d64bb141af7` |
| salesforce-validation | Salesforce migration validation | `pages/salesforce-validation.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-27 | `sha256:8b976826439fc78744ea93fd6a3e9cee5e4d25370b2d9edac2ea2189250e7d92` |
| salesforce-case-management-console | Salesforce Case Management Console migration | `pages/salesforce-case-management-console.md` | salesforce | Salesforce API 67.0 | pilot | 2026-08-27 | `sha256:2dfff91f7f84dcbe59a81af834500952156f1727d72fb0b9915b209de93a554a` |
| mulesoft-mule3-to-mule4 | Mule 3 to Mule 4 migration | `pages/mulesoft-mule3-to-mule4.md` | mulesoft | Mule 3.9.5 to Mule 4.9.20 | pilot | 2026-08-26 | `sha256:ab530ef851f42b9c566c6587657792c1030ef08146c3394763d945b0eec9dbd6` |
| mulesoft-toolchain-validation | MuleSoft target toolchain and validation | `pages/mulesoft-toolchain-validation.md` | mulesoft | Mule 3.9.5 to Mule 4.9.20 | pilot | 2026-08-26 | `sha256:7eb3f44f1a91850f44165eb2b81d4da5c44db4a99dfe650e278ab916a0ba3568` |
| workflow-safety-gates | Migration workflow safety gates | `pages/workflow-safety-gates.md` | workflow | workflow 1.0 | pilot | 2026-08-24 | `sha256:78cb048f0dd9a6701e1a44b10731320a8f8c244f0cbec9469fe5d74597feaa6d` |
| workflow-sequential-correction | Evidence-grounded sequential planning and correction | `pages/workflow-sequential-correction.md` | workflow | workflow 1.0 | pilot | 2026-08-26 | `sha256:533583fcf53bd1030809af23436813d4deb9285a57b86465bbbb21ae920cb827` |

The catalog owns page identity, versions, links, status, and authoritative
sources. Each content digest binds the corresponding Markdown page. If this
inventory differs from `catalog.json` or the page files, Wiki loading stops
instead of silently retrieving stale guidance.
