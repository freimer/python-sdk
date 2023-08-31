import dataclasses
import os
import uuid
from dataclasses import dataclass
from functools import lru_cache
from random import random
from time import sleep
from typing import Any, Dict, List, Optional

import requests
from requests import Response
from typing_extensions import Literal

from airplane._version import __version__
from airplane.api.entities import PromptReviewers, Sleep, TaskReviewer
from airplane.exceptions import (
    HTTPError,
    InvalidEnvironmentException,
    InvalidZoneException,
)
from airplane.params import ParamDefTypes, ParamTypes, SerializedParam, serialize_param
from airplane.types import File, JSONType


@dataclass(frozen=True)
class ClientOpts:
    """Client options for an APIClient."""

    api_host: str
    api_token: str
    env_id: str
    team_id: str
    run_id: str = ""
    tunnel_token: str = ""
    sandbox_token: str = ""
    # The timeout to apply to each HTTP request.
    timeout_seconds: float = 10


class APIClient:
    """API client to interact with the Airplane API."""

    _opts: ClientOpts
    _version: str

    def __init__(self, opts: ClientOpts, version: str):
        self._opts = opts
        self._version = version

    def create_run(
        self,
        task_id: str,
        parameters: Optional[Dict[str, Any]] = None,
        env: Optional[Dict[str, Any]] = None,
        constraints: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Creates an Airplane run with parameters, env and constraints from a task id.

        Args:
            task_id: The id of the task to run.
            parameters: Optional map of parameter slugs to values.
            env: Optional map of environment variables.
            constraints: Optional map of run constraints.

        Returns:
            The id of the run.

        Raises:
            HTTPError: If the run cannot be created or executed properly.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request(
            "POST",
            "/v0/runs/create",
            body={
                "taskID": task_id,
                "params": parameters,
                "env": env or {},
                "constraints": constraints or {},
            },
        )
        return resp["runID"]

    def execute_task(
        self,
        slug: str,
        param_values: Optional[Dict[str, ParamTypes]] = None,
        resources: Optional[Dict[str, str]] = None,
    ) -> str:
        """Executes an Airplane task with parameters and resources from a task slug.

        Args:
            slug: The slug of the task to run.
            param_values: Optional map of parameter slugs to values.
            resources: Optional map of resource aliases to ids.

        Returns:
            The id of the run.

        Raises:
            HTTPError: If the run cannot be executed.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """

        serialized_params = {
            key: serialize_param(val) for key, val in (param_values or {}).items()
        }

        return (
            self.__execute_task_self_hosted_storage(
                slug, serialized_params, resources or {}
            )
            if os.getenv("AP_AGENT_STORAGE_ZONE_SLUG")
            else self.__execute_task_airplane(slug, serialized_params, resources or {})
        )

    def __execute_task_airplane(
        self,
        slug: str,
        serialized_param_values: Dict[str, ParamDefTypes],
        resources: Dict[str, str],
    ) -> str:
        resp = self.__request(
            "POST",
            "/v0/tasks/execute",
            body={
                "slug": slug,
                "paramValues": serialized_param_values,
                "resources": resources,
            },
        )
        return resp["runID"]

    def __execute_task_self_hosted_storage(
        self,
        slug: str,
        serialized_param_values: Dict[str, ParamDefTypes],
        resources: Dict[str, str],
    ) -> str:
        pick_zone_resp = self.__pick_zone()
        if "zone" not in pick_zone_resp or not pick_zone_resp["zone"]:
            return self.__execute_task_airplane(
                slug, serialized_param_values, resources
            )
        zone = pick_zone_resp["zone"]

        # Create the inputs via the agent.
        agent_resp = self.__request(
            "POST",
            "/v0/dp/inputs/create",
            body={
                "paramValues": serialized_param_values,
                "constraintParams": pick_zone_resp["constraintParams"],
                "parameters": pick_zone_resp["parameters"],
            },
            extra_headers={"X-Airplane-Dataplane-Token": zone["accessToken"]},
            host=zone["dataPlaneURL"],
        )

        # Create the run in the Airplane api.
        airplane_resp = self.__request(
            "POST",
            "/v0/tasks/execute",
            body={
                "slug": slug,
                "paramValues": {
                    agent_resp["substituteValues"],
                },
                "resources": resources or {},
                "inputsZoneID": zone["id"],
                "inputsZoneToken": agent_resp["token"],
            },
        )
        return airplane_resp["runID"]

    def get_run(self, run_id: str) -> Dict[str, Any]:
        """Fetches an Airplane run.

        Args:
            run_id: The id of the run to fetch.

        Returns:
            The Airplane run's attributes.

        Raises:
            HTTPError: If the run cannot be fetched.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request(
            "GET",
            "/v0/runs/get",
            params={"id": run_id},
        )
        return resp

    def get_run_output(self, run_id: str) -> Any:
        """Fetches an Airplane run's output from the Airplane API.

        Args:
            run_id: The id of the run for which to fetch output.

        Returns:
            The Airplane run's outputs.

        Raises:
            HTTPError: If the run outputs cannot be fetched.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request(
            "GET",
            "/v0/runs/getOutputs",
            params={"id": run_id},
        )
        return resp["output"]

    def get_run_output_from_zone(self, run_id: str) -> Any:
        """Fetches an Airplane run's output from a self-hosted storage zone.

        Args:
            run_id: The id of the run for which to fetch output.

        Returns:
            The Airplane run's outputs.

        Raises:
            HTTPError: If the run outputs cannot be fetched.
            InvalidZoneException: If the zone info response is invalid.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        zone_info = self.get_run_zone(run_id)
        if (
            zone_info is None
            or "accessToken" not in zone_info
            or "dataPlaneURL" not in zone_info
        ):
            raise InvalidZoneException(
                f"Missing required fields in zone info response: {zone_info}",
            )
        resp = self.__request(
            "GET",
            "/v0/dp/runs/getOutputs",
            params={"runID": run_id},
            extra_headers={
                "X-Airplane-Dataplane-Token": zone_info["accessToken"],
            },
            host=zone_info["dataPlaneURL"],
        )
        return resp["output"]

    def get_run_zone(self, run_id: str) -> Any:
        """Fetches information about the storage zone for a run.

        Args:
            run_id: The id of the run for which to fetch zone information.

        Returns:
            The run's zone.

        Raises:
            HTTPError: If the run zone cannot be fetched.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request(
            "GET",
            "/v0/runs/getZone",
            params={"id": run_id},
        )
        return resp

    def create_text_display(self, content: str) -> str:
        """Creates a text display.

        Args:
            content: Content to display

        Returns:
            The Airplane display's id.

        Raises:
            HTTPError: If the display could not be created.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request(
            "POST",
            "/v0/displays/create",
            body={"display": {"content": content, "kind": "markdown"}},
        )
        if "display" in resp:
            # Older versions of the CLI incorrectly returned a "display" object.
            return resp["display"]["id"]
        return resp["id"]

    def create_json_display(
        self,
        payload: JSONType,
    ) -> str:
        """Creates a json display.

        Args:
            payload: Payload to display

        Returns:
            The Airplane display's id.

        Raises:
            HTTPError: If the display could not be created.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request(
            "POST",
            "/v0/displays/create",
            body={"display": {"value": payload, "kind": "json"}},
        )
        if "display" in resp:
            # Older versions of the CLI incorrectly returned a "display" object.
            return resp["display"]["id"]
        return resp["id"]

    def create_file_display(
        self,
        file: File,  # pylint: disable=redefined-outer-name
    ) -> str:
        """Creates a file display.

        Args:
            file: File to display

        Returns:
            The Airplane display's id.

        Raises:
            HTTPError: If the display could not be created.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request(
            "POST",
            "/v0/displays/create",
            body={"display": {"uploadID": file.id, "kind": "file"}},
        )
        if "display" in resp:
            # Older versions of the CLI incorrectly returned a "display" object.
            return resp["display"]["id"]
        return resp["id"]

    def create_upload(self, file_name: str, num_bytes: int) -> Dict[str, Any]:
        """Creates an upload.

        Args:
            file_name: Name of the file to create.
            num_bytes: Number of bytes in the upload.

        Returns:
            The Airplane upload's attributes.

        Raises:
            HTTPError: If the upload could not be created.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """

        return (
            self.__create_upload_self_hosted_storage(file_name, num_bytes)
            if os.getenv("AP_AGENT_STORAGE_ZONE_SLUG")
            else self.__create_upload_airplane_storage(file_name, num_bytes)
        )

    def __create_upload_airplane_storage(
        self, file_name: str, num_bytes: int
    ) -> Dict[str, Any]:
        return self.__request(
            "POST",
            "/v0/uploads/create",
            body={"fileName": file_name, "sizeBytes": num_bytes},
        )

    def __create_upload_self_hosted_storage(
        self, file_name: str, num_bytes: int
    ) -> Dict[str, Any]:
        # Find the zone to create the upload in.
        pick_zone_resp = self.__pick_zone()
        if "zone" not in pick_zone_resp or not pick_zone_resp["zone"]:
            return self.__create_upload_airplane_storage(file_name, num_bytes)
        zone = pick_zone_resp["zone"]

        # Create the upload via the agent.
        agent_upload_resp = self.__request(
            "POST",
            "/v0/dp/uploads/create",
            body={"fileName": file_name, "sizeBytes": num_bytes},
            extra_headers={"X-Airplane-Dataplane-Token": zone["accessToken"]},
            host=zone["dataPlaneURL"],
        )
        upload = agent_upload_resp["upload"]

        # Create the upload in the Airplane api.
        airplane_upload_resp = self.__request(
            "POST",
            "/v0/uploads/create",
            body={
                "fileName": file_name,
                "sizeBytes": num_bytes,
                "zoneID": zone["id"],
                "zoneToken": upload["zoneToken"],
            },
        )

        # Return the upload from Airplane and the urls from the agent.
        return {
            "upload": airplane_upload_resp["upload"],
            "readOnlyURL": agent_upload_resp["readOnlyURL"],
            "writeOnlyURL": agent_upload_resp["writeOnlyURL"],
        }

    def __pick_zone(self) -> Dict[str, Any]:
        return self.__request(
            "GET",
            "/v0/inputs/pickZone",
            params={
                "taskRevisionID": os.getenv("AIRPLANE_TASK_REVISION_ID"),
            },
        )

    def create_table_display(
        self,
        columns: Optional[List[Dict[str, Optional[str]]]],
        rows: List[Dict[str, Any]],
    ) -> str:
        """Creates a table display.

        Args:
            columns: Table columns containing keys slug and name
            rows: Table rows containing a map from column name to value.

        Returns:
            The Airplane display's id.

        Raises:
            HTTPError: If the display could not be created.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request(
            "POST",
            "/v0/displays/create",
            body={"display": {"columns": columns, "rows": rows, "kind": "table"}},
        )
        if "display" in resp:
            # Older versions of the CLI incorrectly returned a "display" object.
            return resp["display"]["id"]
        return resp["id"]

    def create_prompt(
        self,
        parameters: List[SerializedParam],
        reviewers: Optional[PromptReviewers],
        confirm_text: Optional[str],
        cancel_text: Optional[str],
        description: Optional[str],
        notify: bool,
    ) -> str:
        """Creates an Airplane prompt.

        Args:
            parameters: List of parameters.
            reviewers: Reviewers that are allowed to approve the prompt.
            confirm_text: Text of the confirmation button on the prompt dialog.
            cancel_text: Text of the cancellation button on the prompt dialog.
            description: Prompt description to display. Supports markdown.
            notify: Whether to notify reviewers when the prompt is created.

        Raises:
            HTTPError: If the prompt cannot be created properly.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request(
            "POST",
            "/v0/prompts/create",
            body={
                "schema": {
                    "parameters": [dataclasses.asdict(p) for p in parameters],
                },
                "reviewers": {
                    "users": reviewers.users,
                    "groups": reviewers.groups,
                    "allowSelfApprovals": reviewers.allow_self_approvals,
                }
                if reviewers
                else None,
                "confirmText": confirm_text,
                "cancelText": cancel_text,
                "description": description,
                "notify": notify,
            },
        )
        return resp["id"]

    def get_prompt(self, prompt_id: str) -> Dict[str, Any]:
        """Fetches an Airplane prompt.

        Args:
            prompt_id: The id of the prompt to fetch.

        Returns:
            The Airplane prompt's attributes.

        Raises:
            HTTPError: If the prompt cannot be fetched.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request("GET", "/v0/prompts/get", params={"id": prompt_id})
        return resp["prompt"]

    def get_user(self, user_id: str) -> Dict[str, Any]:
        """Fetches an Airplane user.

        Args:
            user_id: The id of the user to fetch.

        Returns:
            The Airplane user's attributes.

        Raises:
            HTTPError: If the user cannot be fetched.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request("GET", "/v0/users/get", params={"userID": user_id})
        return resp["user"]

    def get_task_reviewers(self, slug: str) -> Dict[str, Any]:
        """Fetches reviewers for an Airplane task.

        Args:
            slug: The slug of the task to fetch reviewers for.

        Returns:
            The Airplane task's reviewers.

        Raises:
            HTTPError: If the task reviewers cannot be fetched.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request(
            "GET", "/v0/tasks/getTaskReviewers", params={"taskSlug": slug}
        )
        return resp

    def get_trigger_request(self, trigger_request_id: str) -> Dict[str, Any]:
        """Fetches an Airplane trigger request.

        Args:
            trigger_request_id: The id of the trigger request to fetch.

        Returns:
            The Airplane trigger request's attributes.

        Raises:
            HTTPError: If the trigger request cannot be fetched.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request(
            "GET",
            "/v0/requests/get",
            params={"triggerRequestID": trigger_request_id},
        )
        return resp

    def create_task_request(
        self,
        trigger_id: str,
        param_values: Optional[Dict[str, ParamTypes]] = None,
        reason: Optional[str] = None,
        reviewers: Optional[List[TaskReviewer]] = None,
    ) -> str:
        """Requests an Airplane task.

        Args:
            trigger_id: The id of the trigger to execute.
            param_values: Optional map of parameter slugs to values.
            reason: Optional reason for the request.
            reviewers: Optional list of reviewers to request.

        Returns:
            The id of the trigger request.

        Raises:
            HTTPError: If the task cannot be requested.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        serialized_params = {}
        for key, val in (param_values or {}).items():
            serialized_params[key] = serialize_param(val)
        resp = self.__request(
            "POST",
            "/v0/requests/create",
            body={
                "triggerID": trigger_id,
                "reason": reason,
                "reviewers": [
                    {
                        "userID": r.user_id,
                        "groupID": r.group_id,
                    }
                    for r in reviewers
                ]
                if reviewers
                else None,
                "requestData": {
                    "taskData": {
                        "paramValues": serialized_params,
                    },
                },
            },
        )
        return resp["triggerRequestID"]

    def generate_id_token(self, audience: str) -> str:
        """Generates an OIDC ID token.

        Args:
            audience: Intended audience for the token.

        Returns:
            The id token.

        Raises:
            HTTPError: If the display could not be created.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request(
            "POST",
            "/v0/oidc/generateIDToken",
            body={"audience": audience},
        )
        return resp["token"]

    def create_sleep(self, seconds: float, until: str) -> str:
        """Creates an Airplane sleep.
        Args:
            seconds: Duration of the sleep in seconds.
            until: Time to sleep until in RFC3339 format.
        Raises:
            HTTPError: If the sleep cannot be created properly.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        duration_ms = int(seconds * 1000)
        resp = self.__request(
            "POST",
            "/v0/sleeps/create",
            body={
                "durationMs": duration_ms,
                "until": until,
            },
        )
        return resp["id"]

    def get_sleep(self, sleep_id: str) -> Sleep:
        """Fetches an Airplane sleep.
        Args:
            sleep_id: The id of the sleep to fetch.
        Returns:
            The Airplane sleep's attributes.
        Raises:
            HTTPError: If the sleep cannot be fetched.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        resp = self.__request("GET", "/v0/sleeps/get", params={"id": sleep_id})
        return Sleep(
            id=resp["id"],
            run_id=resp["runID"],
            created_at=resp["createdAt"],
            until=resp["until"],
            duration_ms=resp["durationMs"],
            skipped_at=resp.get("skippedAt"),
            skipped_by=resp.get("skippedBy"),
        )

    def __request(
        self,
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[JSONType] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        host: Optional[str] = None,
    ) -> Any:
        """Issues an Airplane API request.

        Args:
            method: The HTTP method to perform in uppercase.
            path: The API path to request (usually starting with `/v0/`).
            params: Optional query parameters to attach to the URL.
            body: Optional JSON body to send in the request.
            extra_headers: Optional extra headers to add to the request.
            host: Optional host. If unset, uses the configured Airplane API host.

        Returns:
            The deserialized JSON contents of the API response.

        Raises:
            HTTPError: If an occurs while issuing the API request.
            requests.exceptions.Timeout: If the request times out.
            requests.exceptions.ConnectionError: If a network error occurs.
        """
        user_agent = f"airplane/sdk/python/{self._version} team/{self._opts.team_id}"
        if self._opts.run_id:
            user_agent += " run/" + self._opts.run_id

        headers = {
            "Accept": "application/json",
            "User-Agent": user_agent,
            "X-Airplane-Client-Kind": "sdk/python",
            "X-Airplane-Client-Version": self._version,
            "X-Airplane-Token": self._opts.api_token,
            "X-Airplane-Env-ID": self._opts.env_id,
            "X-Team-ID": self._opts.team_id,
            "Idempotency-Key": str(uuid.uuid4()),
        }
        if method != "GET" and body is not None:
            headers["Content-Type"] = "application/json"
        if self._opts.tunnel_token:
            headers["X-Airplane-Dev-Token"] = self._opts.tunnel_token
        if self._opts.sandbox_token:
            headers["X-Airplane-Sandbox-Token"] = self._opts.sandbox_token

        if extra_headers:
            for key, value in extra_headers.items():
                headers[key] = value

        retries = 0
        # Perform up to 10 total attempts.
        max_retries = 9
        retry_after_seconds = 0

        if host:
            url = host + path
        else:
            url = self._opts.api_host + path

        while True:
            try:
                duration_seconds = _compute_retry_delay(retries)
                if retry_after_seconds > 0:
                    duration_seconds = retry_after_seconds
                    retry_after_seconds = 0
                if duration_seconds > 0:
                    sleep(duration_seconds)

                resp = requests.request(
                    method,
                    url=url,
                    params=params,
                    json=body,
                    headers=headers,
                    timeout=self._opts.timeout_seconds,
                )
                status = resp.status_code

                # If we got a 2xx status code, we can return successfully.
                if 200 <= status < 300:
                    return resp.json() if _is_json_response(resp) else resp.text

                airplane_retryable = resp.headers.get("x-airplane-retryable")
                can_retry_status = status == 429 or (status >= 500 and status != 501)
                can_retry = (
                    airplane_retryable != "false"
                    and retries < max_retries
                    and (can_retry_status or airplane_retryable == "true")
                )
                if not can_retry:
                    raise _http_error_from_resp(resp)

                retry_after_seconds = _parse_retry_after(resp)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if retries == max_retries:
                    raise
            finally:
                retries += 1


def _is_json_response(resp: Response) -> bool:
    return "application/json" in resp.headers.get("content-type", "")


def _parse_retry_after(resp: Response) -> int:
    header = resp.headers.get("retry-after", 0)
    try:
        return int(header)
    except ValueError:
        return 0


def _compute_retry_delay(retries: int) -> float:
    if retries <= 1:
        return 0
    base_sec = 0.1
    cap_sec = 30
    return random() * min(cap_sec, base_sec * 2 ** (retries - 1))


def _http_error_from_resp(resp: Response) -> HTTPError:
    msg = f"Request failed: {resp.status_code}"
    error_code = None
    if _is_json_response(resp):
        body = resp.json()
        if "error" in body:
            msg = body["error"]
        error_code = body.get("code")
    return HTTPError(message=msg, status_code=resp.status_code, error_code=error_code)


def client_opts_from_env() -> ClientOpts:
    """Creates ClientOpts from environment variables.

    Returns:
        Unvalidated ClientOpts from environment variables.

    Raises:
         InvalidEnvironmentException: If the environment is improperly configured.
    """

    opts = ClientOpts(
        api_host=os.getenv("AIRPLANE_API_HOST", ""),
        api_token=os.getenv("AIRPLANE_TOKEN", ""),
        env_id=os.getenv("AIRPLANE_ENV_ID", ""),
        team_id=os.getenv("AIRPLANE_TEAM_ID", ""),
        run_id=os.getenv("AIRPLANE_RUN_ID", ""),
        tunnel_token=os.getenv("AIRPLANE_TUNNEL_TOKEN", ""),
        sandbox_token=os.getenv("AIRPLANE_SANDBOX_TOKEN", ""),
    )
    if any(not x for x in [opts.api_host, opts.api_token, opts.env_id, opts.team_id]):
        raise InvalidEnvironmentException()
    return opts


def api_client_from_env() -> APIClient:
    """Creates an APIClient from environment variables.

    Returns:
        An APIClient to interact with the Airplane API.

    Raises:
        InvalidEnvironmentException: If the environment is improperly configured.
    """
    return api_client(client_opts_from_env())


@lru_cache(maxsize=None)
def api_client(opts: ClientOpts) -> APIClient:
    """Creates an APIClient

    Args:
        opts: The ClientOpts to use for the APIClient.

    Returns:
        An APIClient to interact with the Airplane API.
    """
    return APIClient(opts, __version__)
