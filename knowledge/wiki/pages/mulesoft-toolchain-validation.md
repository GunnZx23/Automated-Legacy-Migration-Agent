# MuleSoft target toolchain and validation

The bounded target is one reviewed compatibility set: Mule runtime 4.9.20 LTS,
Java 17, Mule Maven Plugin 4.10.1, MUnit 3.7.3, and HTTP Connector 1.12.0.
Mule Maven Plugin 4.10.1 and MUnit 3.7.3 both document Maven 3.9.0–3.9.15;
remain inside that intersection and record the resolved Maven executable and
version. Keep `pom.xml` and `mule-artifact.json` aligned with the pins.

The generated POM must be a standalone active project model: declare direct
`modelVersion`, `groupId`, `artifactId`, and `version` values on the project,
then declare the allowlisted dependencies under the direct project
`dependencies`, the allowlisted plugins under direct `build/plugins`, and the
MuleSoft release URLs under direct `repositories` and `pluginRepositories`.
The direct `mule-maven-plugin` declaration must activate the pinned runtime
through exactly one configuration binding:

```xml
<configuration>
    <runtimeVersion>${app.runtime}</runtimeVersion>
</configuration>
```

The direct `app.runtime` property must resolve to Mule runtime 4.9.20. Merely
declaring the property without binding it to the active plugin does not pin the
build runtime.
Do not inherit target coordinates from a parent or move build inputs into
dependency management, plugin management, or profiles. Those sections are not
accepted as a substitute for the active project model.

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

Project correction signal
`mulesoft_candidate.dataweave_contract.response_dataweave`: repair only the
generated response DataWeave. It must use DataWeave 2.0 and produce the bounded
`customerId`, `status`, and `source` JSON response fields from runtime state.
An empty object, `null`, hard-coded arbitrary payload, or header-only `%dw 2.0`
file is not behavioral evidence. Formatting, field order, locals, and
equivalent expressions remain candidate choices.

Project correction signal `mulesoft_candidate.munit_contract.candidate_munit`:
repair only the candidate-owned Mule 4 MUnit suite. It must reach a callable
from the generated Mule 4 application and make at least one nontrivial assertion
over a value produced by that call. An always-true assertion, a
self-comparison, a suite that reaches only the preserved Mule 3 application,
or a disconnected test file does not validate the candidate. Test identities,
setup values, and assertion style remain candidate choices.

Project correction signal `mulesoft_candidate.pom_contract.pom_xml`: repair
only the generated `pom.xml`. Restore the approved Mule application packaging,
direct standalone project coordinates, active allowlisted plugins and
dependencies, and MuleSoft release repository restriction. The direct
`mule-maven-plugin` must contain exactly one direct `configuration` whose
`runtimeVersion` resolves through the direct `app.runtime` property to 4.9.20.
Do not add a parent, management or profile indirection, credentials, extra
build capabilities, or change non-POM artifacts without a separate diagnostic.

Project correction signal `mulesoft_candidate.version_mismatch.pom_xml`:
repair only the generated `pom.xml`. Align only the direct active POM runtime,
Mule Maven Plugin, MUnit, and HTTP Connector versions with the approved
compatibility set. Do not edit `mule-artifact.json`, Mule XML, DataWeave, or
MUnit without a separate artifact-specific diagnostic.

When MUnit exercises the public HTTP listener, use
`munit:enable-flow-source` for that exact generated listener flow, a loopback
`http:request` with the bounded GET route, and meaningful assertions over the
response. The repository permits loopback HTTP only for this MUnit path; any
other HTTP request configuration is classified by the artifact-specific
`mulesoft_candidate.outbound_connector.candidate_munit` signal and rejected.

Artifact parsing signals are exact file boundaries, not recipes. The UTF-8
signals are `mulesoft_candidate.unsafe_text.mule_artifact_json`,
`mulesoft_candidate.unsafe_text.pom_xml`,
`mulesoft_candidate.unsafe_text.application_xml`,
`mulesoft_candidate.unsafe_text.application_yaml`,
`mulesoft_candidate.unsafe_text.response_dataweave`, and
`mulesoft_candidate.unsafe_text.candidate_munit`. Repair only the artifact named
by the signal so a bounded safe parser accepts its complete text; preserve its
semantic contract and choose any equivalent private structure.

Secret-material signals are
`mulesoft_candidate.secret_material.mule_artifact_json`,
`mulesoft_candidate.secret_material.pom_xml`,
`mulesoft_candidate.secret_material.application_xml`,
`mulesoft_candidate.secret_material.application_yaml`,
`mulesoft_candidate.secret_material.response_dataweave`, and
`mulesoft_candidate.secret_material.candidate_munit`. Remove embedded
credentials, private keys, credential-bearing URLs, and secret-shaped
assignments only from the named generated artifact; never invent a replacement
secret or weaken the detector.

Safe-XML signals are `mulesoft_candidate.unsafe_xml.pom_xml`,
`mulesoft_candidate.unsafe_xml.application_xml`,
`mulesoft_candidate.unsafe_xml.candidate_munit`,
`mulesoft_candidate.malformed_xml.pom_xml`,
`mulesoft_candidate.malformed_xml.application_xml`, and
`mulesoft_candidate.malformed_xml.candidate_munit`. Repair only the named XML
artifact so a safe parser accepts it without DTD or entity declarations while
preserving its Maven, Mule application, or MUnit semantic role.

Descriptor parsing signals
`mulesoft_candidate.malformed_yaml.application_yaml` and
`mulesoft_candidate.malformed_json.mule_artifact_json` repair only their named
generated descriptor. YAML must remain a bounded scalar mapping without graph
features; JSON must remain a strict Mule application descriptor object with
unique keys and no `NaN`, positive infinity, or negative infinity values.

Application contract signals
`mulesoft_candidate.mule4_contract.application_xml` and
`mulesoft_candidate.mule4_contract.application_yaml` repair only the generated
Mule application or loopback configuration named by the signal. Preserve the
bounded GET route and response behavior; internal flow topology and equivalent
Mule 4 expressions remain candidate choices.

Descriptor contract signals
`mulesoft_candidate.artifact_contract.mule_artifact_json` and
`mulesoft_candidate.version_mismatch.mule_artifact_json` repair only
`mule-artifact.json`. Restore the approved Mule runtime, Java, required product,
and project-version alignment without changing unrelated safe metadata.

Outbound signals `mulesoft_candidate.outbound_connector.application_xml` and
`mulesoft_candidate.outbound_connector.candidate_munit` repair only the named
generated XML. Remove external connector or request capability; the sole
permitted request is candidate MUnit loopback HTTP to the approved public route.

Graph signal `mulesoft_dependency_closure.target_graph` permits changes only to
the generated Mule application XML, response DataWeave, and candidate MUnit
suite. Repair unresolved generated flow, configuration, transform, or test
references while leaving internal topology candidate-owned.

Runtime signal `mulesoft_munit_execution.candidate_behavior` permits changes
only to the generated application XML, loopback configuration, response
DataWeave, and candidate MUnit suite. Use the terminal controller-owned MUnit
failure evidence to restore observable behavior. Do not change the pinned POM
or controller-owned tests without their own diagnostic.

The checked-in capstone runtime authority is intentionally disabled, so its
honest ceiling is static fixture-contract evidence until a reviewed immutable
runtime and dependencies are authorized. A future enabled authority must bind
its immutable image labels and toolchain probe to Java 17, Mule runtime 4.9.20,
Mule Maven Plugin 4.10.1, MUnit 3.7.3, and a concrete Maven version from 3.9.0
through 3.9.15; self-consistent but different version labels are not authority.
