# Copyright (C) 2011-2012 Canonical Services Ltd
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

from collections import deque
import re
import time
import logging

from twisted.python import log

from txstatsd.metrics.metermetric import MeterMetricReporter


SPACES = re.compile(b"\s+")
SLASHES = re.compile(b"\/+")
NON_ALNUM = re.compile(b"[^a-zA-Z_\-0-9\.]")
RATE = re.compile(b"^@([\d\.]+)")


def normalize_key(key):
    """
    Normalize a key that might contain spaces, forward-slashes and other
    special characters into something that is acceptable by graphite.
    """
    key = SPACES.sub("_", key)
    key = SLASHES.sub("-", key)
    key = NON_ALNUM.sub("", key)
    return key


class BaseMessageProcessor(object):

    def process(self, message):
        """
        """
        if b":" not in message:
            return self.fail(message)

        key, data = message.strip().split(b":", 1)
        if b"|" not in data:
            return self.fail(message)

        fields = data.split(b"|")
        if len(fields) < 2 or len(fields) > 3:
            return self.fail(message)

        key = normalize_key(key)
        metric_type = fields[1]
        return self.process_message(message, metric_type, key, fields)

    def rebuild_message(self, metric_type, key, fields):
        return key + b":" + b"|".join(fields)

    def fail(self, message):
        """Log and discard malformed message."""
        log.msg("Bad line: %r" % message, logLevel=logging.DEBUG)


class MessageProcessor(BaseMessageProcessor):
    """
    This C{MessageProcessor} produces StatsD-compliant messages
    for publishing to a Graphite server.
    Metrics behaviour that varies from StatsD should be placed in
    some specialised C{MessageProcessor} (see L{ConfigurableMessageProcessor
    <txstatsd.server.configurableprocessor.ConfigurableMessageProcessor>}).
    """

    def __init__(self, time_function=time.time, plugins=None):
        self.time_function = time_function

        self.stats_prefix = b"stats."
        self.internal_metrics_prefix = b"statsd."
        self.count_prefix = b"stats_counts."
        self.timer_prefix = self.stats_prefix + b"timers."
        self.gauge_prefix = self.stats_prefix + b"gauge."

        self.process_timings = {}
        self.by_type = {}
        self.last_flush_duration = 0
        self.last_process_duration = 0

        self.timer_metrics = {}
        self.counter_metrics = {}
        self.gauge_metrics = deque()
        self.meter_metrics = {}

        self.plugins = {}
        self.plugin_metrics = {}

        if plugins is not None:
            for plugin in plugins:
                self.plugins[plugin.metric_type] = plugin

    def get_metric_names(self):
        """Return the names of all seen metrics."""
        metrics = set()
        metrics.update(list(self.timer_metrics.keys()))
        metrics.update(list(self.counter_metrics.keys()))
        metrics.update(v for k, v in self.gauge_metrics)
        metrics.update(list(self.meter_metrics.keys()))
        metrics.update(list(self.plugin_metrics.keys()))
        return list(metrics)

    def process_message(self, message, metric_type, key, fields):
        """
        Process a single entry, adding it to either C{counters}, C{timers},
        or C{gauge_metrics} depending on which kind of message it is.
        """
        start = self.time_function()
        if metric_type == b"c":
            self.process_counter_metric(key, fields, message)
        elif metric_type == b"ms":
            self.process_timer_metric(key, fields[0], message)
        elif metric_type == b"g":
            self.process_gauge_metric(key, fields[0], message)
        elif metric_type == b"m":
            self.process_meter_metric(key, fields[0], message)
        elif metric_type in self.plugins:
            self.process_plugin_metric(metric_type, key, fields, message)
        else:
            return self.fail(message)
        self.process_timings.setdefault(metric_type, 0)
        self.process_timings[metric_type] += self.time_function() - start
        self.by_type.setdefault(metric_type, 0)
        self.by_type[metric_type] += 1

    def get_message_prefix(self, kind):
        return b"stats." + kind

    def process_plugin_metric(self, metric_type, key, items, message):
        if not key in self.plugin_metrics:
            factory = self.plugins[metric_type]
            metric = factory.build_metric(
                self.get_message_prefix(factory.name),
                name=key, wall_time_func=self.time_function)
            self.plugin_metrics[key] = metric
        self.plugin_metrics[key].process(items)

    def process_timer_metric(self, key, duration, message):
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            return self.fail(message)

        self.compose_timer_metric(key, duration)

    def compose_timer_metric(self, key, duration):
        if key not in self.timer_metrics:
            self.timer_metrics[key] = []
        self.timer_metrics[key].append(duration)

    def process_counter_metric(self, key, composite, message):
        try:
            value = float(composite[0])
        except (TypeError, ValueError):
            return self.fail(message)
        rate = 1
        if len(composite) == 3:
            match = RATE.match(composite[2])
            if match is None:
                return self.fail(message)
            rate = match.group(1)

        self.compose_counter_metric(key, value, rate)

    def compose_counter_metric(self, key, value, rate):
        if key not in self.counter_metrics:
            self.counter_metrics[key] = 0
        self.counter_metrics[key] += value * (1 / float(rate))

    def process_gauge_metric(self, key, composite, message):
        values = composite.split(b":")
        if not len(values) == 1:
            return self.fail(message)

        try:
            value = float(values[0])
        except (TypeError, ValueError):
            self.fail(message)

        self.compose_gauge_metric(key, value)

    def compose_gauge_metric(self, key, value):
        metric = [value, key]
        self.gauge_metrics.append(metric)

    def process_meter_metric(self, key, composite, message):
        print(composite)
        values = composite.split(b":")
        if not len(values) == 1:
            return self.fail(message)

        try:
            value = float(values[0])
        except (TypeError, ValueError):
            self.fail(message)

        self.compose_meter_metric(key, value)

    def compose_meter_metric(self, key, value):
        if not key in self.meter_metrics:
            metric = MeterMetricReporter(key, self.time_function,
                                         prefix=b"stats.meter")
            self.meter_metrics[key] = metric
        self.meter_metrics[key].mark(value)

    def flush(self, interval=10000, percent=90):
        """
        Flush all queued stats, computing a normalized count based on
        C{interval} and mean timings based on C{threshold}.
        """
        per_metric = {}
        num_stats = 0
        interval = interval / 1000
        timestamp = int(self.time_function())

        start = self.time_function()
        events = 0
        for metrics in self.flush_counter_metrics(interval, timestamp):
            for metric in metrics:
                yield metric
            events += 1
        duration = self.time_function() - start
        num_stats += events
        per_metric["counter"] = (events, duration)

        start = self.time_function()
        events = 0
        for metrics in self.flush_timer_metrics(percent, timestamp):
            for metric in metrics:
                yield metric
            events += 1
        duration = self.time_function() - start
        num_stats += events
        per_metric["timer"] = (events, duration)

        start = self.time_function()
        events = 0
        for metrics in self.flush_gauge_metrics(timestamp):
            for metric in metrics:
                yield metric
            events += 1
        duration = self.time_function() - start
        num_stats += events
        per_metric["gauge"] = (events, duration)

        start = self.time_function()
        events = 0
        for metrics in self.flush_meter_metrics(timestamp):
            for metric in metrics:
                yield metric
            events += 1
        duration = self.time_function() - start
        num_stats += events
        per_metric["meter"] = (events, duration)

        start = self.time_function()
        events = 0
        for metrics in self.flush_plugin_metrics(interval, timestamp):
            for metric in metrics:
                yield metric
            events += 1
        duration = self.time_function() - start
        num_stats += events
        per_metric["plugin"] = (events, duration)

        for metrics in self.flush_metrics_summary(num_stats, per_metric,
                                                  timestamp):
            for metric in metrics:
                yield metric

    def flush_counter_metrics(self, interval, timestamp):
        for key, count in list(self.counter_metrics.items()):
            self.counter_metrics[key] = 0

            value = int(count / interval)
            yield ((self.stats_prefix + key, value, timestamp),
                   (self.count_prefix + key, count, timestamp))

    def flush_timer_metrics(self, percent, timestamp):
        threshold_value = ((100 - percent) / 100.0)
        for key, timers in list(self.timer_metrics.items()):
            count = len(timers)
            if count > 0:
                self.timer_metrics[key] = []

                timers.sort()
                lower = timers[0]
                upper = timers[-1]
                count = len(timers)

                mean = lower
                threshold_upper = upper

                if count > 1:
                    index = count - int(round(threshold_value * count))
                    timers = timers[:index]
                    threshold_upper = timers[-1]
                    mean = int(sum(timers) / index)

                items = {b".mean": mean,
                         b".upper": upper,
                         b".upper_%d" % percent: threshold_upper,
                         b".lower": lower,
                         b".count": count}
                yield sorted((self.timer_prefix + key + item, value, timestamp)
                             for item, value in list(items.items()))

    def flush_gauge_metrics(self, timestamp):
        for metric in self.gauge_metrics:
            value = metric[0]
            key = metric[1]

            yield ((self.gauge_prefix + key + b".value", value, timestamp),)

    def flush_meter_metrics(self, timestamp):
        for metric in list(self.meter_metrics.values()):
            messages = metric.report(timestamp)
            yield messages

    def flush_plugin_metrics(self, interval, timestamp):
        for metric in list(self.plugin_metrics.values()):
            messages = metric.flush(interval, timestamp)
            yield messages

    def flush_metrics_summary(self, num_stats, per_metric, timestamp):
        yield ((self.internal_metrics_prefix + b"numStats",
                num_stats, timestamp),)

        self.last_flush_duration = 0
        for name, (value, duration) in list(per_metric.items()):
            if type(name) != bytes:
                name = name.encode('utf-8')
            yield ((self.internal_metrics_prefix +
                    b"flush.%s.count" % name,
                    value, timestamp),
                   (self.internal_metrics_prefix +
                    b"flush.%s.duration" % name,
                    duration * 1000, timestamp))
            log.msg("Flushed %d %s metrics in %.6f" %
                    (value, name, duration))
            self.last_flush_duration += duration

        self.last_process_duration = 0
        for metric_type, duration in list(self.process_timings.items()):
            yield ((self.internal_metrics_prefix +
                    b"receive.%s.count" %
                    metric_type, self.by_type[metric_type], timestamp),
                   (self.internal_metrics_prefix +
                    b"receive.%s.duration" %
                    metric_type, duration * 1000, timestamp))
            log.msg("Processing %d %s metrics took %.6f" %
                    (self.by_type[metric_type], metric_type, duration))
            self.last_process_duration += duration

        self.process_timings.clear()
        self.by_type.clear()
