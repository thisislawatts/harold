#!/usr/bin/python

import getpass
import itertools
import os
import sys
import urllib.request, urllib.parse, urllib.error
import urllib.parse

import requests

from requests.auth import HTTPBasicAuth

from harold.conf import HaroldConfiguration
from harold.plugins.database import DatabaseConfig
from harold.plugins.github import GitHubConfig, SalonDatabase


class SynchronousDatabase(object):
    """A class that mimics twisted's ADBAPI but is synchronous."""

    def __init__(self, config):
        self.module, kwargs = config.get_module_and_params()
        self.connection = self.module.connect(**kwargs)

    def runQuery(self, *args, **kwargs):
        cursor = self.connection.cursor()
        cursor.execute(*args, **kwargs)
        return cursor.fetchall()

    def runOperation(self, *args, **kwargs):
        cursor = self.connection.cursor()
        cursor.execute(*args, **kwargs)
        self.connection.commit()

    def __del__(self):
        self.connection.close()


def make_pullrequest_url(repo, state):
    return urllib.parse.urlunsplit((
        "https",
        "api.github.com",
        "/".join(["/repos", repo, "pulls"]),
        urllib.parse.urlencode({
            "state": state,
        }),
        None
    ))


def make_comments_url(repo):
    return urllib.parse.urlunsplit((
        "https",
        "api.github.com",
        "/".join(["/repos", repo, "issues", "comments"]),
        urllib.parse.urlencode({
            "sort": "created",
            "direction": "desc",
        }),
        None
    ))


def fetch_paginated(session, url):
    scheme, netloc, path, query, fragment = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qs(query)
    params["per_page"] = 100

    for page in itertools.count(start=1):
        params["page"] = page
        new_querystring = urllib.parse.urlencode(params)
        paginated_url = urllib.parse.urlunsplit((scheme, netloc, path,
                                             new_querystring, fragment))

        response = session.get(paginated_url)
        response.raise_for_status()

        payload = response.json()
        if not payload:
            break

        for item in payload:
            yield item


def main():
    # config file is an expected argument
    bin_name = os.path.basename(sys.argv[0])
    if len(sys.argv) != 2:
        print("USAGE: %s INI_FILE" % bin_name, file=sys.stderr)
        sys.exit(1)

    config_file = sys.argv[1]
    try:
        config = HaroldConfiguration(config_file)
    except Exception as e:
        print("%s: failed to read config file %r: %s" % (
            bin_name,
            config_file,
            e,
        ), file=sys.stderr)
        sys.exit(1)

    # quickly load up the flask app to create the tables if not already done
    print("Ensuring schema present...")
    os.environ["HAROLD_CONFIG"] = config_file
    import salon.app
    import salon.models
    print("done")
    print()

    # connect to db
    gh_config = GitHubConfig(config)
    db_config = DatabaseConfig(config)
    database = SalonDatabase(SynchronousDatabase(db_config))

    # figure out which repos we care about
    repositories = list(gh_config.repositories_by_name.keys())

    if not repositories:
        print("No repositories to sync!")
        sys.exit(0)

    print("I will synchronize salon status for:")
    for repo in repositories:
        print("  - " + repo)
    print()

    # get auth credentials
    print()
    print("Please enter a GitHub personal access token (found at Settings >>")
    print("Applications on GitHub) with the repo scope authorized")
    token = getpass.getpass("Token: ").strip()

    # set up an http session
    session = requests.session()
    session.auth = HTTPBasicAuth(token, "x-oauth-basic")
    session.verify = True
    session.headers["User-Agent"] = "Harold-by-@spladug"

    # query and sync the database
    for repo in repositories:
        print(repo)

        # synchronize pull requests
        pull_requests = itertools.chain(
            fetch_paginated(session, make_pullrequest_url(repo, "open")),
            fetch_paginated(session, make_pullrequest_url(repo, "closed")),
        )
        for pull_request in pull_requests:
            print("  %s#%s" % (repo, pull_request["number"]))
            database.process_pullrequest(pull_request, repository=repo)

        # synchronize comments
        for comment in fetch_paginated(session, make_comments_url(repo)):
            print("  %s comment #%d" % (repo, comment["id"]))
            database.process_comment(comment)
