# JWT Key Rotation (Stage 21)

Both the API session tokens and MCP tokens are HS256 JWTs. Rotation is
zero-downtime via a dual-secret window.

## Procedure
1. Generate a new secret:
   `python -c 'import secrets; print(secrets.token_urlsafe(48))'`
2. In the deployment env:
   - `CELMIS_JWT_SECRET_PREVIOUS=<current value of CELMIS_JWT_SECRET>`
   - `CELMIS_JWT_SECRET=<new secret>`
   - (if MCP uses its own secret: same dance with `MCP_JWT_SECRET` /
     `MCP_JWT_SECRET_PREVIOUS`)
3. Restart the API. New tokens are signed with the new secret; tokens
   signed with the previous secret still verify.
4. After the longest token TTL passes (session tokens live 30 days),
   remove `CELMIS_JWT_SECRET_PREVIOUS` and restart.

## Forced revocation (compromise)
Skip step 3's grace: set only the new `CELMIS_JWT_SECRET` and do NOT
set `_PREVIOUS`. All outstanding tokens become invalid immediately —
every user re-logs in, MCP clients re-run the OAuth flow.
