import copy
import datetime
import hashlib
import html
import os
import re
import threading
import time

import fishtest.stats.stat_util
import requests
from fishtest.util import (
    delta_date,
    diff_date,
    estimate_game_duration,
    format_results,
    get_chi2,
    password_strength,
    update_residuals,
)
from pyramid.httpexceptions import HTTPFound, exception_response
from pyramid.security import forget, remember
from pyramid.view import forbidden_view_config, view_config

HTTP_TIMEOUT = 15.0


def clear_cache():
    global last_time, last_tests
    building.acquire()
    last_time = 0
    last_tests = None
    building.release()


def cached_flash(request, requestString):
    clear_cache()
    request.session.flash(requestString)
    return


@view_config(route_name="home")
def home(request):
    return HTTPFound(location=request.route_url("tests"))


@view_config(
    route_name="login",
    renderer="login.mak",
    require_csrf=True,
    request_method=("GET", "POST"),
)
@forbidden_view_config(renderer="login.mak")
def login(request):
    login_url = request.route_url("login")
    referrer = request.url
    if referrer == login_url:
        referrer = "/"  # never use the login form itself as came_from
    came_from = request.params.get("came_from", referrer)

    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        token = request.userdb.authenticate(username, password)
        if "error" not in token:
            if request.POST.get("stay_logged_in"):
                # Session persists for a year after login
                headers = remember(request, username, max_age=60 * 60 * 24 * 365)
            else:
                # Session ends when the browser is closed
                headers = remember(request, username)
            next_page = request.params.get("next") or came_from
            return HTTPFound(location=next_page, headers=headers)
        message = token["error"]
        if "Account blocked for user:" in message:
            message += (
                " . If you recently registered to fishtest, "
                "a person will now manually approve your new account, to avoid spam. "
                "This is usually quick, but sometimes takes a few hours. "
                "Thank you!"
            )
        request.session.flash(message, "error")
    return {}


# Guard against upload timeouts/retries
uploading = threading.Semaphore()


@view_config(route_name="nn_upload", renderer="nn_upload.mak", require_csrf=True)
def upload(request):
    if not uploading.acquire(False):
        request.session.flash(
            "An other upload is in progress, please try again later", "error"
        )
        return {}
    result = sync_upload(request)
    uploading.release()
    return result


def sync_upload(request):
    userid = request.authenticated_userid
    if not userid:
        request.session.flash("Please login")
        return HTTPFound(location=request.route_url("login"))
    if request.method != "POST":
        return {}
    try:
        filename = request.POST["network"].filename
        input_file = request.POST["network"].file
        network = input_file.read()
    except AttributeError:
        request.session.flash(
            "Specify a network file with the 'Choose File' button", "error"
        )
        return {}
    except Exception as e:
        print("Error reading the network file: " + str(e))
        request.session.flash("Error reading the network file", "error")
        return {}
    if request.rundb.get_nn(filename):
        request.session.flash("Network already exists", "error")
        return {}
    errors = []
    if len(network) >= 120000000:
        errors.append("Network must be < 120MB")
    if not re.match(r"^nn-[0-9a-f]{12}\.nnue$", filename):
        errors.append('Name must match "nn-[SHA256 first 12 digits].nnue"')
    hash = hashlib.sha256(network).hexdigest()
    if hash[:12] != filename[3:15]:
        errors.append(
            "Wrong SHA256 hash: " + hash[:12] + " Filename: " + filename[3:15]
        )
    if errors:
        for error in errors:
            request.session.flash(error, "error")
        return {}
    try:
        with open(os.path.expanduser("~/fishtest.upload"), "r") as f:
            upload_server = f.read().replace("\n", "")
            upload_server = upload_server + "/" + filename
    except Exception as e:
        print("Network upload not configured: " + str(e))
        request.session.flash("Network upload not configured", "error")
        return {}
    try:
        response = requests.post(upload_server, data=network, timeout=HTTP_TIMEOUT * 20)
        response.raise_for_status()
    except Exception as e:
        print("Network upload failed: " + str(e))
        request.session.flash(
            "Network upload failed: " + str(response.status_code), "error"
        )
        return {}

    request.actiondb.upload_nn(request.authenticated_userid, filename)
    request.rundb.upload_nn(userid, filename, network)

    return HTTPFound(location=request.route_url("nns"))


@view_config(route_name="logout", require_csrf=True, request_method="POST")
def logout(request):
    session = request.session
    headers = forget(request)
    session.invalidate()
    return HTTPFound(location=request.route_url("tests"), headers=headers)


@view_config(
    route_name="signup",
    renderer="signup.mak",
    require_csrf=True,
    request_method=("GET", "POST"),
)
def signup(request):
    if request.method != "POST":
        return {}
    errors = []

    signup_username = request.POST.get("username", "")
    signup_password = request.POST.get("password", "")
    signup_password_verify = request.POST.get("password2", "")
    signup_email = request.POST.get("email", "")

    strong_password, password_err = password_strength(
        signup_password, signup_username, signup_email
    )
    if not strong_password:
        errors.append("Error! Weak password: " + password_err)
    if signup_password != signup_password_verify:
        errors.append("Error! Matching verify password required")
    if "@" not in signup_email:
        errors.append("Error! Email required")
    if len(signup_username) == 0:
        errors.append("Error! Username required")
    if not signup_username.isalnum():
        errors.append("Error! Alphanumeric username required")
    if errors:
        for error in errors:
            request.session.flash(error, "error")
        return {}

    path = os.path.expanduser("~/fishtest.captcha.secret")
    if os.path.exists(path):
        with open(path, "r") as f:
            secret = f.read()
            payload = {
                "secret": secret,
                "response": request.POST.get("g-recaptcha-response", ""),
                "remoteip": request.remote_addr,
            }
            response = requests.post(
                "https://www.google.com/recaptcha/api/siteverify",
                data=payload,
                timeout=HTTP_TIMEOUT,
            ).json()
            if "success" not in response or not response["success"]:
                if "error-codes" in response:
                    print(response["error-codes"])
                request.session.flash("Captcha failed", "error")
                return {}

    result = request.userdb.create_user(
        username=signup_username, password=signup_password, email=signup_email
    )
    if not result:
        request.session.flash("Error! Invalid username or password", "error")
    else:
        request.session.flash(
            "Account created! "
            "To avoid spam, a person will now manually approve your new account. "
            "This is usually quick but sometimes takes a few hours. "
            "Thank you for contributing!"
        )
        return HTTPFound(location=request.route_url("login"))
    return {}


@view_config(route_name="nns", renderer="nns.mak")
def nns(request):

    page_size = 250
    page = int(request.params.get("page", 1))
    skip = max(0, page - 1) * page_size
    nns_list = []

    for nn in request.rundb.get_nns(page_size, skip=skip):
        nns_list.append(nn)
    if len(nns_list) == page_size:
        next_page = (skip // page_size) + 2
    else:
        next_page = None
    if page > 1:
        prev_page = page - 1
    else:
        prev_page = None
    return {
        "nns": nns_list,
        "next_page": next_page,
        "prev_page": prev_page,
        "non_default_shown": request.cookies.get("non_default_state", "Show") == "Show",
    }


@view_config(route_name="actions", renderer="actions.mak")
def actions(request):
    search_action = request.params.get("action", "")
    search_user = request.params.get("user", "")
    search_before = request.params.get("before", None)
    try:
        search_before = datetime.datetime.utcfromtimestamp(float(search_before))
    except:
        search_before = datetime.datetime.utcnow()
    count = request.params.get("count", "100")
    try:
        count = int(count)
    except:
        count = 100
    count = min(count, 100)

    actions_list = []
    for action in request.actiondb.get_actions(
        count, search_action, search_user, search_before
    ):
        item = {
            "action": action["action"],
            "time": action["time"],
            "username": action["username"],
        }
        if action["action"] == "update_stats":
            item["user"] = ""
            item["description"] = "Update user statistics"
        elif action["action"] == "upload_nn":
            item["user"] = ""
            item["description"] = "Upload " + action["data"]
        elif action["action"] == "block_user":
            item["description"] = (
                "blocked" if action["data"]["blocked"] else "unblocked"
            )
            item["user"] = action["data"]["user"]
        elif action["action"] == "modify_run":
            item["run"] = action["data"]["before"]["args"]["new_tag"]
            item["_id"] = action["data"]["before"]["_id"]
            item["description"] = []

            before = action["data"]["before"]["args"]["priority"]
            after = action["data"]["after"]["args"]["priority"]
            if before != after:
                item["description"].append(
                    "priority changed from {} to {}".format(before, after)
                )

            before = action["data"]["before"]["args"]["num_games"]
            after = action["data"]["after"]["args"]["num_games"]
            if before != after:
                item["description"].append(
                    "games changed from {} to {}".format(before, after)
                )

            before = action["data"]["before"]["args"]["throughput"]
            after = action["data"]["after"]["args"]["throughput"]
            if before != after:
                item["description"].append(
                    "throughput changed from {} to {}".format(before, after)
                )

            before = action["data"]["before"]["args"]["auto_purge"]
            after = action["data"]["after"]["args"]["auto_purge"]
            if before != after:
                item["description"].append(
                    "auto-purge changed from {} to {}".format(before, after)
                )

            item["description"] = "modify: " + ", ".join(item["description"])
        else:
            item["run"] = action["data"]["args"]["new_tag"]
            item["_id"] = action["data"]["_id"]
            item["description"] = " ".join(action["action"].split("_"))
            if action["action"] == "failed_task":
                item["description"] += ": {}".format(
                    action["data"].get("failure_reason", "Unknown reason")
                )
            if action["action"] == "dead_task":
                item["description"] += ": {}".format(action["data"].get("dead_task"))
            if action["action"] == "stop_run":
                item["description"] += ": {}".format(
                    action["data"].get("stop_reason", "User stop")
                )

        actions_list.append(item)

    return {"actions": actions_list, "approver": request.has_permission("approve_run")}


def get_idle_users(request):
    idle = {}
    for u in request.userdb.get_users():
        idle[u["username"]] = u
    for u in request.userdb.user_cache.find():
        del idle[u["username"]]
    idle = list(idle.values())
    return idle


@view_config(route_name="pending", renderer="pending.mak")
def pending(request):
    if not request.has_permission("approve_run"):
        request.session.flash("You cannot view pending users", "error")
        return HTTPFound(location=request.route_url("tests"))

    return {"users": request.userdb.get_pending(), "idle": get_idle_users(request)}


@view_config(route_name="user", renderer="user.mak")
@view_config(route_name="profile", renderer="user.mak")
def user(request):
    userid = request.authenticated_userid
    if not userid:
        request.session.flash("Please login")
        return HTTPFound(location=request.route_url("login"))
    user_name = request.matchdict.get("username", userid)
    profile = user_name == userid
    if not profile and not request.has_permission("approve_run"):
        request.session.flash("You cannot inspect users", "error")
        return HTTPFound(location=request.route_url("tests"))
    user_data = request.userdb.get_user(user_name)
    if "user" in request.POST:
        if profile:

            new_password = request.params.get("password")
            new_password_verify = request.params.get("password2", "")
            new_email = request.params.get("email")

            if len(new_password) > 0:
                if new_password == new_password_verify:
                    strong_password, password_err = password_strength(
                        new_password,
                        user_name,
                        user_data["email"],
                        (new_email if len(new_email) > 0 else None),
                    )
                    if strong_password:
                        user_data["password"] = new_password
                        request.session.flash("Success! Password updated")
                    else:
                        request.session.flash(
                            "Error! Weak password: " + password_err, "error"
                        )
                        return HTTPFound(location=request.route_url("tests"))
                else:
                    request.session.flash(
                        "Error! Matching verify password required", "error"
                    )
                    return HTTPFound(location=request.route_url("tests"))

            if len(new_email) > 0 and user_data["email"] != new_email:
                if "@" not in new_email:
                    request.session.flash("Error! Valid email required", "error")
                    return HTTPFound(location=request.route_url("tests"))
                else:
                    user_data["email"] = new_email
                    request.session.flash("Success! Email updated")

        else:
            user_data["blocked"] = "blocked" in request.POST
            request.userdb.last_pending_time = 0
            request.actiondb.block_user(
                request.authenticated_userid,
                {"user": user_name, "blocked": user_data["blocked"]},
            )
            request.session.flash(
                ("Blocked" if user_data["blocked"] else "Unblocked")
                + " user "
                + user_name
            )
        request.userdb.save_user(user_data)
        return HTTPFound(location=request.route_url("tests"))
    userc = request.userdb.user_cache.find_one({"username": user_name})
    hours = int(userc["cpu_hours"]) if userc is not None else 0
    return {
        "user": user_data,
        "limit": request.userdb.get_machine_limit(user_name),
        "hours": hours,
        "profile": profile,
    }


@view_config(route_name="users", renderer="users.mak")
def users(request):
    users_list = list(request.userdb.user_cache.find())
    users_list.sort(key=lambda k: k["cpu_hours"], reverse=True)
    return {"users": users_list}


@view_config(route_name="users_monthly", renderer="users.mak")
def users_monthly(request):
    users_list = list(request.userdb.top_month.find())
    users_list.sort(key=lambda k: k["cpu_hours"], reverse=True)
    return {"users": users_list}


def get_master_bench():
    bs = re.compile(r"(^|\s)[Bb]ench[ :]+([0-9]+)", re.MULTILINE)
    for c in requests.get(
        "https://api.github.com/repos/official-stockfish/Stockfish/commits"
    ).json():
        if "commit" not in c:
            return None
        m = bs.search(c["commit"]["message"])
        if m:
            return m.group(2)
    return None


def get_sha(branch, repo_url):
    """Resolves the git branch to sha commit"""
    api_url = repo_url.replace("https://github.com", "https://api.github.com/repos")
    try:
        commit = requests.get(api_url + "/commits/" + branch).json()
    except:
        raise Exception("Unable to access developer repository")
    if "sha" in commit:
        return commit["sha"], commit["commit"]["message"].split("\n")[0]
    else:
        return "", ""


def get_net(commit_sha, repo_url):
    """Get the net from evaluate.h or ucioption.cpp in the repo"""
    api_url = repo_url.replace(
        "https://github.com", "https://raw.githubusercontent.com"
    )
    try:
        net = None

        url1 = api_url + "/" + commit_sha + "/src/evaluate.h"
        options = requests.get(url1).content.decode("utf-8")
        for line in options.splitlines():
            if "EvalFileDefaultName" in line and "define" in line:
                p = re.compile("nn-[a-z0-9]{12}.nnue")
                m = p.search(line)
                if m:
                    net = m.group(0)

        if net:
            return net

        url2 = api_url + "/" + commit_sha + "/src/ucioption.cpp"
        options = requests.get(url2).content.decode("utf-8")
        for line in options.splitlines():
            if "EvalFile" in line and "Option" in line:
                p = re.compile("nn-[a-z0-9]{12}.nnue")
                m = p.search(line)
                if m:
                    net = m.group(0)
        return net
    except:
        raise Exception("Unable to access developer repository: " + api_url)


def parse_spsa_params(raw, spsa):
    params = []
    for line in raw.split("\n"):
        chunks = line.strip().split(",")
        if len(chunks) == 1 and chunks[0] == "":  # blank line
            continue
        if len(chunks) != 6:
            raise Exception("the line {} does not have 6 entries".format(chunks))
        param = {
            "name": chunks[0],
            "start": float(chunks[1]),
            "min": float(chunks[2]),
            "max": float(chunks[3]),
            "c_end": float(chunks[4]),
            "r_end": float(chunks[5]),
        }
        param["c"] = param["c_end"] * spsa["num_iter"] ** spsa["gamma"]
        param["a_end"] = param["r_end"] * param["c_end"] ** 2
        param["a"] = param["a_end"] * (spsa["A"] + spsa["num_iter"]) ** spsa["alpha"]
        param["theta"] = param["start"]
        params.append(param)
    return params


def validate_form(request):
    data = {
        "base_tag": request.POST["base-branch"],
        "new_tag": request.POST["test-branch"],
        "tc": request.POST["tc"],
        "new_tc": request.POST["new_tc"],
        "book": request.POST["book"],
        "book_depth": request.POST["book-depth"],
        "base_signature": request.POST["base-signature"],
        "new_signature": request.POST["test-signature"],
        "base_options": request.POST["base-options"],
        "new_options": request.POST["new-options"],
        "username": request.authenticated_userid,
        "tests_repo": request.POST["tests-repo"],
        "info": request.POST["run-info"],
    }
    odds = request.POST.get("odds", "off")  # off checkboxes are not posted
    if odds == "off":
        data["new_tc"] = data["tc"]

    if not re.match(r"^([1-9]\d*/)?\d+(\.\d+)?(\+\d+(\.\d+)?)?$", data["tc"]):
        raise Exception("Bad time control format (base TC)")

    if not re.match(r"^([1-9]\d*/)?\d+(\.\d+)?(\+\d+(\.\d+)?)?$", data["new_tc"]):
        raise Exception("Bad time control format (new TC)")

    if request.POST.get("rescheduled_from"):
        data["rescheduled_from"] = request.POST["rescheduled_from"]

    def strip_message(m):
        s = re.sub(r"[Bb]ench[ :]+[0-9]+\s*", "", m)
        s = re.sub(r"[ \t]+", " ", s)
        s = re.sub(r"\n+", r"\n", s)
        return s.rstrip()

    # Fill new_signature/info from commit info if left blank
    if len(data["new_signature"]) == 0 or len(data["info"]) == 0:
        api_url = data["tests_repo"].replace(
            "https://github.com", "https://api.github.com/repos"
        )
        api_url += "/commits" + "/" + data["new_tag"]
        try:
            c = requests.get(api_url).json()
        except:
            raise Exception("Unable to access developer repository")
        if "commit" not in c:
            raise Exception("Cannot find branch in developer repository")
        if len(data["new_signature"]) == 0:
            bs = re.compile(r"(^|\s)[Bb]ench[ :]+([0-9]+)", re.MULTILINE)
            m = bs.search(c["commit"]["message"])
            if m:
                data["new_signature"] = m.group(2)
            else:
                raise Exception(
                    "This commit has no signature: please supply it manually."
                )
        if len(data["info"]) == 0:
            data["info"] = (
                "" if re.match(r"^[012]?[0-9][^0-9].*", data["tc"]) else "LTC: "
            ) + strip_message(c["commit"]["message"])

    # Check that the book exists in the official books repo
    if len(data["book"]) > 0:
        api_url = "https://api.github.com/repos/official-stockfish/books/contents"
        c = requests.get(api_url).json()
        matcher = re.compile(r"\.(epd|pgn)\.zip$")
        valid_book_filenames = [
            file["name"] for file in c if matcher.search(file["name"])
        ]
        if data["book"] + ".zip" not in valid_book_filenames:
            raise Exception("Invalid book - " + data["book"])

    if request.POST["stop_rule"] == "spsa":
        data["base_signature"] = data["new_signature"]

    for k, v in data.items():
        if len(v) == 0:
            raise Exception("Missing required option: {}".format(k))

    # Handle boolean options
    data["auto_purge"] = request.POST.get("auto-purge") is not None
    # checkbox is to _disable_ adjudication
    data["adjudication"] = request.POST.get("adjudication") is None

    # In case of reschedule use old data,
    # otherwise resolve sha and update user's tests_repo
    if "resolved_base" in request.POST:
        data["resolved_base"] = request.POST["resolved_base"]
        data["resolved_new"] = request.POST["resolved_new"]
        data["msg_base"] = request.POST["msg_base"]
        data["msg_new"] = request.POST["msg_new"]
    else:
        data["resolved_base"], data["msg_base"] = get_sha(
            data["base_tag"], data["tests_repo"]
        )
        data["resolved_new"], data["msg_new"] = get_sha(
            data["new_tag"], data["tests_repo"]
        )
        u = request.userdb.get_user(data["username"])
        if u.get("tests_repo", "") != data["tests_repo"]:
            u["tests_repo"] = data["tests_repo"]
            request.userdb.users.save(u)

    if len(data["resolved_base"]) == 0 or len(data["resolved_new"]) == 0:
        raise Exception("Unable to find branch!")

    # Check entered bench
    if data["base_tag"] == "master":
        found = False
        api_url = data["tests_repo"].replace(
            "https://github.com", "https://api.github.com/repos"
        )
        api_url += "/commits"
        bs = re.compile(r"(^|\s)[Bb]ench[ :]+([0-9]+)", re.MULTILINE)
        for c in requests.get(api_url).json():
            m = bs.search(c["commit"]["message"])
            if m:
                found = True
                break
        if not found or m.group(2) != data["base_signature"]:
            raise Exception(
                "Bench signature of Base master does not match, "
                + 'please "git pull upstream master" !'
            )

    stop_rule = request.POST["stop_rule"]

    # Check if the base branch of the test repo matches official master
    api_url = "https://api.github.com/repos/official-stockfish/Stockfish"
    api_url += "/compare/master..." + data["resolved_base"][:10]
    master_diff = requests.get(
        api_url, headers={"Accept": "application/vnd.github.v3.diff"}
    )
    data["base_same_as_master"] = master_diff.text == ""

    # Test existence of net
    new_net = get_net(data["resolved_new"], data["tests_repo"])
    if new_net:
        if not request.rundb.get_nn(new_net):
            raise Exception(
                "The net {}, used by {}, is not "
                "known to Fishtest. Please upload it to: "
                "{}/upload.".format(new_net, data["new_tag"], request.host_url)
            )

    # Store net info
    data["new_net"] = new_net
    data["base_net"] = get_net(data["resolved_base"], data["tests_repo"])

    # Integer parameters

    if stop_rule == "sprt":
        sprt_batch_size_games = 8
        assert sprt_batch_size_games % 2 == 0
        elo_model = request.POST["elo_model"]
        if elo_model not in ["BayesElo", "logistic", "normalized"]:
            raise Exception("Unknown Elo model")
        data["sprt"] = fishtest.stats.stat_util.SPRT(
            alpha=0.05,
            beta=0.05,
            elo0=float(request.POST["sprt_elo0"]),
            elo1=float(request.POST["sprt_elo1"]),
            elo_model=elo_model,
            batch_size=sprt_batch_size_games // 2,
        )  # game pairs
        # Limit on number of games played.
        data["num_games"] = 800000
    elif stop_rule == "spsa":
        data["num_games"] = int(request.POST["num-games"])
        if data["num_games"] <= 0:
            raise Exception("Number of games must be >= 0")

        data["spsa"] = {
            "A": int(request.POST["spsa_A"]),
            "alpha": float(request.POST["spsa_alpha"]),
            "gamma": float(request.POST["spsa_gamma"]),
            "raw_params": request.POST["spsa_raw_params"],
            "iter": 0,
            "num_iter": int(data["num_games"] / 2),
            "clipping": request.POST["spsa_clipping"],
            "rounding": request.POST["spsa_rounding"],
        }
        data["spsa"]["params"] = parse_spsa_params(
            request.POST["spsa_raw_params"], data["spsa"]
        )
        if len(data["spsa"]["params"]) == 0:
            raise Exception("Number of params must be > 0")
    else:
        data["num_games"] = int(request.POST["num-games"])
        if data["num_games"] <= 0:
            raise Exception("Number of games must be >= 0")

    max_games = 3200000
    if data["num_games"] > max_games:
        raise Exception("Number of games must be <= " + str(max_games))

    data["threads"] = int(request.POST["threads"])
    data["priority"] = int(request.POST["priority"])
    data["throughput"] = int(request.POST["throughput"])

    if data["threads"] <= 0:
        raise Exception("Threads must be >= 1")

    return data


def del_tasks(run):
    run = copy.copy(run)
    run.pop("tasks", None)
    run = copy.deepcopy(run)
    return run


def update_nets(request, run):
    run_id = str(run["_id"])
    data = run["args"]
    if run["base_same_as_master"]:
        base_net = data["base_net"]
        if base_net:
            net = request.rundb.get_nn(base_net)
            if not net:
                # Should never happen:
                raise Exception(
                    "The net {}, used by {}, is not "
                    "known to Fishtest. Please upload it to: "
                    "{}/upload.".format(base_net, data["base_tag"], request.host_url)
                )
            if "is_master" not in net:
                net["is_master"] = True
                request.rundb.update_nn(net)
    new_net = data["new_net"]
    if new_net:
        net = request.rundb.get_nn(new_net)
        if not net:
            return
        if "first_test" not in net:
            net["first_test"] = {"id": run_id, "date": datetime.datetime.utcnow()}
        net["last_test"] = {"id": run_id, "date": datetime.datetime.utcnow()}
        request.rundb.update_nn(net)


@view_config(route_name="tests_run", renderer="tests_run.mak", require_csrf=True)
def tests_run(request):
    if not request.authenticated_userid:
        request.session.flash("Please login")
        next_page = "/tests/run"
        if "id" in request.params:
            next_page += "?id={}".format(request.params["id"])
        return HTTPFound(
            location="{}?next={}".format(request.route_url("login"), next_page)
        )
    if request.method == "POST":
        try:
            data = validate_form(request)
            run_id = request.rundb.new_run(**data)
            run = del_tasks(request.rundb.get_run(run_id))
            request.actiondb.new_run(request.authenticated_userid, run)
            cached_flash(request, "Submitted test to the queue!")
            return HTTPFound(location="/tests/view/" + str(run_id))
        except Exception as e:
            request.session.flash(str(e), "error")

    run_args = {}
    if "id" in request.params:
        run_args = request.rundb.get_run(request.params["id"])["args"]

    username = request.authenticated_userid
    u = request.userdb.get_user(username)

    return {
        "args": run_args,
        "is_rerun": len(run_args) > 0,
        "rescheduled_from": request.params["id"] if "id" in request.params else None,
        "tests_repo": u.get("tests_repo", ""),
        "bench": get_master_bench(),
    }


def can_modify_run(request, run):
    return run["args"][
        "username"
    ] == request.authenticated_userid or request.has_permission("approve_run")


@view_config(route_name="tests_modify", require_csrf=True, request_method="POST")
def tests_modify(request):
    if not request.authenticated_userid:
        request.session.flash("Please login")
        return HTTPFound(location=request.route_url("login"))
    if "num-games" in request.POST:
        run = request.rundb.get_run(request.POST["run"])
        before = del_tasks(run)

        if not can_modify_run(request, run):
            request.session.flash("Unable to modify another user's run!", "error")
            return HTTPFound(location=request.route_url("tests"))

        existing_games = 0
        for chunk in run["tasks"]:
            existing_games += chunk["num_games"]
            if "stats" in chunk:
                stats = chunk["stats"]
                total = stats["wins"] + stats["losses"] + stats["draws"]
                if total < chunk["num_games"]:
                    chunk["pending"] = True

        num_games = int(request.POST["num-games"])
        if (
            num_games > run["args"]["num_games"]
            and "sprt" not in run["args"]
            and "spsa" not in run["args"]
        ):
            request.session.flash(
                "Unable to modify number of games in a fixed game test!", "error"
            )
            return HTTPFound(location=request.route_url("tests"))

        max_games = 3200000
        if num_games > max_games:
            request.session.flash(
                "Number of games must be <= " + str(max_games), "error"
            )
            return HTTPFound(location=request.route_url("tests"))

        run["finished"] = False
        run["failed"] = False
        run["is_green"] = False
        run["is_yellow"] = False
        run["args"]["num_games"] = num_games
        run["args"]["priority"] = int(request.POST["priority"])
        run["args"]["throughput"] = int(request.POST["throughput"])
        run["args"]["auto_purge"] = True if request.POST.get("auto_purge") else False
        request.rundb.calc_itp(run)
        request.rundb.buffer(run, True)
        request.rundb.task_time = 0

        after = del_tasks(run)
        request.actiondb.modify_run(request.authenticated_userid, before, after)

        cached_flash(request, "Run successfully modified!")
    return HTTPFound(location=request.route_url("tests"))


@view_config(route_name="tests_stop", require_csrf=True, request_method="POST")
def tests_stop(request):
    if not request.authenticated_userid:
        request.session.flash("Please login")
        return HTTPFound(location=request.route_url("login"))
    if "run-id" in request.POST:
        run = request.rundb.get_run(request.POST["run-id"])
        if not can_modify_run(request, run):
            request.session.flash("Unable to modify another users run!", "error")
            return HTTPFound(location=request.route_url("tests"))

        run["finished"] = True
        request.rundb.stop_run(request.POST["run-id"])
        run = del_tasks(run)
        request.actiondb.stop_run(request.authenticated_userid, run)
        cached_flash(request, "Stopped run")
    return HTTPFound(location=request.route_url("tests"))


@view_config(route_name="tests_approve", require_csrf=True, request_method="POST")
def tests_approve(request):
    if not request.authenticated_userid:
        request.session.flash("Please login")
        return HTTPFound(location=request.route_url("login"))
    if not request.has_permission("approve_run"):
        request.session.flash("Please login as approver")
        return HTTPFound(location=request.route_url("login"))
    username = request.authenticated_userid
    run_id = request.POST["run-id"]
    if request.rundb.approve_run(run_id, username):
        run = request.rundb.get_run(run_id)
        run = del_tasks(run)
        update_nets(request, run)
        request.actiondb.approve_run(username, run)
        cached_flash(request, "Approved run")
    else:
        request.session.flash("Unable to approve run!", "error")
    return HTTPFound(location=request.route_url("tests"))


@view_config(route_name="tests_purge", require_csrf=True, request_method="POST")
def tests_purge(request):
    if not request.has_permission("approve_run"):
        request.session.flash("Please login as approver")
        return HTTPFound(location=request.route_url("login"))
    username = request.authenticated_userid

    run = request.rundb.get_run(request.POST["run-id"])
    if not run["finished"]:
        request.session.flash("Can only purge completed run", "error")
        return HTTPFound(location=request.route_url("tests"))

    # More relaxed conditions than with auto purge.
    message = request.rundb.purge_run(run, p=0.01, res=4.5)
    if message != "":
        request.session.flash(message)
        return HTTPFound(location=request.route_url("tests"))

    run = del_tasks(run)
    request.actiondb.purge_run(username, run)

    cached_flash(request, "Purged run")
    return HTTPFound(location=request.route_url("tests"))


@view_config(route_name="tests_delete", require_csrf=True, request_method="POST")
def tests_delete(request):
    if not request.authenticated_userid:
        request.session.flash("Please login")
        return HTTPFound(location=request.route_url("login"))
    if "run-id" in request.POST:
        run = request.rundb.get_run(request.POST["run-id"])
        if not can_modify_run(request, run):
            request.session.flash("Unable to modify another users run!", "error")
            return HTTPFound(location=request.route_url("tests"))

        run["deleted"] = True
        run["finished"] = True
        for task in run["tasks"]:
            task["active"] = False
        request.rundb.buffer(run, True)
        request.rundb.task_time = 0

        run = del_tasks(run)
        request.actiondb.delete_run(request.authenticated_userid, run)

        cached_flash(request, "Deleted run")
    return HTTPFound(location=request.route_url("tests"))


@view_config(route_name="tests_stats", renderer="tests_stats.mak")
def tests_stats(request):
    run = request.rundb.get_run(request.matchdict["id"])
    request.rundb.get_results(run)
    return {"run": run}


@view_config(route_name="tests_machines", renderer="machines_table.mak")
def tests_machines(request):
    machines = request.rundb.get_machines()
    for machine in machines:
        diff = diff_date(machine["last_updated"])
        machine["last_updated"] = delta_date(diff)
    return {"machines": machines}


@view_config(route_name="tests_view_spsa_history", renderer="json")
def tests_view_spsa_history(request):
    run = request.rundb.get_run(request.matchdict["id"])
    if "spsa" not in run["args"]:
        return {}

    return run["args"]["spsa"]


@view_config(route_name="tests_view", renderer="tests_view.mak")
def tests_view(request):
    run = request.rundb.get_run(request.matchdict["id"])
    if run is None:
        raise exception_response(404)
    results = request.rundb.get_results(run)
    run["results_info"] = format_results(results, run)
    run_args = [("id", str(run["_id"]), "")]
    if run.get("rescheduled_from"):
        run_args.append(("rescheduled_from", run["rescheduled_from"], ""))

    for name in [
        "new_tag",
        "new_signature",
        "new_options",
        "resolved_new",
        "new_net",
        "base_tag",
        "base_signature",
        "base_options",
        "resolved_base",
        "base_net",
        "sprt",
        "num_games",
        "spsa",
        "tc",
        "new_tc",
        "threads",
        "book",
        "book_depth",
        "auto_purge",
        "priority",
        "itp",
        "username",
        "tests_repo",
        "adjudication",
        "info",
    ]:

        if name not in run["args"]:
            continue

        value = run["args"][name]
        url = ""

        if name == "new_tag" and "msg_new" in run["args"]:
            value += "  (" + run["args"]["msg_new"][:50] + ")"

        if name == "base_tag" and "msg_base" in run["args"]:
            value += "  (" + run["args"]["msg_base"][:50] + ")"

        if name == "sprt" and value != "-":
            value = "elo0: {:.2f} alpha: {:.2f} elo1: {:.2f} beta: {:.2f} state: {} ({})".format(
                value["elo0"],
                value["alpha"],
                value["elo1"],
                value["beta"],
                value.get("state", "-"),
                value.get("elo_model", "BayesElo"),
            )

        if name == "spsa" and value != "-":
            iter_local = value["iter"] + 1  # assume at least one completed,
            # and avoid division by zero
            A = value["A"]
            alpha = value["alpha"]
            gamma = value["gamma"]
            summary = "Iter: {:d}, A: {:d}, alpha {:0.3f}, gamma {:0.3f}, clipping {}, rounding {}".format(
                iter_local,
                A,
                alpha,
                gamma,
                value["clipping"] if "clipping" in value else "old",
                value["rounding"] if "rounding" in value else "deterministic",
            )
            params = value["params"]
            value = [summary]
            for p in params:
                value.append(
                    [
                        p["name"],
                        "{:.2f}".format(p["theta"]),
                        int(p["start"]),
                        int(p["min"]),
                        int(p["max"]),
                        "{:.3f}".format(p["c"] / (iter_local**gamma)),
                        "{:.3f}".format(p["a"] / (A + iter_local) ** alpha),
                    ]
                )
        if "tests_repo" in run["args"]:
            if name == "new_tag":
                url = (
                    run["args"]["tests_repo"] + "/commit/" + run["args"]["resolved_new"]
                )
            elif name == "base_tag":
                url = (
                    run["args"]["tests_repo"]
                    + "/commit/"
                    + run["args"]["resolved_base"]
                )
            elif name == "tests_repo":
                url = value

        if name == "spsa":
            run_args.append(("spsa", value, ""))
        else:
            try:
                strval = str(value)
            except:
                strval = value.encode("ascii", "replace")
            if name not in ["new_tag", "base_tag"]:
                strval = html.escape(strval)
            run_args.append((name, strval, url))

    active = 0
    cores = 0
    for task in run["tasks"]:
        if task["active"]:
            active += 1
            cores += task["worker_info"]["concurrency"]
        last_updated = task.get("last_updated", datetime.datetime.min)
        task["last_updated"] = last_updated

    if run["args"].get("sprt"):
        page_title = "SPRT {} vs {}".format(
            run["args"]["new_tag"], run["args"]["base_tag"]
        )
    elif run["args"].get("spsa"):
        page_title = "SPSA {}".format(run["args"]["new_tag"])
    else:
        page_title = "{} games - {} vs {}".format(
            run["args"]["num_games"], run["args"]["new_tag"], run["args"]["base_tag"]
        )
    chi2 = get_chi2(run["tasks"])
    update_residuals(run["tasks"], cached_chi2=chi2)
    return {
        "run": run,
        "run_args": run_args,
        "page_title": page_title,
        "approver": request.has_permission("approve_run"),
        "chi2": chi2,
        "totals": "({} active worker{} with {} core{})".format(
            active, ("s" if active != 1 else ""), cores, ("s" if cores != 1 else "")
        ),
    }


def get_paginated_finished_runs(request):
    username = request.matchdict.get("username", "")
    success_only = request.params.get("success_only", False)
    yellow_only = request.params.get("yellow_only", False)
    ltc_only = request.params.get("ltc_only", False)

    page_idx = max(0, int(request.params.get("page", 1)) - 1)
    page_size = 25
    finished_runs, num_finished_runs = request.rundb.get_finished_runs(
        username=username,
        success_only=success_only,
        yellow_only=yellow_only,
        ltc_only=ltc_only,
        skip=page_idx * page_size,
        limit=page_size,
    )

    pages = [
        {
            "idx": "Prev",
            "url": "?page={}".format(page_idx),
            "state": "disabled" if page_idx == 0 else "",
        }
    ]
    for idx, _ in enumerate(range(0, num_finished_runs, page_size)):
        if (
            idx < 5
            or abs(page_idx - idx) < 5
            or idx > (num_finished_runs / page_size) - 5
        ):
            pages.append(
                {
                    "idx": idx + 1,
                    "url": "?page={}".format(idx + 1),
                    "state": "active" if page_idx == idx else "",
                }
            )
        elif pages[-1]["idx"] != "...":
            pages.append({"idx": "...", "url": "", "state": "disabled"})
    pages.append(
        {
            "idx": "Next",
            "url": "?page={}".format(page_idx + 2),
            "state": "disabled"
            if page_idx >= (num_finished_runs - 1) // page_size
            else "",
        }
    )

    for page in pages:
        if success_only:
            page["url"] += "&success_only=1"
        if yellow_only:
            page["url"] += "&yellow_only=1"
        if ltc_only:
            page["url"] += "&ltc_only=1"

    failed_runs = []
    for run in finished_runs:
        # Ensure finished runs have results_info
        results = request.rundb.get_results(run)
        if "results_info" not in run:
            run["results_info"] = format_results(results, run)

        # Look for failed runs
        if "failed" in run and run["failed"]:
            failed_runs.append(run)

    return {
        "finished_runs": finished_runs,
        "finished_runs_pages": pages,
        "num_finished_runs": num_finished_runs,
        "failed_runs": failed_runs,
        "page_idx": page_idx,
    }


@view_config(route_name="tests_finished", renderer="tests_finished.mak")
def tests_finished(request):
    return get_paginated_finished_runs(request)


@view_config(route_name="tests_user", renderer="tests_user.mak")
def tests_user(request):
    username = request.matchdict.get("username", "")
    response = {**get_paginated_finished_runs(request), "username": username}
    if int(request.params.get("page", 1)) == 1:
        response["runs"] = request.rundb.aggregate_unfinished_runs(username)[0]
    # page 2 and beyond only show finished test results
    return response


def homepage_results(request):
    # Calculate games_per_minute from current machines
    games_per_minute = 0.0
    machines = request.rundb.get_machines()
    for machine in machines:
        diff = diff_date(machine["last_updated"])
        machine["last_updated"] = delta_date(diff)
        if machine["nps"] != 0:
            games_per_minute += (
                (machine["nps"] / 1280000.0)
                * (60.0 / estimate_game_duration(machine["run"]["args"]["tc"]))
                * (
                    int(machine["concurrency"])
                    // machine["run"]["args"].get("threads", 1)
                )
            )
    machines.reverse()
    # Get updated results for unfinished runs + finished runs
    (runs, pending_hours, cores, nps) = request.rundb.aggregate_unfinished_runs()
    return {
        **get_paginated_finished_runs(request),
        "runs": runs,
        "machines": machines,
        "pending_hours": "{:.1f}".format(pending_hours),
        "cores": cores,
        "nps": nps,
        "games_per_minute": int(games_per_minute),
    }


# For caching the homepage tests output
cache_time = 2
last_tests = None
last_time = 0

# Guard against parallel builds of main page
building = threading.Semaphore()


@view_config(route_name="tests", renderer="tests.mak")
def tests(request):
    if int(request.params.get("page", 1)) > 1:
        # page 2 and beyond only show finished test results
        return get_paginated_finished_runs(request)

    global last_tests, last_time
    if time.time() - last_time > cache_time:
        acquired = building.acquire(last_tests is None)
        if not acquired:
            # We have a current cache and another thread is rebuilding,
            # so return the current cache
            pass
        elif time.time() - last_time < cache_time:
            # Another thread has built the cache for us, so we are done
            building.release()
        else:
            # Not cached, so calculate and fetch homepage results
            try:
                last_tests = homepage_results(request)
            except Exception as e:
                print("Overview exception: " + str(e))
                if not last_tests:
                    raise e
            finally:
                last_time = time.time()
                building.release()
    return {
        **last_tests,
        "machines_shown": request.cookies.get("machines_state") == "Hide",
    }
