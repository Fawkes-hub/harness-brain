# Security policy

## Reporting

Open a private security advisory on GitHub, or email the maintainers listed in `pyproject.toml`.
Please do not file a public issue for an unpatched vulnerability. Expect an answer within a week.

## Threat model (what this software defends against)

- **Untrusted network peers.** Every request needs a token; tokens are stored salted and hashed,
  scoped (`read` / `write` / `admin`), and expirable per machine.
- **Untrusted agents.** An agent with `read` scope cannot write; a compromised client can be
  revoked by token id without touching the others.
- **Accidental data loss.** Atoms are immutable, tombstones are append-only, `verify` checks the
  five invariants, and the whole store is a git repo you can roll back.

## What it does *not* defend against

- **Someone with root on the server.** They can read every atom. Encryption at rest is your
  filesystem's job (LUKS, ZFS encryption, …).
- **A misconfigured public deployment.** Binding `0.0.0.0` and port-forwarding 8790 to the internet
  is not supported. Put a reverse proxy with TLS and rate limiting in front, or use a mesh VPN.
- **Secrets in memory content.** `atoms/` is plaintext by design. Do not paste passwords into it;
  `brain secret` (v2.3) is the intended path, and the private key must never live next to the store.

## Hardening checklist

- [ ] Bind to loopback or a private interface; reach it over VPN / SSH tunnel.
- [ ] One token per machine, minimal scope, expiry set.
- [ ] `chmod 600` the client config (`~/.config/brain/config.toml`); never commit it.
- [ ] `BRAIN_TRUST_PROXY=1` only when a real proxy terminates the request.
- [ ] Back up nightly (`git bundle` + `age`) and rehearse a restore monthly.
- [ ] Expose the admin UI to the same audience as the API — it is the same tokens.
