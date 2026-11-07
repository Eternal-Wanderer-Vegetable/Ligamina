# P4-04 signed Node runtime probe result

Status: `passed_runtime_only` for the exact Node v24.20.0 executable under a Windows 11 Enterprise 26100 disposable Sandbox with networking disabled.

- Guest report: [`signed-runtime-probe.json`](signed-runtime-probe.json), SHA-256 `31af1927cda2e318aeae085a081bf5e0b47fe4a1f76eb6c94017bda66f0c209d`; sidecar matches.
- Host review: [`phase4-p404-signed-runtime-review-20261028.json`](../phase4-p404-signed-runtime-review-20261028.json), SHA-256 `505b7b962958191a8244e98e1042695ef4058181f7c7dfe641d9da223d9599f7`.
- Policy/profile preflight: [`phase4-p404-policy-preflight-20261028.json`](../phase4-p404-policy-preflight-20261028.json), SHA-256 `5387aa9d029923a5b34f105aca7150ebe5a786e47f9930b8e9ef095156101e27`.
- Exact Node executable SHA-256: `5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5`; Authenticode `Valid`, signer OpenJS Foundation.
- `node --version` returned `v24.20.0`; built-in-only JS probe exited 0 and verified writable evidence mount marker create/read/delete. No network request, policy change, or catalog plugin execution occurred.
- `CiTool -lp -json` returned `0x80073BC3`; complete policy inventory and base mapping remain unknown. Admission=`not_evaluated`, catalog claims=false.
- Sandbox ID `a06a3cea-01a4-4ffa-8688-cb06c7380a31` was stopped; `wsb list --raw` returned an empty environment list.

This result establishes only signed runtime execution. It does not establish Koishi host loading, plugin admission, native dependency loading, behavior compatibility, or the `P4-WINDOWS-BEHAVIOR` gate.
