import struct
import threading
import logging
import socket
import select
import ssl
from google.protobuf.internal import encoder
from google.protobuf.internal import decoder
try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse

from linstor.proto.MsgHeader_pb2 import MsgHeader
from linstor.proto.MsgApiVersion_pb2 import MsgApiVersion
from linstor.proto.MsgApiCallResponse_pb2 import MsgApiCallResponse
from linstor.proto.MsgCrtNode_pb2 import MsgCrtNode
from linstor.proto.MsgModNode_pb2 import MsgModNode
from linstor.proto.MsgDelNode_pb2 import MsgDelNode
from linstor.proto.MsgCrtNetInterface_pb2 import MsgCrtNetInterface
from linstor.proto.MsgModNetInterface_pb2 import MsgModNetInterface
from linstor.proto.MsgDelNetInterface_pb2 import MsgDelNetInterface
from linstor.proto.MsgLstNode_pb2 import MsgLstNode
from linstor.proto.MsgLstStorPoolDfn_pb2 import MsgLstStorPoolDfn
from linstor.proto.MsgLstStorPool_pb2 import MsgLstStorPool
from linstor.proto.MsgLstRscDfn_pb2 import MsgLstRscDfn
from linstor.proto.MsgLstRsc_pb2 import MsgLstRsc
import linstor.sharedconsts as apiconsts
import linstor.utils as utils
import linstor.consts as consts


logging.basicConfig(level=logging.WARNING)


class AtomicInt(object):
    def __init__(self, init=0):
        self.val = init
        self.lock = threading.RLock()

    def get_and_inc(self):
        with self.lock:
            val = self.val
            self.val += 1
        return val


class LinstorError(Exception):
    """
    Linstor basic error class with a message
    """
    def __init__(self, msg):
        self._msg = msg

    def __str__(self):
        return "Error: {msg}".format(msg=self._msg)

    def __repr__(self):
        return "LinstorError('{msg}')".format(msg=self._msg)


class ApiCallResponse(object):
    def __init__(self, proto_response):
        self._proto_msg = proto_response  # type: MsgApiCallResponse

    @classmethod
    def from_json(cls, json_data):
        apiresp = MsgApiCallResponse()
        apiresp.ret_code = json_data["ret_code"]
        if "message_format" in json_data:
            apiresp.message_format = json_data["message_format"]
        if "details_format" in json_data:
            apiresp.details_format = json_data["details_format"]

        return ApiCallResponse(apiresp)

    def is_error(self):
        return True if self.ret_code & apiconsts.MASK_ERROR == apiconsts.MASK_ERROR else False

    def is_warning(self):
        return True if self.ret_code & apiconsts.MASK_WARN == apiconsts.MASK_WARN else False

    def is_info(self):
        return True if self.ret_code & apiconsts.MASK_INFO == apiconsts.MASK_INFO else False

    def is_success(self):
        return not self.is_error() and not self.is_warning() and not self.is_info()

    @property
    def ret_code(self):
        return self._proto_msg.ret_code

    @property
    def proto_msg(self):
        return self._proto_msg

    def __repr__(self):
        return "ApiCallResponse({retcode}, {msg})".format(retcode=self.ret_code, msg=self.proto_msg.message_format)


class LinstorNetClient(threading.Thread):
    IO_SIZE = 4096

    def __init__(self, timeout=20):
        super(LinstorNetClient, self).__init__()
        self._socket = None  # type: socket.socket
        self._timeout = timeout
        self._slock = threading.RLock()
        self._cv_sock = threading.Condition(self._slock)
        self._logger = logging.getLogger('LinstorNetClient')
        self._replies = {}
        self._api_version = None
        self._cur_msg_id = AtomicInt(1)

    def __del__(self):
        self.disconnect()

    @classmethod
    def _split_proto_msgs(cls, payload):
        """
        Splits a linstor payload into each raw proto buf message
        :param bytes payload: payload data
        :return list: list of raw proto buf messages
        """
        # split payload, just a list of pbs, the receiver has to deal with them
        pb_msgs = []
        n = 0
        while n < len(payload):
            msg_len, new_pos = decoder._DecodeVarint32(payload, n)
            n = new_pos
            msg_buf = payload[n:n + msg_len]
            n += msg_len
            pb_msgs.append(msg_buf)
        return pb_msgs

    @classmethod
    def _parse_proto_msgs(cls, type_tuple, data):
        """
        Parses a list of proto buf messages into their protobuf and/or wrapper classes,
        defined in the type_tuple.
        :param type_tuple: first item specifies the protobuf message, second item is a wrapper class or None
        :param list data: a list of raw protobuf message data
        :return: A list with protobuf or wrapper classes from the data
        """
        msg_resps = []
        msg_type = type_tuple[0]
        wrapper_type = type_tuple[1]
        for msg in data:
            resp = msg_type()
            resp.ParseFromString(msg)
            if wrapper_type:
                msg_resps.append(wrapper_type(resp))
            else:
                msg_resps.append(resp)
        return msg_resps

    @classmethod
    def _parse_proto_msg(cls, msg_type, data):
        msg = msg_type()
        msg.ParseFromString(data)
        return msg

    def _parse_api_version(self, data):
        msg = self._parse_proto_msg(MsgApiVersion, data)
        if self._api_version is None:
            self._api_version = msg.version
        else:
            self._logger.warning("API version message already received.")

    @classmethod
    def _parse_payload_length(cls, header):
        """
        Parses the payload length from a linstor header
        :param bytes header: 16 bytes header data
        :return: Length of the payload
        """
        struct_format = "!xxxxIxxxxxxxx"
        assert(struct.calcsize(struct_format) == len(header))
        exp_pkg_len, = struct.unpack(struct_format, header)
        return exp_pkg_len

    def _read_api_version_blocking(self):
        """
        Receives a api version message with blocking reads from the _socket and parses/checks it.
        :return: True
        """
        api_msg_data = self._socket.recv(self.IO_SIZE)
        while len(api_msg_data) < 16:
            api_msg_data += self._socket.recv(self.IO_SIZE)

        pkg_len = self._parse_payload_length(api_msg_data[:16])

        while len(api_msg_data) < pkg_len + 16:
            api_msg_data += self._socket.recv(self.IO_SIZE)

        msgs = self._split_proto_msgs(api_msg_data[16:])
        assert (len(msgs) > 0)
        hdr = self._parse_proto_msg(MsgHeader, msgs[0])

        assert(hdr.api_call == apiconsts.API_VERSION)
        self._parse_api_version(msgs[1])
        return True

    def connect(self, server):
        """
        Connects to the given server.
        The url has to be given in the linstor uri scheme. either linstor:// or linstor+ssl://
        :param str server: uri to the server
        :return: True if connected, else raises an LinstorError
        :raise LinstorError: if connection fails.
        """
        self._logger.debug("connecting to " + server)
        try:
            url = urlparse(server)

            if not url.scheme.startswith('linstor'):
                raise LinstorError("Unknown uri scheme '{sc}' in '{uri}'.".format(sc=url.scheme, uri=server))

            host, port = utils.parse_host(url.netloc)
            if not port:
                port = apiconsts.DFLT_CTRL_PORT_SSL if url.scheme == 'linstor+ssl' else apiconsts.DFLT_CTRL_PORT_PLAIN
            self._socket = socket.create_connection((host, port), timeout=self._timeout)

            # check if ssl
            if url.scheme == 'linstor+ssl':
                self._socket = ssl.wrap_socket(self._socket)
            self._socket.settimeout(self._timeout)

            # read api version
            self._read_api_version_blocking()

            self._socket.setblocking(0)
            self._logger.debug("connected to " + server)
            return True
        except socket.error as err:
            self._socket = None
            raise LinstorError("Error connecting: " + str(err))
            #sys.stderr.write("{ip} -> {msg}\n".format(ip=server, msg=str(err)))

    def disconnect(self):
        with self._slock:
            if self._socket:
                self._logger.debug("disconnecting")
                self._socket.close()
                self._socket = None

    def run(self):
        package = bytes()  # current package data
        exp_pkg_len = 0  # expected package length

        while self._socket:
            rds, wds, eds = select.select([self._socket], [], [self._socket], 2)

            self._logger.debug("select exit with:" + ",".join([str(rds), str(wds), str(eds)]))
            for sock in rds:
                with self._slock:
                    if self._socket is None:  # socket was closed
                        break

                    read = self._socket.recv(4096)
                    package += read
                    pkg_len = len(package)
                    self._logger.debug("pkg_len: " + str(pkg_len))
                    if pkg_len > 15 and exp_pkg_len == 0:  # header is 16 bytes
                        exp_pkg_len = self._parse_payload_length(package[:16])

                    self._logger.debug("exp_pkg_len: " + str(exp_pkg_len))
                    if exp_pkg_len and pkg_len == (exp_pkg_len + 16):  # check if we received the full data pkg
                        msgs = self._split_proto_msgs(package[16:])
                        assert (len(msgs) > 0)  # we should have at least a header message

                        # reset state variables
                        package = bytes()
                        exp_pkg_len = 0

                        hdr = self._parse_proto_msg(MsgHeader, msgs[0])  # parse header
                        self._logger.debug(str(hdr))

                        reply_map = {
                            apiconsts.API_REPLY: (MsgApiCallResponse, ApiCallResponse),
                            apiconsts.API_LST_STOR_POOL_DFN: (MsgLstStorPoolDfn, None),
                            apiconsts.API_LST_STOR_POOL: (MsgLstStorPool, None),
                            apiconsts.API_LST_NODE: (MsgLstNode, None),
                            apiconsts.API_LST_RSC_DFN: (MsgLstRscDfn, None),
                            apiconsts.API_LST_RSC: (MsgLstRsc, None)
                        }

                        if hdr.api_call == apiconsts.API_VERSION:  # this shouldn't happen
                            self._parse_api_version(msgs[1])
                            assert False  # this should not be sent a second time
                        elif hdr.api_call in reply_map.keys():
                            # parse other message according to the reply_map and add them to the self._replies
                            replies = self._parse_proto_msgs(reply_map[hdr.api_call], msgs[1:])
                            with self._cv_sock:
                                self._replies[hdr.msg_id] = replies
                                self._cv_sock.notifyAll()
                        else:
                            self._logger.error("Unknown message reply: " + hdr.api_call)

    @property
    def connected(self):
        return self._socket is not None

    def send_msg(self, api_call_type, msg=None):
        """
        Sends a single or just a header message.
        :param str api_call_type: api call type that is set in the header message.
        :param msg: Message to be sent, if None only the header will be sent.
        :return int: Message id of the message for wait_for_result()
        """
        return self.send_msgs(api_call_type, [msg] if msg else None)

    def send_msgs(self, api_call_type, msgs=None):
        """
        Sends a list of message or just a header.
        :param str api_call_type: api call type that is set in the header message.
        :param list msgs: List of message to be sent, if None only the header will be sent.
        :return int: Message id of the message for wait_for_result()
        """
        hdr_msg = MsgHeader()
        hdr_msg.api_call = api_call_type
        hdr_msg.msg_id = self._cur_msg_id.get_and_inc()

        h_type = struct.pack("!I", 0)  # currently always 0, 32 bit
        h_reserved = struct.pack("!Q", 0)  # reserved, 64 bit

        msg_serialized = bytes()

        header_serialized = hdr_msg.SerializeToString()
        delim = encoder._VarintBytes(len(header_serialized))
        msg_serialized += delim + header_serialized

        if msgs:
            for msg in msgs:
                payload_serialized = msg.SerializeToString()
                delim = encoder._VarintBytes(len(payload_serialized))
                msg_serialized += delim + payload_serialized

        h_payload_length = len(msg_serialized)
        h_payload_length = struct.pack("!I", h_payload_length)  # 32 bit

        full_msg = h_type + h_payload_length + h_reserved + msg_serialized

        with self._slock:
            msg_len = len(full_msg)
            sent = 0
            while sent < msg_len:
                sent += self._socket.send(full_msg)
        return hdr_msg.msg_id

    def wait_for_result(self, msg_id):
        """
        This method blocks and waits for an answer to the given msg_id.
        :param int msg_id:
        :return: A list with the replies.
        """
        with self._cv_sock:
            while msg_id not in self._replies:
                if not self.connected:
                    return []
                self._cv_sock.wait(1)
            return self._replies.pop(msg_id)


class Linstor(object):
    def __init__(self, ctrl_host):
        self._ctrl_host = ctrl_host
        self._linstor_client = LinstorNetClient()
        self._logger = logging.getLogger('Linstor')

    def __del__(self):
        self.disconnect()

    def connect(self):
        self._linstor_client.connect(self._ctrl_host)
        self._linstor_client.daemon = True
        self._linstor_client.start()

    @property
    def connected(self):
        return self._linstor_client.connected

    def disconnect(self):
        self._linstor_client.disconnect()

    def node_create(
            self,
            node_name,
            node_type,
            ip,
            com_type=apiconsts.VAL_NETCOM_TYPE_PLAIN,
            port=None,
            netif_name='default'
    ):
        msg = MsgCrtNode()

        msg.node.name = node_name
        msg.node.type = node_type
        netif = msg.node.net_interfaces.add()
        netif.name = netif_name
        netif.address = ip

        if port is None:
            if com_type == apiconsts.VAL_NETCOM_TYPE_PLAIN:
                port = apiconsts.DFLT_CTRL_PORT_PLAIN \
                    if msg.node.type == apiconsts.VAL_NODE_TYPE_CTRL else apiconsts.DFLT_STLT_PORT_PLAIN
            elif com_type == apiconsts.VAL_NETCOM_TYPE_SSL:
                port = apiconsts.DFLT_CTRL_PORT_SSL
            else:
                raise LinstorError("Communication type %s has no default port" % com_type)

            netif.stlt_port = port
            netif.stlt_encryption_type = com_type

        msg_id = self._linstor_client.send_msg(apiconsts.API_CRT_NODE, msg)
        replies = self._linstor_client.wait_for_result(msg_id)
        return replies

    def node_modify(self, node_name, property_dict, delete_props=None):
        msg = MsgModNode()
        msg.node_name = node_name

        for kv in property_dict:
            lin_kv = msg.override_props.add()
            lin_kv.key = kv[0]
            lin_kv.value = kv[1]

        if delete_props:
            msg.delete_prop_keys.extend(delete_props)

        msg_id = self._linstor_client.send_msg(apiconsts.API_MOD_NODE, msg)
        replies = self._linstor_client.wait_for_result(msg_id)
        return replies

    def node_delete(self, node_name):
        msg = MsgDelNode()
        msg.node_name = node_name

        msg_id = self._linstor_client.send_msg(apiconsts.API_DEL_NODE, msg)
        replies = self._linstor_client.wait_for_result(msg_id)
        return replies

    def netinterface_create(self, node_name, interface_name, ip, port=None, com_type=None):
        msg = MsgCrtNetInterface()
        msg.node_name = node_name

        msg.net_if.name = interface_name
        msg.net_if.address = ip

        if port:
            msg.net_if.stlt_port = port
            msg.net_if.stlt_encryption_type = com_type

        msg_id = self._linstor_client.send_msg(apiconsts.API_CRT_NET_IF, msg)
        replies = self._linstor_client.wait_for_result(msg_id)
        return replies

    def netinterface_modify(self, node_name, interface_name, ip, port=None, com_type=None):
        msg = MsgModNetInterface()

        msg.node_name = node_name
        msg.net_if.name = interface_name
        msg.net_if.address = ip

        if port:
            msg.net_if.stlt_port = port
            msg.net_if.stlt_encryption_type = com_type

        msg_id = self._linstor_client.send_msg(apiconsts.API_MOD_NET_IF, msg)
        replies = self._linstor_client.wait_for_result(msg_id)
        return replies

    def netinterface_delete(self, node_name, interface_name):
        msg = MsgDelNetInterface()
        msg.node_name = node_name
        msg.net_if_name = interface_name

        msg_id = self._linstor_client.send_msg(apiconsts.API_DEL_NET_IF, msg)
        replies = self._linstor_client.wait_for_result(msg_id)
        return replies

    def node_list(self):
        msg_id = self._linstor_client.send_msgs(apiconsts.API_LST_NODE)
        replies = self._linstor_client.wait_for_result(msg_id)
        return replies[0] if replies else []

    def storage_pool_dfn_list(self):
        msg_id = self._linstor_client.send_msgs(apiconsts.API_LST_STOR_POOL_DFN)
        replies = self._linstor_client.wait_for_result(msg_id)
        return replies[0] if replies else []

    def storage_pool_list(self):
        msg_id = self._linstor_client.send_msgs(apiconsts.API_LST_STOR_POOL)
        replies = self._linstor_client.wait_for_result(msg_id)
        return replies[0] if replies else []

    def resource_dfn_list(self):
        msg_id = self._linstor_client.send_msgs(apiconsts.API_LST_RSC_DFN)
        replies = self._linstor_client.wait_for_result(msg_id)
        return replies[0] if replies else []

    def resource_list(self):
        msg_id = self._linstor_client.send_msgs(apiconsts.API_LST_RSC)
        replies = self._linstor_client.wait_for_result(msg_id)
        return replies[0] if replies else []


if __name__ == "__main__":
    lin = Linstor("linstor://127.0.0.1")
    lin.connect()
    #print(lin.node_create('testnode', apiconsts.VAL_NODE_TYPE_STLT, '10.0.0.1'))
    for x in range(1, 20):
        print(lin.node_create('testnode' + str(x), apiconsts.VAL_NODE_TYPE_STLT, '10.0.0.' + str(x)))

    for x in range(1, 20):
        print(lin.node_delete('testnode' + str(x)))
    replies = lin.storage_pool_list()
    print(replies)
    # print(lin.list_nodes())
    # print(lin.list_resources())