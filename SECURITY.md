# Security policy

## Supported versions

This project is early-stage. Security fixes are expected to land on the default branch first.

## Reporting a vulnerability

Please do not report security issues in a public GitHub issue.

Use GitHub private vulnerability reporting if it is enabled for this repository. If that option is not available, contact the maintainer privately through GitHub and include:

- A clear description of the issue
- Steps to reproduce
- Impact assessment
- Any proof of concept or logs, with secrets removed

Please do not include API keys, access tokens, personal data, or raw audio from other people unless it is necessary to demonstrate the issue.

## Scope

Examples of relevant issues include:

- Exposure of `TELNYX_API_KEY` or other secrets
- Unsafe handling of microphone input or clipboard contents
- Injection issues in simulated keyboard input or shell commands
- Dependency vulnerabilities that affect local execution
- Insecure logging of sensitive data

## Response goals

The maintainer will try to acknowledge valid reports promptly, assess impact, and prepare a fix before public disclosure.
