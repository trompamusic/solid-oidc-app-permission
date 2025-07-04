import json
import urllib.parse

import click
import jwcrypto.jwk
import jwcrypto.jwt
from flask import Blueprint, current_app

from solid import constants, extensions
from trompasolid import solid
from trompasolid.authentication import (
    ClientDoesNotSupportDynamicRegistration,
    IDTokenValidationError,
    get_client_id_and_secret_for_provider,
    get_client_url_for_issuer,
    get_jwt_kid,
    select_jwk_by_kid,
    validate_id_token_claims,
)
from trompasolid.backend import SolidBackend
from trompasolid.backend.db_backend import DBBackend
from trompasolid.backend.redis_backend import RedisBackend
from trompasolid.dpop import make_random_string

cli_bp = Blueprint("cli", __name__)


def get_backend() -> SolidBackend:
    # function so that we have access to current_app. This should be an extension
    if current_app.config["BACKEND"] == "db":
        backend = DBBackend(extensions.db.session)
    elif current_app.config["BACKEND"] == "redis":
        backend = RedisBackend(extensions.redis_client)
    return backend


@cli_bp.cli.command()
def create_key():
    """Step 1, Create a local key for use by the service"""
    existing_keys = get_backend().get_relying_party_keys()
    if existing_keys:
        print("Got keys, not generating more")
        return
    keys = solid.generate_keys()
    get_backend().save_relying_party_keys(keys)


@cli_bp.cli.command("get-provider-configuration-from-profile")
@click.argument("profileurl")
def get_provider_configuration_from_profile(profileurl):
    """Step 2a, Look-up a user's vcard and find their OP (OpenID Provider).
    Once you have it, look up the provider's OpenID configuration and save it. If it's previously been saved,
    just load it
    """

    provider = solid.lookup_provider_from_profile(profileurl)
    if not provider:
        print("Cannot find provider, quitting")
        return
    print(f"Provider for this user is: {provider}")

    provider_configuration = get_backend().get_resource_server_configuration(provider)
    provider_keys = get_backend().get_resource_server_keys(provider)

    if provider_configuration and provider_keys:
        print(f"Configuration for {provider} already exists, quitting")
        return

    openid_conf = solid.get_openid_configuration(provider)
    # Get the canonical provider url from the openid configuration (e.g. https://solidcommunity.net vs https://solidcommunity.net/)
    provider = openid_conf.get("issuer", provider)
    get_backend().save_resource_server_configuration(provider, openid_conf)
    print(f"Saved configuration for {provider}")

    provider_keys = solid.load_op_jwks(openid_conf)
    get_backend().save_resource_server_keys(provider, provider_keys)
    print(f"Saved keys for {provider}")


@cli_bp.cli.command("get-provider-configuration")
@click.argument("provider")
def get_provider_configuration(provider):
    """Step 2b, if not using step 2a, just get the provider configuration without knowing the user's profile"""

    provider_configuration = get_backend().get_resource_server_configuration(provider)
    provider_keys = get_backend().get_resource_server_keys(provider)

    if provider_configuration and provider_keys:
        print(f"Configuration for {provider} already exists, quitting")
        return

    openid_conf = solid.get_openid_configuration(provider)
    # Get the canonical provider url from the openid configuration (e.g. https://solidcommunity.net vs https://solidcommunity.net/)
    provider = openid_conf.get("issuer", provider)
    get_backend().save_resource_server_configuration(provider, openid_conf)
    print(f"Saved configuration for {provider}")

    provider_keys = solid.load_op_jwks(openid_conf)
    get_backend().save_resource_server_keys(provider, provider_keys)
    print(f"Saved keys for {provider}")


@cli_bp.cli.command()
@click.argument("provider")
def register(provider):
    """Step 3, Register with the OP.
    Pass in the provider url from `get-provider-configuration` or `get-provider-configuration-from-profile`

    This method is similar to `trompasolid.authentication.generate_authentication_url`, but copied here so that we can
    add additional debugging output when testing.
    """

    provider_config = get_backend().get_resource_server_configuration(provider)

    if not provider_config:
        print("No configuration exists for this provider, use `lookup-op` or `get-provider-configuration` first")
        return

    existing_registration = get_backend().get_client_registration(provider)
    if existing_registration:
        print(f"Registration for {provider} already exists, skipping {existing_registration['client_id']}")
        return

    if not solid.op_can_do_dynamic_registration(provider_config):
        # Provider doesn't support dynamic registration - while solid allows us to use a
        # manually created client ("static registration"), we don't want to deal with this
        raise ClientDoesNotSupportDynamicRegistration(
            f"Provider {provider} does not support dynamic client registration. "
            f"Registration endpoint: {provider_config.get('registration_endpoint', 'not available')}"
        )

    if current_app.config["ALWAYS_USE_CLIENT_URL"]:
        # Generate a client URL that points to our client metadata document
        # Section 5 of the Solid-OIDC spec (https://solidproject.org/TR/oidc#clientids) says
        # OAuth and OIDC require the Client application to identify itself to the OP and RS by presenting a client identifier (Client ID). Solid applications SHOULD use a URI that can be dereferenced as a Client ID Document.
        # this means that "token_endpoint_auth_methods_supported" should include "none", otherwise this is not supported
        # https://github.com/solid/solid-oidc/issues/78
        # If we want to use this, then there is no "registration" step, we just use the URL as the client_id
        # at the auth request step.
        issuer = provider_config["issuer"]
        base_url = current_app.config["BASE_URL"]
        client_id = get_client_url_for_issuer(base_url, issuer)
        print("App config requests what we use a client ID document, not dynamic registration")
        print(f"   (config.ALWAYS_USE_CLIENT_URL is {current_app.config['ALWAYS_USE_CLIENT_URL']})")
        print("as a result, registration doesn't exist. Move directly to auth request")
        return
    else:
        print("Requested to do dynamic client registration")
        client_registration = solid.dynamic_registration(
            provider, constants.client_name, current_app.config["REDIRECT_URL"], provider_config
        )
        get_backend().save_client_registration(provider, client_registration)

        print("Registered client with provider")
        client_id = client_registration["client_id"]
        print(f"Client ID is {client_id}")


@cli_bp.cli.command()
@click.argument("profileurl")
def auth_request(profileurl):
    """Step 4, Perform an authorization request.

    Provide a user's profile url
    """
    provider = solid.lookup_provider_from_profile(profileurl)

    always_use_client_url = current_app.config["ALWAYS_USE_CLIENT_URL"]
    base_url = current_app.config["BASE_URL"]

    provider_configuration = get_backend().get_resource_server_configuration(provider)

    if always_use_client_url:
        print("Using client_id as URL for auth request")
        issuer = provider_configuration["issuer"]
        base_url = current_app.config["BASE_URL"]
        client_id = get_client_url_for_issuer(base_url, issuer)
    else:
        print("Using client from dynamic registration for auth request")
        client_registration = get_backend().get_client_registration(provider)
        if client_registration is None:
            print("No client registration, use `register` first")
            return

        client_id = client_registration["client_id"]

    print(f"Client ID is {client_id}")

    code_verifier, code_challenge = solid.make_verifier_challenge()
    state = make_random_string()

    assert get_backend().get_state_data(state) is None
    get_backend().set_state_data(state, code_verifier)

    auth_url = solid.generate_authorization_request(
        provider_configuration, current_app.config["REDIRECT_URL"], client_id, state, code_challenge
    )
    print(auth_url)


@cli_bp.cli.command()
@click.argument("code")
@click.argument("state")
@click.argument("provider", required=False)
def exchange_auth(code, state, provider):
    """Step 5, Exchange a code for a long-term token.

    Provide a provider url, and the code and state that were returned in the redirect by the provider
    Some providers don't include themselves in the &iss= parameter of the callback url, so if it's not
    available then we'll look it up in the state data.

    This is the same code as `authentication_callback`, but copied here so that we can
    add additional debugging output when testing.
    """

    backend = get_backend()
    redirect_uri = current_app.config["REDIRECT_URL"]
    base_url = current_app.config["BASE_URL"]
    always_use_client_url = current_app.config["ALWAYS_USE_CLIENT_URL"]

    client_id, client_secret = get_client_id_and_secret_for_provider(backend, provider, base_url, always_use_client_url)
    auth = (client_id, client_secret) if client_secret else None

    backend_state = backend.get_state_data(state)
    assert backend_state is not None, f"state {state} not in backend?"
    code_verifier = backend_state["code_verifier"]

    if provider is None:
        print(f"No provider provided, using issuer from state: {backend_state['issuer']}")
        provider = backend_state["issuer"]
    provider_config = backend.get_resource_server_configuration(provider)

    keypair = solid.load_key(backend.get_relying_party_keys())

    success, resp = solid.validate_auth_callback(
        keypair, code_verifier, code, provider_config, client_id, redirect_uri, auth
    )

    if success:
        id_token = resp["id_token"]
        server_jwks = backend.get_resource_server_keys(provider)

        # Extract the key ID from the JWT header
        kid = get_jwt_kid(id_token)

        try:
            # Select the correct key based on the kid
            key = select_jwk_by_kid(server_jwks, kid)

            # Validate and decode the ID token
            decoded_id_token = jwcrypto.jwt.JWT()
            decoded_id_token.deserialize(id_token, key=key)

            claims = json.loads(decoded_id_token.claims)

            # Validate ID token claims according to OpenID Connect Core 1.0
            try:
                validate_id_token_claims(claims, provider, client_id)
            except IDTokenValidationError as e:
                print(f"ID token validation failed: {e}")
                return False, {"error": "invalid_token", "error_description": str(e)}

            if "webid" in claims:
                # The user's web id should be in the 'webid' key, but this doesn't always exist
                # (used to be 'sub'). Node Solid Server still uses sub, but other services put a
                # different value in this field
                webid = claims["webid"]
            else:
                webid = claims["sub"]
            issuer = claims["iss"]
            sub = claims["sub"]
            backend.save_configuration_token(issuer, webid, sub, resp)
            print("Successfully validated ID token and saved configuration")
            return True, resp

        except ValueError as e:
            print(f"Error selecting JWK: {e}")
            return False, {"error": "invalid_token", "error_description": str(e)}
        except (
            jwcrypto.jwt.JWTExpiredError,
            jwcrypto.jwt.JWTInvalidSignatureError,
            jwcrypto.jwt.JWTInvalidClaimError,
            ValueError,
            TypeError,
        ) as e:
            # JWTExpiredError: Token has expired
            # JWTInvalidSignatureError: Invalid signature
            # JWTInvalidClaimError: Invalid claims
            # ValueError: Invalid JWT format
            # TypeError: Invalid key type
            print(f"Error validating ID token: {e}")
            return False, {"error": "invalid_token", "error_description": str(e)}
    else:
        print("Error when validating auth callback")
        return False, resp


@cli_bp.cli.command()
@click.argument("url")
@click.pass_context
def exchange_auth_url(ctx, url):
    """
    Step 5b, Exchange an auth url for a token, from a redirect url
    """
    parts = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parts.query)
    if "code" not in query or "state" not in query:
        print("Missing code, or state in query string")
        return
    if "iss" in query:
        provider = query["iss"][0]
    else:
        print("No issuer in query string, will use provider from state")
        provider = None
    code = query["code"][0]
    state = query["state"][0]
    print(f"Provider: {provider}")
    print(f"Code: {code}")
    print(f"State: {state}")
    ctx.invoke(exchange_auth, code=code, state=state, provider=provider)


@cli_bp.cli.command()
@click.argument("profile")
def refresh(profile):
    provider = solid.lookup_provider_from_profile(profile)
    backend = get_backend()

    keypair = solid.load_key(backend.get_relying_party_keys())
    provider_info = backend.get_resource_server_configuration(provider)

    configuration_token = backend.get_configuration_token(provider, profile)
    if not configuration_token.has_expired():
        print("Configuration token has not expired, skipping refresh")
        return
    always_use_client_url = current_app.config["ALWAYS_USE_CLIENT_URL"]
    base_url = current_app.config["BASE_URL"]
    client_id, client_secret = get_client_id_and_secret_for_provider(backend, provider, base_url, always_use_client_url)

    status, resp = solid.refresh_auth_token(keypair, provider_info, client_id, configuration_token)
    print(f"{status=}")
    print(resp)

    if status:
        backend.update_configuration_token(provider, profile, resp)
        print("Token updated")
    else:
        print(f"Failure updating token: {status}")
