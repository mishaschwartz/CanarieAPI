#!/usr/bin/env python
# coding:utf-8

# N.B. : Some of these docstrings are written in reSTructuredText format so that
# Sphinx can use them directly with fancy formatting.

# In the context of a REST application, this module must be loaded first as it
# is the one that instantiates the Flask Application on which other modules
# will depend.

"""
API definition.

This module defines the generic REST API for platforms and services as defined by
the CANARIE API specification. See :
https://collaboration.canarie.ca/elgg/discussion/view/3664/research-software-api-documentation
"""


# -- Standard lib ------------------------------------------------------------
import collections
import datetime
import os
from typing import Dict

# -- 3rd party ---------------------------------------------------------------
from dateutil.parser import parse as dt_parse
from flask import jsonify, redirect, render_template
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import HTTPException

# -- Project specific --------------------------------------------------------
from canarieapi import __meta__
from canarieapi.app_object import APP
from canarieapi.schema import CONFIGURATION_SCHEMA, validate_config_schema
from canarieapi.status import Status
from canarieapi.utility_rest import (
    APIType,
    AnyIntConverter,
    get_api_title,
    get_canarie_api_response,
    get_config,
    get_db,
    make_error_response,
    request_wants_json,
    set_html_as_default_response,
    validate_route
)

# Make sure to test the config on launch to raise exception as soon as possible
validate_config_schema(False)

# Creates the database if it doesn't exist, connects to it and keeps it in
# cache for hassle-free runtime access
with APP.app_context():
    get_db()


START_UTC_TIME = datetime.datetime.utcnow().replace(microsecond=0)

# REST requests required by CANARIE
CANARIE_API_TYPE = ["service", "platform"]
CANARIE_API_VALID_REQUESTS = set()
for _api_type in CANARIE_API_TYPE:
    _required = CONFIGURATION_SCHEMA["definitions"][f"{_api_type}_redirect_schema"]["required"]
    CANARIE_API_VALID_REQUESTS |= set(_required)

# HTML errors for which the application provides a custom error page
HANDLED_HTML_ERRORS = [400, 404, 405, 500, 503]
HANDLED_HTML_ERRORS_STR = ", ".join(map(str, HANDLED_HTML_ERRORS))

# Map an error handler for each handled HTML error
# Errors handled here are the ones that occur internally in the application
#
# The loop replace the following code for each handled html error
# @APP.errorhandler(400)
# def page_not_found_400(some_error):
#     return handle_error(400, str(some_error))
#
# For the lambda syntax see the following page explaining the requirement for
# status_code_copy=status_code
# http://stackoverflow.com/questions/938429/scope-of-python-lambda-functions-
# and-their-parameters/938493#938493
APP.error_handler_spec.setdefault(None, {})
for status_code in HANDLED_HTML_ERRORS:
    APP.error_handler_spec[None].setdefault(status_code, {})
    APP.error_handler_spec[None][status_code][Exception] = \
        lambda more_info, status_code_copy = status_code: \
        make_error_response(http_status=status_code_copy,
                            http_status_response=str(more_info))


# avoid error on missing None key for Exception in Flask>2
APP.error_handler_spec[None].setdefault(None, {})


@APP.errorhandler(Exception)
def handle_exceptions(exception_instance: Exception) -> ResponseReturnValue:
    """
    Generate error response for raised exceptions.

    :param exception_instance: Exception instance.
    """
    APP.logger.debug("Generating error response for the exception %s", repr(exception_instance))
    APP.logger.exception(exception_instance)
    cls = type(exception_instance)
    mod = cls.__module__
    err_name = f"{mod}.{cls}"
    message = f"An exception of type {err_name} occurred. Arguments:\n{exception_instance.args!r}"
    code = exception_instance.code if isinstance(exception_instance, HTTPException) else 500
    APP.logger.error("Making HTTP [%s] exception from [%s]", code, err_name)
    return make_error_response(http_status=code, http_status_response=message)


# -- Flask routes ------------------------------------------------------------
APP.url_map.converters["any_int"] = AnyIntConverter


@APP.route("/")
def home() -> ResponseReturnValue:
    def parse_config(name, api_type, conf):
        # type: (str, str, dict) -> dict
        hostname = APP.config["MY_SERVER_NAME"]
        requests = sorted(["info", "stats", "status"] + list(conf.get("redirect", {}).keys()))
        _content = [
            (
                request,
                f"{hostname}/{name}/{api_type}/{request}"
            )
            for request in requests
        ]
        return collections.OrderedDict(_content)

    config = APP.config
    main_title = APP.config.get("SERVER_MAIN_TITLE", __meta__.__title__)
    content = dict(Platforms={p["info"]["name"]: parse_config(name, "platform", p)
                              for name, p in config["PLATFORMS"].items()},
                   Services={s["info"]["name"]: parse_config(name, "service", s)
                             for name, s in config["SERVICES"].items()})
    return render_template("home.html", Main_Title=main_title, Title="Home", Content=content)


@APP.route("/test")
def manual_test() -> ResponseReturnValue:
    validate_config_schema(True)
    return redirect(APP.config["MY_SERVER_NAME"])


@APP.route("/<any_int(" + HANDLED_HTML_ERRORS_STR + "):status_code_str>")
def extern_html_error_handler(status_code_str: str) -> ResponseReturnValue:
    """
    Handle HTML errors from an external response.

    Handle errors that occur externally provided that Apache is configured so
    that it uses this route for handling errors.

    For this add this line for each handled html errors in the Apache
    configuration::

       ErrorDocument 400 <Rest root>/400
    """
    return make_error_response(http_status=int(status_code_str))


@APP.route("/<route_name>/<any(" + ",".join(CANARIE_API_TYPE) + "):api_type>/info")
def information(route_name: str, api_type: APIType) -> ResponseReturnValue:
    """
    Info route required by CANARIE.
    """

    # JSON is used by default but the Canarie API requires html as default
    set_html_as_default_response()

    validate_route(route_name, api_type)

    info_schema = f"{api_type}_info_schema"
    info_categories = CONFIGURATION_SCHEMA["definitions"][info_schema]["required"]

    config = get_config(route_name, api_type).get("info", {})
    info = []
    for category in info_categories:
        cat = config.get(category, "")
        info.append((category, cat))

    info = collections.OrderedDict(info)

    if request_wants_json():
        return jsonify(info)
    info["tags"] = ", ".join(info["tags"])
    return render_template("default.html", Main_Title=get_api_title(route_name, api_type), Title="Info", Tags=info)


def get_status(route_name: str) -> Dict[str, Dict[str, Status]]:
    db = get_db()
    cur = db.cursor()

    # Gather service(s) status
    all_status = {}
    query = "select service, status, message from status where route = ?"
    try:
        cur.execute(query, [route_name])
        rv = cur.fetchall()

        for record in rv:
            all_status[record[0]] = {"status": record[1], "message": record[2]}
    except Exception as e:
        APP.logger.error(str(e))
        pass
    cur.close()

    return all_status


@APP.route("/<route_name>/<any(" + ",".join(CANARIE_API_TYPE) + "):api_type>/stats")
def stats(route_name: str, api_type: APIType) -> ResponseReturnValue:
    """
    Stats route required by CANARIE.
    """

    # JSON is used by default but the Canarie API requires html as default
    set_html_as_default_response()

    validate_route(route_name, api_type)

    db = get_db()
    cur = db.cursor()

    # Gather service(s) status
    all_status = get_status(route_name)

    # Status can be 'ok', 'bad' or 'down'
    if not all(all_status[service]["status"] == Status.ok for service in all_status):
        message = ", ".join([f"{service} : {Status.pretty_msg(all_status[service]['status'])}"
                             for service in all_status])
        return make_error_response(http_status=503,
                                   http_status_response=message)

    # Gather route stats
    invocations = 0
    last_access = "Never"
    query = "select invocations, last_access from stats where route = ?"
    try:
        cur.execute(query, [route_name])
        rv = cur.fetchall()
        if rv:
            invocations = rv[0][0]
            last_access = dt_parse(rv[0][1]).replace(tzinfo=None).isoformat() + "Z"
    except Exception as e:
        APP.logger.error(str(e))
        pass

    # Check last time cron job have run (help to diagnose cron problem)
    last_log_update = "Never"
    last_status_update = "Never"
    query = "select job, last_execution from cron"
    try:
        cur.execute(query)
        rv = cur.fetchall()
        if rv:
            for record in rv:
                if record[0] == "log":
                    last_log_update = dt_parse(record[1]).isoformat() + "Z"
                elif record[0] == "status":
                    last_status_update = dt_parse(record[1]).isoformat() + "Z"

    except Exception as e:
        APP.logger.error(str(e))
        pass

    cur.close()

    monitor_info = [
        ("lastAccess", last_access),
        ("lastInvocationsUpdate", last_log_update),
        ("lastStatusUpdate", last_status_update)
    ]
    for service in all_status:
        monitor_info.append((service, Status.pretty_msg(all_status[service]["status"])))

    monitor_info = collections.OrderedDict(monitor_info)

    service_stats = [
        ("lastReset", START_UTC_TIME.isoformat() + "Z"),
        ("invocations", invocations),
        ("monitoring", monitor_info)
    ]
    service_stats = collections.OrderedDict(service_stats)

    if request_wants_json():
        return jsonify(service_stats)

    return render_template(
        "default.html",
        Main_Title=get_api_title(route_name, api_type),
        Title="Stats",
        Tags=service_stats,
    )


@APP.route("/<route_name>/<any(" + ",".join(CANARIE_API_TYPE) + "):api_type>/status")
def status(route_name: str, api_type: APIType) -> ResponseReturnValue:
    """
    Extra route to know service status.
    """

    # JSON is used by default but the Canarie API requires html as default
    set_html_as_default_response()

    validate_route(route_name, api_type)

    db = get_db()
    cur = db.cursor()

    # Gather service(s) status
    all_status = get_status(route_name)

    # Check last time cron job have run (help to diagnose cron problem)
    last_status_update = "Never"
    query = "select last_execution from cron where job == 'status'"
    try:
        cur.execute(query)
        rv = cur.fetchall()
        if rv:
            last_status_update = dt_parse(rv[0][0]).isoformat() + "Z"
    except Exception as e:
        APP.logger.error(str(e))
        pass

    cur.close()

    monitor_info = [
        ("lastStatusUpdate", last_status_update)
    ]
    for service in all_status:
        status_msg = Status.pretty_msg(all_status[service]["status"])
        if all_status[service]["status"] != Status.ok:
            status_msg += f" : {all_status[service]['message']}"
        monitor_info.append((service, status_msg))

    monitor_info = collections.OrderedDict(monitor_info)

    if request_wants_json():
        return jsonify(monitor_info)

    return render_template("default.html", Main_Title=get_api_title(route_name, api_type), Title="Status",
                           Tags=monitor_info)


@APP.route("/<route_name>/<any(" + ",".join(CANARIE_API_TYPE) + "):api_type>/<any(" +
           ",".join(CANARIE_API_VALID_REQUESTS) + "):api_request>")
def simple_requests_handler(route_name: str, api_type: APIType, api_request: str = "home") -> ResponseReturnValue:
    """
    #Handle simple requests required by CANARIE.
    """

    # JSON is used by default but the Canarie API requires html as default
    set_html_as_default_response()

    return get_canarie_api_response(route_name, api_type, api_request)


@APP.teardown_appcontext
def close_connection(_: Exception) -> None:
    """
    Disconnect database.

    :param _: Exception handled elsewhere, nothing to do with it
    """
    APP.logger.info(u"Disconnecting from database")
    get_db().close()


if __name__ == "__main__":
    APP.debug = False
    APP.run(port=5000)
