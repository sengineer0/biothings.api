import asyncio
import copy
import json
import time
from collections import UserDict
from dataclasses import dataclass
from datetime import datetime
from functools import partial

import boto3
from biothings.hub import SNAPSHOOTER_CATEGORY, SNAPSHOTMANAGER_CATEGORY
from biothings.hub.databuild.buildconfig import AutoBuildConfig
from biothings.hub.datarelease import set_pending_to_release_note
from biothings.utils.common import merge
from biothings.utils.hub import template_out
from biothings.utils.hub_db import get_src_build
from biothings.utils.manager import BaseManager
from elasticsearch import Elasticsearch

from config import logger as logging

from . import snapshot_registrar as registrar
from .snapshot_repo import Repository
from .snapshot_task import Snapshot


class ProcessInfo():
    """
    JobManager Process Info.
    Reported in Biothings Studio.
    """

    def __init__(self, env):
        self.env_name = env

    def get_predicates(self):
        return []

    def get_pinfo(self, step, snapshot, description=""):
        pinfo = {
            "__predicates__": self.get_predicates(),
            "category": SNAPSHOOTER_CATEGORY,
            "step": f"{step}:{snapshot}",
            "description": description,
            "source": self.env_name
        }
        return pinfo


@dataclass
class CloudStorage():
    type: str
    access_key: str
    secret_key: str
    region: str = "us-west-2"

    def get(self):
        if self.type == "aws":
            session = boto3.Session(
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region)
            return session.resource("s3")
        raise ValueError(self.type)

class Bucket():
    # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#S3.Client.create_bucket

    def __init__(self, client, bucket):
        self.client = client
        self.bucket = bucket

    def exists(self):
        bucket = self.client.Bucket(self.bucket)
        return bool(bucket.creation_date)

    def create(self, acl="private"):
        return self.client.create_bucket(
            ACL=acl, Bucket=self.bucket,
            CreateBucketConfiguration={
                'LocationConstraint': self.region
            }
        )

    def __str__(self):
        return (
            f"<Bucket {'READY' if self.exists() else 'MISSING'}"
            f" name='{self.bucket}'"
            f" client={self.client}"
            f">"
        )

class RepositoryConfig(UserDict):
    """
    {
        "type": "s3",
        "name": "s3-$(Y)",
        "settings": {
            "bucket": "<SNAPSHOT_BUCKET_NAME>",
            "base_path": "mynews.info/$(Y)",  # per year
        }
    }
    """
    @property
    def repo(self):
        return self["name"]

    @property
    def bucket(self):
        return self["settings"]["bucket"]

    def format(self, doc=None):
        """ Template special values in this config.

        For example:
        {
            "bucket": "backup-$(Y)",
            "base_path" : "snapshots/%(_meta.build_version)s"
        }
        where "_meta.build_version" value is taken from doc in
        dot field notation, and the current year replaces "$(Y)".
        """
        template = json.dumps(self.data)
        string = template_out(template, doc or {})
        if "%" in string:
            raise ValueError("Failed to template.")
        return RepositoryConfig(json.loads(string))


class _SnapshotResult(UserDict):

    def __str__(self):
        return f"{type(self).__name__}({str(self.data)})"

class CumulativeResult(_SnapshotResult):
    ...

class StepResult(_SnapshotResult):
    ...

class SnapshotEnv():

    def __init__(self, job_manager, cloud, repository, indexer, **kwargs):
        self.job_manager = job_manager

        self.cloud = CloudStorage(**cloud).get()
        self.repcfg = RepositoryConfig(repository)
        self.client = Elasticsearch(**indexer["args"])

        self.name = kwargs["name"]  # snapshot env
        self.idxenv = indexer["name"]  # indexer env

        self.pinfo = ProcessInfo(self.name)
        self.wtime = kwargs.get("monitor_delay", 15)

    def _doc(self, index):  # TODO UNIQUENESS
        doc = get_src_build().find_one({
            f"index.{index}.environment": self.idxenv})
        if not doc:  # not asso. with a build
            raise ValueError("Not a hub-managed index.")
        return doc

    def snapshot(self, index, snapshot=None):
        @asyncio.coroutine
        def _snapshot(snapshot):
            x = CumulativeResult()
            for step in ("pre", "snapshot", "post"):
                state = registrar.dispatch(step)  # _TaskState Class
                state = state(get_src_build(), self._doc(index).get("_id"))
                logging.info(state)
                state.started()

                pinfo = self.pinfo.get_pinfo(step, snapshot)
                job = yield from self.job_manager.defer_to_thread(
                    pinfo, partial(getattr(self, state.func), index, snapshot))
                try:
                    dx = yield from job
                    dx = StepResult(dx)

                except Exception as exc:
                    logging.exception(exc)
                    state.failed(exc)
                    raise exc
                else:
                    merge(x.data, dx.data)
                    logging.info(dx)
                    logging.info(x)
                    state.succeed({
                        snapshot: x.data
                    })
            return x
        future = asyncio.ensure_future(_snapshot(snapshot or index))
        future.add_done_callback(logging.debug)
        return future

    def pre_snapshot(self, index, snapshot):

        cfg = self.repcfg.format(self._doc(index))
        bucket = Bucket(self.cloud, cfg.bucket)
        repo = Repository(self.client, cfg.repo)

        logging.info(bucket)
        logging.info(repo)

        if not repo.exists():
            if not bucket.exists():
                bucket.create(cfg.get("acl", "private"))
                logging.info(bucket)
            repo.create(**cfg["settings"])
            logging.info(repo)

        return {
            "indexer_env": self.idxenv,
            "environment": self.name
        }

    def _snapshot(self, index, snapshot):

        snapshot = Snapshot(
            self.client,
            self.repcfg.repo,
            snapshot)
        logging.info(snapshot)

        _replace = False
        if snapshot.exists():
            snapshot.delete()
            logging.info(snapshot)
            _replace = True

        # ------------------ #
        snapshot.create(index)
        # ------------------ #

        while True:
            logging.info(snapshot)
            state = snapshot.state()

            if state == "FAILED":
                raise ValueError(state)
            elif state == "SUCCESS":
                break

            # Wait "IN_PROGRESS"
            time.sleep(self.wtime)

        return {
            "replaced": _replace,
            "created_at": datetime.now().astimezone()
        }

    def post_snapshot(self, index, snapshot):
        # TODO
        # set_pending_to_release_note(self.build_doc['_id'])
        return {}


class SnapshotManager(BaseManager):
    """
    Hub ES Snapshot Management

    Config Ex:

    # env.<name>:
    {
        "cloud": {
            "type": "aws",  # default, only one supported for now
            "access_key": <------------------>,
            "secret_key": <------------------>,
            "region": "us-west-2"
        },
        "repository": {
            "name": "s3-$(Y)",
            "type": "s3",
            "settings": {
                "bucket": "<SNAPSHOT_BUCKET_NAME>",
                "base_path": "mynews.info/$(Y)",  # per year
                "region": "us-west-2",
            },
            "acl": "private",
        },
        "indexer": {
            "env": "local",
            "args": {
                "timeout": 100,
                "max_retries": 5
            }
        },
        "monitor_delay": 15,
    }    
    """

    def __init__(self, index_manager, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.index_manager = index_manager
        self.snapshot_config = {}

    @staticmethod
    def pending_snapshot(build_name):
        src_build = get_src_build()
        src_build.update({"_id": build_name}, {"$addToSet": {"pending": "snapshot"}})

    # Object Lifecycle Calls
    # --------------------------
    # manager = IndexManager(job_manager)
    # manager.clean_stale_status() # in __init__
    # manager.configure(config)

    def clean_stale_status(self):
        registrar.audit(get_src_build(), logging)

    def configure(self, conf):
        self.snapshot_config = conf
        for name, envdict in conf.get("env", {}).items():

            # Merge Indexer Config
            # -------------------------------
            dx = envdict["indexer"]

            if isinstance(dx, str):  # {"indexer": "prod"}
                return dict(name=dx)  # .        ↓
            if not isinstance(dx, dict):  # {"indexer": {"name": "prod"}}
                raise TypeError(dx)

            # compatibility with previous hubs.
            dx.setdefault("name", dx.pop("env", None))

            x = self.index_manager[dx["name"]]
            x = dict(x)
            merge(x, dx)  # <-

            envdict["indexer"] = x
            # -------------------------------
            envdict["name"] = name

            self.register[name] = SnapshotEnv(self.job_manager, **envdict)

    def poll(self, state, func):
        super().poll(state, func, col=get_src_build())

    # Features
    # -----------

    def snapshot(self, snapshot_env, index, snapshot=None, **kwargs):
        """
        Create a snapshot named "snapshot" (or, by default, same name as the index)
        from "index" according to environment definition (repository, etc...) "env".
        """
        env = self.register[snapshot_env]
        return env.snapshot(index, snapshot, **kwargs)

    def snapshot_build(self, build_doc):
        """
        Create a snapshot basing on the autobuild settings in the build config.
        If the build config associated with this build has:
        {
            "autobuild": {
                "type": "snapshot", // implied when env is set. env must be set.
                "env": "local" // which es env to make the snapshot.
            },
            ...
        }
        Attempt to make a snapshot for this build on the specified es env "local".
        """
        @asyncio.coroutine
        def _():
            autoconf = AutoBuildConfig(build_doc['build_config'])
            env = autoconf.auto_build.get('env')
            assert env, "Unknown autobuild env."
            try:
                latest_index = list(build_doc['index'].keys())[-1]
            except Exception:
                logging.info("No index already created, now create one.")
                yield from self.index_manager.index(env, build_doc['_id'])
                latest_index = build_doc['_id']
            return self.snapshot(env, latest_index)
        return asyncio.ensure_future(_())

    def snapshot_info(self, env=None, remote=False):
        return self.snapshot_config
