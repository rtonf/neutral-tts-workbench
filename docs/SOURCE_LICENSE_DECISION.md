# Source-license decision record

The current project-source license is `NOASSERTION`; public distribution is
blocked. This document supplies decision material but makes no license choice
and grants no permission.

An authorized copyright owner or delegated legal/compliance authority must
choose and record one path:

1. keep the source internal and leave public distribution disabled;
2. approve an established license, its exact text, notices, and distribution
   scope; or
3. approve separately reviewed terms prepared for this source and scope.

The [SPDX license guidance](https://spdx.dev/learn/handling-license-info/)
provides standardized identifiers and expressions, while the
[OSI license list](https://opensource.org/licenses) identifies licenses that
have completed OSI review. Those references help identify a chosen license;
they do not choose one or establish authority for this repository.

## Required authority record

Before changing the ledger from blocked status, record all of the following in
an approved, immutable decision system:

- exact Git commit and tree covered by the decision;
- copyright owner or other grant authority and the basis for that authority;
- approved distribution audience and effective date;
- exact SPDX expression or `LicenseRef`, as applicable;
- exact license-text path and SHA-256;
- exact copyright and notice text;
- contributor-rights or contribution-policy decision;
- compatibility review for every bundled dependency;
- approver identity, role, decision identifier, and decision-record SHA-256.

Only the authorized record may supply these facts. Automation must copy them
exactly, verify their hashes, and leave the release gate blocked if any field is
absent, inconsistent, or outside the approved scope.
