"""The one thing a restore cannot do from inside the stack, done by something that can.

Two of the ten stores this appliance backs up live in volumes owned by other containers.
Keycloak keeps an embedded H2 database in its data volume — every user, every session, the
realm signing keys — and that file is open and memory-mapped while Keycloak runs, so
writing over it under a running server does not restore anything, it corrupts what is
there. Hatchet's generated config is read at boot. Neither can be replaced by a process
inside the stack, because stopping the container that owns a volume is not something the
stack can do to itself.

So they were "offline only", and a restore from the admin UI covered eight of ten stores
while a firm's users and sign-in configuration needed somebody at a terminal with
``scripts/restore-backup.sh``. That is the wrong half to leave at the command line: it is
the half nobody rehearses.

The same reasoning applies to the other thing a restore cannot do from inside the stack:
restart the services it has just pulled the floor out from under. ``pg_restore --clean
--if-exists`` drops and recreates every type and table while the service that owns the
database is still connected, so that service goes on using cached query plans and type OIDs
naming objects which no longer exist. Postgres then answers it with "cached plan must not
change result type" and "cache lookup failed for type ...", and it does not recover on its
own — which is how a restore that put every row back left the appliance without its
orchestrator. Restarting is what fixes it, and restarting a container is again something
the stack cannot do to itself.

This agent closes both, and the shape is chosen for what it refuses rather than what it
does. Reaching Docker means holding a socket that is root on the host, and putting that
socket in the container that parses documents, serves the admin UI and talks to the
internet would mean any hole in any of them is a hole in the host. So the socket lives
here, in a process that:

* accepts exactly two operations — replace the contents of a named volume from a named
  archive, stopping and restarting the one container that owns it, and restart one named
  compose service;
* takes the volume and the service from fixed tables in this file, never from the request,
  so a caller cannot name a volume or a container this appliance does not own;
* takes the archive from the restore staging directory only, so a caller cannot ask it to
  unpack something else;
* is reachable only on the compose network, and only with the shared secret the appliance
  is given.

The second operation does widen what a caller can reach, and the honest way to put it is
that it widens the list rather than the kind. Replacing a volume already stops and starts a
container, so bouncing Keycloak and Hatchet was always available to anything holding the
secret; restarting adds the appliance's own app, worker, watcher, gateway and tracing
containers to the set. What that buys an attacker who already holds the app is the ability
to interrupt an appliance they are already inside. What it does not buy is a container this
appliance does not run, because the table is here and the request only chooses from it.

A compromise of the app therefore buys the ability to restart this appliance's own
containers and rewrite two volumes from a verified backup. It does not buy the host.
"""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DOCKER_SOCKET = os.environ.get("KI_RESTORE_AGENT_DOCKER_SOCKET", "/var/run/docker.sock")
SECRET_ENV = "KI_RESTORE_AGENT_SECRET"
AGENT_URL_ENV = "KI_RESTORE_AGENT_URL"
# How long to wait for a container to actually stop before giving up. Keycloak flushes H2
# on shutdown; killing it early is the corruption this exists to avoid.
STOP_TIMEOUT_SECONDS = 60


def _log(message: str) -> None:
    print(f"[ki restore-agent] {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class ManagedVolume:
    """One volume this agent may replace, and the container that has to stop for it."""

    component: str
    # The compose service that owns it. Resolved to a container by label rather than by
    # name, so a deployment that renamed its project still works.
    service: str
    # Where the volume is mounted into this agent.
    path: str
    why: str


MANAGED: dict[str, ManagedVolume] = {
    "volumes/keycloak": ManagedVolume(
        component="volumes/keycloak",
        service="keycloak",
        path="/restore-target/keycloak",
        why="every user, every session and the realm signing keys",
    ),
    "volumes/hatchet-config": ManagedVolume(
        component="volumes/hatchet-config",
        service="hatchet",
        path="/restore-target/hatchet-config",
        why="the orchestrator's generated config, which is what makes an issued token valid",
    ),
}


@dataclass(frozen=True)
class RestartableService:
    """One compose service this agent may restart, and what a restore breaks in it."""

    service: str
    why: str


# Every service that holds a connection pool against a database this appliance restores,
# and nothing else. A fixed table for the same reason MANAGED is one: the service name
# reaches the Docker API, and a caller who could choose it freely could stop any container
# on the host, including the ones this agent's isolation depends on.
RESTARTABLE: dict[str, RestartableService] = {
    "hatchet": RestartableService(
        service="hatchet",
        why="the orchestrator, which stops polling cron schedules and acquiring queue leases",
    ),
    "litellm": RestartableService(
        service="litellm",
        why="the model gateway, which holds a pool against its own database",
    ),
    "langfuse": RestartableService(
        service="langfuse",
        why="model-call tracing, which holds a pool against its own database",
    ),
    "app": RestartableService(
        service="app",
        why="the admin UI and API, whose pool is the one an operator watches the restore through",
    ),
    "worker": RestartableService(
        service="worker",
        why="the process that runs syncs, insertion and the restore itself",
    ),
    "watcher": RestartableService(
        service="watcher",
        why="the folder watcher, which would otherwise sit failing every scan",
    ),
}


class ReplaceRequest(BaseModel):
    component: str = Field(min_length=1, max_length=100)
    # A path under the restore staging directory. Checked against it rather than trusted.
    archive: str = Field(min_length=1, max_length=4096)


class RestartRequest(BaseModel):
    # Checked against RESTARTABLE, not passed to Docker as given.
    service: str = Field(min_length=1, max_length=100)


def stage_root() -> Path:
    from knowledge_index.backup.restore_runs import stage_root as configured

    return configured()


# ------------------------------------------------------------------------------ docker


def _docker() -> httpx.Client:
    return httpx.Client(transport=httpx.HTTPTransport(uds=DOCKER_SOCKET), base_url="http://docker")


def _container_id(client: httpx.Client, service: str) -> str:
    """The running container for one compose service, by label.

    By label and not by name: the container is called
    ``<project>-<service>-<n>``, and a deployment that set a different project name would
    silently match nothing — which during a restore reads as "the volume was replaced" when
    it was not.
    """
    response = client.get(
        "/containers/json",
        params={"all": "true", "filters": f'{{"label":["com.docker.compose.service={service}"]}}'},
        timeout=30,
    )
    response.raise_for_status()
    found = response.json()
    if not found:
        raise HTTPException(
            status_code=503,
            detail=(
                f"no container is labelled com.docker.compose.service={service}, so the "
                "agent cannot stop the one that owns this volume"
            ),
        )
    return str(found[0]["Id"])


def _stop(client: httpx.Client, container: str) -> None:
    client.post(
        f"/containers/{container}/stop", params={"t": STOP_TIMEOUT_SECONDS}, timeout=STOP_TIMEOUT_SECONDS + 30
    ).raise_for_status()
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS + 30
    while time.monotonic() < deadline:
        state = client.get(f"/containers/{container}/json", timeout=30).json()
        if not state.get("State", {}).get("Running"):
            return
        time.sleep(1)
    raise HTTPException(status_code=504, detail="the container did not stop; nothing was changed")


def _start(client: httpx.Client, container: str) -> None:
    response = client.post(f"/containers/{container}/start", timeout=120)
    # 304 is "already running", which is success for our purposes.
    if response.status_code not in (204, 304):
        response.raise_for_status()


# ------------------------------------------------------------------------------- agent


def _authorize(authorization: str) -> None:
    """The same refusal for every operation, so adding one cannot quietly add a way in."""
    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        raise HTTPException(
            status_code=503,
            detail=f"{SECRET_ENV} is not set on the agent, so it refuses every request",
        )
    if authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="restore agent secret is missing or wrong")


def create_agent() -> FastAPI:
    app = FastAPI(title="knowledge-index restore agent", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "manages": sorted(MANAGED), "restarts": sorted(RESTARTABLE)}

    @app.post("/restart-service")
    def restart_service(payload: RestartRequest, authorization: str = Header(default="")) -> dict:
        _authorize(authorization)
        managed = RESTARTABLE.get(payload.service)
        if managed is None:
            raise HTTPException(
                status_code=400,
                detail=f"{payload.service} is not a service this agent may restart",
            )
        with _docker() as client:
            container = _container_id(client, managed.service)
            _log(f"restarting {managed.service}: {managed.why}")
            # The same stop and start that replacing a volume uses, rather than Docker's
            # restart endpoint: one fewer call whose failure modes this agent has never had
            # to handle, and _stop is the half that polls until the daemon reports the
            # container down and answers 504 if it never does. So a restart is reported as
            # done only after the process holding the pool the restore invalidated has been
            # watched to go, rather than on the strength of one 204.
            _stop(client, container)
            _start(client, container)
        return {"service": managed.service, "ok": True}

    @app.post("/replace-volume")
    def replace_volume(payload: ReplaceRequest, authorization: str = Header(default="")) -> dict:
        _authorize(authorization)

        managed = MANAGED.get(payload.component)
        if managed is None:
            raise HTTPException(
                status_code=400,
                detail=f"{payload.component} is not a volume this agent manages",
            )
        target = Path(managed.path)
        if not target.is_dir():
            raise HTTPException(
                status_code=503,
                detail=f"{target} is not mounted into the agent, so it cannot be replaced",
            )

        archive = Path(payload.archive).resolve()
        root = stage_root().resolve()
        # The archive comes from the staging directory or from nowhere. Otherwise this
        # endpoint is "unpack any file on the host into a volume as root".
        if root != archive.parent and root not in archive.parents:
            raise HTTPException(
                status_code=400,
                detail=f"{archive} is not in this appliance's restore staging directory",
            )
        if not archive.is_file():
            raise HTTPException(status_code=400, detail=f"{archive} does not exist")
        _verify_archive(archive, target)

        with _docker() as client:
            container = _container_id(client, managed.service)
            _log(f"stopping {managed.service} to replace {payload.component}")
            _stop(client, container)
            try:
                _replace(archive, target)
            finally:
                # Started again whatever happened. Leaving a firm's identity provider down
                # because an extraction failed turns a failed restore into an outage.
                _log(f"starting {managed.service}")
                _start(client, container)
        return {"component": payload.component, "service": managed.service, "ok": True}

    return app


def _verify_archive(archive: Path, target: Path) -> None:
    """Refuse an archive that would write outside the volume, before anything is stopped."""
    try:
        with tarfile.open(archive, "r:*") as tar:
            for member in tar.getmembers():
                resolved = (target / member.name).resolve()
                if target.resolve() != resolved and target.resolve() not in resolved.parents:
                    raise HTTPException(
                        status_code=400,
                        detail=f"archive entry escapes the volume: {member.name!r}",
                    )
    except tarfile.TarError as exc:
        raise HTTPException(status_code=400, detail=f"{archive} is not a readable archive") from exc


def _replace(archive: Path, target: Path) -> None:
    """Empty the volume and unpack the backup into it.

    Emptied first: extracting over what is there merges two appliances' state, which for
    Keycloak means stale realm keys sitting beside the restored ones and sign-ins failing
    in a way nobody can trace back to this moment.
    """
    for item in target.iterdir():
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        try:
            tar.extractall(target, filter="data")
        except TypeError:  # Python without PEP 706 filters; _verify_archive already ran
            tar.extractall(target)  # noqa: S202


# ------------------------------------------------------------------------------- client


def agent_url() -> str:
    return os.environ.get(AGENT_URL_ENV, "").strip()


def available() -> bool:
    """Whether a restore can replace the volume-backed stores from the admin UI."""
    if not agent_url() or not os.environ.get(SECRET_ENV, ""):
        return False
    try:
        with httpx.Client(base_url=agent_url(), timeout=5) as client:
            return client.get("/healthz").status_code == 200
    except Exception:  # noqa: BLE001 - unreachable is the same answer as absent
        return False


def replace_volume(component: str, archive: Path) -> dict:
    """Ask the agent to replace one volume. Raises with the agent's own words."""
    url = agent_url()
    secret = os.environ.get(SECRET_ENV, "")
    if not url or not secret:
        raise RuntimeError(
            "the restore agent is not configured, so the identity and orchestrator volumes "
            "cannot be replaced from here. Set KI_RESTORE_AGENT_URL and "
            "KI_RESTORE_AGENT_SECRET, or use scripts/restore-backup.sh."
        )
    with httpx.Client(base_url=url, timeout=600) as client:
        response = client.post(
            "/replace-volume",
            json={"component": component, "archive": str(archive)},
            headers={"Authorization": f"Bearer {secret}"},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"the restore agent refused {component}: {_refusal(response)}")
    return response.json()


def restart_service(service: str) -> dict:
    """Ask the agent to restart one compose service. Raises with the agent's own words.

    Every failure arrives as ``RuntimeError``, transport included: the caller is a restore
    that has already written the estate back, and it has to tell the difference between
    "restarted" and "somebody must do this by hand" without also having to know what an
    ``httpx`` exception looks like.
    """
    url = agent_url()
    secret = os.environ.get(SECRET_ENV, "")
    if not url or not secret:
        raise RuntimeError(
            "the restore agent is not configured, so the services holding connections to "
            "the restored databases cannot be restarted from here. Set KI_RESTORE_AGENT_URL "
            "and KI_RESTORE_AGENT_SECRET, or restart them with docker compose."
        )
    try:
        # Long enough for the stop to run its full grace period and the container to come
        # back, and no longer: a restart that is still not finished by then is a fact the
        # operator needs, not something to keep waiting on.
        with httpx.Client(base_url=url, timeout=STOP_TIMEOUT_SECONDS + 120) as client:
            response = client.post(
                "/restart-service",
                json={"service": service},
                headers={"Authorization": f"Bearer {secret}"},
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"the restore agent could not be reached to restart {service}: {exc}"
        ) from exc
    if response.status_code >= 400:
        raise RuntimeError(f"the restore agent refused to restart {service}: {_refusal(response)}")
    return response.json()


def _refusal(response: httpx.Response) -> str:
    """The agent's own words about a refusal, rather than a status code nobody can act on."""
    try:
        return str(response.json().get("detail") or response.text)
    except ValueError:
        return response.text
