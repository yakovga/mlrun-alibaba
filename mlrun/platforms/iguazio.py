# Copyright 2023 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import urllib
from collections import namedtuple
from urllib.parse import urlparse

import kfp.dsl
import requests
import semver
import v3io

import mlrun.errors
from mlrun.config import config as mlconf
from mlrun.utils import dict_to_json

_cached_control_session = None

VolumeMount = namedtuple("Mount", ["path", "sub_path"])


def mount_v3io(
    name="v3io",
    remote="",
    access_key="",
    user="",
    secret=None,
    volume_mounts=None,
):
    """Modifier function to apply to a Container Op to volume mount a v3io path

    :param name:            the volume name
    :param remote:          the v3io path to use for the volume. ~/ prefix will be replaced with /users/<username>/
    :param access_key:      the access key used to auth against v3io. if not given V3IO_ACCESS_KEY env var will be used
    :param user:            the username used to auth against v3io. if not given V3IO_USERNAME env var will be used
    :param secret:          k8s secret name which would be used to get the username and access key to auth against v3io.
    :param volume_mounts:   list of VolumeMount. empty volume mounts & remote will default to mount /v3io & /User.
    """
    volume_mounts, user = _enrich_and_validate_v3io_mounts(
        remote=remote,
        volume_mounts=volume_mounts,
        user=user,
    )

    def _attach_volume_mounts_and_creds(container_op: kfp.dsl.ContainerOp):
        from kubernetes import client as k8s_client

        vol = v3io_to_vol(name, remote, access_key, user, secret=secret)
        container_op.add_volume(vol)
        for volume_mount in volume_mounts:
            container_op.container.add_volume_mount(
                k8s_client.V1VolumeMount(
                    mount_path=volume_mount.path,
                    sub_path=volume_mount.sub_path,
                    name=name,
                )
            )

        if not secret:
            container_op = v3io_cred(access_key=access_key, user=user)(container_op)
        return container_op

    return _attach_volume_mounts_and_creds


def _enrich_and_validate_v3io_mounts(remote="", volume_mounts=None, user=""):
    if remote and not volume_mounts:
        raise mlrun.errors.MLRunInvalidArgumentError(
            "volume_mounts must be specified when remote is given"
        )

    # Empty remote & volume_mounts defaults are volume mounts of /v3io and /User
    if not remote and not volume_mounts:
        user = _resolve_mount_user(user)
        if not user:
            raise mlrun.errors.MLRunInvalidArgumentError(
                "user name/env must be specified when using empty remote and volume_mounts"
            )
        volume_mounts = [
            VolumeMount(path="/v3io", sub_path=""),
            VolumeMount(path="/User", sub_path="users/" + user),
        ]

    if not isinstance(volume_mounts, list) and any(
        [not isinstance(x, VolumeMount) for x in volume_mounts]
    ):
        raise TypeError("mounts should be a list of Mount")

    return volume_mounts, user


def _resolve_mount_user(user=None):
    return user or os.environ.get("V3IO_USERNAME")


def mount_spark_conf():
    def _mount_spark(container_op: kfp.dsl.ContainerOp):
        from kubernetes import client as k8s_client

        container_op.container.add_volume_mount(
            k8s_client.V1VolumeMount(
                name="spark-master-config", mount_path="/etc/config/spark"
            )
        )
        return container_op

    return _mount_spark


def mount_v3iod(namespace, v3io_config_configmap):
    def _mount_v3iod(container_op: kfp.dsl.ContainerOp):
        from kubernetes import client as k8s_client

        def add_vol(name, mount_path, host_path):
            vol = k8s_client.V1Volume(
                name=name,
                host_path=k8s_client.V1HostPathVolumeSource(path=host_path, type=""),
            )
            container_op.add_volume(vol)
            container_op.container.add_volume_mount(
                k8s_client.V1VolumeMount(mount_path=mount_path, name=name)
            )

        # this is a legacy path for the daemon shared memory
        host_path = "/dev/shm/"

        # path to shared memory for daemon was changed in Iguazio 3.2.3-b1
        igz_version = mlrun.mlconf.get_parsed_igz_version()
        if igz_version and igz_version >= semver.VersionInfo.parse("3.2.3-b1"):
            host_path = "/var/run/iguazio/dayman-shm/"
        add_vol(name="shm", mount_path="/dev/shm", host_path=host_path + namespace)

        add_vol(
            name="v3iod-comm",
            mount_path="/var/run/iguazio/dayman",
            host_path="/var/run/iguazio/dayman/" + namespace,
        )

        vol = k8s_client.V1Volume(
            name="daemon-health", empty_dir=k8s_client.V1EmptyDirVolumeSource()
        )
        container_op.add_volume(vol)
        container_op.container.add_volume_mount(
            k8s_client.V1VolumeMount(
                mount_path="/var/run/iguazio/daemon_health", name="daemon-health"
            )
        )

        vol = k8s_client.V1Volume(
            name="v3io-config",
            config_map=k8s_client.V1ConfigMapVolumeSource(
                name=v3io_config_configmap, default_mode=420
            ),
        )
        container_op.add_volume(vol)
        container_op.container.add_volume_mount(
            k8s_client.V1VolumeMount(mount_path="/etc/config/v3io", name="v3io-config")
        )

        container_op.container.add_env_variable(
            k8s_client.V1EnvVar(
                name="CURRENT_NODE_IP",
                value_from=k8s_client.V1EnvVarSource(
                    field_ref=k8s_client.V1ObjectFieldSelector(
                        api_version="v1", field_path="status.hostIP"
                    )
                ),
            )
        )
        container_op.container.add_env_variable(
            k8s_client.V1EnvVar(
                name="IGZ_DATA_CONFIG_FILE", value="/igz/java/conf/v3io.conf"
            )
        )

        return container_op

    return _mount_v3iod


def v3io_cred(api="", user="", access_key=""):
    """
    Modifier function to copy local v3io env vars to container

    Usage::

        train = train_op(...)
        train.apply(use_v3io_cred())
    """

    def _use_v3io_cred(container_op: kfp.dsl.ContainerOp):
        from os import environ

        from kubernetes import client as k8s_client

        web_api = api or environ.get("V3IO_API") or mlconf.v3io_api
        _user = user or environ.get("V3IO_USERNAME")
        _access_key = access_key or environ.get("V3IO_ACCESS_KEY")
        v3io_framesd = mlconf.v3io_framesd or environ.get("V3IO_FRAMESD")

        return (
            container_op.container.add_env_variable(
                k8s_client.V1EnvVar(name="V3IO_API", value=web_api)
            )
            .add_env_variable(k8s_client.V1EnvVar(name="V3IO_USERNAME", value=_user))
            .add_env_variable(
                k8s_client.V1EnvVar(name="V3IO_ACCESS_KEY", value=_access_key)
            )
            .add_env_variable(
                k8s_client.V1EnvVar(name="V3IO_FRAMESD", value=v3io_framesd)
            )
        )

    return _use_v3io_cred


def split_path(mntpath=""):
    if mntpath[0] == "/":
        mntpath = mntpath[1:]
    paths = mntpath.split("/")
    container = paths[0]
    subpath = ""
    if len(paths) > 1:
        subpath = mntpath[len(container) :]
    return container, subpath


def v3io_to_vol(name, remote="~/", access_key="", user="", secret=None):
    from os import environ

    from kubernetes import client

    access_key = access_key or environ.get("V3IO_ACCESS_KEY")
    opts = {"accessKey": access_key}
    user = user or environ.get("V3IO_USERNAME")
    if user:
        opts["dirsToCreate"] = f'[{{"name": "users//{user}", "permissions": 488}}]'

    remote = str(remote)

    if remote.startswith("~/"):
        if not user:
            raise mlrun.errors.MLRunInvalidArgumentError(
                'user name/env must be specified when using "~" in path'
            )
        if remote == "~/":
            remote = "users/" + user
        else:
            remote = "users/" + user + remote[1:]
    if remote:
        container, subpath = split_path(remote)
        opts["container"] = container
        opts["subPath"] = subpath

    if secret:
        secret = {"name": secret}

    vol = {
        "flexVolume": client.V1FlexVolumeSource(
            "v3io/fuse", options=opts, secret_ref=secret
        ),
        "name": name,
    }
    return vol


class OutputStream:
    def __init__(
        self,
        stream_path,
        shards=None,
        retention_in_hours=None,
        create=True,
        endpoint=None,
        access_key=None,
        mock=False,
        **kwargs,  # to avoid failing on extra parameters
    ):
        v3io_client_kwargs = {}
        if endpoint:
            v3io_client_kwargs["endpoint"] = endpoint
        if access_key:
            v3io_client_kwargs["access_key"] = access_key

        self._v3io_client = v3io.dataplane.Client(**v3io_client_kwargs)
        self._container, self._stream_path = split_path(stream_path)
        self._mock = mock
        self._mock_queue = []

        if create and not mock:
            # this import creates an import loop via the utils module, so putting it in execution path
            from mlrun.utils.helpers import logger

            logger.debug(
                "Creating output stream",
                endpoint=endpoint,
                container=self._container,
                stream_path=self._stream_path,
                shards=shards,
                retention_in_hours=retention_in_hours,
            )
            response = self._v3io_client.stream.create(
                container=self._container,
                stream_path=self._stream_path,
                shard_count=shards or 1,
                retention_period_hours=retention_in_hours or 24,
                raise_for_status=v3io.dataplane.RaiseForStatus.never,
            )
            if not (
                response.status_code == 400 and "ResourceInUse" in str(response.body)
            ):
                response.raise_for_status([409, 204])

    def push(self, data):
        def dump_record(rec):
            if not isinstance(rec, (str, bytes)):
                return dict_to_json(rec)
            return str(rec)

        if not isinstance(data, list):
            data = [data]
        records = [{"data": dump_record(rec)} for rec in data]
        if self._mock:
            # for mock testing
            self._mock_queue.extend(records)
        else:
            self._v3io_client.stream.put_records(
                container=self._container,
                stream_path=self._stream_path,
                records=records,
            )


class HTTPOutputStream:
    """HTTP output source that usually used for CE mode and debugging process"""

    def __init__(self, stream_path: str):
        self._stream_path = stream_path

    def push(self, data):
        def dump_record(rec):
            if isinstance(rec, bytes):
                return rec

            if not isinstance(rec, str):
                rec = dict_to_json(rec)

            return rec.encode("UTF-8")

        if not isinstance(data, list):
            data = [data]

        for record in data:
            # Convert the new record to the required format
            serialized_record = dump_record(record)
            response = requests.post(self._stream_path, data=serialized_record)
            if not response:
                raise mlrun.errors.MLRunInvalidArgumentError(
                    f"API call failed push a new record through {self._stream_path}, "
                    f"status {response.status_code}: {response.reason}"
                )


class KafkaOutputStream:
    def __init__(
        self,
        topic,
        brokers,
        producer_options=None,
        mock=False,
    ):
        self._kafka_producer = None
        self._topic = topic
        self._brokers = brokers
        self._producer_options = producer_options or {}

        self._mock = mock
        self._mock_queue = []

        self._initialized = False

    def _lazy_init(self):
        if self._initialized:
            return

        import kafka

        self._kafka_producer = kafka.KafkaProducer(
            bootstrap_servers=self._brokers,
            **self._producer_options,
        )

        self._initialized = True

    def push(self, data):
        self._lazy_init()

        def dump_record(rec):
            if isinstance(rec, bytes):
                return rec

            if not isinstance(rec, str):
                rec = dict_to_json(rec)

            return rec.encode("UTF-8")

        if not isinstance(data, list):
            data = [data]

        if self._mock:
            # for mock testing
            self._mock_queue.extend(data)
        else:
            for record in data:
                serialized_record = dump_record(record)
                self._kafka_producer.send(self._topic, serialized_record)


class V3ioStreamClient:
    def __init__(self, url: str, shard_id: int = 0, seek_to: str = None, **kwargs):
        endpoint, stream_path = parse_path(url)
        seek_options = ["EARLIEST", "LATEST", "TIME", "SEQUENCE"]
        seek_to = seek_to or "LATEST"
        seek_to = seek_to.upper()
        if seek_to not in seek_options:
            raise ValueError(f'seek_to must be one of {", ".join(seek_options)}')

        self._url = url
        self._container, self._stream_path = split_path(stream_path)
        self._shard_id = shard_id
        self._seek_to = seek_to
        self._client = v3io.dataplane.Client(endpoint=endpoint)
        self._seek_done = False
        self._location = ""
        self._kwargs = kwargs

    @property
    def url(self):
        return self._url

    @property
    def shard_id(self):
        return self._shard_id

    def seek(self):
        response = self._client.stream.seek(
            self._container,
            self._stream_path,
            self._shard_id,
            self._seek_to,
            raise_for_status=v3io.dataplane.RaiseForStatus.never,
            **self._kwargs,
        )
        if response.status_code == 404 and "ResourceNotFound" in str(response.body):
            return 0
        response.raise_for_status()
        self._location = response.output.location
        self._seek_done = True
        return response.status_code

    def get_records(self):
        if not self._seek_done:
            resp = self.seek()
            if resp == 0:
                return []
        response = self._client.stream.get_records(
            self._container, self._stream_path, self._shard_id, self._location
        )
        response.raise_for_status()
        self._location = response.output.next_location
        return response.output.records


def is_iguazio_endpoint(endpoint_url: str) -> bool:
    # TODO: find a better heuristic
    return ".default-tenant." in endpoint_url


def is_iguazio_session(value: str) -> bool:
    # TODO: find a better heuristic
    return len(value) > 20 and "-" in value


def is_iguazio_session_cookie(session_cookie: str) -> bool:
    if not session_cookie.strip():
        return False

    # decode url encoded cookie
    # from: j%3A%7B%22sid%22%3A%20%22946b0749-5c40-4837-a4ac-341d295bfaf7%22%7D
    # to:   j:{"sid":"946b0749-5c40-4837-a4ac-341d295bfaf7"}
    try:
        unqouted_cookie = urllib.parse.unquote(session_cookie.strip())
        if not unqouted_cookie.startswith("j:"):
            return is_iguazio_session(session_cookie)
        return json.loads(unqouted_cookie[2:])["sid"] is not None
    except Exception:
        return False


# we assign the control session or access key to the password since this is iguazio auth scheme
# (requests should be sent with username:control_session/access_key as auth header)
def add_or_refresh_credentials(
    api_url: str, username: str = "", password: str = "", token: str = ""
) -> (str, str, str):
    if is_iguazio_session(password):
        return username, password, token

    username = username or os.environ.get("V3IO_USERNAME")
    password = password or os.environ.get("V3IO_PASSWORD")
    # V3IO_ACCESS_KEY` is used by other packages like v3io, MLRun also uses it as the access key used to
    # communicate with the API from the client. `MLRUN_AUTH_SESSION` is for when we want
    # different access keys for the 2 usages
    token = (
        token
        # can't use mlrun.runtimes.constants.FunctionEnvironmentVariables.auth_session cause this is running in the
        # import execution path (when we're initializing the run db) and therefore we can't import mlrun.runtimes
        or os.environ.get("MLRUN_AUTH_SESSION")
        or os.environ.get("V3IO_ACCESS_KEY")
    )

    # When it's not iguazio endpoint it's one of two options:
    # Enterprise, but we're in the cluster (and not from remote), e.g. url will be something like http://mlrun-api:8080
    # In which we enforce to have access key which is needed for the API auth
    # Open source in which auth is not enabled so no creds needed
    # We don't really have an easy/nice way to differentiate between the two so we're just sending creds anyways
    # (ideally if we could identify we're in enterprise we would have verify here that token and username have value)
    if not is_iguazio_endpoint(api_url):
        return "", "", token

    if not username or not token:
        raise ValueError(
            "username and access key required to authenticate against iguazio system"
        )
    return username, token, ""


def parse_path(url, suffix="/"):
    """return endpoint and table path from url"""
    parsed_url = urlparse(url)
    if parsed_url.netloc:
        scheme = parsed_url.scheme.lower()
        if scheme == "s3":
            prefix = "s3"
        elif scheme == "v3ios":
            prefix = "https"
        elif scheme == "v3io":
            prefix = "http"
        elif scheme == "redis":
            prefix = "redis"
        elif scheme == "rediss":
            prefix = "rediss"
        elif scheme == "ds":
            prefix = "ds"
        else:
            raise mlrun.errors.MLRunInvalidArgumentError(
                "url must start with v3io/v3ios/redis/rediss, got " + url
            )
        endpoint = f"{prefix}://{parsed_url.netloc}"
    else:
        # no netloc is mainly when using v3io (v3io:///) and expecting the url to be resolved automatically from env or
        # config
        endpoint = None
    return endpoint, parsed_url.path.strip("/") + suffix


def sanitize_username(username: str):
    """
    The only character an Iguazio username may have that is not valid for k8s usage is underscore (_)
    So simply replace it with dash
    """
    return username.replace("_", "-")
