"""Garmin MCP server hosted on Modal."""

import subprocess

import modal


def _git_info() -> tuple[str, str]:
    """Capture short HEAD sha and dirty flag at deploy time.

    Runs locally during `modal deploy` (where `.git` exists) and bakes the
    values into the image as env vars. Inside the container the subprocess
    falls through to "unknown", but that fallback is unreachable in practice
    because `image.env` has already set the values from the deploy host.
    """
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = "1" if subprocess.check_output(
            ["git", "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL
        ).strip() else "0"
        return sha, dirty
    except Exception:
        return "unknown", "0"


_git_sha, _git_dirty = _git_info()

image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("git")
    # Installing garmin-mcp separately first so that we can avoid
    # dependency conflicts when installing the other libraries
    .uv_pip_install("garmin-mcp@git+https://github.com/Taxuspt/garmin_mcp.git")
    .uv_pip_install(
        "curl-cffi>=0.15.0",
        "fastapi>=0.136.1",
        "fastmcp>=2.14.0,<3",
        "mcp>=1.27.0,<2",
    )
    .add_local_python_source("garmin_session")
    .env({"GIT_COMMIT": _git_sha, "GIT_DIRTY": _git_dirty})
)

with image.imports():
    import base64
    import hashlib
    import hmac
    import json
    import os
    import secrets
    import time
    from urllib.parse import urlencode

    from fastapi import FastAPI, Request  # ty:ignore[unresolved-import]
    from fastapi.responses import JSONResponse, RedirectResponse  # ty:ignore[unresolved-import]
    from fastmcp import Client  # ty:ignore[unresolved-import]
    from fastmcp.client.transports import StreamableHttpTransport  # ty:ignore[unresolved-import]
    from garmin_mcp import (
        activity_management,
        challenges,
        data_management,
        devices,
        gear_management,
        health_wellness,
        training,
        user_profile,
        weight_management,
        workout_templates,
        workouts,
    )
    from garminconnect import Garmin
    from mcp.server.auth.middleware.bearer_auth import AccessToken
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import TransportSecuritySettings

    from garmin_session import install_curl_impersonation

app = modal.App(
    name="garmin_mcp",
    image=image,
    secrets=[modal.Secret.from_name("garmin-tokens"), modal.Secret.from_name("mcp-auth")],
)


@app.function()
@modal.asgi_app()
def endpoint():
    """ASGI web endpoint for the MCP server."""
    tokens_base64 = os.environ.get("GARMINTOKENS_BASE64")
    if not tokens_base64:
        raise RuntimeError(
            "GARMINTOKENS_BASE64 secret is not set. Run: modal secret create garmin-tokens GARMINTOKENS_BASE64=$(cat ~/.garminconnect_base64)"
        )

    mcp_bearer_token = os.environ.get("MCP_BEARER_TOKEN")
    if not mcp_bearer_token:
        raise RuntimeError(
            "MCP_BEARER_TOKEN secret is not set. Run: modal secret create mcp-auth MCP_BEARER_TOKEN=<your-secret>"
        )

    class StaticBearerVerifier:
        """Accepts a single static bearer token — simple cross-device auth."""

        async def verify_token(self, token: str) -> AccessToken | None:
            if token == mcp_bearer_token:
                return AccessToken(token=token, client_id="static", scopes=[])
            return None

    garmin_client = Garmin()
    # Pin a curl_cffi adapter on garth's session before login so it survives
    # `configure()` calls inside login (which would otherwise replace it with a
    # vanilla HTTPAdapter, breaking OAuth2 refresh with 429s).
    install_curl_impersonation(garmin_client.garth)
    garmin_client.login(tokenstore=tokens_base64)
    # Always re-fetch from /userprofile/profile rather than trusting login()'s
    # branchy code path — we've observed it leave display_name None when the
    # /socialProfile branch runs and the response lacks the field. A None here
    # makes every URL `daily/None` → 403. Hard-fail if the canonical endpoint
    # doesn't yield a displayName so Modal restarts the container instead of
    # serving broken state.
    prof = garmin_client.garth.connectapi("/userprofile-service/userprofile/profile")
    if not isinstance(prof, dict) or not prof.get("displayName"):
        raise RuntimeError(f"Garmin profile fetch returned no displayName (got {prof!r})")
    garmin_client.display_name = prof["displayName"]
    garmin_client.full_name = prof.get("fullName") or garmin_client.full_name

    print(
        f"[startup] commit={os.environ.get('GIT_COMMIT', '?')} "
        f"dirty={os.environ.get('GIT_DIRTY', '?')} "
        f"display_name={garmin_client.display_name!r}"
    )

    activity_management.configure(garmin_client)
    health_wellness.configure(garmin_client)
    user_profile.configure(garmin_client)
    devices.configure(garmin_client)
    gear_management.configure(garmin_client)
    weight_management.configure(garmin_client)
    challenges.configure(garmin_client)
    training.configure(garmin_client)
    workouts.configure(garmin_client)
    data_management.configure(garmin_client)

    base_url = endpoint.get_web_url()

    fast_mcp_app = FastMCP(
        "Garmin Connect v1.0",
        stateless_http=True,
        token_verifier=StaticBearerVerifier(),
        # AuthSettings is required whenever token_verifier is set; it tells
        # FastMCP which OAuth issuer it should advertise to clients and which
        # URL to use as the protected-resource identifier.
        auth=AuthSettings(issuer_url=base_url, resource_server_url=f"{base_url}/mcp/"),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    fast_mcp_app = activity_management.register_tools(fast_mcp_app)
    fast_mcp_app = health_wellness.register_tools(fast_mcp_app)
    fast_mcp_app = user_profile.register_tools(fast_mcp_app)
    fast_mcp_app = devices.register_tools(fast_mcp_app)
    fast_mcp_app = gear_management.register_tools(fast_mcp_app)
    fast_mcp_app = weight_management.register_tools(fast_mcp_app)
    fast_mcp_app = challenges.register_tools(fast_mcp_app)
    fast_mcp_app = training.register_tools(fast_mcp_app)
    fast_mcp_app = workouts.register_tools(fast_mcp_app)
    fast_mcp_app = data_management.register_tools(fast_mcp_app)
    # Skipping Garmin features that I'm not using:
    # fast_mcp_app = womens_health.register_tools(fast_mcp_app)  # noqa: ERA001
    # fast_mcp_app = nutrition.register_tools(fast_mcp_app)  # noqa: ERA001

    # Register resources (workout templates)
    fast_mcp_app = workout_templates.register_resources(fast_mcp_app)

    # Use streamable HTTP transport for stateless compatibility with Modal
    mcp_app = fast_mcp_app.streamable_http_app()

    fastapi_app = FastAPI(lifespan=mcp_app.router.lifespan_context)

    # ── OAuth 2.0 Authorization Code + PKCE ───────────────────────────────
    # Claude.ai's custom-connector UI only drives Authorization Code with PKCE,
    # so we wrap the static MCP_BEARER_TOKEN in that flow:
    #   /authorize  — auto-approves (the user's proof of authorization is the
    #                 client_secret they pasted into the UI; nothing to ask
    #                 them in a browser) and 302s back with a signed code.
    #   /token      — verifies client_secret + PKCE + code signature, then
    #                 returns MCP_BEARER_TOKEN as the access token.
    #
    # Codes are HMAC-signed (key = MCP_BEARER_TOKEN) and self-contained, so the
    # flow stays stateless across Modal containers — no shared store needed.
    # Routes MUST be declared before the catch-all mount below, otherwise
    # Starlette's Mount("/") matches first and the OAuth endpoints become
    # unreachable.

    CLAUDE_CALLBACKS = {
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    }
    CODE_TTL_SECONDS = 300

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def _b64url_decode(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def _sign_code(payload: dict) -> str:
        body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
        sig = hmac.new(mcp_bearer_token.encode(), body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def _verify_code(code: str) -> dict | None:
        try:
            body, sig = code.rsplit(".", 1)
        except ValueError:
            return None
        expected = hmac.new(mcp_bearer_token.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            payload = json.loads(_b64url_decode(body))
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("exp", 0) < int(time.time()):
            return None
        return payload

    @fastapi_app.get("/.well-known/oauth-authorization-server")
    async def oauth_metadata():
        return JSONResponse(
            {
                "issuer": base_url,
                "authorization_endpoint": f"{base_url}/authorize",
                "token_endpoint": f"{base_url}/token",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_post"],
            }
        )

    @fastapi_app.get("/.well-known/oauth-protected-resource/mcp")
    async def resource_metadata():
        return JSONResponse(
            {
                "resource": f"{base_url}/mcp/",
                "authorization_servers": [base_url],
                "bearer_methods_supported": ["header"],
            }
        )

    @fastapi_app.get("/authorize")
    async def authorize(
        response_type: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        state: str | None = None,
    ):
        if response_type != "code":
            return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
        if redirect_uri not in CLAUDE_CALLBACKS:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uri not allowed"},
                status_code=400,
            )
        if code_challenge_method != "S256":
            return JSONResponse(
                {"error": "invalid_request", "error_description": "S256 PKCE required"},
                status_code=400,
            )

        code = _sign_code(
            {
                "cid": client_id,
                "ru": redirect_uri,
                "cc": code_challenge,
                "exp": int(time.time()) + CODE_TTL_SECONDS,
                "n": secrets.token_hex(8),
            }
        )
        params = {"code": code}
        if state:
            params["state"] = state
        return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)

    @fastapi_app.post("/token")
    async def token(request: Request):
        form = await request.form()
        if form.get("grant_type") != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        client_secret = form.get("client_secret") or ""
        if not hmac.compare_digest(client_secret, mcp_bearer_token):
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        payload = _verify_code(form.get("code") or "")
        if payload is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        if payload.get("cid") != form.get("client_id") or payload.get("ru") != form.get("redirect_uri"):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        code_verifier = form.get("code_verifier") or ""
        challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
        if not hmac.compare_digest(challenge, payload.get("cc", "")):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        return JSONResponse(
            {
                "access_token": mcp_bearer_token,
                "token_type": "bearer",
                "expires_in": 3600,
            }
        )

    # Catch-all MCP mount — must be registered LAST so it doesn't shadow the
    # OAuth routes above.
    fastapi_app.mount("/", mcp_app, "mcp")

    return fastapi_app


@app.function()
async def test_tool(tool_name: str | None = None):
    """Make sure that we can run tools from the MCP server."""
    if tool_name is None:
        tool_name = "get_full_name"

    bearer_token = os.environ.get("MCP_BEARER_TOKEN")
    if not bearer_token:
        raise RuntimeError("MCP_BEARER_TOKEN must be set to test the authenticated endpoint.")

    transport = StreamableHttpTransport(
        url=f"{endpoint.get_web_url()}/mcp/",
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    client = Client(transport)

    async with client:
        tools = await client.list_tools()

        for tool in tools:
            print(tool)
            if tool.name == tool_name:
                result = await client.call_tool(tool_name)
                print(result.data)
                return

    raise Exception(f"could not find tool {tool_name}")
