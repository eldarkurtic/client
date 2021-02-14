# File is generated by: tox -e codemod
# -*- coding: utf-8 -*-
"""Handle Manager."""

from __future__ import print_function

import json
import logging
import numbers
import os

import six
import wandb
from wandb.proto import wandb_internal_pb2

from . import meta, sample, stats
from . import tb_watcher
from ..lib import proto_util


if wandb.TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        Dict,
        Iterable,
        Optional,
    )
    from .settings_static import SettingsStatic
    from six.moves.queue import Queue
    from threading import Event
    from ..interface.interface import BackendSender
    from wandb.proto.wandb_internal_pb2 import Record, Result

    SummaryDict = Dict[str, Any]


logger = logging.getLogger(__name__)


class HandleManager(object):

    # _consolidated_summary: SummaryDict
    # _sampled_history: Dict[str, sample.UniformSampleAccumulator]
    # _settings: SettingsStatic
    # _record_q: "Queue[Record]"
    # _result_q: "Queue[Result]"
    # _stopped: Event
    # _sender_q: "Queue[Record]"
    # _writer_q: "Queue[Record]"
    # _interface: BackendSender
    # _system_stats: Optional[stats.SystemStats]
    # _tb_watcher: Optional[tb_watcher.TBWatcher]
    # _metric_defines: Dict[str, wandb_internal_pb2.MetricRecord]
    # _metric_globs: Dict[str, wandb_internal_pb2.MetricRecord]
    # _metric_track: Dict[str, float]

    def __init__(
        self,
        settings,
        record_q,
        result_q,
        stopped,
        sender_q,
        writer_q,
        interface,
    ):
        self._settings = settings
        self._record_q = record_q
        self._result_q = result_q
        self._stopped = stopped
        self._sender_q = sender_q
        self._writer_q = writer_q
        self._interface = interface

        self._tb_watcher = None
        self._system_stats = None
        self._step = 0

        # keep track of summary from key/val updates
        self._consolidated_summary = dict()
        self._sampled_history = dict()
        self._metric_defines = dict()
        self._metric_globs = dict()
        self._metric_track = dict()

    def handle(self, record):
        record_type = record.WhichOneof("record_type")
        assert record_type
        handler_str = "handle_" + record_type
        handler = getattr(self, handler_str, None)
        assert handler, "unknown handle: {}".format(handler_str)
        handler(record)

    def handle_request(self, record):
        request_type = record.request.WhichOneof("request_type")
        assert request_type
        handler_str = "handle_request_" + request_type
        handler = getattr(self, handler_str, None)
        logger.debug("handle_request: {}".format(request_type))
        assert handler, "unknown handle: {}".format(handler_str)
        handler(record)

    def _dispatch_record(self, record, always_send = False):
        if not self._settings._offline or always_send:
            self._sender_q.put(record)
        if not record.control.local:
            self._writer_q.put(record)

    def handle_request_defer(self, record):
        defer = record.request.defer
        state = defer.state

        logger.info("handle defer: {}".format(state))
        # only handle flush tb (sender handles the rest)
        if state == defer.FLUSH_STATS:
            if self._system_stats:
                # TODO(jhr): this could block so we dont really want to call shutdown
                # from handler thread
                self._system_stats.shutdown()
        elif state == defer.FLUSH_TB:
            if self._tb_watcher:
                # shutdown tensorboard workers so we get all metrics flushed
                self._tb_watcher.finish()
                self._tb_watcher = None
        elif state == defer.FLUSH_SUM:
            self._save_summary(self._consolidated_summary, flush=True)

        # defer is used to drive the sender finish state machine
        self._dispatch_record(record, always_send=True)

    def handle_request_login(self, record):
        self._dispatch_record(record)

    def handle_run(self, record):
        self._dispatch_record(record)

    def handle_stats(self, record):
        self._dispatch_record(record)

    def handle_config(self, record):
        self._dispatch_record(record)

    def handle_output(self, record):
        self._dispatch_record(record)

    def handle_files(self, record):
        self._dispatch_record(record)

    def handle_artifact(self, record):
        self._dispatch_record(record)

    def handle_alert(self, record):
        self._dispatch_record(record)

    def _save_summary(self, summary_dict, flush = False):
        summary = wandb_internal_pb2.SummaryRecord()
        for k, v in six.iteritems(summary_dict):
            update = summary.update.add()
            update.key = k
            update.value_json = json.dumps(v)
        record = wandb_internal_pb2.Record(summary=summary)
        if flush:
            self._dispatch_record(record)
        elif not self._settings._offline:
            self._sender_q.put(record)

    def _save_history(self, record):
        for item in record.history.item:
            # TODO(jhr) save nested keys?
            k = item.key
            v = json.loads(item.value_json)
            if isinstance(v, numbers.Real):
                self._sampled_history.setdefault(k, sample.UniformSampleAccumulator())
                self._sampled_history[k].add(v)

    def _update_summary_metrics(
        self,
        s,
        k,
        v,
        float_v,
        goal_max,
    ):
        updated = False
        best_key = None
        if s.best and goal_max is not None:
            best_key = k + ".best"
        if s.max or goal_max and best_key:
            max_key = k + ".max"
            old_max = self._metric_track.get(max_key, None)
            if old_max is None or float_v > old_max:
                self._metric_track[max_key] = float_v
                if s.max:
                    self._consolidated_summary[max_key] = v
                    updated = True
                if best_key:
                    self._consolidated_summary[best_key] = v
                    updated = True
        if s.min or not goal_max and best_key:
            min_key = k + ".min"
            old_min = self._metric_track.get(min_key, None)
            if old_min is None or float_v < old_min:
                self._metric_track[min_key] = float_v
                if s.min:
                    self._consolidated_summary[min_key] = v
                    updated = True
                if best_key:
                    self._consolidated_summary[best_key] = v
                    updated = True
        if s.mean:
            tot_key = k + ".tot"
            num_key = k + ".num"
            avg_key = k + ".mean"
            tot = self._metric_track.get(tot_key, 0)
            num = self._metric_track.get(num_key, 0)
            tot += float_v
            num += 1
            self._metric_track[tot_key] = tot
            self._metric_track[num_key] = num
            self._consolidated_summary[avg_key] = tot / num
        return updated

    def _update_summary(self, history_dict):
        if not self._metric_defines:
            self._consolidated_summary.update(history_dict)
            return True
        updated = False
        for k, v in six.iteritems(history_dict):
            # TODO(jhr): handle nested metrics
            d = self._metric_defines.get(k, None)
            float_v = float(v)

            # Always store last metric (for now)
            last_key = k + ".last"
            old_last = self._metric_track.get(last_key, None)
            if old_last is None or float_v != old_last:
                self._metric_track[last_key] = float_v
                self._consolidated_summary[k] = v
                updated = True
            if not d:
                continue
            if not isinstance(v, numbers.Real):
                continue
            if d.summary:
                goal_max = None
                if d.goal:
                    goal_max = d.goal.type == d.goal.GoalType.MAXIMIZE
                if self._update_summary_metrics(
                    d.summary, k=k, v=v, float_v=float_v, goal_max=goal_max
                ):
                    updated = True
        return updated

    def _history_assign_step(self, record, history_dict):
        has_step = record.history.HasField("step")
        item = record.history.item.add()
        item.key = "_step"
        if has_step:
            step = record.history.step.num
            history_dict["_step"] = step
            item.value_json = json.dumps(step)
            self._step = step + 1
        else:
            history_dict["_step"] = self._step
            item.value_json = json.dumps(self._step)
            self._step += 1

    def _history_define_metric(
        self, hkey
    ):
        """check for hkey match in glob metrics, return defined metric."""
        return None

    def _history_update(self, record, history_dict):
        # if syncing an old run, we can skip this logic
        if history_dict.get("_step") is None:
            self._history_assign_step(record, history_dict)

        # Look for metric matches
        for hkey in history_dict:
            m = self._metric_defines.get(hkey)
            if not m:
                m = self._history_define_metric(hkey)
                if not m:
                    continue
            # if here, m is a defined metric

    def handle_history(self, record):
        history_dict = proto_util.dict_from_proto_list(record.history.item)
        self._history_update(record, history_dict)
        self._dispatch_record(record)
        self._save_history(record)

        updated = self._update_summary(history_dict)
        if updated:
            self._save_summary(self._consolidated_summary)

    def handle_summary(self, record):
        summary = record.summary

        for item in summary.update:
            if len(item.nested_key) > 0:
                # we use either key or nested_key -- not both
                assert item.key == ""
                key = tuple(item.nested_key)
            else:
                # no counter-assertion here, because technically
                # summary[""] is valid
                key = (item.key,)

            target = self._consolidated_summary

            # recurse down the dictionary structure:
            for prop in key[:-1]:
                target = target[prop]

            # use the last element of the key to write the leaf:
            target[key[-1]] = json.loads(item.value_json)

        for item in summary.remove:
            if len(item.nested_key) > 0:
                # we use either key or nested_key -- not both
                assert item.key == ""
                key = tuple(item.nested_key)
            else:
                # no counter-assertion here, because technically
                # summary[""] is valid
                key = (item.key,)

            target = self._consolidated_summary

            # recurse down the dictionary structure:
            for prop in key[:-1]:
                target = target[prop]

            # use the last element of the key to erase the leaf:
            del target[key[-1]]

        self._save_summary(self._consolidated_summary)

    def handle_exit(self, record):
        self._dispatch_record(record, always_send=True)

    def handle_final(self, record):
        self._dispatch_record(record, always_send=True)

    def handle_header(self, record):
        self._dispatch_record(record)

    def handle_footer(self, record):
        self._dispatch_record(record)

    def handle_request_check_version(self, record):
        self._dispatch_record(record)

    def handle_telemetry(self, record):
        self._dispatch_record(record)

    def handle_request_run_start(self, record):
        run_start = record.request.run_start
        assert run_start
        assert run_start.run

        if not self._settings._disable_stats:
            pid = os.getpid()
            self._system_stats = stats.SystemStats(pid=pid, interface=self._interface)
            self._system_stats.start()

        if not self._settings._disable_meta:
            run_meta = meta.Meta(settings=self._settings, interface=self._interface)
            run_meta.probe()
            run_meta.write()

        self._tb_watcher = tb_watcher.TBWatcher(
            self._settings, interface=self._interface, run_proto=run_start.run
        )

        if run_start.run.resumed:
            self._step = run_start.run.starting_step
        result = wandb_internal_pb2.Result(uuid=record.uuid)
        self._result_q.put(result)

    def handle_request_resume(self, record):
        if self._system_stats is not None:
            logger.info("starting system metrics thread")
            self._system_stats.start()

    def handle_request_pause(self, record):
        if self._system_stats is not None:
            logger.info("stopping system metrics thread")
            self._system_stats.shutdown()

    def handle_request_poll_exit(self, record):
        self._dispatch_record(record, always_send=True)

    def handle_request_status(self, record):
        self._dispatch_record(record)

    def handle_request_get_summary(self, record):
        result = wandb_internal_pb2.Result(uuid=record.uuid)
        for key, value in six.iteritems(self._consolidated_summary):
            item = wandb_internal_pb2.SummaryItem()
            item.key = key
            item.value_json = json.dumps(value)
            result.response.get_summary_response.item.append(item)
        self._result_q.put(result)

    def handle_tbrecord(self, record):
        logger.info("handling tbrecord: %s", record)
        if self._tb_watcher:
            tbrecord = record.tbrecord
            self._tb_watcher.add(tbrecord.log_dir, tbrecord.save, tbrecord.root_dir)
        self._dispatch_record(record)

    def _handle_defined_metric(self, record):
        metric = record.metric
        self._metric_defines.setdefault(
            metric.name, wandb_internal_pb2.MetricRecord()
        ).MergeFrom(metric)
        self._dispatch_record(record)

    def _handle_glob_metric(self, record):
        metric = record.metric
        self._metric_globs.setdefault(
            metric.glob_name, wandb_internal_pb2.MetricRecord()
        ).MergeFrom(metric)
        self._dispatch_record(record)

    def handle_metric(self, record):
        """Handle MetricRecord.

        Walkthrough of the life of a MetricRecord:

        Metric defined:
        - run.define_metric() parses arguments create wandb_metric.Metric
        - build MetricRecord publish to interface
        - handler (this function) keeps list of metrics published:
          - self._metric_defines: Fully defined metrics
          - self._metric_globs: metrics that have a wildcard
        - dispatch writer and sender thread
          - writer: records are saved to persistent store
          - sender: fully defined metrics get mapped into metadata for UI

        History logged:
        - handle_history
        - check if metric matches _metric_defines
        - if not, check if metric matches _metric_globs
        - if _metric globs match, generate defined metric and call _handle_metric

        Args:
            record (Record): Metric record to process
        """
        if record.metric.name:
            self._handle_defined_metric(record)
        elif record.metric.glob_name:
            self._handle_glob_metric(record)

    def handle_request_sampled_history(self, record):
        result = wandb_internal_pb2.Result(uuid=record.uuid)
        for key, sampled in six.iteritems(self._sampled_history):
            item = wandb_internal_pb2.SampledHistoryItem()
            item.key = key
            values = sampled.get()
            if all(isinstance(i, numbers.Integral) for i in values):
                item.values_int.extend(values)
            elif all(isinstance(i, numbers.Real) for i in values):
                item.values_float.extend(values)
            result.response.sampled_history_response.item.append(item)
        self._result_q.put(result)

    def handle_request_shutdown(self, record):
        # TODO(jhr): should we drain things and stop new requests from coming in?
        result = wandb_internal_pb2.Result(uuid=record.uuid)
        self._result_q.put(result)
        self._stopped.set()

    def finish(self):
        logger.info("shutting down handler")
        if self._tb_watcher:
            self._tb_watcher.finish()
