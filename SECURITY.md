# Security

Report vulnerabilities privately, not in a public issue.

## Reporting

1. Preferred: GitHub private vulnerability reporting. Open the repo's Security tab and choose "Report a vulnerability". (The maintainer enables this in the repo settings.)
2. Fallback: email the maintainer at mail+github@schillman.se.

Include:
- Version (`typebay-linkedin-mcp --version`) and MCP client.
- Steps to reproduce.
- Whether a token or a post was exposed.

Never paste tokens, client secrets or keychain contents into a report. Redact them.

## Supported versions

Latest release only.

## Design rules

No credential in any file, log, error message or commit; secrets only via hidden prompt or keychain; least privilege scopes; publish requires a matching preview. Run `gitleaks detect` before committing.
