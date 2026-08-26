# Mule 3 to Mule 4 migration semantics

Treat this as a platform migration, not a namespace replacement. Inventory
endpoints, connectors, MEL/DataWeave, variables, message properties, exception
strategies, configuration, and MUnit behavior before generating Mule 4 code.

The synthetic input has no runtime descriptor. Its Mule 3.9.5 and Java 8 labels
are declared scenario assumptions, not facts verified from the source files.

For the bounded Customer Status API fixture:

- Map Mule 3 `flowVars` to Mule 4 `vars`.
- Do not mechanically map `sessionVars`: Mule 4 removed session variables.
  Redesign any cross-flow or transport lifetime explicitly.
- Map inbound HTTP properties to typed Mule 4 attributes such as
  `attributes.uriParams`, preserving types instead of flattening to strings.
- Rewrite DataWeave 1/MEL expressions for DataWeave 2 syntax and semantics;
  changing `%output` to `output` is only one part of that review.
- Migrate a catch strategy to `on-error-continue` only when treating the owner
  as successful and committing its transaction preserves behavior. A simple
  rollback strategy maps to `on-error-propagate`, which fails the owner and
  rolls back a transaction it owns.

The fixture is additive and happy-path bounded. Static checks do not execute
Mule or compile DataWeave. Its MUnit targets a response subflow, not the full
HTTP source or error path. Use the linked toolchain page for exact target pins
and validation boundaries.
