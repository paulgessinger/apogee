from flask import Blueprint, render_template, request, session, redirect, url_for, g
from apogee.model import CernUser

from apogee.web import oauth, github

bp = Blueprint("auth", __name__, url_prefix="/auth")


@bp.route("/login")
async def login():
    if g.gh_user is not None and g.cern_user is not None:
        return redirect(request.args.get("next", url_for("index")))
    return render_template("login.html")


@bp.route("/login-cern")
def cern_login():
    redirect_uri = url_for(
        "auth.cern_callback", next=request.args.get("next"), _external=True
    )
    return oauth.cern.authorize_redirect(redirect_uri)


@bp.route("/cern-callback")
def cern_callback():
    token = oauth.cern.authorize_access_token()
    session["cern_user"] = CernUser(**token["userinfo"])

    target = request.args.get("next", url_for("index"))

    return redirect(target)


@bp.route("/login-github", methods=["GET", "POST"])
async def login_github():
    redirect_uri = github.authorize()
    return redirect_uri


@bp.route("/github-callback")
@github.authorized_handler
def github_callback(oauth_token):
    next_url = request.args.get("next", url_for("auth.login"))

    session["gh_token"] = oauth_token

    return redirect(next_url)
