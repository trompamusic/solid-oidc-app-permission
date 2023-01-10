import jwt
import flask
from flask import request, current_app
from flask_login import login_user, login_required, logout_user

import solid
from solid.admin import init_admin
from solid.backend.db_backend import DBBackend
from solid.backend.redis_backend import RedisBackend
from solid import extensions
from solid import db
from solid.auth import is_safe_url, LoginForm

#backend = RedisBackend(extensions.redis_client)
backend = DBBackend()

def create_app():
    app = flask.Flask(__name__, template_folder="../templates")
    app.config.from_pyfile("../config.py")
    extensions.admin.init_app(app)
    extensions.db.init_app(app)
    extensions.redis_client.init_app(app)
    extensions.login_manager.init_app(app)
    init_admin()

    @extensions.login_manager.user_loader
    def load_user(user_id):
        return db.User.query.filter_by(user=user_id).first()

    with app.app_context():
        # On startup, generate keys if they don't exist
        if backend.is_ready():
            if not backend.get_relying_party_keys():
                new_key = solid.generate_keys()
                backend.save_relying_party_keys(new_key)
        else:
            print("Warning: Backend isn't ready yet")

    return app

webserver_bp = flask.Blueprint('register', __name__)


@webserver_bp.route("/")
def web_index():
    return flask.render_template("index.html")


@webserver_bp.route('/login', methods=['GET', 'POST'])
def login():
    # Here we use a class of some kind to represent and validate our
    # client-side form data. For example, WTForms is a library that will
    # handle this for us, and we use a custom LoginForm to validate.
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


@webserver_bp.route("/register", methods=["POST"])
def web_register():
    client_key = solid.load_key(backend.get_relying_party_keys())
    log_messages = []

    webid = request.form.get("webid")
    provider = solid.lookup_provider_from_profile(webid)
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
        print(f"Configuration for {provider} already exists, quitting")
    else:
        provider_config = solid.get_openid_configuration(provider)
        backend.save_resource_server_configuration(provider, provider_config)

        keys = solid.load_op_jwks(provider_config)
        backend.save_resource_server_keys(provider, keys)

        log_messages.append("Got configuration and jwks for provider")

    client_registration = backend.get_client_registration(provider)
    if client_registration:
        # TODO: Check if redirect url is the same as the one configured here
        log_messages.append(f"Registration for {provider} already exists, skipping")
        print(f"Registration for {provider} already exists, quitting")
    else:
        client_registration = solid.dynamic_registration(provider, current_app.config['REDIRECT_URL'], provider_config)
        backend.save_client_registration(provider, client_registration)

        log_messages.append("Registered client with provider")

    auth_url = solid.generate_authorization_request(provider_config,
                                                    client_registration,
                                                    current_app.config['REDIRECT_URL'],
                                                    client_key)
    log_messages.append("Got an auth url")

    return flask.render_template("register.html", log_messages=log_messages, auth_url=auth_url)


@webserver_bp.route("/redirect")
def web_redirect():
    return flask.render_template("redirect.html")


@webserver_bp.route("/redirect_post", methods=["POST"])
def web_redirect_save():
    body = request.json
    # TODO: If the required fields aren't set
    id_token = body['id_token']
    decoded_token = jwt.decode(id_token, algorithms=["RS256"], options={"verify_signature": False})
    issuer = decoded_token['iss']
    sub = decoded_token['sub']

    # TODO: We need to store more information here, including token expiry information, and
    #  a method to renew the key if needed
    backend.save_configuration_token(issuer, sub, id_token)

    return flask.jsonify({"status": "ok"})
