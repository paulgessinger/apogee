from flask import (
    Blueprint,
    current_app,
    render_template,
    request,
    session,
    redirect,
    url_for,
    g,
)
from authlib.integrations.flask_client import OAuth

from apogee.model import CernUser
from apogee import config


def update_token(name, token, refresh_token=None, access_token=None):
    if name != "github":
        raise RuntimeError("No CERN tokens to fetch")

    session["gh_token"] = token


def fetch_token(name):
    if name != "github":
        raise RuntimeError("No CERN tokens to fetch")

    return session.get("gh_token")


oauth = OAuth(update_token=update_token, fetch_token=fetch_token)

oauth.register(
    name="cern",
    server_metadata_url=config.CERN_AUTH_METADATA_URL,
    client_id=config.CERN_AUTH_CLIENT_ID,
    client_secret=config.CERN_AUTH_CLIENT_SECRET,
    client_kwargs={"scope": "openid email profile"},
)

oauth.register(
    name="github",
    client_id=config.GITHUB_CLIENT_ID,
    client_secret=config.GITHUB_CLIENT_SECRET,
    authorize_url="https://github.com/login/oauth/authorize",
    access_token_url="https://github.com/login/oauth/access_token",
    userinfo_endpoint="https://api.github.com/user",
    api_base_url="https://api.github.com",
    client_kwargs={"scope": "user:email"},
)


bp = Blueprint("auth", __name__, url_prefix="/auth")


@bp.get("/login")
async def login():
    #  print(g.gh_user, g.cern_user)
    if ("gh_user" in g and g.gh_user is not None and "gh_token" in session) and (
        "cern_user" in g and g.cern_user is not None
    ):
        return redirect(request.args.get("next", url_for("index")))
    return render_template("login.html")


@bp.get("/login-cern")
def cern_login():
    redirect_uri = url_for(
        "auth.cern_callback",
        next=request.args.get("next"),
        _external=True,
    )
    return oauth.cern.authorize_redirect(redirect_uri)


@bp.get("/cern-callback")
def cern_callback():
    token = oauth.cern.authorize_access_token()
    session["cern_user"] = CernUser(**token["userinfo"])

    target = request.args.get("next", url_for("index"))

    return redirect(target)


@bp.get("/login-github")
async def login_github():
    redirect_uri = url_for(
        "auth.github_callback",
        next=request.args.get("next"),
        _external=True,
    )
    return oauth.github.authorize_redirect(redirect_uri)


@bp.get("/github-callback")
def github_callback():
    next_url = request.args.get("next", url_for("auth.login"))

    token = oauth.github.authorize_access_token()
    session["gh_token"] = token

    return redirect(next_url)
