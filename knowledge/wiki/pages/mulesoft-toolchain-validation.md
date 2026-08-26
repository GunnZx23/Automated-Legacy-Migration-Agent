# MuleSoft target toolchain and validation

The bounded target is one reviewed compatibility set: Mule runtime 4.9.20 LTS,
Java 17, Mule Maven Plugin 4.10.1, MUnit 3.7.3, and HTTP Connector 1.12.0.
Mule Maven Plugin 4.10.1 and MUnit 3.7.3 both document Maven 3.9.0–3.9.15;
remain inside that intersection and record the resolved Maven executable and
version. Keep `pom.xml` and `mule-artifact.json` aligned with the pins.

Evidence is layered:

- Static checks prove bounded inventory, XML structure, exact coordinates,
  legacy byte preservation, and forbidden-connector rules. They do not compile
  DataWeave or execute Mule, Maven, HTTP, or MUnit.
- MUnit is the Mule analogue of local component tests: it can exercise the
  migrated Mule flow/subflow when dependencies resolve and an authorized
  runtime is available. A subflow suite does not prove the HTTP listener,
  deployment target, credentials, policies, or production behavior.
- A missing Maven tool, repository-authentication failure, disabled runtime
  authority, or unavailable dependency is `environment_unavailable`, not pass.

The checked-in capstone runtime authority is intentionally disabled, so its
honest ceiling is static fixture-contract evidence until a reviewed immutable
runtime and dependencies are authorized.
