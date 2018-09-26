# BEGIN_COPYRIGHT
#
# Copyright 2009-2018 CRS4.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
# END_COPYRIGHT

# ---
import logging
logging.basicConfig(level=logging.DEBUG)
# ---

import os
import socket
from collections import namedtuple
from itertools import groupby
from operator import itemgetter
try:
    from cPickle import dumps, loads, HIGHEST_PROTOCOL
except ImportError:
    from pickle import dumps, loads, HIGHEST_PROTOCOL

import pydoop.sercore as sercore
from .api import JobConf
from .string_utils import create_digest

# py2 compat
try:
    as_text = unicode
except NameError:
    as_text = str

PROTOCOL_VERSION = 0

# We can use an enum.IntEnum after dropping Python2 compatibility
START = 0
SET_JOB_CONF = 1
SET_INPUT_TYPES = 2
RUN_MAP = 3
MAP_ITEM = 4
RUN_REDUCE = 5
REDUCE_KEY = 6
REDUCE_VALUE = 7
CLOSE = 8
ABORT = 9
AUTHENTICATION_REQ = 10
OUTPUT = 50
PARTITIONED_OUTPUT = 51
STATUS = 52
PROGRESS = 53
DONE = 54
REGISTER_COUNTER = 55
INCREMENT_COUNTER = 56
AUTHENTICATION_RESP = 57

CMD_REPR = {
    START: "START",
    SET_JOB_CONF: "SET_JOB_CONF",
    SET_INPUT_TYPES: "SET_INPUT_TYPES",
    RUN_MAP: "RUN_MAP",
    MAP_ITEM: "MAP_ITEM",
    RUN_REDUCE: "RUN_REDUCE",
    REDUCE_KEY: "REDUCE_KEY",
    REDUCE_VALUE: "REDUCE_VALUE",
    CLOSE: "CLOSE",
    ABORT: "ABORT",
    AUTHENTICATION_REQ: "AUTHENTICATION_REQ",
    OUTPUT: "OUTPUT",
    PARTITIONED_OUTPUT: "PARTITIONED_OUTPUT",
    STATUS: "STATUS",
    PROGRESS: "PROGRESS",
    DONE: "DONE",
    REGISTER_COUNTER: "REGISTER_COUNTER",
    INCREMENT_COUNTER: "INCREMENT_COUNTER",
    AUTHENTICATION_RESP: "AUTHENTICATION_RESP",
}

IS_JAVA_RW = "mapreduce.pipes.isjavarecordwriter"


class InputSplit(object):
    """\
    Represents a subset of the input data assigned to a single map task.

    ``InputSplit`` objects are created by the framework and made available
    to the user application via the ``input_split`` context attribute.
    """
    pass


class FileSplit(InputSplit,
                namedtuple("FileSplit", "filename, offset, length")):
    """\
    A subset (described by offset and length) of an input file.
    """
    @classmethod
    def fromstream(cls, stream):
        stream.read_vint()  # whole serialized split length
        filename = stream.read_string()
        offset = stream.read_long_writable()
        length = stream.read_long_writable()
        return cls(filename, offset, length)


def _get_LongWritable(downlink):
    assert downlink.stream.read_vint() == 8
    return downlink.stream.read_long_writable()


def _get_Text(downlink):
    return downlink.stream.read_string()


DESERIALIZERS = {
    "org.apache.hadoop.io.LongWritable": _get_LongWritable,
    "org.apache.hadoop.io.Text": _get_Text,
}


def _get_pickled(downlink):
    return loads(downlink.stream.read_bytes())


def _emit_pickled(context, key, value):
    context.uplink.output(dumps(key, HIGHEST_PROTOCOL),
                          dumps(value, HIGHEST_PROTOCOL))


def get_password():
    try:
        pass_fn = os.environ["hadoop.pipes.shared.secret.location"]
    except KeyError:
        return None
    with open(pass_fn, "rb") as f:
        return f.read()


class BinaryProtocol(object):

    def __init__(self, istream, context, ostream, **kwargs):
        self.stream = istream
        self.context = context
        self.uplink = CommandWriter(ostream)
        self.raw_split = kwargs.get("raw_split", False)
        self.raw_k = kwargs.get("raw_keys", False)
        self.raw_v = kwargs.get("raw_values", False)
        self.uplink.private_encoding = kwargs.get("private_encoding", True)
        self.uplink.auto_serialize = kwargs.get("auto_serialize", True)
        self.password = get_password()
        self.auth_done = False

    def close(self):
        self.stream.close()
        self.uplink.close()

    def read_job_conf(self):
        n = self.stream.read_vint()
        if n & 1:
            raise RuntimeError("number of items is not even")
        t = self.stream.read_tuple(n * 's')
        return JobConf(t[i: i + 2] for i in range(0, n, 2))

    def verify_digest(self, digest, challenge):
        if self.password is not None:
            if create_digest(self.password, challenge) != digest:
                raise RuntimeError("server failed to authenticate")
            response_digest = create_digest(self.password, digest)
            self.uplink.authenticate(response_digest)
        # self.password is None: assume reading from cmd file
        self.auth_done = True

    def setup_record_writer(self, piped_output):
        writer = self.context.create_record_writer()
        if writer and piped_output:
            raise RuntimeError("record writer defined when not needed")
        if not writer and not piped_output:
            raise RuntimeError("record writer not defined")

    def get_k(self):
        return self.stream.read_bytes()

    def get_v(self):
        return self.stream.read_bytes()

    def __next__(self):
        cmd = self.stream.read_vint()
        if cmd != AUTHENTICATION_REQ and not self.auth_done:
            raise RuntimeError("%d received before authentication" % cmd)
        if cmd == AUTHENTICATION_REQ:
            digest, challenge = self.stream.read_tuple('bb')
            logging.debug("%s: %r, %r", CMD_REPR[cmd], digest, challenge)
            self.verify_digest(digest, challenge)
        elif cmd == START:
            v = self.stream.read_vint()
            logging.debug("%s: %d", CMD_REPR[cmd], v)
            if (v != PROTOCOL_VERSION):
                raise RuntimeError("Unknown protocol id: %d" % v)
        elif cmd == SET_JOB_CONF:
            self.context.job_conf = self.read_job_conf()
            logging.debug(
                "%s: %r", CMD_REPR[cmd],
                {k: v for k, v in list(self.context.job_conf.items())[:3]}
            )
        elif cmd == RUN_MAP:
            if self.raw_split:
                split, nred, piped_input = self.stream.read_tuple('bii')
            else:
                # TODO: support opaque splits
                split = FileSplit.fromstream(self.stream)
                nred, piped_input = self.stream.read_tuple("ii")
            logging.debug("%s: %r, %r, %r",
                          CMD_REPR[cmd], split, nred, piped_input)
            self.context.input_split = split
            reader = self.context.create_record_reader()
            if reader and piped_input:
                raise RuntimeError("record reader defined when not needed")
            if not reader and not piped_input:
                raise RuntimeError("record reader not defined")
            if nred < 1:  # map-only job
                self.uplink.private_encoding = False
                piped_output = self.context.job_conf.get_bool(IS_JAVA_RW)
                self.setup_record_writer(piped_output)
            self.context.nred = nred
            self.context.create_mapper()
            self.context.create_partitioner()
            if reader:
                # TODO: progress
                for self.context.key, self.context.value in reader:
                    self.context.mapper.map(self.context)
                # no more commands from upstream, not even CLOSE
                try:
                    self.context.close()
                finally:
                    raise StopIteration
        elif cmd == SET_INPUT_TYPES:
            key_type, value_type = self.stream.read_tuple('ss')
            logging.debug("%s: %r, %r", CMD_REPR[cmd], key_type, value_type)
            if not self.raw_k:
                d = DESERIALIZERS.get(key_type)
                if d is not None:
                    self.__class__.get_k = d
            if not self.raw_v:
                d = DESERIALIZERS.get(value_type)
                if d is not None:
                    self.__class__.get_v = d
        elif cmd == MAP_ITEM:
            self.context.key = self.get_k()
            self.context.value = self.get_v()
            logging.debug("%s: %r, %r",
                          CMD_REPR[cmd], self.context.key, self.context.value)
            self.context.mapper.map(self.context)
        elif cmd == RUN_REDUCE:
            part, piped_output = self.stream.read_tuple('ii')
            # for some reason, part is always 0
            logging.debug("%s: %r, %r", CMD_REPR[cmd], part, piped_output)
            self.context.create_reducer()
            self.setup_record_writer(piped_output)
            if self.uplink.private_encoding:
                self.__class__.get_k = _get_pickled
                self.__class__.get_v = _get_pickled
            for cmd, subs in groupby(self, itemgetter(0)):
                logging.debug("  GOT: %r, %r", CMD_REPR[cmd], subs)
                if cmd == REDUCE_KEY:
                    _, self.context.key = next(subs)
                if cmd == REDUCE_VALUE:
                    self.context.values = (v for _, v in subs)
                    self.context.reducer.reduce(self.context)
                if cmd == CLOSE:
                    try:
                        self.context.close()
                    finally:
                        raise StopIteration
        elif cmd == REDUCE_KEY:
            k = self.get_k()
            logging.debug("%s: %r", CMD_REPR[cmd], k)
            return cmd, k  # pass on to RUN_REDUCE iterator
        elif cmd == REDUCE_VALUE:
            v = self.get_v()
            logging.debug("%s: %r", CMD_REPR[cmd], v)
            return cmd, v  # pass on to RUN_REDUCE iterator
        elif cmd == ABORT:
            raise RuntimeError("received ABORT command")
        elif cmd == CLOSE:
            logging.debug(CMD_REPR[cmd])
            if self.context.mapper:
                try:
                    self.context.close()
                finally:
                    raise StopIteration
            else:
                return cmd, None  # pass on to RUN_REDUCE iterator
        else:
            raise RuntimeError("unknown command: %d" % cmd)

    def __iter__(self):
        return self

    # py2 compat
    def next(self):
        return self.__next__()


# rename to BinaryUpwardProtocol?
class CommandWriter(object):

    def __init__(self, stream):
        self.stream = stream

    def close(self):
        self.stream.flush()
        self.stream.close()

    def authenticate(self, response_digest):
        logging.debug("    Out: %r", (AUTHENTICATION_RESP, response_digest))
        self.stream.write_tuple("ib", (AUTHENTICATION_RESP, response_digest))
        self.stream.flush()

    def output(self, k, v):
        logging.debug("    Out: %r", (OUTPUT, k, v))
        self.stream.write_tuple("ibb", (OUTPUT, k, v))
        self.stream.flush()

    def partitioned_output(self, part, k, v):
        logging.debug("    Out: %r", (PARTITIONED_OUTPUT, part, k, v))
        self.stream.write_tuple("iibb", (PARTITIONED_OUTPUT, part, k, v))
        self.stream.flush()

    def status(self, msg):
        logging.debug("    Out: %r", (STATUS, msg))
        self.stream.write_tuple("is", (STATUS, msg))
        self.stream.flush()

    def progress(self, p):
        logging.debug("    Out: %r", (PROGRESS, p))
        self.stream.write_tuple("if", (PROGRESS, p))
        self.stream.flush()

    def done(self):
        logging.debug("    Out: %r", DONE)
        self.stream.write_vint(DONE)
        self.stream.flush()

    def register_counter(self, id, group, name):
        logging.debug("    Out: %r", (REGISTER_COUNTER, id, group, name))
        self.stream.write_tuple("iiss", (REGISTER_COUNTER, id, group, name))
        self.stream.flush()

    def increment_counter(self, id, amount):
        logging.debug("    Out: %r", (INCREMENT_COUNTER, id, amount))
        self.stream.write_tuple("iil", (INCREMENT_COUNTER, id, amount))
        self.stream.flush()


class Connection(object):

    def __init__(self):
        self.downlink = None

    def close(self):
        if self.downlink:
            self.downlink.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class NetworkConnection(Connection):

    def __init__(self, context, host, port, **kwargs):
        super(NetworkConnection, self).__init__()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((host, port))
        istream = sercore.FileInStream(self.socket)
        ostream = sercore.FileOutStream(self.socket)
        self.downlink = BinaryProtocol(istream, context, ostream, **kwargs)

    def close(self):
        super(NetworkConnection, self).close()
        self.socket.close()


class FileConnection(Connection):

    def __init__(self, context, in_fn, out_fn, **kwargs):
        super(FileConnection, self).__init__()
        istream = sercore.FileInStream(in_fn)
        ostream = sercore.FileOutStream(out_fn)
        self.downlink = BinaryProtocol(istream, context, ostream, **kwargs)


def get_connection(context, **kwargs):
    port = os.getenv("mapreduce.pipes.command.port")
    if port:
        return NetworkConnection(context, "localhost", int(port), **kwargs)
    in_fn = os.getenv("mapreduce.pipes.commandfile")
    if in_fn:
        out_fn = "%s.out" % in_fn
        return FileConnection(context, in_fn, out_fn, **kwargs)
    raise NotImplementedError  # TBD: text protocol (all kwargs ignored?)


class Context(object):

    JOB_OUTPUT_DIR = "mapreduce.output.fileoutputformat.outputdir"
    TASK_OUTPUT_DIR = "mapreduce.task.output.dir"
    TASK_PARTITION = "mapreduce.task.partition"

    def __init__(self, factory):
        self.factory = factory
        self.downlink = None
        self.uplink = None
        self.job_conf = {}
        self.key = None
        self.value = None
        self.mapper = None
        self.partitioner = None
        self.record_reader = None
        self.record_writer = None
        self.reducer = None
        self.nred = None

    def create_mapper(self):
        self.mapper = self.factory.create_mapper(self)
        return self.mapper

    def create_partitioner(self):
        self.partitioner = self.factory.create_partitioner(self)
        return self.partitioner

    def create_record_reader(self):
        self.record_reader = self.factory.create_record_reader(self)
        return self.record_reader

    def create_record_writer(self):
        self.record_writer = self.factory.create_record_writer(self)
        return self.record_writer

    def create_reducer(self):
        self.reducer = self.factory.create_reducer(self)
        return self.reducer

    def emit(self, key, value):
        # TODO: send progress
        if self.record_writer:
            self.record_writer.emit(key, value)
            return
        if self.mapper and self.uplink.private_encoding:
            key = dumps(key, HIGHEST_PROTOCOL)
            value = dumps(value, HIGHEST_PROTOCOL)
        elif self.uplink.auto_serialize:
            # optimize by writing directly as "ss"?
            key = as_text(key).encode("utf-8")
            value = as_text(value).encode("utf-8")
        if self.partitioner:
            part = self.partitioner.partition(key, self.nred)
            self.uplink.partitioned_output(part, key, value)
        else:
            self.uplink.output(key, value)

    def close(self):
        # do *not* call uplink.done while user components are still active
        try:
            if self.mapper:
                self.mapper.close()
            if self.record_reader:
                self.record_reader.close()
            if self.record_writer:
                self.record_writer.close()
            if self.reducer:
                self.reducer.close()
        finally:
            self.uplink.done()

    def get_output_dir(self):
        return self.job_conf[self.JOB_OUTPUT_DIR]

    def get_work_path(self):
        try:
            return self.job_conf[self.TASK_OUTPUT_DIR]
        except KeyError:
            raise RuntimeError("%r not set" % (self.TASK_OUTPUT_DIR,))

    def get_task_partition(self):
        return self.job_conf.get_int(self.TASK_PARTITION)

    def get_default_work_file(self, extension=""):
        partition = self.get_task_partition()
        if partition is None:
            raise RuntimeError("%r not set" % (self.TASK_PARTITION,))
        base = self.job_conf.get("mapreduce.output.basename", "part")
        task_type = "m" if self.mapper else "r"
        return "%s/%s-%s-%05d%s" % (
            self.get_work_path(), base, task_type, partition, extension
        )


# class Factory(api.Factory):
class Factory(object):

    def __init__(self, mapper_class,
                 reducer_class=None,
                 combiner_class=None,
                 partitioner_class=None,
                 record_writer_class=None,
                 record_reader_class=None):
        self.mclass = mapper_class
        self.rclass = reducer_class
        self.cclass = combiner_class
        self.pclass = partitioner_class
        self.rwclass = record_writer_class
        self.rrclass = record_reader_class

    def create_mapper(self, context):
        return self.mclass(context)

    def create_reducer(self, context):
        return None if not self.rclass else self.rclass(context)

    def create_combiner(self, context):
        return None if not self.cclass else self.cclass(context)

    def create_partitioner(self, context):
        return None if not self.pclass else self.pclass(context)

    def create_record_reader(self, context):
        return None if not self.rrclass else self.rrclass(context)

    def create_record_writer(self, context):
        return None if not self.rwclass else self.rwclass(context)


def run_task(factory, **kwargs):
    """\
    Run a MapReduce task.

    Available keyword arguments:

    * ``raw_keys`` (default: :obj:`False`): pass map input keys to context
      as byte strings (ignore any type information)
    * ``raw_values`` (default: :obj:`False`): pass map input values to context
      as byte strings (ignore any type information)
    * ``raw_split`` (default: :obj:`False`): pass input split to context as a
      byte string (by default, a :class:`FileSplit` is assumed)
    * ``private_encoding`` (default: :obj:`True`): automatically serialize map
      output k/v and deserialize reduce input k/v (pickle)
    * ``auto_serialize`` (default: :obj:`True`): automatically serialize reduce
      output k/v (call str/unicode then encode as utf-8)
    """
    context = Context(factory)
    with get_connection(context, **kwargs) as connection:
        context.downlink = connection.downlink
        context.uplink = connection.downlink.uplink
        for _ in connection.downlink:
            pass
