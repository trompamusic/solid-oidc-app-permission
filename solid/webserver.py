import json
from typing import Optional

import jwcrypto
import jwcrypto.jwk
import jwcrypto.jwt
import flask
import zlib
from flask import request, current_app, jsonify, session
from flask_login import login_user, login_required, logout_user

import solid
from solid.admin import init_admin
from trompasolid.backend import SolidBackend
from trompasolid.backend.db_backend import DBBackend
from trompasolid.backend.redis_backend import RedisBackend
from solid import extensions
from solid import db
from solid.auth import is_safe_url, LoginForm
from trompasolid.dpop import make_random_string

backend: Optional[SolidBackend] = None


def create_app():
    app = flask.Flask(__name__, template_folder="../templates")
    app.config.from_pyfile("../config.py")
    extensions.admin.init_app(app)
    extensions.db.init_app(app)
    extensions.redis_client.init_app(app)
    extensions.login_manager.init_app(app)
    init_admin()

    global backend
    if app.config["BACKEND"] == "db":
        backend = DBBackend(extensions.db.session)
    elif app.config["BACKEND"] == "redis":
        backend = RedisBackend(extensions.redis_client)

    @extensions.login_manager.user_loader
    def load_user(user_id):
        return db.User.query.filter_by(user=user_id).first()

    with app.app_context():
        # On startup, generate keys if they don't exist
        if backend.is_ready():
            if not backend.get_relying_party_keys():
                print("On startup generating new RP keys")
                new_key = solid.generate_keys()
                backend.save_relying_party_keys(new_key)
        else:
            print("Warning: Backend isn't ready yet")

    return app


webserver_bp = flask.Blueprint('register', __name__)


@webserver_bp.route("/logo.png")
def logo():
    return flask.current_app.send_static_file("solid-app-logo.png")


@webserver_bp.route("/client/<string:cid>.jsonld")
def client_id_url(cid):
    # In Solid-OIDC you can register a client by having the "client_id" field be a URL to a json-ld document
    # It's normally recommended that this is a static file, but for simplicity serve it from flask

    baseurl = current_app.config['BASE_URL']
    if not baseurl.endswith("/"):
        baseurl += "/"

    client_information = {
        "@context": ["https://www.w3.org/ns/solid/oidc-context.jsonld"],

        "client_id": baseurl + f"client/{cid}.jsonld",
        "client_name": "Alastair's cool test application",
        "redirect_uris": [current_app.config['REDIRECT_URL']],
        "post_logout_redirect_uris": [baseurl + "logout"],
        "client_uri": baseurl,
        "logo_uri": baseurl + "logo.png",
        "tos_uri": baseurl + "tos.html",
        "scope": "openid webid offline_access",
        "grant_types": ["refresh_token", "authorization_code"],
        "response_types": ["code"],
        "default_max_age": 3600,
        "require_auth_time": True
    }

    response = jsonify(client_information)
    response.content_type = "application/ld+json"
    return response


@webserver_bp.route("/")
def web_index():
    profile_url = request.args.get("profile")
    if not profile_url:
        profile_url = ""
    redirect_after = request.args.get("redirect")
    if redirect_after:
        session["redirect_after"] = redirect_after
    return flask.render_template("index.html", profile_url=profile_url)


@webserver_bp.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        # Login and validate the user.

        login_user(form.user)

        flask.flash('Logged in successfully.')

        next = request.args.get('next')
        # is_safe_url should check if the url is safe for redirects.
        # See http://flask.pocoo.org/snippets/62/ for an example.
        if not is_safe_url(next):
            return flask.abort(400)

        return flask.redirect(next or flask.url_for('admin.index'))
    return flask.render_template('login.html', form=form)


@webserver_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return flask.redirect("/")


def get_client_url_for_issuer(baseurl, issuer):
    if not baseurl.endswith("/"):
        baseurl += "/"
    issuer_hash = zlib.adler32(issuer.encode())
    client_url = baseurl + f"client/{issuer_hash}.jsonld"
    return client_url


@webserver_bp.route("/register", methods=["POST"])
def web_register():
    log_messages = []

    webid = request.form.get("webid_or_provider")

    if solid.is_webid(webid):
        provider = solid.lookup_provider_from_profile(webid)
    else:
        provider = webid

    if not provider:
        print("Cannot find provider, quitting")
        log_messages.append(f"Cannot find a provider for webid {webid}")
        return flask.render_template("register.html", log_messages=log_messages)

    log_messages.append(f"Provider for this user is: {provider}")
    print(f"Provider for this user is: {provider}")

    provider_config = backend.get_resource_server_configuration(provider)
    provider_jwks = backend.get_resource_server_keys(provider)
    if provider_config and provider_jwks:
        log_messages.append(f"Configuration for {provider} already exists, skipping setup")
        print(f"Configuration for {provider} already exists, skipping")
    else:
        provider_config = solid.get_openid_configuration(provider)
        backend.save_resource_server_configuration(provider, provider_config)

        keys = solid.load_op_jwks(provider_config)
        backend.save_resource_server_keys(provider, keys)

        log_messages.append("Got configuration and jwks for provider")

    do_dynamic_registration = solid.op_can_do_dynamic_registration(provider_config) and not current_app.config['ALWAYS_USE_CLIENT_URL']
    print("Can do dynamic:", solid.op_can_do_dynamic_registration(provider_config))

    # By default, try and do dynamic registration.
    # If the OP can't do it, send a client URL
    # If ALWAYS_USE_CLIENT_URL is True, send a client URL

    if do_dynamic_registration:
        log_messages.append(f"Requested to do dynamic client registration")
        print(f"Requested to do dynamic client registration")
        client_registration = backend.get_client_registration(provider)
        if client_registration:
            # TODO: Check if redirect url is the same as the one configured here
            log_messages.append(f"Registration for {provider} already exists, skipping")
            print(f"Registration for {provider} already exists, skipping")
        else:
            client_registration = solid.dynamic_registration(provider, current_app.config['REDIRECT_URL'], provider_config)
            backend.save_client_registration(provider, client_registration)

            log_messages.append("Registered client with provider")
        client_id = client_registration["client_id"]
    else:
        log_messages.append(f"Requested to use client URL for requests")
        print(f"Requested to use client URL for requests")

        # TODO: For now, generate a random URL based on the issuer + a basic hash.
        #  For testing this might need to be semi-random in case the provider caches it
        issuer = provider_config["issuer"]
        client_id = get_client_url_for_issuer(current_app.config['BASE_URL'], issuer)
        log_messages.append(f"client_id {client_id}")
        print(f"client_id {client_id}")

    code_verifier, code_challenge = solid.make_verifier_challenge()
    state = make_random_string()

    assert backend.get_state_data(state) is None
    backend.set_state_data(state, code_verifier)

    auth_url = solid.generate_authorization_request(
        provider_config, current_app.config['REDIRECT_URL'],
        client_id,
        state, code_challenge
    )
    log_messages.append("Got an auth url")

    flask.session['provider'] = provider

    return flask.render_template("register.html", log_messages=log_messages, auth_url=auth_url)


@webserver_bp.route("/redirect")
def web_redirect():
    auth_code = flask.request.args.get('code')
    state = flask.request.args.get('state')

    provider = flask.session['provider']
    provider_config = backend.get_resource_server_configuration(provider)

    do_dynamic_registration = solid.op_can_do_dynamic_registration(provider_config) and not current_app.config['ALWAYS_USE_CLIENT_URL']
    if do_dynamic_registration:
        client_registration = backend.get_client_registration(provider)
        if not client_registration:
            raise Exception("Expected to find a registration for a backend but can't get one")
        client_id = client_registration["client_id"]
        client_secret = client_registration["client_secret"]
        auth = (client_id, client_secret)
    else:
        issuer = provider_config["issuer"]
        client_id = get_client_url_for_issuer(current_app.config['BASE_URL'], issuer)
        auth = None

    redirect_uri = current_app.config['REDIRECT_URL']

    code_verifier = backend.get_state_data(state)

    keypair = solid.load_key(backend.get_relying_party_keys())
    assert code_verifier is not None, f"state {state} not in backend?"

    resp = solid.validate_auth_callback(keypair, code_verifier, auth_code, provider_config, client_id, redirect_uri, auth)

    if resp:
        id_token = resp['id_token']
        server_key = backend.get_resource_server_keys(provider)
        # TODO: It seems like a server may give more than one key, is this the correct one?
        # TODO: We need to load the jwt, and from its header find the "kid" (key id) parameter
        #  from this, we can load through the list of server_key keys and find the key with this keyid
        #  and then use that key to validate the message
        key = server_key['keys'][0]
        key = jwcrypto.jwk.JWK.from_json(json.dumps(key))
        decoded_id_token = jwcrypto.jwt.JWT()
        decoded_id_token.deserialize(id_token, key=key)

        claims = json.loads(decoded_id_token.claims)

        if "webid" in claims:
            # The user's web id should be in the 'webid' key, but this doesn't always exist
            # (used to be 'sub'). Node Solid Server still uses sub, but other services put a
            # different value in this field
            webid = claims["webid"]
        else:
            webid = claims["sub"]
        issuer = claims['iss']
        sub = claims['sub']
        backend.save_configuration_token(issuer, webid, sub, resp)

    else:
        print("Error when validating auth callback")

    # TODO: If we want, we can make the original auth page include a redirect URL field, and redirect the user
    #  back to that when this has finished
    # return flask.redirect(STATE_STORAGE[state].pop('redirect_url'))
    redirect_after = session.get("redirect_after")
    return flask.render_template("success.html", redirect_after=redirect_after)
