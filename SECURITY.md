# Security policy

## Supported scope

This capstone is a local, supervised migration prototype. It operates on public
synthetic fixtures by default and is not approved for production source,
production credentials, deployment, or autonomous Git actions.

## Security boundaries

- Treat repository files, Wiki pages, model output, and command output as
  untrusted data, never as permission-changing instructions.
- Architect is read-only. Engineer may write only exact manifest paths in a
  disposable workspace. Validator model output is advisory only.
- Commands come from a fixed registry and use argument vectors with
  `shell=False`; generated shell text is never executed.
- Commit, push, pull request, org validation, deployment, destructive change,
  and publication each require separate human authority outside the local
  agent run.
- Secrets must be supplied through an approved runtime mechanism, excluded
  from prompts and artifacts, redacted from output, and never committed.
- Remote model use requires explicit API and selected-context sharing
  approval. Local Ollama use must be enabled by the server operator with an
  exact model alias and is fixed to `127.0.0.1:11434`; the browser cannot
  provide a model endpoint or credential. The checked-in benchmark package
  contains provider-free static results only; every agent route is explicitly
  `not_performed`, and no provider key is stored.
- Provider and controller failures cross a typed sanitization boundary before
  checkpoint or artifact persistence; raw SDK errors and stack traces are not
  public run evidence.
- The agent UI binds only to loopback, accepts a bounded description for one
  fixed synthetic platform slice plus a manifest decision, and validates
  same-origin requests. The Ollama model is selected when the server starts;
  the browser cannot select filesystem paths, validation commands, model
  identifiers, provider endpoints, credentials, deployment targets, or
  external actions.
- UI approval creates only an isolated local candidate. The downloadable ZIP
  contains approved candidate files; it does not mutate legacy source or grant
  commit, deployment, Salesforce org, or Mule runtime authority.

## Reporting a vulnerability

Do not include secrets, proprietary source, credentials, or exploit payloads in
a public issue. Report the minimum reproducible description to the repository
owner through the private course/project channel. Include the affected module,
expected boundary, observed behavior, and a synthetic reproduction when
possible.

## Known limitations

The disposable workspace and command policy are application-level controls,
not an OS or container security boundary. Salesforce sandbox validation and
Mule runtime execution require separately governed environments. The complete
threat model, authority boundary, evidence interpretation, and remaining work
are maintained in the README sections [Safety and authority](README.md#safety-and-authority)
and [Limitations and next evidence](README.md#limitations-and-next-evidence).
