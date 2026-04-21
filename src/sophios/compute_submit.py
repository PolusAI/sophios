from __future__ import annotations

import json
from pathlib import Path
from pprint import pprint
import time
from typing import Any, Mapping, cast

import requests

from .compute_payload import ComputeWorkflowPayload, validate_compute_payload
_TIMEOUT = (5, 30)
_STARTED = frozenset({"RUNNING", "COMPLETED", "ERROR", "CANCELLED"})
_SUCCESS = frozenset({"RUNNING", "COMPLETED"})


def submit_compute_payload(
    payload: ComputeWorkflowPayload,
    submit_url: str,
    *,
    timeout: tuple[int, int] = _TIMEOUT,
    poll_interval_seconds: int = 15,
    log_path: str | Path | None = None,
) -> int:
    """Submit a compute payload and wait for the job to start."""
    return _submit_payload_json(
        payload.get_compute_payload(),
        submit_url,
        timeout=timeout,
        poll_interval_seconds=poll_interval_seconds,
        log_path=log_path,
    )


def submit_compute_json(
    payload_json: Mapping[str, Any],
    submit_url: str,
    *,
    timeout: tuple[int, int] = _TIMEOUT,
    poll_interval_seconds: int = 15,
    log_path: str | Path | None = None,
) -> int:
    """Submit an already-rendered compute payload JSON object."""
    return _submit_payload_json(
        validate_compute_payload(payload_json),
        submit_url,
        timeout=timeout,
        poll_interval_seconds=poll_interval_seconds,
        log_path=log_path,
    )


def _submit_payload_json(
    payload_json: Mapping[str, Any],
    submit_url: str,
    *,
    timeout: tuple[int, int],
    poll_interval_seconds: int,
    log_path: str | Path | None,
) -> int:
    workflow_id = payload_json.get(
        "id") or payload_json["cwlWorkflow"].get("id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise ValueError(
            "compute payload must contain 'id' or 'cwlWorkflow.id' for status polling"
        )

    with requests.Session() as session:
        print("Sending request to Compute")
        response = session.post(
            _url(submit_url), json=payload_json, timeout=timeout)
        print(f"Post response code: {response.status_code}")
        print(f"Submit response: {_json_or_text(response)}")
        if not response.ok:
            return 1

        phase = _wait_for_started(
            session,
            submit_url,
            workflow_id,
            timeout=timeout,
            poll_interval_seconds=poll_interval_seconds,
        )
        if phase == "RUNNING":
            _print_logs(session, submit_url, workflow_id,
                        timeout=timeout, log_path=log_path)
        else:
            print(
                f"Job reached {phase or 'an unknown state'} before RUNNING; skipping log fetch."
            )
        return 0 if phase in _SUCCESS else 1


def _wait_for_started(
    session: requests.Session,
    submit_url: str,
    workflow_id: str,
    *,
    timeout: tuple[int, int],
    poll_interval_seconds: int,
) -> str:
    status_url = _url(submit_url, workflow_id, "status")
    while True:
        response = session.get(status_url, timeout=timeout)
        payload = _json_or_text(response)
        if response.ok and isinstance(payload, dict) and "status" in payload:
            print(json.dumps(payload, indent=2))
            phase = str(payload["status"]).upper()
            if phase in _STARTED:
                return phase
        time.sleep(poll_interval_seconds)


def _print_logs(
    session: requests.Session,
    submit_url: str,
    workflow_id: str,
    *,
    timeout: tuple[int, int],
    log_path: str | Path | None,
) -> None:
    response = session.get(
        _url(submit_url, workflow_id, "logs"), timeout=timeout)
    print(f"Logs response code: {response.status_code}")
    payload = _json_or_text(response)
    print("Toil logs:")
    if isinstance(payload, dict) and payload:
        payload = payload[next(iter(payload))]
    pprint(payload, indent=4)
    if log_path is not None:
        Path(log_path).write_text(str(payload), encoding="utf-8")


def _json_or_text(response: requests.Response) -> str | dict[str, Any] | list[Any]:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict):
        return cast(dict[str, Any], payload)
    if isinstance(payload, list):
        return payload
    return str(payload)


def _url(submit_url: str, workflow_id: str | None = None, endpoint: str | None = None) -> str:
    base = submit_url.rstrip("/") + "/"
    return base if workflow_id is None else f"{base}{workflow_id}/{endpoint}/"


__all__ = ["submit_compute_json", "submit_compute_payload"]
