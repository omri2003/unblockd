# Security

Report vulnerabilities privately through GitHub: **Security tab > Report a vulnerability** on
[omri2003/unblockd](https://github.com/omri2003/unblockd/security/advisories/new). Please don't open a public issue.

In scope: the web server and REST API, the HTTP MCP server, the CLI, path handling (docs, attachments, `/files`), and the Docker image.
Out of scope: a board served with no token on a trusted machine (that is the documented default), and bugs in dependencies (report those upstream).

Good to know:

- Auth is a single shared token (`UNBLOCKD_TOKEN`), not per-user identity.
- Without a token, the server refuses to start on a non-loopback `HOST` unless `UNBLOCKD_ALLOW_OPEN=1`.
- stdio MCP and the local CLI open the ticket folder directly and skip the token.

Details: [docs/reference.md](docs/reference.md).
