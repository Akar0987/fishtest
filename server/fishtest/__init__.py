import hashlib
import os
import subprocess
from pathlib import Path

from fishtest.rundb import RunDb
from pyramid.authentication import AuthTktAuthenticationPolicy
from pyramid.authorization import ACLAuthorizationPolicy
from pyramid.config import Configurator
from pyramid.events import BeforeRender, NewRequest
from pyramid.session import SignedCookieSessionFactory

from fishtest import helpers


def main(global_config, **settings):
    """This function returns a Pyramid WSGI application."""
    session_factory = SignedCookieSessionFactory("fishtest")
    config = Configurator(
        settings=settings,
        session_factory=session_factory,
        root_factory="fishtest.models.RootFactory",
    )
    config.include("pyramid_mako")
    config.set_default_csrf_options(require_csrf=False)

    def static_file_full_path(static_file_path):
        return Path(__file__).parent / "./static/{}".format(static_file_path)

    def static_file_hash(static_file_path):
        with open(static_file_full_path(static_file_path), "r") as f:
            return hashlib.md5(f.read().encode("utf-8")).hexdigest()

    # the same hash calculated by browser for sub-resource integrity checks:
    # https://developer.mozilla.org/en-US/docs/Web/Security/Subresource_Integrity
    dark_theme_sha256_integrity = (
        subprocess.run(
            "openssl dgst -sha256 -binary {} | openssl base64 -A".format(
                str(static_file_full_path(("css/theme.dark.css")))
            ),
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
        )
        .stdout.strip()
        .decode("utf-8")
    )

    cache_busters = {
        "css/application.css": static_file_hash("css/application.css"),
        "css/theme.dark.css": dark_theme_sha256_integrity,
        "js/application.js": static_file_hash("js/application.js"),
        "html/SPRTcalculator.html": static_file_hash("html/SPRTcalculator.html"),
    }

    rundb = RunDb()

    def add_rundb(event):
        event.request.rundb = rundb
        event.request.userdb = rundb.userdb
        event.request.actiondb = rundb.actiondb

    def add_renderer_globals(event):
        event["h"] = helpers
        event["cache_busters"] = cache_busters

    config.add_subscriber(add_rundb, NewRequest)
    config.add_subscriber(add_renderer_globals, BeforeRender)

    # Authentication
    def group_finder(username, request):
        return request.userdb.get_user_groups(username)

    with open(os.path.expanduser("~/fishtest.secret"), "r") as f:
        secret = f.read()
    config.set_authentication_policy(
        AuthTktAuthenticationPolicy(
            secret, callback=group_finder, hashalg="sha512", http_only=True
        )
    )
    config.set_authorization_policy(ACLAuthorizationPolicy())

    config.add_static_view("html", "static/html", cache_max_age=3600)
    config.add_static_view("css", "static/css", cache_max_age=3600)
    config.add_static_view("js", "static/js", cache_max_age=3600)
    config.add_static_view("img", "static/img", cache_max_age=3600)

    config.add_route("home", "/")
    config.add_route("login", "/login")
    config.add_route("nn_upload", "/upload")
    config.add_route("logout", "/logout")
    config.add_route("signup", "/signup")
    config.add_route("user", "/user/{username}")
    config.add_route("profile", "/user")
    config.add_route("pending", "/pending")
    config.add_route("users", "/users")
    config.add_route("users_monthly", "/users/monthly")
    config.add_route("actions", "/actions")
    config.add_route("nns", "/nns")

    config.add_route("tests", "/tests")
    config.add_route("tests_machines", "/tests/machines")
    config.add_route("tests_finished", "/tests/finished")
    config.add_route("tests_run", "/tests/run")
    config.add_route("tests_view", "/tests/view/{id}")
    config.add_route("tests_view_spsa_history", "/tests/view/{id}/spsa_history")
    config.add_route("tests_user", "/tests/user/{username}")
    config.add_route("tests_stats", "/tests/stats/{id}")

    # Tests - actions
    config.add_route("tests_modify", "/tests/modify")
    config.add_route("tests_delete", "/tests/delete")
    config.add_route("tests_stop", "/tests/stop")
    config.add_route("tests_approve", "/tests/approve")
    config.add_route("tests_purge", "/tests/purge")

    # API
    config.add_route("api_request_task", "/api/request_task")
    config.add_route("api_update_task", "/api/update_task")
    config.add_route("api_failed_task", "/api/failed_task")
    config.add_route("api_stop_run", "/api/stop_run")
    config.add_route("api_request_version", "/api/request_version")
    config.add_route("api_beat", "/api/beat")
    config.add_route("api_request_spsa", "/api/request_spsa")
    config.add_route("api_active_runs", "/api/active_runs")
    config.add_route("api_get_run", "/api/get_run/{id}")
    config.add_route("api_get_task", "/api/get_task/{id}/{task_id}")
    config.add_route("api_upload_pgn", "/api/upload_pgn")
    config.add_route("api_download_pgn", "/api/pgn/{id}")
    config.add_route("api_download_pgn_100", "/api/pgn_100/{skip}")
    config.add_route("api_download_nn", "/api/nn/{id}")
    config.add_route("api_get_elo", "/api/get_elo/{id}")

    config.scan()
    return config.make_wsgi_app()
