"""Test-only construction of one valid synthetic Mule migration candidate.

The shipped fixture contains only the immutable Mule 3 input. Tests that need
a complete candidate construct it in their temporary directory; product code
never reads these bytes and no finished Mule 4 tree is stored as a fixture.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from legacy_migration_agent.platforms.mulesoft_local_checks import (
    MULE4_APP,
    MULE4_ARTIFACT,
    MULE4_DATAWEAVE,
    MULE4_POM,
    MULE4_PROPERTIES,
    MULE4_TEST,
)

_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>

    <groupId>example.synthetic</groupId>
    <artifactId>customer-status-api-mule4</artifactId>
    <version>1.0.0-SNAPSHOT</version>
    <packaging>mule-application</packaging>

    <properties>
        <app.runtime>4.9.20</app.runtime>
        <mule.maven.plugin.version>4.10.1</mule.maven.plugin.version>
        <munit.version>3.7.3</munit.version>
        <http.connector.version>1.12.0</http.connector.version>
        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
        <project.reporting.outputEncoding>UTF-8</project.reporting.outputEncoding>
    </properties>

    <build>
        <plugins>
            <plugin>
                <groupId>org.mule.tools.maven</groupId>
                <artifactId>mule-maven-plugin</artifactId>
                <version>${mule.maven.plugin.version}</version>
                <extensions>true</extensions>
                <configuration>
                    <runtimeVersion>${app.runtime}</runtimeVersion>
                </configuration>
            </plugin>
            <plugin>
                <groupId>com.mulesoft.munit.tools</groupId>
                <artifactId>munit-maven-plugin</artifactId>
                <version>${munit.version}</version>
                <executions>
                    <execution>
                        <id>munit-test</id>
                        <phase>test</phase>
                        <goals>
                            <goal>test</goal>
                        </goals>
                    </execution>
                </executions>
            </plugin>
        </plugins>
    </build>

    <dependencies>
        <dependency>
            <groupId>org.mule.connectors</groupId>
            <artifactId>mule-http-connector</artifactId>
            <version>${http.connector.version}</version>
            <classifier>mule-plugin</classifier>
        </dependency>
        <dependency>
            <groupId>com.mulesoft.munit</groupId>
            <artifactId>munit-runner</artifactId>
            <version>${munit.version}</version>
            <classifier>mule-plugin</classifier>
            <scope>test</scope>
        </dependency>
        <dependency>
            <groupId>com.mulesoft.munit</groupId>
            <artifactId>munit-tools</artifactId>
            <version>${munit.version}</version>
            <classifier>mule-plugin</classifier>
            <scope>test</scope>
        </dependency>
    </dependencies>

    <repositories>
        <repository>
            <id>mulesoft-releases</id>
            <name>MuleSoft Releases</name>
            <url>https://repository.mulesoft.org/releases/</url>
            <layout>default</layout>
        </repository>
    </repositories>
    <pluginRepositories>
        <pluginRepository>
            <id>mulesoft-release-plugins</id>
            <name>MuleSoft Release Plugins</name>
            <url>https://repository.mulesoft.org/releases/</url>
            <layout>default</layout>
        </pluginRepository>
    </pluginRepositories>
</project>
"""

_ARTIFACT = """{
  "minMuleVersion": "4.9.20",
  "javaSpecificationVersions": [
    "17"
  ],
  "requiredProduct": "MULE_EE"
}
"""

_APPLICATION = """<?xml version="1.0" encoding="UTF-8"?>
<mule xmlns="http://www.mulesoft.org/schema/mule/core"
      xmlns:ee="http://www.mulesoft.org/schema/mule/ee/core"
      xmlns:http="http://www.mulesoft.org/schema/mule/http"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xsi:schemaLocation="
        http://www.mulesoft.org/schema/mule/core http://www.mulesoft.org/schema/mule/core/current/mule.xsd
        http://www.mulesoft.org/schema/mule/ee/core http://www.mulesoft.org/schema/mule/ee/core/current/mule-ee.xsd
        http://www.mulesoft.org/schema/mule/http http://www.mulesoft.org/schema/mule/http/current/mule-http.xsd">

    <configuration-properties file="application.yaml"/>

    <http:listener-config name="customer-status-http-listener" basePath="/api">
        <http:listener-connection host="${http.host}" port="${http.port}"/>
    </http:listener-config>

    <flow name="customer-status-api-flow">
        <http:listener config-ref="customer-status-http-listener"
                       path="/customers/{customerId}/status"
                       allowedMethods="GET"/>
        <set-variable variableName="customerId"
                      value="#[attributes.uriParams.customerId as String]"/>
        <flow-ref name="build-customer-status-response"/>
    </flow>

    <sub-flow name="build-customer-status-response">
        <ee:transform>
            <ee:message>
                <ee:set-payload resource="dw/customer-status-response.dwl"/>
            </ee:message>
        </ee:transform>
    </sub-flow>
</mule>
"""

_PROPERTIES = """http:
  host: "127.0.0.1"
  port: "8081"
"""

_DATAWEAVE = """%dw 2.0
output application/json
---
{
  customerId: vars.customerId,
  status: "ACTIVE",
  source: "synthetic-fixture"
}
"""

_MUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<mule xmlns="http://www.mulesoft.org/schema/mule/core"
      xmlns:munit="http://www.mulesoft.org/schema/mule/munit"
      xmlns:munit-tools="http://www.mulesoft.org/schema/mule/munit-tools"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xsi:schemaLocation="
        http://www.mulesoft.org/schema/mule/core http://www.mulesoft.org/schema/mule/core/current/mule.xsd
        http://www.mulesoft.org/schema/mule/munit http://www.mulesoft.org/schema/mule/munit/current/mule-munit.xsd
        http://www.mulesoft.org/schema/mule/munit-tools http://www.mulesoft.org/schema/mule/munit-tools/current/mule-munit-tools.xsd">

    <munit:config name="customer-status-api-test-suite"/>

    <munit:test name="build-customer-status-response-test"
                description="Builds a synthetic ACTIVE customer response">
        <munit:behavior>
            <munit:set-event cloneOriginalEvent="false">
                <munit:variables>
                    <munit:variable key="customerId"
                                    value="#[&quot;CUST-100&quot;]"
                                    mediaType="text/plain"
                                    encoding="UTF-8"/>
                </munit:variables>
            </munit:set-event>
        </munit:behavior>
        <munit:execution>
            <flow-ref name="build-customer-status-response"/>
        </munit:execution>
        <munit:validation>
            <munit-tools:assert-that expression="#[payload.customerId]"
                                     is="#[MunitTools::equalTo(&quot;CUST-100&quot;)]"
                                     message="The customer ID must be preserved"/>
            <munit-tools:assert-that expression="#[payload.status]"
                                     is="#[MunitTools::equalTo(&quot;ACTIVE&quot;)]"
                                     message="The synthetic status must remain ACTIVE"/>
            <munit-tools:assert-that expression="#[payload.source]"
                                     is="#[MunitTools::equalTo(&quot;synthetic-fixture&quot;)]"
                                     message="The fixture provenance must be explicit"/>
        </munit:validation>
    </munit:test>
</mule>
"""


def mulesoft_target_outputs() -> dict[str, bytes]:
    """Return fresh target bytes for a temporary test candidate."""

    return {
        MULE4_ARTIFACT: _ARTIFACT.encode(),
        MULE4_POM: _POM.encode(),
        MULE4_APP: _APPLICATION.encode(),
        MULE4_PROPERTIES: _PROPERTIES.encode(),
        MULE4_DATAWEAVE: _DATAWEAVE.encode(),
        MULE4_TEST: _MUNIT.encode(),
    }


def build_mulesoft_candidate(source_root: Path, candidate_root: Path) -> Path:
    """Copy immutable input and add a valid target only under a test temp root."""

    shutil.copytree(source_root, candidate_root)
    for relative_path, content in mulesoft_target_outputs().items():
        destination = candidate_root.joinpath(*relative_path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return candidate_root
