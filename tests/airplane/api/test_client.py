import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Tuple, Union
from unittest import mock

import pytest
import requests
import responses
from requests import PreparedRequest, Response
from requests.structures import CaseInsensitiveDict
from responses.matchers import json_params_matcher

from airplane.api.client import (
    APIClient,
    ClientOpts,
    _compute_retry_delay,
    _parse_retry_after,
    api_client_from_env,
    client_opts_from_env,
)
from airplane.exceptions import HTTPError, InvalidEnvironmentException

client = APIClient(
    ClientOpts(
        api_host="http://example.com",
        api_token="token",
        env_id="env123",
        team_id="tea123",
        run_id="",
        tunnel_token="",
        sandbox_token="",
        timeout_seconds=0.1,
    ),
    "test_version",
)

uuid_regex = re.compile(
    "^[0-9(a-f|A-F)]{8}-[0-9(a-f|A-F)]{4}-4[0-9(a-f|A-F)]{3}-[89ab][0-9(a-f|A-F)]{3}-[0-9(a-f|A-F)]{12}$"
)


def header_matcher(
    headers: Dict[str, Union[str, None, re.Pattern]]
) -> Callable[[PreparedRequest], Tuple[bool, str]]:
    """
    Matcher that matches header values.

    Unlike the built-in responses.matchers.header_matcher, this matcher will assert that headers
    whose value is `None` are not set on the request. It also supports matching against a regex.
    """

    def match(request: PreparedRequest) -> Tuple[bool, str]:
        request_headers: Union[Dict[Any, Any], Any] = request.headers or {}
        for header, expected in headers.items():
            actual = request_headers.get(header, None)
            if expected is None:
                if actual is not None:
                    return (
                        False,
                        f'Expected header "{header}" to not be set: got "{actual}".',
                    )
            elif actual is None:
                if expected is not None:
                    return False, f'Expected header "{header}" to be set.'
            elif isinstance(expected, str):
                if expected != actual:
                    return (
                        False,
                        f'Expected header "{header}" to be "{expected}": got "{actual}".',
                    )
            elif isinstance(expected, re.Pattern):
                if expected.match(actual) is None:
                    return (
                        False,
                        f'Expected header "{header}" to be "{expected.pattern}": got "{actual}".',
                    )
            else:
                return False, f'Header "{header}" cannot be matched.'

        return True, ""

    return match


def test_header_matcher() -> None:
    matcher = header_matcher(
        {
            "String": "hello",
            "None": None,
            "Regex": re.compile("^abc?$"),
        }
    )

    cases = [
        # Valid
        ({"String": "hello", "Regex": "abc"}, True, ""),
        ({"String": "hello", "Regex": "ab"}, True, ""),
        # Invalid: String
        (
            {"String": "hello world", "Regex": "abc"},
            False,
            'Expected header "String" to be "hello": got "hello world".',
        ),
        (
            {"String": "", "Regex": "abc"},
            False,
            'Expected header "String" to be "hello": got "".',
        ),
        ({"Regex": "abc"}, False, 'Expected header "String" to be set.'),
        # Invalid: None
        (
            {"String": "hello", "None": "something", "Regex": "abc"},
            False,
            'Expected header "None" to not be set: got "something".',
        ),
        (
            {"String": "hello", "None": "", "Regex": "abc"},
            False,
            'Expected header "None" to not be set: got "".',
        ),
        # Invalid: Regex
        (
            {"String": "hello", "Regex": "abcd"},
            False,
            'Expected header "Regex" to be "^abc?$": got "abcd".',
        ),
        (
            {"String": "hello", "Regex": ""},
            False,
            'Expected header "Regex" to be "^abc?$": got "".',
        ),
        ({"String": "hello"}, False, 'Expected header "Regex" to be set.'),
    ]
    for headers, valid, reason in cases:
        req = PreparedRequest()
        req.headers = CaseInsensitiveDict(headers)
        assert (valid, reason) == matcher(req)


@responses.activate
def test_client_get() -> None:
    responses.get(
        "http://example.com/v0/runs/get?id=run123",
        json={"status": "Succeeded"},
        status=200,
        match=[
            header_matcher(
                {
                    "Accept": "application/json",
                    "User-Agent": "airplane/sdk/python/test_version team/tea123",
                    "X-Airplane-Client-Kind": "sdk/python",
                    "X-Airplane-Client-Version": "test_version",
                    "X-Airplane-Token": "token",
                    "X-Airplane-Env-ID": "env123",
                    "X-Team-ID": "tea123",
                    # Assert Content-Type is not set.
                    "Content-Type": None,
                    "Idempotency-Key": uuid_regex,
                }
            )
        ],
    )

    resp = client.get_run(run_id="run123")
    assert resp == {"status": "Succeeded"}


@responses.activate
def test_client_post() -> None:
    responses.post(
        "http://example.com/v0/tasks/execute",
        json={"runID": "run123"},
        status=200,
        match=[
            json_params_matcher(
                {
                    "slug": "my_task",
                    "paramValues": {"value": 10},
                    "resources": {},
                }
            ),
            header_matcher(
                {
                    "Accept": "application/json",
                    "User-Agent": "airplane/sdk/python/test_version team/tea123",
                    "X-Airplane-Client-Kind": "sdk/python",
                    "X-Airplane-Client-Version": "test_version",
                    "X-Airplane-Token": "token",
                    "X-Airplane-Env-ID": "env123",
                    "X-Team-ID": "tea123",
                    "Content-Type": "application/json",
                    "Idempotency-Key": uuid_regex,
                },
            ),
        ],
    )

    resp = client.execute_task(slug="my_task", param_values={"value": 10})
    assert resp == "run123"


@responses.activate
def test_client_optional_headers_unset() -> None:
    responses.post(
        "http://example.com/v0/tasks/execute",
        json={"runID": "run123"},
        status=200,
        match=[
            header_matcher(
                {
                    "User-Agent": "airplane/sdk/python/test_version team/tea123",
                    "X-Airplane-Dev-Token": None,
                    "X-Airplane-Sandbox-Token": None,
                },
            ),
        ],
    )

    resp = client.execute_task(slug="my_task")
    assert resp == "run123"


@responses.activate
def test_client_optional_headers_set() -> None:
    client2 = APIClient(
        ClientOpts(
            api_host="http://example.com",
            api_token="token",
            env_id="env123",
            team_id="tea123",
            run_id="run123",
            tunnel_token="tunnel123",
            sandbox_token="sandbox123",
        ),
        "test_version",
    )
    responses.post(
        "http://example.com/v0/tasks/execute",
        json={"runID": "run123"},
        status=200,
        match=[
            header_matcher(
                {
                    "User-Agent": "airplane/sdk/python/test_version team/tea123 run/run123",
                    "X-Airplane-Dev-Token": "tunnel123",
                    "X-Airplane-Sandbox-Token": "sandbox123",
                },
            ),
        ],
    )

    resp = client2.execute_task(slug="my_task")
    assert resp == "run123"


@responses.activate
def test_client_error() -> None:
    responses.post(
        "http://example.com/v0/tasks/execute",
        json={"error": "A slug must be provided."},
        status=400,
        match=[
            json_params_matcher(
                {
                    "slug": "",
                    "paramValues": {"value": 10},
                    "resources": {},
                }
            ),
        ],
    )

    with pytest.raises(HTTPError, match="A slug must be provided."):
        client.execute_task(slug="", param_values={"value": 10})


@responses.activate
@mock.patch("airplane.api.client._compute_retry_delay")
def test_client_retries(mocked_retry_delay: mock.MagicMock) -> None:
    mocked_retry_delay.return_value = 0.01  # 10ms
    responses.post(
        "http://example.com/v0/tasks/execute",
        json={"error": "An internal error occurred."},
        status=500,
        match=[
            json_params_matcher(
                {
                    "slug": "my_task",
                    "paramValues": {"value": 10},
                    "resources": {},
                }
            ),
        ],
    )

    with pytest.raises(HTTPError, match="An internal error occurred."):
        client.execute_task(slug="my_task", param_values={"value": 10})

    # Since this endpoint returned a 500, it should have been tried 10 times.
    assert len(responses.calls) == 10
    keys = []
    for call in responses.calls:
        keys.append(call[0].headers["Idempotency-Key"])
    assert len(set(keys)) == 1
    assert uuid_regex.match(keys[0])


@responses.activate
@mock.patch("airplane.api.client._compute_retry_delay")
def test_client_airplane_retryable(mocked_retry_delay: mock.MagicMock) -> None:
    mocked_retry_delay.return_value = 0.01  # 10ms
    responses.post(
        "http://example.com/v0/tasks/execute",
        json={"error": "Conflict."},
        status=409,
    )

    with pytest.raises(HTTPError, match="Conflict."):
        client.execute_task(slug="my_task")

    # This status code is not retryable. It should not be retried.
    assert len(responses.calls) == 1
    responses.reset()

    responses.post(
        "http://example.com/v0/tasks/execute",
        json={"error": "Conflict."},
        status=409,
        headers={"X-Airplane-Retryable": "true"},
    )

    with pytest.raises(HTTPError, match="Conflict."):
        client.execute_task(slug="my_task")

    # Since the API indicated it was retryable, the client should retry it.
    assert len(responses.calls) == 10
    responses.reset()

    responses.post(
        "http://example.com/v0/tasks/execute",
        json={"error": "An internal error occurred."},
        status=500,
        headers={"X-Airplane-Retryable": "false"},
    )

    with pytest.raises(HTTPError, match="An internal error occurred."):
        client.execute_task(slug="my_task")

    # Since the API indicated it was NOT retryable, the client should NOT retry it.
    assert len(responses.calls) == 1
    responses.reset()


@responses.activate
def test_client_retry_after() -> None:
    # This test asserts that we respect the Retry-After header upon error.
    # It does this by adding a 1s Retry-After delay upon the first API request.
    # If the test completes in less than 1s, then it did not wait.

    count = 0

    def request_callback(_request: PreparedRequest) -> Any:
        nonlocal count
        count += 1

        if count > 1:
            return (
                200,
                {"Content-Type": "application/json"},
                json.dumps({"runID": "run123"}),
            )

        return (
            429,
            {"Retry-After": "1", "Content-Type": "application/json"},
            json.dumps({"error": "An internal error occurred."}),
        )

    responses.add_callback(
        responses.POST,
        "http://example.com/v0/tasks/execute",
        callback=request_callback,
    )

    start = time.time()
    assert client.execute_task(slug="my_task") == "run123"
    assert 1 < time.time() - start < 2  # seconds


@responses.activate
@mock.patch("airplane.api.client._compute_retry_delay")
def test_client_timeout(mocked_retry_delay: mock.MagicMock) -> None:
    mocked_retry_delay.return_value = 0.01  # 10ms
    responses.post(
        "http://example.com/v0/tasks/execute",
        status=500,
        body=requests.exceptions.Timeout(),
    )

    with pytest.raises(requests.exceptions.Timeout):
        client.execute_task(slug="my_task", param_values={"value": 10})

    # Timeouts should be retried.
    assert len(responses.calls) == 10


def test_compute_retry_delay() -> None:
    assert _compute_retry_delay(0) == 0
    assert _compute_retry_delay(1) == 0
    assert _compute_retry_delay(2) > 0


def test_parse_retry_after() -> None:
    assert _parse_retry_after(Response()) == 0

    resp = Response()
    resp.headers["Retry-After"] = "10"
    assert _parse_retry_after(resp) == 10

    resp = Response()
    resp.headers["Retry-After"] = "broken"
    assert _parse_retry_after(resp) == 0


@mock.patch.dict(
    os.environ,
    {
        "AIRPLANE_API_HOST": "https://api.airplane.dev",
        "AIRPLANE_TOKEN": "foo_token",
        "AIRPLANE_ENV_ID": "foo_env",
        "AIRPLANE_TEAM_ID": "foo_team",
        "AIRPLANE_RUN_ID": "foo_run",
        "AIRPLANE_TUNNEL_TOKEN": "foo_tunnel",
        "AIRPLANE_SANDBOX_TOKEN": "foo_sandbox",
    },
)
def test_api_client_from_env() -> None:
    opts = client_opts_from_env()

    assert opts.api_host == "https://api.airplane.dev"
    assert opts.api_token == "foo_token"
    assert opts.env_id == "foo_env"
    assert opts.team_id == "foo_team"
    assert opts.run_id == "foo_run"
    assert opts.tunnel_token == "foo_tunnel"
    assert opts.sandbox_token == "foo_sandbox"

    assert api_client_from_env() is not None


def test_client_opts_invalid_env_throws() -> None:
    with pytest.raises(InvalidEnvironmentException):
        client_opts_from_env()
